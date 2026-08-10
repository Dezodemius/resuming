import main


EXPECTED_TABLES = {
    "users", "sessions", "magic_tokens", "profiles",
    "resumes", "payments", "anon_usage",
}


def test_init_db_creates_all_tables(db):
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert EXPECTED_TABLES <= tables


def test_init_db_idempotent(tmp_path, monkeypatch):
    import config
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    main.init_db()
    main.init_db()  # second call must not raise
    with main.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_get_db_closes_connection_on_exit(db):
    """get_db() обязан закрывать соединение — иначе дескрипторы текут."""
    import pytest
    import sqlite3

    with main.get_db() as conn:
        conn.execute("SELECT 1")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_get_db_rolls_back_on_exception(db):
    """Исключение внутри блока не должно оставлять полузапись в БД."""
    import pytest

    with pytest.raises(RuntimeError):
        with main.get_db() as conn:
            conn.execute("INSERT INTO users (email) VALUES (?)", ("rollback@test.com",))
            raise RuntimeError("boom")

    row = db.execute(
        "SELECT COUNT(*) c FROM users WHERE email=?", ("rollback@test.com",)
    ).fetchone()
    assert row["c"] == 0


def test_new_user_default_free_credits(db):
    db.execute("INSERT INTO users (email) VALUES (?)", ("u@test.com",))
    db.commit()
    row = db.execute("SELECT free_left, paid_left FROM users WHERE email=?", ("u@test.com",)).fetchone()
    assert row["free_left"] == 3
    assert row["paid_left"] == 0
