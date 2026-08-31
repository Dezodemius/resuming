"""Привязка нескольких способов входа (Яндекс/VK/Mail.ru) к одному аккаунту.

Провайдер не гарантирует один и тот же email на каждом входе — VK ID, например,
может отдать адрес самого VK-аккаунта, а не тот, что человек считает основным.
`oauth_identities` запоминает провайдера один раз по его стабильному id, и
дальше вход находит аккаунт по этой привязке, а не пересчитывает его по email
заново. См. _resolve_oauth_user в main.py.
"""
import pytest

import main


def _mock_oauth_http(monkeypatch, responses):
    """Подменяет httpx.AsyncClient на конвейер из заготовленных JSON-ответов.

    Каждый callback в main.py делает ровно два сетевых вызова через один
    `async with httpx.AsyncClient(...) as http` — обмен кода на токен, потом
    userinfo. `responses` передаются в этом порядке независимо от того,
    вызван ли на сессии post() или get().
    """
    class _Resp:
        def __init__(self, data):
            self._data = data
            self.text = str(data)

        def json(self):
            return self._data

    queue = [_Resp(r) for r in responses]

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **kw):
            return queue.pop(0)

        async def get(self, *a, **kw):
            return queue.pop(0)

    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeClient)


async def _create_logged_in_user(client, db, email):
    """Заводит пользователя и возвращает его session_id, не трогая куки client."""
    with db as c:
        c.execute("INSERT INTO users (email) VALUES (?)", (email,))
        c.commit()
        uid = c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]
        sid = main._create_session(c, uid)
    return uid, sid


# ── VK: полный цикл входа/привязки ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_vk_callback_creates_identity_on_first_login(monkeypatch, client, db):
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 555, "email": "vk-returned@example.com", "first_name": "И"}},
    ])

    r = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/new?login=success"
    row = db.execute(
        "SELECT user_id FROM oauth_identities WHERE provider='vk' AND provider_uid='555'"
    ).fetchone()
    assert row is not None
    user = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    assert user["email"] == "vk-returned@example.com"


@pytest.mark.asyncio
async def test_vk_callback_second_login_ignores_changed_email(monkeypatch, client, db):
    """Тот случай, который сегодня сломался: VK на втором входе отдаёт другой
    email — вход всё равно должен попасть в тот же аккаунт, что и в первый раз."""
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")

    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 555, "email": "first@example.com"}},
    ])
    await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)
    first_user_id = db.execute(
        "SELECT user_id FROM oauth_identities WHERE provider='vk' AND provider_uid='555'"
    ).fetchone()["user_id"]

    client.cookies.delete("session_id")
    client.cookies.set("vk_state", "s2")
    client.cookies.set("vk_verifier", "v2")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 555, "email": "second-different@example.com"}},
    ])
    r2 = await client.get("/auth/vk/callback?code=abc&state=s2&device_id=dev", follow_redirects=False)

    assert r2.status_code == 303
    assert r2.headers["location"] == "/new?login=success"
    sid = r2.cookies["session_id"]
    logged_in_user_id = db.execute(
        "SELECT user_id FROM sessions WHERE id=?", (sid,)
    ).fetchone()["user_id"]
    assert logged_in_user_id == first_user_id, "второй вход должен попасть в тот же аккаунт"
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1, \
        "второй email не должен был завести отдельного пользователя"
    # email аккаунта не переписывается вслед за новым ответом провайдера —
    # иначе первый способ входа (например, magic-link) перестал бы работать.
    user = db.execute("SELECT email FROM users WHERE id=?", (first_user_id,)).fetchone()
    assert user["email"] == "first@example.com"


@pytest.mark.asyncio
async def test_vk_callback_with_active_session_links_to_current_user(monkeypatch, client, db):
    """Уже залогиненный пользователь жмёт «Войти с VK» — это привязка, а не
    новый аккаунт, и после нее должен уйти на /settings, а не на /new."""
    uid, sid = await _create_logged_in_user(client, db, "ivan@example.com")
    client.cookies.set("session_id", sid)

    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 777, "email": "vk-owns-this@example.com"}},
    ])

    r = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/settings?linked=vk"
    row = db.execute(
        "SELECT user_id FROM oauth_identities WHERE provider='vk' AND provider_uid='777'"
    ).fetchone()
    assert row["user_id"] == uid
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1, \
        "привязка не должна была завести второго пользователя"
    user = db.execute("SELECT email FROM users WHERE id=?", (uid,)).fetchone()
    assert user["email"] == "ivan@example.com", "email аккаунта не подменяется привязкой"


