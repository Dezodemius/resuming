import asyncio
import base64
import ipaddress
import os
import json
import re
import secrets
import socket
import uuid
import hashlib
import hmac
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlencode, urljoin, urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException
import httpx
from mcp.server.fastmcp import Context, FastMCP

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    _RATE_LIMIT = True
except ImportError:
    _RATE_LIMIT = False

# Конфигурация вынесена в config.py. Импортируем имена в пространство main —
# существующий код и тесты обращаются к ним как к main.* (в т.ч. monkeypatch).
from config import (  # noqa: E402
    log,
    OLLAMA_URL, MODEL, AI_API_KEY, APP_URL,
    ROBOKASSA_LOGIN, ROBOKASSA_PASSWORD1, ROBOKASSA_PASSWORD2, ROBOKASSA_TEST_MODE,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM,
    YANDEX_CLIENT_ID, YANDEX_CLIENT_SECRET,
    VK_CLIENT_ID,
    MAILRU_CLIENT_ID, MAILRU_CLIENT_SECRET,
    YANDEX_LOGIN_ENABLED, VK_LOGIN_ENABLED, MAILRU_LOGIN_ENABLED,
    FREE_USES, FREE_RESUMES, PRO_PRICE, PRO_DAYS, ANON_LIMIT_CONST,
    ANON_IP_LIMIT_CONST, ANON_IP_WINDOW_HOURS, ANON_COOKIE_WINDOW_HOURS,
    SESSION_DAYS, MAGIC_MINUTES, AI_CONCURRENCY,
    SECRET_KEY,
    ADMIN_EMAILS, METRIKA_ID,
    CSP_MODE, CLEANUP_INTERVAL_SEC, ANON_USAGE_TTL_DAYS, EVENTS_TTL_DAYS,
    RATE_LIMIT_ENABLED,
)

# Семафор: не более AI_CONCURRENCY параллельных генераций.
# Создаём здесь — asyncio инициализирует при первом await.
_ai_sem: asyncio.Semaphore | None = None

def get_ai_sem() -> asyncio.Semaphore:
    global _ai_sem
    if _ai_sem is None:
        _ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
    return _ai_sem

tpl = Jinja2Templates(directory="templates")
tpl.env.globals["metrika_id"] = METRIKA_ID
tpl.env.globals["current_year"] = datetime.now(timezone.utc).year

# ── Database ── слой БД вынесен в db.py (get_db/init_db).
from db import get_db, init_db  # noqa: E402


# ── Обслуживание БД ──────────────────────────────────────────────────────
def cleanup_expired() -> dict[str, int]:
    """Удалить протухшие сессии, токены и старые служебные записи.

    Без этого таблицы растут бесконечно: sessions и magic_tokens пополняются
    на каждый вход, anon_usage — на каждого анонимного посетителя. Возвращает
    число удалённых строк по таблицам (для логов и тестов).
    """
    removed: dict[str, int] = {}
    with get_db() as db:
        removed["sessions"] = db.execute(
            "DELETE FROM sessions WHERE expires_at < datetime('now')"
        ).rowcount
        # Использованный токен держим сутки: если пользователь кликнет по
        # ссылке дважды, честнее ответить «ссылка уже использована», чем
        # «ссылка недействительна».
        removed["magic_tokens"] = db.execute(
            "DELETE FROM magic_tokens"
            " WHERE expires_at < datetime('now','-1 day')"
            "    OR (used=1 AND created < datetime('now','-1 day'))"
        ).rowcount
        # Cookie anon_id живёт 7 дней — записи старше TTL уже никому не
        # соответствуют и лимит не удерживают.
        removed["anon_usage"] = db.execute(
            f"DELETE FROM anon_usage WHERE created < datetime('now','-{ANON_USAGE_TTL_DAYS} days')"
        ).rowcount
        removed["usage_events"] = db.execute(
            f"DELETE FROM usage_events WHERE created < datetime('now','-{EVENTS_TTL_DAYS} days')"
        ).rowcount
        db.commit()
    return removed


async def _cleanup_loop():
    """Фоновая уборка раз в CLEANUP_INTERVAL_SEC. Падение цикла не должно
    ронять приложение — любую ошибку логируем и ждём следующего круга."""
    while True:
        try:
            removed = await asyncio.to_thread(cleanup_expired)
            if any(removed.values()):
                log.info("cleanup: удалено %s", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("cleanup: ошибка уборки БД")
        try:
            await asyncio.sleep(CLEANUP_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise

@asynccontextmanager
async def lifespan(app):
    init_db()
    cleaner = asyncio.create_task(_cleanup_loop())
    try:
        # Session manager MCP-сервера должен жить весь срок работы приложения
        # (mcp_server определён ниже; смонтирован в конце файла)
        async with mcp_server.session_manager.run():
            yield
    finally:
        cleaner.cancel()
        try:
            await cleaner
        except asyncio.CancelledError:
            pass

app = FastAPI(title="Резюмирую.рф", lifespan=lifespan)
os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── CORS ── разрешаем только собственный домен ───────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_URL, "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)

# ── Request logging ──────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    if request.url.path.startswith("/static"):
        return await call_next(request)
    t0 = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        log.exception("%s %s -> unhandled error (%.0f ms)",
                      request.method, request.url.path, (time.monotonic() - t0) * 1000)
        raise
    log.info("%s %s -> %s (%.0f ms)",
             request.method, request.url.path, response.status_code,
             (time.monotonic() - t0) * 1000)
    return response

# ── Rate limiting ────────────────────────────────────────────────────────
def _client_key(request: Request) -> str:
    """Ключ лимитера — адрес конечного пользователя.

    Сайт стоит за Cloudflare → nginx, поэтому peer-адрес приложения — всегда
    прокси: без разбора заголовков все посетители попали бы в одно ведро.

    Читаем X-Real-IP, а не CF-Connecting-IP, и разница принципиальна.
    X-Real-IP наш собственный nginx ставит сам из $remote_addr и **затирает**
    то, что прислал клиент. CF-Connecting-IP он пропускает как есть — им
    распоряжается Cloudflare, а не мы. При этом $remote_addr на стороне nginx
    уже разобран модулем real_ip: если запрос пришёл из сети Cloudflare, это
    настоящий адрес посетителя, а если кто-то достучался до origin мимо
    Cloudflare — его собственный адрес. Подделать ключ не выходит ни на одном
    из путей.

    Раньше первым читался CF-Connecting-IP, и обращение к origin напрямую с
    ротацией этого заголовка обходило и лимитер, и суточный предел анонимных
    генераций (тот считает ключ этой же функцией).

    X-Forwarded-For намеренно не используется: nginx только дописывает в него
    свой хвост, а начало списка присылает клиент. Если X-Real-IP нет вовсе,
    значит перед нами не наш прокси, и остаётся peer-адрес: ключ станет общим
    на всех, то есть лимит будет строже, а не слабее.
    """
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


if _RATE_LIMIT:
    from slowapi.middleware import SlowAPIMiddleware

    # default_limits — глобальный backstop: он применяется ко всем маршрутам,
    # а не только к помеченным @rate. Порог высокий: живой пользователь его не
    # достигает, а скрипт, долбящий генерацию или вебхук, упирается.
    limiter = Limiter(
        key_func=_client_key,
        default_limits=["240/minute"],
        enabled=RATE_LIMIT_ENABLED,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

def rate(limit: str):
    """Декоратор-заглушка если slowapi не установлен."""
    def decorator(fn):
        if _RATE_LIMIT:
            return limiter.limit(limit)(fn)
        return fn
    return decorator

# ── Security headers ─────────────────────────────────────────────────────
# Перечислены ровно те внешние источники, которые реально используются в
# шаблонах: шрифты Google, html2pdf с cdnjs, Яндекс.Метрика.
_CSP = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    # inline-обработчики (onclick) и <style> прямо в шаблонах требуют
    # 'unsafe-inline'; 'unsafe-eval' нужен сборщику PDF
    "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
    "https://cdnjs.cloudflare.com https://mc.yandex.ru",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "font-src 'self' data: https://fonts.gstatic.com",
    # Аватарки приходят с доменов VK/Яндекса/Mail.ru — перечислять
    # все хрупко, а картинка не исполняется: разрешаем любой https.
    "img-src 'self' data: blob: https:",
    "connect-src 'self' https://mc.yandex.ru https://mc.yandex.com",
    # blob:/'self' — html2pdf клонирует страницу в служебный iframe
    "frame-src 'self' blob: data: https://mc.yandex.ru",
    "worker-src 'self' blob:",
])

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
}
_CSP_HEADER = {
    "enforce": "Content-Security-Policy",
    "report":  "Content-Security-Policy-Report-Only",
}.get(CSP_MODE)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    if _CSP_HEADER:
        response.headers.setdefault(_CSP_HEADER, _CSP)
    if APP_URL.startswith("https://"):
        # Без includeSubDomains: поддомен ops.* обслуживается отдельно и не
        # должен зависеть от политики основного домена.
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response

# ── Обработчики ошибок ───────────────────────────────────────────────────
def _wants_html(request: Request) -> bool:
    """Навигация браузера (а не запрос из JS): только ей отдаём HTML-страницу.

    API обязано и дальше отвечать JSON с полем detail — на него завязан весь
    фронтенд, а Accept у fetch подделать может и сам браузер (например,
    prefetch-запросом). Поэтому решает не только Accept, но и префикс пути.
    """
    path = request.url.path
    if path.startswith(("/api/", "/auth/", "/mcp")):
        return False
    if request.method not in ("GET", "HEAD"):
        return False
    return "text/html" in request.headers.get("accept", "")


_ERROR_TEXTS = {
    404: ("Страница не найдена", "Похоже, ссылка устарела или в адресе опечатка."),
    403: ("Доступ закрыт", "У вас нет прав на эту страницу."),
    500: ("Что-то сломалось", "Мы уже знаем о проблеме. Попробуйте обновить страницу через минуту."),
}


def _error_response(request: Request, status: int, detail: str):
    if _wants_html(request):
        title, text = _ERROR_TEXTS.get(status, ("Ошибка", detail or "Что-то пошло не так."))
        return tpl.TemplateResponse(
            request, "error.html",
            {"status": status, "title": title, "text": text},
            status_code=status,
        )
    return JSONResponse({"detail": detail}, status_code=status)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    resp = _error_response(request, exc.status_code, exc.detail)
    for name, value in (getattr(exc, "headers", None) or {}).items():
        resp.headers[name] = value
    return resp


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Сам трейс уже записан в log_requests; наружу его не отдаём.
    return _error_response(
        request, 500, "Внутренняя ошибка сервера. Попробуйте позже."
    )

