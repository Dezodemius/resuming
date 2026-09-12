"""Контракты cookie-согласия, которые не должны зависеть от поведения UI.

Тесты проверяют границу на сервере: клиентский баннер легко обойти ручным
запросом, поэтому /api/track обязан сам отвергать всё, кроме выданного
сервером аналитического согласия.
"""
from pathlib import Path
import re

import pytest

import main


def _track_count() -> int:
    """Возвращает число записанных событий воронки в изолированной БД теста."""
    with main.get_db() as db:
        return db.execute(
            "SELECT COUNT(*) FROM usage_events WHERE event='landing_view'"
        ).fetchone()[0]


async def _choose(client, choice: str):
    response = await client.post("/api/site-consent", json={"choice": choice})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["choice"] == choice
    assert isinstance(body["rev"], str) and body["rev"], body
    return response


async def test_tracking_is_disabled_by_default_and_does_not_write_event(client):
    """До выбора пользователя счетчик не должен оставлять серверный след."""
    main.init_db()

    response = await client.post("/api/track", json={"event": "landing_view"})

    assert response.status_code in (200, 204), response.text
    assert _track_count() == 0


async def test_necessary_choice_does_not_enable_tracking(client):
    """Запоминание отказа — технически необходимое, но не разрешение аналитики."""
    main.init_db()
    await _choose(client, "necessary")

    response = await client.post("/api/track", json={"event": "landing_view"})

    assert response.status_code in (200, 204), response.text
    assert _track_count() == 0


async def test_analytics_choice_issued_by_server_enables_tracking(client):
    """Только cookie, выпущенная сервером после явного выбора, допускает запись."""
    main.init_db()
    issued = await _choose(client, "analytics")
    assert "site_consent" in issued.cookies

    response = await client.post("/api/track", json={"event": "landing_view"})

    assert response.status_code == 200, response.text
    assert _track_count() == 1


async def test_tampered_site_consent_cookie_is_not_accepted(client):
    """Подмена одной буквы подписи не должна превращать отказ в разрешение."""
    main.init_db()
    issued = await _choose(client, "analytics")
    value = issued.cookies["site_consent"]
    replacement = "x" if value[-1] != "x" else "y"
    tampered = value[:-1] + replacement
    client.cookies.clear()
    client.cookies.set("site_consent", tampered)

    response = await client.post(
        "/api/track",
        json={"event": "landing_view"},
    )

    assert response.status_code in (200, 204), response.text
    assert _track_count() == 0


async def test_site_consent_cookie_has_required_safe_attributes(client, monkeypatch):
    """Cookie ограничена сервером, сайтом и сроком политики."""
    monkeypatch.setattr(main, "APP_URL", "https://test")

    response = await _choose(client, "necessary")
    header = next(
        value for value in response.headers.get_list("set-cookie")
        if value.startswith("site_consent=")
    )

    assert re.search(r"(?:^|;)\s*Max-Age=15552000(?:;|$)", header, re.I), header
    assert re.search(r"(?:^|;)\s*Path=/(?:;|$)", header, re.I), header
    assert re.search(r"(?:^|;)\s*SameSite=Lax(?:;|$)", header, re.I), header
    assert re.search(r"(?:^|;)\s*Secure(?:;|$)", header, re.I), header
    assert re.search(r"(?:^|;)\s*HttpOnly(?:;|$)", header, re.I), header


@pytest.mark.parametrize("path", ["/", "/new", "/pricing", "/privacy"])
async def test_pages_do_not_eagerly_load_unnecessary_external_resources(client, path):
    """До решения пользователя HTML не должен запускать Google Fonts или CDN PDF."""
    response = await client.get(path)

    assert response.status_code == 200
    assert not re.search(
        r'<(?:script|link)\b[^>]+(?:fonts\.googleapis|fonts\.gstatic|cdnjs\.cloudflare)',
        response.text,
        re.I,
    )


async def test_public_html_defers_metrika_and_exposes_an_accessible_banner(client, monkeypatch):
    """Счетчик не может попасть в HTML до работы consent-менеджера.

    Баннер проверяется семантически, а не по конкретной раскладке или CSS.
    """
    monkeypatch.setitem(main.tpl.env.globals, "metrika_id", "123456")

    response = await client.get("/")
    html = response.text

    assert response.status_code == 200
    assert "mc.yandex.ru/metrika/tag.js" not in html
    assert "mc.yandex.ru/watch/" not in html
    assert 'id="site-consent-banner"' in html
    banner = re.search(
        r'<(?P<tag>[^ >]+)[^>]*id="site-consent-banner"[^>]*>', html, re.I
    )
    assert banner
    # <section aria-label> имеет неявную семантику region; явный role не нужен.
    is_explicit_region = re.search(r'\brole="(?:dialog|region)"', banner.group(0), re.I)
    is_named_section = (
        banner.group("tag").lower() == "section"
        and re.search(r'\baria-(?:label|labelledby)=', banner.group(0), re.I)
    )
    assert is_explicit_region or is_named_section
    assert re.search(r'\baria-(?:label|labelledby)=', banner.group(0), re.I)
    assert "Разрешить аналитику" in html
    assert "Только необходимые" in html
    assert re.search(r'href="/privacy(?:#[^"]*)?"', html, re.I)


def test_resume_profile_has_a_separate_unchecked_opt_in_and_no_direct_write():
    """Контактный профиль нельзя незаметно отправить в localStorage из шаблона."""
    source = Path("templates/index.html").read_text(encoding="utf-8")
    opt_in = re.search(
        r'<input\b[^>]*id="resume-local-profile-opt-in"[^>]*>', source, re.I
    )

    assert opt_in, "нужен отдельный opt-in для локального профиля"
    assert "checked" not in opt_in.group(0).lower(), opt_in.group(0)
    assert not re.search(
        r"localStorage\.setItem\(\s*['\"]resume_profile['\"]", source
    ), "запись resume_profile должна идти только через проверяющий opt-in helper"