@pytest.mark.asyncio
async def test_vk_callback_identity_of_other_user_switches_session(monkeypatch, client, db):
    """Identity уже привязана к пользователю A; вход с VK-аккаунтом,
    привязанным к A, должен залогинить в A — даже если сейчас в браузере
    сессия пользователя B (так же ведёт себя «Войти через Google» в любом
    стороннем сервисе: совпадение по идентичности провайдера первично)."""
    uid_a, _sid_a = await _create_logged_in_user(client, db, "user-a@example.com")
    with db as c:
        c.execute(
            "INSERT INTO oauth_identities (provider, provider_uid, user_id) VALUES ('vk', '999', ?)",
            (uid_a,)
        )
        c.commit()
    _uid_b, sid_b = await _create_logged_in_user(client, db, "user-b@example.com")
    client.cookies.set("session_id", sid_b)

    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 999, "email": "whatever@example.com"}},
    ])

    r = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)

    assert r.headers["location"] == "/new?login=success", "это вход, а не привязка — identity уже существовала"
    new_sid = r.cookies["session_id"]
    logged_in_user_id = db.execute(
        "SELECT user_id FROM sessions WHERE id=?", (new_sid,)
    ).fetchone()["user_id"]
    assert logged_in_user_id == uid_a
    assert db.execute("SELECT COUNT(*) c FROM oauth_identities WHERE provider='vk' AND provider_uid='999'").fetchone()["c"] == 1


# ── Яндекс и Mail.ru: тот же механизм на другом поле id ──────────────────────

@pytest.mark.asyncio
async def test_yandex_callback_creates_identity(monkeypatch, client, db):
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-ya-id")
    monkeypatch.setattr(main, "YANDEX_CLIENT_SECRET", "secret")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("ya_state", "s1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"id": "40802813", "default_email": "ivan@ya.ru"},
    ])

    r = await client.get("/auth/yandex/callback?code=abc&state=s1", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/new?login=success"
    row = db.execute(
        "SELECT user_id FROM oauth_identities WHERE provider='yandex' AND provider_uid='40802813'"
    ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_mailru_callback_creates_identity(monkeypatch, client, db):
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "test-mr-id")
    monkeypatch.setattr(main, "MAILRU_CLIENT_SECRET", "secret")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("mr_state", "s1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"id": "12640001", "email": "ivan@mail.ru"},
    ])

    r = await client.get("/auth/mailru/callback?code=abc&state=s1", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/new?login=success"
    row = db.execute(
        "SELECT user_id FROM oauth_identities WHERE provider='mailru' AND provider_uid='12640001'"
    ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_yandex_callback_no_id_redirects_to_error(monkeypatch, client):
    """Провайдер обязан отдать стабильный id — без него привязку заводить не на чем."""
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "test-ya-id")
    monkeypatch.setattr(main, "YANDEX_CLIENT_SECRET", "secret")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("ya_state", "s1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"default_email": "ivan@ya.ru"},
    ])

    r = await client.get("/auth/yandex/callback?code=abc&state=s1", follow_redirects=False)

    assert r.status_code == 303
    assert "auth_error=yandex" in r.headers["location"]


# ── Отвязка ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unlink_removes_identity_but_keeps_user(client, db):
    uid, sid = await _create_logged_in_user(client, db, "ivan@example.com")
    with db as c:
        c.execute(
            "INSERT INTO oauth_identities (provider, provider_uid, user_id) VALUES ('vk', '1', ?)",
            (uid,)
        )
        c.commit()
    client.cookies.set("session_id", sid)

    r = await client.post("/api/settings/oauth/vk/unlink")

    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert db.execute("SELECT COUNT(*) c FROM oauth_identities WHERE user_id=?", (uid,)).fetchone()["c"] == 0
    assert db.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone() is not None


