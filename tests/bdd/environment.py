"""Окружение behave: изолированная БД и цикл событий для шагов.

Переменные окружения выставляются до импорта `config` — он читает их на
импорте, а `load_dotenv()` не перетирает уже заданные. Запускать behave нужно
из корня проекта: `main.py` монтирует `static/` и `templates/` по путям
относительно рабочего каталога.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

# Тот же набор, что и в tests/conftest.py: отдельный каталог данных (чтобы
# сценарии не писали в рабочую БД), http-APP_URL (иначе session-cookie уйдёт
# с флагом Secure и клиент её не вернёт) и выключенный лимитер — его счётчики
# живут в памяти процесса и копили бы запросы всех сценариев в одно ведро.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-behave")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="behave-data-")
os.environ["APP_URL"] = "http://test"
os.environ["RATE_LIMIT_ENABLED"] = "0"


def before_all(context) -> None:
    import main

    # Клиент ходит в ASGI-приложение напрямую, lifespan при этом не выполняется —
    # схему создаём руками, иначе первый же запрос к БД упадёт на missing table.
    main.init_db()
    context.app = main.app
    # Шаги синхронные, а httpx-клиент асинхронный: держим один цикл на прогон
    # и прокидываем в шаги хелпер context.run(coro).
    context.loop = asyncio.new_event_loop()
    context.run = context.loop.run_until_complete


def after_scenario(context, scenario) -> None:
    client = getattr(context, "client", None)
    if client is not None:
        context.run(client.aclose())


def after_all(context) -> None:
    context.loop.close()
