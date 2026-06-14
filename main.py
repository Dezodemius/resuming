import asyncio
import ipaddress
import os
import json
import re
import socket
import uuid
import hashlib
import hmac
import time
from urllib.parse import urlencode, urljoin, urlparse
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import httpx
from mcp.server.fastmcp import Context, FastMCP

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    _RATE_LIMIT = True
except ImportError:
    _RATE_LIMIT = False

# Конфигурация вынесена в config.py. Импортируем имена в пространство main —
# существующий код и тесты обращаются к ним как к main.* (в т.ч. monkeypatch).
from config import (  # noqa: E402
    log,
    OLLAMA_URL, MODEL, YOKASSA_SHOP, YOKASSA_SECRET, APP_URL,
    TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_NAME,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM,
    YANDEX_CLIENT_ID, YANDEX_CLIENT_SECRET,
    FREE_RESUMES, PRO_PRICE, PRO_DAYS, ANON_LIMIT_CONST,
    PAID_PACK, PACK_PRICE, SESSION_DAYS, MAGIC_MINUTES, AI_CONCURRENCY,
    SECRET_KEY,
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

# ── Database ── слой БД вынесен в db.py (get_db/init_db).
from db import get_db, init_db  # noqa: E402

@asynccontextmanager
async def lifespan(app):
    init_db()
    # Session manager MCP-сервера должен жить весь срок работы приложения
    # (mcp_server определён ниже; смонтирован в конце файла)
    async with mcp_server.session_manager.run():
        yield

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
if _RATE_LIMIT:
    limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

def rate(limit: str):
    """Декоратор-заглушка если slowapi не установлен."""
    def decorator(fn):
        if _RATE_LIMIT:
            return limiter.limit(limit)(fn)
        return fn
    return decorator

# ── Auth helpers ─────────────────────────────────────────────────────────
def _verify_telegram(data: dict) -> bool:
    """Verify Telegram Login Widget signature."""
    if not TELEGRAM_BOT_TOKEN:
        return False
    d = {k: v for k, v in data.items() if k != "hash"}
    check_hash = data.get("hash", "")
    data_check = "\n".join(f"{k}={d[k]}" for k in sorted(d))
    secret = hashlib.sha256(TELEGRAM_BOT_TOKEN.encode()).digest()
    expected = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, check_hash):
        return False
    if time.time() - int(data.get("auth_date", 0)) > 3600:
        return False
    return True

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

def _upsert_user_by_email(db, email: str) -> dict:
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
    TgAuthData, EmailReq, ProfileData, MatchReq, GenerateFromProfileReq,
    GenerateReq, PayReq, ImproveReq, AnonymousPreviewReq,
)

ANON_LIMIT = ANON_LIMIT_CONST

# ── Anonymous preview (no auth, no save) ─────────────────────────────────
@app.post("/api/generate-preview")
@rate("10/minute")
async def generate_preview(req: AnonymousPreviewReq, request: Request, response: Response):
    """
    Анонимная генерация: профиль инлайн, результат не сохраняется.
    Cookie anon_id подписан HMAC — нельзя сбросить лимит очисткой cookie.
    """
    # Читаем и верифицируем подписанный cookie
    signed  = request.cookies.get("anon_id", "")
    anon_id = _verify_anon(signed) if signed else None
    if not anon_id:
        anon_id = str(uuid.uuid4())

    # Пишем подписанный cookie обратно (httpOnly)
    response.set_cookie(
        "anon_id", _sign_anon(anon_id),
        max_age=7 * 86400, samesite="lax",
        secure=APP_URL.startswith("https"), httponly=True,
    )

    # Текст вакансии: вручную или по ссылке (до списания лимита)
    job_text = req.job_text.strip()
    if req.kind == "match":
        if len(job_text) < 30 and req.job_url.strip():
            job_text = await _fetch_job_text(req.job_url.strip())
        if len(job_text) < 30:
            raise HTTPException(400, "Вставьте текст вакансии или ссылку на неё")

    with get_db() as db:
        row  = db.execute("SELECT uses FROM anon_usage WHERE anon_id=?", (anon_id,)).fetchone()
        uses = row["uses"] if row else 0
        if uses >= ANON_LIMIT:
            return JSONResponse(status_code=429,
                                content={"error": "anon_limit", "limit": ANON_LIMIT})
        db.execute(
            "INSERT INTO anon_usage (anon_id, uses) VALUES (?,1)"
            " ON CONFLICT(anon_id) DO UPDATE SET uses=uses+1",
            (anon_id,)
        )
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
            db.execute("UPDATE anon_usage SET uses=uses-1 WHERE anon_id=?", (anon_id,))
            db.commit()
        raise

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
        async with httpx.AsyncClient(timeout=5) as http:
            r = await http.get(f"{OLLAMA_URL}/api/tags")
        if r.status_code == 200:
            models = [m.get("name", "") for m in r.json().get("models", [])]
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
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = await get_current_user(request)
    return tpl.TemplateResponse(request, "index.html", {
        "telegram_bot_name": TELEGRAM_BOT_NAME,
        "yandex_enabled": bool(YANDEX_CLIENT_ID),
        "user": user,
    })

