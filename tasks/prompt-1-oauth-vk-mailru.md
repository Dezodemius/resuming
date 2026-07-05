# Задача: добавить вход через VK ID и Mail.ru ID

Ты работаешь в репозитории сайта **Резюмирую.рф** (FastAPI + SQLite + Jinja2). Вход через Яндекс ID уже реализован — твоя задача добавить два новых OAuth-провайдера **по точно такому же образцу**: VK ID и Mail.ru ID.

## Правила работы с кодом (обязательно)

- `main.py` большой — **не читай его целиком**. Сначала Grep по имени функции/маршрута, потом читай только нужный диапазон строк.
- Образец для копирования: `auth_yandex_start` и `auth_yandex_callback` в `main.py` (строки ~503–565). Прочитай их полностью и повторяй их структуру, стиль логов и обработку ошибок один в один.
- Вспомогательные функции уже есть, не пиши свои: `_upsert_user_by_email(db, email)`, `_create_session(db, user_id)`, `_set_session_cookie(r, sid)` — найди их Grep'ом и используй.

## Шаг 1. Конфигурация

В `config.py` рядом с `YANDEX_CLIENT_ID` добавь:

```python
VK_CLIENT_ID         = os.getenv("VK_CLIENT_ID", "")
VK_CLIENT_SECRET     = os.getenv("VK_CLIENT_SECRET", "")
MAILRU_CLIENT_ID     = os.getenv("MAILRU_CLIENT_ID", "")
MAILRU_CLIENT_SECRET = os.getenv("MAILRU_CLIENT_SECRET", "")
```

Затем синхронно обнови (это обязательное правило проекта):
1. `.env.example` — добавь эти 4 переменные с пустыми значениями и комментарием, по образцу YANDEX_*.
2. `CLAUDE.md` — в таблицу «Key env vars» добавь строки для VK и Mail.ru.
3. `docker-compose.yml` трогать не нужно — приложение читает `env_file: .env` целиком.

В `main.py` найди строку импорта из config, где перечислен `YANDEX_CLIENT_ID` (строка ~41), и добавь новые переменные в этот же импорт.

## Шаг 2. VK ID (OAuth 2.1 с PKCE)

VK ID использует **новый** протокол на `id.vk.com` (НЕ старый oauth.vk.com — он не работает). Отличия от Яндекса: обязателен PKCE и параметр `device_id`.

`GET /auth/vk` — старт:
1. Если `VK_CLIENT_ID` пуст — `HTTPException(503, "Вход через VK не настроен")`.
2. Сгенерируй `state = str(uuid.uuid4())` и PKCE:
   ```python
   code_verifier  = secrets.token_urlsafe(64)
   code_challenge = base64.urlsafe_b64encode(
       hashlib.sha256(code_verifier.encode()).digest()
   ).decode().rstrip("=")
   ```
3. Redirect 302 на `https://id.vk.com/authorize` с параметрами: `response_type=code`, `client_id`, `redirect_uri={APP_URL}/auth/vk/callback`, `state`, `code_challenge`, `code_challenge_method=S256`, `scope=email`.
4. Положи в cookie `vk_state` и `vk_verifier` (оба: `max_age=600, httponly=True, samesite="lax", secure=APP_URL.startswith("https")` — как `ya_state` у Яндекса).

`GET /auth/vk/callback` — параметры `code`, `state`, `device_id`, `error` (все со значением по умолчанию `""`):
1. При ошибке/пустом code/несовпадении state с cookie — redirect на `/?auth_error=vk` (303), с warning-логом как у Яндекса.
2. Обмен кода (POST `https://id.vk.com/oauth2/auth`, form-data):
   `grant_type=authorization_code`, `code`, `code_verifier` (из cookie), `client_id`, `client_secret`, `device_id` (из query!), `redirect_uri={APP_URL}/auth/vk/callback`, `state`.
   Из ответа возьми `access_token`.
