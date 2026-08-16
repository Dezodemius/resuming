"""Step definitions для сценариев доступности API.

Запросы уходят прямо в ASGI-приложение через `httpx.ASGITransport`: сценариям
не нужен ни свободный порт, ни запущенный uvicorn, и в CI они выполняются так
же быстро, как обычные тесты.
"""
from __future__ import annotations

import json

from behave import given, then, when
from httpx import ASGITransport, AsyncClient, Response


def _json(response: Response) -> dict:
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise AssertionError(f"ответ не JSON: {response.text[:200]!r}") from exc


@given("запущенное приложение")
def step_app_is_running(context) -> None:
    context.client = AsyncClient(
        transport=ASGITransport(app=context.app), base_url="http://test"
    )


@when('я отправляю {method} на "{path}"')
def step_send_request(context, method: str, path: str) -> None:
    context.response = context.run(context.client.request(method, path))


@then("код ответа {status:d}")
def step_check_status(context, status: int) -> None:
    actual = context.response.status_code
    assert actual == status, f"ожидали {status}, получили {actual}: {context.response.text[:200]}"


@then('JSON-поле "{field}" равно "{value}"')
def step_check_json_field_value(context, field: str, value: str) -> None:
    payload = _json(context.response)
    assert payload.get(field) == value, f"{field}={payload.get(field)!r}, ожидали {value!r}"


@then('ответ содержит JSON-поле "{field}"')
def step_check_json_field_present(context, field: str) -> None:
    payload = _json(context.response)
    assert field in payload, f"в ответе нет поля {field!r}: {payload}"


@then('заголовок "{header}" равен "{value}"')
def step_check_header(context, header: str, value: str) -> None:
    actual = context.response.headers.get(header)
    assert actual == value, f"{header}={actual!r}, ожидали {value!r}"
