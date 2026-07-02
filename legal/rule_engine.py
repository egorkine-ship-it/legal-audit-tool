"""
Rule engine: детерминированные предикаты правил R001..R027.

Метаданные каждого правила (текст, уровень риска, вес, флаги) хранятся в
data/legal_rules.yml и загружаются `load_rules`. Логика условия каждого правила
реализована здесь как предикат с тем же id в словаре `PREDICATES`. Предикат
получает `ScanContext` и возвращает `Evidence`, если правило сработало, иначе
None.

Тон: правило фиксирует ПРИЗНАК риска, а не установленное нарушение. Формулировки
берутся из yaml (report_phrase / client_friendly_explanation), а не задаются в
коде.

Никакая публичная функция не бросает исключение: рискованные участки обёрнуты в
try/except, при сбое предикат возвращает None (правило не срабатывает), а
`apply_rules` пропускает проблемное правило.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional

from scanner import utils
from scanner.models import (
    DocumentResult,
    Evidence,
    FormResult,
    Risk,
    RiskLevel,
    RISK_LEVEL_ORDER,
    ScanContext,
)

try:
    from scanner.evidence import make_evidence as _make_evidence
except Exception:  # pragma: no cover - fallback, если evidence.py недоступен
    _make_evidence = None


# ---------------------------------------------------------------------------
# Загрузка правил
# ---------------------------------------------------------------------------
def load_rules(path: str) -> List[dict]:
    """Загрузить список правил из YAML (ключ `rules`). Никогда не бросает."""
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        rules = data.get("rules") if isinstance(data, dict) else None
        if isinstance(rules, list):
            return [r for r in rules if isinstance(r, dict)]
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Построение Evidence (с безопасным фолбэком)
# ---------------------------------------------------------------------------
def _evidence(page_url: str = "", quote: str = "", html_snippet: str = "", **extra) -> Evidence:
    """Сформировать Evidence через scanner.evidence, с фолбэком на прямую модель."""
    if _make_evidence is not None:
        try:
            return _make_evidence(
                page_url=page_url,
                quote=quote,
                html_snippet=html_snippet,
                **extra,
            )
        except Exception:
            pass
    try:
        return Evidence(
            page_url=page_url or "",
            quote=utils.truncate(quote or "", 500),
            html_snippet=utils.truncate(html_snippet or "", 800),
            extra=dict(extra) if extra else {},
        )
    except Exception:
        return Evidence()


# ---------------------------------------------------------------------------
# Вспомогательные функции чтения контекста
# ---------------------------------------------------------------------------
# Приоритет статусов чек-листа: found > unclear > not_found > not_applicable.
_STATUS_RANK = {
    "found": 3,
    "unclear": 2,
    "not_found": 1,
    "not_applicable": 0,
    "": 0,
}


def _docs(ctx: ScanContext, doc_type: str) -> List[DocumentResult]:
    """
    Документы указанного типа, которые РЕАЛЬНО присутствуют (единый критерий
    document_finder.document_is_present — защита от catch-all-200 и галлюцинаций
    агента). Раньше учитывалось простое наличие любого DocumentResult этого типа,
    из-за чего 404-догадка «подавляла» риск отсутствия документа.
    """
    out: List[DocumentResult] = []
    try:
        from scanner import document_finder as _df

        for d in ctx.documents:
            if getattr(d, "doc_type", "") == doc_type and _df.document_is_present(d):
                out.append(d)
    except Exception:
        try:
            return [d for d in ctx.documents if getattr(d, "doc_type", "") == doc_type
                    and (getattr(d, "is_accessible", False) or getattr(d, "link_confirmed", False))]
        except Exception:
            return []
    return out


def _all_analyzed_docs(ctx: ScanContext) -> List[DocumentResult]:
    out: List[DocumentResult] = []
    try:
        for d in ctx.documents:
            if getattr(d, "analysis", None) is not None:
                out.append(d)
    except Exception:
        return []
    return out


def _item_status(ctx: ScanContext, item_id: str) -> str:
    """Лучший статус элемента чек-листа item_id среди всех документов.

    Просматривает analysis.checklist_results всех документов и возвращает
    наилучший найденный статус по приоритету found > unclear > not_found.
    Если элемент нигде не встречается — возвращает "not_found".
    """
    best = "not_found"
    best_rank = -1
    seen_any = False
    try:
        for d in ctx.documents:
            analysis = getattr(d, "analysis", None)
            if analysis is None:
                continue
            for item in getattr(analysis, "checklist_results", []) or []:
                if getattr(item, "id", "") != item_id:
                    continue
                seen_any = True
                status = getattr(item, "status", "") or "not_found"
                rank = _STATUS_RANK.get(status, 0)
                if rank > best_rank:
                    best_rank = rank
                    best = status
    except Exception:
        return "not_found"
    if not seen_any:
        return "not_found"
    return best


def _item_found(ctx: ScanContext, item_id: str) -> bool:
    return _item_status(ctx, item_id) == "found"


def _pd_forms(ctx: ScanContext) -> List[FormResult]:
    """Формы, предположительно собирающие персональные данные."""
    out: List[FormResult] = []
    try:
        for f in ctx.forms:
            if getattr(f, "potentially_personal_data_form", False):
                out.append(f)
    except Exception:
        return []
    return out


def _any_form(ctx: ScanContext, cond: Callable[[FormResult], bool]) -> Optional[FormResult]:
    """Первая форма, удовлетворяющая условию, либо None."""
    try:
        for f in ctx.forms:
            try:
                if cond(f):
                    return f
            except Exception:
                continue
    except Exception:
        return None
    return None


def _has_privacy_policy(ctx: ScanContext) -> bool:
    return bool(getattr(ctx, "has_privacy_policy", False)) or bool(_docs(ctx, "privacy_policy"))


def _readable_docs(ctx: ScanContext, doc_type: str) -> List[DocumentResult]:
    """Документы типа с реально извлечённым текстом (можно оценивать содержимое)."""
    try:
        return [
            d for d in ctx.documents
            if getattr(d, "doc_type", "") == doc_type
            and getattr(d, "is_accessible", False)
            and int(getattr(d, "text_length", 0) or 0) > 0
        ]
    except Exception:
        return []


def _tracking_trackers(ctx: ScanContext) -> List:
    """Трекеры категорий analytics/marketing."""
    out = []
    try:
        for t in ctx.trackers:
            if getattr(t, "category", "") in ("analytics", "marketing"):
                out.append(t)
    except Exception:
        return []
    return out


def _foreign_trackers(ctx: ScanContext) -> List:
    out = []
    try:
        for t in ctx.trackers:
            if getattr(t, "country_hint", "") == "foreign":
                out.append(t)
    except Exception:
        return []
    return out


def _form_page(f: FormResult) -> str:
    return getattr(f, "page_url", "") or ""


def _first_page_url(ctx: ScanContext) -> str:
    try:
        for p in ctx.pages:
            u = getattr(p, "final_url", "") or getattr(p, "url", "")
            if u:
                return u
    except Exception:
        pass
    return getattr(ctx, "final_url", "") or getattr(ctx, "site_url", "") or ""


# ---------------------------------------------------------------------------
# Предикаты правил
# ---------------------------------------------------------------------------
def _r001(ctx: ScanContext) -> Optional[Evidence]:
    """Формы сбора ПДн есть, публичная политика обработки ПДн не найдена."""
    pd = _pd_forms(ctx)
    if pd and not _has_privacy_policy(ctx):
        f = pd[0]
        return _evidence(
            page_url=_form_page(f),
            quote="Обнаружены формы, предполагающие сбор персональных данных; "
            "публично доступная политика обработки ПДн не найдена.",
            extra={"pd_forms_count": len(pd)},
        )
    return None


def _r002(ctx: ScanContext) -> Optional[Evidence]:
    """У формы с ПДн нет ссылки на политику рядом с формой."""
    pd = _pd_forms(ctx)
    if not pd:
        return None
    for f in pd:
        consent = getattr(f, "consent", None)
        if consent is None or not getattr(consent, "privacy_link_found", False):
            return _evidence(
                page_url=_form_page(f),
                quote=getattr(getattr(f, "consent", None), "evidence_text", "")
                or "Рядом с формой сбора ПДн не обнаружена ссылка на политику обработки ПДн.",
            )
    return None


def _r003(ctx: ScanContext) -> Optional[Evidence]:
    """У формы с ПДн не найден отдельный механизм согласия."""
    for f in _pd_forms(ctx):
        consent = getattr(f, "consent", None)
        checkbox = getattr(consent, "checkbox_found", False) if consent else False
        text = getattr(consent, "consent_text_found", False) if consent else False
        if not checkbox and not text:
            return _evidence(
                page_url=_form_page(f),
                quote="У формы, предполагающей сбор ПДн, не обнаружен чекбокс "
                "или отдельный текст согласия.",
                html_snippet=getattr(consent, "evidence_html_snippet", "") if consent else "",
            )
    return None


def _r004(ctx: ScanContext) -> Optional[Evidence]:
    """Согласие оформлено только фразой 'нажимая кнопку'."""
    f = _any_form(
        ctx,
        lambda fr: bool(getattr(getattr(fr, "consent", None), "button_acceptance_only", False)),
    )
    if f is not None:
        consent = getattr(f, "consent", None)
        return _evidence(
            page_url=_form_page(f),
            quote=getattr(consent, "evidence_text", "")
            or "Согласие оформлено только формулировкой об акцепте по нажатию кнопки.",
        )
    return None


def _r005(ctx: ScanContext) -> Optional[Evidence]:
    """Чекбокс согласия предустановлен (prechecked)."""
    f = _any_form(
        ctx,
        lambda fr: getattr(getattr(fr, "consent", None), "checkbox_prechecked", None) is True,
    )
    if f is not None:
        return _evidence(
            page_url=_form_page(f),
            quote="Обнаружен признак предустановленного (заранее отмеченного) "
            "чекбокса согласия.",
            html_snippet=getattr(getattr(f, "consent", None), "evidence_html_snippet", ""),
        )
    return None


def _r006(ctx: ScanContext) -> Optional[Evidence]:
    """Согласие объединено с офертой/соглашением/рассылкой."""
    f = _any_form(
        ctx,
        lambda fr: bool(getattr(getattr(fr, "consent", None), "merged_with_offer", False)),
    )
    if f is not None:
        return _evidence(
            page_url=_form_page(f),
            quote=getattr(getattr(f, "consent", None), "evidence_text", "")
            or "Обнаружен признак объединения согласия на обработку ПДн с иными документами.",
        )
    return None


def _r007(ctx: ScanContext) -> Optional[Evidence]:
    """Политика есть, но оператор не идентифицирован (PP_003/PP_005/PP_006)."""
    if not _readable_docs(ctx, "privacy_policy"):
        return None
    missing = [i for i in ("PP_003", "PP_005", "PP_006") if not _item_found(ctx, i)]
    if missing:
        docs = _docs(ctx, "privacy_policy")
        url = docs[0].url if docs else _first_page_url(ctx)
        return _evidence(
            page_url=url,
            quote="В политике обработки ПДн не обнаружены полные сведения об "
            "операторе (наименование/адрес/контакт для обращений).",
            extra={"missing_items": missing},
        )
    return None


def _r008(ctx: ScanContext) -> Optional[Evidence]:
    """Политика есть, но нет целей или перечня ПДн/категорий (PP_009/PP_010/PP_011)."""
    if not _readable_docs(ctx, "privacy_policy"):
        return None
    missing = [i for i in ("PP_009", "PP_010", "PP_011") if not _item_found(ctx, i)]
    if missing:
        docs = _docs(ctx, "privacy_policy")
        url = docs[0].url if docs else _first_page_url(ctx)
        return _evidence(
            page_url=url,
            quote="В политике обработки ПДн не обнаружены конкретные цели "
            "обработки и/или перечень персональных данных и категорий субъектов.",
            extra={"missing_items": missing},
        )
    return None


def _r009(ctx: ScanContext) -> Optional[Evidence]:
    """Политика не соответствует полям форм (PP_012 not_found/unclear)."""
    if not _readable_docs(ctx, "privacy_policy"):
        return None
    status = _item_status(ctx, "PP_012")
    if status in ("not_found", "unclear"):
        docs = _docs(ctx, "privacy_policy")
        url = docs[0].url if docs else _first_page_url(ctx)
        return _evidence(
            page_url=url,
            quote="Обнаружен признак несоответствия перечня ПДн в политике "
            "фактически собираемым полям форм сайта.",
            extra={"pp_012_status": status},
        )
    return None


def _r010(ctx: ScanContext) -> Optional[Evidence]:
    """Нет сроков обработки/хранения или порядка отзыва согласия.

    Срабатывает при наличии форм с ПДн, если ни один из элементов
    PP_015/PP_018/Consent_008/Consent_009 не найден.
    """
    if not _pd_forms(ctx):
        return None
    items = ("PP_015", "PP_018", "Consent_008", "Consent_009")
    if all(not _item_found(ctx, i) for i in items):
        return _evidence(
            page_url=_first_page_url(ctx),
            quote="В документах не обнаружены сроки обработки/хранения ПДн "
            "и/или порядок отзыва согласия.",
            extra={"checked_items": list(items)},
        )
    return None


def _r011(ctx: ScanContext) -> Optional[Evidence]:
    """Найдены аналитические/маркетинговые трекеры, но нет cookie-баннера."""
    trackers = _tracking_trackers(ctx)
    if trackers and not getattr(ctx, "cookie_banner_found", False):
        t = trackers[0]
        return _evidence(
            page_url=getattr(t, "page_url", "") or _first_page_url(ctx),
            quote="На сайте обнаружены аналитические/маркетинговые трекеры при "
            "отсутствии cookie-баннера.",
            extra={"trackers": [getattr(x, "provider_name", "") for x in trackers[:10]]},
        )
    return None


def _r012(ctx: ScanContext) -> Optional[Evidence]:
    """Аналитические/маркетинговые cookies/запросы до согласия."""
    if getattr(ctx, "marketing_cookies_before_consent", False):
        return _evidence(
            page_url=_first_page_url(ctx),
            quote=getattr(ctx, "cookies_before_consent_evidence", "")
            or "Обнаружен признак установки аналитических/маркетинговых cookies "
            "и/или запросов к трекерам до получения согласия пользователя.",
        )
    return None


def _r013(ctx: ScanContext) -> Optional[Evidence]:
    """Cookies/трекеры есть, cookie policy/раздел отсутствует (PP_022 not found)."""
    cookies_present = bool(getattr(ctx, "cookies_present", False))
    if not cookies_present:
        return None
    if bool(getattr(ctx, "has_cookie_policy", False)) or bool(_docs(ctx, "cookie_policy")):
        return None
    if _item_found(ctx, "PP_022"):
        return None
    return _evidence(
        page_url=_first_page_url(ctx),
        quote="На сайте обнаружены cookies/трекеры, при этом cookie policy "
        "или раздел о cookies не найден.",
    )


def _r014(ctx: ScanContext) -> Optional[Evidence]:
    """Трекеры есть, политика есть, но трекеры не раскрыты (PP_024 not found)."""
    if not getattr(ctx, "trackers_present", False):
        return None
    if not _has_privacy_policy(ctx):
        return None
    if _item_found(ctx, "PP_024"):
        return None
    return _evidence(
        page_url=_first_page_url(ctx),
        quote="Обнаружен признак того, что используемые на сайте трекеры/сервисы "
        "не раскрыты в политике обработки ПДн.",
        extra={"trackers": [getattr(t, "provider_name", "") for t in ctx.trackers[:10]]},
    )


def _r015(ctx: ScanContext) -> Optional[Evidence]:
    """Иностранные сервисы без раскрытия трансграничной передачи (PP_020 not found)."""
    foreign = _foreign_trackers(ctx)
    if not foreign and not getattr(ctx, "foreign_trackers_present", False):
        return None
    if _item_found(ctx, "PP_020"):
        return None
    return _evidence(
        page_url=_first_page_url(ctx),
        quote="Обнаружены иностранные сервисы при отсутствии раскрытия "
        "трансграничной передачи ПДн в политике.",
        extra={"foreign_trackers": [getattr(t, "provider_name", "") for t in foreign[:10]]},
    )


def _r016(ctx: ScanContext) -> Optional[Evidence]:
    """Признаки иностранной инфраструктуры при сборе ПДн."""
    if not _pd_forms(ctx):
        return None
    tech = getattr(ctx, "technical", None)
    if tech is None:
        return None
    country = getattr(tech, "server_country", "unknown") or "unknown"
    confidence = 0
    try:
        confidence = int(getattr(tech, "geoip_confidence", 0) or 0)
    except Exception:
        confidence = 0
    foreign = country not in ("", "unknown", "RU", "Russia", "Российская Федерация", "Россия")
    if foreign and confidence > 0:
        return _evidence(
            page_url=_first_page_url(ctx),
            quote="IP-адрес сайта отнесён к иностранной инфраструктуре при "
            "наличии форм сбора ПДн — требует проверки места хранения ПДн.",
            extra={
                "server_country": country,
                "geoip_confidence": confidence,
                "ip_addresses": list(getattr(tech, "ip_addresses", []) or [])[:5],
            },
        )
    return None


def _r017(ctx: ScanContext) -> Optional[Evidence]:
    """Отсутствует HTTPS."""
    tech = getattr(ctx, "technical", None)
    if tech is None:
        return None
    if not getattr(tech, "https_enabled", False):
        return _evidence(
            page_url=getattr(ctx, "final_url", "") or getattr(ctx, "site_url", ""),
            quote="Обнаружен признак отсутствия защищённого соединения (HTTPS).",
        )
    return None


def _r018(ctx: ScanContext) -> Optional[Evidence]:
    """Смешанный контент (mixed content)."""
    tech = getattr(ctx, "technical", None)
    if tech is None:
        return None
    if getattr(tech, "mixed_content_found", False):
        urls = list(getattr(tech, "mixed_content_urls", []) or [])
        return _evidence(
            page_url=getattr(ctx, "final_url", "") or getattr(ctx, "site_url", ""),
            quote="Обнаружен признак смешанного контента: HTTPS-страница "
            "подгружает ресурсы по HTTP.",
            extra={"mixed_content_urls": urls[:20]},
        )
    return None


def _r019(ctx: ScanContext) -> Optional[Evidence]:
    """Форма подписки без отдельного согласия на рекламу."""
    if not getattr(ctx, "newsletter_form", False):
        # Дополнительно проверим флаг на самих формах.
        if not any(getattr(f, "is_newsletter", False) for f in ctx.forms):
            return None
    has_ad_doc = bool(getattr(ctx, "has_ad_consent_doc", False)) or bool(_docs(ctx, "ad_consent"))
    if has_ad_doc:
        return None
    ad_consent_on_form = any(
        getattr(getattr(f, "consent", None), "ad_consent_found", False) for f in ctx.forms
    )
    if ad_consent_on_form:
        return None
    f = _any_form(ctx, lambda fr: getattr(fr, "is_newsletter", False))
    return _evidence(
        page_url=_form_page(f) if f is not None else _first_page_url(ctx),
        quote="Обнаружена форма подписки/рассылки без отдельного согласия на "
        "получение рекламы.",
    )


def _r020(ctx: ScanContext) -> Optional[Evidence]:
    """Медицинская форма и спец. категории ПДн (PP_027/Consent_014 not found)."""
    is_medical = bool(getattr(ctx, "medical", False)) or any(
        getattr(f, "is_medical", False) for f in ctx.forms
    )
    if not is_medical:
        return None
    if _item_found(ctx, "PP_027") and _item_found(ctx, "Consent_014"):
        return None
    f = _any_form(ctx, lambda fr: getattr(fr, "is_medical", False))
    return _evidence(
        page_url=_form_page(f) if f is not None else _first_page_url(ctx),
        quote="Обнаружены признаки сбора сведений, которые могут относиться к "
        "специальным категориям ПДн, без соответствующего раскрытия и "
        "отдельного согласия.",
    )


def _r021(ctx: ScanContext) -> Optional[Evidence]:
    """Данные несовершеннолетних без раскрытия (PP_029 not found)."""
    is_children = bool(getattr(ctx, "children", False)) or any(
        getattr(f, "is_children_related", False) for f in ctx.forms
    )
    if not is_children:
        return None
    if _item_found(ctx, "PP_029"):
        return None
    f = _any_form(ctx, lambda fr: getattr(fr, "is_children_related", False))
    return _evidence(
        page_url=_form_page(f) if f is not None else _first_page_url(ctx),
        quote="Обнаружены признаки обработки данных несовершеннолетних/их "
        "представителей без соответствующего раскрытия.",
    )


def _r022(ctx: ScanContext) -> Optional[Evidence]:
    """Загрузка файлов без раскрытия обработки (Consent_015 not found)."""
    file_upload = bool(getattr(ctx, "file_upload", False)) or any(
        getattr(f, "has_file_upload", False) for f in ctx.forms
    )
    if not file_upload:
        return None
    if _item_found(ctx, "Consent_015"):
        return None
    f = _any_form(ctx, lambda fr: getattr(fr, "has_file_upload", False))
    return _evidence(
        page_url=_form_page(f) if f is not None else _first_page_url(ctx),
        quote="Обнаружена форма с загрузкой файлов без раскрытия состава и целей "
        "обработки таких данных.",
    )


def _r023(ctx: ScanContext) -> Optional[Evidence]:
    """HR/резюме без раскрытия обработки кандидатов (Consent_008/PP_015 not found)."""
    is_hr = bool(getattr(ctx, "hr", False)) or any(getattr(f, "is_hr", False) for f in ctx.forms)
    if not is_hr:
        return None
    missing = [i for i in ("Consent_008", "PP_015") if not _item_found(ctx, i)]
    if not missing:
        return None
    f = _any_form(ctx, lambda fr: getattr(fr, "is_hr", False))
    return _evidence(
        page_url=_form_page(f) if f is not None else _first_page_url(ctx),
        quote="Обнаружена форма отклика/резюме без раскрытия целей, состава и "
        "сроков обработки данных кандидатов.",
        extra={"missing_items": missing},
    )


def _r024(ctx: ScanContext) -> Optional[Evidence]:
    """Онлайн-продажи без публичной оферты."""
    if not bool(getattr(ctx, "ecommerce", False)):
        return None
    if bool(getattr(ctx, "has_offer", False)) or bool(_docs(ctx, "offer")):
        return None
    return _evidence(
        page_url=_first_page_url(ctx),
        quote="Обнаружены признаки онлайн-продаж при отсутствии публичной оферты.",
    )


def _r025(ctx: ScanContext) -> Optional[Evidence]:
    """В каком-либо документе шаблонные незаполненные поля (плейсхолдеры)."""
    try:
        for d in ctx.documents:
            if getattr(d, "template_placeholder_detected", False):
                placeholders = utils.find_placeholders(getattr(d, "text", "") or "")
                return _evidence(
                    page_url=getattr(d, "url", "") or _first_page_url(ctx),
                    quote="В документе обнаружены признаки незаполненных шаблонных "
                    "полей (плейсхолдеров).",
                    extra={"placeholders": placeholders[:10], "doc_type": getattr(d, "doc_type", "")},
                )
    except Exception:
        return None
    return None


def _r026(ctx: ScanContext) -> Optional[Evidence]:
    """В политике указан контактный домен другой организации/группы.

    Срабатывает при детерминированном (не-LLM) конфликте по компании/домену.
    Уровень риска — MEDIUM (см. legal_rules.yml): несовпадение email-домена часто
    означает лишь другой домен той же организации/группы, поэтому вывод формулируется
    как «уточнить юрлицо», а не как критическое «политика чужой компании».
    """
    try:
        for d in _all_analyzed_docs(ctx):
            analysis = getattr(d, "analysis", None)
            if analysis is None:
                continue
            for conflict in getattr(analysis, "conflicts", []) or []:
                # Вывод по компании/домену допускаем только по ДЕТЕРМИНИРОВАННОМУ
                # конфликту (не по предложению LLM) и только для типов, явно
                # указывающих на несоответствие компании/домена.
                if (getattr(conflict, "source", "heuristic") or "heuristic").lower() == "llm":
                    continue
                ctype = (getattr(conflict, "type", "") or "").lower()
                if "company" in ctype or "domain" in ctype:
                    return _evidence(
                        page_url=getattr(d, "url", "") or _first_page_url(ctx),
                        quote=getattr(conflict, "comment", "")
                        or "В политике указан контактный домен другой "
                        "организации/группы — требуется уточнить юрлицо оператора.",
                        extra={
                            "conflict_type": getattr(conflict, "type", ""),
                            "detected_fact": getattr(conflict, "detected_fact", ""),
                            "document_statement": getattr(conflict, "document_statement", ""),
                        },
                    )
    except Exception:
        return None
    return None


def _r027(ctx: ScanContext) -> Optional[Evidence]:
    """Требуется проверка уведомления РКН (всегда advisory при наличии форм ПДн)."""
    if _pd_forms(ctx):
        return _evidence(
            page_url=_first_page_url(ctx),
            quote="Сайт, по признакам, обрабатывает персональные данные — "
            "рекомендуется проверить наличие и актуальность уведомления оператора "
            "в реестре Роскомнадзора.",
        )
    return None


# ---------------------------------------------------------------------------
# Реестр предикатов (id из legal_rules.yml -> функция)
# ---------------------------------------------------------------------------
PREDICATES: Dict[str, Callable[[ScanContext], Optional[Evidence]]] = {
    "R001_NO_PRIVACY_POLICY": _r001,
    "R002_PRIVACY_NOT_LINKED_NEAR_FORM": _r002,
    "R003_FORM_NO_SEPARATE_CONSENT": _r003,
    "R004_BUTTON_ACCEPTANCE_ONLY": _r004,
    "R005_PRECHECKED_CONSENT": _r005,
    "R006_CONSENT_MERGED_WITH_OTHER_DOCUMENTS": _r006,
    "R007_PRIVACY_INCOMPLETE_OPERATOR": _r007,
    "R008_PRIVACY_INCOMPLETE_PURPOSES_DATA_CATEGORIES": _r008,
    "R009_PRIVACY_DOES_NOT_MATCH_FORMS": _r009,
    "R010_NO_RETENTION_OR_WITHDRAWAL": _r010,
    "R011_TRACKERS_NO_COOKIE_BANNER": _r011,
    "R012_COOKIES_BEFORE_CONSENT": _r012,
    "R013_COOKIE_POLICY_MISSING": _r013,
    "R014_TRACKERS_NOT_DISCLOSED_IN_POLICY": _r014,
    "R015_FOREIGN_TRACKERS_CROSS_BORDER_NOT_DISCLOSED": _r015,
    "R016_SERVER_OUTSIDE_RUSSIA_WITH_FORMS": _r016,
    "R017_NO_HTTPS": _r017,
    "R018_MIXED_CONTENT": _r018,
    "R019_NEWSLETTER_NO_AD_CONSENT": _r019,
    "R020_MEDICAL_FORM_SPECIAL_CATEGORY_RISK": _r020,
    "R021_CHILDREN_DATA_RISK": _r021,
    "R022_FILE_UPLOAD_RISK": _r022,
    "R023_HR_RESUME_RISK": _r023,
    "R024_OFFER_MISSING_FOR_ONLINE_SALES": _r024,
    "R025_POLICY_TEMPLATE_PLACEHOLDERS": _r025,
    "R026_POLICY_OTHER_COMPANY_CONFLICT": _r026,
    "R027_RKN_NOTIFICATION_CHECK_NEEDED": _r027,
}


# ---------------------------------------------------------------------------
# Применение правил
# ---------------------------------------------------------------------------
def _risk_level_value(rule: dict) -> str:
    level = str(rule.get("risk_level", "") or "").strip().lower()
    valid = {rl.value for rl in RiskLevel}
    return level if level in valid else RiskLevel.medium.value


def _sort_key(risk: Risk):
    try:
        level = RiskLevel(risk.risk_level)
    except Exception:
        level = RiskLevel.medium
    return (RISK_LEVEL_ORDER.get(level, 0), int(getattr(risk, "score", 0) or 0))


def apply_rules(context: ScanContext, rules: List[dict]) -> List[Risk]:
    """Применить все включённые правила к контексту и вернуть отсортированные риски.

    Для каждого правила, у которого есть предикат в PREDICATES, вызывается
    предикат; если он возвращает Evidence — формируется Risk. Правила без
    предиката или отключённые (enabled: false) пропускаются. Ошибки в отдельном
    правиле не ломают весь прогон.
    """
    risks: List[Risk] = []
    if not rules:
        return risks
    for rule in rules:
        try:
            if not isinstance(rule, dict):
                continue
            if not rule.get("enabled", True):
                continue
            rule_id = rule.get("id", "")
            predicate = PREDICATES.get(rule_id)
            if predicate is None:
                continue
            evidence = predicate(context)
            if evidence is None:
                continue
            try:
                score = int(rule.get("score", 0) or 0)
            except Exception:
                score = 0
            risk = Risk(
                id=rule_id,
                title=str(rule.get("title", "") or rule_id),
                risk_level=_risk_level_value(rule),
                score=score,
                page_url=getattr(evidence, "page_url", "") or "",
                evidence=evidence,
                legal_meaning=str(rule.get("legal_basis", "") or ""),
                recommendation=str(rule.get("recommendation", "") or ""),
                client_friendly_explanation=str(rule.get("client_friendly_explanation", "") or ""),
                report_phrase=str(rule.get("report_phrase", "") or ""),
                requires_manual_review=bool(rule.get("requires_manual_review", True)),
            )
            risks.append(risk)
        except Exception:
            # Проблемное правило не должно ломать весь прогон.
            continue
    try:
        risks.sort(key=_sort_key, reverse=True)
    except Exception:
        pass
    return risks
