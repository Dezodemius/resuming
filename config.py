"""Конфигурация приложения: переменные окружения, константы, логгер.

Все настройки собраны здесь, чтобы остальные модули импортировали их отсюда,
а не дублировали чтение os.getenv. Импортируется первым — выполняет load_dotenv,
создаёт каталог данных и настраивает логирование.
"""
import logging
import os
from urllib.parse import urlsplit, urlunsplit

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
# Bearer-токен для внешних OpenAI-совместимых провайдеров (DeepSeek и т.п.).
# Пусто — заголовок Authorization не отправляется (локальная Ollama его не требует).
AI_API_KEY           = os.getenv("AI_API_KEY", "")
ROBOKASSA_LOGIN      = os.getenv("ROBOKASSA_LOGIN", "")
ROBOKASSA_PASSWORD1  = os.getenv("ROBOKASSA_PASSWORD1", "")
ROBOKASSA_PASSWORD2  = os.getenv("ROBOKASSA_PASSWORD2", "")
ROBOKASSA_TEST_MODE  = os.getenv("ROBOKASSA_TEST_MODE", "0") == "1"
def _idna_url(url: str) -> str:
    """Хост URL в punycode (IDNA). Браузер, Origin-заголовок и OAuth-провайдеры
    оперируют ASCII-формой домена, поэтому кириллический APP_URL ломает точное
    сравнение redirect_uri (Mail.ru отвечает invalid_grant) и CORS-матчинг.
    Для ASCII-хоста — no-op."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host or host.isascii():
            return url
        netloc = host.encode("idna").decode("ascii")
        if parts.port:
            netloc += f":{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except (UnicodeError, ValueError):
        return url

_raw_app_url = os.getenv("APP_URL", "http://localhost:8000")
APP_URL              = _idna_url(_raw_app_url)
if APP_URL != _raw_app_url:
    log.info("APP_URL нормализован в punycode: %s -> %s", _raw_app_url, APP_URL)
SMTP_HOST            = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT            = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER            = os.getenv("SMTP_USER", "")
SMTP_PASS            = os.getenv("SMTP_PASS", "")
SMTP_FROM            = os.getenv("SMTP_FROM", SMTP_USER)
YANDEX_CLIENT_ID     = os.getenv("YANDEX_CLIENT_ID", "")
YANDEX_CLIENT_SECRET = os.getenv("YANDEX_CLIENT_SECRET", "")
VK_CLIENT_ID         = os.getenv("VK_CLIENT_ID", "")  # секрет не нужен: VK ID работает по PKCE
MAILRU_CLIENT_ID     = os.getenv("MAILRU_CLIENT_ID", "")
MAILRU_CLIENT_SECRET = os.getenv("MAILRU_CLIENT_SECRET", "")
# Общий рубильник кнопок входа через Яндекс/VK/Mail.ru — по умолчанию выключены
# (нестабильны, требуют присмотра). Client ID/secret можно оставить настроенными
# и включить кнопки без смены конфигурации провайдера: OAUTH_LOGIN_ENABLED=1.
OAUTH_LOGIN_ENABLED  = os.getenv("OAUTH_LOGIN_ENABLED", "0") == "1"
# ── Какие способы входа реально включены ────────────────────────────────────
# Считаем это здесь, в одном месте, а не тремя одинаковыми выражениями в main:
# кнопка в шаблоне и проверка в самой ручке обязаны решать одинаково, иначе
# получится либо кнопка в никуда, либо рабочая ручка без кнопки.
#
# Провайдеру мало client_id: Яндексу и Mail.ru нужен ещё секрет для обмена кода
# на токен (VK ID работает по PKCE и секрета не требует). Настроенный наполовину
# провайдер хуже выключенного: кнопка есть, а вход неизбежно падает.
YANDEX_LOGIN_ENABLED = bool(OAUTH_LOGIN_ENABLED and YANDEX_CLIENT_ID and YANDEX_CLIENT_SECRET)
VK_LOGIN_ENABLED = bool(OAUTH_LOGIN_ENABLED and VK_CLIENT_ID)
MAILRU_LOGIN_ENABLED = bool(OAUTH_LOGIN_ENABLED and MAILRU_CLIENT_ID and MAILRU_CLIENT_SECRET)


def _login_methods_report() -> tuple[list[str], list[str]]:
    """(активные способы входа, замечания по недонастроенным).

    Вынесено в функцию, чтобы это можно было проверить тестом, а не только
    глазами в логе при старте.
    """
    active = ["email"]
    if YANDEX_LOGIN_ENABLED:
        active.append("yandex")
    if VK_LOGIN_ENABLED:
        active.append("vk")
    if MAILRU_LOGIN_ENABLED:
        active.append("mailru")

    notes = []
    configured = [
        name for name, value in (
            ("Яндекс", YANDEX_CLIENT_ID),
            ("VK", VK_CLIENT_ID),
            ("Mail.ru", MAILRU_CLIENT_ID),
        ) if value
    ]
    if configured and not OAUTH_LOGIN_ENABLED:
        notes.append(
            f"ключи настроены ({', '.join(configured)}), но OAUTH_LOGIN_ENABLED=0 "
            "— кнопки скрыты"
        )
    if OAUTH_LOGIN_ENABLED and YANDEX_CLIENT_ID and not YANDEX_CLIENT_SECRET:
        notes.append("у Яндекса нет YANDEX_CLIENT_SECRET — кнопка скрыта")
    if OAUTH_LOGIN_ENABLED and MAILRU_CLIENT_ID and not MAILRU_CLIENT_SECRET:
        notes.append("у Mail.ru нет MAILRU_CLIENT_SECRET — кнопка скрыта")
    return active, notes


_active_logins, _login_notes = _login_methods_report()
# Одна строка в логе на старте: молчаливо отвалившаяся конфигурация входа
# выглядит для пользователя как «сайт умеет только почту», и понять это можно
# было лишь чтением шаблонов.
log.info("Способы входа: %s", ", ".join(_active_logins))
for _note in _login_notes:
    log.warning("Вход: %s", _note)

ADMIN_EMAILS         = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
# Номер счётчика Метрики — только цифры: значение подставляется в JS и HTML
# всех страниц, так что произвольная строка (например, случайно вписанный
# секрет) и ломает скрипт, и утекает наружу.
METRIKA_ID           = os.getenv("METRIKA_ID", "")
if METRIKA_ID and not METRIKA_ID.isdigit():
    log.warning("METRIKA_ID не является числом — счётчик Метрики отключён")
    METRIKA_ID = ""

# ── Лимиты / тарифы ─────────────────────────────────────────────────────────
FREE_USES        = 3
FREE_RESUMES     = 5
PRO_PRICE        = "399.00"
PRO_DAYS         = 30
ANON_LIMIT_CONST = 2
# Второй предел — по IP и с суточным окном. Счётчик по cookie не удерживает
# ничего: клиенту достаточно не возвращать cookie, чтобы каждый раз получать
# новый anon_id с нулём попыток. Порог заметно выше «домашнего», чтобы
# несколько человек за одним NAT не блокировали друг друга; при злоупотреблении
# ужимается переменной окружения без выкатки кода.
ANON_IP_LIMIT_CONST = int(os.getenv("ANON_IP_LIMIT", "10"))
ANON_IP_WINDOW_HOURS = 24
# Окно счётчика по cookie равно сроку жизни самой cookie.
ANON_COOKIE_WINDOW_HOURS = 7 * 24
PAID_PACK        = 20
PACK_PRICE       = PRO_PRICE
SESSION_DAYS     = 30
MAGIC_MINUTES    = 15
AI_CONCURRENCY   = 2   # max одновременных вызовов Ollama
# Потолок токенов ответа модели (top-level max_tokens, см. call_ai). Держит
# счёт даже если промпт-инъекцией модель заставят генерировать что-то длинное
# не по формату — при внешнем провайдере (DeepSeek) это ещё и реальные деньги.
# 4096 — с запасом на длинное резюме (несколько мест работы, подробные
# bullet-points), но по-прежнему на порядки меньше, чем ничем не
# ограниченный ответ.
AI_MAX_TOKENS    = int(os.getenv("AI_MAX_TOKENS", "4096"))

# Pro формально «безлимит», но без потолка один скомпрометированный или
# написанный ботом аккаунт мог бы жать AI_CONCURRENCY-слот до предельной
# скорости лимитера (20/мин ≈ тысячи запросов в сутки) — это не про честное
# использование, а про счёт от внешнего провайдера. Порог посчитан от
# реальной цены DeepSeek (см. коммит): рядовому пользователю недостижим,
# аномальный расход обрежет заметно раньше тысяч запросов. Настраивается без
# выкатки кода — см. .env.example.
PRO_FAIR_USE_LIMIT = int(os.getenv("PRO_FAIR_USE_LIMIT", "300"))
PRO_FAIR_USE_DAYS  = int(os.getenv("PRO_FAIR_USE_DAYS", "30"))

# ── Обслуживание БД ─────────────────────────────────────────────────────────
# Протухшие сессии/токены и старые анонимные счётчики иначе растут бесконечно.
CLEANUP_INTERVAL_SEC = int(os.getenv("CLEANUP_INTERVAL_SEC", "3600"))
ANON_USAGE_TTL_DAYS  = int(os.getenv("ANON_USAGE_TTL_DAYS", "30"))
EVENTS_TTL_DAYS      = int(os.getenv("EVENTS_TTL_DAYS", "180"))

# ── Безопасность ────────────────────────────────────────────────────────────
# Режим Content-Security-Policy: enforce | report | off.
# `report` и `off` — аварийные вентили: если сторонний скрипт (Метрика,
# html2pdf) начнёт блокироваться, прод чинится переменной
# окружения, без выкатки кода.
RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}

CSP_MODE = os.getenv("CSP_MODE", "enforce").strip().lower()
if CSP_MODE not in {"enforce", "report", "off"}:
    log.warning("CSP_MODE=%r не распознан — используется enforce", CSP_MODE)
    CSP_MODE = "enforce"

_IS_PROD = APP_URL.startswith("https://")

# Секрет для подписи anon-cookie. ОБЯЗАТЕЛЬНО задайте в .env!
# В проде случайный ключ недопустим: при каждом рестарте он меняется, все
# анонимные cookie становятся невалидными и лимит бесплатных превью
# сбрасывается — то есть приложение раздаёт генерации бесплатно.
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY:
    if _IS_PROD:
        raise RuntimeError(
            "SECRET_KEY не задан. В проде (APP_URL=https://…) это обязательная "
            "переменная: без неё подпись anon-cookie меняется при каждом "
            "рестарте. Сгенерируйте: python -c \"import secrets;print(secrets.token_hex(32))\""
        )
    SECRET_KEY = "dev-insecure-" + os.urandom(16).hex()
    log.warning("SECRET_KEY не задан — сгенерирован временный ключ (только для разработки)")
