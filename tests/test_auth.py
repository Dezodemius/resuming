"""Тесты авторизации: OAuth-провайдеры, magic-ссылка, нормализация адреса.

Вход через Telegram убран (бота не будет) — вместе с ним ушли и его тесты.
"""
import pytest

import main


# ── _auth_ctx ─────────────────────────────────────────────────────────
# Что именно включено, решается один раз в config (там же и предупреждения о
# недонастроенных провайдерах). _auth_ctx обязан лишь честно это отразить:
# кнопка в шаблоне и проверка в ручке должны решать одинаково, иначе получится
# либо кнопка в никуда, либо рабочая ручка без кнопки.
def test_auth_ctx_reflects_enabled_flags(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", True)
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is True
    assert ctx["vk_enabled"] is False
    assert ctx["mailru_enabled"] is True


def test_auth_ctx_all_providers_off_by_default(monkeypatch):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", False)
    ctx = main._auth_ctx(None)
    assert ctx["yandex_enabled"] is False
    assert ctx["vk_enabled"] is False
    assert ctx["mailru_enabled"] is False


def test_auth_ctx_passes_through_user():
    sentinel_user = {"id": 42}
    assert main._auth_ctx(sentinel_user)["user"] is sentinel_user


# ── Yandex OAuth tests ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_yandex_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "")
    r = await client.get("/auth/yandex")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_yandex_oauth_disabled_returns_503_even_with_client_id(monkeypatch, client):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    r = await client.get("/auth/yandex")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_yandex_start_redirects_to_yandex_domain(monkeypatch, client):
    monkeypatch.setattr(main, "YANDEX_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-yandex-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/yandex", follow_redirects=False)
    assert r.status_code == 302
    assert "oauth.yandex.ru" in r.headers["location"]
    assert "client_id=test-yandex-id" in r.headers["location"]


# ── VK OAuth tests ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_vk_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "")
    r = await client.get("/auth/vk")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_vk_oauth_disabled_returns_503_even_with_client_id(monkeypatch, client):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_vk_start_redirects_to_vk_domain(monkeypatch, client):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/vk", follow_redirects=False)
    assert r.status_code == 302
    assert "id.vk.com" in r.headers["location"]
    assert "client_id=test-vk-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_vk_start_sets_state_cookie(monkeypatch, client):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/vk", follow_redirects=False)
    assert "vk_state" in r.cookies
    assert "vk_verifier" in r.cookies


@pytest.mark.asyncio
async def test_vk_callback_state_mismatch_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk/callback?code=test&state=wrong&device_id=test", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=vk" in r.headers["location"]


