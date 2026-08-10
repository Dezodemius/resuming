"""Характеризационные тесты на «горячие» инварианты безопасности.

Фиксируют поведение ДО декомпозиции бэкенда, чтобы любой рефактор,
который их нарушит, падал немедленно: SSRF-защита fetch-job, подтверждение
платежа ЮKassa в вебхуке, срок жизни magic-ссылки.
"""
import pytest
from fastapi import HTTPException

import main


# ── SSRF: _assert_public_host ─────────────────────────────────────────────────
@pytest.mark.parametrize("host", [
    "127.0.0.1",        # loopback
    "10.0.0.1",         # private
    "192.168.1.1",      # private
    "169.254.169.254",  # cloud metadata (link-local)
    "::1",              # IPv6 loopback
])
def test_assert_public_host_blocks_internal(host):
    with pytest.raises(HTTPException) as exc:
        main._assert_public_host(host)
    assert exc.value.status_code == 400


def test_assert_public_host_allows_public():
    # Публичный литерал — не должен бросать (без обращения к сети)
    main._assert_public_host("8.8.8.8")


# ── Magic-link: срок жизни одноразового токена ───────────────────────────────
async def test_magic_link_expired_rejected(client):
    main.init_db()
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','-1 minute'),0)",
            ("tok-expired", "e@test.com"),
        )
        db.commit()
    r = await client.get("/auth/email/verify?token=tok-expired", follow_redirects=False)
    assert r.status_code == 200            # отрисована страница «истекла», не редирект
    assert "истекла" in r.text
    assert "session_id" not in r.headers.get("set-cookie", "")


async def test_magic_link_valid_creates_session(client):
    main.init_db()
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            ("tok-ok", "ok@test.com"),
        )
        db.commit()
    r = await client.get("/auth/email/verify?token=tok-ok", follow_redirects=False)
    assert r.status_code == 303
    assert "session_id" in r.headers.get("set-cookie", "")
    with main.get_db() as db:
        used = db.execute("SELECT used FROM magic_tokens WHERE token=?", ("tok-ok",)).fetchone()["used"]
    assert used == 1                       # токен одноразовый — помечен использованным


# ── Платёж: вебхук не выдаёт Pro без подтверждения в ЮKassa ──────────────────
class _FakeResp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self._payload = payload
    def json(self):
        return self._payload


class _FakeClient:
    """Подменяет httpx.AsyncClient: .get() всегда отдаёт заранее заданный ответ."""
    def __init__(self, payload, status=200):
        self._payload, self._status = payload, status
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, *a, **k):
        return _FakeResp(self._payload, self._status)


def _add_user(email):
    with main.get_db() as db:
        db.execute("INSERT INTO users (email) VALUES (?)", (email,))
        db.commit()
        return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def _add_payment(user_id, pay_id):
    """Строка платежа, какую создаёт /api/pay до редиректа в ЮKassa."""
    with main.get_db() as db:
        db.execute("INSERT INTO payments (user_id, pay_id, idem_key) VALUES (?,?,?)",
                   (user_id, pay_id, f"idem-{pay_id}"))
        db.commit()


def _ok_payment_resp():
    return {"status": "succeeded", "amount": {"value": main.PRO_PRICE, "currency": "RUB"}}


async def test_webhook_without_confirmation_no_pro(client, monkeypatch):
    main.init_db()
    uid = _add_user("pay-pending@test.com")
    _add_payment(uid, "p-pending")
    # ЮKassa отвечает 'pending' → платёж не подтверждён
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient({"status": "pending"}))
    r = await client.post("/api/pay/webhook", json={
        "event": "payment.succeeded",
        "object": {"id": "p-pending", "metadata": {"user_id": uid}},
    })
    assert r.json().get("ok") is False
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_with_confirmation_grants_pro(client, monkeypatch):
    main.init_db()
    uid = _add_user("pay-ok@test.com")
    _add_payment(uid, "p-ok")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_ok_payment_resp()))
    r = await client.post("/api/pay/webhook", json={
        "event": "payment.succeeded",
        "object": {"id": "p-ok", "metadata": {"user_id": uid}},
    })
    assert r.json().get("ok") is True
    with main.get_db() as db:
        row = db.execute("SELECT is_pro, pro_expires_at FROM users WHERE id=?", (uid,)).fetchone()
    assert row["is_pro"] == 1
    assert row["pro_expires_at"]


