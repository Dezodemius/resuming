#!/usr/bin/env bash
# Серверная половина деплоя. Не запускается вручную: deploy.sh кладёт этот
# файл в /tmp на сервере и вызывает `bash /tmp/… <команда>`, переменные
# приходят через окружение ssh-команды.
#
# Команды: deploy | status | logs | restart | down
#
# Ожидаемые переменные: DEPLOY_PATH, STAGING_HTTP_PORT, ARCHIVE_PATH,
# ENV_UPLOADED (0/1), AUTH_UPLOADED (0/1), BOOTSTRAP (0/1), PRUNE (0/1),
# LOG_LINES.
set -euo pipefail

CMD="${1:-deploy}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/resuming}"
STAGING_HTTP_PORT="${STAGING_HTTP_PORT:-80}"
ARCHIVE_PATH="${ARCHIVE_PATH:-/tmp/resuming-src.tgz}"
ENV_UPLOADED="${ENV_UPLOADED:-0}"
AUTH_UPLOADED="${AUTH_UPLOADED:-0}"
BOOTSTRAP="${BOOTSTRAP:-1}"
PRUNE="${PRUNE:-1}"
LOG_LINES="${LOG_LINES:-200}"

SRC="$DEPLOY_PATH/src"
COMPOSE_FILE="$SRC/deploy/docker-compose.staging.yml"

say()  { printf '  [server] %s\n' "$*"; }
die()  { printf '  [server] ОШИБКА: %s\n' "$*" >&2; exit 1; }

# На timeweb по умолчанию заходим root'ом, но скрипт не должен ломаться под
# обычным пользователем с sudo.
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "нужен root или sudo"
    SUDO="sudo"
fi

compose() {
    [ -f "$COMPOSE_FILE" ] || die \
        "на сервере нет развёрнутого стенда ($COMPOSE_FILE). Сначала выкати код: ./deploy/deploy.sh"
    $SUDO env STAGING_HTTP_PORT="$STAGING_HTTP_PORT" \
        docker compose -f "$COMPOSE_FILE" "$@"
}

# ── Установка docker на свежую VPS ────────────────────────────────────────
ensure_docker() {
    if command -v docker >/dev/null 2>&1 && $SUDO docker compose version >/dev/null 2>&1; then
        return
    fi
    [ "$BOOTSTRAP" = "1" ] || die "docker (или плагин compose) не установлен, а bootstrap выключен"

    say "docker не найден — ставлю через get.docker.com (это займёт пару минут)"
    command -v curl >/dev/null 2>&1 || {
        $SUDO apt-get update -qq && $SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -qq curl
    }
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    $SUDO sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
    $SUDO systemctl enable --now docker
    $SUDO docker compose version >/dev/null 2>&1 || die "docker compose plugin так и не появился"
    say "docker установлен: $($SUDO docker --version)"
}

# ── Разворачивание нового кода ────────────────────────────────────────────
unpack_src() {
    [ -f "$ARCHIVE_PATH" ] || die "архив с кодом не найден: $ARCHIVE_PATH"

    $SUDO mkdir -p "$DEPLOY_PATH"
    $SUDO rm -rf "$DEPLOY_PATH/src.new"
    $SUDO mkdir -p "$DEPLOY_PATH/src.new"
    $SUDO tar -xzf "$ARCHIVE_PATH" -C "$DEPLOY_PATH/src.new"
    rm -f "$ARCHIVE_PATH"

    # .env стенда живёт вне сменяемого каталога, иначе его затирал бы каждый
    # деплой. Если свежий файл только что залили — он уже лежит в $DEPLOY_PATH.
    [ -f "$DEPLOY_PATH/.env" ] || die \
        ".env стенда не найден в $DEPLOY_PATH. Залейте его: ./deploy/deploy.sh (см. deploy/.env.staging.example)"
    $SUDO cp "$DEPLOY_PATH/.env" "$DEPLOY_PATH/src.new/.env"
    $SUDO chmod 600 "$DEPLOY_PATH/.env" "$DEPLOY_PATH/src.new/.env"

    # Каталог для basic-auth: nginx монтирует его всегда, содержимое опционально
    $SUDO mkdir -p "$DEPLOY_PATH/src.new/deploy/_authconf"
    if [ "$AUTH_UPLOADED" = "1" ]; then
        $SUDO cp -r "$DEPLOY_PATH/_authconf/." "$DEPLOY_PATH/src.new/deploy/_authconf/"
        say "basic-auth включён"
    fi

    $SUDO rm -rf "$DEPLOY_PATH/src.old"
    if [ -d "$SRC" ]; then
        $SUDO mv "$SRC" "$DEPLOY_PATH/src.old"
    fi
    $SUDO mv "$DEPLOY_PATH/src.new" "$SRC"
}