@pytest.mark.asyncio
async def test_unlink_requires_session(client):
    r = await client.post("/api/settings/oauth/vk/unlink")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unlink_unknown_provider_404(client, db):
    _, sid = await _create_logged_in_user(client, db, "ivan@example.com")
    client.cookies.set("session_id", sid)
    r = await client.post("/api/settings/oauth/telegram/unlink")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unlink_does_not_touch_other_users_identity(client, db):
    """Отвязка идёт по (provider, user_id) — не должна задеть чужую привязку
    с тем же provider, даже если в БД она лежит следующей строкой."""
    uid_a, sid_a = await _create_logged_in_user(client, db, "user-a@example.com")
    uid_b, _ = await _create_logged_in_user(client, db, "user-b@example.com")
    with db as c:
        c.execute("INSERT INTO oauth_identities (provider, provider_uid, user_id) VALUES ('vk', '1', ?)", (uid_a,))
        c.execute("INSERT INTO oauth_identities (provider, provider_uid, user_id) VALUES ('vk', '2', ?)", (uid_b,))
        c.commit()
    client.cookies.set("session_id", sid_a)

    r = await client.post("/api/settings/oauth/vk/unlink")

    assert r.status_code == 200
    assert db.execute(
        "SELECT COUNT(*) c FROM oauth_identities WHERE provider='vk' AND user_id=?", (uid_b,)
    ).fetchone()["c"] == 1


# ── /settings отражает текущие привязки ─────────────────────────────────────

@pytest.mark.asyncio
async def test_settings_page_lists_linked_provider_email(monkeypatch, client, db):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", True)
    uid, sid = await _create_logged_in_user(client, db, "ivan@example.com")
    with db as c:
        c.execute(
            "INSERT INTO oauth_identities (provider, provider_uid, user_id, email_at_link)"
            " VALUES ('vk', '1', ?, 'vk-account@example.com')",
            (uid,)
        )
        c.commit()
    client.cookies.set("session_id", sid)

    r = await client.get("/settings")

    assert r.status_code == 200
    assert "vk-account@example.com" in r.text
    assert "Отключить" in r.text


@pytest.mark.asyncio
async def test_settings_page_offers_connect_for_unlinked_provider(monkeypatch, client, db):
    monkeypatch.setattr(main, "VK_LOGIN_ENABLED", True)
    _, sid = await _create_logged_in_user(client, db, "ivan@example.com")
    client.cookies.set("session_id", sid)

    r = await client.get("/settings")

    assert r.status_code == 200
    assert '/auth/vk' in r.text
    assert "Подключить" in r.text

# ── Занятый адрес: провайдеру не отдают чужой аккаунт ───────────────────────
# Раньше новая привязка без сессии подхватывала существующего пользователя по
# email, который назвал провайдер. Это доверие к чужому справочнику: провайдер,
# отдающий неподтверждённый адрес, отдавал вместе с ним и чужой аккаунт со
# всеми резюме и оплаченным Pro. Теперь совпадение адреса требует доказать
# владение аккаунтом — войти своим способом и подключить провайдера в
# настройках.


async def _create_user_without_session(db, email):
    """Аккаунт как после входа по magic-link: есть, но привязок к OAuth нет."""
    with db as c:
        c.execute("INSERT INTO users (email) VALUES (?)", (email,))
        c.commit()
        return c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


