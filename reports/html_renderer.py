"""
Формирование HTML-отчёта юридического бюро.

Основной путь — рендеринг Jinja2-шаблона `templates/report_template.html` с
инлайновым CSS из `templates/report_style.css`. Если Jinja2 недоступен, работает
минимальный запасной рендер на строках Python (без внешних зависимостей).

Все пользовательские данные экранируются. Функция никогда не бросает исключение:
при любой ошибке возвращается безопасный HTML с дисклеймером.
"""
from __future__ import annotations

import base64
import html as _html
import mimetypes
import os
from typing import Any, Dict, List, Optional

from scanner.models import (
    ChecklistStatus,
    DOC_TYPE_RU,
    RISK_LEVEL_RU,
    RiskLevel,
    ScanResult,
)

# --- Дисклеймер / ограничения из legal.legal_basis (с запасными значениями) ---
try:
    from legal.legal_basis import (
        DISCLAIMER_FULL,
        DISCLAIMER_SHORT,
        MANUAL_REVIEW_NOTE,
        SCOPE_LIMITATIONS,
    )
except Exception:  # pragma: no cover - модуль всегда должен импортироваться
    DISCLAIMER_FULL = (
        "Отчёт сформирован автоматически по результатам анализа публично "
        "доступных страниц сайта и не является юридическим заключением. Все "
        "выводы носят характер признаков риска и требуют подтверждения юристом."
    )
    DISCLAIMER_SHORT = (
        "Отчёт сформирован автоматически и не является юридическим заключением. "
        "Все выводы — признаки риска, требующие проверки юристом."
    )
    MANUAL_REVIEW_NOTE = (
        "Данный отчёт носит предварительный характер и требует ручной проверки "
        "юристом."
    )
    SCOPE_LIMITATIONS = [
        "Система не имеет доступа к внутренним системам и базам данных оператора.",
    ]


_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
# Каталог data/ в корне проекта (…/reports/html_renderer.py -> корень на уровень выше).
_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# Отчёт капает баллы риска на 100 (шкала «N / 100»): если движок вернул больше,
# в отчёте всё равно показываем 100.
_SCORE_CAP = 100

# Человекочитаемые подписи для статусов пунктов чек-листа.
CHECKLIST_STATUS_RU = {
    ChecklistStatus.found.value: "есть",
    ChecklistStatus.not_found.value: "не обнаружено",
    ChecklistStatus.unclear.value: "требует проверки",
    ChecklistStatus.not_applicable.value: "не применимо",
}

COUNTRY_HINT_RU = {
    "RU": "Россия",
    "foreign": "зарубежный",
    "unknown": "не определено",
}

# Цветовая точка + подпись для статусов ядро-чеклиста (без эмодзи — WeasyPrint
# не имеет эмодзи-шрифта, поэтому используем цветные CSS-точки и текст).
CORE_STATUS_DOT = {
    "ok": "ok",
    "risk": "risk",
    "unclear": "unclear",
    "not_applicable": "na",
}

# Отрасль "auto" — это значение по умолчанию (не выбрана пользователем), а не
# реальная отрасль «авто». В отчёте показываем нейтральное «не указана».
_INDUSTRY_ALIASES = {
    "auto": "не указана",
    "": "не указана",
}

# Встроенный запасной список возможной ответственности (нейтральные, справочные
# формулировки). Используется, если data/liability.yml отсутствует/нечитаем.
# Схема совпадает с data/liability.yml (владелец — агент B).
_DEFAULT_LIABILITY = {
    "disclaimer": (
        "Суммы приведены исключительно справочно как возможная ответственность по "
        "действующему законодательству и не являются утверждением о нарушении. "
        "Итоговая квалификация и размер ответственности определяются юристом с "
        "учётом конкретных обстоятельств."
    ),
    "items": [
        {
            "label": "Обработка ПДн без надлежащего согласия",
            "basis": "ст. 13.11 КоАП РФ",
            "amount": "до 700 000 ₽",
        },
        {
            "label": "Неопубликование/неполнота политики обработки ПДн",
            "basis": "ст. 13.11 КоАП РФ",
            "amount": "до 100 000 ₽",
        },
        {
            "label": "Нарушение требований к трансграничной передаче ПДн",
            "basis": "КоАП РФ",
            "amount": "отдельный состав",
        },
    ],
}