# ── Auth helpers ─────────────────────────────────────────────────────────
def _create_session(db, user_id: int) -> str:
    sid = str(uuid.uuid4())
    # Срок жизни считаем внутри SQLite (datetime('now', ...)), чтобы формат хранения
    # совпадал с форматом сравнения в get_current_user. ISO-строка с 'T' и смещением
    # '+00:00' при текстовом сравнении в SQLite даёт неверный результат на границе суток.
    db.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (?,?,datetime('now',?))",
        (sid, user_id, f"+{SESSION_DAYS} days"),
    )
    db.commit()
    return sid

def _set_session_cookie(response: Response, session_id: str):
    response.set_cookie(
        "session_id", session_id,
        httponly=True, samesite="lax",
        max_age=SESSION_DAYS * 86400,
        secure=APP_URL.startswith("https"),
    )

# ── Signed anon-cookie helpers ────────────────────────────────────────────
def _sign_anon(anon_id: str) -> str:
    """Подписываем anon_id через HMAC-SHA256 — нельзя подделать."""
    sig = hmac.new(SECRET_KEY.encode(), anon_id.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{anon_id}.{sig}"

def _verify_anon(value: str) -> Optional[str]:
    """Возвращает anon_id если подпись верна, иначе None."""
    parts = value.rsplit(".", 1)
    if len(parts) != 2:
        return None
    anon_id, sig = parts
    expected = hmac.new(SECRET_KEY.encode(), anon_id.encode(), hashlib.sha256).hexdigest()[:16]
    return anon_id if hmac.compare_digest(sig, expected) else None

async def get_current_user(request: Request) -> Optional[dict]:
    sid = request.cookies.get("session_id")
    if not sid:
        return None
    with get_db() as db:
        row = db.execute(
            "SELECT u.* FROM sessions s JOIN users u ON s.user_id = u.id "
            "WHERE s.id = ? AND s.expires_at > datetime('now')",
            (sid,)
        ).fetchone()
    return dict(row) if row else None

def _normalize_email(email: str) -> str:
    """Адрес как ключ аккаунта — всегда в нижнем регистре.

    Домен регистронезависим по RFC, локальная часть формально нет, но её
    регистр игнорируют все массовые почтовые провайдеры. Значение имеет другое:
    один и тот же человек приходит то из формы («Ivan@ya.ru», как набрал), то
    из OAuth («ivan@ya.ru», как отдал провайдер). Без нормализации это два
    разных аккаунта, и во втором нет ни его резюме, ни оплаченного Pro.
    """
    return email.strip().lower()


def _upsert_user_by_email(db, email: str) -> dict:
    email = _normalize_email(email)
    db.execute(
        "INSERT OR IGNORE INTO users (email, display_name) VALUES (?,?)",
        (email, email.split("@")[0])
    )
    db.commit()
    return dict(db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone())

async def _resolve_user(request: Request, body_email: Optional[str] = None) -> Optional[dict]:
    """Возвращает пользователя ТОЛЬКО из cookie-сессии.

    Раньше был fallback: если сессии нет — брать пользователя по `body_email`
    без какой-либо проверки. Это давало обход авторизации — любой мог читать и
    перезаписывать чужой профиль (`GET/POST /api/profile?email=...`) и тратить
    чужую квоту, зная email жертвы. Fallback убран; параметр сохранён для
    обратной совместимости сигнатуры вызовов, но больше не используется.
    """
    return await get_current_user(request)

def _require_admin(user):
    if not user or (user.get("email") or "").lower() not in ADMIN_EMAILS:
        raise HTTPException(404)

# ── Email magic link ──────────────────────────────────────────────────────
async def _send_magic_email(to_email: str, token: str) -> Optional[str]:
    """Отправляет magic-ссылку. Возвращает None при успехе, иначе строку с причиной ошибки."""
    if not SMTP_USER:
        log.info("[DEV] Magic link: %s/auth/email/verify?token=%s", APP_URL, token)
        return None
    log.info("magic-email: sending to %s via %s:%s", to_email, SMTP_HOST, SMTP_PORT)
    link = f"{APP_URL}/auth/email/verify?token={token}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = "Ваша ссылка для входа в Резюмирую.рф"
    msg["From"]    = SMTP_FROM
    msg["To"]      = to_email
    html = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto;padding:32px 24px">
      <h2 style="font-size:20px;font-weight:600;color:#0F1C3F;margin-bottom:8px">Резюмирую</h2>
      <p style="color:#64748B;margin-bottom:24px">Нажмите кнопку ниже чтобы войти. Ссылка действует {MAGIC_MINUTES} минут.</p>
      <a href="{link}" style="display:inline-block;background:#0F1C3F;color:#fff;padding:12px 28px;border-radius:8px;text-decoration:none;font-weight:600">Войти в Резюмирую.рф</a>
      <p style="color:#94A3B8;font-size:12px;margin-top:24px">Если вы не запрашивали этот email — просто проигнорируйте его.</p>
    </div>"""
    msg.attach(MIMEText(html, "html"))
    try:
        import aiosmtplib
        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST, port=SMTP_PORT,
            username=SMTP_USER, password=SMTP_PASS,
            use_tls=(SMTP_PORT == 465),
            start_tls=(SMTP_PORT == 587),
        )
        log.info("magic-email: sent to %s", to_email)
        return None
    except Exception as e:
        errno = getattr(e, "errno", None) or getattr(getattr(e, "os_error", None), "errno", None)
        reason = f"{type(e).__name__}: {e}"
        log.error("magic-email failed (SMTP %s:%s, errno=%s): %s", SMTP_HOST, SMTP_PORT, errno, reason)
        if errno in (51, 101, 113, 10051):  # ENETUNREACH / EHOSTUNREACH
            reason += " — нет сетевого маршрута до SMTP-сервера (порт блокирует хостер/VPN)"
        return reason

# Pydantic-схемы вынесены в schemas.py.
from schemas import (  # noqa: E402
    EmailReq, ProfileData, MatchReq, GenerateFromProfileReq,
    GenerateReq, PayReq, ImproveReq, AnonymousPreviewReq,
    PromoActivateReq, PromoCreateReq, TrackReq,
)

ANON_LIMIT = ANON_LIMIT_CONST
ANON_IP_LIMIT = ANON_IP_LIMIT_CONST


def _anon_ip_key(request: Request) -> str:
    """Ключ счётчика анонимных превью по адресу посетителя.

    В БД кладём HMAC, а не сам адрес: новых персональных данных от этого
    счётчика не появляется. Точность ключа — ровно та же, что у лимитера
    (см. _client_key): подделать его нельзя ни через Cloudflare, ни в обход.
    """
    digest = hmac.new(SECRET_KEY.encode(), _client_key(request).encode(), hashlib.sha256).hexdigest()
    return f"ip:{digest[:32]}"


def _set_anon_cookie(response: Response, anon_id: str) -> None:
    response.set_cookie(
        "anon_id", _sign_anon(anon_id),
        max_age=7 * 86400, samesite="lax",
        secure=APP_URL.startswith("https"), httponly=True,
    )


def _anon_uses(db, key: str, window_hours: int) -> int:
    """Потрачено попыток внутри окна. Строка старше окна считается нулевой —
    иначе один NAT выгорал бы навсегда."""
    row = db.execute(
        "SELECT uses FROM anon_usage WHERE anon_id=? AND created > datetime('now',?)",
        (key, f"-{window_hours} hours"),
    ).fetchone()
    return row["uses"] if row else 0


def _anon_bump(db, key: str, window_hours: int) -> None:
    """Инкремент счётчика с обнулением протухшего окна."""
    window = f"-{window_hours} hours"
    db.execute(
        "INSERT INTO anon_usage (anon_id, uses) VALUES (?,1)"
        " ON CONFLICT(anon_id) DO UPDATE SET"
        "   uses    = CASE WHEN created > datetime('now',?) THEN uses + 1 ELSE 1 END,"
        "   created = CASE WHEN created > datetime('now',?) THEN created ELSE datetime('now') END",
        (key, window, window),
    )


def _anon_refund(db, key: str) -> None:
    db.execute("UPDATE anon_usage SET uses = MAX(uses - 1, 0) WHERE anon_id=?", (key,))

# ── Anonymous preview (no auth, no save) ─────────────────────────────────
@app.post("/api/generate-preview")
@rate("10/minute")
async def generate_preview(req: AnonymousPreviewReq, request: Request, response: Response):
    """Анонимная генерация: профиль инлайн, результат не сохраняется.

    Пределов два. По подписанному cookie anon_id — честный счётчик для
    обычного посетителя (подпись не даёт присвоить чужой идентификатор, но
    сбросить свой можно, просто не вернув cookie). По адресу посетителя —
    предел, который очисткой cookie не обходится.
    """
    # Читаем и верифицируем подписанный cookie
    signed  = request.cookies.get("anon_id", "")
    anon_id = _verify_anon(signed) if signed else None
    if not anon_id:
        anon_id = str(uuid.uuid4())
    ip_key = _anon_ip_key(request)

    # Пишем подписанный cookie обратно (httpOnly)
    _set_anon_cookie(response, anon_id)

    # Текст вакансии: вручную или по ссылке (до списания лимита)
    job_text = req.job_text.strip()
    if req.kind == "match":
        if len(job_text) < 30 and req.job_url.strip():
            job_text = await _fetch_job_text(req.job_url.strip())
        if len(job_text) < 30:
            raise HTTPException(400, "Вставьте текст вакансии или ссылку на неё")

    # Считаем по двум ключам сразу. Счёт по cookie — «честный» для обычного
    # посетителя, но он не удерживает ничего: клиенту достаточно не возвращать
    # cookie, чтобы каждый раз приходить с новым anon_id и нулём попыток.
    # Поэтому решает ещё и счётчик по адресу с суточным окном.
    with get_db() as db:
        uses = _anon_uses(db, anon_id, ANON_COOKIE_WINDOW_HOURS)
        ip_uses = _anon_uses(db, ip_key, ANON_IP_WINDOW_HOURS)
        if uses >= ANON_LIMIT or ip_uses >= ANON_IP_LIMIT:
            denied = JSONResponse(status_code=429,
                                  content={"error": "anon_limit", "limit": ANON_LIMIT})
            # Cookie ставим и на отказе: иначе отказ не закрепляется за
            # посетителем и следующий заход снова выглядит первым.
            _set_anon_cookie(denied, anon_id)
            return denied
        _anon_bump(db, anon_id, ANON_COOKIE_WINDOW_HOURS)
        _anon_bump(db, ip_key, ANON_IP_WINDOW_HOURS)
        db.commit()

    try:
        prompt = (
            _match_prompt(req.profile, job_text, req.hint)
            if req.kind == "match"
            else _general_prompt(req.profile, req.target_role, req.hint)
        )
        raw    = await call_ai(prompt)
        resume = _parse_ai(raw)
    except HTTPException:
        with get_db() as db:
            _anon_refund(db, anon_id)
            _anon_refund(db, ip_key)
            log_event(db, "generate_fail", anon_id=anon_id, kind="preview")
            db.commit()
        raise

    with get_db() as db:
        log_event(db, "anon_preview", anon_id=anon_id)
        db.commit()

    return {"resume": resume, "anon_uses_left": ANON_LIMIT - uses - 1}

# ── Health checks ─────────────────────────────────────────────────────────
_NO_STORE = {"Cache-Control": "no-store"}


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def healthz():
    return JSONResponse({"status": "ok"}, headers=_NO_STORE)


@app.api_route("/readyz", methods=["GET", "HEAD"])
async def readyz():
    db_ok = False
    ollama_ok = False
    try:
        with get_db() as db:
            db.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass
    try:
        headers = {"Authorization": f"Bearer {AI_API_KEY}"} if AI_API_KEY else {}
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{OLLAMA_URL}/v1/models", headers=headers)
        if r.status_code == 200:
            models = [m.get("id", "") for m in r.json().get("data", [])]
            ollama_ok = any(MODEL in m for m in models)
    except Exception:
        pass
    if db_ok and ollama_ok:
        return JSONResponse({"status": "ok"}, headers=_NO_STORE)
    return JSONResponse(
        {"status": "degraded", "checks": {"db": db_ok, "ollama": ollama_ok}},
        status_code=503,
        headers=_NO_STORE,
    )

# ── Static pages ──────────────────────────────────────────────────────────
def _auth_ctx(user) -> dict:
    return {
        "yandex_enabled": YANDEX_LOGIN_ENABLED,
        "vk_enabled": VK_LOGIN_ENABLED,
        "mailru_enabled": MAILRU_LOGIN_ENABLED,
        "user": user,
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Публичный лендинг. Залогиненного пользователя маркетинг не интересует —
    отправляем сразу в генератор."""
    user = await get_current_user(request)
    if user:
        return RedirectResponse(url="/new", status_code=302, headers=_NO_STORE)
    resp = tpl.TemplateResponse(request, "landing.html", {
        **_auth_ctx(user),
        "app_url": APP_URL,
        "pro_price": PRO_PRICE,
        "pro_days": PRO_DAYS,
        "free_uses": FREE_USES,
        "free_resumes": FREE_RESUMES,
        "anon_limit": ANON_LIMIT_CONST,
    })
    # Ответ зависит от cookie сессии: без no-store прокси может отдать лендинг
    # залогиненному пользователю (и наоборот).
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/new", response_class=HTMLResponse)
async def generator_page(request: Request):
    """Генератор резюме. Доступен и анонимам — это шаг воронки «попробовать»."""
    user = await get_current_user(request)
    return tpl.TemplateResponse(request, "index.html", _auth_ctx(user))


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return PlainTextResponse(
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /settings\n"
        "Disallow: /resumes\n"
        "Disallow: /api/\n"
        "Disallow: /auth/\n"
        "Disallow: /pay/\n"
        f"Sitemap: {APP_URL}/sitemap.xml\n"
    )


# Шаги воронки лендинга. Ручка публичная и пишет в БД, поэтому список закрытый:
# произвольные строки от клиента в usage_events не попадают.
_FUNNEL_EVENTS = {
    "landing_view", "landing_pricing_view",
    "cta_header", "cta_hero", "cta_demo", "cta_how",
    "cta_plan_free", "cta_plan_pro", "cta_final", "cta_sticky",
    "pricing_view", "pricing_buy_click",
}


@app.post("/api/track")
@rate("60/minute")
async def track_funnel(req: TrackReq, request: Request):
    """Серверный счётчик шагов воронки — чтобы конверсия считалась и без Метрики
    (её режут блокировщики). Событие вне белого списка молча игнорируем."""
    event = req.event.strip()
    if event not in _FUNNEL_EVENTS:
        return {"ok": False}
    user = await get_current_user(request)
    anon_id = None
    if not user:
        signed = request.cookies.get("anon_id", "")
        anon_id = _verify_anon(signed) if signed else None
    with get_db() as db:
        log_event(db, event, user_id=(user["id"] if user else None), anon_id=anon_id)
        db.commit()
    return {"ok": True}


@app.get("/sitemap.xml")
async def sitemap():
    pages = ["/", "/new", "/pricing", "/offer", "/privacy", "/contacts"]
    urls = "".join(f"<url><loc>{APP_URL}{p}</loc></url>" for p in pages)
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?>'
                f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )

