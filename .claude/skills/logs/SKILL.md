---
name: logs
description: Снять логи и статус контейнеров с прод-сервера через GitHub Actions воркфлоу logs.yml и поставить диагноз («сайт лежит», ошибка 522, не генерятся резюме). Use when the user asks about production errors, logs, why the site is down, 522 from Cloudflare, OOM, containers crashing, or resume generation failing in prod.
---

# Logs — диагностика прода через self-hosted раннер

> **Контекст:** весь сбор и анализ логов выполняй в отдельном субагенте
> (Explore или general-purpose) — сырой вывод `gh run view --log` объёмный и в
> основной диалог не нужен. Наружу верни только краткий диагноз.

SSH-доступа к прод-серверу с дев-машины НЕТ. Единственный канал — self-hosted GitHub-раннер на самом сервере. Логи снимаются запуском workflow `logs.yml` (workflow_dispatch) и чтением вывода джоба через `gh`. Все команды ниже — PowerShell-совместимые (никаких `&&`).

## Шаг 0. Предварительные проверки

```powershell
gh auth status
```

Если не залогинен — `gh auth login`, дальше без авторизации ничего не сработает.

**Важно:** `workflow_dispatch` работает только после того, как `logs.yml` попал в default branch (`main`). Если воркфлоу ещё не смержен туда, `gh workflow run` выдаст ошибку вида «could not find any workflows named logs.yml» / «workflow not found». В этом случае сообщи пользователю, что нужен мерж `develop` → `main`, и остановись.

## Шаг 1. Запуск воркфлоу

Подбери `since` под жалобу пользователя: «только что упало» → `30m`; «не работает пару часов» → `2h`–`4h`; «со вчера» → `24h`. `service` — пусто (все сервисы) либо один из: `ollama`, `app`, `nginx`. При 522 и подозрении на OOM начинай со всех сервисов.

```powershell
gh workflow run logs.yml --repo Dezodemius/resuming -f since=2h -f service=
```

## Шаг 2. Дождаться завершения

```powershell
gh run list --repo Dezodemius/resuming --workflow=logs.yml --limit 1
```

Возьми ID последнего запуска (run может появиться с задержкой в несколько секунд — если списка нет, повтори). Затем:

```powershell
gh run watch <id> --repo Dezodemius/resuming
```

## Шаг 3. Скачать вывод

```powershell
gh run view <id> --repo Dezodemius/resuming --log
```

Вывод может быть большим (особенно при `since=24h`) — при необходимости сохрани во временный файл и анализируй через Grep:

```powershell
gh run view <id> --repo Dezodemius/resuming --log | Out-File -Encoding utf8 $env:TEMP\prod-logs.txt
```

## Шаг 4. Анализ

Смотреть в выводе три блока: `Container status` (docker compose ps), `Collect logs`, `Memory and CPU stats` (docker stats).

1. **Статус контейнеров** — все три (`resuming-ollama`, `resuming-app`, `resuming-nginx`) должны быть `Up`/`running`. `Restarting`, `Exited`, малый uptime при давно работающем сервере — признак падений/рестартов.
2. **Фильтр ошибок в логах** — искать `ERROR`, `Traceback`, `CRITICAL`, статусы `500`/`502`/`504` в логах nginx/app, `exception`.
3. **Признаки OOM у ollama** — `llama-server died`, `out of memory`, `OOM`, `killed`, обрывы загрузки модели, повторяющиеся строки старта модели (контейнер перезапускался). Сверить с `docker stats`: модель `qwen2.5:14b` требует ~10+ ГБ — если у ollama память упирается в лимит, это оно.
4. **Свести краткий диагноз** пользователю: что упало, когда (по таймстампам), вероятная причина, что делать.

## Известные режимы отказа проекта

- **(а) Ollama OOM при загрузке модели** → контейнер ollama умирает/рестартится → app не получает ответ → Cloudflare отдаёт 522. Самый частый сценарий «сайт лежит».
- **(б) Вебхук ЮKassa** (`/api/pay/webhook`) — ошибки верификации платежа через API ЮKassa; искать в логах app по `webhook`, `yookassa`, `pay`.
- **(в) SMTP при отправке magic link** — таймауты/ошибки aiosmtplib; пользователи «не получают письмо для входа»; искать по `smtp`, `magic`, `mail`.

## Когда НЕ использовать

- Проблема воспроизводится локально — отлаживай локально, не дёргай прод.
- Нужно что-то **поменять** на сервере — это деплой через `ci_cd.yml` (push в `main`), а не этот скилл.
