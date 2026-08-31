# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**Резюмирую.рф** — AI-генератор резюме. Адаптирует резюме под конкретную вакансию, хранит версии по компаниям, предоставляет редактор. Использует локальную LLM через Ollama.

Stack: FastAPI + SQLite + Jinja2 + Ollama (`qwen2.5:14b`) + Робокасса + OAuth (Яндекс/VK/Mail.ru) + Email magic link.

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

# Стенд на timeweb.cloud (VPS по IP, без Ollama — она внешняя)
export DEPLOY_HOST=… DEPLOY_SSH_KEY=…
./deploy/deploy.sh            # выкатить рабочее дерево; --status / --logs / --down

# Контроль качества (см. раздел «Quality gates»)
ruff check .                                 # линтер
pytest tests/ -q                             # юнит- и интеграционные тесты
behave                                       # Gherkin-сценарии
python tools/mutation_diff.py --base origin/main   # мутанты по git-диффу (POSIX)
```

В dev-режиме без `SMTP_USER` magic-ссылка печатается в stdout вместо отправки письма.

**Три контура деплоя** (подробно — `deploy/README.md`). Прод (`резюмирую.рф`)
живёт на **app-01** (Timeweb, `/srv/apps/resuming`): пуш в `main` →
`.github/workflows/ci_cd.yml` → SSH → `deploy/deploy-prod.sh` с
`deploy/docker-compose.prod.yml` (только `app`, на `127.0.0.1:8001`,
Ollama внешняя). Домен и TLS держит **хостовой** nginx этой машины, общий ещё с
двумя проектами. Дев-стенд — на той же машине, `/srv/apps/resuming-dev`: пуш в
`develop` → джоба `deploy-dev` → `deploy/deploy-dev.sh` с
`docker-compose.dev.yml` (`127.0.0.1:8002`, свой том, `DEV_MODE=1`, домена нет —
вход SSH-туннелем). Стенд на отдельном VPS — `deploy/deploy.sh`: архив рабочего
дерева по SSH, `docker-compose.staging.yml` с app + nginx.

Корневой `docker-compose.yml` — четвёртый, самодостаточный контур (локальная
Ollama + свой nginx на порту 80). **На app-01 его запускать нельзя**: займёт
80-й порт и уронит хостовой прокси вместе с соседними сайтами. Правки в
инфраструктуре вносятся в каждый контур отдельно, они не наследуют друг друга.

## Quality gates

Отдельный воркфлоу `.github/workflows/quality.yml` — три последовательных
этапа: линтер → Gherkin → мутации. Деплой (`ci_cd.yml`) от него намеренно не
зависит: мутационный гейт жёсткий (любой выживший мутант = красная сборка), и
пока балл на старом коде не подтянут, он блокировал бы выкатку.

**Gherkin (behave).** Сценарии — `tests/bdd/features/*.feature` (русский
Gherkin, `# language: ru`), шаги — `tests/bdd/steps/`, окружение —
`tests/bdd/environment.py` (свой `DATA_DIR`, выключенный лимитер, клиент поверх
ASGI без uvicorn). Конфиг — `behave.ini`, запускать из корня проекта.

**Мутации (mutmut).** Конфиг — `[mutmut]` в `setup.cfg`; мутируются только
`main.py`, `config.py`, `db.py`, `prompts.py`, `schemas.py`. Инкрементальный
режим — `tools/mutation_diff.py`: берёт дифф с базовой веткой, сужает до
задетых функций и роняет прогон на любом невыжившем мутанте. Полный прогон по
`main.py` — десятки минут, поэтому в CI только дифф. **mutmut использует
`os.fork()` и на Windows не работает** — локально гонять из WSL или контейнера.

## Architecture

Весь бэкенд — один файл `main.py` (~2200 строк). Нет отдельных модулей, роутеров или сервисов.

**Страницы** — `/` отдаёт лендинг (`landing.html`) анонимам и редиректит залогиненных на `/resumes` (доска); `/new` — сам генератор (`index.html`). Ссылки «Создать резюме» во всех шаблонах ведут на `/new`, логотип и `doLogout()` — на `/`. Шаги воронки лендинга пишутся в `usage_events` через `POST /api/track` (белый список `_FUNNEL_EVENTS`).

**База данных** — SQLite в WAL-режиме. Путь: `/app/data/resume.db` в Docker, `./data/resume.db` локально (задаётся через `DATA_DIR`). Схема инициализируется при старте через `init_db()`. Один воркер + asyncio + SQLite — намеренное решение; для масштабирования потребует переход на PostgreSQL.

**Изменение схемы.** `init_db()` выполняет `CREATE TABLE IF NOT EXISTS` — на
базе, где таблица уже есть, это не делает ничего. Поэтому новую колонку или
ограничение мало дописать в `init_db`: на рабочих базах они не появятся.
Изменения едут шагами миграций в `db.py` (`SCHEMA_VERSION` + `migrate()`,
версия хранится в `PRAGMA user_version`). Добавили шаг — подняли
`SCHEMA_VERSION` и дописали тест в `tests/test_db.py`.

`get_db()` — **контекстный менеджер** (`with get_db() as db:`): коммитит при успехе, откатывает при исключении и всегда закрывает соединение. Для «сырого» соединения (тесты, скрипты) есть `db.connect()`. Протухшие сессии, magic-токены и старые `anon_usage`/`usage_events` чистит фоновая задача `_cleanup_loop` → `cleanup_expired()`.

**Ошибки и безопасность** — middleware `security_headers` вешает CSP (режим в `CSP_MODE`), `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` и HSTS (только при https-`APP_URL`). Обработчики `StarletteHTTPException`/`Exception` отдают `error.html` на навигацию браузера и JSON `{"detail": …}` на всё, что под `/api/`, `/auth/`, `/mcp`. MCP смонтирован на `/` через обёртку `_McpMountOr404` — иначе он перехватывал бы все неизвестные URL.

**AI-вызовы** — `call_ai()` обращается к Ollama через OpenAI-совместимый endpoint `/v1/chat/completions`. Семафор `_ai_sem` ограничивает параллельность (по умолчанию 2). Промпты — `_match_prompt`, `_general_prompt`, `_generate_prompt` — возвращают строгий JSON-формат резюме.

**Авторизация** — два метода:
- Email magic link: UUID-токен в БД, действует 15 минут, отправка через aiosmtplib
- OAuth: Яндекс ID, VK ID (PKCE, без секрета), Mail.ru — общим рубильником `OAUTH_LOGIN_ENABLED`

Состав включённых способов пишется в лог при старте (`Способы входа: …`), там же
предупреждения о недонастроенных провайдерах. Вход через Telegram убран.

Сессии — cookie `session_id` (httpOnly, 30 дней). Анонимный превью считается по двум ключам: HMAC-подписанный cookie `anon_id` (`ANON_LIMIT`) и HMAC адреса посетителя с суточным окном (`ANON_IP_LIMIT`). Второй нужен потому, что cookie клиент может просто не возвращать — подпись мешает присвоить чужой идентификатор, но не мешает сбросить свой.

**Лимиты использования** — у каждого пользователя: `free_left` (3 бесплатных), `paid_left` (докупаемые пачки), `is_pro` + `pro_expires_at` (подписка). `_deduct()` / `_refund()` — атомарные списания с откатом при ошибке AI. FREE_RESUMES=5 — лимит хранимых резюме для бесплатных.

**Согласие на передачу данных AI-провайдеру** — генерация уносит анкету
внешнему провайдеру за пределы РФ (ст. 12 152-ФЗ), поэтому она закрыта
отдельным подтверждением. У зарегистрированного оно живёт в
`users.ai_consent_at` + `ai_consent_rev`, у анонима — флагом `consent` в теле
`/api/generate-preview` (в браузере отметка лежит в `localStorage`). Проверяют
`_has_ai_consent()` / `_consent_required()` (403 `consent_required`) —
`_run_generation`, `/api/match/start` и `/api/improve-text`, то есть все пути к
модели. Редакция текста — `AI_CONSENT_REV` в `config.py`: поднять её —
значит спросить согласие заново у всех, поэтому она обязана совпадать с датой
редакции `privacy.html`. Модалка общая для генератора и редактора —
`templates/_consent_modal.html` + `static/consent.js` (глобал `AiConsent`),
расхождение текста между страницами означало бы два разных согласия.

**Платежи** — Робокасса, без вызова внешнего API: `POST /api/pay` сам собирает поля POST-формы и подпись `MD5(LOGIN:OutSum:InvId:Receipt:PASSWORD1)`, где `InvId` = `payments.id + INV_ID_OFFSET`, а `Receipt` (`_robokassa_receipt`) — URL-кодированная номенклатура чека из одной позиции (`full_payment` / `service` / `tax: none`). В подпись и в поле формы обязана уйти одна и та же закодированная строка — вторая перекодировка ломает платёж. `Description` Робокасса ограничивает 100 символами, поэтому слишком длинное описание роняет создание счёта в 503, а не создаёт неоплачиваемую строку. Ответ (`action` / `method` / `fields`) отправляет `static/payment.js` одноразовой формой; платёжный адрес продублирован в `ROBOKASSA_PAY_URL`, в `form-action` CSP и в самом `payment.js` — менять надо все три (держит `test_payment_url_agrees_across_backend_csp_and_frontend`). Вебхук `/api/pay/webhook` — ResultURL Робокассы (form/query, не JSON): проверяет подпись `MD5(OutSum:InvId:PASSWORD2)`, затем подтверждает платёж через `OpStateExt` (не доверяет только вебхуку) и отвечает `OK{InvId}` при успехе.

**Rate limiting** — через `slowapi`; опционален (graceful fallback если не установлен). Есть глобальный backstop `240/minute` (`SlowAPIMiddleware` + `default_limits`) поверх точечных `@rate`. Ключ лимита — `_client_key`: `CF-Connecting-IP` → первый `X-Forwarded-For` → peer, иначе за Cloudflare+nginx все посетители попали бы в одно ведро. В тестах лимитер выключен через `RATE_LIMIT_ENABLED=0` (см. `tests/conftest.py`).

**MCP** — FastMCP (streamable-http, stateless, json_response) смонтирован в конце `main.py` через `app.mount("/")`; endpoint — `/mcp`, session manager стартует внутри lifespan. Инструменты `get_profile` / `adapt_resume` авторизуются по `Authorization: Bearer <token>` через таблицу `api_tokens`; токен выдаёт `POST /api/mcp-token` (один активный на пользователя).

**Фронтенд** — Jinja2-шаблоны в `templates/`. JS-логика встроена прямо в HTML. `_app_footer.html` и `_legal_base.html` — переиспользуемые части; общий подвал включают все публичные страницы, `_footer.html` остался только у `/admin`. Публичные реквизиты продавца живут в `config.py` (`SELLER_*`) и приходят в шаблоны глобалом `seller` — дублировать их в разметке нельзя, подвал и юридические страницы обязаны совпадать. Дизайн-каркас и токены — `static/app.css`, блоки лендинга и `/pricing` — `static/landing.css` (префикс `.lp-`).

## Key env vars

| Переменная | Назначение |
|---|---|
| `OLLAMA_URL` | URL Ollama (по умолчанию `http://localhost:11434`) |
| `OLLAMA_MODEL` | Модель (по умолчанию `qwen2.5:14b`) |
| `SECRET_KEY` | HMAC-ключ для подписи anon-cookie — **обязателен** в проде |
| `ROBOKASSA_LOGIN` / `ROBOKASSA_PASSWORD1` / `ROBOKASSA_PASSWORD2` | Робокасса — платежи |
| `ROBOKASSA_TEST_MODE` | `1` включает тестовый режим Робокассы (`IsTest=1`) |
| `INV_ID_OFFSET` | Смещение номера счёта (`InvId = payments.id + INV_ID_OFFSET`); по умолчанию 0, поднять вручную выше макс. номера после восстановления БД из бэкапа |
| `SMTP_*` | Email magic link |
| `YANDEX_CLIENT_ID` / `YANDEX_CLIENT_SECRET` | Вход через Яндекс ID (OAuth) |
| `VK_CLIENT_ID` | Вход через VK ID (OAuth с PKCE; секрет не нужен) |
| `MAILRU_CLIENT_ID` / `MAILRU_CLIENT_SECRET` | Вход через Mail.ru (OAuth) |
| `LOG_LEVEL` | Уровень логов бэкенда (по умолчанию `INFO`) |
| `APP_URL` | Публичный URL (влияет на secure-cookie и CORS) |
| `AI_CONCURRENCY` | Параллельных вызовов Ollama (по умолчанию 2) |
| `AI_MAX_TOKENS` | Потолок токенов ответа модели (по умолчанию 4096) |
| `PRO_FAIR_USE_LIMIT` / `PRO_FAIR_USE_DAYS` | Реальная квота тарифа Pro — генераций за N дней (по умолчанию 100/30); число показывается на `/pricing`, в оферте и в описании платежа Робокассы |
| `ADMIN_EMAILS` | Email админов (через запятую), им доступен `/admin` |
| `ADMIN_IPS` | Адреса/CIDR, с которых пускают в `/admin` (пусто = не ограничивать) |
| `METRIKA_ID` | Номер счётчика Яндекс.Метрики (пусто = выключено) |
| `CSP_MODE` | `enforce` (по умолчанию) / `report` / `off` — аварийный вентиль для CSP |
| `RATE_LIMIT_ENABLED` | `0` выключает лимитер (тесты, отладка) |
| `DEV_MODE` | `1` открывает `/dev` и `/api/dev/*` — вход без письма и тариф без оплаты (дев-стенд). При `APP_URL` на https гасится принудительно |
| `DEV_BIND` | Адрес публикации порта дев-стенда (по умолчанию `127.0.0.1`) |
| `CLEANUP_INTERVAL_SEC` / `ANON_USAGE_TTL_DAYS` / `EVENTS_TTL_DAYS` | Фоновая уборка БД |

## Context management (экономия токенов)

`main.py` — ~2200 строк. Не читай его целиком. Сначала Grep по имени функции
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
