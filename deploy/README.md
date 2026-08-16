# Деплой на стенд timeweb.cloud

Стенд — отдельная копия сайта на VPS timeweb, открывается **по IP**, без домена
и TLS. Прод (`резюмирую.рф`) деплоится иначе — через GitHub Actions на
self-hosted раннер, `.github/workflows/ci_cd.yml`. Эти два пути не пересекаются.

Что поднимается на стенде: `app` (FastAPI) + `nginx`. **Ollama на стенде нет** —
приложение ходит на внешний `OLLAMA_URL` из `.env` стенда. `ops-mcp` тоже не
поднимается: он монтирует `docker.sock`, а это root на хосте.

## Первый запуск

1. Создай VPS в timeweb (Ubuntu 22.04/24.04, 2 ГБ RAM хватает — модель крутится
   не здесь) и добавь свой SSH-ключ при создании.

2. Заполни `.env` стенда:

```bash
cp deploy/.env.staging.example deploy/.env.staging
```

   Обязательно: `OLLAMA_URL` (внешний адрес Ollama), `APP_URL=http://<IP стенда>`,
   `SECRET_KEY`. `METRIKA_ID` оставь пустым, ключи ЮKassa — тестовые или пустые.

3. Задай доступы и выкатывай:

```bash
export DEPLOY_HOST=1.2.3.4
export DEPLOY_SSH_KEY=~/.ssh/timeweb_stage
./deploy/deploy.sh
```

Первый запуск сам поставит docker (`get.docker.com`) — это добавит пару минут.
Дальше скрипт кладёт код, поднимает контейнеры, ждёт healthcheck и проверяет
`/healthz` снаружи.

Из PowerShell — та же логика через обёртку:

```powershell
$env:DEPLOY_HOST = "1.2.3.4"; $env:DEPLOY_SSH_KEY = "$env:USERPROFILE\.ssh\timeweb_stage"; .\deploy\deploy.ps1
```

## Повседневное

```bash
./deploy/deploy.sh                 # выкатить текущее рабочее дерево
./deploy/deploy.sh --committed     # выкатить HEAD, без локальных правок
./deploy/deploy.sh --status        # docker compose ps
./deploy/deploy.sh --logs 100      # логи контейнеров
./deploy/deploy.sh --restart
./deploy/deploy.sh --down          # остановить (volume с базой остаётся)
./deploy/deploy.sh --ssh           # шелл на стенде
./deploy/deploy.sh --dry-run       # проверить настройки, не трогая код на сервере
```

Полный список флагов — `./deploy/deploy.sh --help`.

## Переменные

Приоритет: переменные окружения → `deploy/.deploy.env` → значения по умолчанию.
Файл `deploy/.deploy.env` (в `.gitignore`) удобен, чтобы не экспортировать
каждый раз:

```bash
DEPLOY_HOST=1.2.3.4
DEPLOY_SSH_KEY=/c/Users/gladk/.ssh/timeweb_stage
STAGING_AUTH_USER=stage
STAGING_AUTH_PASSWORD=длинный-пароль
```

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `DEPLOY_HOST` | — | IP стенда (обязательно) |
| `DEPLOY_SSH_KEY` | — | приватный SSH-ключ (обязательно) |
| `DEPLOY_USER` | `root` | пользователь SSH |
| `DEPLOY_PORT` | `22` | порт SSH |
| `DEPLOY_PATH` | `/opt/resuming` | каталог на сервере |
| `STAGING_ENV_FILE` | `deploy/.env.staging` | локальный `.env` стенда |
| `STAGING_HTTP_PORT` | `80` | внешний порт nginx |
| `STAGING_AUTH_USER` / `STAGING_AUTH_PASSWORD` | пусто | basic-auth поверх стенда |
| `SSH_STRICT_HOST_KEY` | `accept-new` | режим проверки host key |

## Что где лежит на сервере

```
/opt/resuming/
  .env            секреты стенда (0600), переживают передеплой
  src/            код; каждый деплой заменяет каталог целиком
  src.old/        предыдущая версия — из неё идёт автооткат
  src.failed/     версия, которая не поднялась (остаётся для разбора)
```

База — в docker volume `resuming-staging_app_data`, `--down` её не удаляет.
Снести вместе с данными: `docker compose -f /opt/resuming/src/deploy/docker-compose.staging.yml down -v`.

## Доступ и безопасность

Стенд по умолчанию открыт всем, кто знает IP, и содержит рабочую копию сайта
с админкой. Пароль включается двумя переменными:

```bash
export STAGING_AUTH_USER=stage
export STAGING_AUTH_PASSWORD='длинный-пароль'
./deploy/deploy.sh
```

`/healthz`, `/readyz` и `/robots.txt` остаются без пароля — иначе деплой-скрипт
получал бы 401 вместо статуса. Убрать пароль — очистить переменные и выкатить
заново.

Прочее, что учтено в конфиге стенда:

- `X-Robots-Tag: noindex` и `robots.txt` с `Disallow: /` — стенд не должен
  попасть в поиск;
- nginx перетирает `CF-Connecting-IP` реальным адресом соединения. Перед
  стендом нет Cloudflare, а приложение берёт из этого заголовка ключ
  rate-limit — иначе лимиты обходились бы подделкой заголовка;
- `deploy.sh` отказывается заливать `.env` с боевым ключом ЮKassa (`live_…`);
  осознанно — флаг `--allow-live-keys`.

## Если что-то пошло не так

**Деплой упал на healthcheck.** Скрипт сам вернул предыдущую версию и оставил
сломанную в `/opt/resuming/src.failed`. Логи: `./deploy/deploy.sh --logs 100`.

**`readyz: degraded`, `"ollama": false`.** Стенд не видит внешнюю Ollama.
Проверь `OLLAMA_URL` в `deploy/.env.staging`, что на той машине Ollama слушает
`0.0.0.0` и порт 11434 открыт для IP стенда, и что `OLLAMA_MODEL` совпадает с
реально загруженной моделью. Сайт при этом работает — не отвечают только
генерации.

**Снаружи `/healthz` не открывается, а контейнер healthy.** Скорее всего
фаервол в панели timeweb: открой TCP 80 (или `STAGING_HTTP_PORT`).

**`Permission denied (publickey)`.** Ключ не тот или не добавлен на сервер:
`ssh -i <ключ> root@<IP>` руками. Скрипт сам копирует ключ во временный файл с
правами 600, так что права на исходный файл значения не имеют.
