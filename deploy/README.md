# Деплой

Два независимых контура. Ниже сначала прод, потом стенд.

| | Прод | Стенд |
|---|---|---|
| Что | `резюмирую.рф` | копия по IP, без домена и TLS |
| Где | app-01 (Timeweb), `/srv/apps/resuming` | отдельный VPS, `/opt/resuming` |
| Чем | `ci_cd.yml` → SSH → `deploy/deploy-prod.sh` | `deploy/deploy.sh` с ноутбука |
| Compose | `deploy/docker-compose.prod.yml` | `deploy/docker-compose.staging.yml` |
| Что поднимается | `app` + `ops-mcp`, оба на `127.0.0.1` | `app` + `nginx` |
| Кто держит домен | **хостовой** nginx машины, общий с двумя другими проектами | свой nginx в compose |

Корневой `docker-compose.yml` — третий, самодостаточный вариант (с локальной
Ollama и собственным nginx на порту 80). **На app-01 его запускать нельзя**: он
займёт 80-й порт и уронит хостовой прокси, а с ним dndshing.ru и sgorelo.ru.

## Прод: как это работает

Пуш в `main` → джоба `deploy` в `.github/workflows/ci_cd.yml` (на
`ubuntu-latest`) → SSH на app-01 → серверная обёртка обновляет рабочее дерево до
`origin/main` и запускает `deploy/deploy-prod.sh` из свежего кода. Скрипт
помечает текущий образ как точку отката, пересобирает, ждёт healthcheck и
проверяет `/healthz` и что `POST /api/fetch-job` без сессии отдаёт 401. После
этого джоба отдельно проверяет сайт снаружи, через Cloudflare.

### Разовая настройка

