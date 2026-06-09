# Резюме.ИИ

AI-генератор резюме: адаптирует резюме под конкретную вакансию, хранит версии по компаниям, экспортирует в PDF.

## Стек

- **Backend**: Python, FastAPI, SQLite
- **AI**: Ollama (локально) — `qwen2.5:14b` по умолчанию
- **Платежи**: ЮKassa
- **Авторизация**: Telegram Login Widget + Email magic link

## Быстрый старт

```bash
git clone https://github.com/Dezodemius/resuming.git
cd resuming
cp .env.example .env
# Заполни .env своими ключами
docker build -t resuming .
docker run -d --name resuming -p 80:8000 --env-file .env resuming
```

## Конфигурация (.env)

```
OLLAMA_MODEL=qwen2.5:14b
TELEGRAM_BOT_TOKEN=...
TELEGRAM_BOT_NAME=...
SMTP_HOST=smtp.yandex.ru
SMTP_USER=...
SMTP_PASS=...
YOKASSA_SHOP_ID=...
YOKASSA_SECRET_KEY=...
APP_URL=https://your-domain.com
SECRET_KEY=...  # python -c "import secrets; print(secrets.token_hex(32))"
```

## Страницы

| Маршрут | Описание |
|---|---|
| `/` | Генератор резюме (главная) |
| `/resumes` | Дашборд всех резюме |
| `/resumes/:id` | Редактор резюме |
| `/settings` | Настройки и подписка |
| `/pricing` | Тарифы |
| `/offer` | Публичная оферта |
| `/privacy` | Политика конфиденциальности |
| `/contacts` | Контакты |

## Лицензия

[AGPL-3.0](LICENSE) © 2025 Гладков Егор Сергеевич (Dezodemius)
