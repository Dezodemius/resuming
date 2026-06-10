# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

**Резюме.ИИ** — AI-генератор резюме. Адаптирует резюме под конкретную вакансию, хранит версии по компаниям, предоставляет редактор. Использует локальную LLM через Ollama.

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

**База данных** — SQLite в WAL-режиме. Путь: `/app/data/resume.db` в Docker, `./data/resume.db` локально (задаётся через `DATA_DIR`). Схема инициализируется при старте через `init_db()`. Один воркер + asyncio + SQLite — намеренное решение; для масштабирования потребует переход на PostgreSQL.

**AI-вызовы** — `call_ai()` обращается к Ollama через OpenAI-совместимый endpoint `/v1/chat/completions`. Семафор `_ai_sem` ограничивает параллельность (по умолчанию 2). Промпты — `_match_prompt`, `_general_prompt`, `_generate_prompt` — возвращают строгий JSON-формат резюме.

**Авторизация** — два метода:
- Telegram Login Widget: верификация HMAC-SHA256 подписи + проверка `auth_date` (не старше 1 часа)
- Email magic link: UUID-токен в БД, действует 15 минут, отправка через aiosmtplib

Сессии — cookie `session_id` (httpOnly, 30 дней). Анонимный превью — HMAC-подписанный cookie `anon_id` (нельзя сбросить очисткой cookie).

**Лимиты использования** — у каждого пользователя: `free_left` (3 бесплатных), `paid_left` (докупаемые пачки), `is_pro` + `pro_expires_at` (подписка). `_deduct()` / `_refund()` — атомарные списания с откатом при ошибке AI. FREE_RESUMES=5 — лимит хранимых резюме для бесплатных.

**Платежи** — ЮKassa. Вебхук `/api/pay/webhook` верифицирует платёж напрямую через ЮKassa API (не доверяет только webhook-данным) перед выдачей Pro.

**Rate limiting** — через `slowapi`; опционален (graceful fallback если не установлен).

**Фронтенд** — Jinja2-шаблоны в `templates/`. JS-логика встроена прямо в HTML. `_footer.html` и `_legal_base.html` — переиспользуемые части.

## Key env vars

| Переменная | Назначение |
|---|---|
| `OLLAMA_URL` | URL Ollama (по умолчанию `http://localhost:11434`) |
| `OLLAMA_MODEL` | Модель (по умолчанию `qwen2.5:14b`) |
| `SECRET_KEY` | HMAC-ключ для подписи anon-cookie — **обязателен** в проде |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_NAME` | Telegram Login Widget |
| `YOKASSA_SHOP_ID` / `YOKASSA_SECRET_KEY` | ЮKassa платежи |
| `SMTP_*` | Email magic link |
| `APP_URL` | Публичный URL (влияет на secure-cookie и CORS) |
| `AI_CONCURRENCY` | Параллельных вызовов Ollama (по умолчанию 2) |
