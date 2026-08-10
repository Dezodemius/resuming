# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**Резюмирую.рф** — AI-генератор резюме. Адаптирует резюме под конкретную вакансию, хранит версии по компаниям, предоставляет редактор. Использует локальную LLM через Ollama.

Stack: FastAPI + SQLite + Jinja2 + Ollama (`qwen2.5:14b`) + ЮKassa + Telegram Login Widget + Email magic link.

## Commands

```bash
# Локальная разработка
pip install -r requirements.txt
cp .env.example .env   # заполнить ключи
uvicorn main:app --reload

# Docker (рекомендуется — поднимает ollama + app + nginx)
docker compose up --build

# Запустить модель вручную (если Ollama уже запущена отдельно)
ollama pull qwen2.5:14b
```

В dev-режиме без `SMTP_USER` magic-ссылка печатается в stdout вместо отправки письма.

## Architecture

Весь бэкенд — один файл `main.py` (~1100 строк). Нет отдельных модулей, роутеров или сервисов.

**Страницы** — `/` отдаёт лендинг (`landing.html`) анонимам и редиректит залогиненных на `/new`; `/new` — сам генератор (`index.html`). Ссылки «Создать резюме» во всех шаблонах ведут на `/new`, логотип и `doLogout()` — на `/`. Шаги воронки лендинга пишутся в `usage_events` через `POST /api/track` (белый список `_FUNNEL_EVENTS`).

**База данных** — SQLite в WAL-режиме. Путь: `/app/data/resume.db` в Docker, `./data/resume.db` локально (задаётся через `DATA_DIR`). Схема инициализируется при старте через `init_db()`. Один воркер + asyncio + SQLite — намеренное решение; для масштабирования потребует переход на PostgreSQL.

`get_db()` — **контекстный менеджер** (`with get_db() as db:`): коммитит при успехе, откатывает при исключении и всегда закрывает соединение. Для «сырого» соединения (тесты, скрипты) есть `db.connect()`. Протухшие сессии, magic-токены и старые `anon_usage`/`usage_events` чистит фоновая задача `_cleanup_loop` → `cleanup_expired()`.

**Ошибки и безопасность** — middleware `security_headers` вешает CSP (режим в `CSP_MODE`), `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` и HSTS (только при https-`APP_URL`). Обработчики `StarletteHTTPException`/`Exception` отдают `error.html` на навигацию браузера и JSON `{"detail": …}` на всё, что под `/api/`, `/auth/`, `/mcp`. MCP смонтирован на `/` через обёртку `_McpMountOr404` — иначе он перехватывал бы все неизвестные URL.

**AI-вызовы** — `call_ai()` обращается к Ollama через OpenAI-совместимый endpoint `/v1/chat/completions`. Семафор `_ai_sem` ограничивает параллельность (по умолчанию 2). Промпты — `_match_prompt`, `_general_prompt`, `_generate_prompt` — возвращают строгий JSON-формат резюме.

**Авторизация** — два метода:
- Telegram Login Widget: верификация HMAC-SHA256 подписи + проверка `auth_date` (не старше 1 часа)
- Email magic link: UUID-токен в БД, действует 15 минут, отправка через aiosmtplib

Сессии — cookie `session_id` (httpOnly, 30 дней). Анонимный превью — HMAC-подписанный cookie `anon_id` (нельзя сбросить очисткой cookie).

**Лимиты использования** — у каждого пользователя: `free_left` (3 бесплатных), `paid_left` (докупаемые пачки), `is_pro` + `pro_expires_at` (подписка). `_deduct()` / `_refund()` — атомарные списания с откатом при ошибке AI. FREE_RESUMES=5 — лимит хранимых резюме для бесплатных.

**Платежи** — ЮKassa. Вебхук `/api/pay/webhook` верифицирует платёж напрямую через ЮKassa API (не доверяет только webhook-данным) перед выдачей Pro.

