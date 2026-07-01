# Production-образ веб-версии инструмента экспресс-аудита сайтов (152-ФЗ).
# Python 3.12 + Playwright Chromium (headless) + системные зависимости.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    EXPORTS_DIR=/data

WORKDIR /app

# Базовые системные пакеты + шрифты (в т.ч. кириллица для PDF-отчёта).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        fonts-dejavu-core \
        fonts-liberation \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Chromium + все системные библиотеки для Playwright (--with-deps ставит apt-пакеты).
RUN playwright install --with-deps chromium

# Код приложения.
COPY . .

# Каталог для БД (SQLite) и PDF; на persistent volume монтируется сюда же.
RUN mkdir -p /data/pdf

EXPOSE 8501

# Порт задаёт платформа через $PORT (Railway/Render). Локально — 8501.
CMD ["bash", "production_start.sh"]
