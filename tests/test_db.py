import sqlite3

import db as db_module
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


# ── Миграции схемы ──────────────────────────────────────────────────────────
# `CREATE TABLE IF NOT EXISTS` не трогает уже существующие таблицы, поэтому
# изменения схемы едут отдельными шагами. Проверяем и сам механизм, и первый
# шаг — тот, что делает email ключом аккаунта без учёта регистра.


# Схема users до миграции 1: UNIQUE сравнивает побайтово.
_OLD_USERS = """
    CREATE TABLE users (
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
"""


def _legacy_db(tmp_path, monkeypatch):
    """База со старой схемой users и остальными таблицами из init_db."""
    import config

    path = str(tmp_path / "legacy.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    main.init_db()                      # создаёт всё и прогоняет миграции
    conn = db_module.connect()
    # Откатываем users к старому виду и обнуляем версию — как если бы база
    # осталась с прежнего релиза.
    conn.executescript("DROP TABLE users;" + _OLD_USERS + "PRAGMA user_version = 0;")
    conn.commit()
    return conn


def test_fresh_db_email_is_case_insensitive(db):
    db.execute("INSERT INTO users (email) VALUES ('Ivan@ya.ru')")
    db.commit()
    row = db.execute("SELECT id FROM users WHERE email='ivan@YA.RU'").fetchone()
    assert row is not None, "поиск по адресу должен игнорировать регистр"


def test_fresh_db_rejects_case_variant_duplicate(db):
    db.execute("INSERT INTO users (email) VALUES ('ivan@ya.ru')")
    db.commit()
    try:
        db.execute("INSERT INTO users (email) VALUES ('IVAN@ya.ru')")
        db.commit()
        assert False, "второй аккаунт на тот же адрес создаваться не должен"
    except sqlite3.IntegrityError:
        pass


def test_migrate_is_idempotent(db):
    assert db.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
    assert db_module.migrate(db) == 0, "повторный прогон не должен ничего делать"


def test_migration_lowercases_existing_emails(tmp_path, monkeypatch):
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'Ivan@Ya.RU')")
    conn.execute("INSERT INTO magic_tokens (token, email, expires_at)"
                 " VALUES ('t', 'Ivan@Ya.RU', datetime('now','+10 minutes'))")
    conn.commit()

    assert db_module.migrate(conn) == db_module.SCHEMA_VERSION
    assert conn.execute("SELECT email FROM users WHERE id=1").fetchone()[0] == "ivan@ya.ru"
    assert conn.execute("SELECT email FROM magic_tokens").fetchone()[0] == "ivan@ya.ru"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
    conn.close()


def test_migration_merges_duplicates_keeping_paid_account(tmp_path, monkeypatch):
    """Главное свойство: оплаченный Pro не должен пропасть при слиянии."""
    conn = _legacy_db(tmp_path, monkeypatch)
    # id=1 — бесплатный, id=2 — тот же человек с оплаченным Pro.
    conn.execute("INSERT INTO users (id, email, free_left, paid_left, is_pro, pro_expires_at)"
                 " VALUES (1, 'Ivan@ya.ru', 1, 5, 0, NULL)")
    conn.execute("INSERT INTO users (id, email, free_left, paid_left, is_pro, pro_expires_at)"
                 " VALUES (2, 'ivan@ya.ru', 3, 20, 1, '2099-01-01 00:00:00')")
    conn.execute("INSERT INTO resumes (user_id, resume_data) VALUES (1, '{}')")
    conn.execute("INSERT INTO profiles (user_id, data) VALUES (1, '{}')")
    conn.execute("INSERT INTO payments (user_id, pay_id) VALUES (1, 'p1')")
    conn.commit()

    db_module.migrate(conn)

    rows = conn.execute("SELECT * FROM users").fetchall()
    assert len(rows) == 1, "дубли должны схлопнуться в один аккаунт"
    survivor = rows[0]
    assert survivor["id"] == 2, "выживает аккаунт с оплаченной подпиской"
    assert survivor["email"] == "ivan@ya.ru"
    assert survivor["is_pro"] == 1
    assert survivor["pro_expires_at"] == "2099-01-01 00:00:00"
    assert survivor["paid_left"] == 25, "купленные генерации складываются"
    assert survivor["free_left"] == 3, "бесплатные не суммируются, берётся максимум"

    # Данные проигравшего перевешены, а не потеряны.
    assert conn.execute("SELECT user_id FROM resumes").fetchone()["user_id"] == 2
    assert conn.execute("SELECT user_id FROM profiles").fetchone()["user_id"] == 2
    assert conn.execute("SELECT user_id FROM payments").fetchone()["user_id"] == 2
    conn.close()


