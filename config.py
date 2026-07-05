"""Конфигурация приложения: переменные окружения, константы, логгер.

Все настройки собраны здесь, чтобы остальные модули импортировали их отсюда,
а не дублировали чтение os.getenv. Импортируется первым — выполняет load_dotenv,
создаёт каталог данных и настраивает логирование.
"""
import logging
import os

from dotenv import load_dotenv

load_dotenv()

# ── Пути / база данных ──────────────────────────────────────────────────────
# В Docker используем volume /app/data, локально — каталог data рядом с кодом.
DATA_DIR = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "resume.db")

# Совместимость: раньше переменная называлась _data_dir.
_data_dir = DATA_DIR

# ── Logging ─────────────────────────────────────────────────────────────────
# Логи идут И в stdout (docker logs), И в ротируемый файл на volume DATA_DIR —
# чтобы история переживала перезапуски и не зависела от docker log-драйвера.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.getenv("LOG_DIR", os.path.join(DATA_DIR, "logs"))

if LOG_DIR:
    os.makedirs(LOG_DIR, exist_ok=True)

_log_formatter = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    from logging.handlers import RotatingFileHandler

    _file_handler = RotatingFileHandler(
        os.path.join(LOG_DIR, "app.log"),
        maxBytes=10 * 1024 * 1024,  # 10 МБ на файл
        backupCount=5,              # app.log + app.log.1 … app.log.5
        encoding="utf-8",
    )
    _log_handlers.append(_file_handler)
except OSError:
    # ФС только для чтения и т.п. — остаёмся на stdout.
    pass

for _h in _log_handlers:
    _h.setFormatter(_log_formatter)
logging.basicConfig(level=LOG_LEVEL, handlers=_log_handlers)
log = logging.getLogger("resuming")

# ── Внешние сервисы ─────────────────────────────────────────────────────────
OLLAMA_URL           = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL                = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
YOKASSA_SHOP         = os.getenv("YOKASSA_SHOP_ID", "")
YOKASSA_SECRET       = os.getenv("YOKASSA_SECRET_KEY", "")
APP_URL              = os.getenv("APP_URL", "http://localhost:8000")
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_NAME    = os.getenv("TELEGRAM_BOT_NAME", "")
SMTP_HOST            = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT            = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER            = os.getenv("SMTP_USER", "")
SMTP_PASS            = os.getenv("SMTP_PASS", "")
SMTP_FROM            = os.getenv("SMTP_FROM", SMTP_USER)
YANDEX_CLIENT_ID     = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")
VK_CLIENT_ID         = os.getenv("VK_CLIENT_ID", "")
VK_CLIENT_SECRET     = os.getenv("VK_CLIENT_SECRET", "")
MAILRU_CLIENT_ID     = os.getenv("MAILRU_CLIENT_ID", "")
MAILRU_CLIENT_SECRET = os.getenv("MAILRU_CLIENT_SECRET", "")
ADMIN_EMAILS         = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
METRIKA_ID           = os.getenv("METRIKA_ID", "")

# ── Лимиты / тарифы ─────────────────────────────────────────────────────────
FREE_USES        = 3
FREE_RESUMES     = 5
PRO_PRICE        = "399.00"
PRO_DAYS         = 30
ANON_LIMIT_CONST = 2
PAID_PACK        = 20
PACK_PRICE       = PRO_PRICE
SESSION_DAYS     = 30
MAGIC_MINUTES    = 15
AI_CONCURRENCY   = 2   # max одновременных вызовов Ollama

# Секрет для подписи anon-cookie. ОБЯЗАТЕЛЬНО задайте в .env!
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-" + os.urandom(16).hex())
