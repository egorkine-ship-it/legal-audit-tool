"""
Ядро-чеклист: 21 детерминированная, надёжно автоматизируемая проверка.

Модуль вычисляет фиксированный набор проверок (compute_core_checks) по УЖЕ
СОБРАННЫМ данным сканирования: ScanContext + ScanResult. Никакой сети, никаких
LLM — только детерминированная логика поверх result.documents,
result.document_checklists, result.risks и агрегированных флагов контекста.

Каждый пункт получает вердикт ok / risk / unclear / not_applicable, короткий
комментарий в осторожном юридическом тоне («признак риска», «требует
проверки» — никогда не «нарушение») и цитату-доказательство (до 200 символов),
если она есть.

Гарантии:
  * compute_core_checks НИКОГДА не бросает исключение;
  * ВСЕГДА возвращает ровно 21 пункт в фиксированном порядке (_CORE_ITEMS).
"""
from __future__ import annotations

from typing import Callable, List, Optional, Tuple

from scanner.models import (
    CoreCheckItem,
    DocumentResult,
    FormResult,
    Risk,
    ScanContext,
    ScanResult,
)

# Максимальная длина цитаты-доказательства.
_EVIDENCE_LIMIT = 200

# Приоритет статусов чек-листа: found > unclear > not_found > not_applicable.
_STATUS_RANK = {
    "found": 3,
    "unclear": 2,
    "not_found": 1,
    "not_applicable": 0,
}

# Сопоставление статуса чек-листа документа со статусом ядро-чеклиста.
_CHECKLIST_TO_CORE = {
    "found": "ok",
    "not_found": "risk",
    "unclear": "unclear",
    "not_applicable": "not_applicable",
}

_VALID_CORE_STATUSES = {"ok", "risk", "unclear", "not_applicable"}

# Комментарии по умолчанию, если у пункта чек-листа нет собственного.
_DEFAULT_COMMENTS = {
    "ok": "Соответствующая информация обнаружена в документах сайта.",
    "risk": "Соответствующая информация в документах не обнаружена — признак риска.",
    "unclear": "Однозначно подтвердить автоматически не удалось — требует проверки.",
    "not_applicable": "Пункт не применим к данному сайту в рамках автоматической проверки.",
}

_MANUAL_COMMENT = "Пункт не удалось проверить автоматически — требует ручной проверки."
_NO_POLICY_COMMENT = (
    "Доступная политика обработки ПДн не обнаружена — пункт не проверялся "
    "(отсутствие политики отражено в пункте PP_001)."
)
_NO_PD_FORMS_COMMENT = "Формы, предполагающие сбор персональных данных, не обнаружены."


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _truncate(text: Optional[str], limit: int = _EVIDENCE_LIMIT) -> str:
    """Аккуратно усечь строку до limit символов. Никогда не бросает."""
    try:
        s = str(text or "").strip()
        if len(s) <= limit:
            return s
        return s[: limit - 1].rstrip() + "…"
    except Exception:
        return ""


def _checklist_status(
    result: ScanResult, item_id: str
) -> Optional[Tuple[str, str, str]]:
    """Лучший статус пункта item_id среди всех проанализированных документов.

    Просматривает result.document_checklists и analysis у result.documents.
    Возвращает (status, evidence_quote, comment) с наилучшим статусом по
    приоритету found > unclear > not_found > not_applicable, либо None, если
    пункт нигде не встречается.
    """
    best: Optional[Tuple[int, str, str, str]] = None
    try:
        analyses = list(getattr(result, "document_checklists", None) or [])
        for d in getattr(result, "documents", None) or []:
            analysis = getattr(d, "analysis", None)
            if analysis is not None:
                analyses.append(analysis)
        for analysis in analyses:
            for item in getattr(analysis, "checklist_results", None) or []:
                if getattr(item, "id", "") != item_id:
                    continue
                status = getattr(item, "status", "") or ""
                rank = _STATUS_RANK.get(status, -1)
                if rank < 0:
                    continue
                if best is None or rank > best[0]:
                    best = (
                        rank,
                        status,
                        getattr(item, "evidence_quote", "") or "",
                        getattr(item, "comment", "") or "",
                    )
    except Exception:
        return None
    if best is None:
        return None
    return (best[1], best[2], best[3])


