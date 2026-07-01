"""
Слой доступа к БД на SQLAlchemy Core.

Поддерживает два бэкенда одним кодом:
  * PostgreSQL — если задан DATABASE_URL (предпочтительно для облака/Railway);
  * SQLite    — по умолчанию (локально или на persistent volume).

Движок кэшируется по строке подключения. Ни одна функция не бросает наружу при
инициализации — при недоступности БД приложение продолжает работать (история
будет недоступна, но проверки — нет).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional

from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
)
from sqlalchemy.engine import Engine

metadata = MetaData()

scans = Table(
    "scans", metadata,
    Column("scan_id", String(64), primary_key=True),
    Column("created_at", Text),
    Column("company_name", Text),
    Column("site_url", Text),
    Column("final_url", Text),
    Column("industry", Text),
    Column("email", Text),
    Column("comment", Text),
    Column("risk_score", Integer),
    Column("risk_level", Text),
    Column("confidence", Integer),
    Column("status", Text),
    Column("pdf_path", Text),
    Column("executive_summary", Text),
    Column("commercial_offer_text", Text),
    Column("email_text", Text),
    Column("raw_json", Text),
)

pages = Table(
    "pages", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", String(64), index=True),
    Column("url", Text),
    Column("title", Text),
    Column("status_code", Integer),
    Column("text_length", Integer),
    Column("errors", Text),
)

forms = Table(
    "forms", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", String(64), index=True),
    Column("page_url", Text),
    Column("form_type", Text),
    Column("fields_json", Text),
    Column("consent_json", Text),
    Column("evidence_json", Text),
)

documents = Table(
    "documents", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", String(64), index=True),
    Column("doc_type", Text),
    Column("url", Text),
    Column("title", Text),
    Column("format", Text),
    Column("text_length", Integer),
    Column("checklist_json", Text),
    Column("errors", Text),
)

risks = Table(
    "risks", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", String(64), index=True),
    Column("risk_id", Text),
    Column("title", Text),
    Column("level", Text),
    Column("score", Integer),
    Column("evidence", Text),
    Column("recommendation", Text),
)

exports = Table(
    "exports", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scan_id", String(64), index=True),
    Column("pdf_path", Text),
    Column("created_at", Text),
)

# Ключ-значение для персистентности настроек, изменённых в UI (переживает
# перезапуск контейнера при использовании Postgres/persistent volume).
app_settings = Table(
    "app_settings", metadata,
    Column("key", String(128), primary_key=True),
    Column("value", Text),
)


_engines: Dict[str, Engine] = {}
_initialized: Dict[str, bool] = {}
_lock = threading.Lock()


def normalize_db_url(url: str) -> str:
    """Привести URL к драйверу psycopg3 (postgres://... -> postgresql+psycopg://...)."""
    if not url:
        return ""
    url = url.strip()
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine(database_url: str = "", sqlite_path: str = "") -> Engine:
    """
    Вернуть (кэшированный) движок SQLAlchemy. Если задан database_url — Postgres,
    иначе SQLite по пути sqlite_path.
    """
    if database_url:
        key = normalize_db_url(database_url)
        connect_args: dict = {}
    else:
        sqlite_path = sqlite_path or "data/database.sqlite"
        try:
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        key = "sqlite:///" + sqlite_path
        connect_args = {"check_same_thread": False}

    with _lock:
        eng = _engines.get(key)
        if eng is None:
            eng = create_engine(key, pool_pre_ping=True, connect_args=connect_args, future=True)
            _engines[key] = eng
        return eng


def init_db(engine: Optional[Engine] = None, database_url: str = "", sqlite_path: str = "") -> Engine:
    """Создать таблицы (идемпотентно). Возвращает движок."""
    if engine is None:
        engine = get_engine(database_url, sqlite_path)
    key = str(engine.url)
    with _lock:
        if _initialized.get(key):
            return engine
    try:
        metadata.create_all(engine)
        with _lock:
            _initialized[key] = True
    except Exception:
        # Не валим приложение, если БД временно недоступна.
        pass
    return engine


def engine_for(settings) -> Engine:
    """Движок по объекту настроек (database_url или db_path)."""
    return get_engine(getattr(settings, "database_url", "") or "", getattr(settings, "db_path", "") or "")
