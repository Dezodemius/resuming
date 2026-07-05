# Задача: промокоды, админ-страница, журнал использования + защита монетизации

Сайт **Резюмирую.рф** (FastAPI + SQLite + Jinja2). Монетизация: `free_left` (3 бесплатных генерации), `paid_left` (пачки), `is_pro` + `pro_expires_at` (подписка через ЮKassa). Нужны четыре вещи:

1. **Промокоды** — чтобы владелец и тестеры пользовались продуктом без оплаты.
2. **Админ-страница `/admin`** — создание кодов и статистика потребления.
3. **Журнал использования** — кто, когда и сколько генерирует; плюс Яндекс.Метрика на страницы.
4. **Проверка защиты монетизации** — чтобы лимиты нельзя было обойти («заскамить продукт»).

## Правила работы с кодом (обязательно)

- `main.py` большой — **не читай целиком**: Grep по имени функции, затем читай нужный диапазон.
- Ключевые места: `_deduct`/`_refund` (~строки 620–650), их вызовы (~885, 915, 941, 1205), вебхук ЮKassa `payment_webhook` (~1082), `get_current_user`, `root()` (~322). Схема БД — в `db.py` (маленький, можно целиком).
- Стиль: одинарные функции, `log.info/warning/error` с префиксом ручки, ответы об ошибках на русском — смотри соседний код и копируй манеру.

## Шаг 1. Схема БД (db.py)

В `init_db()` в `executescript` добавь (все — `CREATE TABLE IF NOT EXISTS`, существующие таблицы не менять):

```sql
CREATE TABLE IF NOT EXISTS promo_codes (
    code       TEXT PRIMARY KEY,              -- хранится в UPPERCASE
    kind       TEXT NOT NULL CHECK (kind IN ('pro_days','gen_pack','unlimited')),
    value      INTEGER NOT NULL DEFAULT 0,    -- дни для pro_days / штук для gen_pack
    max_uses   INTEGER NOT NULL DEFAULT 1,
    used_count INTEGER NOT NULL DEFAULT 0,
    active     INTEGER NOT NULL DEFAULT 1,
    expires_at TEXT,                          -- NULL = бессрочный
    comment    TEXT,
    created    TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS promo_activations (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    code    TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    created TEXT DEFAULT (datetime('now')),
    UNIQUE(code, user_id)
);
CREATE TABLE IF NOT EXISTS usage_events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    anon_id TEXT,
    event   TEXT NOT NULL,
    meta    TEXT,                             -- JSON-строка
    created TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_events_event_created ON usage_events(event, created);
CREATE INDEX IF NOT EXISTS idx_events_user_created  ON usage_events(user_id, created);
```

## Шаг 2. Конфигурация

В `config.py`:

```python
ADMIN_EMAILS = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
METRIKA_ID   = os.getenv("METRIKA_ID", "")
```

Обнови синхронно (обязательное правило проекта): `.env.example` (обе переменные с комментариями: ADMIN_EMAILS — «email админов через запятую, им доступен /admin»; METRIKA_ID — «номер счётчика Яндекс.Метрики, пусто = выключено») и таблицу «Key env vars» в `CLAUDE.md`. `docker-compose.yml` не трогать — он читает `env_file: .env`.

## Шаг 3. Журнал событий

В `main.py` добавь хелпер:

```python
def log_event(db, event: str, user_id=None, anon_id=None, **meta):
    db.execute("INSERT INTO usage_events (user_id, anon_id, event, meta) VALUES (?,?,?,?)",
               (user_id, anon_id, event, json.dumps(meta, ensure_ascii=False) if meta else None))
```

Вызови его (внутри уже открытых `with get_db() as db` блоков, отдельных commit не нужно — контекст-менеджер коммитит):
- в 4 генерирующих ручках после успешной генерации: `event="generate"`, meta: `kind` (какая ручка), `col` (free/paid — возвращает `_deduct`); при ошибке AI (там, где сейчас `_refund`) — `event="generate_fail"`;
- в анонимном превью (Grep `anon_usage`) — `event="anon_preview"`, `anon_id=...`;
- во всех login-обработчиках после успешного входа (Telegram, magic-link, Яндекс; если в коде уже есть VK/Mail.ru — тоже): `event="login"`, meta `method`;
- в вебхуке ЮKassa при выдаче Pro: `event="payment"`, meta `pay_id`;
- при активации промокода (шаг 4): `event="promo_activate"`, meta: `code_prefix` — **только первые 4 символа кода**, целиком код в журнал не писать.