def _clamp_score(value: Any) -> int:
    """Привести балл риска к целому в диапазоне 0..100. Никогда не бросает."""
    try:
        n = int(value)
    except Exception:
        return 0
    if n < 0:
        return 0
    if n > _SCORE_CAP:
        return _SCORE_CAP
    return n


def _industry_ru(industry: Any) -> str:
    """Человекочитаемая отрасль: 'auto'/пусто -> «не указана»."""
    raw = str(industry or "").strip()
    return _INDUSTRY_ALIASES.get(raw.lower(), raw) or "не указана"


def _load_liability() -> Dict[str, Any]:
    """Прочитать data/liability.yml. При любой ошибке — встроенный запасной список.

    Возвращает словарь {"disclaimer": str, "items": [{"label","basis","amount"}]}.
    Никогда не бросает исключение.
    """
    path = os.path.join(_DATA_DIR, "liability.yml")
    try:
        import yaml  # type: ignore

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            return dict(_DEFAULT_LIABILITY)
        raw_items = data.get("items")
        items: List[Dict[str, str]] = []
        if isinstance(raw_items, list):
            for it in raw_items:
                if not isinstance(it, dict):
                    continue
                label = str(it.get("label") or "").strip()
                if not label:
                    continue
                items.append(
                    {
                        "label": label,
                        "basis": str(it.get("basis") or "").strip(),
                        "amount": str(it.get("amount") or "").strip(),
                    }
                )
        if not items:
            return dict(_DEFAULT_LIABILITY)
        disclaimer = str(data.get("disclaimer") or "").strip()
        if not disclaimer:
            disclaimer = _DEFAULT_LIABILITY["disclaimer"]
        return {"disclaimer": disclaimer, "items": items}
    except Exception:
        return dict(_DEFAULT_LIABILITY)