1. **Ключ.** Сгенерировать отдельную пару только для выката (без пароля, иначе
   CI не сможет ей воспользоваться):

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/resuming_ci -C "resuming-ci-deploy" -N ""
   ```

2. **Обёртка на сервере.** Она нужна, чтобы ключ не давал шелл: forced-command
   в `authorized_keys` игнорирует любую присланную команду и запускает только её.

   ```bash
   ssh app01 'cat > /usr/local/sbin/resuming-deploy <<"EOF"
   #!/bin/bash
   set -euo pipefail
   cd /srv/apps/resuming
   git fetch --quiet origin main
   git reset --hard --quiet origin/main
   exec bash deploy/deploy-prod.sh
   EOF
   chmod 700 /usr/local/sbin/resuming-deploy'
   ```

3. **Публичный ключ с ограничением.** Дописать в `/root/.ssh/authorized_keys`
   на app-01 одной строкой:

   ```
   command="/usr/local/sbin/resuming-deploy",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,restrict ssh-ed25519 AAAA... resuming-ci-deploy
   ```

   Проверить, что шелл этим ключом не даётся:
   `ssh -i ~/.ssh/resuming_ci root@201.34.132.125 "id"` — должен пойти выкат, а не `id`.

4. **Секреты репозитория** (Settings → Secrets and variables → Actions):

   | Секрет | Значение | Обязателен |
   |---|---|---|
   | `SSH_HOST` | `201.34.132.125` | да |
   | `SSH_USER` | `root` | да |
   | `SSH_KEY` | содержимое приватного `~/.ssh/resuming_ci` целиком | да |
   | `SSH_KNOWN_HOSTS` | вывод `ssh-keyscan -H 201.34.132.125` | да |
   | `SSH_PORT` | порт SSH; без него подразумевается `22` | нет |

   Имена намеренно общие, а не `APP01_*`: те же секреты пригодятся, если
   прод переедет на другую машину. Обратная сторона — по имени не видно, на
   что они указывают, поэтому значение `SSH_HOST` стоит перепроверять при
   любой смене сервера.

   `SSH_KNOWN_HOSTS` не косметика: без него `BatchMode` откажется соединяться,
   а `StrictHostKeyChecking=no` открыл бы выкат для MITM.

   Через `gh`, чтобы не ходить в веб-интерфейс (ключ при этом остаётся на вашей
   машине и в терминал не печатается):

   ```bash
   gh secret set SSH_HOST --body "201.34.132.125"
   gh secret set SSH_USER --body "root"
   gh secret set SSH_KEY < ~/.ssh/resuming_ci
   ssh-keyscan -H 201.34.132.125 | gh secret set SSH_KNOWN_HOSTS
   ```

   Проверить, что все четыре на месте: `gh secret list`. Пока хотя бы одного
   нет, джоба `deploy` падает на первом же шаге с перечнем недостающих —
   прод при этом продолжает работать на прежней версии.

### Способы входа

Приложение при старте пишет в лог одну строку с тем, что реально включено:

```
Способы входа: email, telegram, yandex
Вход: ключи настроены (Яндекс), но OAUTH_LOGIN_ENABLED=0 — кнопки скрыты
```

Посмотреть на проде: `ssh app01 'docker logs resuming-app 2>&1 | grep "Способы входа" | tail -1'`.

Почта работает всегда (magic-ссылка). Остальное надо зарегистрировать у
провайдера и вписать в `/srv/apps/resuming/.env`. Домен везде указывается **в
punycode** — провайдеры сравнивают redirect_uri посимвольно, и кириллическая
запись даёт `invalid_grant`.

| Провайдер | Где регистрировать | Что вписать у провайдера | Переменные |
|---|---|---|---|
| Яндекс ID | [oauth.yandex.ru](https://oauth.yandex.ru/client/new) | Redirect URI `https://xn--e1aedprev8fe.xn--p1ai/auth/yandex/callback`, права `login:email` и `login:info` | `YANDEX_CLIENT_ID`, `YANDEX_CLIENT_SECRET` |
| VK ID | [id.vk.com](https://id.vk.com/about/business/go/docs/developer) | Redirect URI `https://xn--e1aedprev8fe.xn--p1ai/auth/vk/callback` | `VK_CLIENT_ID` (секрет не нужен, PKCE) |
| Mail.ru | [oauth.mail.ru](https://oauth.mail.ru) | Redirect URI `https://xn--e1aedprev8fe.xn--p1ai/auth/mailru/callback` | `MAILRU_CLIENT_ID`, `MAILRU_CLIENT_SECRET` |

Три последних вдобавок закрыты общим рубильником: пока `OAUTH_LOGIN_ENABLED=1`
не выставлен, кнопки скрыты, а `/auth/{yandex,vk,mailru}` отдают 503 — даже с
верными ключами. Так и задумано, но именно это чаще всего и забывают, поэтому
приложение пишет об этом предупреждение при старте.

После правки `.env` контейнер надо перезапустить, переменные читаются при
старте:

```bash
ssh app01 'cd /srv/apps/resuming && docker compose -f deploy/docker-compose.prod.yml up -d'
```

### Ручной выкат и откат

```bash
ssh app01 'cd /srv/apps/resuming && git fetch origin main && git reset --hard origin/main && bash deploy/deploy-prod.sh'
```

Скрипт перед пересборкой помечает текущий образ `resuming-app:rollback-<дата>`.
Откатиться на него:

```bash
ssh app01 'cd /srv/apps/resuming && docker tag resuming-app:rollback-<дата> resuming-app && docker compose -f deploy/docker-compose.prod.yml up -d'
```

Откат образом не откатывает код в рабочем дереве — если нужен и код, добавьте
`git reset --hard <коммит>` (предыдущий записан в `/root/resuming-deployed-commit.txt`).

### Осторожно с SSH

На app-01 включён `ufw limit 22/tcp`: частые переподключения начинают молча
отваливаться по таймауту, и это выглядит как недоступность сервера. Делайте
одну сессию на задачу и не ретраьте чаще, чем раз в 15–20 секунд.

## Стенд timeweb.cloud

Стенд — отдельная копия сайта на VPS timeweb, открывается **по IP**, без домена
и TLS.

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
   `SECRET_KEY`. `METRIKA_ID` оставь пустым, ключи Робокассы — тестовый магазин
   с `ROBOKASSA_TEST_MODE=1` или пустые.

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
- `deploy.sh` отказывается заливать `.env` с боевым `ROBOKASSA_LOGIN` без
  `ROBOKASSA_TEST_MODE=1`; осознанно — флаг `--allow-live-keys`.

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
