import uuid
from datetime import datetime, timezone, timedelta

from main import _deduct, _refund, _parse_ai


def _add_user(db, free_left=3, paid_left=0, is_pro=0, pro_expires_at=None) -> int:
    cur = db.execute(
        "INSERT INTO users (email, free_left, paid_left, is_pro, pro_expires_at) VALUES (?,?,?,?,?)",
        (f"{uuid.uuid4()}@test.com", free_left, paid_left, is_pro, pro_expires_at),
    )
    db.commit()
    return cur.lastrowid


def _future(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


# ── _deduct ──────────────────────────────────────────────────────────────────

def test_deduct_uses_free_credits(db):
    uid = _add_user(db, free_left=3)
    ok, col, left = _deduct(db, uid)
    assert ok is True
    assert col == "free_left"
    assert left == 2


def test_deduct_paid_before_free_when_free_empty(db):
    uid = _add_user(db, free_left=0, paid_left=5)
    ok, col, left = _deduct(db, uid)
    assert ok is True
    assert col == "paid_left"
    assert left == 4


def test_deduct_no_credits_returns_false(db):
    uid = _add_user(db, free_left=0, paid_left=0)
    ok, col, left = _deduct(db, uid)
    assert ok is False
    assert left == 0


def test_deduct_pro_user_unlimited(db):
    uid = _add_user(db, free_left=0, paid_left=0, is_pro=1, pro_expires_at=_future(30))
    ok, col, left = _deduct(db, uid)
    assert ok is True
    assert col == "pro"
    assert left == 999


def test_deduct_decrements_counter(db):
    uid = _add_user(db, free_left=3)
    _deduct(db, uid)
    row = db.execute("SELECT free_left FROM users WHERE id=?", (uid,)).fetchone()
    assert row["free_left"] == 2


# ── _refund ──────────────────────────────────────────────────────────────────

def test_refund_restores_free_credit(db):
    uid = _add_user(db, free_left=3)
    _deduct(db, uid)
    _refund(db, uid, "free_left")
    row = db.execute("SELECT free_left FROM users WHERE id=?", (uid,)).fetchone()
    assert row["free_left"] == 3


def test_refund_restores_paid_credit(db):
    uid = _add_user(db, free_left=0, paid_left=5)
    _deduct(db, uid)
    _refund(db, uid, "paid_left")
    row = db.execute("SELECT paid_left FROM users WHERE id=?", (uid,)).fetchone()
    assert row["paid_left"] == 5


def test_refund_pro_is_noop(db):
    uid = _add_user(db, free_left=3)
    _refund(db, uid, "pro")  # should not crash or change anything
    row = db.execute("SELECT free_left FROM users WHERE id=?", (uid,)).fetchone()
    assert row["free_left"] == 3


# ── _parse_ai ────────────────────────────────────────────────────────────────

def test_parse_ai_plain_json():
    assert _parse_ai('{"name": "Alice"}') == {"name": "Alice"}


def test_parse_ai_fenced_json():
    assert _parse_ai('```json\n{"name": "Bob"}\n```') == {"name": "Bob"}


def test_parse_ai_fenced_no_lang():
    assert _parse_ai('```\n{"name": "Carol"}\n```') == {"name": "Carol"}


def test_parse_ai_with_whitespace():
    assert _parse_ai('  \n{"role": "Dev"}  \n') == {"role": "Dev"}