def test_migration_keeps_survivor_profile(tmp_path, monkeypatch):
    """profiles.user_id UNIQUE: если профиль есть у обоих, остаётся один."""
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'a@b.ru')")
    conn.execute("INSERT INTO users (id, email) VALUES (2, 'A@B.ru')")
    conn.execute("INSERT INTO profiles (user_id, data) VALUES (1, '{\"n\":1}')")
    conn.execute("INSERT INTO profiles (user_id, data) VALUES (2, '{\"n\":2}')")
    conn.commit()

    db_module.migrate(conn)

    profiles = conn.execute("SELECT user_id, data FROM profiles").fetchall()
    assert len(profiles) == 1
    assert profiles[0]["user_id"] == 1, "при равных правах выживает старший аккаунт"
    assert profiles[0]["data"] == '{"n":1}', "остаётся профиль выжившего"
    survivor = conn.execute("SELECT is_pro FROM users").fetchone()
    assert survivor["is_pro"] == 0, "слияние двух бесплатных не выдаёт Pro"
    conn.close()


def test_migration_drops_telegram_columns(tmp_path, monkeypatch):
    """Вход через Telegram убран — колонки уходят вместе с ним."""
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email, telegram_id, tg_name)"
                 " VALUES (1, 'ivan@ya.ru', 111, 'Иван')")
    conn.commit()

    db_module.migrate(conn)

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert not (columns & {"telegram_id", "tg_name", "tg_photo"})
    row = conn.execute("SELECT id, email FROM users").fetchone()
    assert row["id"] == 1 and row["email"] == "ivan@ya.ru", "аккаунт с почтой сохраняется"
    conn.close()


def test_migration_removes_accounts_without_email(tmp_path, monkeypatch):
    """Аккаунт, у которого был только telegram_id, войти уже не может — его
    способ входа исчез. Оставлять такую строку значит считать её живым
    пользователем в статистике."""
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email, telegram_id) VALUES (1, NULL, 111)")
    conn.execute("INSERT INTO users (id, email, telegram_id) VALUES (2, '', 222)")
    conn.execute("INSERT INTO users (id, email, telegram_id) VALUES (3, 'ivan@ya.ru', 333)")
    conn.commit()

    db_module.migrate(conn)

    rows = conn.execute("SELECT id FROM users").fetchall()
    assert [r["id"] for r in rows] == [3]
    conn.close()


def test_migration_2_is_idempotent_on_new_schema(tmp_path, monkeypatch):
    """Шаг не должен ничего делать, если колонок уже нет."""
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'ivan@ya.ru')")
    conn.commit()
    db_module.migrate(conn)
    before = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    db_module._migration_2_drop_telegram_columns(conn)

    assert conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == before
    conn.close()


def test_migration_makes_column_case_insensitive(tmp_path, monkeypatch):
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'ivan@ya.ru')")
    conn.commit()
    db_module.migrate(conn)
    try:
        conn.execute("INSERT INTO users (email) VALUES ('IVAN@YA.RU')")
        conn.commit()
        assert False, "после миграции дубль по регистру создаваться не должен"
    except sqlite3.IntegrityError:
        pass
    conn.close()


# ── Выбор выжившего при слиянии ─────────────────────────────────────────────
# Правило: дороже всего потерять оплаченное, поэтому решает сначала активная
# подписка и её срок, и лишь при равенстве — возраст аккаунта.

def test_pick_survivor_prefers_furthest_paid_subscription():
    rows = [
        {"id": 1, "is_pro": 1, "pro_expires_at": "2026-01-01 00:00:00"},
        {"id": 2, "is_pro": 1, "pro_expires_at": "2099-01-01 00:00:00"},
    ]
    assert db_module._pick_survivor(rows)["id"] == 2


def test_pick_survivor_prefers_paid_over_free_at_equal_dates():
    """Флаг подписки должен решать сам по себе, а не только через срок."""
    rows = [
        {"id": 1, "is_pro": 0, "pro_expires_at": "2099-01-01 00:00:00"},
        {"id": 2, "is_pro": 1, "pro_expires_at": "2099-01-01 00:00:00"},
    ]
    assert db_module._pick_survivor(rows)["id"] == 2


