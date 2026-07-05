import hashlib
import hmac as hmac_module
import time

import pytest
import pytest_asyncio
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


# ── VK OAuth tests ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vk_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "")
    r = await client.get("/auth/vk")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_vk_start_redirects_to_vk_domain(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/vk", follow_redirects=False)
    assert r.status_code == 302
    assert "id.vk.com" in r.headers["location"]
    assert "client_id=test-vk-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_vk_start_sets_state_cookie(monkeypatch, client):
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
async def test_mailru_start_redirects_to_mailru_domain(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/mailru", follow_redirects=False)
    assert r.status_code == 302
    assert "oauth.mail.ru" in r.headers["location"]
    assert "client_id=test-mr-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_mailru_start_sets_state_cookie(monkeypatch, client):
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