Проверь, что `json` импортирован.

## Шаг 4. Активация промокода

`POST /api/promo/activate`, тело `{"code": "..."}` (pydantic-модель по образцу соседних):

1. Требуется авторизация (`get_current_user`, иначе 401 «Войдите в аккаунт»).
2. Rate limit: в проекте есть обёртка над slowapi (Grep `limiter` в main.py, строки ~27–110) — повесь лимит `5/minute` тем же способом, каким он повешен на другие ручки (если декоратор-заглушка — просто используй её).
3. Нормализуй: `code = body.code.strip().upper()`.
4. **Атомарное** списание использования — одним UPDATE, никаких SELECT-потом-UPDATE:
   ```sql
   UPDATE promo_codes SET used_count = used_count + 1
   WHERE code = ? AND active = 1 AND used_count < max_uses
     AND (expires_at IS NULL OR expires_at > datetime('now'))
   ```
   Если `rowcount == 0` — ответ 400 с **одинаковым** текстом «Код недействителен» (не раскрывать, существует код, исчерпан или просрочен — это защита от перебора).
5. `INSERT INTO promo_activations` — при `sqlite3.IntegrityError` (повторная активация тем же юзером) откати счётчик (`used_count = used_count - 1`) и верни 400 «Код уже активирован».
6. Применение (прочитай `kind`, `value` кода):
   - `pro_days`: `is_pro=1`, `pro_expires_at = max(текущее, now) + value дней` (продление, не перезапись; формат даты — как в вебхуке ЮKassa, посмотри там);
   - `gen_pack`: `paid_left = paid_left + value`;
   - `unlimited`: `is_pro=1`, `pro_expires_at = '2099-12-31 00:00:00'`.
7. `log_event(..., "promo_activate", ...)`; ответ `{"ok": True, "kind": ..., "message": "..."}` с человеческим текстом («Pro до …», «+20 генераций», «Безлимит активирован»).

**UI**: в `templates/settings.html` (страница настроек, старый дизайн — просто соответствуй его стилю, карточки `.usage-card`) добавь карточку «Промокод»: поле ввода + кнопка «Активировать», fetch на `/api/promo/activate`, вывод результата через существующий `toast(...)` и перезагрузка данных страницы после успеха.

## Шаг 5. Админка

1. Хелпер:
   ```python
   def _require_admin(user):
       if not user or (user["email"] or "").lower() not in ADMIN_EMAILS:
           raise HTTPException(404)   # 404, а не 403 — не раскрываем существование админки
   ```
   Вызывай его **в каждой** админ-ручке, первой строкой после `get_current_user`.
