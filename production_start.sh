#!/usr/bin/env bash
# Запуск Streamlit в production. Порт берётся из $PORT (Railway/Render),
# иначе 8501. Слушаем 0.0.0.0, headless, за HTTPS-прокси платформы.
set -euo pipefail

PORT="${PORT:-8501}"

exec streamlit run app.py \
  --server.address=0.0.0.0 \
  --server.port="${PORT}" \
  --server.headless=true \
  --server.enableCORS=false \
  --server.enableXsrfProtection=false \
  --browser.gatherUsageStats=false
