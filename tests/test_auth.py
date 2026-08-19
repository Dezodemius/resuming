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
def test_auth_ctx_all_providers_off_by_default(monkeypatch):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is False
    assert ctx["vk_enabled"] is False
    assert ctx["mailru_enabled"] is False


def test_auth_ctx_enabled_toggle_still_needs_client_id(monkeypatch):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "")
    monkeypatch.setattr(main, "VK_CLIENT_ID", "")
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "")
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is False
    assert ctx["vk_enabled"] is False
    assert ctx["mailru_enabled"] is False


def test_auth_ctx_enabled_and_configured_returns_true(monkeypatch):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is True
    assert ctx["vk_enabled"] is True
    assert ctx["mailru_enabled"] is True


def test_auth_ctx_passes_through_telegram_and_user(monkeypatch):
    monkeypatch.setattr(main, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(main, "TELEGRAM_BOT_NAME", "MyResumeBot")
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
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    r = await client.get("/auth/yandex")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_yandex_start_redirects_to_yandex_domain(monkeypatch, client):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
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
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_vk_start_redirects_to_vk_domain(monkeypatch, client):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/vk", follow_redirects=False)
    assert r.status_code == 302
    assert "id.vk.com" in r.headers["location"]
    assert "client_id=test-vk-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_vk_start_sets_state_cookie(monkeypatch, client):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
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
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_mailru_start_redirects_to_mailru_domain(monkeypatch, client):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/mailru", follow_redirects=False)
    assert r.status_code == 302
    assert "oauth.mail.ru" in r.headers["location"]
    assert "client_id=test-mr-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_mailru_start_sets_state_cookie(monkeypatch, client):
    monkeypatch.setattr(main, "OAUTH_LOGIN_ENABLED", True)
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
