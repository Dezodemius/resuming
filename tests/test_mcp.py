import json

import pytest

import main


async def test_mcp_token_requires_auth(client):
    r = await client.post("/api/mcp-token")
    assert r.status_code == 401


async def test_mcp_token_issued_with_session(client):
    main.init_db()
    with main.get_db() as db:
        db.execute("INSERT INTO users (email) VALUES (?)", ("mcp@test.com",))
        db.commit()
        uid = db.execute(
            "SELECT id FROM users WHERE email=?", ("mcp@test.com",)
        ).fetchone()["id"]
        sid = main._create_session(db, uid)

    client.cookies.set("session_id", sid)
    r = await client.post("/api/mcp-token")
    assert r.status_code == 200
    token = r.json()["token"]
    assert token

    with main.get_db() as db:
        row = db.execute(
            "SELECT user_id FROM api_tokens WHERE token=?", (token,)
        ).fetchone()
    assert row is not None
    assert row["user_id"] == uid

    # повторная выдача заменяет старый токен (один активный токен на пользователя)
    r2 = await client.post("/api/mcp-token")
    assert r2.status_code == 200
    with main.get_db() as db:
        cnt = db.execute(
            "SELECT COUNT(*) FROM api_tokens WHERE user_id=?", (uid,)
        ).fetchone()[0]
    assert cnt == 1


async def test_mcp_endpoint_without_auth_is_not_5xx(client):
    # ASGITransport не запускает lifespan — поднимаем session manager вручную
    async with main.app.router.lifespan_context(main.app):
        # Главный инвариант: MCP смонтирован и не падает 5xx.
        # Конкретный 4xx зависит от версии mcp-транспорта: без заголовка
        # Accept: text/event-stream старые версии отвечают 406, новые (с
        # проверкой Host) — 421 на тестовый хост. Принимаем оба.
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert r.status_code < 500
        assert r.status_code in (406, 421)


async def test_mcp_adapt_resume_refunds_generation_when_resume_limit_hit(db, monkeypatch):
    db.execute(
        "INSERT INTO users (email, free_left, paid_left) VALUES (?,?,?)",
        ("mcp-limit@test.com", 0, 1),
    )
    uid = db.execute(
        "SELECT id FROM users WHERE email=?", ("mcp-limit@test.com",)
    ).fetchone()["id"]
    profile = {
        "name": "Test User",
        "phone": "",
        "city": "",
        "linkedin": "",
        "skills": "Python",
        "languages": "",
        "experience": [],
        "education": [],
    }
    db.execute(
        "INSERT INTO profiles (user_id, data) VALUES (?,?)",
        (uid, json.dumps(profile, ensure_ascii=False)),
    )
    for i in range(main.FREE_RESUMES):
        db.execute(
            "INSERT INTO resumes (user_id, resume_data, kind) VALUES (?,?,?)",
            (uid, json.dumps({"name": f"Resume {i}"}), "matched"),
        )
    db.commit()

    async def fake_call_ai(_prompt):
        return json.dumps({"name": "Test User", "target_role": "Developer"})

    monkeypatch.setattr(main, "_mcp_user", lambda _ctx: {"id": uid})
    monkeypatch.setattr(main, "call_ai", fake_call_ai)

    with pytest.raises(ValueError, match="resume_limit"):
        await main.adapt_resume("Senior Python developer with production systems", object())

    row = db.execute("SELECT paid_left FROM users WHERE id=?", (uid,)).fetchone()
    assert row["paid_left"] == 1
