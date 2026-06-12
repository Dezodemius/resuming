import hashlib
import hmac as hmac_module
import time

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