def _risk_fired(result: ScanResult, rule_prefix: str) -> Optional[Risk]:
    """Найти сработавший риск, id которого начинается с rule_prefix (например, 'R001')."""
    try:
        for risk in getattr(result, "risks", None) or []:
            rid = getattr(risk, "id", "") or ""
            if rid == rule_prefix or rid.startswith(rule_prefix + "_"):
                return risk
    except Exception:
        return None
    return None


def _risk_quote(risk: Optional[Risk]) -> str:
    """Цитата-доказательство из риска (усечённая), либо пустая строка."""
    if risk is None:
        return ""
    try:
        evidence = getattr(risk, "evidence", None)
        return _truncate(getattr(evidence, "quote", "") if evidence is not None else "")
    except Exception:
        return ""


def _pd_forms(ctx: ScanContext) -> List[FormResult]:
    """Формы, предположительно собирающие персональные данные."""
    out: List[FormResult] = []
    try:
        for f in getattr(ctx, "forms", None) or []:
            if getattr(f, "potentially_personal_data_form", False):
                out.append(f)
    except Exception:
        return []
    return out


def _docs_of_type(result: ScanResult, doc_type: str) -> List[DocumentResult]:
    """Документы указанного типа из результата сканирования."""
    out: List[DocumentResult] = []
    try:
        for d in getattr(result, "documents", None) or []:
            if getattr(d, "doc_type", "") == doc_type:
                out.append(d)
    except Exception:
        return []
    return out


def _accessible_policy(result: ScanResult) -> Optional[DocumentResult]:
    """Первый реально присутствующий и доступный документ типа privacy_policy.

    Важно: догадки по типовым путям (/privacy, /policy и т.п.) на catch-all
    сайтах могут открываться с 200 и давать обычный текст главной. Такие
    документы нельзя считать опубликованной политикой, поэтому используем тот же
    критерий document_is_present, что и rule engine.
    """
    try:
        from scanner import document_finder as _df
    except Exception:
        _df = None
    for d in _docs_of_type(result, "privacy_policy"):
        try:
            present = _df.document_is_present(d) if _df is not None else True
        except Exception:
            present = True
        if present and getattr(d, "is_accessible", False):
            return d
    return None


def _link_confirmed_policy(result: ScanResult) -> Optional[DocumentResult]:
    """Политика, существование которой подтверждено реальной ссылкой сайта."""
    try:
        from scanner import document_finder as _df
    except Exception:
        _df = None
    for d in _docs_of_type(result, "privacy_policy"):
        try:
            present = _df.document_is_present(d) if _df is not None else True
        except Exception:
            present = True
        if (
            present
            and getattr(d, "link_confirmed", False)
            and not getattr(d, "is_accessible", False)
        ):
            return d
    return None


def _map_checklist(cs: Tuple[str, str, str]) -> Tuple[str, str, str]:
    """Преобразовать (status, quote, comment) чек-листа в (core_status, comment, evidence)."""
    status, quote, comment = cs
    core = _CHECKLIST_TO_CORE.get(status, "unclear")
    if not comment:
        comment = _DEFAULT_COMMENTS.get(core, _MANUAL_COMMENT)
    return (core, comment, _truncate(quote))


