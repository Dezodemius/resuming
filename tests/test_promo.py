"""Tests for promo code functionality."""
import pytest
import pytest_asyncio
import main


@pytest_asyncio.fixture
async def user_session(client, db):
    """Create a test user and session."""
    with db as c:
        c.execute(
            "INSERT INTO users (email, display_name) VALUES (?,?)",
            ("test@example.com", "Test User")
        )
        c.commit()
        user = c.execute("SELECT id FROM users WHERE email=?", ("test@example.com",)).fetchone()
        user_id = user["id"]
        sid = main._create_session(c, user_id)
    return {"user_id": user_id, "session_id": sid}


@pytest.fixture
def reset_rate_limit(monkeypatch):
    """Reset rate limiter for tests."""
    try:
        from slowapi import Limiter
        from slowapi.util import get_remote_address
        limiter = Limiter(key_func=get_remote_address)
        monkeypatch.setattr(main, "limiter", limiter)
    except ImportError:
        pass


@pytest.mark.asyncio
async def test_activate_pro_days_promo(client, db, user_session, monkeypatch):
    """Test activating a pro_days promo code."""
    # Create a promo code
    with db as c:
        c.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses) VALUES (?,?,?,?)",
            ("PROMO0000TEST01", "pro_days", 30, 1)
        )
        c.commit()

    # Activate promo
    resp = await client.post(
        "/api/promo/activate",
        json={"code": "promo0000test01"},  # lowercase — should be normalized
        cookies={"session_id": user_session["session_id"]}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["kind"] == "pro_days"
    assert "Pro до" in data["message"]

    # Check user is now Pro
    with db as c:
        user = c.execute("SELECT is_pro, pro_expires_at FROM users WHERE id=?",
                        (user_session["user_id"],)).fetchone()
    assert user["is_pro"] == 1
    assert user["pro_expires_at"] is not None


@pytest.mark.asyncio
async def test_activate_gen_pack_promo(client, db, user_session):
    """Test activating a gen_pack promo code."""
    with db as c:
        c.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses) VALUES (?,?,?,?)",
            ("GENPACK000001", "gen_pack", 20, 1)
        )
        c.commit()

    resp = await client.post(
        "/api/promo/activate",
        json={"code": "GENPACK000001"},
        cookies={"session_id": user_session["session_id"]}
    )

    assert resp.status_code == 200
    assert "+20 генераций" in resp.json()["message"]

    with db as c:
        user = c.execute("SELECT paid_left FROM users WHERE id=?",
                        (user_session["user_id"],)).fetchone()
    assert user["paid_left"] == 20


@pytest.mark.asyncio
async def test_activate_unlimited_promo(client, db, user_session):
    """Test activating an unlimited promo code."""
    with db as c:
        c.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses) VALUES (?,?,?,?)",
            ("UNLIMITED0001", "unlimited", 0, 1)
        )
        c.commit()

    resp = await client.post(
        "/api/promo/activate",
        json={"code": "UNLIMITED0001"},
        cookies={"session_id": user_session["session_id"]}
    )

    assert resp.status_code == 200
    assert "Безлимит" in resp.json()["message"]

    with db as c:
        user = c.execute("SELECT is_pro, pro_expires_at FROM users WHERE id=?",
                        (user_session["user_id"],)).fetchone()
    assert user["is_pro"] == 1
    assert user["pro_expires_at"] == "2099-12-31 00:00:00"


@pytest.mark.asyncio
async def test_duplicate_activation_rejected(client, db, user_session, reset_rate_limit):
    """Test that a user cannot activate the same code twice."""
    with db as c:
        c.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses) VALUES (?,?,?,?)",
            ("ONESHOT0001", "gen_pack", 10, 1)
        )
        c.commit()

    # First activation
    resp1 = await client.post(
        "/api/promo/activate",
        json={"code": "ONESHOT0001"},
        cookies={"session_id": user_session["session_id"]}
    )
    assert resp1.status_code == 200

    # Second activation — should fail
    resp2 = await client.post(
        "/api/promo/activate",
        json={"code": "ONESHOT0001"},
        cookies={"session_id": user_session["session_id"]}
    )
    assert resp2.status_code == 400
    detail = resp2.json().get("detail", "")
    assert "код" in detail.lower()


@pytest.mark.asyncio
async def test_admin_requires_login(client):
    """Test that /admin returns 404 without authentication."""
    resp = await client.get("/admin")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_requires_admin_email(client, db, user_session):
    """Test that non-admin users get 404 on /admin."""
    # user_session is a regular user, not admin
    resp = await client.get(
        "/admin",
        cookies={"session_id": user_session["session_id"]}
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_admin_page_loads(client, db, user_session, monkeypatch):
    """Test that /admin loads for authorized admin."""
    import main
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["test@example.com"])

    resp = await client.get(
        "/admin",
        cookies={"session_id": user_session["session_id"]}
    )
    assert resp.status_code == 200
    assert "admin" in resp.text.lower() or "промокод" in resp.text.lower()


@pytest.mark.asyncio
async def test_admin_create_promo(client, db, user_session, monkeypatch):
    """Test creating a promo code via admin API."""
    import main
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["test@example.com"])

    resp = await client.post(
        "/api/admin/promo",
        json={"kind": "gen_pack", "value": 25, "max_uses": 5, "comment": "Test"},
        cookies={"session_id": user_session["session_id"]}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "code" in data
    assert len(data["code"]) == 14  # XXXX-XXXX-XXXX


@pytest.mark.asyncio
async def test_admin_list_promo(client, db, user_session, monkeypatch):
    """Test listing promo codes."""
    import main
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["test@example.com"])

    with db as c:
        c.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses) VALUES (?,?,?,?)",
            ("CODE0001", "gen_pack", 10, 2)
        )
        c.commit()

    resp = await client.get(
        "/api/admin/promo",
        cookies={"session_id": user_session["session_id"]}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "codes" in data
    assert len(data["codes"]) > 0
    assert any(c["code"] == "CODE0001" for c in data["codes"])


@pytest.mark.asyncio
async def test_admin_stats(client, db, user_session, monkeypatch):
    """Test /api/admin/stats endpoint."""
    import main
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["test@example.com"])

    resp = await client.get(
        "/api/admin/stats",
        cookies={"session_id": user_session["session_id"]}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert "users" in data
    assert "generations" in data
    assert "logins" in data
    assert "payments_30days" in data
    assert "promos_30days" in data
    assert "top_users" in data
