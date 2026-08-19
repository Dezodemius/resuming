"""Характеризационные тесты на «горячие» инварианты безопасности.

Фиксируют поведение ДО декомпозиции бэкенда, чтобы любой рефактор,
который их нарушит, падал немедленно: SSRF-защита fetch-job, подтверждение
платежа Робокассы в вебхуке, срок жизни magic-ссылки.
"""
import hashlib
import xml.etree.ElementTree as ET

import httpx
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


# ── Платёж: вебхук не выдаёт Pro без подтверждения через OpStateExt ──────────
_TEST_PASSWORD2 = "test-password2"


class _FakeResp:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text
    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeClient:
    """Подменяет httpx.AsyncClient: .get() всегда отдаёт заранее заданный XML-ответ
    OpStateExt и запоминает (url, kwargs) каждого вызова в self.calls."""
    def __init__(self, text, status=200):
        self._text, self._status = text, status
        self.calls = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def get(self, url, **kw):
        self.calls.append((url, kw))
        return _FakeResp(self._text, self._status)


def _add_user(email):
    with main.get_db() as db:
        db.execute("INSERT INTO users (email) VALUES (?)", (email,))
        db.commit()
        return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


def _add_payment(user_id, pay_id):
    """Строка платежа, какую создаёт /api/pay до редиректа в Робокассу (InvId=pay_id)."""
    with main.get_db() as db:
        db.execute("INSERT INTO payments (user_id, pay_id, idem_key) VALUES (?,?,?)",
                   (user_id, pay_id, f"idem-{pay_id}"))
        db.commit()


def _opstate_xml(code, out_sum=None):
    """Фейковый XML-ответ OpStateExt. code=100 — оплачено, любой другой — нет."""
    out_sum = main.PRO_PRICE if out_sum is None else out_sum
    return (
        '<?xml version="1.0"?>'
        '<OperationStateResponse xmlns="http://merchant.roboxchange.com/WebService/">'
        '<Result><Code>0</Code></Result>'
        f'<State><Code>{code}</Code></State>'
        f'<Info><OutSum>{out_sum}</OutSum></Info>'
        '</OperationStateResponse>'
    )


def _webhook_form(inv_id, out_sum=None, password2=_TEST_PASSWORD2, extra=None):
    """Form-данные ResultURL с корректной подписью для (out_sum, inv_id, password2)."""
    out_sum = main.PRO_PRICE if out_sum is None else out_sum
    signature = main._robokassa_signature(out_sum, inv_id, password2)
    form = {"OutSum": out_sum, "InvId": inv_id, "SignatureValue": signature}
    if extra:
        form.update(extra)
    return form


# ── Прямые юнит-тесты платёжных хелперов (не только через /api/pay/webhook) ──
# Вебхук-тесты ниже мокают httpx целиком и не смотрят, с какими параметрами
# он был вызван, — мутации внутри _robokassa_confirmed (не тот URL, не тот
# ключ params, перепутанный порядок аргументов подписи) не меняли бы исход
# теста, значит не были бы пойманы. Эти тесты бьют по самим хелперам напрямую.
def test_robokassa_signature_is_md5_of_colon_joined_parts():
    assert main._robokassa_signature("shop", "399.00", "12", "pass1") == \
        hashlib.md5(b"shop:399.00:12:pass1").hexdigest()


def test_robokassa_signature_order_matters():
    assert main._robokassa_signature("a", "b") != main._robokassa_signature("b", "a")


def test_strip_xml_ns_allows_lookup_by_local_name():
    xml = ('<Root xmlns="http://merchant.roboxchange.com/WebService/">'
           '<State><Code>100</Code></State></Root>')
    root = main._strip_xml_ns(ET.fromstring(xml))
    assert root.findtext(".//State/Code") == "100"


def test_strip_xml_ns_splits_only_on_first_brace():
    """Реальные namespace-теги содержат ровно одну '}', так что split(maxsplit=1),
    unlimited split и rsplit(maxsplit=1) дают одинаковый результат на них — этот
    тест бьёт по самому алгоритму искусственным тегом с двумя '}', где они расходятся."""
    el = ET.Element("{a}b}c")
    main._strip_xml_ns(el)
    assert el.tag == "b}c"


async def test_robokassa_confirmed_sends_correct_params(monkeypatch):
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "shop")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "pass2")
    fake = _FakeClient(_opstate_xml("100"))
    client_kwargs = {}

    def factory(*a, **k):
        client_kwargs.update(k)
        return fake

    monkeypatch.setattr(main.httpx, "AsyncClient", factory)
    result = await main._robokassa_confirmed("42", main.PRO_PRICE)
    assert result is True
    assert client_kwargs == {"timeout": 10}
    assert len(fake.calls) == 1
    url, kw = fake.calls[0]
    assert url == "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"
    assert kw["params"] == {
        "MerchantLogin": "shop",
        "InvoiceID": "42",
        "Signature": main._robokassa_signature("shop", "42", "pass2"),
    }


