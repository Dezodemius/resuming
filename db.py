"""Слой доступа к данным: соединение SQLite (WAL) и инициализация схемы.

`get_db`/`init_db` читают путь из config.DB_PATH в момент вызова — это позволяет
тестам подменять каталог БД через monkeypatch(config, "DB_PATH", ...).

`get_db()` — контекстный менеджер: коммитит при успешном выходе, откатывает при
исключении и **всегда закрывает** соединение. Голый sqlite3.Connection как
контекстный менеджер соединение не закрывает — при одном процессе и сотнях
запросов это утечка файловых дескрипторов и WAL-читателей.
"""
import sqlite3
from contextlib import contextmanager
from typing import Iterator

import config


def connect() -> sqlite3.Connection:
    """Открыть настроенное соединение. Закрывать обязан вызывающий."""
    conn = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")       # параллельные чтения без блокировок
    conn.execute("PRAGMA synchronous=NORMAL")     # баланс скорость/надёжность
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=10000")
    conn.execute("PRAGMA busy_timeout=30000")     # ждать снятия блокировки, а не падать сразу
    return conn


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                email       TEXT UNIQUE COLLATE NOCASE,
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

            -- Очистка протухшего идёт по expires_at — без индекса это full scan
            CREATE INDEX IF NOT EXISTS idx_sessions_expires  ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_magic_expires     ON magic_tokens(expires_at);
            CREATE INDEX IF NOT EXISTS idx_anon_created      ON anon_usage(created);

            -- Промокоды для маркетинга и тестирования
            CREATE TABLE IF NOT EXISTS promo_codes (
                code       TEXT PRIMARY KEY,
                kind       TEXT NOT NULL CHECK (kind IN ('pro_days','gen_pack','unlimited')),
                value      INTEGER NOT NULL DEFAULT 0,
                max_uses   INTEGER NOT NULL DEFAULT 1,
                used_count INTEGER NOT NULL DEFAULT 0,
                active     INTEGER NOT NULL DEFAULT 1,
                expires_at TEXT,
                comment    TEXT,
                created    TEXT DEFAULT (datetime('now'))
            );

            -- Фиксация активации кода для пользователя (предотвращение повторных активаций)
            CREATE TABLE IF NOT EXISTS promo_activations (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                code    TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created TEXT DEFAULT (datetime('now')),
                UNIQUE(code, user_id)
            );

            -- Журнал событий: логины, генерации, платежи, активации промокодов
            CREATE TABLE IF NOT EXISTS usage_events (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                anon_id TEXT,
                event   TEXT NOT NULL,
                meta    TEXT,
                created TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_events_event_created ON usage_events(event, created);
            CREATE INDEX IF NOT EXISTS idx_events_user_created  ON usage_events(user_id, created);
            CREATE INDEX IF NOT EXISTS idx_events_created       ON usage_events(created);
        """)
        migrate(db)


# ── Миграции ────────────────────────────────────────────────────────────────
# `CREATE TABLE IF NOT EXISTS` в init_db() умеет только создавать таблицы с
# нуля: на базе, где таблица уже есть, он молча ничего не делает. Поэтому любое
# изменение существующей схемы должно ехать отдельным шагом — иначе оно
# применяется к новым базам и не применяется к рабочим, а расхождение
# обнаруживается уже в проде, при первом запросе к несуществующей колонке.
#
# Версия схемы живёт в `PRAGMA user_version` — счётчике внутри самого файла
# базы, ничего дополнительно хранить не нужно. Шаги применяются по возрастанию
# и ровно один раз.
#
# Правила для новых шагов:
#   • шаг идемпотентен настолько, насколько возможно, и не падает на пустой базе;
#   • шаг не переиспользует функции приложения — он должен работать и через год,
#     когда те функции изменятся;
#   • добавили шаг — подняли SCHEMA_VERSION и дописали тест в tests/test_db.py.
SCHEMA_VERSION = 1


def migrate(db: sqlite3.Connection) -> int:
    """Догоняет схему до SCHEMA_VERSION. Возвращает число применённых шагов."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    applied = 0
    if version < 1:
        _migration_1_case_insensitive_email(db)
        db.execute("PRAGMA user_version = 1")
        applied += 1
    if applied:
        db.commit()
    return applied


def _pick_survivor(rows: list[sqlite3.Row]) -> sqlite3.Row:
    """Из группы дублей выбираем тот аккаунт, потеря которого дороже.

    Сначала активная Pro-подписка с самым дальним сроком: именно её потеря —
    та самая жалоба «заплатил и всё пропало». При прочих равных — самый старый
    аккаунт, он вероятнее связан с остальными данными.
    """
    def rank(row):
        expires = row["pro_expires_at"] or ""
        return (1 if row["is_pro"] else 0, expires, -row["id"])

    return max(rows, key=rank)


def _merge_user(db: sqlite3.Connection, survivor_id: int, loser: sqlite3.Row) -> None:
    """Переносит всё, что принадлежало дублю, на выживший аккаунт."""
    loser_id = loser["id"]

    # Таблицы без ограничений уникальности на user_id — просто перевешиваем.
    for table in ("sessions", "resumes", "payments", "usage_events"):
        db.execute(f"UPDATE {table} SET user_id=? WHERE user_id=?", (survivor_id, loser_id))

    # profiles.user_id UNIQUE: профиль выжившего приоритетнее, он свежее по
    # смыслу (человек пользовался тем аккаунтом, который мы оставляем).
    has_profile = db.execute(
        "SELECT 1 FROM profiles WHERE user_id=?", (survivor_id,)
    ).fetchone()
    if has_profile:
        db.execute("DELETE FROM profiles WHERE user_id=?", (loser_id,))
    else:
        db.execute("UPDATE profiles SET user_id=? WHERE user_id=?", (survivor_id, loser_id))

    # promo_activations UNIQUE(code, user_id): переносим только те коды,
    # которых у выжившего ещё нет, остальные отбрасываем — повторная активация
    # одного кода на один аккаунт и так запрещена.
    db.execute(
        "UPDATE OR IGNORE promo_activations SET user_id=? WHERE user_id=?",
        (survivor_id, loser_id),
    )
    db.execute("DELETE FROM promo_activations WHERE user_id=?", (loser_id,))

    # MCP-токен у пользователя один активный; у выжившего он свой, чужой не
    # переносим — его придётся перевыпустить (POST /api/mcp-token).
    db.execute("DELETE FROM api_tokens WHERE user_id=?", (loser_id,))

    # Счётчики. paid_left складываем — это купленное, терять нельзя. free_left
    # берём максимум, а не сумму: бесплатные генерации выдаются на человека, и
    # сложение подарило бы их за сам факт дубля.
    survivor = db.execute("SELECT * FROM users WHERE id=?", (survivor_id,)).fetchone()
    pro_expires = max(
        (r or "") for r in (survivor["pro_expires_at"], loser["pro_expires_at"])
    ) or None
    db.execute(
        "UPDATE users SET free_left=?, paid_left=?, is_pro=?, pro_expires_at=? WHERE id=?",
        (
            max(survivor["free_left"], loser["free_left"]),
            survivor["paid_left"] + loser["paid_left"],
            1 if (survivor["is_pro"] or loser["is_pro"]) else 0,
            pro_expires,
            survivor_id,
        ),
    )
    db.execute("DELETE FROM users WHERE id=?", (loser_id,))


def _migration_1_case_insensitive_email(db: sqlite3.Connection) -> None:
    """email становится ключом аккаунта без учёта регистра.

    `TEXT UNIQUE` в SQLite сравнивает побайтово, а адрес приходит то из формы
    как человек набрал (`Ivan@ya.ru`), то из OAuth в нижнем регистре
    (`ivan@ya.ru`) — и один человек получал два аккаунта, теряя резюме и
    оплаченный Pro.

    Три шага: слить уже возникшие дубли, привести адреса к нижнему регистру и
    пересобрать таблицу с `COLLATE NOCASE`, чтобы повторить это стало нельзя.
    Порядок важен — уникальный индекс без учёта регистра не создастся, пока
    дубли на месте.
    """
    duplicates = db.execute(
        "SELECT lower(email) AS key FROM users"
        " WHERE email IS NOT NULL AND trim(email) <> ''"
        " GROUP BY lower(email) HAVING COUNT(*) > 1"
    ).fetchall()
    for group in duplicates:
        rows = db.execute(
            "SELECT * FROM users WHERE lower(email)=? ORDER BY id", (group["key"],)
        ).fetchall()
        survivor = _pick_survivor(rows)
        for row in rows:
            if row["id"] != survivor["id"]:
                _merge_user(db, survivor["id"], row)

    db.execute("UPDATE users SET email=lower(email) WHERE email IS NOT NULL")
    db.execute("UPDATE magic_tokens SET email=lower(email) WHERE email IS NOT NULL")

    # Сменить collation у колонки ALTER-ом нельзя — только пересобрать таблицу.
    db.executescript("""
        CREATE TABLE users_migrated (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            email       TEXT UNIQUE COLLATE NOCASE,
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
        INSERT INTO users_migrated
            (id, email, telegram_id, tg_name, tg_photo, display_name,
             free_left, paid_left, is_pro, pro_expires_at, created)
        SELECT id, email, telegram_id, tg_name, tg_photo, display_name,
               free_left, paid_left, is_pro, pro_expires_at, created
        FROM users;
        DROP TABLE users;
        ALTER TABLE users_migrated RENAME TO users;
    """)
