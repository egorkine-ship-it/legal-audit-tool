"""
Репозитории: сохранение и чтение результатов проверок, статусы лидов,
персистентность UI-настроек и агрегаты для дашборда.

Работает поверх SQLAlchemy Core (database.db), поэтому одинаково функционирует
на PostgreSQL и SQLite. Ни одна функция не бросает исключение наружу.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, insert, select, update

from database import db
from scanner.models import ScanResult

# Ключи настроек, которые НЕ персистим в БД (секреты/инфраструктура берутся из env).
# admin_password_hash СОХРАНЯЕМ (это хэш, не открытый пароль): чтобы смена пароля
# в UI переживала перезапуск контейнера.
_SETTINGS_EXCLUDE = {
    "admin_email",
    "admin_password",
    "session_secret",
    "database_url",
    "db_path",
    "data_dir",
    "exports_dir",
    "pdf_dir",
}


def _engine(settings):
    eng = db.engine_for(settings)
    db.init_db(eng)
    return eng


def _model_dump_json(result: ScanResult) -> str:
    try:
        return result.model_dump_json()
    except Exception:
        try:
            return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
        except Exception:
            return "{}"


# ---------------------------------------------------------------------------
# Сохранение / чтение проверок
# ---------------------------------------------------------------------------
def save_scan(result: ScanResult, settings) -> str:
    """Сохранить (upsert) результат проверки и связанные строки. Вернуть scan_id."""
    scan_id = result.scan_id or datetime.now().strftime("%Y%m%d%H%M%S")
    try:
        eng = _engine(settings)
        raw_json = _model_dump_json(result)
        with eng.begin() as conn:
            # upsert: удаляем прежние строки этого scan_id, затем вставляем.
            for tbl in (db.pages, db.forms, db.documents, db.risks, db.exports, db.scans):
                conn.execute(delete(tbl).where(tbl.c.scan_id == scan_id))

            conn.execute(insert(db.scans).values(
                scan_id=scan_id,
                created_at=result.created_at,
                company_name=result.company_name,
                site_url=result.site_url,
                final_url=result.final_url,
                industry=result.industry,
                email=result.email,
                comment=result.comment,
                risk_score=int(result.risk_score or 0),
                risk_level=result.risk_level,
                confidence=int(result.confidence or 0),
                status=result.status,
                pdf_path=result.pdf_path,
                executive_summary=result.executive_summary,
                commercial_offer_text=result.commercial_offer_text,
                email_text=result.email_text,
                raw_json=raw_json,
            ))

            page_rows = [{
                "scan_id": scan_id, "url": p.url, "title": p.title,
                "status_code": int(p.status_code or 0), "text_length": int(p.text_length or 0),
                "errors": json.dumps(p.errors, ensure_ascii=False),
            } for p in result.pages]
            if page_rows:
                conn.execute(insert(db.pages), page_rows)

            form_rows = [{
                "scan_id": scan_id, "page_url": f.page_url, "form_type": f.form_type,
                "fields_json": json.dumps([x.model_dump() for x in f.fields], ensure_ascii=False),
                "consent_json": json.dumps(f.consent.model_dump(), ensure_ascii=False),
                "evidence_json": json.dumps(f.evidence.model_dump(), ensure_ascii=False),
            } for f in result.forms]
            if form_rows:
                conn.execute(insert(db.forms), form_rows)

            doc_rows = [{
                "scan_id": scan_id, "doc_type": d.doc_type, "url": d.url, "title": d.title,
                "format": d.format, "text_length": int(d.text_length or 0),
                "checklist_json": json.dumps(d.analysis.model_dump(), ensure_ascii=False) if d.analysis else "",
                "errors": d.extraction_error or "",
            } for d in result.documents]
            if doc_rows:
                conn.execute(insert(db.documents), doc_rows)

            risk_rows = [{
                "scan_id": scan_id, "risk_id": r.id, "title": r.title, "level": r.risk_level,
                "score": int(r.score or 0),
                "evidence": json.dumps(r.evidence.model_dump(), ensure_ascii=False),
                "recommendation": r.recommendation,
            } for r in result.risks]
            if risk_rows:
                conn.execute(insert(db.risks), risk_rows)

            if result.pdf_path:
                conn.execute(insert(db.exports).values(
                    scan_id=scan_id, pdf_path=result.pdf_path,
                    created_at=datetime.now().isoformat(timespec="seconds"),
                ))
    except Exception:
        pass
    return scan_id


def _top_risk_titles(raw_json: str, n: int = 3) -> List[str]:
    try:
        result = ScanResult.model_validate_json(raw_json)
        return [r.title for r in result.top_risks(n) if getattr(r, "title", "")]
    except Exception:
        try:
            data = json.loads(raw_json or "{}")
            rr = data.get("risks", []) or []
            return [r.get("title", "") for r in rr[:n]]
        except Exception:
            return []


def list_scans(settings, limit: int = 500) -> List[Dict[str, Any]]:
    """Список проверок (последние первыми) с производным полем top_risks."""
    out: List[Dict[str, Any]] = []
    try:
        eng = _engine(settings)
        with eng.connect() as conn:
            stmt = select(db.scans).order_by(db.scans.c.created_at.desc()).limit(limit)
            rows = conn.execute(stmt).mappings().all()
        for row in rows:
            d = dict(row)
            raw = d.pop("raw_json", "") or ""
            try:
                d["top_risks"] = "; ".join(_top_risk_titles(raw, 3))
            except Exception:
                d["top_risks"] = ""
            out.append(d)
    except Exception:
        return out
    return out


def get_scan(scan_id: str, settings) -> Optional[ScanResult]:
    try:
        eng = _engine(settings)
        with eng.connect() as conn:
            row = conn.execute(
                select(db.scans.c.raw_json).where(db.scans.c.scan_id == scan_id)
            ).first()
        if not row or not row[0]:
            return None
        return ScanResult.model_validate_json(row[0])
    except Exception:
        return None


def update_status(scan_id: str, status: str, settings) -> None:
    try:
        eng = _engine(settings)
        with eng.begin() as conn:
            conn.execute(
                update(db.scans).where(db.scans.c.scan_id == scan_id).values(status=status)
            )
            # Синхронизируем статус в raw_json, чтобы get_scan возвращал актуальное.
            row = conn.execute(
                select(db.scans.c.raw_json).where(db.scans.c.scan_id == scan_id)
            ).first()
            if row and row[0]:
                try:
                    data = json.loads(row[0])
                    data["status"] = status
                    conn.execute(
                        update(db.scans).where(db.scans.c.scan_id == scan_id)
                        .values(raw_json=json.dumps(data, ensure_ascii=False))
                    )
                except Exception:
                    pass
    except Exception:
        pass


def update_pdf_path(scan_id: str, pdf_path: str, settings) -> None:
    try:
        eng = _engine(settings)
        with eng.begin() as conn:
            conn.execute(
                update(db.scans).where(db.scans.c.scan_id == scan_id).values(pdf_path=pdf_path)
            )
    except Exception:
        pass


def save_export(scan_id: str, pdf_path: str, settings) -> None:
    try:
        eng = _engine(settings)
        with eng.begin() as conn:
            conn.execute(insert(db.exports).values(
                scan_id=scan_id, pdf_path=pdf_path,
                created_at=datetime.now().isoformat(timespec="seconds"),
            ))
    except Exception:
        pass


def delete_scan(scan_id: str, settings) -> None:
    try:
        eng = _engine(settings)
        with eng.begin() as conn:
            for tbl in (db.pages, db.forms, db.documents, db.risks, db.exports, db.scans):
                conn.execute(delete(tbl).where(tbl.c.scan_id == scan_id))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Дашборд
# ---------------------------------------------------------------------------
def dashboard_stats(settings) -> Dict[str, Any]:
    """Агрегаты для дашборда."""
    stats: Dict[str, Any] = {"total": 0, "high_critical": 0, "by_level": {}, "recent": []}
    try:
        rows = list_scans(settings, limit=1000)
        stats["total"] = len(rows)
        for r in rows:
            lvl = (r.get("risk_level") or "").lower()
            stats["by_level"][lvl] = stats["by_level"].get(lvl, 0) + 1
            if lvl in ("high", "critical"):
                stats["high_critical"] += 1
        stats["recent"] = rows[:10]
    except Exception:
        pass
    return stats


# ---------------------------------------------------------------------------
# Персистентность UI-настроек
# ---------------------------------------------------------------------------
def load_settings_overrides(settings) -> Dict[str, Any]:
    """Загрузить сохранённые в БД переопределения настроек (только UI-поля)."""
    try:
        eng = _engine(settings)
        with eng.connect() as conn:
            row = conn.execute(
                select(db.app_settings.c.value).where(db.app_settings.c.key == "__ui_settings__")
            ).first()
        if row and row[0]:
            data = json.loads(row[0])
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k not in _SETTINGS_EXCLUDE}
    except Exception:
        pass
    return {}


def save_settings_overrides(data: Dict[str, Any], settings) -> None:
    """Сохранить в БД переопределения настроек (секреты/инфраструктура исключены)."""
    try:
        payload = {k: v for k, v in (data or {}).items() if k not in _SETTINGS_EXCLUDE}
        value = json.dumps(payload, ensure_ascii=False)
        eng = _engine(settings)
        with eng.begin() as conn:
            conn.execute(delete(db.app_settings).where(db.app_settings.c.key == "__ui_settings__"))
            conn.execute(insert(db.app_settings).values(key="__ui_settings__", value=value))
    except Exception:
        pass
