"""Характеризационные тесты на «горячие» инварианты безопасности.

Фиксируют поведение ДО декомпозиции бэкенда, чтобы любой рефактор,
который их нарушит, падал немедленно: SSRF-защита fetch-job, подтверждение
платежа Робокассы в вебхуке, срок жизни magic-ссылки.
"""
import hashlib
import json
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


def _add_payment(user_id, pay_id, amount=None):
    """Строка платежа, какую создаёт /api/pay до редиректа в Робокассу (InvId=pay_id).

    amount=None воспроизводит платежи, созданные до issue #43 — колонка ещё
    пуста, и вебхук должен сверять такую строку со старой глобальной PRO_PRICE
    (main.payment_webhook: `pay_row["amount"] or PRO_PRICE`).
    """
    with main.get_db() as db:
        db.execute("INSERT INTO payments (user_id, pay_id, idem_key, amount) VALUES (?,?,?,?)",
                   (user_id, pay_id, f"idem-{pay_id}", amount))
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
# Receipt уходит в подпись Password #1 и в поле формы одной и той же строкой,
# поэтому важен каждый параметр сериализации: сумма позиции, кодировка
# кириллицы, разделители JSON и набор символов, которые quote() не трогает.
# Через /api/pay это не проверить — там цена ровная и без спецсимволов.
def test_robokassa_receipt_rounds_sum_to_kopecks():
    """Сумма позиции — рубли с копейками. Округление до целого разошлось бы
    с OutSum, а лишние знаки Робокасса не принимает."""
    from urllib.parse import unquote

    item = json.loads(unquote(main._robokassa_receipt("Pro", "399.567")))["items"][0]
    assert item["sum"] == 399.57


def test_robokassa_receipt_keeps_cyrillic_literal():
    r"""ensure_ascii=False обязателен: с \uXXXX-экранированием чек становится
    нечитаемым в кабинете, а строка подписи — другой."""
    from urllib.parse import unquote

    raw = unquote(main._robokassa_receipt("Доступ Pro", "399.00"))
    assert "Доступ Pro" in raw
    assert "\\u04" not in raw


def test_robokassa_receipt_json_has_no_padding_spaces():
    """Разделители без пробелов: сериализация «как получится» дала бы другую
    строку под подписью при том же содержимом."""
    from urllib.parse import unquote

    raw = unquote(main._robokassa_receipt("Pro", "399.00"))
    assert '", "' not in raw
    assert '": "' not in raw
    assert '"items":[{' in raw


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


async def test_webhook_works_over_get(client, monkeypatch):
    """ResultURL в кабинете Робокассы можно настроить и на GET. Роут принимал
    только POST, и такая настройка молча съедала бы все платежи: 405 в ответ,
    ни строчки в логах приложения, Pro не выдан."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-get@test.com")
    _add_payment(uid, "150")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("100")))
    r = await client.get("/api/pay/webhook", params=_webhook_form("150"))
    assert r.text == "OK150"
    with main.get_db() as db:
        row = db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()
    assert row["is_pro"] == 1


async def test_webhook_bad_signature_over_get_no_pro(client, monkeypatch):
    """Подпись проверяется одинаково на обоих методах."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-get-bad@test.com")
    _add_payment(uid, "151")
    params = dict(_webhook_form("151"))
    params["SignatureValue"] = "0" * 32
    r = await client.get("/api/pay/webhook", params=params)
    assert r.status_code == 400
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


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


# ── Платёж: сумма сверяется со строкой конкретного платежа (issue #43) ───────
# Раньше вебхук и OpStateExt сверяли OutSum с глобальной константой PRO_PRICE.
# Смена цены в конфиге между выставлением счёта и приходом вебхука либо ломала
# разбор уже выставленных счетов, либо позволяла закрыть платёж на другую
# сумму — теперь источник истины для конкретного счёта — его собственная
# строка в payments.
async def test_webhook_confirms_using_amount_stored_in_payment_row(client, monkeypatch):
    """Сумма сверяется со значением, записанным в payments.amount при создании
    счёта, а не с текущей глобальной PRO_PRICE."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-custom-amount@test.com")
    # Сумма в строке платежа отличается от текущей PRO_PRICE — как если бы счёт
    # выставили до последующей смены цены в конфиге.
    custom_amount = "199.00"
    assert custom_amount != main.PRO_PRICE
    _add_payment(uid, "200", amount=custom_amount)
    monkeypatch.setattr(main.httpx, "AsyncClient",
                         lambda *a, **k: _FakeClient(_opstate_xml("100", out_sum=custom_amount)))
    r = await client.post("/api/pay/webhook", data=_webhook_form("200", out_sum=custom_amount))
    assert r.text == "OK200"
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 1


async def test_webhook_rejects_amount_not_matching_payment_row(client, monkeypatch):
    """OutSum, совпадающий с текущей глобальной PRO_PRICE, но НЕ с суммой,
    записанной в строке ЭТОГО платежа, не должен подтверждать оплату — иначе
    платёж можно было бы закрыть на сумму, отличную от показанной покупателю."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-mismatched-amount@test.com")
    _add_payment(uid, "201", amount="199.00")
    # out_sum по умолчанию в _webhook_form — main.PRO_PRICE, что не равно "199.00"
    r = await client.post("/api/pay/webhook", data=_webhook_form("201"))
    assert not r.text.startswith("OK")
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 0


