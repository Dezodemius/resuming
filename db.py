"""Слой доступа к данным: соединение SQLite (WAL) и инициализация схемы.

`get_db`/`init_db` читают путь из config.DB_PATH в момент вызова — это позволяет
тестам подменять каталог БД через monkeypatch(config, "DB_PATH", ...).
"""
import sqlite3

import config


def get_db():
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # параллельные чтения без блокировок
    conn.execute("PRAGMA synchronous=NORMAL")     # баланс скорость/надёжность
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=10000")
    return conn


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT UNIQUE,
                telegram_id INTEGER UNIQUE,
                tg_name     TEXT,
                tg_photo    TEXT,
                display_name TEXT,
                free_left    INTEGER NOT NULL DEFAULT 3,
                paid_left    INTEGER NOT NULL DEFAULT 0,
                is_pro       INTEGER NOT NULL DEFAULT 0,
                pro_expires_at TEXT,
                created      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id         TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                created    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS magic_tokens (
                token      TEXT PRIMARY KEY,
                email      TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used       INTEGER DEFAULT 0,
                created    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS profiles (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE NOT NULL,
                data    TEXT NOT NULL,
                updated TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS resumes (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                company_name TEXT,
                job_url      TEXT,
                job_snippet  TEXT,
                resume_data  TEXT NOT NULL,
                kind         TEXT DEFAULT 'matched',
                status       TEXT DEFAULT 'draft',
                created      TEXT DEFAULT (datetime('now')),
                updated      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS payments (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id  INTEGER NOT NULL,
                pay_id   TEXT,
                idem_key TEXT UNIQUE,
                status   TEXT DEFAULT 'pending',
                created  TEXT DEFAULT (datetime('now'))
            );

            -- Анонимные превью: ограничиваем по cookie-id, без привязки к аккаунту
            CREATE TABLE IF NOT EXISTS anon_usage (
                anon_id  TEXT PRIMARY KEY,
                uses     INTEGER DEFAULT 0,
                created  TEXT DEFAULT (datetime('now'))
            );

            -- API-токены для MCP-доступа (один активный токен на пользователя)
            CREATE TABLE IF NOT EXISTS api_tokens (
                token      TEXT PRIMARY KEY,
                user_id    INTEGER NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- Индексы под частые выборки по владельцу (иначе full scan при росте)
            CREATE INDEX IF NOT EXISTS idx_resumes_user_id   ON resumes(user_id);
            CREATE INDEX IF NOT EXISTS idx_sessions_user_id  ON sessions(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_user_id  ON payments(user_id);
            CREATE INDEX IF NOT EXISTS idx_payments_pay_id   ON payments(pay_id);
            CREATE INDEX IF NOT EXISTS idx_api_tokens_user_id ON api_tokens(user_id);
        """)
