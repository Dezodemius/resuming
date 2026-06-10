import asyncio
import os, json, uuid, sqlite3, hashlib, hmac, time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
import httpx
from dotenv import load_dotenv

try:
    from slowapi import Limiter, _rate_limit_exceeded_handler
    from slowapi.util import get_remote_address
    from slowapi.errors import RateLimitExceeded
    _RATE_LIMIT = True
except ImportError:
    _RATE_LIMIT = False

load_dotenv()

# База данных: в Docker используем volume /app/data, локально — текущая папка
_data_dir = os.getenv("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
os.makedirs(_data_dir, exist_ok=True)
DB_PATH = os.path.join(_data_dir, "resume.db")

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────
OLLAMA_URL          = os.getenv("OLLAMA_URL", "http://localhost:11434")
MODEL               = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
YOKASSA_SHOP        = os.getenv("YOKASSA_SHOP_ID", "")
YOKASSA_SECRET      = os.getenv("YOKASSA_SECRET_KEY", "")
APP_URL             = os.getenv("APP_URL", "http://localhost:8000")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_BOT_NAME   = os.getenv("TELEGRAM_BOT_NAME", "")
SMTP_HOST           = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT           = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER           = os.getenv("SMTP_USER", "")
SMTP_PASS           = os.getenv("SMTP_PASS", "")
SMTP_FROM           = os.getenv("SMTP_FROM", SMTP_USER)

FREE_USES         = 3
FREE_RESUMES      = 5
PRO_PRICE         = "399.00"
PRO_DAYS          = 30
ANON_LIMIT_CONST  = 2
PAID_PACK         = 20
PACK_PRICE        = PRO_PRICE
SESSION_DAYS      = 30
MAGIC_MINUTES     = 15
AI_CONCURRENCY    = 2   # max одновременных вызовов Ollama

# Секрет для подписи anon-cookie. ОБЯЗАТЕЛЬНО задайте в .env!
SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-" + os.urandom(16).hex())

# Семафор: не более AI_CONCURRENCY параллельных генераций.
# Создаём здесь — asyncio инициализирует при первом await.
_ai_sem: asyncio.Semaphore | None = None

def get_ai_sem() -> asyncio.Semaphore:
    global _ai_sem
    if _ai_sem is None:
        _ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
    return _ai_sem

tpl = Jinja2Templates(directory="templates")

# ── Database ────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
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
        """)

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app = FastAPI(title="Резюмирую.рф", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── CORS ── разрешаем только собственный домен ───────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[APP_URL, "http://localhost:8000"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)

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
    exp = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()
    db.execute("INSERT INTO sessions (id, user_id, expires_at) VALUES (?,?,?)", (sid, user_id, exp))
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
    """Get user from session, or create/fetch by email (backward compat)."""
    user = await get_current_user(request)
    if user:
        return user
    if body_email:
        with get_db() as db:
            return _upsert_user_by_email(db, body_email)
    return None

# ── Email magic link ──────────────────────────────────────────────────────
async def _send_magic_email(to_email: str, token: str) -> bool:
    if not SMTP_USER:
        print(f"[DEV] Magic link: {APP_URL}/auth/email/verify?token={token}")
        return True
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
        return True
    except Exception as e:
        print(f"Email error: {e}")
        return False

# ── Pydantic models ────────────────────────────────────────────────────────
class TgAuthData(BaseModel):
    id: int
    first_name: Optional[str] = ""
    last_name: Optional[str] = ""
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str

class EmailReq(BaseModel):
    email: str

class ProfileData(BaseModel):
    email:      Optional[str] = None
    name:       str
    phone:      str
    city:       str
    linkedin:   str = ""
    experience: List[Dict[str, Any]]
    education:  List[Dict[str, Any]]
    skills:     str
    languages:  str

class MatchReq(BaseModel):
    email:       Optional[str] = None
    job_text:    str
    company:     str = ""
    job_url:     str = ""
    extra_hint:  str = ""

class GenerateFromProfileReq(BaseModel):
    email:       Optional[str] = None
    target_role: str = ""
    hint:        str = ""

class GenerateReq(BaseModel):
    email:      Optional[str] = None
    name:       str
    phone:      str
    city:       str
    linkedin:   str = ""
    target:     str
    hint:       str = ""
    experience: List[Dict[str, Any]]
    education:  List[Dict[str, Any]]
    skills:     str
    languages:  str

class PayReq(BaseModel):
    email: Optional[str] = None

class ResumeStatusReq(BaseModel):
    status: str

class AnonymousPreviewReq(BaseModel):
    """Генерация без аккаунта — профиль передаётся инлайн, ничего не сохраняется."""
    kind:        str        # "match" | "general"
    profile:     dict
    job_text:    str = ""
    target_role: str = ""
    hint:        str = ""

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
            _match_prompt(req.profile, req.job_text, req.hint)
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

# ── Static pages ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = await get_current_user(request)
    return tpl.TemplateResponse("index.html", {
        "request": request,
        "telegram_bot_name": TELEGRAM_BOT_NAME,
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
    return tpl.TemplateResponse("resume_edit.html", {
        "request":    request,
        "resume_id":  resume_id,
        "telegram_bot_name": TELEGRAM_BOT_NAME,
    })

# ── AI section improvement ────────────────────────────────────────────────
class ImproveReq(BaseModel):
    kind:    str        # "summary" | "bullets" | "skills"
    text:    str
    context: str = ""

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
    return tpl.TemplateResponse("resumes.html", {
        "request": request,
        "telegram_bot_name": TELEGRAM_BOT_NAME,
        "user": user,
    })

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user = await get_current_user(request)
    if not user:
        return RedirectResponse(url="/?auth_required=1", status_code=303)
    return tpl.TemplateResponse("settings.html", {
        "request": request,
        "telegram_bot_name": TELEGRAM_BOT_NAME,
    })

# ── Public / legal pages (no auth required) ───────────────────────────────
@app.get("/pricing", response_class=HTMLResponse)
async def pricing_page(request: Request):
    return tpl.TemplateResponse("pricing.html", {"request": request})

@app.get("/offer", response_class=HTMLResponse)
async def offer_page(request: Request):
    return tpl.TemplateResponse("offer.html", {"request": request})

@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page(request: Request):
    return tpl.TemplateResponse("privacy.html", {"request": request})

@app.get("/contacts", response_class=HTMLResponse)
async def contacts_page(request: Request):
    return tpl.TemplateResponse("contacts.html", {"request": request})

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
    _set_session_cookie(response, sid)
    return {"ok": True, "user": {"name": u["display_name"], "photo": u["tg_photo"], "free_left": u["free_left"]}}

@app.post("/auth/email/request")
async def auth_email_request(req: EmailReq):
    token = str(uuid.uuid4())
    exp   = (datetime.now() + timedelta(minutes=MAGIC_MINUTES)).isoformat()
    with get_db() as db:
        db.execute(
            "INSERT OR REPLACE INTO magic_tokens (token, email, expires_at) VALUES (?,?,?)",
            (token, req.email, exp)
        )
        db.commit()
    ok = await _send_magic_email(req.email, token)
    if not ok:
        raise HTTPException(500, "Не удалось отправить письмо. Проверьте настройки SMTP.")
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
    return True, col, upd["free_left"] + upd["paid_left"]

def _refund(db, user_id: int, col: str):
    if col == "pro":
        return  # Pro-пользователям не нужен возврат
    db.execute(f"UPDATE users SET {col}={col}+1 WHERE id=?", (user_id,))
    db.commit()

# ── AI call ────────────────────────────────────────────────────────────────
async def call_ai(prompt: str) -> str:
    """
    Вызов Ollama с:
    - Семафором AI_CONCURRENCY (не более N одновременных запросов)
    - Таймаутом 120 сек (connect 5 сек)
    - Безопасными сообщениями об ошибках (без деталей внутренностей)
    """
    async with get_ai_sem():
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
                return r.json()["choices"][0]["message"]["content"]
        except httpx.ConnectError:
            raise HTTPException(503, "Сервис генерации недоступен. Проверьте Ollama.")
        except httpx.TimeoutException:
            raise HTTPException(504, "Генерация заняла слишком долго. Попробуйте ещё раз.")
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"Ошибка модели: {e.response.status_code}")
        except Exception:
            raise HTTPException(500, "Ошибка генерации. Попробуйте позже.")

def _parse_ai(raw: str) -> dict:
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    return json.loads(raw)

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
        rows = db.execute(
            "SELECT id, company_name, kind, status, created, updated FROM resumes"
            " WHERE user_id=? ORDER BY updated DESC",
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
    if not req.job_text.strip():
        raise HTTPException(400, "Вставьте текст вакансии")
    with get_db() as db:
        p = db.execute("SELECT data FROM profiles WHERE user_id=?", (user["id"],)).fetchone()
    if not p:
        raise HTTPException(404, "Сначала сохраните профиль")
    with get_db() as db:
        ok, col, uses_left = _deduct(db, user["id"])
        if not ok:
            return JSONResponse(status_code=402, content={"error": "no_uses"})
    try:
        raw = await call_ai(_match_prompt(json.loads(p["data"]), req.job_text, req.extra_hint))
        resume = _parse_ai(raw)
    except Exception as e:
        with get_db() as db:
            _refund(db, user["id"], col)
        raise HTTPException(500, str(e))
    with get_db() as db:
        try:
            rid = _save_resume(db, user["id"], resume, "matched", req.company, req.job_url, req.job_text)
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
@app.post("/api/fetch-job")
async def fetch_job(request: Request):
    body = await request.json()
    url  = body.get("url", "").strip()
    if not url.startswith("http"):
        raise HTTPException(400, "Нужен URL")
    import re
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"}) as h:
            r = await h.get(url)
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"\s+", " ", text).strip()
        return {"text": text[:4000]}
    except Exception as e:
        raise HTTPException(502, str(e))

# ── Payments ───────────────────────────────────────────────────────────────
@app.post("/api/pay")
async def create_payment(req: PayReq, request: Request):
    user = await _resolve_user(request, req.email)
    if not user:
        raise HTTPException(401, "Войдите в аккаунт")
    idem = str(uuid.uuid4())
    with get_db() as db:
        db.execute("INSERT INTO payments (user_id, idem_key) VALUES (?,?)", (user["id"], idem))
        db.commit()
    async with httpx.AsyncClient() as http:
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
    if "id" not in data:
        raise HTTPException(502, "ЮKassa недоступна")
    with get_db() as db:
        db.execute("UPDATE payments SET pay_id=? WHERE idem_key=?", (data["id"], idem))
        db.commit()
    return {"url": data["confirmation"]["confirmation_url"]}

@app.post("/api/pay/webhook")
async def payment_webhook(request: Request):
    body = await request.json()
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
                confirmed = False  # не выдаём Pro при ошибке проверки

            if not confirmed:
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
    return {"ok": True}

# ── Prompts ────────────────────────────────────────────────────────────────
def _match_prompt(profile: dict, job_text: str, extra: str = "") -> str:
    exp = "\n".join(f"  - {e.get('role','')} в {e.get('company','')} ({e.get('period','')}): {e.get('desc','')}" for e in profile.get("experience", [])) or "  не указан"
    edu = "\n".join(f"  - {e.get('degree','')} — {e.get('institution','')} ({e.get('year','')})" for e in profile.get("education", [])) or "  не указано"
    return f"""Ты — ведущий HR-консультант. Адаптируй резюме под конкретную вакансию.

ПРОФИЛЬ: {profile.get('name','')} | {profile.get('city','')} | {profile.get('phone','')}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {profile.get('skills','')} | Языки: {profile.get('languages','')}
Пожелания: {extra}

ВАКАНСИЯ:\n{job_text[:3500]}

ЗАДАЧИ: извлеки ключевые требования, выбери релевантный опыт, вплети ключевые слова ATS, напиши точный summary.
НЕ выдумывай навыков которых нет в профиле.

JSON ТОЛЬКО (без markdown):
{{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."],"ats_keywords":["..."]}}"""

def _general_prompt(profile: dict, target_role: str = "", hint: str = "") -> str:
    exp = "\n".join(f"  - {e.get('role','')} в {e.get('company','')} ({e.get('period','')}): {e.get('desc','')}" for e in profile.get("experience", [])) or "  не указан"
    edu = "\n".join(f"  - {e.get('degree','')} — {e.get('institution','')} ({e.get('year','')})" for e in profile.get("education", [])) or "  не указано"
    role_line = f"Желаемая должность: {target_role}" if target_role else "Желаемая должность: определи сам по опыту"
    return f"""Ты — ведущий HR-консультант. Создай универсальное профессиональное резюме.

ПРОФИЛЬ: {profile.get('name','')} | {profile.get('city','')}
{role_line} | Пожелания: {hint}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {profile.get('skills','')} | Языки: {profile.get('languages','')}

Включи весь опыт, 3–5 bullet-points с достижениями, широкий summary, сгруппируй навыки.

JSON ТОЛЬКО: {{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."]}}"""

def _generate_prompt(r: GenerateReq) -> str:
    exp = "\n".join(f"  - {e.get('role','')} в {e.get('company','')} ({e.get('period','')}): {e.get('desc','')}" for e in r.experience) or "  не указан"
    edu = "\n".join(f"  - {e.get('degree','')} — {e.get('institution','')} ({e.get('year','')})" for e in r.education) or "  не указано"
    return f"""Ты — HR-консультант. Создай резюме.
Имя: {r.name} | Должность: {r.target} | Пожелания: {r.hint}
Опыт:\n{exp}\nОбразование:\n{edu}
Навыки: {r.skills} | Языки: {r.languages}
JSON ТОЛЬКО: {{"name":"...","contact":{{"phone":"...","email":"...","city":"...","linkedin":"..."}},"target_role":"...","summary":"...","experience":[{{"company":"...","role":"...","period":"...","location":"...","bullets":["..."]}}],"education":[{{"institution":"...","degree":"...","year":"..."}}],"skills":{{"Категория":["навык"]}},"languages":["..."]}}"""
