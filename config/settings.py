"""
Конфигурация приложения.

Значения по умолчанию берутся из переменных окружения (.env), поверх них
накладываются пользовательские настройки из data/app_settings.json (их
редактирует вкладка «Настройки» в Streamlit). LLM-ключ и прочие секреты
хранятся локально.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - python-dotenv может быть не установлен
    pass


# Корень проекта (…/config/settings.py -> корень на уровень выше).
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXPORTS_DIR = BASE_DIR / "exports"
PDF_DIR = EXPORTS_DIR / "pdf"
SETTINGS_JSON = DATA_DIR / "app_settings.json"

DEFAULT_USER_AGENT = (
    "NexoraLegalInternalComplianceScanner/1.0 "
    "(+internal legal audit; no form submission)"
)


def _env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return val if val is not None else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on", "да")


class Settings(BaseModel):
    """Все настройки приложения в одном объекте."""

    # --- Авторизация (production) ---
    admin_email: str = "admin@example.com"
    admin_password: str = ""           # bootstrap-пароль из env (ADMIN_PASSWORD)
    admin_password_hash: str = ""      # pbkdf2-хэш пароля, заданного через UI
    session_secret: str = "change-me"

    # --- LLM ---
    llm_provider: str = "openai-compatible"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 4000
    enable_llm: bool = True

    # --- База данных ---
    database_url: str = ""             # postgresql://... ; пусто -> SQLite (db_path)

    # --- Сканирование ---
    max_pages: int = 20
    page_timeout_ms: int = 20000
    delay_between_pages_s: float = 1.0
    request_timeout_s: int = 20
    max_download_bytes: int = 8 * 1024 * 1024  # 8 МБ на документ
    user_agent: str = DEFAULT_USER_AGENT
    enable_screenshots: bool = False
    enable_geoip: bool = True
    geoip_db_path: str = ""  # путь к GeoLite2-Country.mmdb (опционально)

    # --- Юридическое бюро ---
    firm_name: str = "Юридическое бюро"
    firm_address: str = ""
    firm_contacts: str = ""
    lawyer_name: str = ""
    firm_email: str = ""
    firm_phone: str = ""
    firm_website: str = ""
    logo_path: str = ""

    # --- Цены услуг ---
    price_express_docs: str = ""       # экспресс-комплект документов для сайта
    price_full_audit: str = ""         # полный аудит 152-ФЗ
    price_turnkey: str = ""            # сопровождение под ключ
    price_express_audit: str = ""      # экспресс-аудит

    # --- Пути ---
    db_path: str = str(DATA_DIR / "database.sqlite")
    data_dir: str = str(DATA_DIR)
    exports_dir: str = str(EXPORTS_DIR)
    pdf_dir: str = str(PDF_DIR)

    # Пути к YAML-конфигам правил/трекеров/чек-листов/пакетов.
    def rules_path(self) -> Path:
        return Path(self.data_dir) / "legal_rules.yml"

    def trackers_path(self) -> Path:
        return Path(self.data_dir) / "tracker_domains.yml"

    def checklists_path(self) -> Path:
        return Path(self.data_dir) / "document_checklists.yml"

    def packages_path(self) -> Path:
        return Path(self.data_dir) / "service_packages.yml"

    def prices(self) -> Dict[str, str]:
        return {
            "express_audit": self.price_express_audit,
            "express_docs": self.price_express_docs,
            "full_audit": self.price_full_audit,
            "turnkey": self.price_turnkey,
        }


def _defaults_from_env() -> Settings:
    # EXPORTS_DIR позволяет вынести БД (SQLite) и PDF на persistent volume.
    exports_dir = _env("EXPORTS_DIR", str(EXPORTS_DIR))
    pdf_dir = str(Path(exports_dir) / "pdf")
    database_url = _env("DATABASE_URL", "")
    # Путь к SQLite: рядом с exports (актуально, если Postgres не задан).
    db_path = _env("SQLITE_PATH", str(Path(exports_dir) / "database.sqlite"))

    # PAGE_TIMEOUT_SECONDS (сек) — приоритетнее, чем PAGE_TIMEOUT_MS.
    if os.getenv("PAGE_TIMEOUT_SECONDS"):
        page_timeout_ms = _env_int("PAGE_TIMEOUT_SECONDS", 20) * 1000
    else:
        page_timeout_ms = _env_int("PAGE_TIMEOUT_MS", 20000)

    return Settings(
        admin_email=_env("ADMIN_EMAIL", "admin@example.com"),
        admin_password=_env("ADMIN_PASSWORD", ""),
        session_secret=_env("SESSION_SECRET", "change-me"),
        database_url=database_url,
        llm_provider=_env("LLM_PROVIDER", "openai-compatible"),
        llm_api_key=_env("LLM_API_KEY", _env("OPENAI_API_KEY", "")),
        llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_model=_env("LLM_MODEL", "gpt-4o-mini"),
        enable_llm=_env_bool("ENABLE_LLM", True),
        max_pages=_env_int("MAX_PAGES_DEFAULT", _env_int("MAX_PAGES", 20)),
        page_timeout_ms=page_timeout_ms,
        delay_between_pages_s=float(_env("DELAY_BETWEEN_PAGES_S", "1.0") or "1.0"),
        request_timeout_s=_env_int("REQUEST_TIMEOUT_S", 20),
        user_agent=_env("USER_AGENT", DEFAULT_USER_AGENT),
        enable_screenshots=_env_bool("ENABLE_SCREENSHOTS", False),
        enable_geoip=_env_bool("ENABLE_GEOIP", False),
        geoip_db_path=_env("GEOIP_DB_PATH", ""),
        # BUREAU_* — производственные имена переменных; FIRM_* поддержаны как fallback.
        firm_name=_env("BUREAU_NAME", _env("FIRM_NAME", "Юридическое бюро")),
        firm_address=_env("FIRM_ADDRESS", ""),
        firm_contacts=_env("FIRM_CONTACTS", ""),
        lawyer_name=_env("LAWYER_NAME", ""),
        firm_email=_env("BUREAU_EMAIL", _env("FIRM_EMAIL", "")),
        firm_phone=_env("BUREAU_PHONE", _env("FIRM_PHONE", "")),
        firm_website=_env("BUREAU_WEBSITE", _env("FIRM_WEBSITE", "")),
        logo_path=_env("LOGO_PATH", ""),
        price_express_audit=_env("PRICE_EXPRESS_AUDIT", ""),
        price_express_docs=_env("PRICE_EXPRESS_DOCS", ""),
        price_full_audit=_env("PRICE_FULL_AUDIT", ""),
        price_turnkey=_env("PRICE_TURNKEY", ""),
        db_path=db_path,
        exports_dir=exports_dir,
        pdf_dir=pdf_dir,
    )


def ensure_dirs(settings: Optional[Settings] = None) -> None:
    paths = [DATA_DIR, EXPORTS_DIR, PDF_DIR]
    if settings is not None:
        paths += [Path(settings.exports_dir), Path(settings.pdf_dir), Path(settings.db_path).parent]
    for p in paths:
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


def load_settings() -> Settings:
    """
    Загрузить настройки: env-значения по умолчанию + overrides.

    Порядок наложения: env -> локальный JSON (для локальной разработки) ->
    БД (для облака; переживает перезапуск контейнера). БД имеет приоритет.
    Секреты/инфраструктура (admin_*, session_secret, database_url, пути) всегда
    берутся из env и не переопределяются.
    """
    settings = _defaults_from_env()
    ensure_dirs(settings)

    merged = settings.model_dump()

    # 1) Локальный JSON.
    if SETTINGS_JSON.exists():
        try:
            data = json.loads(SETTINGS_JSON.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for k, v in data.items():
                    if v is not None and k not in _INFRA_KEYS:
                        merged[k] = v
        except Exception:
            pass

    # 2) БД (приоритет для облака).
    try:
        from database import repositories

        overrides = repositories.load_settings_overrides(settings)
        for k, v in (overrides or {}).items():
            if v is not None and k not in _INFRA_KEYS:
                merged[k] = v
    except Exception:
        pass

    try:
        return Settings(**merged)
    except Exception:
        return settings


# Инфраструктурные/секретные ключи — только из env, не переопределяются overrides.
_INFRA_KEYS = {
    "admin_email",
    "admin_password",
    "session_secret",
    "database_url",
    "db_path",
    "data_dir",
    "exports_dir",
    "pdf_dir",
}


def save_settings(settings: Settings) -> None:
    """Сохранить UI-настройки в локальный JSON и (если доступна) в БД."""
    ensure_dirs(settings)
    try:
        SETTINGS_JSON.write_text(
            json.dumps(settings.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    try:
        from database import repositories

        repositories.save_settings_overrides(settings.model_dump(), settings)
    except Exception:
        pass


def get_data_path(name: str) -> Path:
    return DATA_DIR / name
