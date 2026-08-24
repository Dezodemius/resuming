"""Ограничение доступа к админке по адресам (ADMIN_IPS)."""
import ipaddress

import pytest
import pytest_asyncio

import config
import main


def _nets(*items):
    return [ipaddress.ip_network(i, strict=False) for i in items]


@pytest_asyncio.fixture
async def admin_session(client, db, monkeypatch):
    """Сессия пользователя, чья почта уже в ADMIN_EMAILS."""
    with db as c:
        c.execute(
            "INSERT INTO users (email, display_name) VALUES (?,?)",
            ("test@example.com", "Admin")
        )
        c.commit()
        uid = c.execute(
            "SELECT id FROM users WHERE email=?", ("test@example.com",)
        ).fetchone()["id"]
        sid = main._create_session(c, uid)
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["test@example.com"])
    return sid


# ── Разбор переменной окружения ────────────────────────────────────────────

def test_parse_empty_gives_empty_list():
    assert config._parse_admin_ips("") == []
    assert config._parse_admin_ips("  ,  ") == []


def test_parse_address_and_cidr():
    nets = config._parse_admin_ips(" 203.0.113.7 , 10.0.0.0/8 ")
    assert [str(n) for n in nets] == ["203.0.113.7/32", "10.0.0.0/8"]


def test_parse_keeps_cidr_with_host_bits():
    """«203.0.113.7/24» — обычная человеческая запись, ронять её на strict нельзя."""
    assert [str(n) for n in config._parse_admin_ips("203.0.113.7/24")] == ["203.0.113.0/24"]


def test_parse_continues_past_empty_entry():
    """«,203.0.113.7» — пустая запись пропускается, а не обрывает разбор."""
    assert [str(n) for n in config._parse_admin_ips(",203.0.113.7")] == ["203.0.113.7/32"]


def test_parse_skips_garbage_but_keeps_the_rest(caplog):
    with caplog.at_level("WARNING"):
        nets = config._parse_admin_ips("не-адрес,203.0.113.7")
    assert [str(n) for n in nets] == ["203.0.113.7/32"]
    # Молчаливо съеденная опечатка = незаметно выключенный фильтр.
    assert any("ADMIN_IPS" in r.getMessage() and "не-адрес" in r.getMessage()
               for r in caplog.records)


# ── Проверка адреса на входе в админку ─────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_list_allows_any_address(client, admin_session, monkeypatch):
    """Пустой ADMIN_IPS = фильтр выключен: иначе стенд остался бы без админки."""
    monkeypatch.setattr(main, "ADMIN_IPS", [])
    resp = await client.get(
        "/admin",
        headers={"X-Real-IP": "198.51.100.44"},
        cookies={"session_id": admin_session},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_listed_address_passes(client, admin_session, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("203.0.113.7"))
    resp = await client.get(
        "/admin",
        headers={"X-Real-IP": "203.0.113.7"},
        cookies={"session_id": admin_session},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unlisted_address_gets_404_despite_admin_email(client, admin_session, monkeypatch, caplog):
    """Адрес проверяется до почты, и отказ маскируется под 404."""
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("203.0.113.7"))
    with caplog.at_level("WARNING"):
        resp = await client.get(
            "/admin",
            headers={"X-Real-IP": "198.51.100.44"},
            cookies={"session_id": admin_session},
        )
    assert resp.status_code == 404
    # Отказ должен быть виден в логе с адресом — иначе перебор не заметить.
    assert any("198.51.100.44" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_address_inside_listed_network(client, admin_session, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("203.0.113.7/24"))
    resp = await client.get(
        "/admin",
        headers={"X-Real-IP": "203.0.113.200"},
        cookies={"session_id": admin_session},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_match_on_any_entry_not_all(client, admin_session, monkeypatch):
    """Совпадения с одной записью достаточно — список это «или», а не «и»."""
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("10.0.0.0/8", "203.0.113.7"))
    resp = await client.get(
        "/admin",
        headers={"X-Real-IP": "203.0.113.7"},
        cookies={"session_id": admin_session},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_unparsable_address_is_denied(client, admin_session, monkeypatch):
    """Мусор в X-Real-IP не должен трактоваться как «проверить не смогли — пропустим»."""
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("203.0.113.7"))
    resp = await client.get(
        "/admin",
        headers={"X-Real-IP": "not-an-ip"},
        cookies={"session_id": admin_session},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_peer_address_used_without_proxy_header(client, admin_session, monkeypatch):
    """Запрос прямо в сокет (SSH-туннель): X-Real-IP нет, сверяется peer."""
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("127.0.0.0/8"))
    resp = await client.get("/admin", cookies={"session_id": admin_session})
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_peer_address_denied_when_not_listed(client, admin_session, monkeypatch):
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("203.0.113.7"))
    resp = await client.get("/admin", cookies={"session_id": admin_session})
    assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/admin/stats", "/api/admin/promo"])
async def test_admin_api_is_guarded_too(client, admin_session, monkeypatch, path):
    """Фильтр стоит не только на странице: иначе ручки остались бы открытыми."""
    monkeypatch.setattr(main, "ADMIN_IPS", _nets("203.0.113.7"))
    resp = await client.get(
        path,
        headers={"X-Real-IP": "198.51.100.44"},
        cookies={"session_id": admin_session},
    )
    assert resp.status_code == 404


# ── Проверка почты остаётся на месте ───────────────────────────────────────

@pytest.mark.asyncio
async def test_anonymous_gets_404(client, db, monkeypatch):
    """Без сессии — 404, а не 500: user приходит как None."""
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["test@example.com"])
    monkeypatch.setattr(main, "ADMIN_IPS", [])
    resp = await client.get("/admin")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_user_without_email_is_not_admin(client, db, monkeypatch):
    """users.email — nullable, и такой пользователь не должен попадать в админку."""
    monkeypatch.setattr(main, "ADMIN_EMAILS", ["xxxx"])
    monkeypatch.setattr(main, "ADMIN_IPS", [])
    with db as c:
        c.execute("INSERT INTO users (email, display_name) VALUES (NULL, ?)", ("Ghost",))
        c.commit()
        uid = c.execute("SELECT id FROM users WHERE display_name=?", ("Ghost",)).fetchone()["id"]
        sid = main._create_session(c, uid)
    resp = await client.get("/admin", cookies={"session_id": sid})
    assert resp.status_code == 404
