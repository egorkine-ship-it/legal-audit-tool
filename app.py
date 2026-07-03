"""
Экспресс-аудит публичной части сайтов на признаки рисков по 152-ФЗ.

Локальный Streamlit-интерфейс. Запуск:
    streamlit run app.py

ВАЖНО: приложение анализирует только публичную часть сайта и формирует
ПРЕДВАРИТЕЛЬНЫЕ выводы («признаки риска»). Перед отправкой клиенту любой отчёт,
коммерческое предложение и письмо должны быть проверены юристом.
"""
from __future__ import annotations

import base64
import io
import json
import html as html_lib
import time
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import streamlit as st

import auth
from config.settings import Settings, load_settings, save_settings
from database import repositories
from database.db import init_db
from scanner.models import (
    INDUSTRIES,
    RISK_LEVEL_RU,
    SCAN_STATUS_RU,
    RiskLevel,
    ScanInput,
    ScanResult,
    ScanStatus,
)
from scanner.orchestrator import run_scan
from services.jobs import JobManager

APP_DIR = Path(__file__).resolve().parent
APP_LOGO_PATH = APP_DIR / "assets" / "nexora_logo_app.jpg"

st.set_page_config(
    page_title="Nexora Legal · Compliance Scanner",
    page_icon=str(APP_LOGO_PATH) if APP_LOGO_PATH.exists() else "NL",
    layout="wide",
)

RISK_EMOJI = {
    RiskLevel.unknown.value: "•",
    RiskLevel.low.value: "•",
    RiskLevel.medium.value: "•",
    RiskLevel.high.value: "•",
    RiskLevel.critical.value: "•",
}

# Короткие текстовые маркеры статусов пунктов ядро-чеклиста.
CORE_STATUS_EMOJI = {
    "ok": "OK",
    "risk": "!",
    "unclear": "?",
    "not_applicable": "-",
}

RISK_LEVEL_CLASS = {
    RiskLevel.unknown.value: "muted",
    RiskLevel.low.value: "low",
    RiskLevel.medium.value: "medium",
    RiskLevel.high.value: "high",
    RiskLevel.critical.value: "critical",
}


# ---------------------------------------------------------------------------
# Визуальная система приложения
# ---------------------------------------------------------------------------
def _escape(value) -> str:
    """HTML-escape для любых пользовательских/сканированных значений."""
    if value is None:
        return ""
    return html_lib.escape(str(value), quote=True)


def _asset_data_uri(path: Path) -> str:
    """Вернуть data URI для небольшого локального изображения. Никогда не бросает."""
    try:
        if not path.exists() or not path.is_file():
            return ""
        data = path.read_bytes()
        if not data:
            return ""
        ext = path.suffix.lower().lstrip(".") or "jpeg"
        mime = "image/png" if ext == "png" else "image/jpeg"
        return "data:{};base64,{}".format(mime, base64.b64encode(data).decode("ascii"))
    except Exception:
        return ""


def _logo_data_uri(settings: Optional[Settings] = None) -> str:
    """Логотип приложения: сначала logo_path из настроек, затем bundled Nexora."""
    custom = ""
    try:
        custom = getattr(settings, "logo_path", "") or ""
    except Exception:
        custom = ""
    if custom:
        uri = _asset_data_uri(Path(custom))
        if uri:
            return uri
    return _asset_data_uri(APP_LOGO_PATH)


