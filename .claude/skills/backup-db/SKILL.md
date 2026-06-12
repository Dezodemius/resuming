---
name: backup-db
description: Снять консистентный бэкап продовой SQLite-базы resume.db через GitHub Actions (self-hosted раннер на проде) и скачать его на дев-машину с проверкой целостности. Use when the user asks to back up the production database, download the prod DB, or before risky DB migrations / schema changes.
---

# Backup DB — бэкап продовой resume.db

SSH-доступа к проду нет. Единственный канал — self-hosted GitHub-раннер на самом сервере: workflow `.github/workflows/backup-db.yml` делает консистентный снимок WAL-базы через `sqlite3.Connection.backup()` внутри контейнера `app` и выгружает его артефактом (retention 7 дней).

Все команды ниже — PowerShell (Windows PowerShell 5.1: без `&&`, без юникс-команд).

## Шаг 1. Проверка gh и запуск workflow

```powershell
gh auth status
gh workflow run backup-db.yml
```

**Оговорка:** `workflow_dispatch` работает только после попадания `backup-db.yml` в default-ветку `main`. Если `gh` отвечает «could not find any workflows named backup-db.yml» — нужен мерж `develop` → `main`, после него повторить запуск.

## Шаг 2. Дождаться завершения

Запуск регистрируется не мгновенно — подожди пару секунд, затем:

```powershell
$run = gh run list --workflow=backup-db.yml --limit 1 --json databaseId,status,createdAt | ConvertFrom-Json
gh run watch $run.databaseId
```

Убедись, что взят свежий запуск (поле `createdAt` — только что), а не старый. Если раннер занят деплоем, run повисит в `queued` — это нормально, `gh run watch` дождётся.

## Шаг 3. Скачать артефакт

```powershell
$stamp = Get-Date -Format "yyyy-MM-dd_HH-mm"
gh run download $run.databaseId -D "backups\$stamp"
```

Файл окажется в `backups\<дата>\resume-db-backup-<timestamp>\resume-backup.db`.

## Шаг 4. Проверка целостности локально

```powershell
$db = (Get-ChildItem "backups\$stamp" -Recurse -Filter *.db | Select-Object -First 1).FullName
python -c "import sqlite3; c=sqlite3.connect(r'$db'); print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0]); [print(t.ljust(14), c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]) for t in ['users','sessions','magic_tokens','profiles','resumes','payments','anon_usage']]"
```

Если `python` на машине не найден (на этой дев-машине его нет в PATH), прогони ту же проверку через Docker:

```powershell
$dir = Split-Path $db
$file = Split-Path $db -Leaf
docker run --rm -v "${dir}:/b" python:3.12-slim python -c "import sqlite3; c=sqlite3.connect('/b/$file'); print('integrity:', c.execute('PRAGMA integrity_check').fetchone()[0]); [print(t.ljust(14), c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]) for t in ['users','sessions','magic_tokens','profiles','resumes','payments','anon_usage']]"
```

Ожидаемо: `integrity: ok` и ненулевые счётчики хотя бы в `users` / `resumes`. Таблицы соответствуют `init_db()` в `main.py`: **users, sessions, magic_tokens, profiles, resumes, payments, anon_usage**.

## Важно: PII

Бэкап содержит персональные данные пользователей (email, telegram id, тексты резюме, платежи). Хранить **только локально** в `backups/` (каталог в `.gitignore`), не коммитить, не выкладывать, не передавать. Workflow сам удаляет копию из рабочей папки раннера; артефакт в GitHub истекает через 7 дней.
