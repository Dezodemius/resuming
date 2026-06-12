"""Ops MCP-сервер для прод-диагностики «Резюмирую.рф».

Streamable HTTP MCP-сервер (0.0.0.0:8765, path /mcp), живёт в docker compose
рядом с приложением и ходит к docker CLI через проброшенный docker.sock.

Доступ только с заголовком `Authorization: Bearer $OPS_MCP_TOKEN`.
Без токена в окружении сервер отказывается стартовать.

Инструменты фиксированные (никакого произвольного выполнения команд):
  - get_logs(service, since)  — docker logs по allowlist контейнеров
  - status()                  — docker ps + docker stats
  - restart(service)          — docker restart по allowlist
  - db_stats()                — COUNT(*) по таблицам SQLite (read-only)
"""

import hmac
import os
import re
import subprocess
import sys

import uvicorn
from mcp.server.fastmcp import FastMCP

# Короткое имя -> имя контейнера. Только эти контейнеры доступны инструментам.
SERVICES = {
    "app": "resuming-app",
    "ollama": "resuming-ollama",
    "nginx": "resuming-nginx",
}

# Допустимый формат --since: 90s / 30m / 2h и т.п.
SINCE_RE = re.compile(r"^\d{1,5}[smh]$")

# Лимит вывода одного инструмента (~50 КБ)
MAX_OUTPUT = 50_000

DB_TABLES = [
    "users",
    "sessions",
    "magic_tokens",
    "profiles",
    "resumes",
    "payments",
    "anon_usage",
]

mcp = FastMCP(
    "resuming-ops",
    instructions=(
        "Диагностика прода Резюмирую.рф: логи, статус и рестарт контейнеров, "
        "статистика SQLite-базы."
    ),
    host="0.0.0.0",
    port=8765,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


def _truncate(text: str) -> str:
    """Обрезает вывод до MAX_OUTPUT символов, оставляя хвост (он обычно важнее)."""
    if len(text) <= MAX_OUTPUT:
        return text
    return f"[... обрезано, показаны последние {MAX_OUTPUT} символов ...]\n" + text[-MAX_OUTPUT:]


def _run(args: list[str], timeout: int = 30) -> tuple[int, str]:
    """Запускает docker CLI с таймаутом. Возвращает (returncode, объединённый вывод)."""
    try:
        proc = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # docker logs пишет в оба потока — склеиваем
            text=True,
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"[таймаут {timeout}s] {' '.join(args)}"
    except FileNotFoundError:
        return 127, "docker CLI не найден внутри контейнера ops-mcp"
    return proc.returncode, (proc.stdout or "").strip()


def _format(rc: int, out: str) -> str:
    if rc != 0:
        out = f"[команда завершилась с кодом {rc}]\n{out}"
    return _truncate(out) if out else "(пустой вывод)"


@mcp.tool()
def get_logs(service: str = "all", since: str = "2h") -> str:
    """Логи прод-контейнеров (docker logs --timestamps).

    Args:
        service: app | ollama | nginx | all
        since: глубина, например 90s, 30m, 2h (по умолчанию 2h)
    """
    if not SINCE_RE.match(since):
        return f"Недопустимый since={since!r}. Формат: число + s/m/h, например 30m или 2h."
    if service == "all":
        targets = list(SERVICES.items())
    elif service in SERVICES:
        targets = [(service, SERVICES[service])]
    else:
        return f"Недопустимый service={service!r}. Доступно: {', '.join(SERVICES)} или all."

    chunks = []
    for _, container in targets:
        rc, out = _run(
            ["docker", "logs", "--since", since, "--timestamps", container],
            timeout=60,
        )
        chunks.append(f"===== {container} (since {since}) =====\n{_format(rc, out)}")
    return _truncate("\n\n".join(chunks))


@mcp.tool()
def status() -> str:
    """Статус прод-контейнеров: docker ps (name=resuming) + docker stats --no-stream."""
    rc, ps_out = _run(
        [
            "docker", "ps", "-a",
            "--filter", "name=resuming",
            "--format", "table {{.Names}}\t{{.Status}}\t{{.RunningFor}}\t{{.Image}}",
        ]
    )
    result = f"=== docker ps (name=resuming) ===\n{_format(rc, ps_out)}"

    rc, names_out = _run(
        ["docker", "ps", "--filter", "name=resuming", "--format", "{{.Names}}"]
    )
    running = [n for n in names_out.splitlines() if n.strip()] if rc == 0 else []
    if running:
        rc, stats_out = _run(["docker", "stats", "--no-stream", *running], timeout=45)
        result += f"\n\n=== docker stats ===\n{_format(rc, stats_out)}"
    else:
        result += "\n\n=== docker stats ===\n(нет запущенных контейнеров resuming-*)"
    return _truncate(result)


@mcp.tool()
def restart(service: str) -> str:
    """Перезапускает один прод-контейнер (docker restart).

    Args:
        service: app | ollama | nginx (строгий allowlist)
    """
    if service not in SERVICES:
        return f"Недопустимый service={service!r}. Доступно: {', '.join(SERVICES)}."
    container = SERVICES[service]
    rc, out = _run(["docker", "restart", container], timeout=120)
    if rc != 0:
        return f"Не удалось перезапустить {container}:\n{_format(rc, out)}"
    return f"Контейнер {container} перезапущен (docker restart выполнен успешно)."


@mcp.tool()
def db_stats() -> str:
    """Количество строк в основных таблицах SQLite (read-only, file:?mode=ro)."""
    tables = ", ".join(repr(t) for t in DB_TABLES)
    script = (
        "import json, sqlite3\n"
        "con = sqlite3.connect('file:/app/data/resume.db?mode=ro', uri=True)\n"
        f"out = {{}}\n"
        f"for t in [{tables}]:\n"
        "    try:\n"
        "        out[t] = con.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]\n"
        "    except sqlite3.Error as e:\n"
        "        out[t] = 'error: ' + str(e)\n"
        "con.close()\n"
        "print(json.dumps(out, ensure_ascii=False, indent=2))\n"
    )
    rc, out = _run(["docker", "exec", "resuming-app", "python", "-c", script], timeout=30)
    return _format(rc, out)


class BearerAuthMiddleware:
    """ASGI-middleware: пускает только запросы с верным Authorization: Bearer <token>."""

    def __init__(self, app, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":  # lifespan и пр. — пропускаем как есть
            await self.app(scope, receive, send)
            return
        auth = b""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"authorization":
                auth = value
                break
        if not hmac.compare_digest(auth, self._expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send(
                {"type": "http.response.body", "body": b'{"error": "unauthorized"}'}
            )
            return
        await self.app(scope, receive, send)


def main() -> None:
    token = os.environ.get("OPS_MCP_TOKEN", "").strip()
    if not token:
        print(
            "FATAL: переменная окружения OPS_MCP_TOKEN не задана или пуста.\n"
            "Сервер отказывается стартовать без Bearer-токена: добавь в .env\n"
            "  OPS_MCP_TOKEN=<длинная случайная строка>\n"
            "и перезапусти контейнер.",
            file=sys.stderr,
        )
        sys.exit(1)

    app = BearerAuthMiddleware(mcp.streamable_http_app(), token)
    uvicorn.run(app, host="0.0.0.0", port=8765, log_level="info")


if __name__ == "__main__":
    main()
