# Деплой

Три независимых контура. Ниже по порядку: прод, дев-стенд, стенд на отдельном VPS.

| | Прод | Дев-стенд | Стенд |
|---|---|---|---|
| Что | `резюмирую.рф` | копия `develop` для своих тестов | копия по IP, без домена и TLS |
| Где | app-01 (Timeweb), `/srv/apps/resuming` | app-01, `/srv/apps/resuming-dev` | отдельный VPS, `/opt/resuming` |
| Чем | `ci_cd.yml` → SSH → `deploy/deploy-prod.sh` | `ci_cd.yml` → SSH → `deploy/deploy-dev.sh` | `deploy/deploy.sh` с ноутбука |
| Compose | `deploy/docker-compose.prod.yml` | `deploy/docker-compose.dev.yml` | `deploy/docker-compose.staging.yml` |
| Что поднимается | `app`, на `127.0.0.1:8001` | `app`, на `127.0.0.1:8002` | `app` + `nginx` |
| Кто держит домен | **хостовой** nginx машины, общий с двумя другими проектами | никто — вход по SSH-туннелю | свой nginx в compose |

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

### Админка

`/admin` (и `/api/admin/*`) закрыта двумя проверками: адрес запроса должен
попадать в `ADMIN_IPS`, а почта аккаунта — быть в `ADMIN_EMAILS`. Любой отказ
отдаётся как 404, поэтому снаружи страница неотличима от несуществующей. Пустой
`ADMIN_IPS` означает «не ограничивать по адресам» — на стенде и локально так и
надо, на проде переменную стоит заполнить.

Адрес берётся из `X-Real-IP`, который host-nginx ставит сам и затирает
клиентский, — подделать его снаружи нельзя. Отсюда два пути внутрь.

**С публичного сайта.** Впишите в `ADMIN_IPS` свой внешний адрес и перезапустите
app. Годится, если адрес статический.

**Через SSH-туннель.** Порт приложения опубликован на `127.0.0.1:8001` и наружу
не смотрит; пробрасываем его к себе:

```bash
ssh -N -L 8081:127.0.0.1:8001 app01
```

Дальше — `http://localhost:8081/admin`. Запрос приходит прямо в сокет, минуя
Cloudflare и host-nginx, `X-Real-IP` в нём нет, и приложение видит адрес
docker-шлюза — поэтому в `ADMIN_IPS` нужна запись `172.16.0.0/12,127.0.0.0/8`.
Пускает сюда SSH-ключ, домашний адрес может быть любым; для динамического IP
это основной способ.

Сессия на `localhost:8081` своя — cookie боевого домена туда не поедет, войти
надо один раз изнутри туннеля (дальше cookie живёт 30 дней):

1. открыть `http://localhost:8081/` и запросить вход по почте;
2. в письме ссылка ведёт на боевой домен — скопировать из неё `token` и открыть
   `http://localhost:8081/auth/email/verify?token=…`.

Вход через Яндекс/VK/Mail.ru в туннеле не работает: `redirect_uri` у провайдеров
зарегистрирован на боевой домен.

## Дев-стенд на app-01

Копия приложения из ветки `develop`, поднятая на той же машине рядом с продом и
намеренно невидимая снаружи. Смысл — гонять платные сценарии, ничего не покупая:
на стенде включён `DEV_MODE=1`, и страница `/dev` выдаёт вход по любой почте и
любой тариф одной кнопкой. Промокоды и тестовый магазин Робокассы для этого
больше не нужны.

Пуш в `develop` → джоба `deploy-dev` в `.github/workflows/ci_cd.yml` → SSH на
app-01 → обёртка приводит `/srv/apps/resuming-dev` к `origin/develop` и
запускает `deploy/deploy-dev.sh`. Скрипт пересобирает образ, ждёт healthcheck и
проверяет изнутри машины `/healthz` и `/dev` — последнее заодно доказывает, что
`DEV_MODE` реально включился, а не был погашен проверкой `APP_URL`.

Чем контуры разведены:

| | Прод | Дев-стенд |
|---|---|---|
| Каталог | `/srv/apps/resuming` | `/srv/apps/resuming-dev` |
| Ветка | `main` | `develop` |
| Compose-проект | `resuming` | `resuming-dev` |
| Том с базой | `resuming_app_data` | `resuming-dev_app_data` |
| Контейнер | `resuming-app` | `resuming-dev-app` |
| Порт | `127.0.0.1:8001` | `127.0.0.1:8002` |
| Ключ выката | `SSH_KEY` | `SSH_DEV_KEY` |
| `DEV_MODE` | выключен и не включается | `1` |

Общего у них — только машина и внешний AI-провайдер. Базы разные (разные тома),
`.env` разные, образы разные. Данные прода стенду недоступны физически.

### Разовая настройка

