# Развёртывание веб-версии (Railway / Render)

Веб-версия — это тот же инструмент экспресс-аудита, но с авторизацией, БД
(PostgreSQL) и запуском в Docker с Playwright/Chromium. Ниже — как получить
публичный HTTPS-URL.

> Все секреты задаются **только** переменными окружения хостинга, не в коде.

---

## Вариант A — Railway (рекомендуется)

Railway собирает проект по `Dockerfile` (в нём уже ставится Chromium и все
системные зависимости) и умеет одним кликом добавить PostgreSQL.

### Шаги

1. **Залейте код в GitHub.** В корне проекта:
   ```bash
   git init && git add -A && git commit -m "Web version"
   git branch -M main
   git remote add origin https://github.com/<ВЫ>/legal-audit-tool.git
   git push -u origin main
   ```
2. **Создайте проект на Railway:** [railway.app](https://railway.app) → *New Project*
   → *Deploy from GitHub repo* → выберите репозиторий. Railway увидит `Dockerfile`
   и `railway.json` и начнёт сборку.
3. **Добавьте базу данных:** в проекте → *New* → *Database* → *Add PostgreSQL*.
   Railway создаст переменную `DATABASE_URL` и (если сослаться) прокинет её в сервис.
   В сервисе → *Variables* → *Add Reference* → выберите `DATABASE_URL` из Postgres.
4. **Задайте переменные окружения** сервиса (*Variables*), минимум:
   ```
   ADMIN_EMAIL=admin@yourbureau.ru
   ADMIN_PASSWORD=<временный надёжный пароль>
   SESSION_SECRET=<openssl rand -hex 32>
   ENABLE_LLM=true
   LLM_API_KEY=<ключ, опционально>
   EXPORTS_DIR=/data
   ```
   (полный список — в `.env.example`).
5. **Публичный домен:** сервис → *Settings* → *Networking* → *Generate Domain*.
   Получите `https://<ваш-сервис>.up.railway.app`.
6. Дождитесь `Deployed`/`Healthy` (healthcheck идёт на `/_stcore/health`).

### Через CLI (если предпочитаете терминал)
```bash
npm i -g @railway/cli          # или: brew install railway
railway login                  # откроется браузер для авторизации
railway init                   # создать проект
railway add --database postgres
railway up                     # собрать и задеплоить из текущей папки
railway variables set ADMIN_EMAIL=admin@yourbureau.ru ADMIN_PASSWORD=... SESSION_SECRET=...
railway domain                 # сгенерировать публичный URL
```

---

## Вариант B — Render (Blueprint)

В репозитории есть `render.yaml` (web-сервис в Docker + PostgreSQL + диск `/data`).

1. Залейте код в GitHub (как выше).
2. [dashboard.render.com](https://dashboard.render.com) → *New* → *Blueprint* →
   выберите репозиторий. Render прочитает `render.yaml`.
3. Заполните секреты, помеченные `sync: false`: `ADMIN_EMAIL`, `ADMIN_PASSWORD`,
   `LLM_API_KEY`. `SESSION_SECRET` и `DATABASE_URL` создаются автоматически.
4. *Apply* → дождитесь деплоя. Публичный URL — на странице сервиса
   (`https://legal-audit-tool.onrender.com`).

---

## Локальная проверка Docker (по желанию)

```bash
docker build -t legal-audit-tool .
docker run -p 8501:8501 --env-file .env legal-audit-tool
# затем откройте http://localhost:8501
```

---

## Хранение данных и PDF

- **История проверок** хранится в БД (`DATABASE_URL` → PostgreSQL) и переживает
  перезапуск/редеплой.
- **PDF-отчёты** при отсутствии файла на диске **перегенерируются на лету** из
  сохранённого результата, поэтому доступны для скачивания и после рестарта.
- Если используете SQLite вместо Postgres — обязательно смонтируйте persistent
  volume и укажите `EXPORTS_DIR` на него (иначе данные теряются при редеплое).

---

## Переменные окружения (сводка)

| Переменная | Назначение |
|---|---|
| `ADMIN_EMAIL` / `ADMIN_PASSWORD` | учётная запись администратора (вход) |
| `SESSION_SECRET` | секрет сессии |
| `DATABASE_URL` | PostgreSQL; пусто → SQLite в `EXPORTS_DIR` |
| `EXPORTS_DIR` | каталог для PDF и SQLite (по умолчанию `/data`) |
| `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `ENABLE_LLM` | LLM (можно задать и в UI «Настройки») |
| `MAX_PAGES_DEFAULT` / `PAGE_TIMEOUT_SECONDS` | параметры сканирования |
| `ENABLE_GEOIP` / `ENABLE_SCREENSHOTS` | доп. функции |
| `BUREAU_NAME/EMAIL/PHONE/WEBSITE` | реквизиты бюро для отчётов |

---

## Обновление проекта

1. Внесите изменения локально, закоммитьте и `git push` в `main`.
2. Railway/Render с включённым автодеплоем **пересоберут и выкатят** новую версию
   автоматически. (Railway: можно и `railway up`.)
3. **Переменные окружения** меняются в дашборде хостинга (*Variables* / *Environment*);
   часть настроек (LLM-ключ, цены, реквизиты) — прямо в приложении на странице
   «Настройки» (сохраняются в БД).
4. **Логи** — в дашборде хостинга (Railway: вкладка *Deployments/Logs*; Render:
   вкладка *Logs*).

---

## Первый вход

Откройте публичный URL → страница входа → введите `ADMIN_EMAIL` и `ADMIN_PASSWORD`.
Сразу после входа зайдите в **Настройки → Администратор и доступ** и **смените пароль**
(он сохранится как хэш в БД).
