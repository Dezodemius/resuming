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
        # без заголовка Accept: text/event-stream транспорт отвечает 406
        r = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1})
        assert r.status_code < 500
        assert r.status_code == 406