1. **Каталог и `.env`.** Отдельный клон, не копия боевого дерева:

   ```bash
   ssh app01 'git clone -b develop https://github.com/Dezodemius/resuming.git /srv/apps/resuming-dev'
   ```

   Затем заполнить `/srv/apps/resuming-dev/.env` по образцу
   `deploy/.env.dev.example`. Боевой `.env` копировать нельзя: стенд начал бы
   слать письма и дёргать Робокассу от лица прода. Обязательный минимум —
   `DEV_MODE=1`, `APP_URL=http://localhost:8002`, свой `SECRET_KEY`, ключ
   AI-провайдера. `deploy-dev.sh` откажется работать, если `APP_URL` начинается
   с `https` или если `DEV_MODE=1` в файле нет.

2. **Ключ.** Отдельная пара — не та, что у прода: forced-command привязан к
   ключу, и одним ключом два разных выката не сделать.

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/resuming_ci_dev -C "resuming-ci-deploy-dev" -N ""
   ```

3. **Обёртка на сервере:**

   ```bash
   ssh app01 'cat > /usr/local/sbin/resuming-deploy-dev <<"EOF"
   #!/bin/bash
   set -euo pipefail
   cd /srv/apps/resuming-dev
   git fetch --quiet origin develop
   git reset --hard --quiet origin/develop
   exec bash deploy/deploy-dev.sh
   EOF
   chmod 700 /usr/local/sbin/resuming-deploy-dev'
   ```

   `git reset --hard` не трогает `.env` — он в `.gitignore`.

4. **Публичный ключ с ограничением** — строкой в `/root/.ssh/authorized_keys`
   на app-01, рядом с боевым:

   ```
   command="/usr/local/sbin/resuming-deploy-dev",no-agent-forwarding,no-port-forwarding,no-pty,no-user-rc,restrict ssh-ed25519 AAAA... resuming-ci-deploy-dev
   ```

5. **Секрет репозитория** — один новый, остальное переиспользуется:

   ```bash
   gh secret set SSH_DEV_KEY < ~/.ssh/resuming_ci_dev
   ```

   Пока секрета нет, джоба `deploy-dev` не падает, а пропускает выкат с
   предупреждением: пуш в `develop` — обычная рабочая операция, и красить её в
   красное из-за ненастроенного стенда неправильно.

### Как заходить

Стенд слушает `127.0.0.1:8002` — снаружи его нет. Пробрасываем порт к себе:

```powershell
./deploy/dev-tunnel.ps1
```

То же самое руками: `ssh -N -L 8002:127.0.0.1:8002 app01`. Дальше —
`http://localhost:8002/dev`. Пускает внутрь обычный SSH-ключ, поэтому смена
внешнего адреса (в том числе включённый VPN) ничего не ломает.

Вход через Яндекс/VK/Mail.ru в туннеле не работает — `redirect_uri` у
провайдеров зарегистрирован на боевой домен. На стенде для этого и есть `/dev`.

### Если нужен доступ прямо по адресу, без туннеля

Например, чтобы открыть стенд с телефона. Тогда `DEV_BIND` в `.env` стенда
меняется на `0.0.0.0`, а порт закрывается файрволом на конкретный адрес:

```bash
ssh app01 'ufw allow from <ваш-внешний-адрес> to any port 8002 proto tcp'
```

Порядок именно такой: сначала правило `ufw`, потом `DEV_BIND`. Иначе между
перезапуском контейнера и правилом остаётся окно, в котором `/dev` открыт всему
интернету — то есть кто угодно заходит любым аккаунтом. И помните, что внешний
адрес меняется вместе с VPN и перезагрузкой роутера: правило придётся
переписывать, а туннель — нет.

### Что даёт `DEV_MODE`

| | |
|---|---|
| `GET /dev` | пульт: поле почты и кнопки тарифов |
| `POST /api/dev/login` | сессия по одной почте, без письма и OAuth |
| `POST /api/dev/grant` | `pro` / `pack` / `free` / `empty` / `reset_usage` |

`free` возвращает аккаунт в состояние новичка, `empty` обнуляет счётчики (так
проверяется пейволл), `reset_usage` чистит `usage_events` — иначе квоту Pro не
отмотать, она считается по событиям за окно, а не декрементом.

Выключенный `DEV_MODE` отдаёт на все три 404, а не 403: снаружи ручек не видно
вовсе. Плюс `config.py` гасит флаг сам, если `APP_URL` начинается с `https`, —
боевой контур игнорирует переменную, даже если её принесёт случайно
скопированный `.env`, и пишет об этом `critical` в лог.

### Повседневное

```bash
ssh app01 'docker logs -f resuming-dev-app'
ssh app01 'cd /srv/apps/resuming-dev && docker compose -f deploy/docker-compose.dev.yml restart'
ssh app01 'cd /srv/apps/resuming-dev && docker compose -f deploy/docker-compose.dev.yml down'
```

Снести базу стенда и начать с чистого листа (прода не касается — том другой):

```bash
ssh app01 'cd /srv/apps/resuming-dev && docker compose -f deploy/docker-compose.dev.yml down -v && bash deploy/deploy-dev.sh'
```

## Стенд timeweb.cloud

Стенд — отдельная копия сайта на VPS timeweb, открывается **по IP**, без домена
и TLS.

Что поднимается на стенде: `app` (FastAPI) + `nginx`. **Ollama на стенде нет** —
приложение ходит на внешний `OLLAMA_URL` из `.env` стенда.

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
