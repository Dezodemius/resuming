# Ops MCP-сервер

Лёгкий MCP-сервер диагностики прода. Живёт в docker compose рядом с приложением,
наружу доступен через nginx за Cloudflare-прокси: `https://ops.резюмирую.рф/mcp`
(`ops.xn--e1aedprev8fe.xn--p1ai`), каждый запрос — с заголовком
`Authorization: Bearer $OPS_MCP_TOKEN`.

Инструменты: `get_logs`, `status`, `restart`, `db_stats`. Все — фиксированные
docker-команды по allowlist контейнеров (`resuming-app` / `resuming-ollama` /
`resuming-nginx`), произвольное выполнение команд невозможно.

## Разовая настройка (один раз)

1. **DNS-запись в Cloudflare** (обычный дашборд, Zero Trust не нужен):
   DNS → Records → добавить запись `A` (или `CNAME` на корневой домен):
   - name: `ops`
   - значение: тот же IP (или домен), что у записи `резюмирую.рф`
   - Proxy status: **Proxied** (оранжевое облако)

   Дальше nginx на сервере сам отроутит `ops.резюмирую.рф` → `ops-mcp:8765`
   (см. второй `server`-блок в `nginx.conf`).

2. **На прод-сервере** в `.env` (рядом с `docker-compose.yml`) добавить:

   ```
   OPS_MCP_TOKEN=<длинная случайная строка, например 64 hex-символа>
   ```

   Сгенерировать токен можно так (PowerShell):

   ```powershell
   -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
   ```

   Затем: `docker compose up -d --build ops-mcp; docker compose restart nginx`.

3. **На дев-машине** (где работает Claude Code) задать тот же токен —
   его подставляет `.mcp.json`:

   ```powershell
   $env:OPS_MCP_TOKEN = '<тот же токен, что на сервере>'
   ```

   (Чтобы не вводить каждый раз — добавить в профиль PowerShell или
   в переменные окружения пользователя Windows.)

## Безопасность — прочитай честно

- В контейнер прокинут `/var/run/docker.sock`. Это **эквивалент root на хосте**:
  кто управляет этим контейнером — управляет всей машиной. Поэтому сервер
  ограничен фиксированными инструментами, а единственная защита снаружи —
  Bearer-токен. Береги его как пароль root.
- Порты ops-mcp на хост **не публикуются** — не добавляй `ports:` этому сервису.
  Внутрь ведёт только nginx-прокси по hostname `ops.*`.
- Без `OPS_MCP_TOKEN` сервер не стартует; любой запрос без верного
  `Authorization: Bearer ...` получает 401.
- Трафик Cloudflare → origin идёт по HTTP (nginx слушает только 80), то есть
  Bearer-токен на этом плече не шифруется — как и cookie основного сайта.
  Если захочется закрыть это плечо — поднять TLS на nginx с Cloudflare
  Origin-сертификатом и переключить SSL-режим на Full.
- Кто знает IP origin-сервера, может ходить мимо Cloudflare прямо на порт 80
  с заголовком `Host: ops.…` — авторизация всё равно требует Bearer-токен,
  но rate-limiting и WAF Cloudflare при этом не работают.

## Как проверить после деплоя

```powershell
# без токена — должен быть 401
curl.exe -i https://ops.xn--e1aedprev8fe.xn--p1ai/mcp

# с токеном — НЕ 401 (MCP-ответ/406 на GET — это нормально, главное не 401)
curl.exe -i -H "Authorization: Bearer <OPS_MCP_TOKEN>" https://ops.xn--e1aedprev8fe.xn--p1ai/mcp
```

В Claude Code: `/mcp` → reconnect сервера ops (или перезапустить сессию) —
должны появиться инструменты `get_logs`, `status`, `restart`, `db_stats`.