async def test_webhook_falls_back_to_pro_price_for_legacy_payment_without_amount(client, monkeypatch):
    """Платежи, созданные до этой правки, хранят amount=NULL — вебхук обязан
    по-прежнему сверять их со старой константой PRO_PRICE, иначе уже
    выставленные и ещё не оплаченные счета перестанут подтверждаться сразу
    после деплоя."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", _TEST_PASSWORD2)
    uid = _add_user("pay-legacy@test.com")
    _add_payment(uid, "202")  # amount не передан → NULL, как до миграции
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient(_opstate_xml("100")))
    r = await client.post("/api/pay/webhook", data=_webhook_form("202"))
    assert r.text == "OK202"
    with main.get_db() as db:
        assert db.execute("SELECT is_pro FROM users WHERE id=?", (uid,)).fetchone()["is_pro"] == 1


# ── Оплата: InvId учитывает несбрасываемый оффсет (issue #43) ────────────────
async def test_payment_invid_includes_offset(client, monkeypatch):
    """InvId = payments.id + INV_ID_OFFSET, а не голый autoincrement.

    После восстановления БД из бэкапа (или пересоздания volume) payments.id
    стартует заново и без оффсета совпал бы с уже оплаченным старым номером —
    OpStateExt подтвердил бы новый счёт данными чужого, давно закрытого.
    """
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "shop")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD1", "pass1")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "pass2")
    monkeypatch.setattr(main, "INV_ID_OFFSET", 100000)
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            ("tok-offset", "offset@test.com"),
        )
        db.commit()
    await client.get("/auth/email/verify?token=tok-offset", follow_redirects=False)

    r = await client.post("/api/pay", json={})
    assert r.status_code == 200, r.text
    inv_id = int(r.json()["fields"]["InvId"])
    assert inv_id >= 100000, "InvId обязан включать оффсет, а не быть голым payments.id"
    with main.get_db() as db:
        row = db.execute("SELECT id FROM payments WHERE pay_id=?", (str(inv_id),)).fetchone()
    assert row is not None, "InvId в redirect-URL должен находиться в payments по pay_id"
    assert inv_id == row["id"] + 100000


async def test_payment_invid_matches_default_offset_zero(client, monkeypatch):
    """Без настройки INV_ID_OFFSET (по умолчанию 0) поведение не меняется —
    существующие установки не должны молча получить сдвинутые номера счетов."""
    main.init_db()
    monkeypatch.setattr(main, "ROBOKASSA_LOGIN", "shop")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD1", "pass1")
    monkeypatch.setattr(main, "ROBOKASSA_PASSWORD2", "pass2")
    assert main.INV_ID_OFFSET == 0
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            ("tok-offset-zero", "offset-zero@test.com"),
        )
        db.commit()
    await client.get("/auth/email/verify?token=tok-offset-zero", follow_redirects=False)

    r = await client.post("/api/pay", json={})
    inv_id = int(r.json()["fields"]["InvId"])
    with main.get_db() as db:
        row = db.execute("SELECT id FROM payments WHERE pay_id=?", (str(inv_id),)).fetchone()
    assert inv_id == row["id"]


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


# ── fetch-job: авторизация и предел размера ответа ───────────────────────────
# Настоящий класс запоминаем до подмены main.httpx.AsyncClient — иначе
# фабрика вызывала бы саму себя.
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _mock_client(handler):
    """Фабрика AsyncClient поверх MockTransport — сеть не трогаем, но
    настоящий httpx-стриминг проверяем как есть."""
    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return _REAL_ASYNC_CLIENT(*a, **k)
    return factory


async def test_fetch_job_requires_auth(client):
    """Анонимная ручка делала из сервера открытый прокси."""
    main.init_db()
    r = await client.post("/api/fetch-job", json={"url": "https://example.com/job"})
    assert r.status_code == 401


async def test_fetch_job_stops_reading_after_limit(monkeypatch):
    """Тело обрывается на MAX_JOB_BYTES, а не читается целиком в память."""
    produced = {"bytes": 0}

    async def body():
        for _ in range(100):                      # 100 x 64 КБ = 6.4 МБ, если не оборвать
            chunk = b"<p>" + b"a" * 65536 + b"</p>"
            produced["bytes"] += len(chunk)
            yield chunk

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    text = await main._fetch_job_text("https://example.com/huge")

    assert len(text) <= 4000
    assert produced["bytes"] < main.MAX_JOB_BYTES * 2, (
        f"прочитано {produced['bytes']} байт при пределе {main.MAX_JOB_BYTES}"
    )


async def test_fetch_job_rejects_non_text_content_type(monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(HTTPException) as exc:
        await main._fetch_job_text("https://example.com/file.pdf")
    assert exc.value.status_code == 400


async def test_fetch_job_reads_normal_page(monkeypatch):
    """Обычная страница по-прежнему разбирается: теги срезаны, текст на месте."""
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content="<html><body><h1>Ищем Python-разработчика</h1></body></html>".encode(),
        )

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    text = await main._fetch_job_text("https://example.com/job")
    assert "Ищем Python-разработчика" in text
    assert "<h1>" not in text


async def test_fetch_job_follows_redirect_and_checks_each_hop(monkeypatch):
    """Редирект на внутренний адрес не проходит — проверка на каждом хопе."""
    def handler(request):
        return httpx.Response(302, headers={"location": "http://127.0.0.1:11434/api/tags"})

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(HTTPException) as exc:
        await main._fetch_job_text("https://example.com/redirect")
    assert exc.value.status_code == 400


async def test_fetch_job_sends_user_agent(monkeypatch):
    """Без User-Agent часть job-бордов отдаёт заглушку вместо вакансии."""
    seen = {}

    def handler(request):
        seen["ua"] = request.headers.get("user-agent", "")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>text</p>")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    await main._fetch_job_text("https://example.com/job")
    assert seen["ua"].startswith("Mozilla/")


async def test_fetch_job_accepts_plain_http(monkeypatch):
    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content="<p>вакансия</p>".encode())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "вакансия" in await main._fetch_job_text("http://example.com/job")


async def test_fetch_job_rejects_url_without_host(monkeypatch):
    """Схема на месте, хоста нет — до сети доходить не должны."""
    def handler(request):                      # pragma: no cover — не должен вызваться
        raise AssertionError("запрос не должен был уйти")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(HTTPException) as exc:
        await main._fetch_job_text("http:///no-host")
    assert exc.value.status_code == 400


async def test_fetch_job_follows_redirect_to_public_host(monkeypatch):
    """Редирект на внешний адрес отрабатывает, текст берётся со второго хопа."""
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "https://example.org/final"})
        return httpx.Response(200, headers={"content-type": "text/html"},
                              content="<p>Финальная вакансия</p>".encode())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "Финальная вакансия" in await main._fetch_job_text("https://example.com/start")


async def test_fetch_job_gives_up_after_five_redirects(monkeypatch):
    """Пять редиректов подряд — предел; шестой хоп делать не должны."""
    hops = {"n": 0}

    def handler(request):
        hops["n"] += 1
        if hops["n"] <= 5:
            return httpx.Response(302, headers={"location": f"https://example.org/hop{hops['n']}"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>late</p>")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(HTTPException) as exc:
        await main._fetch_job_text("https://example.com/start")
    assert exc.value.status_code == 400
    assert hops["n"] == 5


async def test_fetch_job_treats_200_with_location_as_content(monkeypatch):
    """Заголовок Location на обычном 200 — не редирект: читаем тело."""
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "location": "https://example.org/elsewhere"},
            content="<p>Настоящая вакансия</p>".encode(),
        )

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "Настоящая вакансия" in await main._fetch_job_text("https://example.com/job")


async def test_fetch_job_without_content_type_is_read(monkeypatch):
    """Заголовка нет — не повод падать: проверку типа просто пропускаем."""
    def handler(request):
        return httpx.Response(200, content="<p>Вакансия без типа</p>".encode())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "Вакансия без типа" in await main._fetch_job_text("https://example.com/job")


async def test_fetch_job_respects_declared_charset(monkeypatch):
    """Кодировка берётся из заголовка, а не предполагается utf-8."""
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=windows-1251"},
            content="<p>Требуется инженер</p>".encode("cp1251"),
        )

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "Требуется инженер" in await main._fetch_job_text("https://example.com/job")


async def test_fetch_job_stops_exactly_at_limit(monkeypatch):
    """Ровно MAX_JOB_BYTES — уже достаточно, следующий кусок не запрашиваем."""
    produced = {"chunks": 0}

    async def body():
        for _ in range(5):
            produced["chunks"] += 1
            yield b"a" * main.MAX_JOB_BYTES

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    await main._fetch_job_text("https://example.com/huge")
    assert produced["chunks"] == 1


async def test_fetch_job_follows_relative_redirect(monkeypatch):
    """Location часто относительный — он должен склеиваться с текущим URL."""
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/vacancy/42"})
        assert request.url.path == "/vacancy/42"
        return httpx.Response(200, headers={"content-type": "text/html"},
                              content="<p>Относительный редирект</p>".encode())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "Относительный редирект" in await main._fetch_job_text("https://example.com/start")


async def test_fetch_job_survives_broken_encoding(monkeypatch):
    """Байты, не бьющиеся с объявленной кодировкой, не должны ронять разбор."""
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<p>job " + bytes([0xFF, 0xFE, 0xFD]) + b" offer</p>",
        )

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    text = await main._fetch_job_text("https://example.com/job")
    assert "job" in text and "offer" in text


async def test_fetch_job_rejects_non_http_scheme(monkeypatch):
    """Схема не http(s) — отказ до всякой сети, с кодом 400."""
    def handler(request):                      # pragma: no cover — не должен вызваться
        raise AssertionError("запрос не должен был уйти")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(HTTPException) as exc:
        await main._fetch_job_text("ftp://example.com/vacancy")
    assert exc.value.status_code == 400


async def test_fetch_job_maps_network_error_to_502(monkeypatch):
    """Сеть не ответила — это 502, а не 500 и не проброс исключения наружу."""
    def handler(request):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    with pytest.raises(HTTPException) as exc:
        await main._fetch_job_text("https://example.com/job")
    assert exc.value.status_code == 502


async def test_fetch_job_uses_get(monkeypatch):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>x</p>")

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    await main._fetch_job_text("https://example.com/job")
    assert seen["method"] == "GET"


async def test_fetch_job_joins_chunks_without_separator(monkeypatch):
    """Куски потока склеиваются встык: слово на границе не должно рваться."""
    async def body():
        yield "<p>Раз".encode()
        yield "дватри</p>".encode()

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=body())

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert "Раздватри" in await main._fetch_job_text("https://example.com/job")


async def test_fetch_job_normalises_text(monkeypatch):
    """Теги заменяются пробелом, пробельные последовательности схлопываются,
    края обрезаются — на выходе ровно текст вакансии."""
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content="<div>\n  <b>Иванов</b>\t\tПётр\n</div>".encode(),
        )

    monkeypatch.setattr(main.httpx, "AsyncClient", _mock_client(handler))
    assert await main._fetch_job_text("https://example.com/job") == "Иванов Пётр"


async def test_fetch_job_client_is_configured_defensively(monkeypatch):
    """Параметры клиента — часть защиты, а не стиль оформления.

    follow_redirects=False принципиален: автоследование внутри httpx обошло бы
    проверку _assert_public_host на каждом хопе, то есть вернуло бы SSRF через
    редирект на внутренний адрес. Таймаут не даёт зависшему сайту вакансии
    держать слот AI-очереди, а User-Agent нужен, чтобы job-борды не отдавали
    заглушку вместо вакансии.
    """
    captured: dict = {}

    def handler(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<p>x</p>")

    def factory(*args, **kwargs):
        captured.update(kwargs)
        return _REAL_ASYNC_CLIENT(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main.httpx, "AsyncClient", factory)
    await main._fetch_job_text("https://example.com/job")

    assert captured["follow_redirects"] is False
    assert captured["timeout"] == 15
    assert captured["headers"]["User-Agent"].startswith("Mozilla/")