# ---------------------------------------------------------------------------
# Вспомогательные функции загрузки ресурсов
# ---------------------------------------------------------------------------
def _read_template_file(name: str) -> str:
    path = os.path.join(_TEMPLATES_DIR, name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except Exception:
        return ""


def _inline_css() -> str:
    return _read_template_file("report_style.css")


def _logo_data_uri(settings: Any) -> str:
    """Вернуть data-URI логотипа для встраивания в HTML, либо пустую строку."""
    path = getattr(settings, "logo_path", "") or ""
    if not path:
        return ""
    try:
        if not os.path.isfile(path):
            return ""
        mime, _ = mimetypes.guess_type(path)
        if not mime or not mime.startswith("image"):
            mime = "image/png"
        with open(path, "rb") as fh:
            data = fh.read()
        if not data:
            return ""
        encoded = base64.b64encode(data).decode("ascii")
        return "data:{};base64,{}".format(mime, encoded)
    except Exception:
        return ""


def _firm_contact_lines(settings: Any) -> List[str]:
    lines: List[str] = []
    for attr, prefix in (
        ("firm_name", ""),
        ("lawyer_name", ""),
        ("firm_address", ""),
        ("firm_phone", "Телефон: "),
        ("firm_email", "E-mail: "),
        ("firm_website", "Сайт: "),
        ("firm_contacts", ""),
    ):
        val = (getattr(settings, attr, "") or "").strip()
        if val:
            lines.append(prefix + val)
    return lines


# ---------------------------------------------------------------------------
# Подготовка данных
# ---------------------------------------------------------------------------
def _ordered_packages(packages: Optional[dict], settings: Any) -> List[Dict[str, Any]]:
    """Собрать список пакетов в порядке packages['order'] с подставленными ценами."""
    if not isinstance(packages, dict):
        return []
    raw_items = packages.get("packages") or []
    by_id: Dict[str, dict] = {}
    for p in raw_items:
        if isinstance(p, dict) and p.get("id"):
            by_id[str(p.get("id"))] = p
    order = packages.get("order")
    if not isinstance(order, list) or not order:
        order = list(by_id.keys())

    try:
        prices = settings.prices() if hasattr(settings, "prices") else {}
    except Exception:
        prices = {}

    out: List[Dict[str, Any]] = []
    for pid in order:
        p = by_id.get(str(pid))
        if not p:
            continue
        price_key = p.get("price_key") or pid
        price = ""
        try:
            price = (prices.get(price_key) or "").strip()
        except Exception:
            price = ""
        items = p.get("items") or []
        if not isinstance(items, list):
            items = []
        out.append(
            {
                "id": pid,
                "title": str(p.get("title") or ""),
                "duration": str(p.get("duration") or ""),
                "description": str(p.get("description") or ""),
                "price": price,
                "items": [str(x) for x in items],
            }
        )
    return out


def _doc_completeness(doc: Any) -> Optional[int]:
    """Полнота документа (%) из analysis.overall_completeness, либо None."""
    analysis = getattr(doc, "analysis", None)
    if analysis is None:
        return None
    try:
        return int(getattr(analysis, "overall_completeness", 0) or 0)
    except Exception:
        return None


def _doc_open_items(doc: Any, limit: int = 3) -> List[str]:
    """2-3 главных незакрытых пункта документа (по чек-листу его analysis).

    Незакрытыми считаются пункты со статусом not_found/unclear. Возвращаем
    подписи (label) в порядке значимости (сначала not_found, затем unclear).
    """
    analysis = getattr(doc, "analysis", None)
    if analysis is None:
        return []
    not_found: List[str] = []
    unclear: List[str] = []
    try:
        for item in getattr(analysis, "checklist_results", None) or []:
            status = getattr(item, "status", "") or ""
            label = str(getattr(item, "label", "") or "").strip()
            if not label:
                continue
            if status == ChecklistStatus.not_found.value:
                not_found.append(label)
            elif status == ChecklistStatus.unclear.value:
                unclear.append(label)
    except Exception:
        return []
    return (not_found + unclear)[:limit]


def _unique_documents(result: ScanResult) -> List[Dict[str, Any]]:
    """Уникальные найденные документы для кратких карточек.

    Берём только реально существующие документы (is_accessible или
    link_confirmed), пропуская неподтверждённые «догадки». Дедупликация по
    (doc_type, url). Порядок появления сохраняется. Для каждого документа —
    компактная сводка: тип, URL, полнота %, 2-3 главных незакрытых пункта.
    """
    seen = set()
    out: List[Dict[str, Any]] = []
    try:
        for doc in getattr(result, "documents", None) or []:
            accessible = bool(getattr(doc, "is_accessible", False))
            confirmed = bool(getattr(doc, "link_confirmed", False))
            if not accessible and not confirmed:
                continue
            dt = getattr(doc, "doc_type", "other") or "other"
            url = str(getattr(doc, "url", "") or "").strip()
            key = (dt, url)
            if key in seen:
                continue
            seen.add(key)
            title = str(getattr(doc, "title", "") or "").strip()
            out.append(
                {
                    "doc_type": dt,
                    "type_ru": DOC_TYPE_RU.get(dt, dt),
                    "title": title,
                    "url": url,
                    "format": str(getattr(doc, "format", "") or "html"),
                    "completeness": _doc_completeness(doc),
                    "has_analysis": getattr(doc, "analysis", None) is not None,
                    "open_items": _doc_open_items(doc),
                    "placeholder": bool(getattr(doc, "template_placeholder_detected", False)),
                    "manual_only": (
                        confirmed and not accessible
                    ) or getattr(doc, "analysis", None) is None,
                }
            )
    except Exception:
        return []
    return out


def _recommendations(result: ScanResult) -> Dict[str, List[Any]]:
    """Сгруппировать риски по срочности исправления."""
    urgent: List[Any] = []
    soon: List[Any] = []
    optional: List[Any] = []
    for risk in result.risks:
        lvl = getattr(risk, "risk_level", "") or ""
        if lvl in (RiskLevel.critical.value, RiskLevel.high.value):
            urgent.append(risk)
        elif lvl == RiskLevel.medium.value:
            soon.append(risk)
        else:
            optional.append(risk)
    return {"urgent": urgent, "soon": soon, "optional": optional}


def _core_status_ru() -> Dict[str, str]:
    """RU-подписи статусов ядро-чеклиста (мягкий импорт — не роняем рендер)."""
    try:
        from scanner.models import CORE_CHECK_STATUS_RU

        return dict(CORE_CHECK_STATUS_RU)
    except Exception:
        return {
            "ok": "выполнено",
            "risk": "зона риска",
            "unclear": "требует проверки",
            "not_applicable": "не применимо",
        }


def _summary_metrics(result: ScanResult) -> Dict[str, int]:
    """4 метрики-плитки для сводки. Никогда не бросает."""
    risk_zones = 0
    try:
        for it in getattr(result, "core_checklist", None) or []:
            if getattr(it, "status", "") == "risk":
                risk_zones += 1
    except Exception:
        risk_zones = 0

    docs_found = 0
    try:
        for d in getattr(result, "documents", None) or []:
            if getattr(d, "is_accessible", False) or getattr(d, "link_confirmed", False):
                docs_found += 1
    except Exception:
        docs_found = 0

    foreign = 0
    try:
        for tr in getattr(result, "trackers", None) or []:
            if getattr(tr, "country_hint", "") == "foreign":
                foreign += 1
    except Exception:
        foreign = 0

    return {
        "risk_zones": risk_zones,
        "pages_checked": int(getattr(result, "pages_checked", 0) or 0),
        "docs_found": docs_found,
        "foreign_services": foreign,
    }


def _top_risk_cards(top_risks: List[Any], risk_level_ru_map: Dict[str, str]) -> List[Dict[str, str]]:
    """Компактные карточки top-рисков: title + report_phrase + уровень."""
    cards: List[Dict[str, str]] = []
    for risk in top_risks or []:
        lvl = getattr(risk, "risk_level", "") or ""
        phrase = (
            getattr(risk, "report_phrase", "")
            or getattr(risk, "client_friendly_explanation", "")
            or ""
        )
        cards.append(
            {
                "title": str(getattr(risk, "title", "") or ""),
                "phrase": str(phrase),
                "level": str(lvl),
                "level_ru": risk_level_ru_map.get(lvl, lvl),
            }
        )
    return cards


def _build_context(result: ScanResult, settings: Any, packages: Optional[dict]) -> Dict[str, Any]:
    risk_level = getattr(result, "risk_level", RiskLevel.unknown.value) or RiskLevel.unknown.value
    try:
        risk_level_ru = RISK_LEVEL_RU.get(RiskLevel(risk_level), risk_level)
    except Exception:
        risk_level_ru = risk_level

    risk_level_ru_map = {lvl.value: RISK_LEVEL_RU[lvl] for lvl in RiskLevel}

    try:
        top_risks = result.top_risks(3)
    except Exception:
        top_risks = list(result.risks)[:3]

    liability = _load_liability()

    return {
        "result": result,
        "settings": settings,
        "packages": _ordered_packages(packages, settings),
        "prices": (settings.prices() if hasattr(settings, "prices") else {}),
        "RISK_LEVEL_RU": RISK_LEVEL_RU,
        "risk_level_ru": risk_level_ru,
        "risk_level_ru_map": risk_level_ru_map,
        "risk_score_display": _clamp_score(getattr(result, "risk_score", 0)),
        "industry_ru": _industry_ru(getattr(result, "industry", "")),
        "DOC_TYPE_RU": DOC_TYPE_RU,
        "checklist_status_ru": CHECKLIST_STATUS_RU,
        "core_status_ru": _core_status_ru(),
        "core_status_dot": CORE_STATUS_DOT,
        "country_hint_ru": COUNTRY_HINT_RU,
        "disclaimer": DISCLAIMER_FULL,
        "disclaimer_short": DISCLAIMER_SHORT,
        "manual_review_note": MANUAL_REVIEW_NOTE,
        "scope_limitations": list(SCOPE_LIMITATIONS),
        "top_risks": top_risks,
        "top_risk_cards": _top_risk_cards(top_risks, risk_level_ru_map),
        "metrics": _summary_metrics(result),
        "liability": liability,
        "generated_at": getattr(result, "created_at", "") or "",
        "unique_documents": _unique_documents(result),
        "recommendations": _recommendations(result),
        "logo_data_uri": _logo_data_uri(settings),
        "firm_contact_lines": _firm_contact_lines(settings),
        "inline_css": _inline_css(),
    }


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def render_report_html(result: ScanResult, settings: Any, packages: Optional[dict]) -> str:
    """Сформировать полный HTML-отчёт. Никогда не бросает исключение."""
    try:
        context = _build_context(result, settings, packages)
    except Exception as exc:  # pragma: no cover
        return _fallback_error_html(result, "Не удалось подготовить данные отчёта: " + str(exc))

    # Основной путь — Jinja2.
    try:
        import jinja2

        template_src = _read_template_file("report_template.html")
        if template_src:
            env = jinja2.Environment(
                autoescape=jinja2.select_autoescape(["html", "xml"]),
                trim_blocks=True,
                lstrip_blocks=True,
            )
            # CSS вставляется через фильтр {{ inline_css|safe }} в шаблоне, поэтому
            # автоэкранирование его не портит и обёртка Markup не нужна (в Jinja2
            # 3.x jinja2.Markup удалён).
            template = env.from_string(template_src)
            html = template.render(**context)
            if html and html.strip():
                return html
    except Exception:
        # Падаем в запасной рендер.
        pass

    # Запасной путь — рендер на строках Python.
    try:
        return _render_plain(context)
    except Exception as exc:  # pragma: no cover
        return _fallback_error_html(result, "Ошибка формирования отчёта: " + str(exc))


# ---------------------------------------------------------------------------
# Запасной рендер (без Jinja2)
# ---------------------------------------------------------------------------
def _e(value: Any) -> str:
    """Экранировать значение для вставки в HTML."""
    if value is None:
        return ""
    return _html.escape(str(value), quote=True)


def _render_plain(ctx: Dict[str, Any]) -> str:
    result: ScanResult = ctx["result"]
    settings = ctx["settings"]
    parts: List[str] = []
    a = parts.append

    css = ctx.get("inline_css", "")
    a("<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">")
    title = "Экспресс-анализ рисков по 152-ФЗ"
    if result.company_name:
        title += " — " + result.company_name
    a("<title>" + _e(title) + "</title>")
    a("<style>" + css + "</style>")
    a("</head><body><div class=\"report\">")

    level = _e(getattr(result, "risk_level", "") or "unknown")
    level_ru = _e(ctx.get("risk_level_ru", ""))
    score_display = _e(ctx.get("risk_score_display", 0))

    # 1. Обложка + вердикт
    a("<section class=\"cover\">")
    a("<div class=\"cover-brand\">")
    logo = ctx.get("logo_data_uri", "")
    if logo:
        a("<img class=\"logo\" src=\"" + _e(logo) + "\" alt=\"" + _e(getattr(settings, "firm_name", "")) + "\">")
    elif getattr(settings, "firm_name", ""):
        a("<div class=\"firm-name\">" + _e(settings.firm_name) + "</div>")
    a("</div>")
    a("<div class=\"cover-title\">Экспресс-анализ рисков по 152-ФЗ</div>")
    a("<div class=\"cover-subtitle\">Проверка публичной части сайта на признаки риска в области персональных данных</div>")
    a("<div class=\"cover-meta\">")
    if result.company_name:
        a("<div><span class=\"label\">Организация</span><span class=\"val\">" + _e(result.company_name) + "</span></div>")
    a("<div><span class=\"label\">Сайт</span><span class=\"val\">" + _e(result.site_url) + "</span></div>")
    a("<div><span class=\"label\">Отрасль</span><span class=\"val\">" + _e(ctx.get("industry_ru", "")) + "</span></div>")
    a("<div><span class=\"label\">Дата</span><span class=\"val\">" + _e(ctx.get("generated_at", "")) + "</span></div>")
    a("</div>")
    a("<div class=\"verdict verdict-" + level + "\">")
    a("<div class=\"verdict-col verdict-level\"><div class=\"verdict-cap\">Уровень риска</div><div class=\"verdict-word\">" + level_ru + "</div></div>")
    a("<div class=\"verdict-col verdict-score\"><div class=\"verdict-cap\">Балл риска</div><div class=\"verdict-num\">" + score_display + " <span class=\"verdict-den\">/ 100</span></div></div>")
    a("<div class=\"verdict-col verdict-conf\"><div class=\"verdict-cap\">Достоверность</div><div class=\"verdict-num\">" + _e(result.confidence) + "<span class=\"verdict-den\">%</span></div></div>")
    a("</div>")
    a("<div class=\"disclaimer-box cover-disclaimer\">" + _e(ctx.get("disclaimer_short", "")) + "</div>")
    a("</section>")

    # 2. Сводка: плитки + топ-риски
    metrics = ctx.get("metrics", {})
    a("<section class=\"section\"><h2>Сводка</h2>")
    a("<div class=\"tiles\">")
    a("<div class=\"tile tile-risk\"><div class=\"tile-num\">" + _e(metrics.get("risk_zones", 0)) + "</div><div class=\"tile-cap\">зон риска</div></div>")
    a("<div class=\"tile\"><div class=\"tile-num\">" + _e(metrics.get("pages_checked", 0)) + "</div><div class=\"tile-cap\">страниц проверено</div></div>")
    a("<div class=\"tile\"><div class=\"tile-num\">" + _e(metrics.get("docs_found", 0)) + "</div><div class=\"tile-cap\">документов найдено</div></div>")
    a("<div class=\"tile\"><div class=\"tile-num\">" + _e(metrics.get("foreign_services", 0)) + "</div><div class=\"tile-cap\">иностранных сервисов</div></div>")
    a("</div>")
    top_cards = ctx.get("top_risk_cards", [])
    if top_cards:
        a("<h3 class=\"sub\">Главные зоны риска</h3><div class=\"top-risks\">")
        for card in top_cards:
            clvl = _e(card.get("level", ""))
            a("<div class=\"top-risk-card level-" + clvl + "\">")
            a("<div class=\"top-risk-head\"><span class=\"top-risk-title\">" + _e(card.get("title", "")) + "</span>")
            a("<span class=\"chip chip-" + clvl + "\">" + _e(card.get("level_ru", "")) + "</span></div>")
            if card.get("phrase"):
                a("<div class=\"top-risk-phrase\">" + _e(card.get("phrase")) + "</div>")
            a("</div>")
        a("</div>")
    else:
        a("<p class=\"empty-note\">Значимых зон риска в публичной части сайта автоматически не выявлено.</p>")
    if result.executive_summary:
        a("<div class=\"summary-text\">" + _e(result.executive_summary) + "</div>")
    a("</section>")

    # 3. Блок ответственности
    liability = ctx.get("liability", {}) or {}
    a("<section class=\"section liability\">")
    a("<div class=\"liability-header\">Справочно: возможная ответственность</div>")
    a("<table class=\"liability-table\">")
    for row in liability.get("items", []) or []:
        a("<tr><td class=\"liab-label\">" + _e(row.get("label", "")))
        if row.get("basis"):
            a("<div class=\"liab-basis\">" + _e(row.get("basis")) + "</div>")
        a("</td><td class=\"liab-amount\">" + _e(row.get("amount", "")) + "</td></tr>")
    a("</table>")
    a("<div class=\"liability-disclaimer\">" + _e(liability.get("disclaimer", "")) + "</div>")
    a("</section>")

    # 4. Ядро-чеклист (одна таблица)
    core_status_ru = ctx.get("core_status_ru", {})
    core_dot = ctx.get("core_status_dot", {})
    if getattr(result, "core_checklist", None):
        a("<section class=\"section\"><h2>Основные проверки</h2>")
        a("<p class=\"section-lead\">Фиксированный набор ключевых пунктов. Статус «зона риска» означает признак возможного несоответствия и требует подтверждения юристом.</p>")
        a("<table class=\"core-table\"><tr><th>Проверка</th><th>Статус</th><th>Комментарий</th></tr>")
        for it in result.core_checklist:
            dot = _e(core_dot.get(it.status, "na"))
            a("<tr><td>" + _e(it.label) + "</td>")
            a("<td class=\"cell-status\"><span class=\"dot dot-" + dot + "\"></span>" + _e(core_status_ru.get(it.status, it.status)) + "</td>")
            a("<td class=\"cell-comment\">" + _e(it.comment) + "</td></tr>")
        a("</table>")
        if getattr(result, "agent_audit_used", False) and getattr(result, "agent_audit_notes", ""):
            a("<p class=\"muted\">Агентная перепроверка (LLM-обход сайта): " + _e(result.agent_audit_notes) + "</p>")
        a("</section>")

    # 5. Документы (краткие карточки, без полного чек-листа)
    a("<section class=\"section\"><h2>Документы</h2>")
    unique_docs = ctx.get("unique_documents", [])
    if unique_docs:
        a("<div class=\"doc-cards\">")
        for doc in unique_docs:
            a("<div class=\"doc-card\"><div class=\"doc-card-head\">")
            a("<span class=\"doc-card-type\">" + _e(doc.get("type_ru", "")) + "</span>")
            comp = doc.get("completeness")
            if comp is not None:
                a("<span class=\"doc-card-pct\">Полнота " + _e(comp) + "%</span>")
            a("</div>")
            if doc.get("url"):
                a("<div class=\"doc-card-url mono\">" + _e(doc.get("url")) + "</div>")
            if comp is not None:
                a("<div class=\"mini-bar\"><div class=\"mini-fill\" style=\"width:" + _e(comp) + "%\"></div></div>")
            gaps = doc.get("open_items") or []
            if gaps:
                a("<div class=\"doc-card-gaps\"><span class=\"gaps-label\">Незакрытые пункты:</span><ul>")
                for gap in gaps:
                    a("<li>" + _e(gap) + "</li>")
                a("</ul></div>")
            elif doc.get("manual_only"):
                a("<div class=\"muted\">Документ найден; требует ручной проверки юристом.</div>")
            if doc.get("placeholder"):
                a("<div class=\"doc-card-flag\">Обнаружены признаки незаполненного шаблона — требует проверки.</div>")
            a("</div>")
        a("</div>")
    else:
        a("<p class=\"empty-note\">Документы по обработке персональных данных в публичной части сайта не обнаружены. Их отсутствие в публичном доступе является признаком риска, требующим проверки.</p>")
    a("</section>")

    # 6. Cookie / трекеры + техника (компактно)
    country_ru = ctx.get("country_hint_ru", {})
    tech = result.technical
    a("<section class=\"section\"><h2>Cookie, сторонние сервисы и техника</h2>")
    if result.trackers:
        a("<table class=\"data\"><tr><th>Сервис</th><th>Категория</th><th>Происхождение</th><th>Риск</th></tr>")
        for tr in result.trackers:
            a("<tr><td>" + _e(tr.provider_name))
            if tr.matched_domain:
                a("<br><span class=\"muted mono\">" + _e(tr.matched_domain) + "</span>")
            a("</td><td>" + _e(tr.category) + "</td>")
            a("<td>" + _e(country_ru.get(tr.country_hint, tr.country_hint)) + "</td>")
            a("<td>" + _e(tr.legal_risk) + "</td></tr>")
        a("</table>")
    else:
        a("<p class=\"empty-note\">Сторонние сервисы и трекеры автоматически не обнаружены.</p>")
    a("<table class=\"data compact\">")
    a("<tr><td>Cookie-баннер</td><td>" + ("обнаружен" if result.cookie_banner_found else "не обнаружен") + "</td></tr>")
    a("<tr><td>Маркетинговые cookie до согласия</td><td>" + ("признаки выявлены (требует проверки)" if result.marketing_cookies_before_consent else "не выявлены") + "</td></tr>")
    a("<tr><td>Зарубежные сторонние сервисы</td><td>" + ("присутствуют (возможна трансграничная передача)" if result.foreign_trackers_found else "не выявлены") + "</td></tr>")
    a("<tr><td>HTTPS</td><td>" + ("используется" if tech.https_enabled else "не подтверждён") + "</td></tr>")
    a("<tr><td>Переадресация HTTP → HTTPS</td><td>" + ("настроена" if tech.http_to_https_redirect else "не подтверждена") + "</td></tr>")
    a("<tr><td>Предполагаемая страна сервера</td><td>" + _e(tech.server_country) + (" (достоверность " + _e(tech.geoip_confidence) + "%)" if tech.geoip_confidence else "") + "</td></tr>")
    a("</table></section>")

    # 7. Рекомендации
    a("<section class=\"section\"><h2>Что рекомендуется исправить</h2>")
    recs = ctx.get("recommendations", {})
    rec_blocks = [
        ("urgent", "Срочно", "rec-priority-urgent"),
        ("soon", "В ближайшее время", "rec-priority-soon"),
        ("optional", "Желательно", "rec-priority-optional"),
    ]
    any_rec = any(recs.get(k) for k, _, _ in rec_blocks)
    if any_rec:
        for key, heading, cls in rec_blocks:
            items = recs.get(key) or []
            if not items:
                continue
            a("<div class=\"rec-group " + cls + "\"><h3>" + heading + "</h3><ul>")
            for risk in items:
                line = "<strong>" + _e(risk.title) + ".</strong>"
                if risk.recommendation:
                    line += " " + _e(risk.recommendation)
                a("<li>" + line + "</li>")
            a("</ul></div>")
    else:
        a("<p class=\"empty-note\">Приоритетных рекомендаций по итогам автоматической проверки не сформировано.</p>")
    a("</section>")

    # Как мы можем помочь (КП + пакеты)
    a("<section class=\"section\"><h2>Как мы можем помочь</h2>")
    if result.commercial_offer_text:
        a("<div class=\"offer-text\">" + _e(result.commercial_offer_text) + "</div>")
    packages = ctx.get("packages", [])
    if packages:
        for pkg in packages:
            a("<div class=\"package-card\"><div class=\"pkg-head\"><span class=\"pkg-title\">" + _e(pkg.get("title", "")) + "</span>")
            if pkg.get("price"):
                a("<span class=\"pkg-price\">" + _e(pkg.get("price")) + "</span>")
            a("</div>")
            if pkg.get("duration"):
                a("<div class=\"pkg-duration\">Срок: " + _e(pkg.get("duration")) + "</div>")
            if pkg.get("description"):
                a("<p>" + _e(pkg.get("description")) + "</p>")
            if pkg.get("items"):
                a("<ul>")
                for it in pkg.get("items"):
                    a("<li>" + _e(it) + "</li>")
                a("</ul>")
            a("</div>")
    else:
        a("<p class=\"empty-note\">Состав пакетов услуг уточняется. Свяжитесь с бюро для подготовки предложения.</p>")
    contact_lines = ctx.get("firm_contact_lines", [])
    if contact_lines:
        a("<div class=\"contact-block\">")
        for line in contact_lines:
            a("<div>" + _e(line) + "</div>")
        a("</div>")
    a("</section>")

    # Ограничения проверки (повтор ограничений)
    a("<section class=\"section limitations\"><h2>Ограничения проверки</h2>")
    a("<p class=\"section-lead\">Автоматический анализ не заменяет юридическую экспертизу. Система, в частности, не имеет доступа к следующему:</p><ul class=\"scope-list\">")
    for lim in ctx.get("scope_limitations", []):
        a("<li>" + _e(lim) + "</li>")
    a("</ul>")
    a("<div class=\"disclaimer-box\">" + _e(ctx.get("disclaimer", "")) + "</div>")
    a("</section>")

    a("<div class=\"footer-note\">" + _e(ctx.get("disclaimer_short", "")) + "</div>")
    a("</div></body></html>")
    return "".join(parts)


def _fallback_error_html(result: ScanResult, message: str) -> str:
    """Минимальный безопасный HTML на случай полного сбоя рендера."""
    company = _e(getattr(result, "company_name", "") or "")
    site = _e(getattr(result, "site_url", "") or "")
    return (
        "<!DOCTYPE html><html lang=\"ru\"><head><meta charset=\"utf-8\">"
        "<title>Отчёт о проверке сайта</title></head><body>"
        "<h1>Отчёт о проверке публичной части сайта</h1>"
        + ("<p>Организация: " + company + "</p>" if company else "")
        + "<p>Сайт: " + site + "</p>"
        + "<p>" + _e(message) + "</p>"
        + "<p>" + _e(DISCLAIMER_SHORT) + "</p>"
        "</body></html>"
    )