**Rate limiting** — через `slowapi`; опционален (graceful fallback если не установлен). Есть глобальный backstop `240/minute` (`SlowAPIMiddleware` + `default_limits`) поверх точечных `@rate`. Ключ лимита — `_client_key`: `CF-Connecting-IP` → первый `X-Forwarded-For` → peer, иначе за Cloudflare+nginx все посетители попали бы в одно ведро. В тестах лимитер выключен через `RATE_LIMIT_ENABLED=0` (см. `tests/conftest.py`).

**MCP** — FastMCP (streamable-http, stateless, json_response) смонтирован в конце `main.py` через `app.mount("/")`; endpoint — `/mcp`, session manager стартует внутри lifespan. Инструменты `get_profile` / `adapt_resume` авторизуются по `Authorization: Bearer <token>` через таблицу `api_tokens`; токен выдаёт `POST /api/mcp-token` (один активный на пользователя).

**Фронтенд** — Jinja2-шаблоны в `templates/`. JS-логика встроена прямо в HTML. `_footer.html` и `_legal_base.html` — переиспользуемые части. Дизайн-каркас и токены — `static/app.css`, блоки лендинга и `/pricing` — `static/landing.css` (префикс `.lp-`).

## Key env vars

| Переменная | Назначение |
|---|---|
| `OLLAMA_URL` | URL Ollama (по умолчанию `http://localhost:11434`) |
| `OLLAMA_MODEL` | Модель (по умолчанию `qwen2.5:14b`) |
| `SECRET_KEY` | HMAC-ключ для подписи anon-cookie — **обязателен** в проде |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_NAME` | Telegram Login Widget |
| `YOKASSA_SHOP_ID` / `YOKASSA_SECRET_KEY` | ЮKassa платежи |
| `SMTP_*` | Email magic link |
| `YANDEX_CLIENT_ID` / `YANDEX_CLIENT_SECRET` | Вход через Яндекс ID (OAuth) |
| `VK_CLIENT_ID` | Вход через VK ID (OAuth с PKCE; секрет не нужен) |
| `MAILRU_CLIENT_ID` / `MAILRU_CLIENT_SECRET` | Вход через Mail.ru (OAuth) |
| `LOG_LEVEL` | Уровень логов бэкенда (по умолчанию `INFO`) |
| `APP_URL` | Публичный URL (влияет на secure-cookie и CORS) |
| `AI_CONCURRENCY` | Параллельных вызовов Ollama (по умолчанию 2) |
| `ADMIN_EMAILS` | Email админов (через запятую), им доступен `/admin` |
| `METRIKA_ID` | Номер счётчика Яндекс.Метрики (пусто = выключено) |
| `CSP_MODE` | `enforce` (по умолчанию) / `report` / `off` — аварийный вентиль для CSP |
| `RATE_LIMIT_ENABLED` | `0` выключает лимитер (тесты, отладка) |
| `CLEANUP_INTERVAL_SEC` / `ANON_USAGE_TTL_DAYS` / `EVENTS_TTL_DAYS` | Фоновая уборка БД |

## Context management (экономия токенов)

`main.py` — ~1100 строк. Не читай его целиком. Сначала Grep по имени функции
или маршрута, затем читай только нужный диапазон. Для разведки («где X», «как
устроено Y») используй субагент Explore: он прочитает в своём контексте и
вернёт сводку.

Делегируй субагентам всё, что даёт объёмный одноразовый вывод и дальше в
диалоге не нужно:
- `/logs` — сбор и анализ логов в субагенте, наружу только диагноз;
- `/smoke-prod` — прогон в субагенте, наружу таблица PASS/FAIL;
- `/security-audit` шаги 1–2 — разведка субагентом; починку `main.py` делай сам.

Параллелить правки можно только вне `main.py` (templates/, tests/, .github/,
docker/). Два агента не редактируют `main.py` одновременно.