@app.get("/resumes/{resume_id}", response_class=HTMLResponse)
async def resume_edit_page(resume_id: int, request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/new?auth_required=1", status_code=303)
    with get_db() as db:
        exists = db.execute(
            "SELECT id FROM resumes WHERE id=? AND user_id=?", (resume_id, user["id"])
        ).fetchone()
    if not exists:
        return RedirectResponse(url="/resumes", status_code=303)
    return tpl.TemplateResponse(request, "resume_edit.html", {
        "resume_id":  resume_id,
        "user": user,
    })

# ── AI section improvement ────────────────────────────────────────────────
@app.post("/api/improve-text")
@rate("20/minute")
async def improve_text(req: ImproveReq, request: Request):
    """Per-section AI improvement, used from the resume editor."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    if not req.text.strip():
        raise HTTPException(400, "Текст не может быть пустым")
    # Длину ограничиваем и при наличии квоты: одним запросом иначе можно занять
    # слот семафора AI_CONCURRENCY надолго.
    if len(req.text) > 10_000:
        raise HTTPException(400, "Слишком длинный текст — сократите фрагмент")

    ctx = f"\nКонтекст: {req.context}" if req.context else ""
    prompts = {
        "summary":
            f"Улучши профессиональный профиль резюме. Сделай его убедительным и конкретным.{ctx}\n"
            f"2–3 предложения, профессиональный тон. Верни ТОЛЬКО текст, без кавычек и объяснений:\n\n{req.text}",
        "bullets":
            f"Улучши achievement-bullets для резюме. Каждый пункт: глагол действия + конкретный результат/цифры.{ctx}\n"
            f"Верни ТОЛЬКО улучшенные bullet-points (по одному на строку, с •):\n\n{req.text}",
        "skills":
            f"Структурируй и дополни раздел навыков. Формат строго: «Категория: навык1, навык2» (одна категория — одна строка).{ctx}\n"
            f"Верни ТОЛЬКО отформатированный список:\n\n{req.text}",
    }
    prompt = prompts.get(req.kind, prompts["summary"])

    # Это такой же вызов модели, как и генерация резюме, и списывается так же.
    # Раньше ручка не трогала счётчик вообще: пользователь с нулевым балансом
    # получал безлимитный доступ к модели, а пара таких запросов занимала оба
    # слота AI_CONCURRENCY и выключала генерацию для всех остальных.
    with get_db() as db:
        ok, col, uses_left = _deduct(db, user["id"])
        if not ok:
            return JSONResponse(status_code=402, content={"error": "no_uses"})
    try:
        result = await call_ai(prompt)
    except Exception as e:
        with get_db() as db:
            _refund(db, user["id"], col)
            log_event(db, "generate_fail", user_id=user["id"], kind="improve")
            db.commit()
        if isinstance(e, HTTPException):
            raise
        log.exception("improve-text: неожиданная ошибка (user=%s)", user["id"])
        raise HTTPException(500, "Ошибка генерации. Попробуйте позже.")
    with get_db() as db:
        log_event(db, "generate", user_id=user["id"], kind="improve", col=col)
        db.commit()
    return {"improved": result.strip(), "uses_left": uses_left}

# ── Page routes ────────────────────────────────────────────────────────────
@app.get("/resumes", response_class=HTMLResponse)
async def resumes_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/new?auth_required=1", status_code=303)
    return tpl.TemplateResponse(request, "resumes.html", {
        "user": user,
    })

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/new?auth_required=1", status_code=303)
    return tpl.TemplateResponse(request, "settings.html", {
        "user": user,
    })

# ── Public / legal pages (no auth required) ───────────────────────────────
@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    return tpl.TemplateResponse(request, "pricing.html", {
        "pro_price": PRO_PRICE,
        "pro_days": PRO_DAYS,
        "free_uses": FREE_USES,
        "free_resumes": FREE_RESUMES,
        "anon_limit": ANON_LIMIT_CONST,
    })

@app.get("/offer", response_class=HTMLResponse)
async def offer_page(request: Request):
    return tpl.TemplateResponse(request, "offer.html")

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return tpl.TemplateResponse(request, "privacy.html")

@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    return tpl.TemplateResponse(request, "contacts.html")

@app.get("/api/billing")
async def billing_info(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401)
    pro = _is_pro(user)
    with get_db() as db:
        pays = db.execute(
            "SELECT pay_id, status, created FROM payments"
            " WHERE user_id=? ORDER BY created DESC LIMIT 10",
            (user["id"],)
        ).fetchall()
        resume_cnt = db.execute(
            "SELECT COUNT(*) FROM resumes WHERE user_id=?", (user["id"],)
        ).fetchone()[0]
    return {
        "is_pro":         pro,
        "pro_expires_at": user.get("pro_expires_at"),
        "free_left":      user["free_left"],
        "paid_left":      user["paid_left"],
        "resume_count":   resume_cnt,
        "resume_limit":   None if pro else FREE_RESUMES,
        "pro_price":      PRO_PRICE,
        "payments":       [dict(p) for p in pays],
    }

# ── Auth routes ────────────────────────────────────────────────────────────
@app.post("/auth/email/request")
@rate("5/minute")
async def auth_email_request(req: EmailReq, request: Request):
    token = str(uuid.uuid4())
    with get_db() as db:
        # Срок (15 мин) считаем в SQLite, чтобы формат совпал с datetime('now') при
        # проверке. Раньше хранилась наивная ISO-строка локального времени с 'T':
        # из-за текстового сравнения '...T...' > '... ...' токен фактически жил до
        # конца суток UTC, а не 15 минут — обход короткого окна одноразового входа.
        db.execute(
            "INSERT OR REPLACE INTO magic_tokens (token, email, expires_at)"
            " VALUES (?,?,datetime('now',?))",
            (token, _normalize_email(req.email), f"+{MAGIC_MINUTES} minutes")
        )
        db.commit()
    err = await _send_magic_email(_normalize_email(req.email), token)
    if err:
        raise HTTPException(500, f"Не удалось отправить письмо. {err}")
    return {"ok": True}

@app.get("/auth/email/verify")
async def auth_email_verify(token: str, response: Response):
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM magic_tokens WHERE token=? AND used=0 AND expires_at > datetime('now')",
            (token,)
        ).fetchone()
        if not row:
            return HTMLResponse("""
            <html><head><meta charset="utf-8"><title>Ссылка истекла</title></head>
            <body style="font-family:sans-serif;text-align:center;padding:60px">
              <h2>Ссылка истекла или уже использована</h2>
              <p><a href="/">Вернуться на главную</a></p>
            </body></html>""")
        email = row["email"]
        db.execute("UPDATE magic_tokens SET used=1 WHERE token=?", (token,))
        u = _upsert_user_by_email(db, email)
        sid = _create_session(db, u["id"])
        log_event(db, "login", user_id=u["id"], method="email")
        db.commit()
    r = RedirectResponse(url="/new?login=success", status_code=303)
    _set_session_cookie(r, sid)
    return r

# ── Yandex OAuth ──────────────────────────────────────────────────────────
@app.get("/auth/yandex")
async def auth_yandex_start():
    if not YANDEX_LOGIN_ENABLED:
        raise HTTPException(503, "Вход через Яндекс не настроен")
    state = str(uuid.uuid4())
    params = urlencode({
        "response_type": "code",
        "client_id":     YANDEX_CLIENT_ID,
        "redirect_uri":  f"{APP_URL}/auth/yandex/callback",
        "state":         state,
    })
    r = RedirectResponse(f"https://oauth.yandex.ru/authorize?{params}", status_code=302)
    r.set_cookie("ya_state", state, max_age=600, httponly=True, samesite="lax",
                 secure=APP_URL.startswith("https"))
    log.info("auth/yandex: redirect to Yandex OAuth")
    return r

@app.get("/auth/yandex/callback")
async def auth_yandex_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        log.warning("auth/yandex callback error: %s", error or "no code")
        return RedirectResponse(url="/new?auth_error=yandex", status_code=303)
    if not state or state != request.cookies.get("ya_state"):
        log.warning("auth/yandex callback: state mismatch")
        return RedirectResponse(url="/new?auth_error=yandex", status_code=303)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            tr = await http.post("https://oauth.yandex.ru/token", data={
                "grant_type":    "authorization_code",
                "code":          code,
                "client_id":     YANDEX_CLIENT_ID,
                "client_secret": YANDEX_CLIENT_SECRET,
            })
            access = tr.json().get("access_token")
            if not access:
                log.error("auth/yandex: token exchange failed: %s", tr.text[:300])
                return RedirectResponse(url="/new?auth_error=yandex", status_code=303)
            ir = await http.get("https://login.yandex.ru/info",
                                params={"format": "json"},
                                headers={"Authorization": f"OAuth {access}"})
            info = ir.json()
    except Exception:
        log.exception("auth/yandex: OAuth request failed")
        return RedirectResponse(url="/new?auth_error=yandex", status_code=303)

    email = info.get("default_email") or ""
    if not email:
        log.error("auth/yandex: no default_email in userinfo")
        return RedirectResponse(url="/new?auth_error=yandex", status_code=303)

    with get_db() as db:
        u = _upsert_user_by_email(db, email)
        name = info.get("real_name") or info.get("display_name")
        if name and u.get("display_name") in (None, "", email.split("@")[0]):
            db.execute("UPDATE users SET display_name=? WHERE id=?", (name, u["id"]))
            db.commit()
        sid = _create_session(db, u["id"])
        log_event(db, "login", user_id=u["id"], method="yandex")
        db.commit()
    log.info("auth/yandex: login ok user_id=%s", u["id"])
    r = RedirectResponse(url="/new?login=success", status_code=303)
    r.delete_cookie("ya_state")
    _set_session_cookie(r, sid)
    return r

# ── VK ID OAuth (с PKCE) ──────────────────────────────────────────────────────
@app.get("/auth/vk")
async def auth_vk_start():
    if not VK_LOGIN_ENABLED:
        raise HTTPException(503, "Вход через VK не настроен")
    state = str(uuid.uuid4())
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")
    params = urlencode({
        "response_type":         "code",
        "client_id":             VK_CLIENT_ID,
        "redirect_uri":          f"{APP_URL}/auth/vk/callback",
        "state":                 state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
        "scope":                 "email",
    })
    r = RedirectResponse(f"https://id.vk.com/authorize?{params}", status_code=302)
    r.set_cookie("vk_state", state, max_age=600, httponly=True, samesite="lax",
                 secure=APP_URL.startswith("https"))
    r.set_cookie("vk_verifier", code_verifier, max_age=600, httponly=True, samesite="lax",
                 secure=APP_URL.startswith("https"))
    log.info("auth/vk: redirect to VK OAuth")
    return r

@app.get("/auth/vk/callback")
async def auth_vk_callback(request: Request, code: str = "", state: str = "", device_id: str = "", error: str = ""):
    if error or not code:
        log.warning("auth/vk callback error: %s", error or "no code")
        return RedirectResponse(url="/new?auth_error=vk", status_code=303)
    if not state or state != request.cookies.get("vk_state"):
        log.warning("auth/vk callback: state mismatch")
        return RedirectResponse(url="/new?auth_error=vk", status_code=303)
    try:
        code_verifier = request.cookies.get("vk_verifier")
        if not code_verifier:
            log.error("auth/vk: missing code_verifier cookie")
            return RedirectResponse(url="/new?auth_error=vk", status_code=303)
        async with httpx.AsyncClient(timeout=15) as http:
            # PKCE: code_verifier заменяет client_secret — «Защищённый ключ»
            # из кабинета VK ID в этом флоу не участвует.
            tr = await http.post("https://id.vk.com/oauth2/auth", data={
                "grant_type":     "authorization_code",
                "code":           code,
                "code_verifier":  code_verifier,
                "client_id":      VK_CLIENT_ID,
                "device_id":      device_id,
                "redirect_uri":   f"{APP_URL}/auth/vk/callback",
                "state":          state,
            })
            access = tr.json().get("access_token")
            if not access:
                log.error("auth/vk: token exchange failed: %s", tr.text[:300])
                return RedirectResponse(url="/new?auth_error=vk", status_code=303)
            ir = await http.post("https://id.vk.com/oauth2/user_info", data={
                "access_token": access,
                "client_id":    VK_CLIENT_ID,
            })
            info = ir.json()
    except Exception:
        log.exception("auth/vk: OAuth request failed")
        return RedirectResponse(url="/new?auth_error=vk", status_code=303)

    user_data = info.get("user", {})
    email = user_data.get("email") or ""
    if not email:
        log.error("auth/vk: no email in userinfo")
        return RedirectResponse(url="/new?auth_error=vk", status_code=303)

    with get_db() as db:
        u = _upsert_user_by_email(db, email)
        first_name = user_data.get("first_name") or ""
        last_name = user_data.get("last_name") or ""
        name = (first_name + " " + last_name).strip() if first_name or last_name else ""
        if name and u.get("display_name") in (None, "", email.split("@")[0]):
            db.execute("UPDATE users SET display_name=? WHERE id=?", (name, u["id"]))
            db.commit()
        sid = _create_session(db, u["id"])
        log_event(db, "login", user_id=u["id"], method="vk")
        db.commit()
    log.info("auth/vk: login ok user_id=%s", u["id"])
    r = RedirectResponse(url="/new?login=success", status_code=303)
    r.delete_cookie("vk_state")
    r.delete_cookie("vk_verifier")
    _set_session_cookie(r, sid)
    return r

# ── Mail.ru OAuth ─────────────────────────────────────────────────────────────
@app.get("/auth/mailru")
async def auth_mailru_start():
    if not MAILRU_LOGIN_ENABLED:
        raise HTTPException(503, "Вход через Mail.ru не настроен")
    state = str(uuid.uuid4())
    params = urlencode({
        "response_type": "code",
        "client_id":     MAILRU_CLIENT_ID,
        "redirect_uri":  f"{APP_URL}/auth/mailru/callback",
        "state":         state,
        "scope":         "userinfo",
    })
    r = RedirectResponse(f"https://oauth.mail.ru/login?{params}", status_code=302)
    r.set_cookie("mr_state", state, max_age=600, httponly=True, samesite="lax",
                 secure=APP_URL.startswith("https"))
    log.info("auth/mailru: redirect to Mail.ru OAuth")
    return r

@app.get("/auth/mailru/callback")
async def auth_mailru_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code:
        log.warning("auth/mailru callback error: %s", error or "no code")
        return RedirectResponse(url="/new?auth_error=mailru", status_code=303)
    if not state or state != request.cookies.get("mr_state"):
        log.warning("auth/mailru callback: state mismatch")
        return RedirectResponse(url="/new?auth_error=mailru", status_code=303)
    try:
        async with httpx.AsyncClient(timeout=15) as http:
            tr = await http.post("https://oauth.mail.ru/token", data={
                "client_id":     MAILRU_CLIENT_ID,
                "client_secret": MAILRU_CLIENT_SECRET,
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  f"{APP_URL}/auth/mailru/callback",
            })
            access = tr.json().get("access_token")
            if not access:
                log.error("auth/mailru: token exchange failed: %s", tr.text[:300])
                return RedirectResponse(url="/new?auth_error=mailru", status_code=303)
            ir = await http.get("https://oauth.mail.ru/userinfo",
                                params={"access_token": access})
            info = ir.json()
    except Exception:
        log.exception("auth/mailru: OAuth request failed")
        return RedirectResponse(url="/new?auth_error=mailru", status_code=303)

    email = info.get("email") or ""
    if not email:
        log.error("auth/mailru: no email in userinfo")
        return RedirectResponse(url="/new?auth_error=mailru", status_code=303)

    with get_db() as db:
        u = _upsert_user_by_email(db, email)
        name = info.get("name") or info.get("nickname") or ""
        if name and u.get("display_name") in (None, "", email.split("@")[0]):
            db.execute("UPDATE users SET display_name=? WHERE id=?", (name, u["id"]))
            db.commit()
        sid = _create_session(db, u["id"])
        log_event(db, "login", user_id=u["id"], method="mailru")
        db.commit()
    log.info("auth/mailru: login ok user_id=%s", u["id"])
    r = RedirectResponse(url="/new?login=success", status_code=303)
    r.delete_cookie("mr_state")
    _set_session_cookie(r, sid)
    return r

@app.post("/auth/logout")
async def auth_logout(request: Request, response: Response):
    sid = request.cookies.get("session_id")
    if sid:
        with get_db() as db:
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
            db.commit()
    response.delete_cookie("session_id")
    return {"ok": True}

@app.get("/api/me")
async def me(request: Request):
    user = await get_current_user(request)
    if not user:
        return {"authenticated": False}
    pro_active = _is_pro(user)
    with get_db() as db:
        resume_cnt = db.execute(
            "SELECT COUNT(*) FROM resumes WHERE user_id=?", (user["id"],)
        ).fetchone()[0]
    return {
        "authenticated":  True,
        "id":             user["id"],
        "email":          user.get("email"),
        "name":           user.get("display_name"),
        "is_pro":         pro_active,
        "pro_expires_at": user.get("pro_expires_at"),
        "free_left":      user["free_left"],
        "paid_left":      user["paid_left"],
        "total":          999 if pro_active else user["free_left"] + user["paid_left"],
        "resume_count":   resume_cnt,
        "resume_limit":   None if pro_active else FREE_RESUMES,
    }

# ── Usage / plan helpers ───────────────────────────────────────────────────
def _is_pro(user_row) -> bool:
    """True если у пользователя активная Pro-подписка."""
    if not user_row["is_pro"] or not user_row["pro_expires_at"]:
        return False
    try:
        exp = datetime.fromisoformat(user_row["pro_expires_at"])
        # SQLite хранит без timezone — добавляем UTC если нет
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        return exp > datetime.now(timezone.utc)
    except Exception:
        return False

def _uses_left(user_row) -> int:
    """Сколько генераций осталось. 999 = Pro (безлимит)."""
    if _is_pro(user_row):
        return 999
    return user_row["free_left"] + user_row["paid_left"]

def _deduct(db, user_id: int) -> tuple[bool, str, int]:
    """
    Списывает одну генерацию.
    Returns (ok, col_used, uses_left).
    Pro-пользователи не теряют счётчик — returns ('pro', 999).
    """
    row = db.execute(
        "SELECT free_left, paid_left, is_pro, pro_expires_at FROM users WHERE id=?",
        (user_id,)
    ).fetchone()

    if _is_pro(row):
        return True, "pro", 999          # безлимит, ничего не списываем

    total = row["free_left"] + row["paid_left"]
    if total <= 0:
        return False, "", 0

    col = "free_left" if row["free_left"] > 0 else "paid_left"
    db.execute(f"UPDATE users SET {col}={col}-1 WHERE id=?", (user_id,))
    db.commit()
    upd = db.execute("SELECT free_left, paid_left FROM users WHERE id=?", (user_id,)).fetchone()
    left = upd["free_left"] + upd["paid_left"]
    log.info("deduct: user=%s col=%s left=%s", user_id, col, left)
    return True, col, left

def _refund(db, user_id: int, col: str):
    if col == "pro":
        return  # Pro-пользователям не нужен возврат
    db.execute(f"UPDATE users SET {col}={col}+1 WHERE id=?", (user_id,))
    db.commit()
    log.info("refund: user=%s col=%s (генерация не удалась)", user_id, col)

def log_event(db, event: str, user_id=None, anon_id=None, **meta):
    """Логирует событие в таблицу usage_events (логины, генерации, платежи и т.д.)."""
    db.execute("INSERT INTO usage_events (user_id, anon_id, event, meta) VALUES (?,?,?,?)",
               (user_id, anon_id, event, json.dumps(meta, ensure_ascii=False) if meta else None))

# ── AI call ────────────────────────────────────────────────────────────────
async def call_ai(prompt: str) -> str:
    """
    Вызов Ollama с:
    - Семафором AI_CONCURRENCY (не более N одновременных запросов)
    - Таймаутом 120 сек (connect 5 сек)
    - Безопасными сообщениями об ошибках (без деталей внутренностей)
    """
    async with get_ai_sem():
        t0 = time.monotonic()
        log.info("AI call start: model=%s prompt_len=%d", MODEL, len(prompt))
        try:
            headers = {"Authorization": f"Bearer {AI_API_KEY}"} if AI_API_KEY else {}
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=5.0)
            ) as http:
                r = await http.post(
                    f"{OLLAMA_URL}/v1/chat/completions",
                    headers=headers,
                    json={
                        "model": MODEL,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.25, "num_predict": 2048},
                    },
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
                log.info("AI call ok: %.1f s, response_len=%d", time.monotonic() - t0, len(content))
                return content
        except httpx.ConnectError as e:
            log.error("AI connect error (%s): %s", OLLAMA_URL, e)
            raise HTTPException(503, "Сервис генерации недоступен. Проверьте Ollama.")
        except httpx.TimeoutException:
            log.error("AI timeout after %.1f s (model=%s)", time.monotonic() - t0, MODEL)
            raise HTTPException(504, "Генерация заняла слишком долго. Попробуйте ещё раз.")
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            log.error("AI HTTP %s after %.1f s: %s", e.response.status_code, time.monotonic() - t0, body)
            if e.response.status_code == 500 and ("killed" in body or "terminated" in body):
                # llama-server убит OOM-killer'ом: модели не хватает RAM на сервере
                raise HTTPException(503, "Модель не смогла загрузиться: серверу не хватает памяти. "
                                         "Сообщите администратору или попробуйте позже.")
            raise HTTPException(502, f"Ошибка модели: {e.response.status_code}")
        except Exception:
            log.exception("AI call unexpected error after %.1f s", time.monotonic() - t0)
            raise HTTPException(500, "Ошибка генерации. Попробуйте позже.")

def _parse_ai(raw: str) -> dict:
    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Модель иногда добавляет пояснения вокруг JSON или обрывает ответ
        # (например при упоре в num_predict). Пытаемся вытащить объект {...}.
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end + 1])
            except json.JSONDecodeError:
                pass
        log.warning("AI returned non-JSON (len=%d): %s", len(raw), raw[:500])
        raise HTTPException(502, "Модель вернула некорректный ответ. Попробуйте ещё раз.")

def _save_resume(db, user_id: int, resume: dict, kind: str,
                 company: str = "", job_url: str = "", job_snippet: str = "") -> int:
    """Сохраняет резюме. Для бесплатных пользователей проверяет лимит FREE_RESUMES."""
    user = db.execute(
        "SELECT is_pro, pro_expires_at FROM users WHERE id=?", (user_id,)
    ).fetchone()

    if not _is_pro(user):
        cnt = db.execute(
            "SELECT COUNT(*) FROM resumes WHERE user_id=?", (user_id,)
        ).fetchone()[0]
        if cnt >= FREE_RESUMES:
            raise ValueError("resume_limit")

    now = datetime.now().isoformat()
    c = db.execute(
        "INSERT INTO resumes (user_id, company_name, job_url, job_snippet, resume_data, kind, updated)"
        " VALUES (?,?,?,?,?,?,?)",
        (user_id, company or resume.get("target_role", "Резюме"), job_url, job_snippet[:300],
         json.dumps(resume, ensure_ascii=False), kind, now)
    )
    db.commit()
    return c.lastrowid

# ── Profile ────────────────────────────────────────────────────────────────
@app.post("/api/profile")
async def save_profile(req: ProfileData, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    data = req.dict()
    with get_db() as db:
        db.execute(
            "INSERT INTO profiles (user_id, data) VALUES (?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET data=excluded.data, updated=datetime('now')",
            (user["id"], json.dumps(data, ensure_ascii=False))
        )
        db.commit()
    return {"ok": True}

@app.get("/api/profile")
async def load_profile(request: Request, email: Optional[str] = None):
    user = await _resolve_user(request, email)
    if not user:
        return {"profile": None}
    with get_db() as db:
        row = db.execute("SELECT data FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    return {"profile": json.loads(row["data"]) if row else None}

# ── Resume CRUD ────────────────────────────────────────────────────────────
@app.get("/api/resumes")
async def list_resumes(request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    with get_db() as db:
        # Дополнительные поля вытаскиваем прямо из resume_data JSON (json_extract),
        # чтобы библиотека рендерилась одним запросом без N+1. Эти поля
        # опциональны — где их нет, вернётся NULL и UI деградирует аккуратно.
        rows = db.execute(
            "SELECT id, company_name, kind, status, created, updated,"
            " json_extract(resume_data,'$.target_role') AS title,"
            " json_extract(resume_data,'$.salary')      AS salary,"
            " json_extract(resume_data,'$.location')    AS location,"
            " json_extract(resume_data,'$.ats_match')   AS match"
            " FROM resumes WHERE user_id=? ORDER BY updated DESC",
            (user["id"],)
        ).fetchall()
    return {"resumes": [dict(r) for r in rows]}

@app.get("/api/resumes/{resume_id}")
async def get_resume(resume_id: int, request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM resumes WHERE id=? AND user_id=?", (resume_id, user["id"])
        ).fetchone()
    if not row:
        raise HTTPException(404, "Резюме не найдено")
    r = dict(row)
    r["resume_data"] = json.loads(r["resume_data"])
    return r

@app.put("/api/resumes/{resume_id}")
async def update_resume(resume_id: int, request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    body = await request.json()
    with get_db() as db:
        existing = db.execute(
            "SELECT id FROM resumes WHERE id=? AND user_id=?", (resume_id, user["id"])
        ).fetchone()
        if not existing:
            raise HTTPException(404)
        fields, vals = [], []
        if "resume_data" in body:
            fields.append("resume_data=?")
            vals.append(json.dumps(body["resume_data"], ensure_ascii=False))
        if "company_name" in body:
            fields.append("company_name=?")
            vals.append(body["company_name"])
        if "status" in body:
            fields.append("status=?")
            vals.append(body["status"])
        if fields:
            fields.append("updated=datetime('now')")
            db.execute(
                f"UPDATE resumes SET {', '.join(fields)} WHERE id=? AND user_id=?",
                vals + [resume_id, user["id"]]
            )
            db.commit()
    return {"ok": True}

@app.delete("/api/resumes/{resume_id}")
async def delete_resume(resume_id: int, request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    with get_db() as db:
        db.execute("DELETE FROM resumes WHERE id=? AND user_id=?", (resume_id, user["id"]))
        db.commit()
    return {"ok": True}

@app.post("/api/resumes/save")
async def save_resume_json(request: Request):
    """
    Сохраняет готовый JSON резюме в БД.
    Используется при переходе из анонимного режима в авторизованный:
    пользователь сгенерировал анонимно → вошёл → мы сохраняем результат.
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Требуется авторизация")
    body = await request.json()
    resume_data = body.get("resume_data")
    if not resume_data:
        raise HTTPException(400, "Нет данных резюме")
    with get_db() as db:
        rid = _save_resume(
            db, user["id"], resume_data,
            body.get("kind", "general"),
            body.get("company_name", ""),
            body.get("job_url", ""),
            body.get("job_snippet", ""),
        )
    return {"resume_id": rid}

# ── Generate / Match ───────────────────────────────────────────────────────
@app.post("/api/match")
@rate("20/minute")
async def match_to_job(req: MatchReq, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    # Текст вакансии: либо вставлен вручную, либо подтягиваем по ссылке
    job_text = req.job_text.strip()
    if len(job_text) < 30 and req.job_url.strip():
        job_text = await _fetch_job_text(req.job_url.strip())
    if len(job_text) < 30:
        raise HTTPException(400, "Вставьте текст вакансии или ссылку на неё")
    with get_db() as db:
        p = db.execute("SELECT data FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    if not p:
        raise HTTPException(404, "Сначала сохраните профиль")
    with get_db() as db:
        ok, col, uses_left = _deduct(db, user["id"])
        if not ok:
            return JSONResponse(status_code=402, content={"error": "no_uses"})
    try:
        raw = await call_ai(_match_prompt(json.loads(p["data"]), job_text, req.extra_hint))
        resume = _parse_ai(raw)
    except Exception:
        with get_db() as db:
            _refund(db, user["id"], col)
            log_event(db, "generate_fail", user_id=user["id"], kind="match")
            db.commit()
        raise
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "matched", req.company, req.job_url, job_text)
            log_event(db, "generate", user_id=user["id"], kind="match", col=col)
            db.commit()
        except ValueError as e:
            if "resume_limit" in str(e):
                # Генерация уже потрачена на успешный вызов AI, а сохранить
                # результат некуда — возвращаем списание, иначе пользователь
                # платит за упор в лимит хранилища.
                _refund(db, user["id"], col)
                return JSONResponse(status_code=402, content={"error": "resume_limit"})
            raise
    return {"resume": resume, "resume_id": rid, "uses_left": uses_left}

@app.post("/api/generate-from-profile")
@rate("20/minute")
async def generate_from_profile(req: GenerateFromProfileReq, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    with get_db() as db:
        p = db.execute("SELECT data FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    if not p:
        raise HTTPException(404, "Сначала сохраните профиль")
    with get_db() as db:
        ok, col, uses_left = _deduct(db, user["id"])
        if not ok:
            return JSONResponse(status_code=402, content={"error": "no_uses"})
    try:
        raw  = await call_ai(_general_prompt(json.loads(p["data"]), req.target_role, req.hint))
        resume = _parse_ai(raw)
    except Exception:
        with get_db() as db:
            _refund(db, user["id"], col)
            log_event(db, "generate_fail", user_id=user["id"], kind="from_profile")
            db.commit()
        raise
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "general")
            log_event(db, "generate", user_id=user["id"], kind="from_profile", col=col)
            db.commit()
        except ValueError as e:
            if "resume_limit" in str(e):
                _refund(db, user["id"], col)
                return JSONResponse(status_code=402, content={"error": "resume_limit"})
            raise
    return {"resume": resume, "resume_id": rid, "uses_left": uses_left}

@app.post("/api/generate")
@rate("20/minute")
async def generate(req: GenerateReq, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    with get_db() as db:
        ok, col, uses_left = _deduct(db, user["id"])
        if not ok:
            return JSONResponse(status_code=402, content={"error": "no_uses"})
    try:
        raw    = await call_ai(_generate_prompt(req))
        resume = _parse_ai(raw)
    except Exception:
        with get_db() as db:
            _refund(db, user["id"], col)
            log_event(db, "generate_fail", user_id=user["id"], kind="generate")
            db.commit()
        raise
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "general")
            log_event(db, "generate", user_id=user["id"], kind="generate", col=col)
            db.commit()
        except ValueError as e:
            if "resume_limit" in str(e):
                _refund(db, user["id"], col)
                return JSONResponse(status_code=402, content={"error": "resume_limit"})
            raise
    return {"resume": resume, "resume_id": rid, "uses_left": uses_left}

# ── Fetch job URL ──────────────────────────────────────────────────────────
def _assert_public_host(host: str) -> None:
    """Бросает HTTPException, если host резолвится в приватный/служебный адрес.

    Защита от SSRF: без неё /api/fetch-job (без авторизации) позволял заставить
    сервер ходить на localhost:11434 (Ollama), внутренние сервисы и cloud
    metadata 169.254.169.254 — и возвращал тело ответа пользователю.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        raise HTTPException(400, "Не удалось распознать адрес вакансии")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise HTTPException(400, "Недопустимый адрес — ссылка ведёт во внутреннюю сеть")

# Больше страницы вакансии не бывает, а тело ответа целиком уезжает в память:
# без этого предела ссылка на большой файл выедала всю RAM сервера.
MAX_JOB_BYTES = 512 * 1024
# Вакансия — это текст. Всё остальное качать смысла нет.
JOB_CONTENT_TYPES = ("text/", "application/xhtml+xml", "application/xml")


async def _fetch_job_text(url: str) -> str:
    """Скачивает страницу вакансии и возвращает её текст (без HTML-тегов).

    Редиректы следуем вручную и проверяем каждый хоп через _assert_public_host —
    иначе SSRF-защиту можно обойти редиректом с внешнего URL на внутренний адрес.

    Тело читаем потоком и обрываем на MAX_JOB_BYTES: раньше ответ загружался
    целиком (`r.text`), а обрезка до 4000 символов происходила уже после — то
    есть ссылка на многогигабайтный файл клала процесс по памяти.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Некорректная ссылка на вакансию")
    log.info("fetch-job start: %s", url)
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}
        ) as h:
            current = url
            html, status = "", None
            for _ in range(5):
                parsed = urlparse(current)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise HTTPException(400, "Некорректная ссылка на вакансию")
                _assert_public_host(parsed.hostname)
                async with h.stream("GET", current) as r:
                    if r.is_redirect and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(JOB_CONTENT_TYPES):
                        raise HTTPException(
                            400, "По ссылке не страница с текстом — вставьте описание вакансии вручную"
                        )
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= MAX_JOB_BYTES:
                            log.info("fetch-job: ответ обрезан на %d байтах (%s)", size, current)
                            break
                    html = b"".join(chunks).decode(r.charset_encoding or "utf-8", errors="replace")
                    status = r.status_code
                break
            else:
                raise HTTPException(400, "Слишком много перенаправлений по ссылке")
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()
        log.info("fetch-job ok: %s -> %d chars (HTTP %s)", url, len(text), status)
        return text[:4000]
    except HTTPException:
        raise
    except Exception as e:
        log.warning("fetch-job failed: %s: %s", url, e)
        raise HTTPException(502, "Не удалось загрузить вакансию по ссылке — вставьте текст вручную")

@app.post("/api/fetch-job")
@rate("20/minute")
async def fetch_job(request: Request):
    """Подтягивает текст вакансии по ссылке для редактора.

    Требует сессию: анонимная ручка превращала сервер в открытый прокси —
    любой мог заставить его скачивать произвольные страницы своим IP и
    получать содержимое обратно.
    """
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Некорректная ссылка на вакансию")
    url = str(body.get("url", "")).strip()
    return {"text": await _fetch_job_text(url)}

# ── Payments (Робокасса) ─────────────────────────────────────────────────
def _robokassa_signature(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode()).hexdigest()


def _strip_xml_ns(elem):
    """Убирает namespace из тегов, чтобы искать по локальному имени
    (State/Code/Info/OutSum) не завися от точного xmlns ответа Робокассы."""
    for e in elem.iter():
        if "}" in e.tag:
            e.tag = e.tag.split("}", 1)[1]
    return elem


async def _robokassa_confirmed(inv_id: str, expected_sum: str) -> bool:
    """Независимая проверка платежа через OpStateExt — не доверяем только
    подписи вебхука, как раньше не доверяли только телу вебхука ЮKassa."""
    signature = _robokassa_signature(ROBOKASSA_LOGIN, inv_id, ROBOKASSA_PASSWORD2)
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(
            "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt",
            params={"MerchantLogin": ROBOKASSA_LOGIN, "InvoiceID": inv_id, "Signature": signature},
        )
    r.raise_for_status()
    root = _strip_xml_ns(ET.fromstring(r.text))
    code = root.findtext(".//State/Code")
    out_sum = root.findtext(".//Info/OutSum")
    if code != "100":
        return False
    if out_sum != expected_sum:
        log.warning("pay/webhook: сумма в OpStateExt %s не совпадает с ожидаемой %s", out_sum, expected_sum)
        return False
    return True


@app.post("/api/pay")
@rate("10/minute")
async def create_payment(req: PayReq, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")

    # Робокасса ещё не подключена (нет ключей) — отдаём понятную ошибку.
    if not (ROBOKASSA_LOGIN and ROBOKASSA_PASSWORD1 and ROBOKASSA_PASSWORD2):
        log.warning("pay: Робокасса не настроена (ROBOKASSA_LOGIN/PASSWORD1/PASSWORD2 пусты), user=%s", user["id"])
        raise HTTPException(503, "Оплата временно недоступна. Попробуйте позже.")

    # В отличие от ЮKassa, у Робокассы нет отдельного вызова API для создания
    # платежа — просто собираем подписанный redirect URL сами. insert+update
    # в одном db-блоке: при любой ошибке он откатится целиком сам (get_db()),
    # отдельная очистка «висячей» строки не нужна.
    idem = str(uuid.uuid4())
    with get_db() as db:
        cur = db.execute("INSERT INTO payments (user_id, idem_key) VALUES (?,?)", (user["id"], idem))
        inv_id = cur.lastrowid
        db.execute("UPDATE payments SET pay_id=? WHERE idem_key=?", (str(inv_id), idem))
        db.commit()

    # Описание должно совпадать с тем, что реально выдаёт вебхук (Pro на
    # PRO_DAYS дней) — иначе в чеке у покупателя одна услуга, а в аккаунте другая.
    description = f"Резюмирую.рф Pro, {PRO_DAYS} дней безлимитных генераций"
    signature = _robokassa_signature(ROBOKASSA_LOGIN, PRO_PRICE, str(inv_id), ROBOKASSA_PASSWORD1)
    params = {
        "MerchantLogin": ROBOKASSA_LOGIN,
        "OutSum": PRO_PRICE,
        "InvId": inv_id,
        "Description": description,
        "SignatureValue": signature,
        "Culture": "ru",
    }
    if user.get("email"):
        params["Email"] = user["email"]
    if ROBOKASSA_TEST_MODE:
        params["IsTest"] = 1
    url = f"https://auth.robokassa.ru/Merchant/Index.aspx?{urlencode(params)}"
    log.info("pay: платёж создан user=%s inv_id=%s", user["id"], inv_id)
    return {"url": url}

# GET и POST — метод ResultURL выбирается в кабинете Робокассы. Раньше роут
# принимал только POST, а разбор ветвился на request.method: при настройке
# кабинета на GET каждый вебхук получал бы 405, и оплативший не получал бы
# Pro — молча, без единой записи в логах приложения.
@app.api_route("/api/pay/webhook", methods=["GET", "POST"])
async def payment_webhook(request: Request):
    # ResultURL настраивается в личном кабинете Робокассы методом GET или
    # POST — код не полагается на конкретный выбор.
    data = request.query_params if request.method == "GET" else await request.form()
    out_sum = data.get("OutSum", "")
    inv_id = data.get("InvId", "")
    received_signature = data.get("SignatureValue", "")
    log.info("pay/webhook: InvId=%s", inv_id)
    if not (out_sum and inv_id and received_signature):
        return PlainTextResponse("bad request", status_code=400)

    # Подпись Робокассы (Password#2) — единственное, что доказывает, что
    # запрос реально пришёл от Робокассы, а не подделан снаружи.
    expected_signature = _robokassa_signature(out_sum, inv_id, ROBOKASSA_PASSWORD2)
    if not hmac.compare_digest(expected_signature.lower(), received_signature.lower()):
        log.warning("pay/webhook: неверная подпись для InvId=%s", inv_id)
        return PlainTextResponse("bad signature", status_code=400)

    # Получателя берём ТОЛЬКО из своей таблицы payments по InvId, который мы
    # сами сгенерировали при создании платежа — так же, как раньше искали по
    # pay_id ЮKassa, а не по чему-либо присланному в теле запроса.
    with get_db() as db:
        pay_row = db.execute(
            "SELECT user_id, status FROM payments WHERE pay_id=?", (inv_id,)
        ).fetchone()
    if not pay_row:
        log.warning("pay/webhook: платёж %s не найден в базе, Pro не выдан", inv_id)
        return PlainTextResponse("unknown payment", status_code=400)
    if pay_row["status"] == "succeeded":
        log.info("pay/webhook: повторный webhook для обработанного платежа %s", inv_id)
        return PlainTextResponse(f"OK{inv_id}")
    user_id = pay_row["user_id"]

    if out_sum != PRO_PRICE:
        log.warning("pay/webhook: сумма в вебхуке %s не совпадает с ожидаемой %s", out_sum, PRO_PRICE)
        return PlainTextResponse("amount mismatch", status_code=400)

    # ── КРИТИЧНО: подтверждаем платёж напрямую через OpStateExt Робокассы ──
    # Не доверяем только вебхуку, даже с верной подписью — подтверждаем через API.
    try:
        confirmed = await _robokassa_confirmed(inv_id, PRO_PRICE)
    except Exception:
        log.exception("pay/webhook: ошибка проверки платежа %s в Робокассе", inv_id)
        confirmed = False  # не выдаём Pro при ошибке проверки

    if not confirmed:
        log.warning("pay/webhook: платёж %s не подтверждён Робокассой, Pro не выдан", inv_id)
        return PlainTextResponse("payment not confirmed", status_code=400)

    with get_db() as db:
        # Помечаем обработанным одним UPDATE с условием: параллельный дубль
        # вебхука получит rowcount=0 и не выдаст Pro второй раз.
        claimed = db.execute(
            "UPDATE payments SET status='succeeded' WHERE pay_id=? AND status!='succeeded'",
            (inv_id,)
        ).rowcount
        if not claimed:
            log.info("pay/webhook: повторный webhook для обработанного платежа %s", inv_id)
            return PlainTextResponse(f"OK{inv_id}")
        existing = db.execute(
            "SELECT pro_expires_at, is_pro FROM users WHERE id=?", (user_id,)
        ).fetchone()
        row_pro = _is_pro(existing) if existing else False
        if row_pro and existing["pro_expires_at"]:
            try:
                base = datetime.fromisoformat(existing["pro_expires_at"])
                if base.tzinfo is None:
                    base = base.replace(tzinfo=timezone.utc)
            except Exception:
                base = datetime.now(timezone.utc)
        else:
            base = datetime.now(timezone.utc)
        new_exp = (base + timedelta(days=PRO_DAYS)).isoformat()
        db.execute(
            "UPDATE users SET is_pro=1, pro_expires_at=? WHERE id=?",
            (new_exp, user_id)
        )
        log_event(db, "payment", user_id=user_id, pay_id=inv_id)
        db.commit()
        log.info("pay/webhook: Pro выдан user=%s inv_id=%s до %s", user_id, inv_id, new_exp)
    return PlainTextResponse(f"OK{inv_id}")

# ── Возврат из Робокассы: Success URL / Fail URL ──────────────────────────
# Это возврат браузера покупателя, а не уведомление о платеже. Подписку по ним
# НЕ выдаём, даже если параметры выглядят правильно: единственный источник
# правды — ResultURL-вебхук выше, который сверяет подпись и отдельно
# подтверждает платёж через OpStateExt. Причины ровно две.
#   1. Браузер сюда может вообще не вернуться (закрыл вкладку, потерял сеть) —
#      и подписка всё равно обязана появиться. Значит логика выдачи должна жить
#      в вебхуке, а дублировать её здесь — заводить второй путь к двойной выдаче.
#   2. На эту страницу можно просто зайти руками.
# Поэтому страницы ничего не принимают на веру и показывают то, что видит наша
# собственная база: фронт опрашивает /api/me и ждёт появления Pro.
#
# GET и POST одновременно — метод возврата настраивается в кабинете Робокассы,
# и промах в этой настройке иначе давал бы покупателю 405 после оплаты.
@app.api_route("/pay/success", methods=["GET", "HEAD", "POST"], response_class=HTMLResponse)
async def pay_success(request: Request):
    user = await get_current_user(request)
    resp = tpl.TemplateResponse(request, "pay_success.html", {
        "user": user,
        "pro_days": PRO_DAYS,
    })
    # Ответ зависит от состояния сессии и от того, доехал ли вебхук, — не кешируем.
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.api_route("/pay/fail", methods=["GET", "HEAD", "POST"], response_class=HTMLResponse)
async def pay_fail(request: Request):
    user = await get_current_user(request)
    resp = tpl.TemplateResponse(request, "pay_fail.html", {
        "user": user,
        "pro_price": PRO_PRICE,
    })
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ── Promo codes ────────────────────────────────────────────────────────────
@app.post("/api/promo/activate")
@rate("5/minute")
async def promo_activate(body: PromoActivateReq, request: Request):
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")

    code = body.code.strip().upper()
    if not code:
        raise HTTPException(400, "Код недействителен")

    with get_db() as db:
        result = db.execute(
            "UPDATE promo_codes SET used_count = used_count + 1"
            " WHERE code = ? AND active = 1 AND used_count < max_uses"
            "   AND (expires_at IS NULL OR expires_at > datetime('now'))",
            (code,)
        )
        if result.rowcount == 0:
            raise HTTPException(400, "Код недействителен")

        try:
            db.execute("INSERT INTO promo_activations (code, user_id) VALUES (?,?)",
                      (code, user["id"]))
            db.commit()
        except Exception:
            db.execute("UPDATE promo_codes SET used_count = used_count - 1 WHERE code = ?", (code,))
            db.commit()
            raise HTTPException(400, "Код уже активирован")

    with get_db() as db:
        promo = db.execute("SELECT kind, value FROM promo_codes WHERE code=?", (code,)).fetchone()
        kind, value = promo["kind"], promo["value"]

        if kind == "pro_days":
            existing = db.execute(
                "SELECT pro_expires_at, is_pro FROM users WHERE id=?", (user["id"],)
            ).fetchone()
            row_pro = _is_pro(existing) if existing else False
            if row_pro and existing["pro_expires_at"]:
                try:
                    base = datetime.fromisoformat(existing["pro_expires_at"])
                    if base.tzinfo is None:
                        base = base.replace(tzinfo=timezone.utc)
                except Exception:
                    base = datetime.now(timezone.utc)
            else:
                base = datetime.now(timezone.utc)
            new_exp = (base + timedelta(days=value)).isoformat()
            db.execute("UPDATE users SET is_pro=1, pro_expires_at=? WHERE id=?", (new_exp, user["id"]))
            msg = f"Pro до {new_exp.split('T')[0]}"
        elif kind == "gen_pack":
            db.execute("UPDATE users SET paid_left=paid_left+? WHERE id=?", (value, user["id"]))
            msg = f"+{value} генераций"
        else:  # unlimited
            db.execute("UPDATE users SET is_pro=1, pro_expires_at='2099-12-31 00:00:00' WHERE id=?", (user["id"],))
            msg = "Безлимит активирован"

        log_event(db, "promo_activate", user_id=user["id"], code_prefix=code[:4])
        db.commit()

    log.info("promo/activate: user=%s code=%s kind=%s", user["id"], code[:4], kind)
    return {"ok": True, "kind": kind, "message": msg}

# ── Admin pages and API ────────────────────────────────────────────────────
@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = await get_current_user(request)
    _require_admin(user)
    return tpl.TemplateResponse(request, "admin.html", {
    })

@app.post("/api/admin/promo")
async def admin_create_promo(body: PromoCreateReq, request: Request):
    user = await get_current_user(request)
    _require_admin(user)

    # join без разделителя: с "-".join дефисы попадали в исходную строку, и
    # нарезка ниже давала «X-T--C-H--7-F-» — 7 случайных символов вместо 12.
    code = "".join(secrets.choice("ABCDEFGHJKMNPQRSTUVWXYZ23456789") for _ in range(12))
    code = f"{code[:4]}-{code[4:8]}-{code[8:12]}"

    with get_db() as db:
        db.execute(
            "INSERT INTO promo_codes (code, kind, value, max_uses, expires_at, comment)"
            " VALUES (?,?,?,?,?,?)",
            (code, body.kind, body.value, body.max_uses, body.expires_at, body.comment)
        )
        db.commit()

    log.info("admin/promo: created code=%s kind=%s", code[:4], body.kind)
    return {"ok": True, "code": code}

@app.get("/api/admin/promo")
async def admin_list_promo(request: Request):
    user = await get_current_user(request)
    _require_admin(user)

    with get_db() as db:
        rows = db.execute(
            "SELECT code, kind, value, max_uses, used_count, active, expires_at, comment, created"
            " FROM promo_codes ORDER BY created DESC"
        ).fetchall()

    return {"codes": [dict(r) for r in rows]}

@app.post("/api/admin/promo/deactivate")
async def admin_deactivate_promo(body: dict, request: Request):
    user = await get_current_user(request)
    _require_admin(user)

    code = body.get("code", "").strip().upper()
    with get_db() as db:
        db.execute("UPDATE promo_codes SET active=0 WHERE code=?", (code,))
        db.commit()

    log.info("admin/promo: deactivated code=%s", code[:4] if len(code) > 4 else code)
    return {"ok": True}

@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    user = await get_current_user(request)
    _require_admin(user)

    with get_db() as db:
        # За последние 30 дней
        past_30 = "datetime('now', '-30 days')"

        # Генерации по дням
        gen_by_day = db.execute(f"""
            SELECT date(created) as day,
                   sum(case when event='generate' then 1 else 0 end) as ok,
                   sum(case when event='generate_fail' then 1 else 0 end) as fail
            FROM usage_events
            WHERE created > {past_30}
            GROUP BY date(created)
            ORDER BY day
        """).fetchall()

        # Топ-10 пользователей
        top_users = db.execute(f"""
            SELECT u.id, u.email, u.display_name,
                   count(*) as count
            FROM usage_events e
            JOIN users u ON e.user_id = u.id
            WHERE e.event='generate' AND e.created > {past_30}
            GROUP BY u.id
            ORDER BY count DESC
            LIMIT 10
        """).fetchall()

        # Статистика по пользователям
        users_total = db.execute("SELECT count(*) FROM users").fetchone()[0]
        users_new = db.execute(f"SELECT count(*) FROM users WHERE created > {past_30}").fetchone()[0]

        # Входы по методам
        logins = db.execute(f"""
            SELECT json_extract(meta, '$.method') as method, count(*) as count
            FROM usage_events
            WHERE event='login' AND created > {past_30}
            GROUP BY json_extract(meta, '$.method')
        """).fetchall()

        # Платежи
        payments = db.execute(f"""
            SELECT count(*) as count FROM payments
            WHERE status='succeeded' AND created > {past_30}
        """).fetchone()[0]

        # Активации промокодов
        promos = db.execute(f"""
            SELECT count(*) as count FROM usage_events
            WHERE event='promo_activate' AND created > {past_30}
        """).fetchone()[0]

        # Pro-пользователи
        pro_users = db.execute(
            "SELECT count(*) FROM users WHERE is_pro=1 AND pro_expires_at > datetime('now')"
        ).fetchone()[0]

        # Общие генерации за 30 дней
        total_gen = db.execute(f"""
            SELECT count(*) FROM usage_events
            WHERE event='generate' AND created > {past_30}
        """).fetchone()[0]

    return {
        "users": {
            "total": users_total,
            "new_7days": users_new,
            "pro_active": pro_users,
        },
        "generations": {
            "total_30days": total_gen,
            "by_day": [{"day": dict(r)["day"], "ok": dict(r)["ok"] or 0, "fail": dict(r)["fail"] or 0} for r in gen_by_day],
        },
        "logins": [{"method": dict(r)["method"], "count": dict(r)["count"]} for r in logins],
        "payments_30days": payments,
        "promos_30days": promos,
        "top_users": [{
            "id": dict(r)["id"],
            "email": dict(r)["email"],
            "name": dict(r)["display_name"],
            "count": dict(r)["count"],
        } for r in top_users],
    }

# Промпты вынесены в prompts.py.
from prompts import _match_prompt, _general_prompt, _generate_prompt  # noqa: E402,F401

# ── MCP server (Model Context Protocol) ────────────────────────────────────
# Доступ из Claude Desktop/Code к адаптации резюме. Подключение:
#   claude mcp add --transport http resuming https://xn--e1aedprev8fe.xn--p1ai/mcp \
#     --header "Authorization: Bearer <токен>"
# Токен выдаёт POST /api/mcp-token (требует обычной сессии на сайте).
mcp_server = FastMCP(
    "Резюмирую.рф",
    instructions=(
        "Адаптация резюме под вакансию через сервис Резюмирую.рф. "
        "Требуется токен: войдите на сайте и вызовите POST /api/mcp-token."
    ),
    stateless_http=True,   # каждый запрос независим — не нужны MCP-сессии
    json_response=True,    # обычный JSON вместо SSE — дружелюбно к nginx/Cloudflare
)

MCP_TOKEN_HINT = ("Получите токен: войдите на сайте и выполните POST /api/mcp-token, "
                  "затем подключите MCP с заголовком 'Authorization: Bearer <токен>'.")

def _mcp_user(ctx: Context) -> dict:
    """Достаёт пользователя по заголовку Authorization: Bearer <token> HTTP-запроса MCP."""
    http_req = ctx.request_context.request
    auth = http_req.headers.get("authorization", "") if http_req is not None else ""
    scheme, _, token = auth.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ValueError(f"Нет токена авторизации. {MCP_TOKEN_HINT}")
    with get_db() as db:
        row = db.execute(
            "SELECT u.* FROM api_tokens t JOIN users u ON u.id = t.user_id WHERE t.token=?",
            (token,)
        ).fetchone()
    if not row:
        raise ValueError(f"Токен недействителен. {MCP_TOKEN_HINT}")
    return dict(row)

@mcp_server.tool()
async def get_profile(ctx: Context) -> dict:
    """Возвращает сохранённый профиль пользователя Резюмирую.рф
    (имя, контакты, опыт, образование, навыки, языки)."""
    user = _mcp_user(ctx)
    with get_db() as db:
        row = db.execute("SELECT data FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    if not row:
        raise ValueError("Профиль не найден — сначала заполните профиль на сайте.")
    return json.loads(row["data"])

@mcp_server.tool()
async def adapt_resume(vacancy_text: str, ctx: Context) -> dict:
    """Адаптирует сохранённое резюме пользователя под текст вакансии.
    Списывает одну генерацию (как кнопка «Адаптировать» на сайте).
    Возвращает готовый JSON резюме, id сохранённой версии и остаток генераций."""
    user = _mcp_user(ctx)
    # Семантика 1-в-1 с /api/match (но текст вакансии передаётся только текстом)
    job_text = vacancy_text.strip()
    if len(job_text) < 30:
        raise ValueError("Вставьте текст вакансии (минимум 30 символов)")
    with get_db() as db:
        p = db.execute("SELECT data FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    if not p:
        raise ValueError("Сначала сохраните профиль на сайте")
    with get_db() as db:
        ok, col, uses_left = _deduct(db, user["id"])
        if not ok:
            raise ValueError("Закончились генерации (no_uses) — купите пакет или Pro на сайте")
    try:
        raw = await call_ai(_match_prompt(json.loads(p["data"]), job_text, ""))
        resume = _parse_ai(raw)
    except Exception as e:
        with get_db() as db:
            _refund(db, user["id"], col)
        detail = getattr(e, "detail", None) or str(e)
        raise ValueError(f"Ошибка генерации: {detail}")
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "matched", "", "", job_text)
        except ValueError as e:
            if "resume_limit" in str(e):
                _refund(db, user["id"], col)
                db.commit()
                raise ValueError("Достигнут лимит хранимых резюме (resume_limit) — "
                                 "удалите старые резюме на сайте или оформите Pro")
            raise
    return {"resume": resume, "resume_id": rid, "uses_left": uses_left}

@app.post("/api/mcp-token")
@rate("10/minute")
async def create_mcp_token(request: Request):
    """Выдаёт API-токен для MCP-доступа. Один активный токен на пользователя:
    старые токены удаляются. Токен показывается только один раз."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    token = str(uuid.uuid4())
    with get_db() as db:
        db.execute("DELETE FROM api_tokens WHERE user_id=?", (user["id"],))
        db.execute("INSERT INTO api_tokens (token, user_id) VALUES (?,?)", (token, user["id"]))
        db.commit()
    log.info("mcp-token: issued for user=%s", user["id"])
    return {"token": token}

class _McpMountOr404:
    """MCP смонтирован на «/», поэтому ему достаются вообще все URL, которые не
    разобрал FastAPI, — и на опечатку в адресе пользователь получал голое
    «Not Found» от чужого ASGI-приложения. Пропускаем внутрь только /mcp,
    остальное возвращаем приложению как обычную 404 (её отрисует error.html).
    """

    def __init__(self, mcp_app):
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        is_mcp = path == "/mcp" or path == "/mcp/" or path.startswith("/mcp/")
        if is_mcp or scope.get("type") not in ("http", "websocket"):
            return await self.mcp_app(scope, receive, send)
        raise StarletteHTTPException(status_code=404, detail="Not Found")


# Монтируем streamable-http app в КОНЦЕ файла: FastAPI-роуты, объявленные выше,
# имеют приоритет, а endpoint MCP оказывается ровно на /mcp
# (streamable_http_path по умолчанию "/mcp" внутри под-приложения).
app.mount("/", _McpMountOr404(mcp_server.streamable_http_app()))
