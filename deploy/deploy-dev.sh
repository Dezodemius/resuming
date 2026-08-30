#!/bin/bash
# Выкат дев-стенда на app-01.
#
# Запускается серверной обёрткой /usr/local/sbin/resuming-deploy-dev, которая
# перед этим приводит рабочее дерево /srv/apps/resuming-dev к origin/develop —
# то есть сюда управление попадает уже со свежим кодом. Руками можно и напрямую:
# bash deploy/deploy-dev.sh (код при этом не обновится).
#
# Контур намеренно слепой снаружи: порт публикуется на 127.0.0.1:8002, домена и
# TLS у него нет, хостовой nginx про него не знает. Попадают внутрь
# SSH-туннелем — deploy/README.md, раздел «Дев-стенд».
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE="deploy/docker-compose.dev.yml"

# ── Защита от выката в боевой каталог ───────────────────────────────────────
# Оба контура — это git-дерево с .env и одинаковыми скриптами в deploy/, а
# перепутать их легко: одна буква в пути ssh-команды. Разница видна только по
# .env, поэтому именно его и проверяем. Цена ошибки — дев-стенд, поднятый на
# боевом .env: рассылка писем с прода, реальные ключи Робокассы и открытый
# /dev у всех на виду.
if [ ! -f .env ]; then
    echo "ОШИБКА: нет .env. Заполните его по deploy/.env.dev.example"
    exit 1
fi
if grep -qE '^[[:space:]]*APP_URL=https' .env; then
    echo "ОШИБКА: в .env боевой APP_URL (https). Похоже, это каталог прода —"
    echo "дев-стенд разворачивается в /srv/apps/resuming-dev со своим .env."
    exit 1
fi
if ! grep -qE '^[[:space:]]*DEV_MODE=1[[:space:]]*$' .env; then
    echo "ОШИБКА: в .env нет DEV_MODE=1 — стенд поднимется без /dev и"
    echo "тестировать платные сценарии на нём будет нечем."
    exit 1
fi

# ── Адрес публикации ────────────────────────────────────────────────────────
# Compose при интерполяции ${DEV_BIND} читает .env из каталога compose-файла
# (deploy/), а наш .env лежит в корне дерева — значение туда само не попадёт,
# и порт молча уехал бы на умолчание. Достаём одну строку вручную: source на
# весь .env спотыкается о значения с пробелами и решётками.
DEV_BIND_RAW="$(grep -E '^[[:space:]]*DEV_BIND=' .env | tail -1 | cut -d= -f2-)"
DEV_BIND="${DEV_BIND_RAW//[$'\r'$'\t'\" ]/}"
DEV_BIND="${DEV_BIND//\'/}"
DEV_BIND="${DEV_BIND:-127.0.0.1}"
if ! [[ "$DEV_BIND" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
    echo "ОШИБКА: DEV_BIND=«$DEV_BIND_RAW» не похож на IPv4-адрес"
    exit 1
fi
export DEV_BIND
if [ "$DEV_BIND" != "127.0.0.1" ]; then
    echo "ВНИМАНИЕ: порт 8002 публикуется на $DEV_BIND — стенд виден не только"
    echo "изнутри машины. Убедитесь, что ufw пускает туда только ваш адрес."
fi

echo "=== Дев-стенд: выкат $(git rev-parse --short HEAD) — $(git log -1 --format=%s)"

# Точки отката, в отличие от прода, не делаем: упавший стенд никого не
# затрагивает, а чинится он следующим пушем в develop.
docker compose -f "$COMPOSE" up --build -d --remove-orphans

echo "=== Ожидание healthcheck"
STATUS=""
for _ in $(seq 1 36); do
    STATUS="$(docker inspect resuming-dev-app --format '{{.State.Health.Status}}' 2>/dev/null || echo missing)"
    case "$STATUS" in
        healthy)   echo "healthy"; break ;;
        unhealthy) break ;;
    esac
    sleep 5
done

if [ "$STATUS" != "healthy" ]; then
    echo "ОШИБКА: контейнер не стал healthy (статус: $STATUS)"
    docker compose -f "$COMPOSE" logs --tail 40 app || true
    exit 1
fi

echo "=== Проверка"
docker compose -f "$COMPOSE" ps --format 'table {{.Service}}\t{{.Status}}'

if ! curl -fsS --max-time 10 http://127.0.0.1:8002/healthz; then
    echo "ОШИБКА: /healthz не ответил изнутри машины"
    docker compose -f "$COMPOSE" logs --tail 40 app || true
    exit 1
fi
echo

# DEV_MODE выключается не только переменной: config.py гасит флаг, если APP_URL
# похож на боевой. Молча получить стенд без пульта из-за этого слишком легко,
# поэтому проверяем сам факт, а не наличие строки в .env.
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:8002/dev || echo 000)"
if [ "$CODE" != "200" ]; then
    echo "ОШИБКА: /dev вернул $CODE вместо 200 — DEV_MODE не включился."
    echo "Проверьте APP_URL в .env: при https-значении config.py гасит флаг."
    exit 1
fi
echo "GET /dev: 200"

# Каждый пуш в develop собирает образ заново, а машина общая ещё с двумя
# проектами — без уборки повисшие слои съедают диск за несколько недель.
# Удаляются только dangling-образы: точки отката прода помечены тегами и
# под фильтр не попадают.
docker image prune -f >/dev/null 2>&1 || true

echo "=== Готово. На стенде $(git rev-parse --short HEAD)"
echo "Туннель с ноутбука: ssh -N -L 8002:127.0.0.1:8002 app01 → http://localhost:8002/dev"