def test_pick_survivor_handles_missing_expiry():
    """pro_expires_at пустой у большинства аккаунтов — сравнение не должно
    падать и не должно ставить их выше оплаченных."""
    rows = [
        {"id": 1, "is_pro": 0, "pro_expires_at": None},
        {"id": 2, "is_pro": 1, "pro_expires_at": "2099-01-01 00:00:00"},
    ]
    assert db_module._pick_survivor(rows)["id"] == 2

    both_empty = [
        {"id": 5, "is_pro": 0, "pro_expires_at": None},
        {"id": 9, "is_pro": 0, "pro_expires_at": None},
    ]
    assert db_module._pick_survivor(both_empty)["id"] == 5, "при равенстве — старший"


def test_pick_survivor_ignores_account_without_expiry_date():
    """Оба помечены Pro, но у одного срок не проставлен. Побеждать должен тот,
    у кого срок есть: заглушка для пустого значения обязана быть «меньше» любой
    настоящей даты, иначе аккаунт без срока перевесит оплаченный."""
    rows = [
        {"id": 1, "is_pro": 1, "pro_expires_at": None},
        {"id": 2, "is_pro": 1, "pro_expires_at": "2027-05-05 00:00:00"},
    ]
    assert db_module._pick_survivor(rows)["id"] == 2


def test_migrate_applies_only_missing_steps(tmp_path, monkeypatch):
    """База, уже прошедшая шаг 1, должна получить ровно недостающие шаги (2, 3, 4).

    Если условие версии съедет и шаг 1 выполнится повторно, он пересоберёт
    users по своему списку колонок и вернёт колонки Telegram обратно.
    """
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email, telegram_id) VALUES (1, 'ivan@ya.ru', 111)")
    conn.commit()

    db_module._migration_1_case_insensitive_email(conn)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    assert db_module.migrate(conn) == 3, "должны примениться только шаги 2, 3 и 4, не шаг 1 повторно"
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    assert not (columns & {"telegram_id", "tg_name", "tg_photo"})
    assert db_module.migrate(conn) == 0, "повторный прогон ничего не делает"
    conn.close()


# ── Миграция 3: payments получает amount/product (issue #43) ───────────────
# `payments` не участвует ни в UNIQUE-пересборках, ни в FOREIGN KEY соседних
# таблиц, поэтому шаг — просто ALTER TABLE ADD COLUMN, без пересоздания
# таблицы. Проверяем отдельно от users-миграций: там `_legacy_db()` уже создаёт
# payments по актуальной (пост-правочной) схеме через init_db(), так что ветку
# ALTER TABLE она не задевает.
_OLD_PAYMENTS = """
    CREATE TABLE payments (
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id  INTEGER NOT NULL,
        pay_id   TEXT,
        idem_key TEXT UNIQUE,
        status   TEXT DEFAULT 'pending',
        created  TEXT DEFAULT (datetime('now'))
    );
"""


