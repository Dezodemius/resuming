#!/usr/bin/env bash
# Автодеплой Резюмирую.рф на стенд timeweb.cloud.
#
# Запускается с дев-машины (Git Bash на Windows или любой Linux/macOS).
# Кладёт текущее рабочее дерево на VPS по SSH и поднимает docker compose
# стенда (app + nginx, без Ollama — она внешняя, адрес в .env стенда).
#
# Минимум для запуска:
#   export DEPLOY_HOST=1.2.3.4
#   export DEPLOY_SSH_KEY=~/.ssh/timeweb_stage
#   ./deploy/deploy.sh
#
# Полный список переменных и флагов: ./deploy/deploy.sh --help
set -euo pipefail

# Ассоциативные массивы (приоритет переменных окружения над .deploy.env)
# появились в bash 4. В Git Bash и любом современном Linux это выполняется.
if [ "${BASH_VERSINFO[0]:-0}" -lt 4 ]; then
    echo "Нужен bash 4+, запущен ${BASH_VERSION:-неизвестно}. На macOS: brew install bash" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CONFIG_FILE="$SCRIPT_DIR/.deploy.env"

# ── Вывод ─────────────────────────────────────────────────────────────────
if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_OK=""; C_WARN=""; C_ERR=""; C_DIM=""; C_OFF=""
fi
info() { printf '%s==>%s %s\n' "$C_DIM" "$C_OFF" "$*"; }
ok()   { printf '%s ok %s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s  ! %s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
die()  { printf '%sОШИБКА:%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
Автодеплой Резюмирую.рф на стенд timeweb.cloud.

Использование:
  ./deploy/deploy.sh [флаги]            выкатить текущее рабочее дерево
  ./deploy/deploy.sh --status           статус контейнеров стенда
  ./deploy/deploy.sh --logs [N]         последние N строк логов (по умолчанию 200)
  ./deploy/deploy.sh --restart          перезапустить контейнеры
  ./deploy/deploy.sh --down             остановить стенд (данные сохраняются)
  ./deploy/deploy.sh --ssh              открыть ssh-сессию на стенде

Переменные (окружение > deploy/.deploy.env > дефолт):
  DEPLOY_HOST             IP стенда                       (обязательно)
  DEPLOY_SSH_KEY          путь к приватному SSH-ключу     (обязательно)
  DEPLOY_USER             пользователь SSH                (root)
  DEPLOY_PORT             порт SSH                        (22)
  DEPLOY_PATH             каталог на сервере              (/opt/resuming)
  STAGING_ENV_FILE        локальный .env стенда           (deploy/.env.staging)
  STAGING_HTTP_PORT       внешний порт nginx              (80)
  STAGING_AUTH_USER       логин basic-auth                (пусто = без auth)
  STAGING_AUTH_PASSWORD   пароль basic-auth               (пусто = без auth)
  SSH_STRICT_HOST_KEY     режим StrictHostKeyChecking     (accept-new)

Флаги:
  --host IP | --key PATH | --user U | --port N | --path P
  --env-file F        взять .env стенда из другого файла
  --skip-env          не заливать .env — использовать уже лежащий на сервере
  --committed         выкатить HEAD вместо рабочего дерева
  --no-bootstrap      не ставить docker автоматически
  --no-prune          не чистить висячие образы после сборки
  --allow-live-keys   разрешить .env с боевыми ключами Робокассы (без ROBOKASSA_TEST_MODE=1)
  --dry-run           показать план и остановиться до изменений на сервере
  -h, --help          эта справка
EOF
}

# ── Конфигурация: окружение важнее файла .deploy.env ──────────────────────
CONFIG_KEYS=(DEPLOY_HOST DEPLOY_SSH_KEY DEPLOY_USER DEPLOY_PORT DEPLOY_PATH
             STAGING_ENV_FILE STAGING_HTTP_PORT STAGING_AUTH_USER
             STAGING_AUTH_PASSWORD SSH_STRICT_HOST_KEY)
declare -A _from_env=()
for _k in "${CONFIG_KEYS[@]}"; do
    if [ -n "${!_k:-}" ]; then _from_env[$_k]="${!_k}"; fi
done
if [ -f "$CONFIG_FILE" ]; then
    set -a; . "$CONFIG_FILE"; set +a
fi
for _k in "${!_from_env[@]}"; do printf -v "$_k" '%s' "${_from_env[$_k]}"; done

DEPLOY_USER="${DEPLOY_USER:-root}"
DEPLOY_PORT="${DEPLOY_PORT:-22}"
DEPLOY_PATH="${DEPLOY_PATH:-/opt/resuming}"
STAGING_ENV_FILE="${STAGING_ENV_FILE:-$SCRIPT_DIR/.env.staging}"
STAGING_HTTP_PORT="${STAGING_HTTP_PORT:-80}"
STAGING_AUTH_USER="${STAGING_AUTH_USER:-}"
STAGING_AUTH_PASSWORD="${STAGING_AUTH_PASSWORD:-}"
SSH_STRICT_HOST_KEY="${SSH_STRICT_HOST_KEY:-accept-new}"

ACTION="deploy"
LOG_LINES=200
SKIP_ENV=0
USE_COMMITTED=0
BOOTSTRAP=1
PRUNE=1
ALLOW_LIVE_KEYS=0
DRY_RUN=0

while [ $# -gt 0 ]; do
    case "$1" in
        --host)       DEPLOY_HOST="${2:?--host требует значение}"; shift 2 ;;
        --key)        DEPLOY_SSH_KEY="${2:?--key требует значение}"; shift 2 ;;
        --user)       DEPLOY_USER="${2:?--user требует значение}"; shift 2 ;;
        --port)       DEPLOY_PORT="${2:?--port требует значение}"; shift 2 ;;
        --path)       DEPLOY_PATH="${2:?--path требует значение}"; shift 2 ;;
        --env-file)   STAGING_ENV_FILE="${2:?--env-file требует значение}"; shift 2 ;;
        --skip-env)   SKIP_ENV=1; shift ;;
        --committed)  USE_COMMITTED=1; shift ;;
        --no-bootstrap) BOOTSTRAP=0; shift ;;
        --no-prune)   PRUNE=0; shift ;;
        --allow-live-keys) ALLOW_LIVE_KEYS=1; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        --status)     ACTION="status"; shift ;;
        --restart)    ACTION="restart"; shift ;;
        --down)       ACTION="down"; shift ;;
        --ssh)        ACTION="ssh"; shift ;;
        --logs)       ACTION="logs"; shift
                      if [ "${1:-}" ] && [ -z "${1##[0-9]*}" ]; then LOG_LINES="$1"; shift; fi ;;
        -h|--help)    usage; exit 0 ;;
        *)            die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