3. Данные пользователя (POST `https://id.vk.com/oauth2/user_info`, form-data): `access_token`, `client_id`. Ответ: `{"user": {"user_id": ..., "first_name": ..., "last_name": ..., "email": ...}}`.
4. Если email в ответе нет (у VK-аккаунта может не быть почты) — лог `log.error` и redirect `/?auth_error=vk`.
5. Дальше как у Яндекса: `_upsert_user_by_email`, установка `display_name` из `first_name + " " + last_name` (только если текущее display_name пустое или равно локальной части email), `_create_session`, redirect `/?login=success`, удалить cookies `vk_state`/`vk_verifier`, `_set_session_cookie`.

Проверь, что `secrets`, `base64`, `hashlib` импортированы в `main.py` (Grep), добавь недостающие импорты.

## Шаг 3. Mail.ru ID

Классический OAuth без PKCE — почти копия Яндекса.

`GET /auth/mailru` — старт: redirect на `https://oauth.mail.ru/login` с `client_id`, `response_type=code`, `scope=userinfo`, `redirect_uri={APP_URL}/auth/mailru/callback`, `state`. Cookie `mr_state` (тот же набор атрибутов).

`GET /auth/mailru/callback`:
1. Проверки error/code/state → `/?auth_error=mailru`.
2. Токен: POST `https://oauth.mail.ru/token`, form-data: `client_id`, `client_secret`, `grant_type=authorization_code`, `code`, `redirect_uri`.
3. Юзеринфо: GET `https://oauth.mail.ru/userinfo?access_token=<token>`. В ответе поля `email`, `name`, `nickname`.
4. Email обязателен (у Mail.ru он есть всегда, но проверяй как у Яндекса). Имя — `name` или `nickname`.
5. Дальше стандартно: upsert, сессия, redirect `/?login=success`.

## Шаг 4. Кнопки в модалке входа

Модалка входа есть только в `templates/index.html` (строки ~360–391). Сейчас там кнопка `.btn-yandex` под флагом `{% if yandex_enabled %}`.

1. В `main.py` найди обработчик `root()` (строка ~322): рядом с `"yandex_enabled": bool(YANDEX_CLIENT_ID)` добавь `"vk_enabled": bool(VK_CLIENT_ID)` и `"mailru_enabled": bool(MAILRU_CLIENT_ID)`.
2. В `index.html` после кнопки Яндекса добавь по тому же шаблону (ссылка + бейдж + `appLog('auth', ...)` + `auth-divider`):
   - `{% if vk_enabled %}` → `<a href="/auth/vk" class="btn-vk">` с бейджем «VK», фон кнопки `#0077FF`, белый текст: «Войти через VK ID»;
   - `{% if mailru_enabled %}` → `<a href="/auth/mailru" class="btn-mailru">` с бейджем «@», фон `#005FF9`, белый текст: «Войти через Mail.ru».
3. CSS: найди в `index.html` стили `.btn-yandex` и рядом добавь `.btn-vk` и `.btn-mailru` с той же геометрией (размеры, радиусы, отступы), меняя только цвета. Раздели `auth-divider` «или» так, чтобы он не дублировался между соседними кнопками (один разделитель между блоком OAuth-кнопок и email-формой, кнопки между собой идут просто с отступом).

## Шаг 5. Тесты и проверка

1. Прочитай `tests/test_auth.py` — если там есть тесты Яндекс-OAuth, добавь зеркальные для VK и Mail.ru (минимум: старт возвращает 302 на правильный хост и ставит state-cookie; при пустом CLIENT_ID — 503; callback с несовпадающим state — redirect на `/?auth_error=...`).
2. Запусти `python -m pytest -q` — все тесты должны пройти. Если что-то падает — чини, а не пропускай.
3. Быстрый smoke: `python -c "import main"` без ошибок.

## Шаг 6. Коммит

Закоммить и запушь в текущую ветку (подтверждения не нужны):

```
feat(auth): вход через VK ID и Mail.ru ID

Co-Authored-By: Claude <noreply@anthropic.com>
```

## Чего НЕ делать

- Не рефактори существующий код Яндекса/Telegram/magic-link.
- Не добавляй новые зависимости в requirements.txt — httpx уже есть.
- Не трогай `db.py`, схему БД и платёжный код.
- Не выдумывай другие эндпоинты VK/Mail.ru — используй ровно те URL, что указаны выше.
