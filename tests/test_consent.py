"""Согласие на передачу персональных данных AI-провайдеру (ст. 12 152-ФЗ).

Генерация уносит имя, контакты и опыт внешнему провайдеру за пределы РФ.
Такое согласие обязано быть отдельным подтверждённым действием, а не выводом
из факта входа, — и обязано проверяться на сервере: галочка в форме защищает
только от честного браузера, а данные наружу уносит ручка.

Проверяем главное свойство: пока отметки нет, модель не вызывается ни на одном
из путей, которые к ней ведут, и списание не происходит.
"""
import json

import main


async def _login(client, email):
    """Вход без согласия — ровно то состояние, в котором приходит новый человек."""
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


def _save_profile(user_id: int):
    profile = {
        "name": "Иван Иванов", "phone": "+7 900 000-00-00", "city": "Москва",
        "linkedin": "", "skills": "Python", "languages": "RU",
        "experience": [], "education": [],
    }
    with main.get_db() as db:
        db.execute(
            "INSERT INTO profiles (user_id, data) VALUES (?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data",
            (user_id, json.dumps(profile, ensure_ascii=False)),
        )
        db.commit()


def _never_called(monkeypatch, calls):
    async def fake(prompt):
        calls["n"] += 1
        return '{"target_role": "Dev"}'

    monkeypatch.setattr(main, "call_ai", fake)


_GENERATE_BODY = {
    "name": "Иван", "phone": "+7 900 000-00-00", "city": "Москва", "target": "Dev",
    "experience": [], "education": [], "skills": "Python", "languages": "RU",
}


# ── Зарегистрированный пользователь ───────────────────────────────────────
async def test_generation_without_consent_does_not_reach_the_model(client, monkeypatch):
    """Отказ до вызова модели: данные наружу не ушли, генерация не списана."""
    calls = {"n": 0}
    _never_called(monkeypatch, calls)
    uid = await _login(client, "noconsent@test.com")

    r = await client.post("/api/generate", json=_GENERATE_BODY)

    assert r.status_code == 403
    # Редакция в ответе — чтобы клиент знал, на что именно спрашивать согласие.
    assert r.json() == {"error": "consent_required", "rev": main.AI_CONSENT_REV}
    assert calls["n"] == 0, "без согласия данные не должны уходить провайдеру"
    with main.get_db() as db:
        left = db.execute("SELECT free_left FROM users WHERE id=?", (uid,)).fetchone()
    assert left["free_left"] == main.FREE_USES, "отказ — не повод списывать генерацию"


async def test_async_match_without_consent_is_refused(client, monkeypatch):
    """Асинхронный путь идёт мимо общей обёртки — у него своя проверка."""
    calls = {"n": 0}
    _never_called(monkeypatch, calls)
    uid = await _login(client, "noconsent-async@test.com")
    _save_profile(uid)

    r = await client.post("/api/match/start", json={"job_text": "Backend-разработчик, Python" * 3})

    assert r.status_code == 403
    assert r.json()["error"] == "consent_required"
    assert calls["n"] == 0


async def test_improve_text_without_consent_is_refused(client, monkeypatch):
    """Редактор уносит провайдеру такой же кусок резюме, как и генерация."""
    calls = {"n": 0}
    _never_called(monkeypatch, calls)
    await _login(client, "noconsent-improve@test.com")

    r = await client.post("/api/improve-text", json={"kind": "summary", "text": "Работал"})

    assert r.status_code == 403
    assert r.json()["error"] == "consent_required"
    assert calls["n"] == 0


async def test_consent_unlocks_generation_and_is_recorded(client, monkeypatch):
    """Подтверждение хранится с моментом и редакцией, а не просто флагом."""
    calls = {"n": 0}
    _never_called(monkeypatch, calls)
    uid = await _login(client, "consent@test.com")

    assert (await client.get("/api/me")).json()["ai_consent"] is False

    ok = await client.post("/api/consent")
    assert ok.status_code == 200
    assert ok.json()["rev"] == main.AI_CONSENT_REV

    with main.get_db() as db:
        row = db.execute(
            "SELECT ai_consent_at, ai_consent_rev FROM users WHERE id=?", (uid,)
        ).fetchone()
        logged = db.execute(
            "SELECT COUNT(*) FROM usage_events WHERE event='ai_consent' AND user_id=?", (uid,)
        ).fetchone()[0]
    assert row["ai_consent_at"], "момент подтверждения обязан сохраниться"
    assert row["ai_consent_rev"] == main.AI_CONSENT_REV
    assert logged == 1, "подтверждение должно оставаться в журнале"

    assert (await client.get("/api/me")).json()["ai_consent"] is True

    r = await client.post("/api/generate", json=_GENERATE_BODY)
    assert r.status_code == 200
    assert calls["n"] == 1