done

# ── Preflight ─────────────────────────────────────────────────────────────
for cmd in ssh git tar gzip; do
    command -v "$cmd" >/dev/null 2>&1 || die "не найдена команда '$cmd'"
done
[ -n "${DEPLOY_HOST:-}" ]    || die "не задан DEPLOY_HOST (IP стенда). export DEPLOY_HOST=1.2.3.4 или --host"
[ -n "${DEPLOY_SSH_KEY:-}" ] || die "не задан DEPLOY_SSH_KEY (путь к SSH-ключу). export DEPLOY_SSH_KEY=~/.ssh/key или --key"

TMP_DIR="$(mktemp -d)"
cleanup() { rm -rf "$TMP_DIR"; }
trap cleanup EXIT

# Копия ключа с правами 600: ssh отказывается работать с ключом, доступным
# группе/всем, а файлы с NTFS-дисков в Git Bash обычно приезжают как 0644.
KEY_SRC="$DEPLOY_SSH_KEY"
case "$KEY_SRC" in
    [A-Za-z]:[\\/]*) command -v cygpath >/dev/null 2>&1 && KEY_SRC="$(cygpath -u "$KEY_SRC")" ;;
    "~"/*)           KEY_SRC="$HOME/${KEY_SRC#\~/}" ;;
esac
[ -f "$KEY_SRC" ] || die "SSH-ключ не найден: $DEPLOY_SSH_KEY"
KEY_FILE="$TMP_DIR/deploy_key"
cp "$KEY_SRC" "$KEY_FILE"
chmod 600 "$KEY_FILE"

SSH_OPTS=(-i "$KEY_FILE" -p "$DEPLOY_PORT"
          -o "StrictHostKeyChecking=$SSH_STRICT_HOST_KEY"
          -o PasswordAuthentication=no
          -o ConnectTimeout=15
          -o ServerAliveInterval=30)
REMOTE="$DEPLOY_USER@$DEPLOY_HOST"

ssh_run() { ssh "${SSH_OPTS[@]}" "$REMOTE" "$@"; }

# Заливка файла: путь может лежать под root'ом, поэтому пишем через tee.
upload_to() {
    local src="$1" dst="$2" mode="${3:-644}"
    if [ "$DRY_RUN" = "1" ]; then info "(dry-run) не заливаю $dst"; return 0; fi
    ssh_run "$REMOTE_SUDO install -d -m 755 \"\$(dirname '$dst')\" \
             && $REMOTE_SUDO tee '$dst' > /dev/null \
             && $REMOTE_SUDO chmod $mode '$dst'" < "$src"
}

STAGING_URL="http://$DEPLOY_HOST"
[ "$STAGING_HTTP_PORT" = "80" ] || STAGING_URL="http://$DEPLOY_HOST:$STAGING_HTTP_PORT"

# ── Сервисные действия ────────────────────────────────────────────────────
if [ "$ACTION" = "ssh" ]; then
    exec ssh -t "${SSH_OPTS[@]}" "$REMOTE" "cd '$DEPLOY_PATH' 2>/dev/null; exec \$SHELL -l"
fi

info "проверяю связь с $REMOTE:$DEPLOY_PORT"
REMOTE_UID="$(ssh_run 'id -u' | tr -d '\r')" || die "не подключиться по SSH. Проверь IP, порт, пользователя и ключ"
REMOTE_SUDO=""
[ "$REMOTE_UID" = "0" ] || REMOTE_SUDO="sudo"
ok "SSH работает (uid=$REMOTE_UID)"

# Серверную половину кладём файлом и запускаем оттуда, а не через `bash -s`:
# иначе docker compose внутри неё вычитал бы из stdin остаток самого скрипта.
run_remote() {  # команда remote-deploy.sh
    local remote_script="/tmp/resuming-remote-deploy.sh"
    ssh_run "cat > '$remote_script'" < "$SCRIPT_DIR/remote-deploy.sh"
    ssh_run "DEPLOY_PATH='$DEPLOY_PATH' STAGING_HTTP_PORT='$STAGING_HTTP_PORT' \
             ARCHIVE_PATH='$REMOTE_ARCHIVE' ENV_UPLOADED='$ENV_UPLOADED' \
             AUTH_UPLOADED='$AUTH_UPLOADED' BOOTSTRAP='$BOOTSTRAP' \
             PRUNE='$PRUNE' LOG_LINES='$LOG_LINES' bash '$remote_script' $1"
}

REMOTE_ARCHIVE="/tmp/resuming-src.tgz"
ENV_UPLOADED=0
AUTH_UPLOADED=0

case "$ACTION" in
    status|logs|restart|down)
        run_remote "$ACTION"
        exit 0
        ;;
esac

# ── Сборка архива с кодом ─────────────────────────────────────────────────
BRANCH="$(git -C "$ROOT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '?')"
COMMIT="$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo '?')"
ARCHIVE="$TMP_DIR/src.tgz"
FILE_LIST="$TMP_DIR/files.z"

if [ "$USE_COMMITTED" = "1" ]; then
    info "пакую HEAD ($BRANCH @ $COMMIT)"
    git -C "$ROOT_DIR" archive --format=tar HEAD | gzip -6 > "$ARCHIVE"
else
    info "пакую рабочее дерево ($BRANCH @ $COMMIT)"
    # tracked + новые незаигноренные файлы; .env и data/ отсекаем явно, чтобы
    # прод-секреты и локальная база не уехали на стенд даже если их закоммитят
    git -C "$ROOT_DIR" ls-files -z --cached --others --exclude-standard \
        | grep -zv -E '^(\.env$|deploy/\.env|deploy/_authconf/|data/|backups/)' > "$FILE_LIST" || true
    ( cd "$ROOT_DIR" && tar --null -T "$FILE_LIST" -czf "$ARCHIVE" )
fi

FILE_COUNT="$(tar -tzf "$ARCHIVE" | wc -l | tr -d ' ')"
ARCHIVE_SIZE="$(du -h "$ARCHIVE" | cut -f1)"
ok "архив готов: $FILE_COUNT файлов, $ARCHIVE_SIZE"

if [ "$USE_COMMITTED" != "1" ] && [ -n "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null)" ]; then
    warn "в рабочем дереве есть незакоммиченные правки — они попадут на стенд"
fi

# ── .env стенда ───────────────────────────────────────────────────────────
if [ "$SKIP_ENV" = "1" ]; then
    info ".env не заливаю (--skip-env), беру уже лежащий на сервере"
else
    [ -f "$STAGING_ENV_FILE" ] || die \
        "нет файла $STAGING_ENV_FILE. Скопируй deploy/.env.staging.example и заполни, либо запусти с --skip-env"

    # Стенд с боевыми ключами Робокассы без тестового режима = реальные
    # списания с карт тестировщиков. У Робокассы (в отличие от ЮKassa) нет
    # префикса live_/test_ в самих ключах — тестовый режим включается
    # отдельным флагом ROBOKASSA_TEST_MODE=1 (IsTest=1 в запросе на оплату).
    if [ "$ALLOW_LIVE_KEYS" != "1" ] \
        && grep -qE '^ROBOKASSA_LOGIN=.+' "$STAGING_ENV_FILE" \
        && ! grep -qE '^ROBOKASSA_TEST_MODE=1' "$STAGING_ENV_FILE"; then
        die "в $STAGING_ENV_FILE задан ROBOKASSA_LOGIN без ROBOKASSA_TEST_MODE=1 — боевые платежи на стенде. Возьми тестовый магазин и/или поставь ROBOKASSA_TEST_MODE=1; осознанно — запусти с --allow-live-keys"
    fi
    if grep -qE '^APP_URL=https://' "$STAGING_ENV_FILE"; then
        warn "APP_URL в .env стенда указывает на https — у стенда TLS нет, cookie с Secure не долетят"
    fi

    info "заливаю .env стенда"
    upload_to "$STAGING_ENV_FILE" "$DEPLOY_PATH/.env" 600
    if [ "$DRY_RUN" != "1" ]; then
        ENV_UPLOADED=1
        ok ".env на месте"
    fi
fi

# ── Опциональный basic-auth ───────────────────────────────────────────────
if [ -n "$STAGING_AUTH_USER" ] && [ -n "$STAGING_AUTH_PASSWORD" ]; then
    command -v openssl >/dev/null 2>&1 || die "для basic-auth нужен openssl (генерирует .htpasswd)"
    printf '%s:%s\n' "$STAGING_AUTH_USER" "$(openssl passwd -apr1 "$STAGING_AUTH_PASSWORD")" > "$TMP_DIR/htpasswd"
    cat > "$TMP_DIR/auth.conf" <<'EOF'
auth_basic           "Резюмирую.рф — стенд";
auth_basic_user_file /etc/nginx/staging-auth/.htpasswd;
EOF
    info "включаю basic-auth для пользователя $STAGING_AUTH_USER"
    upload_to "$TMP_DIR/auth.conf" "$DEPLOY_PATH/_authconf/auth.conf" 644
    upload_to "$TMP_DIR/htpasswd" "$DEPLOY_PATH/_authconf/.htpasswd" 644
    AUTH_UPLOADED=1
else
    # Сбрасываем возможный auth от прошлых запусков — иначе стенд остался бы
    # под паролем, которого уже нет в переменных
    if [ "$DRY_RUN" != "1" ]; then
        ssh_run "$REMOTE_SUDO rm -rf '$DEPLOY_PATH/_authconf'" || true
    fi
    warn "basic-auth выключен: стенд открыт всем, кто знает IP (STAGING_AUTH_USER/STAGING_AUTH_PASSWORD включают пароль)"
fi

if [ "$DRY_RUN" = "1" ]; then
    cat <<EOF

${C_DIM}--- dry-run: на сервере ничего не менялось ---${C_OFF}
  сервер      $REMOTE:$DEPLOY_PORT
  каталог     $DEPLOY_PATH
  код         $BRANCH @ $COMMIT ($FILE_COUNT файлов, $ARCHIVE_SIZE)
  compose     $DEPLOY_PATH/src/deploy/docker-compose.staging.yml
  адрес       $STAGING_URL
EOF
    exit 0
fi

# ── Заливка кода и запуск ─────────────────────────────────────────────────
info "отправляю код на сервер"
ssh_run "cat > '$REMOTE_ARCHIVE'" < "$ARCHIVE"
ok "код на сервере"

info "разворачиваю стенд"
run_remote deploy

# ── Проверка снаружи ──────────────────────────────────────────────────────
if command -v curl >/dev/null 2>&1; then
    info "проверяю $STAGING_URL/healthz снаружи"
    if curl -fsS -m 15 "$STAGING_URL/healthz" >/dev/null 2>&1; then
        ok "стенд отвечает: $STAGING_URL"
    else
        warn "снаружи $STAGING_URL/healthz недоступен, хотя контейнер здоров — проверь фаервол timeweb и порт $STAGING_HTTP_PORT"
    fi
fi

printf '\n%sГотово.%s Стенд: %s\n' "$C_OK" "$C_OFF" "$STAGING_URL"
printf '  логи:      ./deploy/deploy.sh --logs 100\n'
printf '  статус:    ./deploy/deploy.sh --status\n'
printf '  остановка: ./deploy/deploy.sh --down\n'
