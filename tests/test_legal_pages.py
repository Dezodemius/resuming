"""Регрессии правовых страниц и договорного подтверждения при входе."""
import pytest

import main


async def _capture_magic_email(client, monkeypatch):
    """Запускает реальную ручку без отправки письма и возвращает её token."""
    async def sent(_email: str, _token: str):
        return None

    monkeypatch.setattr(main, "_send_magic_email", sent)
    response = await client.post(
        "/auth/email/request",
        json={"email": "terms-flow@test.com", "terms_accepted": True},
    )
    assert response.status_code == 200, response.text
    with main.get_db() as db:
        return db.execute(
            "SELECT token FROM magic_tokens WHERE email=?", ("terms-flow@test.com",)
        ).fetchone()[0]


@pytest.mark.parametrize(
    ("path", "heading"),
    [
        ("/terms", "Пользовательское соглашение"),
        ("/privacy", "Политика конфиденциальности"),
        ("/ai-consent", "Согласие"),
        ("/offer", "Публичная оферта"),
    ],
)
async def test_legal_routes_are_public_and_render_their_document(client, path, heading):
    response = await client.get(path)

    assert response.status_code == 200, response.text
    assert heading in response.text


async def test_privacy_page_documents_cookie_controls_and_metrika(client):
    response = await client.get("/privacy")
    text = response.text.lower()

    assert "cookie" in text
    assert "яндекс" in text and "метрик" in text
    assert "настройк" in text
    assert 'id="site-consent-banner"' in response.text
    assert "mc.yandex.ru/metrika/tag.js" not in response.text
    assert "mc.yandex.ru/watch/" not in response.text


async def test_email_start_requires_terms_but_accepted_flow_keeps_auth_usable(client, monkeypatch):
    """Server-side enforcement must not make a compliant magic-link login impossible."""
    main.init_db()
    rejected = await client.post(
        "/auth/email/request", json={"email": "terms-required@test.com"}
    )
    assert rejected.status_code in (400, 403, 422), rejected.text
    with main.get_db() as db:
        assert db.execute("SELECT COUNT(*) FROM magic_tokens").fetchone()[0] == 0

    token = await _capture_magic_email(client, monkeypatch)
    verified = await client.get(f"/auth/email/verify?token={token}", follow_redirects=False)

    assert verified.status_code == 303
    assert "session_id" in verified.cookies
    session_header = next(
        value for value in verified.headers.get_list("set-cookie")
        if value.startswith("session_id=")
    )
    assert "httponly" in session_header.lower()
    assert "samesite=lax" in session_header.lower()


@pytest.mark.parametrize(
    ("route", "enabled", "client_id", "host"),
    [
        ("/auth/yandex", "YANDEX_LOGIN_ENABLED", "YANDEX_CLIENT_ID", "oauth.yandex.ru"),
        ("/auth/vk", "VK_LOGIN_ENABLED", "VK_CLIENT_ID", "id.vk.com"),
        ("/auth/mailru", "MAILRU_LOGIN_ENABLED", "MAILRU_CLIENT_ID", "oauth.mail.ru"),
    ],
)
async def test_oauth_start_requires_terms_and_keeps_state_cookie_for_accepted_flow(
    client, monkeypatch, route, enabled, client_id, host
):
    """OAuth state остаётся технически необходимой cookie, но только после terms=1."""
    monkeypatch.setattr(main, enabled, True)
    monkeypatch.setattr(main, client_id, "test-client-id")
    monkeypatch.setattr(main, "APP_URL", "http://test")

    rejected = await client.get(route, follow_redirects=False)
    assert rejected.status_code in (400, 403, 422), rejected.text
    assert "state" not in rejected.headers.get("set-cookie", "").lower()

    accepted = await client.get(f"{route}?terms=1", follow_redirects=False)
    assert accepted.status_code == 302, accepted.text
    assert host in accepted.headers["location"]
    assert "state" in accepted.headers.get("set-cookie", "").lower()
    assert "httponly" in accepted.headers["set-cookie"].lower()
    assert "samesite=lax" in accepted.headers["set-cookie"].lower()