@app.get("/resumes/{resume_id}", response_class=HTMLResponse)
async def resume_edit_page(resume_id: int, request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/?auth_required=1", status_code=303)
    with get_db() as db:
        exists = db.execute(
            "SELECT id FROM resumes WHERE id=? AND user_id=?", (resume_id, user["id"])
        ).fetchone()
    if not exists:
        return RedirectResponse(url="/resumes", status_code=303)
    return tpl.TemplateResponse(request, "resume_edit.html", {
        "resume_id":  resume_id,
        "telegram_bot_name": TELEGRAM_BOT_NAME,
        "user": user,
    })

# ── AI section improvement ────────────────────────────────────────────────
@app.post("/api/improve-text")
async def improve_text(req: ImproveReq, request: Request):
    """Per-section AI improvement, used from the resume editor."""
    user = await get_current_user(request)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    if not req.text.strip():
        raise HTTPException(400, "Текст не может быть пустым")

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
    try:
        result = await call_ai(prompt)
        return {"improved": result.strip()}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Page routes ────────────────────────────────────────────────────────────
@app.get("/resumes", response_class=HTMLResponse)
async def resumes_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/?auth_required=1", status_code=303)
    return tpl.TemplateResponse(request, "resumes.html", {
        "telegram_bot_name": TELEGRAM_BOT_NAME,
        "user": user,
    })

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/?auth_required=1", status_code=303)
    return tpl.TemplateResponse(request, "settings.html", {
        "telegram_bot_name": TELEGRAM_BOT_NAME,
        "user": user,
    })

# ── Public / legal pages (no auth required) ───────────────────────────────
@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    return tpl.TemplateResponse(request, "pricing.html")

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
@app.post("/auth/telegram")
async def auth_telegram(data: TgAuthData, response: Response):
    if not _verify_telegram(data.dict()):
        raise HTTPException(401, "Неверная подпись Telegram")
    name = f"{data.first_name or ''} {data.last_name or ''}".strip() or data.username or "Пользователь"
    with get_db() as db:
        db.execute(
            "INSERT INTO users (telegram_id, tg_name, tg_photo, display_name)"
            " VALUES (?,?,?,?)"
            " ON CONFLICT(telegram_id) DO UPDATE SET"
            "   tg_name=excluded.tg_name, tg_photo=excluded.tg_photo, display_name=excluded.display_name",
            (data.id, data.username, data.photo_url, name)
        )
        db.commit()
        u = db.execute("SELECT * FROM users WHERE telegram_id=?", (data.id,)).fetchone()
        sid = _create_session(db, u["id"])
    log.info("auth/telegram: login ok user_id=%s", u["id"])
    _set_session_cookie(response, sid)
    return {"ok": True, "user": {"name": u["display_name"], "photo": u["tg_photo"], "free_left": u["free_left"]}}

@app.post("/auth/email/request")
async def auth_email_request(req: EmailReq):
    token = str(uuid.uuid4())
    with get_db() as db:
        # Срок (15 мин) считаем в SQLite, чтобы формат совпал с datetime('now') при
        # проверке. Раньше хранилась наивная ISO-строка локального времени с 'T':
        # из-за текстового сравнения '...T...' > '... ...' токен фактически жил до
        # конца суток UTC, а не 15 минут — обход короткого окна одноразового входа.
        db.execute(
            "INSERT OR REPLACE INTO magic_tokens (token, email, expires_at)"
            " VALUES (?,?,datetime('now',?))",
            (token, req.email, f"+{MAGIC_MINUTES} minutes")
        )
        db.commit()
    err = await _send_magic_email(req.email, token)
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
    r = RedirectResponse(url="/?login=success", status_code=303)
    _set_session_cookie(r, sid)
    return r

# ── Yandex OAuth ──────────────────────────────────────────────────────────
@app.get("/auth/yandex")
async def auth_yandex_start():
    if not YANDEX_CLIENT_ID:
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
        return RedirectResponse(url="/?auth_error=yandex", status_code=303)
    if not state or state != request.cookies.get("ya_state"):
        log.warning("auth/yandex callback: state mismatch")
        return RedirectResponse(url="/?auth_error=yandex", status_code=303)
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
                return RedirectResponse(url="/?auth_error=yandex", status_code=303)
            ir = await http.get("https://login.yandex.ru/info",
                                params={"format": "json"},
                                headers={"Authorization": f"OAuth {access}"})
            info = ir.json()
    except Exception:
        log.exception("auth/yandex: OAuth request failed")
        return RedirectResponse(url="/?auth_error=yandex", status_code=303)

    email = info.get("default_email") or ""
    if not email:
        log.error("auth/yandex: no default_email in userinfo")
        return RedirectResponse(url="/?auth_error=yandex", status_code=303)

    with get_db() as db:
        u = _upsert_user_by_email(db, email)
        name = info.get("real_name") or info.get("display_name")
        if name and u.get("display_name") in (None, "", email.split("@")[0]):
            db.execute("UPDATE users SET display_name=? WHERE id=?", (name, u["id"]))
            db.commit()
        sid = _create_session(db, u["id"])
    log.info("auth/yandex: login ok user_id=%s", u["id"])
    r = RedirectResponse(url="/?login=success", status_code=303)
    r.delete_cookie("ya_state")
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
        "photo":          user.get("tg_photo"),
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
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=5.0)
            ) as http:
                r = await http.post(
                    f"{OLLAMA_URL}/v1/chat/completions",
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
    except Exception as e:
        with get_db() as db:
            _refund(db, user["id"], col)
        raise HTTPException(500, str(e))
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "matched", req.company, req.job_url, job_text)
        except ValueError as e:
            if "resume_limit" in str(e):
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
    except Exception as e:
        with get_db() as db:
            _refund(db, user["id"], col)
        raise HTTPException(500, str(e))
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "general")
        except ValueError as e:
            if "resume_limit" in str(e):
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
    except Exception as e:
        with get_db() as db:
            _refund(db, user["id"], col)
        raise HTTPException(500, str(e))
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "general")
        except ValueError as e:
            if "resume_limit" in str(e):
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

