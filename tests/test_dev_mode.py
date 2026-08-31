"""Дев-режим: контур, который на проде обязан отсутствовать.

Ручки /dev и /api/dev/* дают вход по любой почте и любой тариф без оплаты,
поэтому проверяем не столько их пользу, сколько то, что выключенными они
действительно выключены.
"""
import pytest

import config
import main


@pytest.fixture
def dev_on(monkeypatch):
    """Включает дев-режим на время теста.

    Патчим main.DEV_MODE, а не переменную окружения: config читает её один раз
    при импорте, и перезагрузка модуля посреди сессии тестов утащила бы за
    собой DB_PATH, подменённый фикстурами.
    """
    monkeypatch.setattr(main, "DEV_MODE", True)


# ── Выключенный режим ───────────────────────────────────────────────────────
def test_dev_mode_off_by_default():
    """Без переменной окружения — выключен. Это и есть состояние прода."""
    assert config.DEV_MODE is False


@pytest.mark.parametrize("raw", ["", "0", "no", "off", "false", "маybe", "2"])
def test_dev_mode_needs_explicit_yes(raw):
    assert config._dev_mode_enabled(raw, False) is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "  ON  ", "True"])
def test_dev_mode_accepts_common_yes(raw):
    assert config._dev_mode_enabled(raw, False) is True


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on"])
def test_dev_mode_never_turns_on_in_prod(raw):
    """Главная проверка файла: на боевом контуре переменная не работает.

    Иначе случайно скопированный на прод .env открыл бы вход любым аккаунтом.
    """
    assert config._dev_mode_enabled(raw, True) is False


# Тела намеренно валидные: с мусором ручка ответила бы 422 ещё на разборе
# схемы, и тест перестал бы что-либо говорить про сам режим.
@pytest.mark.parametrize("path,body", [
    ("/dev", None),
    ("/api/dev/login", {"email": "x@example.com"}),
    ("/api/dev/grant", {"plan": "pro"}),
])
async def test_dev_routes_are_invisible_when_off(client, db, path, body):
    """404, а не 403: снаружи не должно быть видно даже факта существования."""
    r = await (client.get(path) if body is None else client.post(path, json=body))
    assert r.status_code == 404


# ── Включённый режим ────────────────────────────────────────────────────────
async def test_dev_page_opens(client, db, dev_on):
    r = await client.get("/dev")
    assert r.status_code == 200
    assert "DEV-пульт" in r.text


async def test_dev_login_creates_account_and_session(client, db, dev_on):
    r = await client.post("/api/dev/login", json={"email": "Tester@Example.com"})
    assert r.status_code == 200
    assert r.json()["email"] == "tester@example.com"

    me = (await client.get("/api/me")).json()
    assert me["authenticated"] is True
    assert me["email"] == "tester@example.com"


async def test_dev_login_reuses_account(client, db, dev_on):
    """Повторный вход той же почтой — тот же аккаунт, а не второй такой же."""
    first = (await client.post("/api/dev/login", json={"email": "a@example.com"})).json()
    second = (await client.post("/api/dev/login", json={"email": "a@example.com"})).json()
    assert first["user_id"] == second["user_id"]


async def test_grant_pro(client, db, dev_on):
    await client.post("/api/dev/login", json={"email": "pro@example.com"})
    r = await client.post("/api/dev/grant", json={"plan": "pro"})
    assert r.status_code == 200
    assert r.json()["state"]["is_pro"] is True
    assert (await client.get("/api/me")).json()["is_pro"] is True


async def test_grant_pro_honours_days(client, db, dev_on):
    await client.post("/api/dev/login", json={"email": "pro2@example.com"})
    r = await client.post("/api/dev/grant", json={"plan": "pro", "value": 1})
    assert r.json()["state"]["is_pro"] is True


async def test_grant_pack_adds_generations(client, db, dev_on):
    await client.post("/api/dev/login", json={"email": "pack@example.com"})
    before = (await client.get("/api/me")).json()["paid_left"]
    r = await client.post("/api/dev/grant", json={"plan": "pack", "value": 7})
    assert r.json()["state"]["paid_left"] == before + 7


async def test_grant_empty_shows_paywall(client, db, dev_on):
    """Обнуление счётчиков — так проверяется поведение «генерации кончились»."""
    await client.post("/api/dev/login", json={"email": "empty@example.com"})
    await client.post("/api/dev/grant", json={"plan": "pro"})
    state = (await client.post("/api/dev/grant", json={"plan": "empty"})).json()["state"]
    assert state == {"is_pro": False, "pro_expires_at": None, "free_left": 0, "paid_left": 0}


async def test_grant_free_restores_newcomer(client, db, dev_on):
    await client.post("/api/dev/login", json={"email": "free@example.com"})
    await client.post("/api/dev/grant", json={"plan": "empty"})
    state = (await client.post("/api/dev/grant", json={"plan": "free"})).json()["state"]
    assert state["free_left"] == config.FREE_USES
    assert state["paid_left"] == 0
    assert state["is_pro"] is False


async def test_grant_reset_usage_clears_events(client, db, dev_on):
    """Квота Pro считается по usage_events — отмотать её можно только так."""
    r = await client.post("/api/dev/login", json={"email": "usage@example.com"})
    uid = r.json()["user_id"]

    db.execute("INSERT INTO usage_events (user_id, event) VALUES (?, 'generate')", (uid,))
    db.commit()

    assert (await client.post("/api/dev/grant", json={"plan": "reset_usage"})).status_code == 200

    assert db.execute(
        "SELECT COUNT(*) FROM usage_events WHERE user_id=?", (uid,)
    ).fetchone()[0] == 0


async def test_grant_rejects_unknown_plan(client, db, dev_on):
    await client.post("/api/dev/login", json={"email": "x@example.com"})
    r = await client.post("/api/dev/grant", json={"plan": "unlimited-forever"})
    assert r.status_code == 400


async def test_grant_requires_session(client, db, dev_on):
    """Даже на стенде тариф выдаётся конкретному аккаунту, а не «всем»."""
    r = await client.post("/api/dev/grant", json={"plan": "pro"})
    assert r.status_code == 401
