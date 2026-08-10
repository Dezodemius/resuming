"""Лендинг, маршрутизация воронки и серверный трекинг её шагов."""
import main


async def _login(client, email):
    """Вход по magic-ссылке — тот же путь, что и у живого пользователя."""
    main.init_db()
    with main.get_db() as db:
        db.execute(
            "INSERT INTO magic_tokens (token, email, expires_at, used)"
            " VALUES (?,?,datetime('now','+10 minutes'),0)",
            (f"tok-{email}", email),
        )
        db.commit()
    await client.get(f"/auth/email/verify?token=tok-{email}", follow_redirects=False)
    with main.get_db() as db:
        return db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()["id"]


# ── Что где лежит ────────────────────────────────────────────────────────
async def test_root_serves_landing_not_generator(client):
    """На «/» анониму нужен лендинг, а не форма генератора."""
    r = await client.get("/")
    assert r.status_code == 200
    assert 'id="pricing"' in r.text            # блок тарифов лендинга
    assert 'href="/new"' in r.text             # CTA ведёт в генератор
    assert 'id="panel-match"' not in r.text    # разметки генератора здесь быть не должно


async def test_generator_moved_to_new(client):
    r = await client.get("/new")
    assert r.status_code == 200
    assert 'id="panel-match"' in r.text


async def test_root_redirects_authenticated_user_to_generator(client):
    """Залогиненному маркетинговая страница не нужна — сразу в продукт."""
    await _login(client, "landing-auth@test.com")
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/new"
    # Ответ зависит от cookie — он не должен попасть в общий кеш прокси
    assert "no-store" in r.headers.get("cache-control", "")


async def test_landing_has_funnel_steps(client):
    """Воронка: обещание → как работает → пример → тарифы → возражения → CTA."""
    r = await client.get("/")
    for anchor in ('id="how"', 'id="demo"', 'id="features"', 'id="pricing"', 'id="faq"'):
        assert anchor in r.text, f"на лендинге нет блока {anchor}"
    assert 'data-goal="cta_hero"' in r.text
    assert 'data-goal="cta_final"' in r.text


async def test_landing_prices_come_from_config(client):
    """Цена на лендинге не захардкожена: иначе она разъедется с реальным платежом."""
    r = await client.get("/")
    assert f"{int(float(main.PRO_PRICE))} ₽" in r.text
    assert str(main.ANON_LIMIT) in r.text


async def test_robots_and_sitemap(client):
    r = await client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /admin" in r.text
    assert "Sitemap:" in r.text

    r = await client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "<urlset" in r.text
    assert "/pricing" in r.text


# ── Трекинг шагов воронки ────────────────────────────────────────────────
async def test_track_records_known_event(client):
    main.init_db()
    r = await client.post("/api/track", json={"event": "landing_view"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    with main.get_db() as db:
        rows = db.execute(
            "SELECT event FROM usage_events WHERE event='landing_view'"
        ).fetchall()
    assert len(rows) == 1


async def test_track_ignores_unknown_event(client):
    """Ручка публичная: произвольные строки в журнал событий не пускаем."""
    main.init_db()
    r = await client.post("/api/track", json={"event": "'; DROP TABLE users; --"})
    assert r.status_code == 200
    assert r.json() == {"ok": False}
    with main.get_db() as db:
        count = db.execute("SELECT COUNT(*) c FROM usage_events").fetchone()["c"]
    assert count == 0


async def test_track_binds_event_to_logged_in_user(client):
    uid = await _login(client, "track@test.com")
    await client.post("/api/track", json={"event": "cta_plan_pro"})
    with main.get_db() as db:
        row = db.execute(
            "SELECT user_id FROM usage_events WHERE event='cta_plan_pro'"
        ).fetchone()
    assert row["user_id"] == uid


# ── Оплата: описание платежа должно совпадать с тем, что выдаётся ────────
async def test_payment_description_matches_granted_service(client, monkeypatch):
    """В чек ЮKassa уходит то же, что вебхук потом кладёт в аккаунт (Pro),
    а не «пакет адаптаций» — иначе покупатель платит за одно, получает другое."""
    await _login(client, "pay-desc@test.com")
    sent = {}

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"id": "pay-1", "confirmation": {"confirmation_url": "https://yookassa/x"}}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, **kw):
            sent.update(kw.get("json") or {})
            return _FakeResp()

    monkeypatch.setattr(main, "YOKASSA_SHOP", "shop")
    monkeypatch.setattr(main, "YOKASSA_SECRET", "secret")
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda *a, **k: _FakeClient())

    r = await client.post("/api/pay", json={})
    assert r.status_code == 200, r.text
    assert sent["amount"]["value"] == main.PRO_PRICE
    assert "Pro" in sent["description"]
    assert str(main.PRO_DAYS) in sent["description"]
    # После оплаты пользователь возвращается в продукт, а не на лендинг:
    # иначе редирект залогиненного с «/» съедает ?paid=1 и уведомления нет.
    assert sent["confirmation"]["return_url"].endswith("/new?paid=1")