2. Ручки:
   - `GET /admin` — рендер `templates/admin.html` (неадмину — 404);
   - `POST /api/admin/promo` — создать код. Вход: `kind`, `value`, `max_uses`, `expires_at` (опц.), `comment` (опц.). Код генерируй сам: 12 символов из алфавита `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (без похожих 0/O/1/I/L) через `secrets.choice`, формат `XXXX-XXXX-XXXX`. Верни созданный код;
   - `GET /api/admin/promo` — список кодов со счётчиками активаций;
   - `POST /api/admin/promo/deactivate` — `{"code": ...}` → `active=0`;
   - `GET /api/admin/stats` — JSON со сводкой из `usage_events` + `users` + `payments`:
     - генерации по дням за 30 дней (`generate` и `generate_fail` отдельно),
     - топ-10 пользователей по генерациям за 30 дней (id, email/display_name, счёт),
     - всего пользователей / новых за 7 дней,
     - входы по методам за 30 дней,
     - успешные оплаты за 30 дней,
     - активации промокодов за 30 дней,
     - текущие Pro-пользователи (count).
3. `templates/admin.html` — в стиле нового дизайна приложения (подключи `/static/app.css`, используй `_app_header.html`/`_app_footer.html` как в `resumes.html` — посмотри, как он собран). Содержимое: две секции —
   - «Промокоды»: форма создания (селект типа, число, лимит активаций, срок, комментарий) + таблица кодов с кнопкой «Отключить»;
   - «Потребление»: карточки-цифры (пользователи, Pro, генерации за 30 дней, оплаты) + таблица по дням + топ пользователей. Простые CSS-бары для графика по дням, **без внешних JS-библиотек**.

## Шаг 6. Яндекс.Метрика

1. Создай `templates/_metrika.html` со стандартным сниппетом счётчика Метрики, обёрнутым в `{% if metrika_id %}`, с `{{ metrika_id }}` вместо номера (tag-версия `mc.yandex.ru/metrika/tag.js`, вызов `ym({{ metrika_id }}, "init", {clickmap:true, trackLinks:true, accurateTrackBounce:true})` + `<noscript>`-пиксель).
2. Grep `Jinja2Templates` в `main.py` — после создания объекта шаблонов задай глобаль: `tpl.env.globals["metrika_id"] = METRIKA_ID` (тогда не нужно менять контексты всех ручек).
3. Подключи `{% include '_metrika.html' %}` перед `</head>` (или в конце `<body>`) в: `index.html`, `resumes.html`, `pricing.html`, `settings.html`, `resume_edit.html`, `_legal_base.html`. На `admin.html` — НЕ подключать.

## Шаг 7. Проверка защиты монетизации (сделать, а не только проверить)

Пройди по списку; где утверждение не выполняется — почини минимальной правкой:

1. **_deduct** (~строка 622): списание должно быть условным UPDATE (`... SET free_left = free_left - 1 WHERE id = ? AND free_left > 0` или эквивалент), а не «прочитал-проверил-записал» без условия в UPDATE. Если там гонка — укрепи.
2. **Вебхук ЮKassa** (~строка 1082): статус платежа уже перепроверяется прямым запросом к API ЮKassa. Убедись, что также сверяется **сумма** (`amount.value` из ответа API == ожидаемой цене из `config.PRO_PRICE`) — если нет, добавь сверку и warning-лог при расхождении.
3. **Админ-ручки**: `_require_admin` стоит в каждой; ответ неадмину — 404; список `ADMIN_EMAILS` сравнивается в lower-case.
4. **Промокоды**: активация атомарна (шаг 4.4), сообщение об ошибке одинаковое, rate limit стоит, код нигде не логируется целиком (ни в `log.*`, ни в usage_events).
5. **IDOR**: Grep всех SQL по таблице `resumes` в main.py — каждый запрос от имени пользователя должен содержать `AND user_id=?`. Аналогично `profiles`. Найдёшь дыру — почини.
6. **Анонимный лимит**: `anon_id` подписан HMAC (Grep `SECRET_KEY` / `anon_id`) — убедись, что cookie без валидной подписи отвергается, а не создаёт новый счётчик с нулём.

Результат проверки опиши в сообщении после коммита: пункт → «ок» или «починил: что именно».

## Шаг 8. Тесты и коммит

1. Новый файл `tests/test_promo.py` (посмотри, как существующие тесты в `tests/` подменяют БД — `db.py` читает `config.DB_PATH` в момент вызова, тесты используют monkeypatch): активация каждого типа кода; повторная активация → 400; исчерпанный/просроченный/неактивный → 400 с тем же текстом; `/admin` и `/api/admin/*` для неадмина → 404; создание кода админом → активация работает; `/api/admin/stats` → 200 и ожидаемые ключи.
2. `python -m pytest -q` — зелёный. `python -c "import main"` — без ошибок.
3. Закоммить и запушь в текущую ветку (подтверждения не нужны):

```
feat: промокоды, админка /admin, журнал использования и Метрика

Co-Authored-By: Claude <noreply@anthropic.com>
```

## Чего НЕ делать

- Не менять существующие таблицы и логику `_deduct`/`_refund` сверх пунктов шага 7.
- Не добавлять зависимости в `requirements.txt`.
- Не строить отдельную систему «лицензий» — `unlimited`-код и есть лицензия.
- Не подключать внешние JS-библиотеки на админку.
- Не хранить email админов в коде — только через env `ADMIN_EMAILS`.