async def test_consent_to_previous_revision_does_not_count(client, monkeypatch):
    """Изменились условия передачи — человек соглашался на другое.

    Без сверки редакции старая отметка молча покрывала бы новую редакцию, и
    смысл в самой константе пропал бы.
    """
    calls = {"n": 0}
    _never_called(monkeypatch, calls)
    uid = await _login(client, "oldrev@test.com")
    with main.get_db() as db:
        db.execute(
            "UPDATE users SET ai_consent_at=datetime('now'), ai_consent_rev='2020-01-01'"
            " WHERE id=?", (uid,)
        )
        db.commit()

    r = await client.post("/api/generate", json=_GENERATE_BODY)

    assert r.status_code == 403
    assert r.json()["error"] == "consent_required"
    assert calls["n"] == 0


async def test_consent_requires_auth(client):
    main.init_db()
    r = await client.post("/api/consent")
    assert r.status_code == 401


# ── Аноним ────────────────────────────────────────────────────────────────
async def test_anonymous_preview_without_consent_is_refused(client, monkeypatch):
    """У анонима согласие едет в запросе — без него данные не уходят.

    И не расходуется анонимный лимит: отказ по формальной причине не должен
    съедать бесплатную попытку.
    """
    main.init_db()
    calls = {"n": 0}
    _never_called(monkeypatch, calls)

    r = await client.post(
        "/api/generate-preview",
        json={"kind": "general", "profile": {"name": "Иван"}, "target_role": "QA"},
        headers={"X-Real-IP": "203.0.113.9"},
    )

    assert r.status_code == 403
    assert r.json()["error"] == "consent_required"
    assert calls["n"] == 0
    with main.get_db() as db:
        used = db.execute("SELECT COUNT(*) FROM anon_usage").fetchone()[0]
    assert used == 0, "отказ до генерации не должен тратить анонимную попытку"


async def test_anonymous_consent_is_logged_with_revision(client, monkeypatch):
    """След согласия анонима — единственный: строки в users у него нет."""
    main.init_db()
    calls = {"n": 0}
    _never_called(monkeypatch, calls)

    r = await client.post(
        "/api/generate-preview",
        json={"kind": "general", "profile": {"name": "Иван"}, "target_role": "QA",
              "consent": True},
        headers={"X-Real-IP": "203.0.113.10"},
    )

    assert r.status_code == 200
    assert calls["n"] == 1
    with main.get_db() as db:
        row = db.execute(
            "SELECT anon_id, meta FROM usage_events WHERE event='ai_consent'"
        ).fetchone()
    assert row is not None, "подтверждение анонима должно оставаться в журнале"
    assert row["anon_id"]
    assert json.loads(row["meta"])["rev"] == main.AI_CONSENT_REV


# ── Разметка ──────────────────────────────────────────────────────────────
async def test_consent_modal_is_present_where_generation_starts(client):
    """Согласие должно быть чем дать: модалка и её логика — на обеих страницах,
    откуда уходит вызов модели."""
    main.init_db()
    r = await client.get("/new")
    assert r.status_code == 200
    assert 'id="modal-consent"' in r.text
    assert 'id="consent-box"' in r.text
    assert "/static/consent.js" in r.text
    assert "трансграничную передачу" in r.text
    assert f'data-rev="{main.AI_CONSENT_REV}"' in r.text


async def test_consent_text_is_shared_between_generator_and_editor(client):
    """Один текст согласия на обе страницы: разошедшиеся формулировки — это
    два разных согласия вместо одного."""
    uid = await _login(client, "editor@test.com")
    with main.get_db() as db:
        rid = db.execute(
            "INSERT INTO resumes (user_id, resume_data) VALUES (?,?)",
            (uid, '{"target_role": "Dev"}'),
        ).lastrowid
        db.commit()

    r = await client.get(f"/resumes/{rid}")
    assert r.status_code == 200
    assert 'id="modal-consent"' in r.text
    assert "/static/consent.js" in r.text