async def _fetch_job_text(url: str) -> str:
    """Скачивает страницу вакансии и возвращает её текст (без HTML-тегов).

    Редиректы следуем вручную и проверяем каждый хоп через _assert_public_host —
    иначе SSRF-защиту можно обойти редиректом с внешнего URL на внутренний адрес.
    """
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Некорректная ссылка на вакансию")
    log.info("fetch-job start: %s", url)
    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=False, headers={"User-Agent": "Mozilla/5.0"}
        ) as h:
            current = url
            r = None
            for _ in range(5):
                parsed = urlparse(current)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise HTTPException(400, "Некорректная ссылка на вакансию")
                _assert_public_host(parsed.hostname)
                r = await h.get(current)
                if r.is_redirect and r.headers.get("location"):
                    current = urljoin(current, r.headers["location"])
                    continue
                break
            else:
                raise HTTPException(400, "Слишком много перенаправлений по ссылке")
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        log.info("fetch-job ok: %s -> %d chars (HTTP %s)", url, len(text), r.status_code)
        return text[:4000]
    except HTTPException:
        raise
    except Exception as e:
        log.warning("fetch-job failed: %s: %s", url, e)
        raise HTTPException(502, "Не удалось загрузить вакансию по ссылке — вставьте текст вручную")

@app.post("/api/fetch-job")
@rate("20/minute")
async def fetch_job(request: Request):
    body = await request.json()
    url  = body.get("url", "").strip()
    return {"text": await _fetch_job_text(url)}

# ── Payments ───────────────────────────────────────────────────────────────
def _drop_payment(idem: str) -> None:
    """Удаляет «висячую» строку платежа, если создать платёж в ЮKassa не удалось."""
    try:
        with get_db() as db:
            db.execute("DELETE FROM payments WHERE idem_key=? AND pay_id IS NULL", (idem,))
            db.commit()
    except Exception:
        log.exception("pay: не удалось удалить висячую строку платежа idem=%s", idem)


