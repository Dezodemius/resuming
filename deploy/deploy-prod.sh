#!/bin/bash
# Выкат прода резюмирую.рф на app-01.
#
# Запускается серверной обёрткой /usr/local/sbin/resuming-deploy, которая перед
# этим приводит рабочее дерево к origin/main — то есть сюда управление попадает
# уже со свежим кодом, и этот файл всегда актуальной версии. Руками можно
# запустить и напрямую: bash deploy/deploy-prod.sh (код при этом не обновится).
#
# Правило контура: наружу ничего не публикуется. Домен и TLS обслуживает
# хостовой nginx этой машины, общий с двумя соседними проектами, — поэтому
# поднимаем именно deploy/docker-compose.prod.yml, а не корневой
# docker-compose.yml (тот займёт порт 80 и уронит всё три сайта разом).
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="deploy/docker-compose.prod.yml"
STAMP="$(date +%Y-%m-%d_%H-%M-%S)"

echo "=== Выкат $(git rev-parse --short HEAD) — $(git log -1 --format=%s)"

# ── Точка отката ────────────────────────────────────────────────────────────
# Помечаем текущий образ до пересборки: откатиться иначе можно только полной
# пересборкой из старого коммита, а это минуты простоя вместо секунд.
if docker image inspect resuming-app >/dev/null 2>&1; then
    docker tag resuming-app "resuming-app:rollback-$STAMP"
    echo "Точка отката: образ resuming-app:rollback-$STAMP"
fi
git rev-parse HEAD > /root/resuming-deployed-commit.txt 2>/dev/null || true

# ── Сборка и запуск ─────────────────────────────────────────────────────────
# --remove-orphans: без него контейнер сервиса, убранного из COMPOSE (как
# ops-mcp — issue #45), compose не трогает, а только предупреждает — сервис
# остаётся работать вместе с примонтированным docker.sock. Убрать его из
# репозитория оказывается недостаточно, если не убрать и с прода.
docker compose -f "$COMPOSE" up --build -d --remove-orphans

# ── Ждём, пока приложение станет healthy ────────────────────────────────────
echo "=== Ожидание healthcheck"
STATUS=""
for _ in $(seq 1 36); do
    STATUS="$(docker inspect resuming-app --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)"
    case "$STATUS" in
        healthy)   echo "healthy"; break ;;
        unhealthy) break ;;
    esac
    sleep 5
done

if [ "$STATUS" != "healthy" ]; then
    echo "ОШИБКА: контейнер не стал healthy (статус: $STATUS)"
    docker compose -f "$COMPOSE" logs --tail 40 app || true
    echo "Откат: docker tag resuming-app:rollback-$STAMP resuming-app && docker compose -f $COMPOSE up -d"
    exit 1
fi

# ── Проверка ────────────────────────────────────────────────────────────────
echo "=== Проверка"
docker compose -f "$COMPOSE" ps --format 'table {{.Service}}\t{{.Status}}'

if ! curl -fsS --max-time 10 http://127.0.0.1:8001/healthz; then
    echo "ОШИБКА: /healthz не ответил изнутри машины"
    docker compose -f "$COMPOSE" logs --tail 40 app || true
    exit 1
fi
echo

# Ручка обязана требовать сессию. Заодно это простейшая проверка, что наверх
# уехал именно свежий код, а не остался работать прежний контейнер.
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
        -X POST -H 'Content-Type: application/json' -d '{}' \
        http://127.0.0.1:8001/api/fetch-job || echo 000)"
if [ "$CODE" != "401" ]; then
    echo "ОШИБКА: POST /api/fetch-job без сессии вернул $CODE вместо 401"
    exit 1
fi
echo "POST /api/fetch-job без сессии: 401"

echo "=== Готово. На проде $(git rev-parse --short HEAD)"
