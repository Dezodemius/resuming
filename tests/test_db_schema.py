"""Сторож схемы: слепок того, что создаёт init_db().

Правило проекта («Изменение схемы» в CLAUDE.md) до сих пор держалось только на
внимательности. `CREATE TABLE IF NOT EXISTS` в init_db() на рабочей базе не
делает ничего, поэтому дописанную туда колонку получают лишь новые базы: прод
и дев расходятся молча, а обнаруживается это первым запросом к несуществующей
колонке — в проде.

Слепок ниже делает правило механическим: любая правка init_db() роняет тест, и
автор обязан либо признать изменение осознанным, либо (почти всегда) написать
шаг миграции. Слепок перечисляет схему целиком, а не «часть, о которой
вспомнили»: EXPECTED_TABLES в tests/test_db.py проверяет вхождение (`<=`) и
потому не замечает ни новых таблиц, ни новых колонок — на момент написания он
не знал о пяти из двенадцати таблиц.
"""
import db as db_module


_EXPECTED_COLUMNS = {
    "anon_usage": ["anon_id", "created", "uses"],
    "api_tokens": ["created_at", "token", "user_id"],
    "magic_tokens": ["created", "email", "expires_at", "token", "used"],
    "oauth_identities": ["created", "email_at_link", "provider", "provider_uid", "user_id"],
    "payments": ["amount", "created", "id", "idem_key", "pay_id", "product", "status", "user_id"],
    "profiles": ["data", "id", "updated", "user_id"],
    "promo_activations": ["code", "created", "id", "user_id"],
    "promo_codes": ["active", "code", "comment", "created", "expires_at", "kind",
                    "max_uses", "used_count", "value"],
    "resumes": ["company_name", "created", "id", "job_snippet", "job_url", "kind",
                "resume_data", "status", "updated", "user_id"],
    "sessions": ["created", "expires_at", "id", "user_id"],
    "usage_events": ["anon_id", "created", "event", "id", "meta", "user_id"],
    "users": ["created", "display_name", "email", "free_left", "id", "is_pro",
              "paid_left", "pro_expires_at"],
}

_EXPECTED_INDEXES = [
    "idx_anon_created",
    "idx_api_tokens_user_id",
    "idx_events_created",
    "idx_events_event_created",
    "idx_events_user_created",
    "idx_magic_expires",
    "idx_oauth_identities_user",
    "idx_payments_pay_id",
    "idx_payments_user_id",
    "idx_resumes_user_id",
    "idx_sessions_expires",
    "idx_sessions_user_id",
]

_SCHEMA_CHANGE_HINT = (
    "\n\n"
    "Схема, которую создаёт init_db(), изменилась.\n"
    "Это не повод просто обновить список выше: на рабочих базах таблицы уже\n"
    "существуют, и CREATE TABLE IF NOT EXISTS их не тронет — изменение\n"
    "получат только новые базы, а прод останется на старой схеме.\n"
    "Порядок такой:\n"
    "  1. добавить шаг миграции в db.py (_migration_N_...);\n"
    "  2. поднять db.SCHEMA_VERSION;\n"
    "  3. дописать тест на сам шаг (см. test_migration_* в tests/test_db.py);\n"
    "  4. и только потом обновить список в этом файле.\n"
    "Если изменение нужно лишь новым базам — так и напишите в комментарии рядом."
)


def _table_columns(conn) -> dict[str, list[str]]:
    tables = sorted(
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    )
    return {
        table: sorted(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))
        for table in tables
    }


def test_init_db_schema_matches_the_recorded_snapshot(db):
    assert _table_columns(db) == _EXPECTED_COLUMNS, _SCHEMA_CHANGE_HINT


def test_init_db_creates_the_recorded_indexes(db):
    """Индексы — тоже схема.

    Потерянный индекс не ломает ни один тест: запросы продолжают отвечать, но
    выборки по владельцу уходят в full scan, и заметно это становится только
    на живых объёмах.
    """
    indexes = sorted(
        row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        )
    )
    assert indexes == _EXPECTED_INDEXES, _SCHEMA_CHANGE_HINT


def test_fresh_db_is_stamped_with_the_current_schema_version(db):
    """Новая база помечается текущей версией, и миграции по ней не гоняются.

    Без пометки следующий запуск прогонит по свежей базе шаги, написанные под
    схему прошлых релизов, — и откатит её назад.
    """
    assert db.execute("PRAGMA user_version").fetchone()[0] == db_module.SCHEMA_VERSION


def test_every_migration_step_has_a_matching_schema_version():
    """Число шагов в migrate() и SCHEMA_VERSION обязаны совпадать.

    Разойтись они могут в обе стороны, и обе плохи: шаг без поднятой версии
    никогда не применится, поднятая версия без шага навсегда пометит базы
    как мигрировавшие — и настоящий шаг под этим номером уже не выполнится.
    """
    import inspect
    import re

    source = inspect.getsource(db_module.migrate)
    steps = sorted(int(n) for n in re.findall(r"PRAGMA user_version = (\d+)", source))
    assert steps == list(range(1, db_module.SCHEMA_VERSION + 1)), (
        f"шаги в migrate(): {steps}, SCHEMA_VERSION={db_module.SCHEMA_VERSION}"
    )