async def test_webhook_wrong_amount_no_pro(client, monkeypatch):
    """Платёж подтверждён, но сумма не совпадает с ожидаемой → Pro не выдаём."""
    main.init_db()
    uid = _add_user("pay-wrong@test.com")
    _add_payment(uid, "p-wrong")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(
        {"status": "succeeded", "amount": {"value": "1.00", "currency": "RUB"}}))
    r = await client.post("/api/pay/webhook", json={
        "event": "payment.succeeded",
        "object": {"id": "p-wrong", "metadata": {"user_id": uid}},
    })
    assert r.json().get("ok") is False
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_unknown_payment_no_pro(client, monkeypatch):
    """Эндпоинт публичный: по чужому/выдуманному pay_id Pro не выдаём.

    Без строки в payments прежний код не мог отметить платёж обработанным,
    поэтому повторы вебхука бесконечно продлевали Pro.
    """
    main.init_db()
    uid = _add_user("pay-unknown@test.com")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_ok_payment_resp()))
    r = await client.post("/api/pay/webhook", json={
        "event": "payment.succeeded",
        "object": {"id": "p-never-created", "metadata": {"user_id": uid}},
    })
    assert r.json() == {"ok": False, "reason": "unknown_payment"}
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_ignores_user_id_from_body(client, monkeypatch):
    """metadata.user_id из тела запроса не влияет: Pro идёт плательщику из payments."""
    main.init_db()
    payer = _add_user("payer@test.com")
    attacker = _add_user("attacker@test.com")
    _add_payment(payer, "p-spoof")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_ok_payment_resp()))
    r = await client.post("/api/pay/webhook", json={
        "event": "payment.succeeded",
        "object": {"id": "p-spoof", "metadata": {"user_id": attacker}},
    })
    assert r.json().get("ok") is True
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (attacker,)).fetchone()["is_pro"] == 0
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (payer,)).fetchone()["is_pro"] == 1


async def test_webhook_replay_does_not_extend_pro(client, monkeypatch):
    """Повторный вебхук по обработанному платежу не продлевает Pro второй раз."""
    main.init_db()
    uid = _add_user("pay-replay@test.com")
    _add_payment(uid, "p-replay")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_ok_payment_resp()))
    payload = {"event": "payment.succeeded",
               "object": {"id": "p-replay", "metadata": {"user_id": uid}}}
    await client.post("/api/pay/webhook", json=payload)
    with main.get_db() as db:
        first_exp = db.execute("SELECT pro_expires_at FROM users WHERE id=?", (uid,)).fetchone()["pro_expires_at"]
    await client.post("/api/pay/webhook", json=payload)
    with main.get_db() as db:
        second_exp = db.execute("SELECT pro_expires_at FROM users WHERE id=?", (uid,)).fetchone()["pro_expires_at"]
    assert first_exp == second_exp


# ── Платёж: понятная ошибка при невыключенной/ненастроенной ЮKassa ──────────
async def test_pay_requires_auth(client):
    """Без сессии /api/pay не пускает к платежу."""
    main.init_db()
    r = await client.post("/api/pay", json={})
    assert r.status_code == 401


async def test_pay_unconfigured_returns_503(client, monkeypatch):
    """Без ключей ЮKassa /api/pay отдаёт понятную 503 и не плодит висячих строк."""
    main.init_db()
    monkeypatch.setattr(main, "YOKASSA_SHOP", "")
    monkeypatch.setattr(main, "YOKASSA_SECRET", "")
    # авторизуемся через magic-ссылку — клиент сохранит session-cookie
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            ("tok-pay", "payer@test.com"),
        )
        db.commit()
    await client.get("/auth/email/verify?token=tok-pay", follow_redirects=False)

    r = await client.post("/api/pay", json={})
    assert r.status_code == 503
    assert "недоступна" in r.json()["detail"].lower()
    with main.get_db() as db:
        assert db.execute("SELECT COUNT(*) AS c FROM payments").fetchone()["c"] == 0