def _pre_migration_3_db(tmp_path, monkeypatch):
    """База на SCHEMA_VERSION=2: payments ещё без amount/product."""
    import config

    path = str(tmp_path / "pre-migration-3.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    main.init_db()
    conn = db_module.connect()
    conn.executescript("DROP TABLE payments;" + _OLD_PAYMENTS + "PRAGMA user_version = 2;")
    conn.commit()
    return conn


def test_migration_3_adds_amount_and_product_columns(tmp_path, monkeypatch):
    conn = _pre_migration_3_db(tmp_path, monkeypatch)
    conn.execute(
        "INSERT INTO payments (user_id, pay_id, idem_key, status) VALUES (1, '5', 'idem-5', 'pending')"
    )
    conn.commit()

    assert db_module.migrate(conn) == 2, "должны примениться шаги 3 и 4"
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(payments)").fetchall()}
    assert {"amount", "product"} <= columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION

    # Старая строка не потеряна, новые колонки у неё пустые (main.py при чтении
    # подставляет вместо NULL прежнюю PRO_PRICE).
    row = conn.execute("SELECT pay_id, amount, product FROM payments WHERE id=1").fetchone()
    assert row["pay_id"] == "5"
    assert row["amount"] is None
    assert row["product"] is None
    conn.close()


def test_migration_3_is_idempotent(tmp_path, monkeypatch):
    conn = _pre_migration_3_db(tmp_path, monkeypatch)
    assert db_module.migrate(conn) == 2
    assert db_module.migrate(conn) == 0, "повторный прогон не должен ничего делать"
    conn.close()


def test_migration_3_guard_short_circuits_on_new_schema(tmp_path, monkeypatch):
    """Прямой вызов шага на уже мигрированной базе не должен падать
    (columns already present) — как и остальные шаги, он идемпотентен сам
    по себе, не только через migrate()."""
    conn = _pre_migration_3_db(tmp_path, monkeypatch)
    db_module.migrate(conn)
    before = {row["name"] for row in conn.execute("PRAGMA table_info(payments)").fetchall()}

    db_module._migration_3_payment_amount_product(conn)

    after = {row["name"] for row in conn.execute("PRAGMA table_info(payments)").fetchall()}
    assert before == after
    conn.close()


def test_fresh_db_payments_has_amount_and_product_columns(db):
    """Новая база создаётся сразу с итоговой схемой — колонки есть без миграции."""
    columns = {row["name"] for row in db.execute("PRAGMA table_info(payments)").fetchall()}
    assert {"amount", "product"} <= columns


def test_migration_2_guard_short_circuits(tmp_path, monkeypatch):
    """Проверка «колонок уже нет» обязана останавливать шаг целиком.

    Иначе он повторно чистит аккаунты без email — а на новой схеме такой
    аккаунт законен: это ещё не подтверждённая запись, а не остаток Telegram.
    """
    conn = _legacy_db(tmp_path, monkeypatch)
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'ivan@ya.ru')")
    conn.commit()
    db_module.migrate(conn)

    conn.execute("INSERT INTO users (id, email) VALUES (2, NULL)")
    conn.commit()
    db_module._migration_2_drop_telegram_columns(conn)

    ids = [r["id"] for r in conn.execute("SELECT id FROM users ORDER BY id")]
    assert ids == [1, 2], "шаг должен был выйти сразу, ничего не трогая"
    conn.close()


# ── Миграция 4: oauth_identities ─────────────────────────────────────────────

def _pre_migration_4_db(tmp_path, monkeypatch):
    """База на SCHEMA_VERSION=3: oauth_identities ещё не существует."""
    import config

    path = str(tmp_path / "pre-migration-4.db")
    monkeypatch.setattr(config, "DB_PATH", path)
    main.init_db()
    conn = db_module.connect()
    conn.executescript("DROP TABLE oauth_identities; PRAGMA user_version = 3;")
    conn.commit()
    return conn


def test_fresh_db_has_oauth_identities_table(db):
    """Новая база создаётся сразу с итоговой схемой — таблица есть без миграции."""
    tables = {row["name"] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "oauth_identities" in tables


def test_migration_4_creates_oauth_identities_table(tmp_path, monkeypatch):
    conn = _pre_migration_4_db(tmp_path, monkeypatch)

    assert db_module.migrate(conn) == 1, "должен примениться только шаг 4"
    tables = {row["name"] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "oauth_identities" in tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION
    conn.close()


def test_migration_4_table_enforces_one_user_per_provider_identity(tmp_path, monkeypatch):
    """PRIMARY KEY (provider, provider_uid) — тот же provider_uid дважды не заводит вторую строку."""
    conn = _pre_migration_4_db(tmp_path, monkeypatch)
    db_module.migrate(conn)
    conn.execute("INSERT INTO users (id, email) VALUES (1, 'ivan@ya.ru')")
    conn.execute(
        "INSERT INTO oauth_identities (provider, provider_uid, user_id) VALUES ('vk', '42', 1)"
    )
    conn.commit()
    try:
        conn.execute(
            "INSERT INTO oauth_identities (provider, provider_uid, user_id) VALUES ('vk', '42', 1)"
        )
        conn.commit()
        assert False, "повторная привязка того же provider_uid должна упасть на PRIMARY KEY"
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_migration_4_is_idempotent(tmp_path, monkeypatch):
    conn = _pre_migration_4_db(tmp_path, monkeypatch)
    assert db_module.migrate(conn) == 1
    assert db_module.migrate(conn) == 0, "повторный прогон не должен ничего делать"
    conn.close()