@app.post("/api/pay")
async def create_payment(req: PayReq, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")

    # ЮKassa ещё не подключена (нет ключей) — не дёргаем API впустую,
    # отдаём понятную ошибку и явно пишем причину в лог.
    if not YOKASSA_SHOP or not YOKASSA_SECRET:
        log.warning("pay: ЮKassa не настроена (YOKASSA_SHOP_ID/SECRET_KEY пусты), user=%s", user["id"])
        raise HTTPException(503, "Оплата временно недоступна. Попробуйте позже.")

    idem = str(uuid.uuid4())
    with get_db() as db:
        db.execute("INSERT INTO payments (user_id, idem_key) VALUES (?,?)", (user["id"], idem))
        db.commit()

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0)) as http:
            r = await http.post(
                "https://api.yookassa.ru/v3/payments",
                headers={"Idempotence-Key": idem},
                auth=(YOKASSA_SHOP, YOKASSA_SECRET),
                json={
                    "amount": {"value": PACK_PRICE, "currency": "RUB"},
                    "confirmation": {"type": "redirect",
                                     "return_url": f"{APP_URL}/?paid=1"},
                    "capture": True,
                    "description": f"{PAID_PACK} адаптаций резюме",
                    "metadata": {"user_id": user["id"], "idem": idem},
                },
            )
        data = r.json()
    except Exception:
        log.exception("pay: ошибка обращения к ЮKassa (user=%s, idem=%s)", user["id"], idem)
        _drop_payment(idem)
        raise HTTPException(502, "Не удалось создать платёж. Попробуйте позже.")

    if "id" not in data or "confirmation" not in data:
        log.error("pay: неожиданный ответ ЮKassa (status=%s): %s", r.status_code, str(data)[:500])
        _drop_payment(idem)
        raise HTTPException(502, "Платёжная система отклонила запрос. Попробуйте позже.")

    with get_db() as db:
        db.execute("UPDATE payments SET pay_id=? WHERE idem_key=?", (data["id"], idem))
        db.commit()
    log.info("pay: платёж создан user=%s pay_id=%s", user["id"], data["id"])
    return {"url": data["confirmation"]["confirmation_url"]}

@app.post("/api/pay/webhook")
async def payment_webhook(request: Request):
    body = await request.json()
    log.info("pay/webhook: event=%s payment_id=%s",
             body.get("event"), body.get("object", {}).get("id"))
    if body.get("event") == "payment.succeeded":
        obj     = body.get("object", {})
        pid     = obj.get("id")
        user_id = obj.get("metadata", {}).get("user_id")
        if pid and user_id:
            # ── КРИТИЧНО: верифицируем платёж напрямую в ЮKassa ─────────
            # Не доверяем только webhook — подтверждаем через API
            confirmed = False
            try:
                async with httpx.AsyncClient(timeout=10) as http:
                    r = await http.get(
                        f"https://api.yookassa.ru/v3/payments/{pid}",
                        auth=(YOKASSA_SHOP, YOKASSA_SECRET),
                    )
                    if r.status_code == 200 and r.json().get("status") == "succeeded":
                        confirmed = True
            except Exception:
                log.exception("pay/webhook: ошибка проверки платежа %s в ЮKassa", pid)
                confirmed = False  # не выдаём Pro при ошибке проверки

            if not confirmed:
                log.warning("pay/webhook: платёж %s не подтверждён, Pro не выдан", pid)
                return {"ok": False, "reason": "payment_not_confirmed"}

            with get_db() as db:
                already = db.execute(
                    "SELECT id FROM payments WHERE pay_id=? AND status='succeeded'", (pid,)
                ).fetchone()
                if not already:
                    db.execute("UPDATE payments SET status='succeeded' WHERE pay_id=?", (pid,))
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
                    db.commit()
                    log.info("pay/webhook: Pro выдан user=%s pay_id=%s до %s", user_id, pid, new_exp)
                else:
                    log.info("pay/webhook: повторный webhook для обработанного платежа %s", pid)
    return {"ok": True}

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

# Монтируем streamable-http app в КОНЦЕ файла: FastAPI-роуты, объявленные выше,
# имеют приоритет, а endpoint MCP оказывается ровно на /mcp
# (streamable_http_path по умолчанию "/mcp" внутри под-приложения).
app.mount("/", mcp_server.streamable_http_app())
