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


def _group_documents(result: ScanResult) -> List[Dict[str, Any]]:
    """Сгруппировать документы по типу, сохраняя порядок появления типов."""
    order: List[str] = []
    groups: Dict[str, List[Any]] = {}
    for doc in result.documents:
        dt = getattr(doc, "doc_type", "other") or "other"
        if dt not in groups:
            groups[dt] = []
            order.append(dt)
        groups[dt].append(doc)
    out: List[Dict[str, Any]] = []
    for dt in order:
        out.append(
            {
                "doc_type": dt,
                "title": DOC_TYPE_RU.get(dt, dt),
                "documents": groups[dt],
            }
        )
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

    return {
        "result": result,
        "settings": settings,
        "packages": _ordered_packages(packages, settings),
        "prices": (settings.prices() if hasattr(settings, "prices") else {}),
        "RISK_LEVEL_RU": RISK_LEVEL_RU,
        "risk_level_ru": risk_level_ru,
        "risk_level_ru_map": risk_level_ru_map,
        "DOC_TYPE_RU": DOC_TYPE_RU,
        "checklist_status_ru": CHECKLIST_STATUS_RU,
        "core_status_ru": _core_status_ru(),
        "country_hint_ru": COUNTRY_HINT_RU,
        "disclaimer": DISCLAIMER_FULL,
        "disclaimer_short": DISCLAIMER_SHORT,
        "manual_review_note": MANUAL_REVIEW_NOTE,
        "scope_limitations": list(SCOPE_LIMITATIONS),
        "top_risks": top_risks,
        "generated_at": getattr(result, "created_at", "") or "",
        "documents_by_type": _group_documents(result),
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
    title = "Отчёт о проверке публичной части сайта"
    if result.company_name:
        title += " — " + result.company_name
    a("<title>" + _e(title) + "</title>")
    a("<style>" + css + "</style>")
    a("</head><body><div class=\"report\">")

    # 1. Обложка
    a("<section class=\"cover\">")
    logo = ctx.get("logo_data_uri", "")
    if logo:
        a("<img class=\"logo\" src=\"" + _e(logo) + "\" alt=\"" + _e(getattr(settings, "firm_name", "")) + "\">")
    elif getattr(settings, "firm_name", ""):
        a("<div class=\"firm-name\">" + _e(settings.firm_name) + "</div>")
    a("<div class=\"report-title\">Отчёт о проверке публичной части сайта</div>")
    a("<div class=\"report-subtitle\">Экспресс-анализ на признаки риска в области персональных данных</div>")
    a("<div class=\"cover-meta\">")
    if result.company_name:
        a("<div><span class=\"label\">Организация:</span>" + _e(result.company_name) + "</div>")
    a("<div><span class=\"label\">Сайт:</span>" + _e(result.site_url) + "</div>")
    if result.industry:
        a("<div><span class=\"label\">Отрасль:</span>" + _e(result.industry) + "</div>")
    a("<div><span class=\"label\">Дата формирования:</span>" + _e(ctx.get("generated_at", "")) + "</div>")
    a(
        "<div><span class=\"label\">Итоговый уровень риска:</span> "
        "<span class=\"badge badge-" + _e(result.risk_level) + "\">" + _e(ctx.get("risk_level_ru", "")) + "</span> "
        "<span class=\"muted\">(баллы: " + _e(result.risk_score) + "; достоверность: " + _e(result.confidence) + "%)</span></div>"
    )
    a("</div>")
    a("<div class=\"disclaimer-box\"><span class=\"disclaimer-title\">Важное уведомление об ограничениях отчёта</span>" + _e(ctx.get("disclaimer", "")) + "</div>")
    a("</section>")

    # 2. Резюме
    a("<section class=\"section\"><h2>1. Краткое резюме</h2>")
    a("<div class=\"summary-grid\">")
    a("<div class=\"summary-cell\"><span class=\"metric-label\">Уровень риска</span><span class=\"metric-value\">" + _e(ctx.get("risk_level_ru", "")) + "</span></div>")
    a("<div class=\"summary-cell\"><span class=\"metric-label\">Баллы риска</span><span class=\"metric-value\">" + _e(result.risk_score) + "</span></div>")
    a("<div class=\"summary-cell\"><span class=\"metric-label\">Достоверность</span><span class=\"metric-value\">" + _e(result.confidence) + "%</span></div>")
    a("</div>")
    if result.executive_summary:
        a("<div class=\"summary-text\">" + _e(result.executive_summary) + "</div>")
    else:
        a("<p class=\"empty-note\">Резюме не сформировано автоматически.</p>")
    a("<p class=\"muted\">Проверено страниц: " + _e(result.pages_checked) + ". Выявлено зон риска: " + _e(len(result.risks)) + ".</p>")
    a("</section>")

    # 3. Что проверено
    a("<section class=\"section\"><h2>2. Что проверено</h2><table class=\"data\">")
    a("<tr><th>Объект проверки</th><th>Результат</th></tr>")
    a("<tr><td>Публичные страницы</td><td>" + _e(result.pages_checked) + "</td></tr>")
    a("<tr><td>Формы сбора данных</td><td>" + _e(len(result.forms)) + "</td></tr>")
    a("<tr><td>Найденные документы</td><td>" + _e(len(result.documents)) + "</td></tr>")
    a("<tr><td>Сторонние сервисы и трекеры</td><td>" + _e(len(result.trackers)) + "</td></tr>")
    a("<tr><td>Cookie-баннер</td><td>" + ("обнаружен" if result.cookie_banner_found else "не обнаружен") + "</td></tr>")
    a("<tr><td>HTTPS</td><td>" + ("используется" if result.technical.https_enabled else "не подтверждён") + "</td></tr>")
    a("</table></section>")

    # 4. Признаки риска
    a("<section class=\"section\"><h2>3. Выявленные признаки риска</h2>")
    risk_map = ctx.get("risk_level_ru_map", {})
    if result.risks:
        for risk in result.risks:
            a("<div class=\"risk-card level-" + _e(risk.risk_level) + "\">")
            a("<div class=\"risk-head\"><span class=\"risk-title\">" + _e(risk.title) + "</span>")
            a("<span class=\"badge badge-" + _e(risk.risk_level) + "\">" + _e(risk_map.get(risk.risk_level, risk.risk_level)) + "</span></div>")
            explanation = risk.client_friendly_explanation or risk.report_phrase
            if explanation:
                a("<div class=\"risk-field\">" + _e(explanation) + "</div>")
            if risk.legal_meaning:
                a("<div class=\"risk-field\"><span class=\"fld-label\">Юридическое значение: </span>" + _e(risk.legal_meaning) + "</div>")
            if risk.recommendation:
                a("<div class=\"risk-field\"><span class=\"fld-label\">Рекомендация: </span>" + _e(risk.recommendation) + "</div>")
            if risk.evidence and risk.evidence.quote:
                a("<div class=\"quote\">" + _e(risk.evidence.quote) + "</div>")
            if risk.evidence and risk.evidence.page_url:
                a("<div class=\"muted mono\">Источник: " + _e(risk.evidence.page_url) + "</div>")
            a("</div>")
    else:
        a("<p class=\"empty-note\">Значимых признаков риска в публичной части сайта автоматически не выявлено.</p>")
    a("</section>")

    # 5. Формы
    a("<section class=\"section\"><h2>4. Проверка форм сбора данных</h2>")
    if result.forms:
        a("<table class=\"data\"><tr><th>Форма</th><th>Тип</th><th>Категории данных</th><th>Чекбокс согласия</th><th>Ссылка на политику</th></tr>")
        for form in result.forms:
            a("<tr><td>" + _e(form.form_type))
            if form.submit_button_text:
                a("<br><span class=\"muted\">«" + _e(form.submit_button_text) + "»</span>")
            a("</td>")
            a("<td>" + ("сбор ПДн" if form.potentially_personal_data_form else "прочее") + "</td>")
            a("<td>" + (_e(", ".join(form.personal_data_fields)) if form.personal_data_fields else "—") + "</td>")
            if form.consent.checkbox_found:
                cell = "есть" + (" (предотмечен)" if form.consent.checkbox_prechecked else "")
            elif form.consent.button_acceptance_only:
                cell = "согласие «нажатием кнопки»"
            else:
                cell = "не обнаружен"
            a("<td>" + cell + "</td>")
            a("<td>" + ("есть" if form.consent.privacy_link_found else "не обнаружена") + "</td></tr>")
        a("</table>")
        a("<p class=\"muted\">Формы не отправлялись. Оценка механики согласия требует ручной проверки.</p>")
    else:
        a("<p class=\"empty-note\">Формы сбора данных в публичной части сайта не обнаружены.</p>")
    a("</section>")

    # 6. Документы
    a("<section class=\"section\"><h2>5. Проверка документов</h2>")
    groups = ctx.get("documents_by_type", [])
    status_ru = ctx.get("checklist_status_ru", {})
    if groups:
        for group in groups:
            a("<h3>" + _e(group.get("title", "")) + "</h3>")
            for doc in group.get("documents", []):
                a("<div class=\"doc-block\"><div class=\"doc-head\"><span>")
                a(_e(doc.title or group.get("title", "")) + " <span class=\"muted\">(" + _e(doc.format) + ")</span></span>")
                if doc.analysis:
                    a("<span class=\"badge badge-status\">Полнота: " + _e(doc.analysis.overall_completeness) + "%</span>")
                a("</div>")
                if doc.url:
                    a("<div class=\"muted mono\">" + _e(doc.url) + "</div>")
                if doc.analysis:
                    a("<div class=\"completeness-bar\"><div class=\"completeness-fill\" style=\"width:" + _e(doc.analysis.overall_completeness) + "%\"></div></div>")
                    if doc.analysis.summary:
                        a("<p>" + _e(doc.analysis.summary) + "</p>")
                    if doc.analysis.checklist_results:
                        a("<table class=\"data\"><tr><th>Пункт</th><th>Статус</th><th>Комментарий / цитата</th></tr>")
                        for item in doc.analysis.checklist_results:
                            a("<tr><td>" + _e(item.label) + "</td>")
                            a("<td class=\"status-" + _e(item.status) + "\">" + _e(status_ru.get(item.status, item.status)) + "</td><td>")
                            if item.comment:
                                a(_e(item.comment))
                            if item.evidence_quote:
                                a("<div class=\"quote\">" + _e(item.evidence_quote) + "</div>")
                            a("</td></tr>")
                        a("</table>")
                    if doc.analysis.conflicts:
                        a("<h4>Возможные расхождения</h4><ul>")
                        for cf in doc.analysis.conflicts:
                            a("<li>" + _e(cf.comment or cf.type) + "</li>")
                        a("</ul>")
                else:
                    a("<p class=\"muted\">Документ найден; автоматический разбор не выполнен. Требует ручной проверки.</p>")
                if doc.template_placeholder_detected:
                    a("<p class=\"status-not_found\">Обнаружены признаки незаполненного шаблона (плейсхолдеры). Требует проверки.</p>")
                a("</div>")
    else:
        a("<p class=\"empty-note\">Документы по обработке персональных данных в публичной части сайта не обнаружены.</p>")
    a("</section>")

    # 7. Cookie и сторонние сервисы
    a("<section class=\"section\"><h2>6. Cookie и сторонние сервисы</h2>")
    country_ru = ctx.get("country_hint_ru", {})
    if result.trackers:
        a("<table class=\"data\"><tr><th>Сервис</th><th>Категория</th><th>Происхождение</th><th>Уровень риска</th></tr>")
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
    a("<ul>")
    a("<li>Cookie-баннер: " + ("обнаружен" if result.cookie_banner_found else "не обнаружен") + ".</li>")
    a("<li>Признаки установки маркетинговых cookie до согласия: " + ("выявлены (требует проверки)" if result.marketing_cookies_before_consent else "автоматически не выявлены") + ".</li>")
    a("<li>Зарубежные сторонние сервисы: " + ("присутствуют (требует проверки)" if result.foreign_trackers_found else "автоматически не выявлены") + ".</li>")
    a("</ul></section>")

    # 8. Техническая проверка
    tech = result.technical
    a("<section class=\"section\"><h2>7. Техническая проверка</h2><table class=\"data\">")
    a("<tr><td>HTTPS</td><td>" + ("используется" if tech.https_enabled else "не подтверждён") + "</td></tr>")
    a("<tr><td>Переадресация HTTP → HTTPS</td><td>" + ("настроена" if tech.http_to_https_redirect else "не подтверждена") + "</td></tr>")
    a("<tr><td>Смешанный контент</td><td>" + ("обнаружен (" + _e(len(tech.mixed_content_urls)) + ")" if tech.mixed_content_found else "не обнаружен") + "</td></tr>")
    a("<tr><td>Предполагаемая страна сервера</td><td>" + _e(tech.server_country) + (" (достоверность " + _e(tech.geoip_confidence) + "%)" if tech.geoip_confidence else "") + "</td></tr>")
    a("<tr><td>CDN</td><td>" + (_e(tech.cdn_detected) if tech.cdn_detected else "не определён") + "</td></tr>")
    a("<tr><td>robots.txt</td><td>" + ("найден" if tech.robots_txt_found else "не найден") + "</td></tr>")
    a("<tr><td>sitemap</td><td>" + ("найден" if tech.sitemap_found else "не найден") + "</td></tr>")
    a("</table></section>")

    # 9. Рекомендации
    a("<section class=\"section\"><h2>8. Что рекомендуется исправить</h2>")
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

    # 10. Коммерческое предложение
    a("<section class=\"section\"><h2>9. Коммерческое предложение</h2>")
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
        a("<p class=\"empty-note\">Состав пакетов услуг уточняется.</p>")
    contact_lines = ctx.get("firm_contact_lines", [])
    if contact_lines:
        a("<div class=\"footer-note\">")
        for line in contact_lines:
            a("<div>" + _e(line) + "</div>")
        a("</div>")
    a("</section>")

    # 11. Ограничения
    a("<section class=\"section\"><h2>10. Ограничения проверки</h2>")
    a("<p class=\"section-lead\">Автоматический анализ не заменяет юридическую экспертизу. Система, в частности, не имеет доступа к следующему:</p><ul class=\"scope-list\">")
    for lim in ctx.get("scope_limitations", []):
        a("<li>" + _e(lim) + "</li>")
    a("</ul>")
    if ctx.get("manual_review_note"):
        a("<div class=\"disclaimer-box\" style=\"margin-top:14px;\">" + _e(ctx.get("manual_review_note")) + "</div>")
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