def _inject_design_system() -> None:
    """Единая визуальная система Streamlit UI: цвета, карточки, кнопки, таблицы."""
    st.markdown(
        """
<style>
    :root {
        --app-bg: #f5f5f7;
        --surface: rgba(255,255,255,0.82);
        --surface-2: rgba(255,255,255,0.56);
        --border: rgba(0,0,0,0.10);
        --border-soft: rgba(0,0,0,0.075);
        --ink: #1d1d1f;
        --muted: #6e6e73;
        --navy: #12355b;
        --navy-2: #2f6db3;
        --blue-soft: #eaf2ff;
        --green: #138a5b;
        --green-soft: #e8f7ef;
        --yellow: #a16207;
        --yellow-soft: #fff7db;
        --orange: #c05621;
        --orange-soft: #fff1e7;
        --red: #bf2f38;
        --red-soft: #fff0f1;
        --shadow: 0 20px 60px rgba(0, 0, 0, 0.075);
        --radius: 10px;
    }

    .stApp {
        font-family: -apple-system, BlinkMacSystemFont, "SF Pro Display", "SF Pro Text", Inter, Arial, sans-serif;
        background:
            radial-gradient(circle at 18% -8%, rgba(0, 113, 227, 0.10), transparent 28rem),
            radial-gradient(circle at 92% 8%, rgba(18, 53, 91, 0.08), transparent 24rem),
            linear-gradient(180deg, #fbfbfd 0%, var(--app-bg) 46%, #f2f2f5 100%);
        color: var(--ink);
    }

    header[data-testid="stHeader"] {
        background: rgba(251, 251, 253, 0.72);
        backdrop-filter: saturate(180%) blur(20px);
        border-bottom: 1px solid rgba(0,0,0,0.06);
    }

    .block-container {
        padding-top: 1.25rem;
        padding-bottom: 3rem;
        max-width: 1380px;
    }

    section[data-testid="stSidebar"] {
        background: rgba(255,255,255,0.64);
        backdrop-filter: saturate(180%) blur(22px);
        border-right: 1px solid rgba(0,0,0,0.075);
    }

    section[data-testid="stSidebar"] [data-testid="stMarkdownContainer"],
    section[data-testid="stSidebar"] label,
    section[data-testid="stSidebar"] p,
    section[data-testid="stSidebar"] span {
        color: var(--ink) !important;
    }

    section[data-testid="stSidebar"] .stButton > button {
        background: rgba(255,255,255,0.72);
        border-color: rgba(0,0,0,0.08);
        color: var(--ink);
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label {
        min-height: 2.45rem;
        padding: 0.38rem 0.55rem;
        border-radius: var(--radius);
        border: 1px solid transparent;
        transition: background .16s ease, border-color .16s ease, transform .16s ease;
    }

    section[data-testid="stSidebar"] div[role="radiogroup"] label:hover {
        background: rgba(255,255,255,0.86);
        border-color: rgba(0,0,0,0.08);
        transform: translateX(1px);
    }

    h1, h2, h3 {
        letter-spacing: 0;
    }

    div[data-testid="stForm"],
    div[data-testid="stExpander"],
    div[data-testid="stVerticalBlockBorderWrapper"] {
        border-color: var(--border-soft) !important;
        border-radius: var(--radius) !important;
        background: rgba(255,255,255,0.74);
        backdrop-filter: saturate(160%) blur(18px);
        box-shadow: 0 14px 38px rgba(0,0,0,0.045);
    }

    .stTextInput input,
    .stTextArea textarea,
    .stNumberInput input,
    .stSelectbox [data-baseweb="select"],
    .stFileUploader section {
        border-radius: var(--radius) !important;
    }

    .stButton > button,
    .stDownloadButton > button,
    button[kind="primary"] {
        border-radius: var(--radius) !important;
        min-height: 2.65rem;
        font-weight: 650;
        letter-spacing: 0;
        transition: transform .13s ease, box-shadow .13s ease, border-color .13s ease;
    }

    .stButton > button:hover,
    .stDownloadButton > button:hover {
        transform: translateY(-1px);
        box-shadow: 0 12px 24px rgba(0, 0, 0, 0.11);
    }

    .stButton > button[kind="primary"] {
        background: linear-gradient(180deg, #2c2c2e 0%, #111113 100%);
        border: 1px solid rgba(0,0,0,0.22);
    }

    [data-testid="stDataFrame"] {
        border: 1px solid var(--border-soft);
        border-radius: var(--radius);
        overflow: hidden;
        background: rgba(255,255,255,0.78);
        backdrop-filter: saturate(160%) blur(18px);
        box-shadow: 0 14px 38px rgba(0,0,0,0.045);
    }

    div[data-testid="stAlert"] {
        border-radius: var(--radius);
        border: 1px solid var(--border-soft);
    }

    .app-pagehead {
        padding: 1.2rem 1.25rem;
        margin: 0 0 1.1rem 0;
        background: rgba(255,255,255,0.74);
        backdrop-filter: saturate(180%) blur(22px);
        color: var(--ink);
        border: 1px solid rgba(0,0,0,0.075);
        border-radius: var(--radius);
        box-shadow: var(--shadow);
    }

    .app-kicker {
        color: #0071e3;
        font-size: .78rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: .08em;
        margin-bottom: .35rem;
    }

    .app-title {
        font-size: clamp(1.45rem, 2.2vw, 2.05rem);
        font-weight: 760;
        line-height: 1.15;
        margin: 0;
    }

    .app-subtitle {
        color: var(--muted);
        max-width: 860px;
        margin-top: .42rem;
        font-size: .98rem;
        line-height: 1.45;
    }

    .metric-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: .85rem;
        margin: .25rem 0 1.1rem 0;
    }

    .metric-card {
        background: rgba(255,255,255,0.78);
        backdrop-filter: saturate(160%) blur(18px);
        border: 1px solid var(--border-soft);
        border-radius: var(--radius);
        padding: 1rem;
        box-shadow: 0 14px 38px rgba(0,0,0,0.045);
        min-height: 7rem;
    }

    .metric-label {
        color: var(--muted);
        font-size: .78rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: .06em;
        margin-bottom: .55rem;
    }

    .metric-value {
        color: var(--ink);
        font-size: clamp(1.45rem, 2vw, 2rem);
        font-weight: 760;
        line-height: 1.05;
        word-break: break-word;
    }

    .metric-note {
        color: var(--muted);
        font-size: .84rem;
        margin-top: .5rem;
        line-height: 1.35;
    }

    .metric-card.low { border-left: 4px solid var(--green); }
    .metric-card.medium { border-left: 4px solid #d6a400; }
    .metric-card.high { border-left: 4px solid var(--orange); }
    .metric-card.critical { border-left: 4px solid var(--red); }
    .metric-card.muted { border-left: 4px solid #94a3b8; }

    .risk-pill {
        display: inline-flex;
        align-items: center;
        gap: .42rem;
        padding: .32rem .58rem;
        border-radius: 999px;
        font-size: .82rem;
        font-weight: 760;
        border: 1px solid transparent;
        white-space: nowrap;
    }

    .risk-pill.low { color: var(--green); background: var(--green-soft); border-color: #bfe8d1; }
    .risk-pill.medium { color: var(--yellow); background: var(--yellow-soft); border-color: #f5df91; }
    .risk-pill.high { color: var(--orange); background: var(--orange-soft); border-color: #ffd2b6; }
    .risk-pill.critical { color: var(--red); background: var(--red-soft); border-color: #ffc8ce; }
    .risk-pill.muted { color: #64748b; background: #f1f5f9; border-color: #dbe3ee; }

    .risk-dot {
        width: .52rem;
        height: .52rem;
        border-radius: 99px;
        background: currentColor;
        display: inline-block;
    }

    .soft-panel {
        background: rgba(255,255,255,0.78);
        backdrop-filter: saturate(160%) blur(18px);
        border: 1px solid var(--border-soft);
        border-radius: var(--radius);
        padding: 1rem 1.1rem;
        box-shadow: 0 14px 38px rgba(0,0,0,0.045);
        margin: .25rem 0 1rem 0;
    }

    .section-label {
        color: var(--muted);
        font-size: .76rem;
        font-weight: 750;
        letter-spacing: .08em;
        text-transform: uppercase;
        margin-bottom: .2rem;
    }

    .section-title {
        color: var(--ink);
        font-weight: 760;
        font-size: 1.08rem;
        margin-bottom: .35rem;
    }

    .muted-copy {
        color: var(--muted);
        font-size: .92rem;
        line-height: 1.45;
    }

    .job-card {
        background: rgba(255,255,255,0.78);
        backdrop-filter: saturate(160%) blur(18px);
        border: 1px solid var(--border-soft);
        border-radius: var(--radius);
        padding: .95rem 1rem;
        box-shadow: 0 12px 32px rgba(0,0,0,0.045);
        margin-bottom: .75rem;
    }

    .job-title {
        color: var(--ink);
        font-weight: 760;
        margin-bottom: .25rem;
    }

    .job-meta {
        color: var(--muted);
        font-size: .88rem;
        line-height: 1.35;
    }

    .login-shell {
        max-width: 980px;
        margin: 5vh auto 1.5rem auto;
        display: grid;
        grid-template-columns: 1.1fr .9fr;
        gap: 1rem;
        align-items: stretch;
    }

    .login-brand {
        background:
            radial-gradient(circle at 12% 8%, rgba(0,113,227,0.14), transparent 18rem),
            linear-gradient(180deg, rgba(255,255,255,0.92) 0%, rgba(255,255,255,0.72) 100%);
        backdrop-filter: saturate(180%) blur(22px);
        border: 1px solid rgba(0,0,0,0.075);
        border-radius: var(--radius);
        padding: 1.35rem;
        color: var(--ink);
        box-shadow: var(--shadow);
        min-height: 18rem;
        display: flex;
        flex-direction: column;
        justify-content: space-between;
    }

    .brand-lockup {
        display: inline-flex;
        align-items: center;
        gap: .72rem;
        padding: .52rem .68rem;
        background: rgba(255,255,255,0.98);
        border-radius: var(--radius);
        border: 1px solid rgba(0,0,0,0.065);
        box-shadow: 0 10px 30px rgba(0,0,0,0.05);
        width: fit-content;
        max-width: 100%;
    }

    .brand-lockup img {
        width: 9.5rem;
        height: auto;
        display: block;
    }

    .brand-lockup span {
        color: #253047;
        font-size: .72rem;
        font-weight: 760;
        letter-spacing: .08em;
        text-transform: uppercase;
        border-left: 1px solid #d6dde8;
        padding-left: .72rem;
        white-space: nowrap;
    }

    .sidebar-brand {
        background: rgba(255,255,255,0.98);
        border-radius: var(--radius);
        padding: .68rem;
        margin: .1rem 0 .65rem 0;
        border: 1px solid rgba(0,0,0,0.065);
    }

    .sidebar-brand img {
        width: 100%;
        max-width: 12rem;
        display: block;
        margin: 0 auto;
    }

    .sidebar-product {
        color: var(--muted);
        font-size: .76rem;
        font-weight: 720;
        letter-spacing: .08em;
        text-transform: uppercase;
        margin: .25rem 0 .8rem 0;
    }

    .login-card {
        background: rgba(255,255,255,0.78);
        backdrop-filter: saturate(160%) blur(18px);
        border: 1px solid var(--border-soft);
        border-radius: var(--radius);
        padding: 1.15rem;
        box-shadow: var(--shadow);
    }

    .login-title {
        font-size: clamp(1.6rem, 3vw, 2.35rem);
        font-weight: 780;
        line-height: 1.08;
        margin-bottom: .7rem;
    }

    .login-copy {
        color: var(--muted);
        line-height: 1.5;
        max-width: 36rem;
    }

    @media (max-width: 980px) {
        .metric-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        .login-shell { grid-template-columns: 1fr; margin-top: 1rem; }
    }

    @media (max-width: 640px) {
        .block-container { padding-left: .9rem; padding-right: .9rem; }
        .metric-grid { grid-template-columns: 1fr; }
        .app-pagehead { padding: 1rem; }
        .login-brand { min-height: auto; }
        .brand-lockup { align-items: flex-start; flex-direction: column; }
        .brand-lockup span { border-left: 0; padding-left: 0; }
    }
</style>
        """,
        unsafe_allow_html=True,
    )


