"""Step definitions сценариев злоупотребления.

Проверяются ровно те ручки, которые стоят денег или ходят наружу:
анонимная генерация, /api/improve-text и /api/fetch-job. Модель подменяется
заглушкой — сценарии про контроль доступа, а не про качество ответа, и живой
вызов Ollama в CI недопустим по времени и по деньгам.
"""
from __future__ import annotations

import json

from behave import given, then, when

import main


@given("модель отвечает заглушкой")
def step_stub_model(context) -> None:
    context.ai_calls = 0

    async def fake_call_ai(_prompt: str) -> str:
        context.ai_calls += 1
        return json.dumps({"name": "Тест", "target_role": "QA"}, ensure_ascii=False)

    context.real_call_ai = main.call_ai
    main.call_ai = fake_call_ai


@given("анонимный предел по адресу равен {limit:d}")
def step_set_ip_limit(context, limit: int) -> None:
    context.real_anon_ip_limit = main.ANON_IP_LIMIT
    main.ANON_IP_LIMIT = limit


def _preview_body(consent: bool = True, profile: dict | None = None) -> dict:
    return {
        "kind": "general",
        "profile": profile or {"name": "Аноним"},
        "target_role": "QA",
        "consent": consent,
    }


@when("я {count:d} раз прошу анонимную генерацию, каждый раз выбрасывая cookie")
def step_preview_dropping_cookies(context, count: int) -> None:
    context.statuses = []
    for _ in range(count):
        # Клиент, который не возвращает cookie, каждый раз выглядит новым
        # посетителем — предел обязан держать счётчик по адресу.
        context.client.cookies.clear()
        response = context.run(
            context.client.post("/api/generate-preview", json=_preview_body())
        )
        context.statuses.append(response.status_code)
    context.response = response


@when("я прошу анонимную генерацию без согласия")
def step_preview_without_consent(context) -> None:
    context.response = context.run(
        context.client.post("/api/generate-preview", json=_preview_body(consent=False))
    )


@when("я прошу анонимную генерацию с профилем на 10 МБ")
def step_preview_with_huge_body(context) -> None:
    huge = {"name": "Аноним", "about": "я" * (10 * 1024 * 1024)}
    context.response = context.run(
        context.client.post("/api/generate-preview", json=_preview_body(profile=huge))
    )


@when('я отправляю POST на "{path}" с телом {body}')
def step_post_with_body(context, path: str, body: str) -> None:
    context.response = context.run(context.client.post(path, json=json.loads(body)))


@then("успешных генераций не больше {limit:d}")
def step_check_successful_count(context, limit: int) -> None:
    ok = [s for s in context.statuses if s == 200]
    assert len(ok) <= limit, f"успешных {len(ok)}, ожидали не больше {limit}: {context.statuses}"


@then("код последнего ответа {status:d}")
def step_check_last_status(context, status: int) -> None:
    actual = context.statuses[-1]
    assert actual == status, f"последний код {actual}, ожидали {status}: {context.statuses}"


@then("код ответа меньше {status:d}")
def step_check_status_below(context, status: int) -> None:
    actual = context.response.status_code
    assert actual < status, f"код {actual}, ожидали меньше {status}"


@then("модель не вызывалась")
def step_check_model_not_called(context) -> None:
    assert context.ai_calls == 0, f"модель вызвана {context.ai_calls} раз(а)"
