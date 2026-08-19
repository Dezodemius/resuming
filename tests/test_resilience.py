"""Устойчивость: security-заголовки, страницы ошибок, уборка БД, лимиты."""
import re
from pathlib import Path

import pytest

import main

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


# ── Security headers ─────────────────────────────────────────────────────
async def test_security_headers_on_pages(client):
    r = await client.get("/")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["Permissions-Policy"]
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp


async def test_security_headers_on_api_and_errors(client):
    """Заголовки нужны на всех ответах, включая ошибочные."""
    for path in ("/api/me", "/nope-404"):
        r = await client.get(path)
        assert r.headers.get("X-Content-Type-Options") == "nosniff", path
        assert r.headers.get("Content-Security-Policy"), path


async def test_hsts_only_for_https_app_url(client, monkeypatch):
    """HSTS на http-стенде бессмысленен и ломает локальную разработку."""
    r = await client.get("/")
    assert "Strict-Transport-Security" not in r.headers  # APP_URL в тестах http://test

    monkeypatch.setattr(main, "APP_URL", "https://xn--e1aedprev8fe.xn--p1ai")
    r = await client.get("/")
    assert r.headers["Strict-Transport-Security"].startswith("max-age=")


def test_csp_allows_every_external_subresource_used_in_templates():
    """CSP должен покрывать все внешние скрипты и стили из шаблонов.

    Иначе новый CDN в шаблоне тихо блокируется браузером — и ломается уже
    в проде, а не на ревью.
    """
    pattern = re.compile(
        r"<(?:script|link)\b[^>]*?(?:src|href)=[\"']https://([a-zA-Z0-9.-]+)", re.I
    )
    hosts = set()
    for tpl in TEMPLATES.glob("*.html"):
        hosts |= set(pattern.findall(tpl.read_text(encoding="utf-8")))

    assert hosts, "не нашли ни одного внешнего ресурса — регулярка сломалась"
    missing = [h for h in hosts if h not in main._CSP]
    assert not missing, f"CSP не разрешает используемые в шаблонах хосты: {missing}"


# ── Страницы ошибок ──────────────────────────────────────────────────────
async def test_unknown_url_returns_branded_page(client):
    """MCP смонтирован на «/» и раньше отвечал голым «Not Found» на любой
    неизвестный адрес."""
    r = await client.get("/такой-страницы-нет", headers={"accept": "text/html"})
    assert r.status_code == 404
    assert "text/html" in r.headers["content-type"]
    assert "Страница не найдена" in r.text
    assert r.text.strip() != "Not Found"


async def test_api_errors_stay_json(client):
    """Фронтенд читает detail из JSON — HTML-страница ошибки его сломает."""
    r = await client.get("/api/resumes", headers={"accept": "text/html"})
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["detail"]


async def test_unhandled_exception_hides_traceback(tmp_path, monkeypatch):
    """Наружу — короткое сообщение, а не внутренности приложения."""
    from httpx import AsyncClient, ASGITransport
    import config

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    main.init_db()

    async def _boom(request):
        raise RuntimeError("секрет из трейсбека")

    monkeypatch.setattr(main, "get_current_user", _boom)
    # raise_app_exceptions=False: ServerErrorMiddleware отдаёт ответ клиенту,
    # но пробрасывает исключение дальше — как это делает и настоящий uvicorn.
    transport = ASGITransport(app=main.app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/api/me")
    assert r.status_code == 500
    assert "секрет из трейсбека" not in r.text
    assert "Traceback" not in r.text
    assert r.json()["detail"]


# ── Уборка БД ────────────────────────────────────────────────────────────
def test_cleanup_removes_expired_and_keeps_live(db):
    db.executescript("""
        INSERT INTO sessions (id, user_id, expires_at)
             VALUES ('dead', 1, datetime('now','-1 day')),
                    ('live', 1, datetime('now','+10 days'));
        INSERT INTO magic_tokens (token, email, expires_at, used, created)
             VALUES ('old',   'a@t.ru', datetime('now','-3 days'), 0, datetime('now','-3 days')),
                    ('used',  'b@t.ru', datetime('now','+5 minutes'), 1, datetime('now','-2 days')),
                    ('fresh', 'c@t.ru', datetime('now','+5 minutes'), 0, datetime('now'));
        INSERT INTO anon_usage (anon_id, uses, created)
             VALUES ('ancient', 2, datetime('now','-400 days')),
                    ('recent',  1, datetime('now','-1 day'));
    """)
    db.commit()

    removed = main.cleanup_expired()

    assert removed["sessions"] == 1
    assert removed["magic_tokens"] == 2
    assert removed["anon_usage"] == 1

    alive = {r[0] for r in db.execute("SELECT id FROM sessions").fetchall()}
    assert alive == {"live"}
    tokens = {r[0] for r in db.execute("SELECT token FROM magic_tokens").fetchall()}
    assert tokens == {"fresh"}
    anon = {r[0] for r in db.execute("SELECT anon_id FROM anon_usage").fetchall()}
    assert anon == {"recent"}


def test_cleanup_keeps_recently_used_token(db):
    """Токен, использованный только что, ещё нужен: иначе повторный клик по
    ссылке из письма даёт «ссылка недействительна» вместо «уже использована»."""
    db.execute(
        "INSERT INTO magic_tokens (token, email, expires_at, used, created)"
        " VALUES ('just-used','d@t.ru',datetime('now','+5 minutes'),1,datetime('now'))"
    )
    db.commit()
    main.cleanup_expired()
    assert db.execute("SELECT COUNT(*) c FROM magic_tokens").fetchone()["c"] == 1


# ── Глобальный лимит запросов ────────────────────────────────────────────
@pytest.mark.skipif(not main._RATE_LIMIT, reason="slowapi не установлен")
async def test_default_rate_limit_is_a_backstop(client):
    """Лимит должен работать на маршрутах без явного @rate — раньше без
    SlowAPIMiddleware default_limits не применялись вообще."""
    main.limiter.enabled = True
    try:
        if hasattr(main.limiter, "reset"):
            main.limiter.reset()
        headers = {"cf-connecting-ip": "203.0.113.7"}
        statuses = set()
        for _ in range(260):
            statuses.add((await client.get("/healthz", headers=headers)).status_code)
            if 429 in statuses:
                break
        assert 429 in statuses, "глобальный лимит не сработал"

        # Другой посетитель не должен пострадать от чужого всплеска
        other = await client.get("/healthz", headers={"cf-connecting-ip": "198.51.100.9"})
        assert other.status_code == 200
    finally:
        main.limiter.enabled = False
        if hasattr(main.limiter, "reset"):
            main.limiter.reset()


def test_rate_limit_key_prefers_real_client_ip():
    """За Cloudflare + nginx peer-адрес всегда один и тот же — ключом должен
    быть IP посетителя, иначе лимит становится общим на весь сайт."""
    class _Req:
        def __init__(self, headers, client_host="10.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": client_host})()

    assert main._client_key(_Req({"cf-connecting-ip": "1.2.3.4"})) == "1.2.3.4"
    assert main._client_key(_Req({"x-forwarded-for": "5.6.7.8, 10.0.0.1"})) == "5.6.7.8"
    assert main._client_key(_Req({})) == "10.0.0.1"