@pytest.mark.asyncio
async def test_vk_callback_refuses_account_owned_by_someone_else(monkeypatch, client, db):
    uid = await _create_user_without_session(db, "victim@example.com")
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 777, "email": "victim@example.com"}},
    ])

    r = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"] == "/new?auth_link_required=vk"
    assert "session_id" not in r.cookies, "вход не должен состояться"
    assert db.execute(
        "SELECT COUNT(*) c FROM oauth_identities WHERE provider='vk'"
    ).fetchone()["c"] == 0, "привязка не должна была записаться"
    assert db.execute("SELECT COUNT(*) c FROM sessions WHERE user_id=?", (uid,)).fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_refusal_does_not_depend_on_letter_case(monkeypatch, client, db):
    """Регистр адреса не обходной путь: users.email COLLATE NOCASE."""
    await _create_user_without_session(db, "victim@example.com")
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 777, "email": "Victim@Example.COM"}},
    ])

    r = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)

    assert r.headers["location"] == "/new?auth_link_required=vk"
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_yandex_callback_refuses_account_owned_by_someone_else(monkeypatch, client, db):
    """Отказ живёт в общем обработчике, а не в одном колбэке."""
    await _create_user_without_session(db, "victim@example.com")
    monkeypatch.setattr(main, "YANDEX_CLIENT_ID", "cid")
    monkeypatch.setattr(main, "YANDEX_CLIENT_SECRET", "sec")
    client.cookies.set("ya_state", "s1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"id": "9001", "default_email": "victim@example.com"},
    ])

    r = await client.get("/auth/yandex/callback?code=abc&state=s1", follow_redirects=False)

    assert r.headers["location"] == "/new?auth_link_required=yandex"
    assert db.execute("SELECT COUNT(*) c FROM oauth_identities").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_mailru_callback_refuses_account_owned_by_someone_else(monkeypatch, client, db):
    await _create_user_without_session(db, "victim@example.com")
    monkeypatch.setattr(main, "MAILRU_CLIENT_ID", "cid")
    monkeypatch.setattr(main, "MAILRU_CLIENT_SECRET", "sec")
    client.cookies.set("mr_state", "s1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"id": "9002", "email": "victim@example.com"},
    ])

    r = await client.get("/auth/mailru/callback?code=abc&state=s1", follow_redirects=False)

    assert r.headers["location"] == "/new?auth_link_required=mailru"
    assert db.execute("SELECT COUNT(*) c FROM oauth_identities").fetchone()["c"] == 0


@pytest.mark.asyncio
async def test_owner_links_the_same_provider_after_logging_in(monkeypatch, client, db):
    """Путь из отказа: войти своим способом и подключить провайдера в настройках.

    Тот же адрес и тот же provider_uid, что в отказе выше, — разница только в
    сессии, то есть в доказательстве владения аккаунтом.
    """
    uid, sid = await _create_logged_in_user(client, db, "owner@example.com")
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("session_id", sid)
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 777, "email": "owner@example.com"}},
    ])

    r = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)

    assert r.headers["location"] == "/settings?linked=vk"
    row = db.execute(
        "SELECT user_id FROM oauth_identities WHERE provider='vk' AND provider_uid='777'"
    ).fetchone()
    assert row["user_id"] == uid
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1


@pytest.mark.asyncio
async def test_second_login_through_saved_identity_is_not_refused(monkeypatch, client, db):
    """Отказ не должен задевать обычный повторный вход.

    После первой привязки аккаунт находится по oauth_identities, до сверки
    адреса дело не доходит — иначе каждый второй вход упирался бы в отказ.
    """
    monkeypatch.setattr(main, "VK_CLIENT_ID", "test-vk-id")
    monkeypatch.setattr(main, "APP_URL", "http://localhost:8000")
    client.cookies.set("vk_state", "s1")
    client.cookies.set("vk_verifier", "v1")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 555, "email": "fresh@example.com"}},
    ])
    r1 = await client.get("/auth/vk/callback?code=abc&state=s1&device_id=dev", follow_redirects=False)
    assert r1.headers["location"] == "/new?login=success"

    client.cookies.delete("session_id")
    client.cookies.set("vk_state", "s2")
    client.cookies.set("vk_verifier", "v2")
    _mock_oauth_http(monkeypatch, [
        {"access_token": "tok"},
        {"user": {"user_id": 555, "email": "fresh@example.com"}},
    ])
    r2 = await client.get("/auth/vk/callback?code=abc&state=s2&device_id=dev", follow_redirects=False)

    assert r2.headers["location"] == "/new?login=success"
    assert db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 1


def test_oauth_email_taken_carries_provider():
    """Провайдер виден и в атрибуте, и в тексте исключения.

    Первое читает обработчик и подставляет в редирект, второе попадает в
    трейсбек, если исключение когда-нибудь выйдет за его пределы, — иначе там
    будет пустое «OAuthEmailTaken» без единой подсказки, какой это вход.
    """
    exc = main.OAuthEmailTaken("vk")

    assert exc.provider == "vk"
    assert str(exc) == "vk"