async def test_robokassa_confirmed_false_when_not_yet_paid(monkeypatch):
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "shop")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "pass2")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("5")))
    assert await main._robokassa_confirmed("1", main.PRO_PRICE) is False


async def test_robokassa_confirmed_false_when_amount_mismatch(monkeypatch, caplog):
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "shop")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "pass2")
    monkeypatch.setattr(main.httpx, "AsyncClient",
                         lambda *a, **k: _FakeClient(_opstate_xml("100", out_sum="1.00")))
    with caplog.at_level("WARNING"):
        result = await main._robokassa_confirmed("1", main.PRO_PRICE)
    assert result is False
    # Точное сообщение, а не просто «где-то встречаются оба числа» — иначе
    # мутации самой строки лога (переставленные аргументы, обрезка) не ловятся.
    expected = f"pay/webhook: сумма в OpStateExt 1.00 не совпадает с ожидаемой {main.PRO_PRICE}"
    assert expected in [r.getMessage() for r in caplog.records]


async def test_robokassa_confirmed_true_when_paid_and_matching(monkeypatch):
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "shop")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "pass2")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("100")))
    assert await main._robokassa_confirmed("1", main.PRO_PRICE) is True


async def test_webhook_without_confirmation_no_pro(client, monkeypatch):
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-pending@test.com")
    _add_payment(uid, "101")
    # OpStateExt отвечает State.Code=5 (инициализирован, не оплачен) → не подтверждён
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("5")))
    r = await client.post("/api/pay/webhook", data=_webhook_form("101"))
    assert not r.text.startswith("OK")
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_with_confirmation_grants_pro(client, monkeypatch):
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-ok@test.com")
    _add_payment(uid, "102")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("100")))
    r = await client.post("/api/pay/webhook", data=_webhook_form("102"))
    assert r.text == "OK102"
    with main.get_db() as db:
        row = db.execute("SELECT is_pro, pro_expires_at FROM users WHERE id=?", (uid,)).fetchone()
    assert row["is_pro"] == 1
    assert row["pro_expires_at"]


async def test_webhook_bad_signature_no_pro(client, monkeypatch):
    """Подпись не сходится с PASSWORD2 → запрос не от Робокассы, Pro не выдаём."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-badsig@test.com")
    _add_payment(uid, "103")
    r = await client.post("/api/pay/webhook", data=_webhook_form("103", password2="wrong-password"))
    assert not r.text.startswith("OK")
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_wrong_amount_no_pro(client, monkeypatch):
    """Сумма в вебхуке не совпадает с ожидаемой ценой → Pro не выдаём (до вызова OpStateExt)."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-wrong@test.com")
    _add_payment(uid, "104")
    r = await client.post("/api/pay/webhook", data=_webhook_form("104", out_sum="1.00"))
    assert not r.text.startswith("OK")
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_unknown_payment_no_pro(client, monkeypatch):
    """Эндпоинт публичный: по чужому/выдуманному InvId Pro не выдаём.

    Без строки в payments прежний код не мог отметить платёж обработанным,
    поэтому повторы вебхука бесконечно продлевали Pro.
    """
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    r = await client.post("/api/pay/webhook", data=_webhook_form("999999"))
    assert not r.text.startswith("OK")


async def test_webhook_ignores_extra_body_fields(client, monkeypatch):
    """Кастомные Shp_-параметры в теле не влияют: Pro идёт плательщику из payments,
    а не тому, кто указан в произвольном поле запроса."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    payer = _add_user("payer@test.com")
    attacker = _add_user("attacker@test.com")
    _add_payment(payer, "105")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("100")))
    r = await client.post("/api/pay/webhook", data=_webhook_form("105", extra={"Shp_user_id": str(attacker)}))
    assert r.text == "OK105"
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (attacker,)).fetchone()["is_pro"] == 0
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (payer,)).fetchone()["is_pro"] == 1


async def test_webhook_replay_does_not_extend_pro(client, monkeypatch):
    """Повторный вебхук по обработанному платежу не продлевает Pro второй раз."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-replay@test.com")
    _add_payment(uid, "106")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("100")))
    form = _webhook_form("106")
    await client.post("/api/pay/webhook", data=form)
    with main.get_db() as db:
        first_exp = db.execute("SELECT pro_expires_at FROM users WHERE id=?", (uid,)).fetchone()["pro_expires_at"]
    r2 = await client.post("/api/pay/webhook", data=form)
    assert r2.text == "OK106"
    with main.get_db() as db:
        second_exp = db.execute("SELECT pro_expires_at FROM users WHERE id=?", (uid,)).fetchone()["pro_expires_at"]
    assert first_exp == second_exp


# ── Платёж: понятная ошибка при невыключенной/ненастроенной Робокассе ────────
async def test_pay_requires_auth(client):
    """Без сессии /api/pay не пускает к платежу."""
    main.init_db()
    r = await client.post("/api/pay", json={})
    assert r.status_code == 401


async def test_pay_unconfigured_returns_503(client, monkeypatch):
    """Без ключей Робокассы /api/pay отдаёт понятную 503 и не плодит висячих строк."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD1", "")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "")
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
