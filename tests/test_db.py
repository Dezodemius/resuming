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
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    main.init_db()
    main.init_db()  # second call must not raise
    conn = main.get_db()
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    conn.close()


def test_new_user_default_free_credits(db):
    db.execute("INSERT INTO users (email) VALUES (?)", ("u@test.com",))
    db.commit()
    row = db.execute("SELECT free_left, paid_left FROM users WHERE email=?", ("u@test.com",)).fetchone()
    assert row["free_left"] == 3
    assert row["paid_left"] == 0
