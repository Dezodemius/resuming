from uuid import UUID

import pytest
from fastapi import HTTPException

import main


@pytest.mark.asyncio
async def test_site_consent_is_signed_and_gates_tracking(client, db):
    refused = await client.post("/api/track", json={"event": "landing_view"})
    assert refused.json() == {"ok": False}

    accepted = await client.post("/api/site-consent", json={"choice": "analytics"})
    assert accepted.json() == {"choice": "analytics", "rev": main.COOKIE_CONSENT_REV}
    cookie = accepted.cookies["site_consent"]
    assert main._verify_anon(cookie) is None, "site consent не должен быть anon identifier"
    revision, document_hash, choice, consent_id, _signature = cookie.split(".")
    assert (revision, document_hash, choice) == (
        main.COOKIE_CONSENT_REV,
        main.COOKIE_CONSENT_HASH,
        "analytics",
    )
    UUID(consent_id)
    assert cookie == main._sign_site_consent("analytics", consent_id)

    tracked = await client.post("/api/track", json={"event": "landing_view"})
    assert tracked.json() == {"ok": True}
    row = db.execute(
        "SELECT action, document_rev, document_hash, correlation_id FROM legal_events "
        "WHERE purpose='cookie_analytics'"
    ).fetchone()
    assert dict(row) == {
        "action": "accepted",
        "document_rev": main.COOKIE_CONSENT_REV,
        "document_hash": main.COOKIE_CONSENT_HASH,
        "correlation_id": consent_id,
    }

    withdrawn = await client.post("/api/site-consent", json={"choice": "necessary"})
    assert withdrawn.json()["choice"] == "necessary"
    assert (await client.post("/api/track", json={"event": "landing_view"})).json() == {"ok": False}
    assert db.execute(
        "SELECT action FROM legal_events WHERE purpose='cookie_analytics' ORDER BY id DESC"
    ).fetchone()["action"] == "withdrawn"


@pytest.mark.asyncio
async def test_site_consent_is_invalidated_when_document_hash_changes(client, db, monkeypatch):
    await client.post("/api/site-consent", json={"choice": "analytics"})
    monkeypatch.setattr(main, "COOKIE_CONSENT_HASH", "0" * 64)

    tracked = await client.post("/api/track", json={"event": "landing_view"})

    assert tracked.json() == {"ok": False}


@pytest.mark.asyncio
async def test_email_terms_are_required_and_carried_to_verify(client, db, monkeypatch):
    rejected = await client.post("/auth/email/request", json={"email": "person@example.com"})
    assert rejected.status_code == 400

    async def no_smtp(*_args):
        return None

    monkeypatch.setattr(main, "_send_magic_email", no_smtp)
    requested = await client.post(
        "/auth/email/request", json={"email": "person@example.com", "terms_accepted": True}
    )
    assert requested.status_code == 200
    token = db.execute("SELECT token FROM magic_tokens WHERE email='person@example.com'").fetchone()["token"]
    verified = await client.get(f"/auth/email/verify?token={token}", follow_redirects=False)
    assert verified.status_code == 303
    user = db.execute("SELECT terms_rev, terms_hash FROM users WHERE email='person@example.com'").fetchone()
    assert dict(user) == {"terms_rev": main.TERMS_REV, "terms_hash": main.TERMS_HASH}
    event = db.execute("SELECT purpose, action FROM legal_events WHERE user_id=(SELECT id FROM users WHERE email='person@example.com')").fetchone()
    assert dict(event) == {"purpose": "user_terms", "action": "accepted"}


@pytest.mark.asyncio
async def test_oauth_start_requires_explicit_terms(client, monkeypatch):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-id")
    assert (await client.get("/auth/yandex", follow_redirects=False)).status_code == 400
    started = await client.get("/auth/yandex?terms=1", follow_redirects=False)
    assert started.status_code == 302
    assert "oauth_terms_yandex" in started.cookies