def _page_header(title: str, subtitle: str = "", kicker: str = "Рабочая панель") -> None:
    st.markdown(
        f"""
<div class="app-pagehead">
    <div class="app-kicker">{_escape(kicker)}</div>
    <div class="app-title">{_escape(title)}</div>
    <div class="app-subtitle">{_escape(subtitle)}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def _brand_lockup(settings: Optional[Settings] = None, compact: bool = False) -> str:
    logo = _logo_data_uri(settings)
    if not logo:
        return '<div class="brand-lockup"><strong>NEXORA LEGAL</strong><span>product</span></div>'
    label = "product" if compact else "program product"
    return (
        '<div class="brand-lockup">'
        f'<img src="{logo}" alt="Nexora Legal">'
        f'<span>{_escape(label)}</span>'
        '</div>'
    )


def _sidebar_brand(settings: Settings) -> None:
    logo = _logo_data_uri(settings)
    if logo:
        st.sidebar.markdown(
            f"""
<div class="sidebar-brand">
    <img src="{logo}" alt="Nexora Legal">
</div>
<div class="sidebar-product">Compliance scanner</div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.sidebar.markdown("### NEXORA LEGAL")


def _risk_pill(level: str) -> str:
    cls = RISK_LEVEL_CLASS.get(level, "muted")
    return (
        f'<span class="risk-pill {cls}"><span class="risk-dot"></span>'
        f'{_escape(_risk_ru(level))}</span>'
    )


def _score_display(score: int) -> str:
    try:
        n = int(score or 0)
    except Exception:
        n = 0
    return "100+" if n > 100 else str(max(0, n))


def _metric_grid(items: List[dict]) -> None:
    cards = []
    for item in items:
        cls = _escape(item.get("class", ""))
        cards.append(
            f"""
<div class="metric-card {cls}">
    <div class="metric-label">{_escape(item.get("label", ""))}</div>
    <div class="metric-value">{item.get("value_html") or _escape(item.get("value", ""))}</div>
    <div class="metric-note">{_escape(item.get("note", ""))}</div>
</div>
            """
        )
    st.markdown('<div class="metric-grid">' + "\n".join(cards) + "</div>", unsafe_allow_html=True)


def _soft_panel(title: str, body: str, label: str = "") -> None:
    st.markdown(
        f"""
<div class="soft-panel">
    <div class="section-label">{_escape(label)}</div>
    <div class="section-title">{_escape(title)}</div>
    <div class="muted-copy">{_escape(body)}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Настройки / инициализация
# ---------------------------------------------------------------------------
def get_settings() -> Settings:
    if "settings" not in st.session_state:
        st.session_state["settings"] = load_settings()
    return st.session_state["settings"]


def ensure_db(settings: Settings) -> None:
    try:
        init_db(database_url=settings.database_url, sqlite_path=settings.db_path)
    except Exception as exc:  # pragma: no cover
        st.warning(f"Не удалось инициализировать базу данных: {exc}")


def _load_packages(settings: Settings) -> dict:
    try:
        import yaml

        with open(settings.packages_path(), "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _get_or_regenerate_pdf(result: ScanResult, settings: Settings) -> Optional[bytes]:
    """
    Байты PDF для скачивания. Если файл на диске отсутствует (эфемерная ФС в
    облаке после перезапуска) — перегенерируем отчёт из сохранённого результата.
    """
    data = _pdf_bytes(result.pdf_path)
    if data:
        return data
    try:
        from reports import pdf_generator

        path = pdf_generator.generate_pdf(result, settings, _load_packages(settings))
        if path:
            try:
                repositories.update_pdf_path(result.scan_id, path, settings)
            except Exception:
                pass
            return _pdf_bytes(path)
    except Exception:
        pass
    return None


def _get_or_generate_teaser_pdf(result: ScanResult, settings: Settings) -> Optional[bytes]:
    """Байты короткого клиентского КП. Генерируется из сохранённого ScanResult."""
    try:
        from reports import teaser_pdf_generator

        path = teaser_pdf_generator.generate_teaser_pdf(result, settings, _load_packages(settings))
        if path:
            return _pdf_bytes(path)
    except Exception:
        pass
    return None


def _risk_ru(level: str) -> str:
    """Русское название уровня риска, безопасно при неизвестном значении."""
    try:
        return RISK_LEVEL_RU[RiskLevel(level)]
    except Exception:
        return level or "—"


def risk_badge(level: str) -> str:
    return f"{RISK_EMOJI.get(level, '•')} {_risk_ru(level)}"


def _pdf_bytes(path: str) -> Optional[bytes]:
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            return p.read_bytes()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Отрисовка результата
# ---------------------------------------------------------------------------
def _render_core_checklist(result: ScanResult) -> None:
    """Ядро-чеклист (основные проверки) и заметки агентной перепроверки."""
    items = getattr(result, "core_checklist", None) or []
    if items:
        st.subheader("Ядро-чеклист")
        counts = {"ok": 0, "risk": 0, "unclear": 0, "not_applicable": 0}
        for item in items:
            status = getattr(item, "status", "unclear") or "unclear"
            counts[status] = counts.get(status, 0) + 1
            line = f"{CORE_STATUS_EMOJI.get(status, '•')} **{item.label}**"
            if item.comment:
                line += f" — {item.comment}"
            st.markdown(line)
        st.caption(
            f"Выполнено: {counts.get('ok', 0)} · Зоны риска: {counts.get('risk', 0)} · "
            f"Требует проверки: {counts.get('unclear', 0)} · "
            f"Не применимо: {counts.get('not_applicable', 0)}"
        )

    notes = (getattr(result, "agent_audit_notes", "") or "").strip()
    if notes:
        st.caption("Агентная перепроверка")
        st.write(notes)


def _render_fetch_diagnostics(result: ScanResult) -> None:
    """Диагностика рендера главной: браузер (Playwright) или простой HTTP."""
    if (result.fetch_method or "http") != "playwright":
        st.warning(
            f"Страницы загружены без браузера (метод: {result.fetch_method or 'http'}). "
            f"На JS-сайтах подвал и документы могут не находиться. "
            f"Ссылок на главной: {result.homepage_links}."
        )
    else:
        st.caption(f"Рендер: браузер (Playwright) · ссылок на главной: {result.homepage_links}")


def render_result_summary(result: ScanResult) -> None:
    _metric_grid([
        {
            "label": "Risk score",
            "value": _score_display(result.risk_score),
            "note": "Шкала отображения 0-100+",
            "class": RISK_LEVEL_CLASS.get(result.risk_level, "muted"),
        },
        {
            "label": "Уровень риска",
            "value_html": _risk_pill(result.risk_level),
            "note": "Предварительный автоматический вывод",
            "class": RISK_LEVEL_CLASS.get(result.risk_level, "muted"),
        },
        {
            "label": "Confidence",
            "value": f"{result.confidence}/100",
            "note": "Достоверность зависит от доказательств",
            "class": "muted",
        },
        {
            "label": "Проверено страниц",
            "value": result.pages_checked,
            "note": "Публичная часть сайта",
            "class": "muted",
        },
    ])

    if result.errors:
        with st.expander(f"Замечания сканера ({len(result.errors)})"):
            for e in result.errors:
                st.text(f"• {e}")

    _soft_panel(
        "Ограничение автоматической проверки",
        "Проверка публичной части сайта не заменяет полноценный юридический аудит. "
        "Выводы предварительные и требуют подтверждения юристом.",
        "Важно",
    )

    st.subheader("Ключевые признаки риска")
    top = result.top_risks(7)
    if not top:
        st.write("Существенных признаков риска не выявлено автоматически "
                 "(требуется ручная проверка).")
    for r in top:
        with st.container(border=True):
            st.markdown(
                f"<div class='section-title'>{_escape(r.title)} {_risk_pill(r.risk_level)}</div>"
                f"<div class='muted-copy'>Вес риска: +{_escape(r.score)}</div>",
                unsafe_allow_html=True,
            )
            if r.report_phrase:
                st.write(r.report_phrase)
            if r.recommendation:
                st.caption(f"Рекомендация: {r.recommendation}")

    _render_core_checklist(result)

    colf, cold, colt = st.columns(3)
    with colf:
        st.subheader("Формы")
        st.write(f"Найдено форм: **{len(result.forms)}**")
        pd_forms = [f for f in result.forms if f.potentially_personal_data_form]
        st.write(f"Из них с признаками сбора ПДн: **{len(pd_forms)}**")
    with cold:
        st.subheader("Документы")
        st.write(f"Найдено документов: **{len(result.documents)}**")
        types = sorted({d.doc_type for d in result.documents if d.is_accessible})
        st.write(", ".join(types) if types else "—")
    with colt:
        st.subheader("Cookies / трекеры")
        st.write(f"Трекеров: **{len(result.trackers)}**")
        st.write(f"Cookie-баннер: {'да' if result.cookie_banner_found else 'нет'}")
        st.write(f"Иностранные сервисы: {'да' if result.foreign_trackers_found else 'нет'}")


def render_texts_and_downloads(result: ScanResult) -> None:
    st.divider()
    settings = get_settings()
    cols = st.columns(4)
    teaser_pdf = _get_or_generate_teaser_pdf(result, settings)
    deep_or_existing_full = (getattr(result, "scan_mode", "quick") == "deep") or bool(result.pdf_path)
    pdf = _get_or_regenerate_pdf(result, settings) if deep_or_existing_full else None
    with cols[0]:
        if teaser_pdf:
            st.download_button(
                "Короткое КП PDF",
                data=teaser_pdf,
                file_name=f"kp_short_{result.scan_id}.pdf",
                mime="application/pdf",
                key=f"teaser_pdf_{result.scan_id}",
                use_container_width=True,
            )
        else:
            st.caption("Короткое КП PDF не сформировано.")
    with cols[1]:
        if pdf:
            st.download_button(
                "Полный PDF-отчёт",
                data=pdf,
                file_name=(Path(result.pdf_path).name if result.pdf_path else f"report_{result.scan_id}.pdf"),
                mime="application/pdf",
                key=f"pdf_{result.scan_id}",
                use_container_width=True,
            )
        else:
            if getattr(result, "scan_mode", "quick") == "quick":
                st.caption("Полный отчёт доступен в режиме «Глубокий аудит».")
            else:
                st.caption("PDF не сформирован (см. замечания сканера).")
    with cols[2]:
        st.download_button(
            "Коммерческое предложение (.txt)",
            data=(result.commercial_offer_text or "").encode("utf-8"),
            file_name=f"kp_{result.scan_id}.txt",
            mime="text/plain",
            key=f"kp_{result.scan_id}",
            use_container_width=True,
        )
    with cols[3]:
        st.download_button(
            "Письмо (.txt)",
            data=(result.email_text or "").encode("utf-8"),
            file_name=f"email_{result.scan_id}.txt",
            mime="text/plain",
            key=f"em_{result.scan_id}",
            use_container_width=True,
        )

    with st.expander("Коммерческое предложение"):
        st.code(result.commercial_offer_text or "—", language="markdown")
    with st.expander("Письмо для первичного контакта"):
        st.code(result.email_text or "—", language="markdown")
    with st.expander("Резюме"):
        st.write(result.executive_summary or "—")
    with st.expander("JSON результата"):
        st.json(json.loads(result.model_dump_json()))


# ---------------------------------------------------------------------------
# Вкладка 1: Проверка одного сайта
# ---------------------------------------------------------------------------
def _run_scan_ui(scan_input, settings, on_progress=None) -> ScanResult:
    """
    Запустить проверку в ОТДЕЛЬНОМ потоке.

    Критично для облака: Playwright sync API не запускается внутри потока
    Streamlit, где активен asyncio event loop, и молча откатывается на простой
    HTTP-запрос (тогда JS-сайты не рендерятся и подвал/документы не находятся).
    Свежий воркер-поток без asyncio-цикла устраняет это. Прогресс передаётся в
    основной поток через очередь (воркер НЕ трогает Streamlit напрямую).
    """
    import queue as _queue
    from concurrent.futures import ThreadPoolExecutor

    q: "_queue.Queue" = _queue.Queue()

    def _worker():
        return run_scan(scan_input, settings, progress_cb=lambda m: q.put(m))

    with ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(_worker)
        while True:
            try:
                msg = q.get(timeout=0.2)
                if on_progress:
                    try:
                        on_progress(msg)
                    except Exception:
                        pass
            except _queue.Empty:
                if fut.done():
                    break
        # Добираем оставшиеся сообщения.
        while not q.empty():
            try:
                m = q.get_nowait()
            except Exception:
                break
            if on_progress:
                try:
                    on_progress(m)
                except Exception:
                    pass
        return fut.result()


def _render_job_result(job, settings: Settings) -> None:
    """Загрузить сохранённый результат задачи из БД и отрисовать его."""
    result = repositories.get_scan(job.scan_id, settings) if job.scan_id else None
    if result is None:
        st.error("Не удалось загрузить результат из базы данных.")
        return
    st.session_state["last_result"] = result.scan_id
    _render_fetch_diagnostics(result)
    render_result_summary(result)
    render_texts_and_downloads(result)


def _render_jobs_block(settings: Settings) -> None:
    """
    «Активные и последние проверки»: фоновые задачи из JobManager.

    Задачи живут в процессе (не в Streamlit-сессии), поэтому после перезагрузки
    страницы пользователь автоматически «переподключается» к идущей проверке.
    """
    manager = JobManager.instance()
    jobs = manager.list_jobs()
    if not jobs:
        return

    st.subheader("Активные и последние проверки")
    for job in jobs:
        label = job.company_name or job.site_url or job.job_id
        st.markdown(
            f"""
<div class="job-card">
    <div class="job-title">{_escape(label)}</div>
    <div class="job-meta">{_escape(job.site_url)} · {_escape(job.status)} · создана {_escape(job.created_at)}</div>
</div>
            """,
            unsafe_allow_html=True,
        )
        with st.container():
            if job.status == "running":
                for line in job.progress[-3:]:
                    st.caption(line)
                if st.button("Остановить", key=f"stop_{job.job_id}"):
                    manager.stop(job.job_id)
                    st.rerun()
            elif job.status == "done":
                st.caption(f"Завершена: {job.finished_at}")
                if job.scan_id:
                    if st.button("Показать результат", key=f"show_{job.job_id}"):
                        st.session_state["show_job_result"] = job.job_id
                    if st.session_state.get("show_job_result") == job.job_id:
                        _render_job_result(job, settings)
                else:
                    st.caption("Результат не сохранён в базе (см. историю).")
            elif job.status == "stopped":
                st.caption(f"Остановлена: {job.finished_at}")
                if job.scan_id:
                    if st.button("Показать частичный результат", key=f"show_{job.job_id}"):
                        st.session_state["show_job_result"] = job.job_id
                    if st.session_state.get("show_job_result") == job.job_id:
                        _render_job_result(job, settings)
            else:  # error
                st.caption(f"Ошибка: {job.finished_at}")
                if job.error:
                    st.caption(job.error)


def tab_single(settings: Settings) -> None:
    _page_header(
        "Проверка одного сайта",
        "Запустите фоновую проверку публичной части сайта, получите risk score, PDF, КП и письмо.",
        "Новая проверка",
    )

    # Блок фоновых задач — всегда сверху: сюда «переподключается» пользователь
    # после перезагрузки страницы или переключения вкладок.
    _render_jobs_block(settings)

    scan_mode = st.radio(
        "Режим проверки",
        options=["quick", "deep"],
        index=0,
        horizontal=True,
        format_func=lambda x: (
            "Быстрое КП (без LLM)" if x == "quick" else "Глубокий аудит (LLM + полный отчёт)"
        ),
    )
    if scan_mode == "quick":
        st.caption(
            "Быстрый режим: сканирует сайт, строит короткое КП PDF и не использует LLM. "
            "Подходит для первичного коммерческого касания."
        )
    else:
        st.caption(
            "Глубокий режим: включает LLM-анализ документов, агентную перепроверку и полный внутренний PDF. "
            "Этот режим может заметно расходовать токены."
        )

    with st.form("single_form"):
        c1, c2 = st.columns(2)
        with c1:
            url = st.text_input("URL сайта", placeholder="example.ru")
            company = st.text_input("Название компании")
            industry = st.selectbox("Отрасль", INDUSTRIES, index=0)
        with c2:
            email = st.text_input("Email компании (необязательно)")
            comment = st.text_area("Комментарий (необязательно)", height=80)
            max_pages = st.number_input(
                "Лимит страниц", min_value=1, max_value=200, value=int(settings.max_pages)
            )
        if scan_mode == "deep":
            create_pdf = st.checkbox("Создать полный внутренний PDF-отчёт", value=True)
        else:
            create_pdf = False
        submitted = st.form_submit_button("Запустить проверку", use_container_width=True)

    if submitted:
        if not url.strip():
            st.error("Укажите URL сайта.")
        elif scan_mode == "deep" and (not settings.enable_llm or not settings.llm_api_key):
            st.error("Для глубокого аудита включите LLM и укажите API key в настройках.")
        else:
            scan_input = ScanInput(
                company_name=company.strip(),
                site_url=url.strip(),
                industry=industry,
                email=email.strip(),
                comment=comment.strip(),
                max_pages=int(max_pages),
                scan_mode=scan_mode,
                use_llm=(scan_mode == "deep"),
                use_agent=(scan_mode == "deep"),
                create_pdf=bool(create_pdf),
            )
            job_id = JobManager.instance().submit(scan_input, settings)
            st.session_state["last_job_id"] = job_id
            st.session_state.pop("show_job_result", None)
            st.rerun()

    # Автообновление, пока есть выполняющиеся задачи (не крутится вхолостую:
    # без running-задач rerun не планируется).
    if JobManager.instance().any_running():
        time.sleep(2)
        st.rerun()


# ---------------------------------------------------------------------------
# Вкладка 2: Массовая проверка
# ---------------------------------------------------------------------------
def _read_table(uploaded) -> "Optional[object]":
    import pandas as pd

    name = (uploaded.name or "").lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded)
        return pd.read_excel(uploaded)
    except Exception as exc:
        st.error(f"Не удалось прочитать файл: {exc}")
        return None


def tab_bulk(settings: Settings) -> None:
    import pandas as pd

    _page_header(
        "Массовая проверка",
        "Ожидаемые столбцы: company_name, site_url, industry, email, comment. "
        "Обязателен только site_url.",
        "CSV / XLSX",
    )
    uploaded = st.file_uploader("CSV или XLSX", type=["csv", "xlsx"])
    if uploaded is None:
        return

    df = _read_table(uploaded)
    if df is None:
        return
    st.subheader("Предпросмотр")
    st.dataframe(df, use_container_width=True, height=240)

    scan_mode = st.radio(
        "Режим массовой проверки",
        options=["quick", "deep"],
        index=0,
        horizontal=True,
        format_func=lambda x: (
            "Быстрое КП (без LLM)" if x == "quick" else "Глубокий аудит (LLM, без агента)"
        ),
    )
    st.caption(
        "В массовом быстром режиме PDF-архив содержит короткие КП. "
        "Глубокий режим анализирует документы через LLM и может заметно расходовать токены."
    )
    cc1, cc2 = st.columns(2)
    b_max = cc1.number_input("Лимит страниц на сайт", 1, 200, int(settings.max_pages))
    b_pdf = cc2.checkbox(
        "Создавать полный внутренний PDF",
        value=(scan_mode == "deep"),
        disabled=(scan_mode == "quick"),
    )

    if not st.button("Запустить массовую проверку", use_container_width=True):
        return
    if scan_mode == "deep" and (not settings.enable_llm or not settings.llm_api_key):
        st.error("Для глубокого аудита включите LLM и укажите API key в настройках.")
        return

    rows = df.to_dict(orient="records")
    progress = st.progress(0.0)
    log_box = st.empty()
    results: List[ScanResult] = []

    for i, row in enumerate(rows, start=1):
        site = str(row.get("site_url") or row.get("url") or "").strip()
        if not site or site.lower() == "nan":
            continue
        company = str(row.get("company_name") or "").strip()
        industry = str(row.get("industry") or "auto").strip() or "auto"
        if industry not in INDUSTRIES:
            industry = "auto"
        scan_input = ScanInput(
            company_name="" if company.lower() == "nan" else company,
            site_url=site,
            industry=industry,
            email="" if str(row.get("email") or "").lower() == "nan" else str(row.get("email") or "").strip(),
            comment="" if str(row.get("comment") or "").lower() == "nan" else str(row.get("comment") or "").strip(),
            max_pages=int(b_max),
            scan_mode=scan_mode,
            use_llm=(scan_mode == "deep"),
            # В массовой проверке агентный обход по умолчанию ВЫКЛЮЧЕН: он
            # медленный и дорогой на каждый сайт. Для глубокой проверки —
            # одиночный режим.
            use_agent=False,
            create_pdf=bool(b_pdf and scan_mode == "deep"),
        )
        log_box.info(f"[{i}/{len(rows)}] Проверка: {site}")

        def cb(msg: str, _s=site) -> None:
            log_box.info(f"[{i}/{len(rows)}] {_s} · {msg}")

        try:
            res = _run_scan_ui(scan_input, settings, on_progress=cb)
        except Exception as exc:
            res = ScanResult(
                scan_id=f"err{i}", company_name=company, site_url=site,
                risk_level=RiskLevel.unknown.value, errors=[str(exc)],
            )
        try:
            repositories.save_scan(res, settings)
        except Exception:
            pass
        results.append(res)
        progress.progress(i / max(1, len(rows)))

    log_box.success(f"Готово. Проверено сайтов: {len(results)}")
    st.session_state["bulk_results"] = [r.scan_id for r in results]

    # Итоговая таблица
    table = [_summary_row(r) for r in results]
    res_df = pd.DataFrame(table)
    st.subheader("Итоги")
    st.dataframe(res_df, use_container_width=True)

    # Экспорт XLSX
    xlsx = _results_to_xlsx(results)
    st.download_button(
        "Скачать результаты (XLSX)",
        data=xlsx,
        file_name=f"scan_results_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # ZIP всех PDF: короткие КП есть у обоих режимов, полный отчёт — только у deep.
    zip_bytes = _pdfs_to_zip(results, settings)
    if zip_bytes:
        st.download_button(
            "Скачать все PDF (ZIP)",
            data=zip_bytes,
            file_name=f"scan_pdfs_{datetime.now():%Y%m%d_%H%M}.zip",
            mime="application/zip",
        )


def _summary_row(r: ScanResult) -> dict:
    docs = {d.doc_type for d in r.documents if d.is_accessible}
    return {
        "company_name": r.company_name,
        "site_url": r.site_url,
        "industry": r.industry,
        "scan_mode": getattr(r, "scan_mode", "quick"),
        "email": r.email,
        "risk_level": r.risk_level,
        "risk_score": r.risk_score,
        "confidence": r.confidence,
        "top_risks": "; ".join(x.title for x in r.top_risks(3)),
        "forms_found": len(r.forms),
        "privacy_policy_found": "privacy_policy" in docs,
        "consent_found": "consent" in docs,
        "cookie_banner_found": r.cookie_banner_found,
        "trackers_found": len(r.trackers),
        "foreign_trackers_found": r.foreign_trackers_found,
        "pdf_path": r.pdf_path,
        "status": r.status,
    }


def _results_to_xlsx(results: List[ScanResult]) -> bytes:
    import pandas as pd

    rows = []
    for r in results:
        row = _summary_row(r)
        row["email_text"] = r.email_text
        row["commercial_offer_text"] = r.commercial_offer_text
        rows.append(row)
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="results")
    return buf.getvalue()


def _pdfs_to_zip(results: List[ScanResult], settings: Settings) -> Optional[bytes]:
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            teaser = _get_or_generate_teaser_pdf(r, settings)
            if teaser:
                zf.writestr(f"kp_short_{r.scan_id}.pdf", teaser)
                count += 1
            data = _pdf_bytes(r.pdf_path)
            if data:
                zf.writestr(f"full_report_{r.scan_id}.pdf", data)
                count += 1
    return buf.getvalue() if count else None


# ---------------------------------------------------------------------------
# Вкладка 3: История
# ---------------------------------------------------------------------------
def tab_history(settings: Settings) -> None:
    import pandas as pd

    _page_header(
        "История проверок",
        "Все результаты, статусы лидов и доступ к сохранённым PDF-отчётам.",
        "CRM-lite",
    )
    try:
        scans = repositories.list_scans(settings, limit=1000)
    except Exception as exc:
        st.error(f"Не удалось загрузить историю: {exc}")
        return

    if not scans:
        st.info("История пуста. Запустите первую проверку во вкладке «Проверка одного сайта».")
        return

    df = pd.DataFrame(scans)
    show_cols = [c for c in [
        "scan_id", "created_at", "company_name", "site_url", "industry",
        "risk_score", "risk_level", "confidence", "top_risks", "status", "pdf_path",
    ] if c in df.columns]
    st.dataframe(df[show_cols], use_container_width=True, height=360)

    st.divider()
    st.subheader("Управление лидом")
    ids = [s["scan_id"] for s in scans]
    sel = st.selectbox(
        "Выберите проверку",
        ids,
        format_func=lambda x: _scan_label(scans, x),
    )
    scan = next((s for s in scans if s["scan_id"] == sel), None)
    if not scan:
        return

    c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
    with c1:
        statuses = [s.value for s in ScanStatus]
        cur = scan.get("status") or ScanStatus.scanned.value
        new_status = st.selectbox(
            "Статус",
            statuses,
            index=statuses.index(cur) if cur in statuses else 0,
            format_func=lambda x: SCAN_STATUS_RU.get(ScanStatus(x), x),
        )
        if st.button("Сохранить статус"):
            try:
                repositories.update_status(sel, new_status, settings)
                st.success("Статус обновлён.")
            except Exception as exc:
                st.error(f"Ошибка: {exc}")
    with c2:
        if st.button("Подробности", use_container_width=True):
            st.session_state["detail_scan_id"] = sel
            st.info("Откройте вкладку «Подробности проверки».")
    with c3:
        res = repositories.get_scan(sel, settings)
        teaser_pdf = _get_or_generate_teaser_pdf(res, settings) if res else None
        if teaser_pdf:
            st.download_button(
                "КП PDF",
                data=teaser_pdf,
                file_name=f"kp_short_{sel}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"hist_teaser_pdf_{sel}",
            )
        else:
            st.caption("КП недоступно")
    with c4:
        res = repositories.get_scan(sel, settings)
        full_allowed = bool(res and (getattr(res, "scan_mode", "quick") == "deep" or res.pdf_path))
        pdf = _get_or_regenerate_pdf(res, settings) if (res and full_allowed) else None
        if pdf:
            st.download_button(
                "Полный PDF",
                data=pdf,
                file_name=f"report_{sel}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"hist_pdf_{sel}",
            )
        else:
            st.caption("Полный PDF недоступен")


def _scan_label(scans: List[dict], scan_id: str) -> str:
    s = next((x for x in scans if x["scan_id"] == scan_id), None)
    if not s:
        return scan_id
    return f"{s.get('created_at','')} · {s.get('company_name') or s.get('site_url')} · {s.get('risk_level')}"


# ---------------------------------------------------------------------------
# Вкладка 4: Подробности
# ---------------------------------------------------------------------------
def tab_details(settings: Settings) -> None:
    _page_header(
        "Подробности проверки",
        "Полная карточка сканирования: страницы, формы, документы, cookie, трекеры, риски и доказательства.",
        "Досье сайта",
    )
    try:
        scans = repositories.list_scans(settings, limit=1000)
    except Exception as exc:
        st.error(f"Не удалось загрузить список: {exc}")
        return
    if not scans:
        st.info("Нет данных.")
        return

    ids = [s["scan_id"] for s in scans]
    default = st.session_state.get("detail_scan_id") or st.session_state.get("last_result")
    idx = ids.index(default) if default in ids else 0
    sel = st.selectbox("scan_id", ids, index=idx, format_func=lambda x: _scan_label(scans, x))

    result = repositories.get_scan(sel, settings)
    if result is None:
        st.error("Не удалось загрузить результат.")
        return

    st.subheader(f"{result.company_name or result.site_url}")
    st.caption(f"{result.site_url} → {result.final_url}")
    render_result_summary(result)

    with st.expander("Проверенные страницы", expanded=False):
        for p in result.pages:
            st.markdown(f"- [{p.title or p.url}]({p.url}) · код {p.status_code} · форм: {len(p.forms)}")

    with st.expander("Формы и согласия"):
        _render_forms(result)

    with st.expander("Документы и чек-листы"):
        _render_documents(result)

    with st.expander("Cookies и сторонние сервисы"):
        _render_cookies_trackers(result)

    with st.expander("Техническая проверка"):
        t = result.technical
        st.write({
            "HTTPS": t.https_enabled,
            "Редирект на HTTPS": t.http_to_https_redirect,
            "Mixed content": t.mixed_content_found,
            "IP": ", ".join(t.ip_addresses) or "—",
            "Страна сервера": t.server_country,
            "CDN": t.cdn_detected or "—",
            "robots.txt": t.robots_txt_found,
            "sitemap": t.sitemap_found,
        })

    render_texts_and_downloads(result)


def _render_forms(result: ScanResult) -> None:
    import pandas as pd

    if not result.forms:
        st.write("Формы не обнаружены.")
        return
    rows = []
    for f in result.forms:
        rows.append({
            "страница": f.page_url,
            "тип": f.form_type,
            "поля": ", ".join(f.personal_data_fields) or "—",
            "чекбокс": "да" if f.consent.checkbox_found else "нет",
            "ссылка на политику": "да" if f.consent.privacy_link_found else "нет",
            "предустановлен": {True: "да", False: "нет", None: "?"}.get(f.consent.checkbox_prechecked, "?"),
            "ПДн": "да" if f.potentially_personal_data_form else "нет",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


def _render_documents(result: ScanResult) -> None:
    if not result.documents:
        st.write("Документы не обнаружены.")
        return
    for d in result.documents:
        st.markdown(f"**{d.doc_type}** — [{d.title or d.url}]({d.url}) · формат {d.format} · "
                    f"{'доступен' if d.is_accessible else 'недоступен'}")
        if d.template_placeholder_detected:
            st.warning("Обнаружены признаки шаблонных незаполненных полей.")
        if d.extraction_error:
            st.caption(f"Извлечение текста: {d.extraction_error}")
        an = d.analysis
        if an:
            st.caption(f"Полнота: {an.overall_completeness}/100 · "
                       f"{'LLM' if an.llm_used else 'эвристика'}")
            if an.summary:
                st.write(an.summary)
            missing = [i for i in an.checklist_results
                       if i.status == "not_found" and i.risk_if_missing in ("high", "critical")]
            if missing:
                st.markdown("_Критичные отсутствующие элементы:_")
                for i in missing[:12]:
                    st.markdown(f"- {i.label} ({i.risk_if_missing})")
            if an.conflicts:
                st.markdown("_Конфликты:_")
                for c in an.conflicts:
                    st.markdown(f"- {c.type}: {c.comment}")
        st.divider()


def _render_cookies_trackers(result: ScanResult) -> None:
    import pandas as pd

    st.write(f"Cookie-баннер: **{'найден' if result.cookie_banner_found else 'не найден'}**")
    st.write(f"Маркетинговые cookies/запросы до согласия: "
             f"**{'да' if result.marketing_cookies_before_consent else 'нет'}**")
    if result.trackers:
        st.dataframe(pd.DataFrame([{
            "провайдер": t.provider_name,
            "категория": t.category,
            "страна": t.country_hint,
            "риск": t.legal_risk,
            "домен": t.matched_domain,
        } for t in result.trackers]), use_container_width=True)
    else:
        st.write("Трекеры не обнаружены.")
    if result.cookies:
        st.markdown("_Cookies (до взаимодействия):_")
        st.dataframe(pd.DataFrame([{
            "имя": c.name, "провайдер": c.provider, "категория": c.category,
            "трекинг": c.is_tracking,
        } for c in result.cookies]), use_container_width=True)


# ---------------------------------------------------------------------------
# Вкладка 5: Настройки
# ---------------------------------------------------------------------------
def tab_settings(settings: Settings) -> None:
    _page_header(
        "Настройки",
        "Изменения сохраняются в базе данных и переживают перезапуск сервиса. "
        "Секреты (пароль администратора, SESSION_SECRET, DATABASE_URL) задаются "
        "через переменные окружения хостинга.",
        "Конфигурация",
    )

    with st.form("settings_form"):
        st.subheader("LLM")
        cp, c1 = st.columns([1, 2])
        llm_provider = cp.text_input("LLM provider", value=settings.llm_provider)
        llm_api_key = c1.text_input("LLM API key", value=settings.llm_api_key, type="password")
        c2, c3 = st.columns(2)
        llm_base_url = c2.text_input("LLM base URL", value=settings.llm_base_url)
        llm_model = c3.text_input("LLM model", value=settings.llm_model)
        enable_llm = st.checkbox("Включить LLM", value=settings.enable_llm)

        st.subheader("Сканирование")
        c4, c5, c6 = st.columns(3)
        max_pages = c4.number_input("Максимум страниц", 1, 200, int(settings.max_pages))
        page_timeout = c5.number_input("Таймаут страницы (мс)", 1000, 120000, int(settings.page_timeout_ms), step=1000)
        delay = c6.number_input("Задержка между страницами (с)", 0.0, 30.0, float(settings.delay_between_pages_s), step=0.5)
        c7, c8, c9 = st.columns(3)
        enable_geoip = c7.checkbox("Включить GeoIP", value=settings.enable_geoip)
        enable_shots = c8.checkbox("Включить скриншоты", value=settings.enable_screenshots)
        geoip_db = c9.text_input("Путь к GeoIP базе (mmdb)", value=settings.geoip_db_path)

        st.subheader("Юридическое бюро")
        c10, c11 = st.columns(2)
        firm_name = c10.text_input("Название бюро", value=settings.firm_name)
        lawyer_name = c11.text_input("ФИО юриста/менеджера", value=settings.lawyer_name)
        firm_address = st.text_input("Адрес", value=settings.firm_address)
        c12, c13, c14 = st.columns(3)
        firm_email = c12.text_input("Email бюро", value=settings.firm_email)
        firm_phone = c13.text_input("Телефон бюро", value=settings.firm_phone)
        firm_website = c14.text_input("Сайт бюро", value=settings.firm_website)
        firm_contacts = st.text_input("Доп. контакты", value=settings.firm_contacts)
        logo_path = st.text_input("Путь к логотипу для PDF (необязательно)", value=settings.logo_path)

        st.subheader("Цены услуг")
        c15, c16 = st.columns(2)
        price_express_audit = c15.text_input("Экспресс-аудит", value=settings.price_express_audit)
        price_express_docs = c16.text_input("Комплект документов для сайта", value=settings.price_express_docs)
        c17, c18 = st.columns(2)
        price_full_audit = c17.text_input("Полный аудит 152-ФЗ", value=settings.price_full_audit)
        price_turnkey = c18.text_input("Сопровождение под ключ", value=settings.price_turnkey)

        saved = st.form_submit_button("Сохранить настройки", use_container_width=True)

    if saved:
        new = settings.model_copy(update=dict(
            llm_provider=llm_provider,
            llm_api_key=llm_api_key, llm_base_url=llm_base_url, llm_model=llm_model,
            enable_llm=enable_llm, max_pages=int(max_pages), page_timeout_ms=int(page_timeout),
            delay_between_pages_s=float(delay), enable_geoip=enable_geoip,
            enable_screenshots=enable_shots, geoip_db_path=geoip_db,
            firm_name=firm_name, lawyer_name=lawyer_name, firm_address=firm_address,
            firm_email=firm_email, firm_phone=firm_phone, firm_website=firm_website,
            firm_contacts=firm_contacts, logo_path=logo_path,
            price_express_audit=price_express_audit, price_express_docs=price_express_docs,
            price_full_audit=price_full_audit, price_turnkey=price_turnkey,
        ))
        save_settings(new)
        st.session_state["settings"] = new
        st.success("Настройки сохранены (в базе данных).")

    # --- Управление доступом администратора ---
    st.divider()
    st.subheader("Администратор и доступ")
    st.caption(f"Логин администратора: **{settings.admin_email}** "
               "(меняется через переменную окружения ADMIN_EMAIL).")
    with st.form("admin_pw_form"):
        st.markdown("**Смена пароля администратора**")
        p1 = st.text_input("Новый пароль (мин. 8 символов)", type="password")
        p2 = st.text_input("Повторите новый пароль", type="password")
        pw_saved = st.form_submit_button("Обновить пароль")
    if pw_saved:
        if p1 != p2:
            st.error("Пароли не совпадают.")
        else:
            ok, msg = auth.set_admin_password(p1, settings)
            (st.success if ok else st.error)(msg)
            if ok:
                st.session_state["settings"] = load_settings()

    # --- Инфраструктура (только чтение) ---
    with st.expander("Инфраструктура (переменные окружения)"):
        st.write({
            "База данных": "PostgreSQL (DATABASE_URL)" if settings.database_url else f"SQLite: {settings.db_path}",
            "Каталог экспорта (EXPORTS_DIR)": settings.exports_dir,
            "LLM включён": settings.enable_llm,
            "GeoIP включён": settings.enable_geoip,
            "Скриншоты включены": settings.enable_screenshots,
        })
        st.caption("Эти параметры задаются переменными окружения на хостинге и здесь не редактируются.")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def tab_dashboard(settings: Settings) -> None:
    _page_header(
        "Дашборд",
        "Оперативный обзор проверок, уровня риска и последних лидов.",
        "Nexora Legal Compliance Scanner",
    )
    stats = repositories.dashboard_stats(settings)

    by = stats.get("by_level", {})
    _metric_grid([
        {
            "label": "Всего проверок",
            "value": stats.get("total", 0),
            "note": "Сохранено в истории",
            "class": "muted",
        },
        {
            "label": "Высокий / критический",
            "value": stats.get("high_critical", 0),
            "note": "Нужно приоритизировать",
            "class": "high",
        },
        {
            "label": "Критический",
            "value": by.get("critical", 0),
            "note": "Требует ручной проверки",
            "class": "critical",
        },
        {
            "label": "Высокий",
            "value": by.get("high", 0),
            "note": "Зоны риска",
            "class": "high",
        },
    ])

    if st.button("Новая проверка", type="primary"):
        st.session_state["nav"] = "Проверка одного сайта"
        st.rerun()

    st.subheader("Последние проверки")
    recent = stats.get("recent", [])
    if not recent:
        st.info("Проверок пока нет. Нажмите «Новая проверка».")
        return
    import pandas as pd

    cols = [c for c in ["created_at", "company_name", "site_url", "industry",
                        "risk_level", "risk_score", "confidence", "status"] if recent and c in recent[0]]
    st.dataframe(pd.DataFrame(recent)[cols], use_container_width=True, height=320)


# ---------------------------------------------------------------------------
# Документация / ограничения
# ---------------------------------------------------------------------------
def tab_docs(settings: Settings) -> None:
    _page_header(
        "Документация и ограничения",
        "Как корректно использовать результаты автоматической проверки и не делать категоричных юридических выводов.",
        "Методология",
    )
    st.markdown(
        """
### Что делает система
Автоматический экспресс-анализ **публично доступной** части сайта на признаки
возможных рисков и несоответствий требованиям законодательства РФ о персональных
данных (152-ФЗ), а также смежных рисков по cookie, рекламным рассылкам, публичным
документам, сторонним сервисам и трансграничной передаче.

### Важное ограничение
Выводы системы — это **признаки риска / зоны риска, требующие проверки**, а **не**
окончательное юридическое заключение и не установление факта нарушения.

Автоматическая проверка видит только публичную часть сайта и **не имеет доступа** к:
CRM, CMS/admin-панели, базе данных, серверной логике, договорам с обработчиками,
внутренним локальным актам, журналам согласий, уведомлениям РКН, фактическому месту
хранения базы, процессам удаления/уточнения ПДн и переписке с субъектами ПДн.

### Как корректно использовать результаты
- Рассматривайте отчёт как **основу для ручной проверки юристом**.
- Перед отправкой клиенту отчёт, коммерческое предложение и письмо **должен проверить
  юрист**.
- Формулировки в отчёте намеренно нейтральны («обнаружены признаки», «требуется
  проверка») — сохраняйте этот тон в коммуникации с клиентом.
- Система **не отправляет** письма и КП автоматически — только формирует тексты.

### Правила сканирования
Проверяются только публичные страницы; авторизация и CAPTCHA не обходятся; формы
не отправляются; нагрузочное тестирование и пентест не выполняются; соблюдаются
лимит страниц и задержки; используется идентифицирующий user-agent.
        """
    )


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------
def _login_screen(settings: Settings) -> None:
    left, right = st.columns([1.1, 0.9], gap="large")
    with left:
        st.markdown(
            f"""
<div class="login-brand">
    {_brand_lockup(settings)}
    <div>
        <div class="login-title">Экспресс-аудит сайтов по 152-ФЗ</div>
        <div class="login-copy">
            Внутренний продукт Nexora Legal для быстрой проверки публичной части сайта,
            документов, форм, cookies и сторонних сервисов.
        </div>
    </div>
    <div class="muted-copy">Автоматические выводы являются признаками риска и требуют проверки юристом.</div>
</div>
            """,
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            """
<div class="login-card">
    <div class="section-label">Secure access</div>
    <div class="section-title">Вход в систему</div>
    <div class="muted-copy">Доступ только для авторизованных сотрудников бюро.</div>
</div>
            """,
            unsafe_allow_html=True,
        )
    if not auth.is_configured(settings):
        st.error(
            "Пароль администратора не задан. Установите переменную окружения "
            "**ADMIN_PASSWORD** (и **ADMIN_EMAIL**) на хостинге и перезапустите сервис."
        )
    with right:
        with st.form("login_form"):
            email = st.text_input("Email", value="")
            password = st.text_input("Пароль", type="password")
            submitted = st.form_submit_button("Войти", use_container_width=True)
    if submitted:
        if auth.check_credentials(email, password, settings):
            st.session_state["authenticated"] = True
            st.session_state["auth_email"] = email.strip()
            # Токен в query-параметре переживает перезагрузку страницы.
            token = auth.issue_session_token(email.strip(), settings)
            if token:
                try:
                    st.query_params["auth"] = token
                except Exception:
                    pass
            st.rerun()
        else:
            st.error("Неверный email или пароль.")
    st.caption("Nexora Legal · internal compliance scanner")


def _is_authenticated() -> bool:
    return bool(st.session_state.get("authenticated"))


def _restore_session_from_token(settings: Settings) -> None:
    """Восстановить вход по токену из URL (после перезагрузки страницы)."""
    if st.session_state.get("authenticated"):
        return
    try:
        token = st.query_params.get("auth", "")
    except Exception:
        token = ""
    if not token:
        return
    email = auth.verify_session_token(token, settings)
    if email:
        st.session_state["authenticated"] = True
        st.session_state["auth_email"] = email
    else:
        # Просроченный/битый токен — убираем из URL.
        try:
            if "auth" in st.query_params:
                del st.query_params["auth"]
        except Exception:
            pass


def _logout() -> None:
    """Выход: чистим сессию и токен в URL."""
    st.session_state["authenticated"] = False
    st.session_state.pop("auth_email", None)
    try:
        if "auth" in st.query_params:
            del st.query_params["auth"]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
MENU = [
    "Дашборд",
    "Проверка одного сайта",
    "Массовая проверка",
    "История",
    "Подробности проверки",
    "Настройки",
    "Документация",
]

PAGES = {
    "Дашборд": tab_dashboard,
    "Проверка одного сайта": tab_single,
    "Массовая проверка": tab_bulk,
    "История": tab_history,
    "Подробности проверки": tab_details,
    "Настройки": tab_settings,
    "Документация": tab_docs,
}


def main() -> None:
    settings = get_settings()
    _inject_design_system()
    ensure_db(settings)

    # Восстановление входа по токену из URL (переживает перезагрузку страницы).
    _restore_session_from_token(settings)

    if not _is_authenticated():
        _login_screen(settings)
        return

    # --- Боковое меню ---
    _sidebar_brand(settings)
    st.sidebar.markdown(f"**{_escape(settings.firm_name or 'Экспресс-аудит')}**")
    st.sidebar.caption(f"Вы вошли как: {st.session_state.get('auth_email', settings.admin_email)}")
    if st.sidebar.button("Выйти"):
        _logout()
        st.rerun()

    if not auth.secret_is_secure(settings):
        st.sidebar.warning(
            "SESSION_SECRET не задан — «запомнить вход» отключено (после "
            "перезагрузки нужно логиниться заново). Задайте переменную окружения "
            "SESSION_SECRET (длинную случайную строку) на хостинге."
        )
    if not (settings.enable_llm and settings.llm_api_key):
        st.sidebar.warning("LLM не настроена — только эвристический анализ.")

    nav = st.session_state.get("nav", MENU[0])
    if nav not in MENU:
        nav = MENU[0]
    choice = st.sidebar.radio("Меню", MENU, index=MENU.index(nav))
    st.session_state["nav"] = choice

    st.sidebar.divider()
    st.sidebar.caption(
        "Автоматическая проверка не заменяет юридический аудит. "
        "Выводы — признаки риска, требующие проверки юристом."
    )

    page = PAGES.get(choice, tab_dashboard)
    page(settings)


if __name__ == "__main__":
    main()