# ---------------------------------------------------------------------------
# Проверки-строители: (ctx, result) -> (status, comment, evidence)
# ---------------------------------------------------------------------------
def _check_pp_001(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Политика обработки ПДн опубликована и доступна."""
    doc = _accessible_policy(result)
    if doc is not None:
        return (
            "ok",
            "Политика обработки персональных данных опубликована и доступна, текст извлечён.",
            _truncate(getattr(doc, "url", "")),
        )
    confirmed = _link_confirmed_policy(result)
    if confirmed is not None:
        return (
            "unclear",
            "Документ найден, но текст не извлечён — ручная проверка.",
            _truncate(getattr(confirmed, "url", "")),
        )
    if getattr(ctx, "has_pd_forms", False):
        risk = _risk_fired(result, "R001")
        return (
            "risk",
            "Обнаружены формы сбора персональных данных, при этом публичная политика "
            "обработки ПДн не найдена — признак риска.",
            _risk_quote(risk),
        )
    return (
        "not_applicable",
        "Формы сбора персональных данных и политика обработки ПДн не обнаружены.",
        "",
    )


def _check_pp_002(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Ссылка на политику рядом с формами."""
    pd = _pd_forms(ctx)
    if not pd:
        return ("not_applicable", _NO_PD_FORMS_COMMENT, "")
    with_link = []
    for f in pd:
        consent = getattr(f, "consent", None)
        if consent is not None and getattr(consent, "privacy_link_found", False):
            with_link.append(f)
    if len(with_link) == len(pd):
        evidence = ""
        try:
            evidence = getattr(getattr(with_link[0], "consent", None), "evidence_text", "") or ""
        except Exception:
            evidence = ""
        return (
            "ok",
            "Рядом с каждой формой сбора ПДн обнаружена ссылка на политику обработки ПДн.",
            _truncate(evidence),
        )
    if with_link:
        return (
            "unclear",
            "Ссылка на политику обнаружена не у всех форм сбора ПДн — требует проверки.",
            "",
        )
    risk = _risk_fired(result, "R002")
    return (
        "risk",
        "Рядом с формами сбора персональных данных ссылка на политику не обнаружена — "
        "признак риска.",
        _risk_quote(risk),
    )


def _policy_checklist_item(item_id: str) -> Callable[[ScanContext, ScanResult], Tuple[str, str, str]]:
    """Фабрика проверок «из чек-листа документа» с общим правилом применимости.

    Если доступная политика обработки ПДн не найдена — пункт не применим
    (отсутствие самой политики отражается пунктом PP_001). Если пункт нигде
    не встречается в чек-листах — вердикт unclear (ручная проверка).
    """
    def _builder(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
        if _accessible_policy(result) is None:
            return ("not_applicable", _NO_POLICY_COMMENT, "")
        cs = _checklist_status(result, item_id)
        if cs is None:
            return ("unclear", _MANUAL_COMMENT, "")
        return _map_checklist(cs)

    return _builder


def _check_pp_012(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Политика соответствует полям форм."""
    if not _pd_forms(ctx):
        return ("not_applicable", _NO_PD_FORMS_COMMENT + " Сопоставление не выполнялось.", "")
    if _accessible_policy(result) is None:
        return ("not_applicable", _NO_POLICY_COMMENT, "")
    cs = _checklist_status(result, "PP_012")
    if cs is None:
        return ("unclear", _MANUAL_COMMENT, "")
    return _map_checklist(cs)


def _check_pp_019(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Третьи лица/обработчики раскрыты (с эскалацией при наличии трекеров)."""
    if _accessible_policy(result) is None:
        return ("not_applicable", _NO_POLICY_COMMENT, "")
    cs = _checklist_status(result, "PP_019")
    if cs is None:
        return ("unclear", _MANUAL_COMMENT, "")
    status, comment, evidence = _map_checklist(cs)
    if status == "risk" and getattr(ctx, "trackers_present", False):
        comment = (
            "На сайте обнаружены сторонние сервисы, однако раскрытие третьих лиц/"
            "обработчиков в документах не найдено — признак риска."
        )
    return (status, comment, evidence)


def _check_pp_020(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Трансграничная передача раскрыта."""
    risk = _risk_fired(result, "R015")
    if risk is not None:
        return (
            "risk",
            "Обнаружены иностранные сервисы при отсутствии раскрытия трансграничной "
            "передачи ПДн — признак риска.",
            _risk_quote(risk),
        )
    foreign = bool(getattr(ctx, "foreign_trackers_present", False))
    cs = _checklist_status(result, "PP_020")
    if cs is None or cs[0] == "not_applicable":
        if not foreign:
            return (
                "not_applicable",
                "Признаков иностранных сервисов не обнаружено — раскрытие трансграничной "
                "передачи, по признакам, не требуется.",
                "",
            )
        return ("unclear", _MANUAL_COMMENT, "")
    return _map_checklist(cs)


def _check_pp_024(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Используемые сервисы/трекеры названы в политике."""
    if not getattr(ctx, "trackers_present", False):
        return ("not_applicable", "Сторонние сервисы/трекеры на сайте не обнаружены.", "")
    cs = _checklist_status(result, "PP_024")
    if cs is None:
        return ("unclear", _MANUAL_COMMENT, "")
    return _map_checklist(cs)


def _check_pp_031(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Нет шаблонных заглушек (плейсхолдеров) в документах."""
    docs = list(getattr(result, "documents", None) or [])
    flagged = None
    for d in docs:
        if getattr(d, "template_placeholder_detected", False):
            flagged = d
            break
    risk = _risk_fired(result, "R025")
    if flagged is not None or risk is not None:
        url = ""
        if flagged is not None:
            url = getattr(flagged, "url", "") or ""
        if not url and risk is not None:
            url = getattr(risk, "page_url", "") or ""
        evidence = _risk_quote(risk) or _truncate(url)
        if url and url not in evidence:
            evidence = _truncate((evidence + " " + url).strip())
        return (
            "risk",
            "В документах обнаружены признаки незаполненных шаблонных полей "
            "(плейсхолдеров) — признак риска.",
            evidence,
        )
    if not docs:
        return ("not_applicable", "Документы для проверки не обнаружены.", "")
    return ("ok", "Незаполненных шаблонных полей в документах не обнаружено.", "")


def _check_pp_032(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Политика не принадлежит другой компании."""
    risk = _risk_fired(result, "R026")
    if risk is not None:
        return (
            "risk",
            "Обнаружен признак того, что реквизиты в политике относятся к иной "
            "компании/домену, чем проверяемый сайт — признак риска.",
            _risk_quote(risk),
        )
    # Резервная проверка детерминированных конфликтов в анализах документов
    # (на случай, если правила не запускались).
    try:
        for d in getattr(result, "documents", None) or []:
            analysis = getattr(d, "analysis", None)
            if analysis is None:
                continue
            for conflict in getattr(analysis, "conflicts", None) or []:
                if (getattr(conflict, "source", "heuristic") or "heuristic").lower() == "llm":
                    continue
                ctype = (getattr(conflict, "type", "") or "").lower()
                if "company" in ctype or "domain" in ctype:
                    return (
                        "risk",
                        "Обнаружен признак расхождения между реквизитами в документе и "
                        "проверяемым сайтом — признак риска.",
                        _truncate(getattr(conflict, "detected_fact", "") or getattr(d, "url", "")),
                    )
    except Exception:
        pass
    if _accessible_policy(result) is not None:
        return (
            "ok",
            "Признаков принадлежности политики иной компании/домену не обнаружено.",
            "",
        )
    if _link_confirmed_policy(result) is not None:
        return (
            "unclear",
            "Политика найдена, но текст не извлечён — принадлежность документа "
            "требует ручной проверки.",
            _truncate(getattr(_link_confirmed_policy(result), "url", "")),
        )
    return (
        "not_applicable",
        "Политика обработки ПДн не обнаружена — проверка принадлежности не выполнялась.",
        "",
    )


def _check_consent_011(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Чекбокс согласия не проставлен заранее."""
    pd = _pd_forms(ctx)
    if not pd:
        return ("not_applicable", _NO_PD_FORMS_COMMENT, "")
    prechecked_form = None
    try:
        for f in getattr(ctx, "forms", None) or []:
            consent = getattr(f, "consent", None)
            if consent is not None and getattr(consent, "checkbox_prechecked", None) is True:
                prechecked_form = f
                break
    except Exception:
        prechecked_form = None
    risk = _risk_fired(result, "R005")
    if prechecked_form is not None or risk is not None:
        evidence = _risk_quote(risk)
        if not evidence and prechecked_form is not None:
            try:
                consent = getattr(prechecked_form, "consent", None)
                evidence = _truncate(
                    (getattr(consent, "evidence_text", "") if consent is not None else "")
                    or getattr(prechecked_form, "page_url", "")
                )
            except Exception:
                evidence = ""
        return (
            "risk",
            "Обнаружен признак предустановленного (заранее отмеченного) чекбокса "
            "согласия — признак риска.",
            evidence,
        )
    with_checkbox = []
    for f in pd:
        consent = getattr(f, "consent", None)
        if consent is not None and getattr(consent, "checkbox_found", False):
            with_checkbox.append(f)
    if not with_checkbox:
        return (
            "unclear",
            "Чекбоксы согласия у форм не обнаружены либо их состояние определить "
            "не удалось — требует проверки.",
            "",
        )
    all_known_unchecked = True
    for f in with_checkbox:
        consent = getattr(f, "consent", None)
        if getattr(consent, "checkbox_prechecked", None) is not False:
            all_known_unchecked = False
            break
    if all_known_unchecked:
        return (
            "ok",
            "Чекбоксы согласия у форм не проставлены заранее.",
            "",
        )
    return (
        "unclear",
        "Состояние части чекбоксов согласия определить не удалось — требует проверки.",
        "",
    )


def _check_r003(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Есть отдельный механизм согласия у форм."""
    pd = _pd_forms(ctx)
    if not pd:
        return ("not_applicable", _NO_PD_FORMS_COMMENT, "")
    risk = _risk_fired(result, "R003")
    if risk is not None:
        return (
            "risk",
            "У формы, предполагающей сбор ПДн, не обнаружен отдельный механизм "
            "согласия (чекбокс или текст согласия) — признак риска.",
            _risk_quote(risk),
        )
    for f in pd:
        consent = getattr(f, "consent", None)
        checkbox = getattr(consent, "checkbox_found", False) if consent is not None else False
        text = getattr(consent, "consent_text_found", False) if consent is not None else False
        if not checkbox and not text:
            return (
                "risk",
                "У части форм сбора ПДн отдельный механизм согласия не обнаружен — "
                "признак риска.",
                _truncate(getattr(f, "page_url", "")),
            )
    return (
        "ok",
        "У всех форм сбора ПДн обнаружен отдельный механизм согласия "
        "(чекбокс или текст согласия).",
        "",
    )


def _check_r017(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """HTTPS включён."""
    tech = getattr(ctx, "technical", None)
    https = bool(getattr(tech, "https_enabled", False)) if tech is not None else False
    if https:
        return ("ok", "Сайт работает по защищённому соединению (HTTPS).", "")
    risk = _risk_fired(result, "R017")
    return (
        "risk",
        "Обнаружен признак отсутствия защищённого соединения (HTTPS) — признак риска.",
        _risk_quote(risk),
    )


def _check_cookie_001(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Cookie-баннер присутствует."""
    if not getattr(ctx, "cookies_present", False):
        return ("not_applicable", "Cookies/сторонние сервисы на сайте не обнаружены.", "")
    if getattr(ctx, "cookie_banner_found", False):
        return ("ok", "Cookie-баннер обнаружен на сайте.", "")
    risk = _risk_fired(result, "R011")
    return (
        "risk",
        "На сайте используются cookies/сторонние сервисы, однако cookie-баннер "
        "не обнаружен — признак риска.",
        _risk_quote(risk),
    )


def _check_cookie_006(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Нет маркетинговых cookies до согласия."""
    if not getattr(ctx, "trackers_present", False) and not getattr(ctx, "cookies_present", False):
        return ("not_applicable", "Cookies/трекеры на сайте не обнаружены.", "")
    if getattr(ctx, "marketing_cookies_before_consent", False):
        risk = _risk_fired(result, "R012")
        evidence = _risk_quote(risk) or _truncate(
            getattr(ctx, "cookies_before_consent_evidence", "")
        )
        return (
            "risk",
            "Обнаружены признаки установки аналитических/маркетинговых cookies "
            "до получения согласия пользователя — признак риска.",
            evidence,
        )
    return (
        "ok",
        "Признаков установки маркетинговых/аналитических cookies до согласия "
        "не обнаружено.",
        "",
    )


def _check_pp_027(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """Специальные категории ПДн (медицина) раскрыты."""
    if not getattr(ctx, "medical", False):
        return (
            "not_applicable",
            "Признаков обработки специальных категорий ПДн (медицина) не обнаружено.",
            "",
        )
    risk = _risk_fired(result, "R020")
    if risk is not None:
        return (
            "risk",
            "Обнаружены признаки сбора сведений, которые могут относиться к специальным "
            "категориям ПДн, без соответствующего раскрытия — признак риска.",
            _risk_quote(risk),
        )
    cs = _checklist_status(result, "PP_027")
    if cs is None:
        return ("unclear", _MANUAL_COMMENT, "")
    return _map_checklist(cs)


def _check_r019(ctx: ScanContext, result: ScanResult) -> Tuple[str, str, str]:
    """У формы подписки есть отдельное рекламное согласие."""
    if not getattr(ctx, "newsletter_form", False):
        return ("not_applicable", "Форма подписки/рассылки на сайте не обнаружена.", "")
    risk = _risk_fired(result, "R019")
    if risk is not None:
        return (
            "risk",
            "Обнаружена форма подписки/рассылки без отдельного согласия на получение "
            "рекламы — признак риска.",
            _risk_quote(risk),
        )
    return (
        "ok",
        "Признаков отсутствия отдельного рекламного согласия у формы подписки "
        "не обнаружено.",
        "",
    )


# ---------------------------------------------------------------------------
# Реестр пунктов: (id, label, risk_level, builder) — фиксированный порядок.
# ---------------------------------------------------------------------------
_CORE_ITEMS: List[Tuple[str, str, str, Callable[[ScanContext, ScanResult], Tuple[str, str, str]]]] = [
    ("PP_001", "Политика обработки ПДн опубликована и доступна", "critical", _check_pp_001),
    ("PP_002", "Ссылка на политику рядом с формами", "high", _check_pp_002),
    ("PP_003", "Оператор идентифицирован", "high", _policy_checklist_item("PP_003")),
    ("PP_004", "ИНН/ОГРН оператора указаны", "medium", _policy_checklist_item("PP_004")),
    ("PP_009", "Цели обработки перечислены", "high", _policy_checklist_item("PP_009")),
    ("PP_011", "Перечень/категории ПДн перечислены", "high", _policy_checklist_item("PP_011")),
    ("PP_012", "Политика соответствует полям форм", "high", _check_pp_012),
    ("PP_015", "Сроки обработки/хранения указаны", "high", _policy_checklist_item("PP_015")),
    ("PP_018", "Порядок отзыва согласия описан", "high", _policy_checklist_item("PP_018")),
    ("PP_019", "Третьи лица/обработчики раскрыты", "high", _check_pp_019),
    ("PP_020", "Трансграничная передача раскрыта", "high", _check_pp_020),
    ("PP_024", "Используемые сервисы/трекеры названы в политике", "high", _check_pp_024),
    ("PP_031", "Нет шаблонных заглушек в документах", "high", _check_pp_031),
    ("PP_032", "Политика не принадлежит другой компании", "critical", _check_pp_032),
    ("Consent_011", "Чекбокс согласия не проставлен заранее", "high", _check_consent_011),
    ("R003", "Есть отдельный механизм согласия у форм", "high", _check_r003),
    ("R017", "HTTPS включён", "high", _check_r017),
    ("Cookie_001", "Cookie-баннер присутствует", "high", _check_cookie_001),
    ("Cookie_006", "Нет маркетинговых cookies до согласия", "high", _check_cookie_006),
    ("PP_027", "Спец. категории (медицина) раскрыты", "critical", _check_pp_027),
    ("R019", "У формы подписки есть отдельное рекламное согласие", "high", _check_r019),
]


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def compute_core_checks(ctx: ScanContext, result: ScanResult) -> List[CoreCheckItem]:
    """Вычислить ядро-чеклист из 21 пункта по уже собранным данным сканирования.

    Всегда возвращает ровно 21 CoreCheckItem в фиксированном порядке
    (_CORE_ITEMS). Никогда не бросает исключение: сбой отдельного пункта даёт
    вердикт unclear с комментарием о ручной проверке.
    """
    items: List[CoreCheckItem] = []
    try:
        for item_id, label, risk_level, builder in _CORE_ITEMS:
            status, comment, evidence = ("unclear", _MANUAL_COMMENT, "")
            try:
                status, comment, evidence = builder(ctx, result)
            except Exception:
                status, comment, evidence = ("unclear", _MANUAL_COMMENT, "")
            if status not in _VALID_CORE_STATUSES:
                status, comment, evidence = ("unclear", _MANUAL_COMMENT, "")
            try:
                items.append(
                    CoreCheckItem(
                        id=item_id,
                        label=label,
                        status=status,
                        risk_level=risk_level,
                        comment=str(comment or ""),
                        evidence=_truncate(evidence),
                        source="auto",
                    )
                )
            except Exception:
                # Даже при сбое конструирования пункт должен присутствовать.
                try:
                    items.append(CoreCheckItem(id=item_id, label=label))
                except Exception:
                    pass
    except Exception:
        pass
    return items
