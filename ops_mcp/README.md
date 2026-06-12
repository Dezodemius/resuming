# Ops MCP-сервер

Лёгкий MCP-сервер диагностики прода. Живёт в docker compose рядом с приложением,
наружу доступен только через Cloudflare Tunnel: `https://ops.резюмирую.рф/mcp`
(`ops.xn--e1aedprev8fe.xn--p1ai`), каждый запрос — с заголовком
`Authorization: Bearer $OPS_MCP_TOKEN`.

Инструменты: `get_logs`, `status`, `restart`, `db_stats`. Все — фиксированные
docker-команды по allowlist контейнеров (`resuming-app` / `resuming-ollama` /
`resuming-nginx`), произвольное выполнение команд невозможно.

## Разовая настройка (один раз)

1. **Cloudflare Tunnel**: Zero Trust → Networks → Tunnels → Create a tunnel
   (тип Cloudflared) → скопировать токен туннеля. В Public Hostname добавить:
   - hostname: `ops.резюмирую.рф`
   - service: `http://ops-mcp:8765`

2. **На прод-сервере** в `.env` (рядом с `docker-compose.yml`) добавить:

   ```
   CF_TUNNEL_TOKEN=<токен туннеля из Cloudflare>
   OPS_MCP_TOKEN=<длинная случайная строка, например 64 hex-символа>
   ```

   Сгенерировать токен можно так (PowerShell):

   ```powershell
   -join ((1..64) | ForEach-Object { '{0:x}' -f (Get-Random -Max 16) })
   ```

   Затем: `docker compose up -d --build ops-mcp cloudflared`.

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
  ограничен фиксированными инструментами и обязан быть недоступен иначе как
  через туннель + Bearer-токен.
- Порты ops-mcp на хост **не публикуются** — не добавляй `ports:` этому сервису.
- Без `OPS_MCP_TOKEN` сервер не стартует; любой запрос без верного
  `Authorization: Bearer ...` получает 401.
- **Рекомендуется** дополнительно повесить Cloudflare Access policy на hostname
  `ops.резюмирую.рф` (Zero Trust → Access → Applications): тогда даже при утечке
  Bearer-токена снаружи потребуется ещё и аутентификация Cloudflare.

## Как проверить после деплоя

```powershell
# без токена — должен быть 401
curl.exe -i https://ops.xn--e1aedprev8fe.xn--p1ai/mcp

# с токеном — НЕ 401 (MCP-ответ/406 на GET — это нормально, главное не 401)
curl.exe -i -H "Authorization: Bearer <OPS_MCP_TOKEN>" https://ops.xn--e1aedprev8fe.xn--p1ai/mcp
```

В Claude Code: `/mcp` → reconnect сервера ops (или перезапустить сессию) —
должны появиться инструменты `get_logs`, `status`, `restart`, `db_stats`.