@pytest.mark.asyncio
async def test_oauth_linking_reuses_current_terms_without_faking_new_acceptance(
    client, monkeypatch
):
    main.init_db()
    with main.get_db() as database:
        database.execute(
            "INSERT INTO users (email, terms_accepted_at, terms_rev, terms_hash) "
            "VALUES (?,datetime('now'),?,?)",
            ("linked@example.com", main.TERMS_REV, main.TERMS_HASH),
        )
        user_id = database.execute(
            "SELECT id FROM users WHERE email='linked@example.com'"
        ).fetchone()["id"]
        session_id = main._create_session(database, user_id)
    client.cookies.set("session_id", session_id)
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-id")

    started = await client.get("/auth/yandex", follow_redirects=False)

    assert started.status_code == 302
    state = started.cookies["ya_state"]
    context = main._oauth_terms_context(
        main.Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/",
                "headers": [
                    (
                        b"cookie",
                        (
                            "oauth_terms_yandex="
                            + started.cookies["oauth_terms_yandex"]
                        ).encode("ascii"),
                    )
                ],
                "query_string": b"",
                "server": ("test", 80),
                "client": ("test", 1),
                "scheme": "http",
            }
        ),
        "yandex",
        state,
    )
    assert context and context["action"] == "existing"


@pytest.mark.asyncio
async def test_html_with_consent_state_is_not_shared_by_cache(client):
    response = await client.get("/privacy")

    assert response.headers["cache-control"] == "private, no-store"
    assert "cookie" in response.headers["vary"].lower()


@pytest.mark.asyncio
async def test_external_ai_is_fail_closed_without_deployment_confirmation(monkeypatch, db):
    monkeypatch.setattr(main, "OLLAMA_URL", "https://8.8.8.8")
    monkeypatch.setattr(main, "AI_EXTERNAL_TRANSFER_CONFIRMED", False)
    with pytest.raises(HTTPException) as error:
        await main.call_ai("sensitive prompt")
    assert error.value.status_code == 503
    assert error.value.detail == "Внешняя AI-генерация временно недоступна."
    assert db.execute("SELECT COUNT(*) FROM ai_prompt_buffer").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_external_ai_prompt_buffer_is_deleted_after_call(monkeypatch, db):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(main, "OLLAMA_URL", "https://8.8.8.8")
    monkeypatch.setattr(main, "AI_EXTERNAL_TRANSFER_CONFIRMED", True)
    monkeypatch.setattr(main, "AI_PROVIDER_NAME", "Test AI")
    monkeypatch.setattr(main, "AI_PROVIDER_COUNTRY", "Testland")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **_kwargs: Client())
    assert await main.call_ai("sensitive prompt") == "ok"
    assert db.execute("SELECT COUNT(*) FROM ai_prompt_buffer").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unavailable_external_ai_cannot_record_false_consent(client, db, monkeypatch):
    db.execute("INSERT INTO users (email) VALUES ('blocked-ai@example.com')")
    user_id = db.execute(
        "SELECT id FROM users WHERE email='blocked-ai@example.com'"
    ).fetchone()["id"]
    session_id = main._create_session(db, user_id)
    db.commit()
    client.cookies.set("session_id", session_id)
    monkeypatch.setattr(main, "OLLAMA_URL", "https://8.8.8.8")
    monkeypatch.setattr(main, "AI_PROVIDER_NAME", "")
    monkeypatch.setattr(main, "AI_PROVIDER_COUNTRY", "")
    monkeypatch.setattr(main, "AI_EXTERNAL_TRANSFER_CONFIRMED", False)

    response = await client.post(
        "/api/consent",
        json={
            "document_rev": main.AI_CONSENT_REV,
            "document_hash": main._current_ai_consent_hash(),
        },
    )

    assert response.status_code == 503
    assert db.execute(
        "SELECT COUNT(*) FROM legal_events WHERE purpose='ai_external_transfer'"
    ).fetchone()[0] == 0
