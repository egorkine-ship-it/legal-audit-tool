"""
Анализ юридического документа сайта.

Содержит детерминированные детекторы (используются тестами) и главную функцию
`analyze_document`, которая прогоняет документ по чек-листу (эвристика), при
доступности LLM аккуратно объединяет результаты и формирует DocumentAnalysis.

Тон — нейтральный: «признак риска» / «требует проверки». Модуль никогда не
бросает исключение наружу.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from scanner import utils
from scanner.models import (
    ChecklistItemResult,
    ChecklistStatus,
    Conflict,
    DocumentAnalysis,
    DocumentResult,
    ScanContext,
)

from legal import checklist_engine


# ---------------------------------------------------------------------------
# Детерминированные детекторы (импортируются тестами)
# ---------------------------------------------------------------------------
# Организационно-правовые формы и последующее наименование в кавычках.
_RE_OPERATOR_ENTITY = re.compile(
    r"\b(ООО|АО|ПАО|ОАО|ЗАО|НАО|ИП)\b[\s\"'«»]*([^\"'«»\n]{1,80})?",
    re.IGNORECASE,
)
_RE_OPERATOR_PHRASE = re.compile(
    r"оператор(?:ом)?[^.\n]{0,60}?(?:является|:)\s*([^.\n]{2,120})",
    re.IGNORECASE,
)

# Маркеры целей обработки.
_PURPOSE_MARKERS = [
    "обработки заявок",
    "обратной связи",
    "заключени",
    "исполнени договор",
    "оказани услуг",
    "доставк",
    "рассылк",
    "аналитик",
    "статистик",
    "информировани",
    "регистраци",
    "рассмотрени резюме",
    "подбор персонала",
    "продвижени",
    "консультаци",
    "записи на прием",
    "записи на приём",
    "бухгалтерск",
    "маркетинг",
]

# Маркеры сроков обработки/хранения.
_RETENTION_MARKERS = [
    "срок хранени",
    "срок действия согласия",
    "до достижения цел",
    "до отзыва",
    "хранятся в течение",
    "срок обработки",
    "в течение",
    "лет с момента",
    "срок договора",
]


def find_operator(text: str) -> Optional[str]:
    """Вернуть наименование оператора (ООО/АО/ИП + название) или None."""
    if not text:
        return None
    try:
        # 1) Явная фраза «оператором является …».
        m = _RE_OPERATOR_PHRASE.search(text)
        if m:
            candidate = utils.clean_text(m.group(1))
            candidate = candidate.strip(" .;,:")
            if candidate:
                return utils.truncate(candidate, 120)
        # 2) Организационно-правовая форма + наименование в кавычках.
        for em in _RE_OPERATOR_ENTITY.finditer(text):
            form = em.group(1).upper()
            # Ищем кавычки после формы для наименования.
            tail = text[em.start():em.start() + 140]
            qm = re.search(r"[\"'«]([^\"'«»]{1,80})[\"'»]", tail)
            if qm:
                name = utils.clean_text(qm.group(1))
                if name:
                    return utils.truncate(f"{form} «{name}»", 120)
            # Для ИП допускаем ФИО без кавычек.
            if form == "ИП":
                fio = re.search(
                    r"ИП\s+([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)",
                    tail,
                )
                if fio:
                    return utils.truncate("ИП " + utils.clean_text(fio.group(1)), 120)
        return None
    except Exception:
        return None


def find_inn(text: str) -> List[str]:
    """Делегирует scanner.utils.find_inn."""
    try:
        return utils.find_inn(text or "")
    except Exception:
        return []


def find_ogrn(text: str) -> List[str]:
    """Делегирует scanner.utils.find_ogrn."""
    try:
        return utils.find_ogrn(text or "")
    except Exception:
        return []


def find_purposes(text: str) -> List[str]:
    """Вернуть список найденных маркеров целей обработки ПДн."""
    if not text:
        return []
    try:
        return utils.find_all_matches(text, _PURPOSE_MARKERS)
    except Exception:
        return []


def find_retention(text: str) -> bool:
    """True, если в тексте есть указание на сроки обработки/хранения ПДн."""
    if not text:
        return False
    try:
        return utils.contains_any(text, _RETENTION_MARKERS)
    except Exception:
        return False


def find_placeholders(text: str) -> List[str]:
    """Делегирует scanner.utils.find_placeholders."""
    try:
        return utils.find_placeholders(text or "")
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Обнаружение конфликтов документа с фактами сканирования
# ---------------------------------------------------------------------------
def detect_conflicts(document: DocumentResult, ctx: ScanContext) -> List[Conflict]:
    """Найти расхождения между текстом документа и фактами сканирования."""
    conflicts: List[Conflict] = []
    try:
        text = document.text or ""
    except Exception:
        text = ""
    if not text:
        return conflicts

    # 1) Трекеры найдены, но не раскрыты в тексте документа (для профильных типов).
    try:
        doc_type = getattr(document, "doc_type", "other")
        relevant_for_trackers = doc_type in (
            "privacy_policy", "cookie_policy", "consent", "user_agreement", "other"
        )
        if relevant_for_trackers and ctx.trackers_present:
            provider_names = utils.dedupe_keep_order(
                [getattr(t, "provider_name", "") for t in ctx.trackers if getattr(t, "provider_name", "")]
            )
            mentions_tracking = utils.contains_any(
                text,
                ["cookie", "куки", "метрик", "аналитик", "статистик", "ip-адрес", "идентификатор"],
            )
            any_provider_named = any(utils.contains_any(text, [p]) for p in provider_names)
            if provider_names and not any_provider_named and not mentions_tracking:
                conflicts.append(
                    Conflict(
                        type="tracker_not_disclosed",
                        detected_fact="Обнаружены сторонние сервисы/трекеры: "
                        + ", ".join(provider_names[:5]),
                        document_statement="В документе не найдено раскрытие cookies/метрик/провайдеров.",
                        risk="high",
                        comment="Признак риска: используемые сторонние сервисы не раскрыты в документе — требует проверки.",
                    )
                )
    except Exception:
        pass

    # 2) Заявлено, что трансграничная передача не осуществляется, но есть иностранные сервисы.
    try:
        if ctx.foreign_trackers_present:
            denies_cross_border = bool(
                re.search(
                    r"трансгранич[а-яё]*[^.\n]{0,80}?не\s+осуществля",
                    utils.normalize_for_search(text),
                )
            ) or (
                utils.contains_any(text, ["трансгранич"])
                and utils.contains_any(text, ["не осуществляется", "не осуществляем", "не производится"])
            )
            if denies_cross_border:
                conflicts.append(
                    Conflict(
                        type="cross_border_denied",
                        detected_fact="Обнаружены сторонние сервисы с иностранной инфраструктурой.",
                        document_statement="В документе указано, что трансграничная передача не осуществляется.",
                        risk="high",
                        comment="Признак риска: заявление о невыполнении трансграничной передачи расходится с обнаруженными иностранными сервисами — требует проверки.",
                    )
                )
    except Exception:
        pass

    # 3) Cookies/трекеры есть, но cookies в документе не раскрыты.
    try:
        if ctx.cookies_present and not utils.contains_any(text, ["cookie", "куки", "файлы cookie"]):
            conflicts.append(
                Conflict(
                    type="cookies_not_disclosed",
                    detected_fact="На сайте обнаружены cookies/сторонние сервисы.",
                    document_statement="В документе не найдено упоминание cookies.",
                    risk="medium",
                    comment="Признак риска: использование cookies не раскрыто в документе — требует проверки.",
                )
            )
    except Exception:
        pass

    # 4) Конфликт по домену/компании (консервативно: по email-домену в тексте).
    try:
        reg_domain = utils.get_registered_domain(ctx.registered_domain or ctx.final_url or "")
        if reg_domain:
            for em in utils.find_emails(text):
                host = em.split("@", 1)[-1]
                em_domain = utils.get_registered_domain(host)
                if not em_domain or em_domain in _PUBLIC_MAIL_DOMAINS:
                    continue
                if em_domain != reg_domain:
                    conflicts.append(
                        Conflict(
                            type="company_conflict",
                            detected_fact="Домен сайта: " + reg_domain,
                            document_statement="Контактный домен в документе: " + em_domain,
                            # MEDIUM, а не HIGH: несовпадение email-домена часто
                            # означает лишь другой домен той же организации/группы
                            # (напр. askona.ru vs askonalife.com — одна компания),
                            # поэтому это не критический вывод, а повод уточнить юрлицо.
                            risk="medium",
                            comment="В политике указан контактный домен другой организации/группы — уточнить юрлицо.",
                        )
                    )
                    break
    except Exception:
        pass

    return conflicts


_PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yandex.ru", "ya.ru", "mail.ru", "bk.ru",
    "inbox.ru", "list.ru", "rambler.ru", "outlook.com", "hotmail.com",
    "icloud.com", "yahoo.com", "proton.me", "protonmail.com",
}

_HIGH_RISKS = {"high", "critical"}

# Максимум символов документа, передаваемых в LLM. Первых 12000 символов
# достаточно, чтобы найти пункты чек-листа; юридические документы на 50КБ+ лишь
# впустую жгут токены. ВАЖНО: усечение касается ТОЛЬКО текста для LLM —
# детерминированная эвристика всегда работает по ПОЛНОМУ document.text.
_LLM_DOC_CHARS = 12000


# ---------------------------------------------------------------------------
# Выбор чек-листа под тип документа
# ---------------------------------------------------------------------------
def _items_for_doc_type(doc_type: str, checklists: Dict[str, Any]) -> List[dict]:
    """Подобрать список пунктов чек-листа под тип документа с фолбэками."""
    if not isinstance(checklists, dict):
        return []
    direct = checklists.get(doc_type)
    if isinstance(direct, list) and direct:
        return direct
    fallback_map = {
        "user_agreement": "offer",
        "special_consent": "consent",
        "distribution_consent": "consent",
        "ad_consent": "ad_consent",
    }
    fb = fallback_map.get(doc_type)
    if fb:
        items = checklists.get(fb)
        if isinstance(items, list):
            return items
    return []


# ---------------------------------------------------------------------------
# Объединение эвристики и LLM
# ---------------------------------------------------------------------------
_STATUS_INFORMATIVENESS = {
    ChecklistStatus.found.value: 3,
    ChecklistStatus.not_found.value: 2,
    ChecklistStatus.unclear.value: 1,
    ChecklistStatus.not_applicable.value: 0,
}


def _quote_in_document(quote: str, doc_text: str) -> bool:
    """
    True, если цитата LLM реально присутствует в тексте документа (с точностью до
    регистра/ё/пробелов). Защита от галлюцинаций: LLM не должна «процитировать»
    то, чего в документе нет.
    """
    if not quote or not doc_text:
        return False
    q = utils.normalize_for_search(quote)
    if len(q) < 8:
        # Слишком короткая «цитата» не является доказательством.
        return False
    return q in utils.normalize_for_search(doc_text)


def _merge_llm(
    heuristic: List[ChecklistItemResult],
    llm_payload: Optional[dict],
    doc_text: str = "",
) -> List[ChecklistItemResult]:
    """Аккуратно объединить эвристику с ответом LLM по id пунктов."""
    if not llm_payload or not isinstance(llm_payload, dict):
        return heuristic
    llm_items = llm_payload.get("checklist_results")
    if not isinstance(llm_items, list):
        return heuristic

    llm_by_id: Dict[str, dict] = {}
    for it in llm_items:
        if isinstance(it, dict) and it.get("id"):
            llm_by_id[str(it.get("id"))] = it

    merged: List[ChecklistItemResult] = []
    for base in heuristic:
        llm_it = llm_by_id.get(base.id)
        if not llm_it:
            merged.append(base)
            continue
        # LLM не может переопределить not_applicable.
        if base.status == ChecklistStatus.not_applicable.value:
            merged.append(base)
            continue

        llm_status = str(llm_it.get("status", "") or "").strip()
        llm_quote = utils.truncate(str(llm_it.get("evidence_quote", "") or ""), 500)
        llm_comment = str(llm_it.get("comment", "") or "")
        llm_location = str(llm_it.get("evidence_location", "") or "")
        try:
            llm_conf = int(llm_it.get("confidence", 0) or 0)
        except Exception:
            llm_conf = 0

        valid_statuses = {s.value for s in ChecklistStatus}
        if llm_status not in valid_statuses:
            merged.append(base)
            continue

        # ENFORCE: LLM 'found'/'unclear' без цитаты не принимаем — оставляем эвристику.
        if llm_status in (ChecklistStatus.found.value, ChecklistStatus.unclear.value) and not llm_quote:
            merged.append(base)
            continue

        # ANTI-HALLUCINATION: цитата обязана реально присутствовать в документе.
        # Если LLM «процитировала» отсутствующий в тексте фрагмент — отклоняем
        # результат LLM для этого пункта и оставляем эвристику.
        if (
            llm_status in (ChecklistStatus.found.value, ChecklistStatus.unclear.value)
            and llm_quote
            and doc_text
            and not _quote_in_document(llm_quote, doc_text)
        ):
            merged.append(base)
            continue

        base_info = _STATUS_INFORMATIVENESS.get(base.status, 0)
        llm_info = _STATUS_INFORMATIVENESS.get(llm_status, 0)

        # Предпочитаем более информативный результат; при равенстве — эвристику,
        # если только LLM не даёт цитату там, где её не было.
        take_llm = False
        if llm_status != base.status:
            if llm_info > base_info:
                take_llm = True
            elif llm_info == base_info and llm_quote and not base.evidence_quote:
                take_llm = True
        else:
            # Тот же статус: обогащаем цитатой, если её не было.
            if llm_quote and not base.evidence_quote:
                take_llm = True

        if take_llm:
            merged.append(
                ChecklistItemResult(
                    id=base.id,
                    label=base.label,
                    status=llm_status,
                    evidence_quote=llm_quote or base.evidence_quote,
                    evidence_location=llm_location or base.evidence_location,
                    comment=llm_comment or base.comment,
                    risk_if_missing=base.risk_if_missing,
                    confidence=max(base.confidence, llm_conf) if llm_conf else base.confidence,
                    source="merged",
                )
            )
        else:
            merged.append(base)
    return merged


def _build_facts(document: DocumentResult, ctx: ScanContext) -> Dict[str, Any]:
    tracker_names = utils.dedupe_keep_order(
        [getattr(t, "provider_name", "") for t in ctx.trackers if getattr(t, "provider_name", "")]
    )
    return {
        "personal_data_categories": list(ctx.personal_data_categories or []),
        "trackers": tracker_names,
        "foreign_trackers": bool(ctx.foreign_trackers_present),
        "cookie_banner_found": bool(ctx.cookie_banner_found),
        "company_name": ctx.company_name or "",
        "domain": ctx.registered_domain or "",
        "industry": ctx.industry or "",
    }


# ---------------------------------------------------------------------------
# Итоговая сводка
# ---------------------------------------------------------------------------
def _build_summary(
    doc_type: str,
    completeness: int,
    results: List[ChecklistItemResult],
    conflicts: List[Conflict],
    document: DocumentResult,
) -> str:
    try:
        from scanner.models import DOC_TYPE_RU

        type_ru = DOC_TYPE_RU.get(doc_type, "Документ")
    except Exception:
        type_ru = "Документ"

    applicable = [r for r in results if r.status != ChecklistStatus.not_applicable.value]
    missing_high = [
        r for r in applicable
        if r.status != ChecklistStatus.found.value and str(r.risk_if_missing) in _HIGH_RISKS
    ]

    parts: List[str] = []
    parts.append(
        f"{type_ru}: полнота раскрытия по автоматической проверке — {completeness}%."
    )
    if getattr(document, "text_extraction_failed", False):
        parts.append(
            "Текст документа не удалось извлечь автоматически — требуется ручная проверка."
        )
    if missing_high:
        labels = "; ".join(r.label for r in missing_high[:3])
        parts.append(
            "Не удалось подтвердить существенные пункты (зона риска): " + labels + "."
        )
    else:
        parts.append("Существенных незакрытых пунктов автоматически не выявлено.")
    if conflicts:
        parts.append(
            f"Обнаружены признаки расхождений с данными сайта ({len(conflicts)}) — требуют проверки юристом."
        )
    parts.append(
        "Выводы носят предварительный характер и требуют подтверждения юристом."
    )
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Главная функция
# ---------------------------------------------------------------------------
def analyze_document(
    document: DocumentResult,
    doc_type: str,
    ctx: ScanContext,
    settings: Any,
    checklists: Dict[str, Any],
    llm_client: Any = None,
) -> DocumentAnalysis:
    """Проанализировать документ по чек-листу (эвристика + опциональный LLM)."""
    doc_type = doc_type or getattr(document, "doc_type", "other") or "other"
    doc_url = ""
    doc_text = ""
    try:
        doc_url = document.url or ""
        doc_text = document.text or ""
    except Exception:
        doc_url = ""
        doc_text = ""

    # 1) Подбор пунктов и эвристический прогон.
    items = _items_for_doc_type(doc_type, checklists)
    try:
        results = checklist_engine.run_heuristic_checklist(items, document, ctx)
    except Exception:
        results = []

    # 2) Конфликты.
    try:
        conflicts = detect_conflicts(document, ctx)
    except Exception:
        conflicts = []

    # 3) Опциональный LLM: аккуратно объединяем.
    llm_used = False
    if llm_client is not None and getattr(llm_client, "enabled", False) and doc_text:
        try:
            facts = _build_facts(document, ctx)
            # В LLM передаём УСЕЧЁННЫЙ текст (экономия токенов); эвристика уже
            # отработала по полному document.text выше.
            llm_doc_text = doc_text[:_LLM_DOC_CHARS]
            llm_payload = llm_client.analyze_document(llm_doc_text, doc_type, items, facts)
            if llm_payload:
                results = _merge_llm(results, llm_payload, doc_text)
                llm_used = True
                # LLM может предложить конфликты — присоединяем ОСТОРОЖНО:
                #  * помечаем source="llm" (чтобы они не управляли критичными
                #    правилами вроде R026 — см. rule_engine);
                #  * валидируем поле risk;
                #  * конфликты по компании/домену принимаем от LLM только если
                #    их подтвердила детерминированная проверка (анти-галлюцинация).
                extra = llm_payload.get("conflicts") if isinstance(llm_payload, dict) else None
                if isinstance(extra, list):
                    det_types = {(getattr(c, "type", "") or "").lower() for c in conflicts}
                    det_company = any(("company" in t or "domain" in t) for t in det_types)
                    valid_risks = {"low", "medium", "high", "critical"}
                    for c in extra:
                        if not (isinstance(c, dict) and c.get("type")):
                            continue
                        ctype = str(c.get("type"))
                        low_ctype = ctype.lower()
                        if ("company" in low_ctype or "domain" in low_ctype) and not det_company:
                            # Не даём LLM в одиночку заявить чужую компанию/домен.
                            continue
                        risk = str(c.get("risk", "medium") or "medium").lower()
                        if risk not in valid_risks:
                            risk = "medium"
                        try:
                            conflicts.append(
                                Conflict(
                                    type=ctype,
                                    detected_fact=str(c.get("detected_fact", "") or ""),
                                    document_statement=str(c.get("document_statement", "") or ""),
                                    risk=risk,
                                    comment=str(c.get("comment", "") or ""),
                                    source="llm",
                                )
                            )
                        except Exception:
                            pass
        except Exception:
            llm_used = False

    # 4) Полнота.
    applicable = [r for r in results if r.status != ChecklistStatus.not_applicable.value]
    found_applicable = [r for r in applicable if r.status == ChecklistStatus.found.value]
    overall_completeness = int(round(100 * len(found_applicable) / max(1, len(applicable))))

    # 5) Требует ручной проверки?
    requires_manual_review = False
    try:
        for r in applicable:
            if r.status != ChecklistStatus.found.value and str(r.risk_if_missing) in _HIGH_RISKS:
                requires_manual_review = True
                break
    except Exception:
        requires_manual_review = True
    if getattr(document, "text_extraction_failed", False):
        requires_manual_review = True
    if not llm_used:
        requires_manual_review = True

    # 6) Сводка (нейтральный тон).
    try:
        summary = _build_summary(doc_type, overall_completeness, results, conflicts, document)
    except Exception:
        summary = "Документ проанализирован автоматически; выводы требуют подтверждения юристом."

    try:
        from legal import legal_basis as _lb  # опциональная нормализация тона

        if hasattr(_lb, "sanitize_tone"):
            summary = _lb.sanitize_tone(summary)
    except Exception:
        pass

    return DocumentAnalysis(
        document_type=doc_type,
        document_url=doc_url,
        overall_completeness=overall_completeness,
        checklist_results=results,
        conflicts=conflicts,
        summary=summary,
        requires_manual_review=requires_manual_review,
        llm_used=llm_used,
    )