@pytest.mark.asyncio
async def test_vk_callback_no_code_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    r = await client.get("/auth/vk/callback?state=&device_id=test", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=vk" in r.headers["location"]


# ── Mail.ru OAuth tests ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_mailru_no_client_id_returns_503(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "")
    r = await client.get("/auth/mailru")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_mailru_oauth_disabled_returns_503_even_with_client_id(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", False)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru")
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_mailru_start_redirects_to_mailru_domain(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/mailru", follow_redirects=False)
    assert r.status_code == 302
    assert "oauth.mail.ru" in r.headers["location"]
    assert "client_id=test-mr-id" in r.headers["location"]


@pytest.mark.asyncio
async def test_mailru_start_sets_state_cookie(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_LOGIN_ENABLED", True)
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    r = await client.get("/auth/mailru", follow_redirects=False)
    assert "mr_state" in r.cookies


@pytest.mark.asyncio
async def test_mailru_callback_state_mismatch_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru/callback?code=test&state=wrong", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=mailru" in r.headers["location"]


@pytest.mark.asyncio
async def test_mailru_callback_no_code_redirects_to_error(monkeypatch, client):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    r = await client.get("/auth/mailru/callback?state=", follow_redirects=False)
    assert r.status_code == 303
    assert "auth_error=mailru" in r.headers["location"]


# ── APP_URL: punycode-нормализация кириллического домена ────────────────
def test_idna_url_cyrillic_host_converted():
    from config import _idna_url
    assert _idna_url("https://резюмирую.рф") == "https://xn--e1aedprev8fe.xn--p1ai"


def test_idna_url_preserves_path_and_port():
    from config import _idna_url
    assert _idna_url("https://резюмирую.рф:8443/auth") == "https://xn--e1aedprev8fe.xn--p1ai:8443/auth"


def test_idna_url_ascii_untouched():
    from config import _idna_url
    assert _idna_url("http://localhost:8000") == "http://localhost:8000"
    assert _idna_url("https://example.com") == "https://example.com"


# ── Регистр адреса не создаёт второй аккаунт ────────────────────────────────
# Один и тот же человек приходит то из формы («Ivan@Ya.ru», как набрал), то из
# OAuth («ivan@ya.ru», как отдал провайдер). Раньше это были два разных
# аккаунта: во втором не было ни резюме, ни оплаченного Pro.

async def _login_by_email(client, email, token):
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            (token, email),
        )
        db.commit()
    await client.get(f"/auth/email/verify?token={token}", follow_redirects=False)
    return (await client.get("/api/me")).json()


@pytest.mark.asyncio
async def test_same_email_different_case_is_one_account(client):
    main.init_db()
    first = await _login_by_email(client, "Ivan@Ya.ru", "tok-upper")
    second = await _login_by_email(client, "ivan@ya.ru", "tok-lower")

    assert first["authenticated"] and second["authenticated"]
    assert first["id"] == second["id"], "разный регистр не должен разводить аккаунты"
    with main.get_db() as db:
        rows = db.execute("SELECT id, email FROM users").fetchall()
    assert len(rows) == 1, f"аккаунтов должно быть один, а не {len(rows)}"
    assert rows[0]["email"] == "ivan@ya.ru", "адрес хранится в нижнем регистре"


@pytest.mark.asyncio
async def test_email_with_spaces_is_trimmed(client):
    """Пробелы по краям приезжают из форм и мобильных клавиатур постоянно."""
    main.init_db()
    me = await _login_by_email(client, "  Petr@Ya.ru  ", "tok-spaces")
    assert me["authenticated"]
    with main.get_db() as db:
        assert db.execute("SELECT email FROM users").fetchone()["email"] == "petr@ya.ru"


def test_normalize_email():
    assert main._normalize_email("Ivan@Ya.RU") == "ivan@ya.ru"
    # Перевод строки и табуляция приезжают из копипасты не реже пробелов.
    assert main._normalize_email(" ivan@ya.ru " + chr(10)) == "ivan@ya.ru"
    assert main._normalize_email(chr(9) + "ivan@ya.ru") == "ivan@ya.ru"
    assert main._normalize_email("ivan@ya.ru") == "ivan@ya.ru"


@pytest.mark.asyncio
async def test_default_display_name_is_local_part(client):
    """Имя по умолчанию — часть адреса до собаки: оно показывается в шапке и в
    админке, и подставлять туда весь адрес значит светить почту пользователя."""
    main.init_db()
    me = await _login_by_email(client, "Ivan.Petrov@ya.ru", "tok-name")
    assert me["name"] == "ivan.petrov"


# ── Отчёт о способах входа при старте ───────────────────────────────────────
# Вся конфигурация входа на проде оказалась пустой, и заметить это можно было
# только чтением шаблонов: наружу сайт выглядел как «умеет только почту».
# Теперь состав способов пишется в лог, а недонастроенный провайдер даёт
# предупреждение — молчать о нём хуже всего.

def _report(monkeypatch, **overrides):
    import config
    defaults = dict(
        OAUTH_LOGIN_ENABLED=False,
        YANDEX_CLIENT_ID="", YANDEX_CLIENT_SECRET="", YANDEX_LOGIN_ENABLED=False,
        VK_CLIENT_ID="", VK_LOGIN_ENABLED=False,
        MAILRU_CLIENT_ID="", MAILRU_CLIENT_SECRET="", MAILRU_LOGIN_ENABLED=False,
    )
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(config, name, value)
    return config._login_methods_report()


def test_login_report_email_always_available(monkeypatch):
    active, notes = _report(monkeypatch)
    assert active == ["email"]
    assert notes == []


def test_login_report_lists_every_enabled_method(monkeypatch):
    active, notes = _report(
        monkeypatch,
        OAUTH_LOGIN_ENABLED=True,
        YANDEX_CLIENT_ID="y", YANDEX_CLIENT_SECRET="s", YANDEX_LOGIN_ENABLED=True,
        VK_CLIENT_ID="v", VK_LOGIN_ENABLED=True,
        MAILRU_CLIENT_ID="m", MAILRU_CLIENT_SECRET="s", MAILRU_LOGIN_ENABLED=True,
    )
    assert active == ["email", "yandex", "vk", "mailru"]
    assert notes == []


def test_login_report_warns_about_hidden_configured_providers(monkeypatch):
    """Ровно та ситуация, ради которой это писалось: ключи есть, рубильник
    выключен, кнопок нет и никто не понимает почему."""
    _, notes = _report(monkeypatch, YANDEX_CLIENT_ID="y", MAILRU_CLIENT_ID="m")
    assert any("OAUTH_LOGIN_ENABLED=0" in n for n in notes)
    assert any("Яндекс" in n and "Mail.ru" in n for n in notes)


def test_login_report_warns_about_missing_secrets(monkeypatch):
    """Client ID без секрета — кнопка была бы, а обмен кода на токен падал."""
    _, notes = _report(monkeypatch, OAUTH_LOGIN_ENABLED=True, YANDEX_CLIENT_ID="y")
    assert any("YANDEX_CLIENT_SECRET" in n for n in notes)

    _, notes = _report(monkeypatch, OAUTH_LOGIN_ENABLED=True, MAILRU_CLIENT_ID="m")
    assert any("MAILRU_CLIENT_SECRET" in n for n in notes)


def test_login_report_vk_needs_no_secret(monkeypatch):
    """VK ID работает по PKCE — требовать у него секрет было бы неверно."""
    active, notes = _report(
        monkeypatch, OAUTH_LOGIN_ENABLED=True, VK_CLIENT_ID="v", VK_LOGIN_ENABLED=True
    )
    assert "vk" in active
    assert notes == []
