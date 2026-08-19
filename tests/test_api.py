import main


async def test_homepage_returns_200(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "Резюмирую" in r.text


async def test_pricing_page(client):
    r = await client.get("/pricing")
    assert r.status_code == 200


async def test_privacy_page(client):
    r = await client.get("/privacy")
    assert r.status_code == 200


async def test_contacts_page(client):
    r = await client.get("/contacts")
    assert r.status_code == 200


async def test_offer_page(client):
    r = await client.get("/offer")
    assert r.status_code == 200


async def test_me_anonymous(client):
    r = await client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["authenticated"] is False


async def test_resumes_redirects_unauthenticated(client):
    r = await client.get("/resumes", follow_redirects=False)
    assert r.status_code in (302, 303)


async def test_settings_redirects_unauthenticated(client):
    r = await client.get("/settings", follow_redirects=False)
    assert r.status_code in (302, 303)


async def test_generate_requires_auth(client):
    body = {
        "name": "Test",
        "phone": "1234567890",
        "city": "Moscow",
        "target": "Python dev",
        "experience": [],
        "education": [],
        "skills": "Python",
        "languages": "Russian",
    }
    r = await client.post("/api/generate", json=body)
    assert r.status_code == 401


async def test_logout_returns_200(client):
    r = await client.post("/auth/logout")
    assert r.status_code == 200


# ── Списание генераций при упоре в лимит хранимых резюме ──────────────────
async def _login(client, email):
    import main
    main.init_db()
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            (f"tok-{email}", email),
        )
        db.commit()
    await client.get(f"/auth/email/verify?token=tok-{email}", follow_redirects=False)
    with main.get_db() as db:
        return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


async def test_resume_limit_refunds_generation(client, monkeypatch):
    """Резюме сгенерировано, но сохранить некуда — списание должно вернуться."""
    import main

    uid = await _login(client, "limit@test.com")
    with main.get_db() as db:
        db.execute("UPDATE users SET free_left=0, paid_left=3 WHERE id=?", (uid,))
        # Забиваем хранилище под завязку, чтобы _save_resume упёрся в FREE_RESUMES
        for i in range(main.FREE_RESUMES):
            db.execute(
                "INSERT INTO resumes (user_id, resume_data) VALUES (?,?)",
                (uid, '{"name":"x"}'),
            )
        db.commit()

    async def _fake_ai(prompt):
        return '{"target_role": "Dev"}'

    monkeypatch.setattr(main, "call_ai", _fake_ai)

    r = await client.post("/api/generate", json={
        "name": "Test", "phone": "123", "city": "Moscow", "target": "Dev",
        "experience": [], "education": [], "skills": "Python", "languages": "RU",
    })
    assert r.status_code == 402
    assert r.json()["error"] == "resume_limit"
    with main.get_db() as db:
        assert db.execute("SELECT paid_left FROM users WHERE id=?", (uid,)).fetchone()["paid_left"] == 3


# ── /api/improve-text: улучшение текста списывает генерацию ──────────────────
# Раньше ручка ходила в модель, не трогая счётчик: пользователь с нулевым
# балансом получал безлимитный доступ к AI, а пара таких запросов занимала оба
# слота AI_CONCURRENCY и выключала генерацию для всех.

def _set_balance(user_id: int, free: int, paid: int = 0):
    with main.get_db() as db:
        db.execute("UPDATE users SET free_left=?, paid_left=? WHERE id=?", (free, paid, user_id))
        db.commit()


def _left(user_id: int) -> int:
    with main.get_db() as db:
        row = db.execute("SELECT free_left, paid_left FROM users WHERE id=?", (user_id,)).fetchone()
    return row["free_left"] + row["paid_left"]


async def test_improve_text_requires_auth(client):
    main.init_db()
    r = await client.post("/api/improve-text", json={"kind": "summary", "text": "текст"})
    assert r.status_code == 401


async def test_improve_text_without_balance_returns_402(client, monkeypatch):
    """Нулевой баланс — до модели дело не доходит."""
    uid = await _login(client, "empty@test.com")
    _set_balance(uid, 0, 0)
    called = {"n": 0}

    async def never(prompt):                     # pragma: no cover
        called["n"] += 1
        return "не должно вызваться"

    monkeypatch.setattr(main, "call_ai", never)
    r = await client.post("/api/improve-text", json={"kind": "summary", "text": "текст"})
    assert r.status_code == 402
    assert r.json()["error"] == "no_uses"
    assert called["n"] == 0


async def test_improve_text_deducts_one_generation(client, monkeypatch):
    uid = await _login(client, "payer2@test.com")
    _set_balance(uid, 3, 0)

    async def fake(prompt):
        return "  улучшенный текст  "

    monkeypatch.setattr(main, "call_ai", fake)
    r = await client.post("/api/improve-text", json={"kind": "summary", "text": "текст"})
    assert r.status_code == 200
    assert r.json()["improved"] == "улучшенный текст"
    assert r.json()["uses_left"] == 2
    assert _left(uid) == 2


async def test_improve_text_refunds_on_ai_failure(client, monkeypatch):
    """Модель не ответила — списание возвращаем, как в /api/match."""
    uid = await _login(client, "refund@test.com")
    _set_balance(uid, 2, 0)

    async def boom(prompt):
        raise main.HTTPException(503, "Сервис генерации недоступен")

    monkeypatch.setattr(main, "call_ai", boom)
    r = await client.post("/api/improve-text", json={"kind": "summary", "text": "текст"})
    assert r.status_code == 503
    assert _left(uid) == 2


async def test_improve_text_free_for_pro(client, monkeypatch):
    """У Pro безлимит — счётчик не трогаем."""
    uid = await _login(client, "pro@test.com")
    _set_balance(uid, 0, 0)
    with main.get_db() as db:
        db.execute("UPDATE users SET is_pro=1, pro_expires_at=datetime('now','+10 days') WHERE id=?", (uid,))
        db.commit()

    async def fake(prompt):
        return "текст для Pro"

    monkeypatch.setattr(main, "call_ai", fake)
    r = await client.post("/api/improve-text", json={"kind": "summary", "text": "текст"})
    assert r.status_code == 200
    assert _left(uid) == 0