# ── Ожидание healthcheck ──────────────────────────────────────────────────
wait_healthy() {
    local tries=60 st
    while [ "$tries" -gt 0 ]; do
        st="$($SUDO docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
              resuming-staging-app 2>/dev/null || echo missing)"
        case "$st" in
            healthy) return 0 ;;
            unhealthy) return 1 ;;
            exited|dead) return 1 ;;
        esac
        tries=$((tries - 1))
        sleep 3
    done
    return 1
}

rollback() {
    [ -d "$DEPLOY_PATH/src.old" ] || { say "откатывать нечего — прошлой версии на сервере нет"; return 1; }
    say "откатываюсь на предыдущую версию"
    $SUDO rm -rf "$DEPLOY_PATH/src.failed"
    $SUDO mv "$SRC" "$DEPLOY_PATH/src.failed"
    $SUDO mv "$DEPLOY_PATH/src.old" "$SRC"
    compose up -d --build >/dev/null 2>&1 || true
    say "откат выполнен, сломанная версия осталась в $DEPLOY_PATH/src.failed"
}

case "$CMD" in
    deploy)
        ensure_docker
        unpack_src

        say "собираю и поднимаю контейнеры"
        if ! compose up -d --build; then
            rollback || true
            die "docker compose up завершился с ошибкой"
        fi

        say "жду healthcheck приложения"
        if ! wait_healthy; then
            say "приложение не стало healthy, последние логи:"
            compose logs --tail 60 app || true
            rollback || true
            die "приложение не поднялось"
        fi

        # /readyz дополнительно проверяет доступность Ollama: на стенде она
        # внешняя, поэтому её недоступность — предупреждение, а не провал.
        # При degraded эндпоинт отвечает 503, и urlopen кидает HTTPError —
        # тело ответа с деталями достаём из исключения.
        ready="$(compose exec -T app python -c "
import urllib.request, urllib.error
try:
    print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=10).read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode())
except Exception as e:
    print('{\"status\": \"unreachable\", \"error\": \"%s\"}' % e)
" < /dev/null 2>/dev/null || echo '{"status": "unreachable"}')"
        say "readyz: $ready"
        # Starlette отдаёт JSON без пробелов, но подстраховываемся обоими видами
        case "$ready" in
            *'"status":"ok"'*|*'"status": "ok"'*) : ;;
            *) say "ВНИМАНИЕ: /readyz не ok — обычно это недоступная внешняя Ollama (OLLAMA_URL) или несовпадение OLLAMA_MODEL; сам сайт при этом работает" ;;
        esac

        if [ "$PRUNE" = "1" ]; then
            $SUDO docker image prune -f >/dev/null 2>&1 || true
        fi

        $SUDO rm -rf "$DEPLOY_PATH/src.old"
        say "деплой завершён"
        compose ps
        ;;

    status)
        compose ps
        ;;

    logs)
        compose logs --tail "$LOG_LINES" --no-color
        ;;

    restart)
        compose restart
        compose ps
        ;;

    down)
        compose down
        say "стенд остановлен (данные в volume resuming-staging_app_data сохранены)"
        ;;

    *)
        die "неизвестная команда: $CMD"
        ;;
esac
