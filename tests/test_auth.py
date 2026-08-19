import hashlib
import hmac as hmac_module
import time

import pytest
import main
from main import _verify_telegram


TEST_TOKEN = "fake-bot-token-123"


def _make_valid_data(token: str, age_seconds: int = 0) -> dict:
    """Build a Telegram login payload with a valid HMAC hash."""
    data = {
        "id": "123456",
        "first_name": "Test",
        "username": "testuser",
        "auth_date": str(int(time.time()) - age_seconds),
    }
    secret = hashlib.sha256(token.encode()).digest()
    check_str = "\n".join(f"{k}={data[k]}" for k in sorted(data))
    data["hash"] = hmac_module.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
    return data


def test_no_token_returns_false(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "")
    assert _verify_telegram({"id": "1", "hash": "x", "auth_date": str(int(time.time()))}) is False


def test_valid_data_returns_true(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    assert _verify_telegram(_make_valid_data(TEST_TOKEN)) is True


def test_wrong_hash_returns_false(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    data = _make_valid_data(TEST_TOKEN)
    data["hash"] = "0" * 64
    assert _verify_telegram(data) is False


def test_expired_auth_date_returns_false(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    # 3601 seconds old — just past the 1-hour limit
    data = _make_valid_data(TEST_TOKEN, age_seconds=3601)
    assert _verify_telegram(data) is False


def test_fresh_auth_date_returns_true(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    data = _make_valid_data(TEST_TOKEN, age_seconds=1800)  # 30 min ago — still valid
    assert _verify_telegram(data) is True


# ── Подпись считается по фактически присланным полям ───────────────────
# Telegram опускает незаполненные поля (username, photo_url, last_name), и в
# подписи их нет. Раньше в _verify_telegram попадала разобранная pydantic-
# модель, которая добавляла их со значениями None — пользователь без username
# получал 401 и войти не мог. Тесты ниже гоняют payload через реальную ручку,
# то есть через ту же схему, что и FastAPI.


def _sign(payload: dict, token: str = TEST_TOKEN) -> dict:
    """Подписывает payload ровно так, как это делает Telegram."""
    secret = hashlib.sha256(token.encode()).digest()
    check_str = "\n".join(f"{k}={payload[k]}" for k in sorted(payload))
    return {**payload, "hash": hmac_module.new(secret, check_str.encode(), hashlib.sha256).hexdigest()}


@pytest.mark.asyncio
async def test_login_works_without_username_and_photo(client, monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    main.init_db()
    body = _sign({"id": 777001, "first_name": "Иван", "auth_date": int(time.time())})
    r = await client.post("/auth/telegram", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "session_id" in r.cookies


@pytest.mark.asyncio
async def test_login_works_with_all_fields(client, monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    main.init_db()
    body = _sign({
        "id": 777002, "first_name": "Иван", "last_name": "Петров",
        "username": "ivan", "photo_url": "https://t.me/i/userpic/1.jpg",
        "auth_date": int(time.time()),
    })
    r = await client.post("/auth/telegram", json=body)
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_login_rejects_tampered_field(client, monkeypatch):
    """Подпись остаётся обязательной: подменённое поле не проходит."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    main.init_db()
    body = _sign({"id": 777003, "first_name": "Иван", "auth_date": int(time.time())})
    body["first_name"] = "Админ"
    r = await client.post("/auth/telegram", json=body)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_login_rejects_extra_unsigned_field(client, monkeypatch):
    """Дописанное поле меняет строку проверки — вход не проходит (fail-closed)."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    main.init_db()
    body = _sign({"id": 777004, "first_name": "Иван", "auth_date": int(time.time())})
    body["username"] = "smuggled"
    r = await client.post("/auth/telegram", json=body)
    assert r.status_code == 401


def test_non_numeric_auth_date_returns_false(monkeypatch):
    """Мусор в auth_date — это 401, а не 500 из-за необработанного ValueError."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    data = _sign({"id": 1, "auth_date": "не-число"})
    assert _verify_telegram(data) is False


def test_empty_hash_returns_false(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    assert _verify_telegram({"id": 1, "auth_date": int(time.time())}) is False


def test_non_string_hash_returns_false(monkeypatch):
    """hash числом — не Telegram. Без проверки типа compare_digest даёт 500."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    assert _verify_telegram({"id": 1, "auth_date": int(time.time()), "hash": 12345}) is False


def test_missing_auth_date_returns_false(monkeypatch):
    """Подпись может сойтись и без auth_date — тогда решает отсутствие поля."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    assert _verify_telegram(_sign({"id": 1, "first_name": "Иван"})) is False


def test_auth_date_exactly_at_ttl_is_accepted(monkeypatch):
    """Граница окна ровно 3600 с: 3600 — ещё валидно."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    now = 1_800_000_000
    monkeypatch.setattr(main.time, "time", lambda: float(now))
    assert _verify_telegram(_sign({"id": 1, "auth_date": now - 3600})) is True


def test_auth_date_one_second_over_ttl_is_rejected(monkeypatch):
    """...а 3601 — уже нет."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", TEST_TOKEN)
    now = 1_800_000_000
    monkeypatch.setattr(main.time, "time", lambda: float(now))
    assert _verify_telegram(_sign({"id": 1, "auth_date": now - 3601})) is False


# ── _auth_ctx ─────────────────────────────────────────────────────────
# Что именно включено, решается один раз в config (там же и предупреждения о
# недонастроенных провайдерах). _auth_ctx обязан лишь честно это отразить:
# кнопка в шаблоне и проверка в ручке должны решать одинаково, иначе получится
# либо кнопка в никуда, либо рабочая ручка без кнопки.
def test_auth_ctx_reflects_enabled_flags(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", True)
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is True
    assert ctx["vk_enabled"] is False
    assert ctx["mailru_enabled"] is True


def test_auth_ctx_all_providers_off_by_default(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", False)
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is False
    assert ctx["vk_enabled"] is False
    assert ctx["mailru_enabled"] is False


def test_auth_ctx_hides_telegram_without_token(monkeypatch):
    """Виджет без токена бесполезен: подпись проверять нечем, и вход всегда
    заканчивался бы 401. Лучше не показывать кнопку вовсе."""
    monkeypatch.setattr(main, "TELEGRAM_BOT_NAME", "MyResumeBot")
    monkeypatch.setattr(main, "TELEGRAM_LOGIN_ENABLED", False)
    assert main._auth_ctx(None)["telegram_bot_name"] == ""


def test_auth_ctx_passes_through_telegram_and_user(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_NAME", "MyResumeBot")
    monkeypatch.setattr(main, "TELEGRAM_LOGIN_ENABLED", True)
    sentinel_user = {"id": 42}
    ctx = main._auth_ctx(sentinel_user)
    assert ctx["telegram_bot_name"] == "MyResumeBot"
    assert ctx["user"] is sentinel_user


# ── Yandex OAuth tests ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_yandex_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "")
    r = await client.get("/auth/yandex")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_yandex_oauth_disabled_returns_503_even_with_client_id(monkeypatch, client):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    r = await client.get("/auth/yandex")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_yandex_start_redirects_to_yandex_domain(monkeypatch, client):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/yandex", follow_redirects=False)
    assert r.status_code == 302
    assert "oauth.yandex.ru" in r.headers["location"]
    assert "client_id=test-yandex-id" in r.headers["location"]


# ── VK OAuth tests ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vk_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "")
    r = await client.get("/auth/vk")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_vk_oauth_disabled_returns_503_even_with_client_id(monkeypatch, client):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_vk_start_redirects_to_vk_domain(monkeypatch, client):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/vk", follow_redirects=False)
    assert r.status_code == 302
    assert "id.vk.com" in r.headers["location"]
    assert "client_id=test-vk-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_vk_start_sets_state_cookie(monkeypatch, client):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/vk", follow_redirects=False)
    assert "vk_state" in r.cookies
    assert "vk_verifier" in r.cookies


@pytest.mark.asyncio
async def test_vk_callback_state_mismatch_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk/callback?code=test&state=wrong&device_id=test", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=vk" in r.headers["location"]


@pytest.mark.asyncio
async def test_vk_callback_no_code_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk/callback?state=&device_id=test", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=vk" in r.headers["location"]


# ── Mail.ru OAuth tests ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mailru_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "")
    r = await client.get("/auth/mailru")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_mailru_oauth_disabled_returns_503_even_with_client_id(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_mailru_start_redirects_to_mailru_domain(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/mailru", follow_redirects=False)
    assert r.status_code == 302
    assert "oauth.mail.ru" in r.headers["location"]
    assert "client_id=test-mr-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_mailru_start_sets_state_cookie(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/mailru", follow_redirects=False)
    assert "mr_state" in r.cookies


@pytest.mark.asyncio
async def test_mailru_callback_state_mismatch_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru/callback?code=test&state=wrong", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=mailru" in r.headers["location"]


@pytest.mark.asyncio
async def test_mailru_callback_no_code_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru/callback?state=", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=mailru" in r.headers["location"]


# ── APP_URL: punycode-нормализация кириллического домена ────────────────
def test_idna_url_cyrillic_host_converted():
    from config import _idna_url
    assert _idna_url("https://резюмирую.рф") == "https://xn--e1aedprev8fe.xn--p1ai"


def test_idna_url_preserves_path_and_port():
    from config import _idna_url
    assert _idna_url("https://резюмирую.рф:8443/auth") == "https://xn--e1aedprev8fe.xn--p1ai:8443/auth"


def test_idna_url_ascii_untouched():
    from config import _idna_url
    assert _idna_url("http://localhost:8000") == "http://localhost:8000"
    assert _idna_url("https://example.com") == "https://example.com"


# ── Регистр адреса не создаёт второй аккаунт ────────────────────────────────
# Один и тот же человек приходит то из формы («Ivan@Ya.ru», как набрал), то из
# OAuth («ivan@ya.ru», как отдал провайдер). Раньше это были два разных
# аккаунта: во втором не было ни резюме, ни оплаченного Pro.

async def _login_by_email(client, email, token):
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            (token, email),
        )
        db.commit()
    await client.get(f"/auth/email/verify?token={token}", follow_redirects=False)
    return (await client.get("/api/me")).json()


@pytest.mark.asyncio
async def test_same_email_different_case_is_one_account(client):
    main.init_db()
    first = await _login_by_email(client, "Ivan@Ya.ru", "tok-upper")
    second = await _login_by_email(client, "ivan@ya.ru", "tok-lower")

    assert first["authenticated"] and second["authenticated"]
    assert first["id"] == second["id"], "разный регистр не должен разводить аккаунты"
    with main.get_db() as db:
        rows = db.execute("SELECT id, email FROM users").fetchall()
    assert len(rows) == 1, f"аккаунтов должно быть один, а не {len(rows)}"
    assert rows[0]["email"] == "ivan@ya.ru", "адрес хранится в нижнем регистре"


@pytest.mark.asyncio
async def test_email_with_spaces_is_trimmed(client):
    """Пробелы по краям приезжают из форм и мобильных клавиатур постоянно."""
    main.init_db()
    me = await _login_by_email(client, "  Petr@Ya.ru  ", "tok-spaces")
    assert me["authenticated"]
    with main.get_db() as db:
        assert db.execute("SELECT email FROM users").fetchone()["email"] == "petr@ya.ru"


def test_normalize_email():
    assert main._normalize_email("Ivan@Ya.RU") == "ivan@ya.ru"
    # Перевод строки и табуляция приезжают из копипасты не реже пробелов.
    assert main._normalize_email(" ivan@ya.ru " + chr(10)) == "ivan@ya.ru"
    assert main._normalize_email(chr(9) + "ivan@ya.ru") == "ivan@ya.ru"
    assert main._normalize_email("ivan@ya.ru") == "ivan@ya.ru"


@pytest.mark.asyncio
async def test_default_display_name_is_local_part(client):
    """Имя по умолчанию — часть адреса до собаки: оно показывается в шапке и в
    админке, и подставлять туда весь адрес значит светить почту пользователя."""
    main.init_db()
    me = await _login_by_email(client, "Ivan.Petrov@ya.ru", "tok-name")
    assert me["name"] == "ivan.petrov"


# ── Отчёт о способах входа при старте ───────────────────────────────────────
# Вся конфигурация входа на проде оказалась пустой, и заметить это можно было
# только чтением шаблонов: наружу сайт выглядел как «умеет только почту».
# Теперь состав способов пишется в лог, а недонастроенный провайдер даёт
# предупреждение — молчать о нём хуже всего.

def _report(monkeypatch, **overrides):
    import config
    defaults = dict(
        TELEGRAM_BOT_NAME="", TELEGRAM_BOT_TOKEN="", TELEGRAM_LOGIN_ENABLED=False,
        OAUTH_LOGIN_ENABLED=False,
        YANDEX_CLIENT_ID="", YANDEX_CLIENT_SECRET="", YANDEX_LOGIN_ENABLED=False,
        VK_CLIENT_ID="", VK_LOGIN_ENABLED=False,
        MAILRU_CLIENT_ID="", MAILRU_CLIENT_SECRET="", MAILRU_LOGIN_ENABLED=False,
    )
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(config, name, value)
    return config._login_methods_report()


def test_login_report_email_always_available(monkeypatch):
    active, notes = _report(monkeypatch)
    assert active == ["email"]
    assert notes == []


def test_login_report_lists_every_enabled_method(monkeypatch):
    active, notes = _report(
        monkeypatch,
        TELEGRAM_BOT_NAME="bot", TELEGRAM_BOT_TOKEN="tok", TELEGRAM_LOGIN_ENABLED=True,
        OAUTH_LOGIN_ENABLED=True,
        YANDEX_CLIENT_ID="y", YANDEX_CLIENT_SECRET="s", YANDEX_LOGIN_ENABLED=True,
        VK_CLIENT_ID="v", VK_LOGIN_ENABLED=True,
        MAILRU_CLIENT_ID="m", MAILRU_CLIENT_SECRET="s", MAILRU_LOGIN_ENABLED=True,
    )
    assert active == ["email", "telegram", "yandex", "vk", "mailru"]
    assert notes == []


def test_login_report_warns_about_hidden_configured_providers(monkeypatch):
    """Ровно та ситуация, ради которой это писалось: ключи есть, рубильник
    выключен, кнопок нет и никто не понимает почему."""
    _, notes = _report(monkeypatch, YANDEX_CLIENT_ID="y", MAILRU_CLIENT_ID="m")
    assert any("OAUTH_LOGIN_ENABLED=0" in n for n in notes)
    assert any("Яндекс" in n and "Mail.ru" in n for n in notes)


def test_login_report_warns_about_half_configured_telegram(monkeypatch):
    _, notes = _report(monkeypatch, TELEGRAM_BOT_NAME="bot")
    assert any("Telegram" in n for n in notes)

    _, notes = _report(monkeypatch, TELEGRAM_BOT_TOKEN="tok")
    assert any("Telegram" in n for n in notes)


def test_login_report_warns_about_missing_secrets(monkeypatch):
    """Client ID без секрета — кнопка была бы, а обмен кода на токен падал."""
    _, notes = _report(monkeypatch, OAUTH_LOGIN_ENABLED=True, YANDEX_CLIENT_ID="y")
    assert any("YANDEX_CLIENT_SECRET" in n for n in notes)

    _, notes = _report(monkeypatch, OAUTH_LOGIN_ENABLED=True, MAILRU_CLIENT_ID="m")
    assert any("MAILRU_CLIENT_SECRET" in n for n in notes)


def test_login_report_vk_needs_no_secret(monkeypatch):
    """VK ID работает по PKCE — требовать у него секрет было бы неверно."""
    active, notes = _report(
        monkeypatch, OAUTH_LOGIN_ENABLED=True, VK_CLIENT_ID="v", VK_LOGIN_ENABLED=True
    )
    assert "vk" in active
    assert notes == []
