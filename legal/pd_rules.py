"""Compact PD-risk findings for commercial output.

The full rule engine keeps the internal audit detailed. This module builds a
smaller sales/legal layer from already collected facts, without LLM and without
reading full document text. It is intentionally conservative: findings are
phrased as risk indicators and require lawyer confirmation.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from scanner.models import (
    CoreCheckItem,
    Evidence,
    Risk,
    RiskLevel,
    ScanResult,
)


LEVEL_ORDER = {
    RiskLevel.unknown.value: -1,
    RiskLevel.low.value: 0,
    RiskLevel.medium.value: 1,
    RiskLevel.high.value: 2,
    RiskLevel.critical.value: 3,
}

PD_LEVEL_BY_SCORE = (
    (80, RiskLevel.critical.value),
    (50, RiskLevel.high.value),
    (25, RiskLevel.medium.value),
    (0, RiskLevel.low.value),
)

LIABILITY_BY_KIND = {
    "consent": "Обработка ПДн без надлежащего согласия",
    "policy": "Неопубликование/неполнота политики обработки ПДн",
    "cross_border": "Нарушение требований к трансграничной передаче ПДн",
    "localization": "Невыполнение обязанности по локализации баз ПДн граждан РФ",
    "ads": "Направление рекламы без согласия адресата",
    "rkn": "Непредставление/несвоевременное уведомление в Роскомнадзор",
    "special": "Специальные категории/биометрия: повышенная зона риска",
    "technical": "Техническая защита данных: требует проверки механики сайта",
}


class PDFinding(BaseModel):
    """One compact finding for the client-facing commercial proposal."""

    id: str
    title: str
    risk_level: str = RiskLevel.medium.value
    score: int = 0
    what_found: str = ""
    why_it_matters: str = ""
    recommendation: str = ""
    evidence_url: str = ""
    evidence_quote: str = ""
    liability_hint: str = ""
    source: str = "auto"
    source_ids: List[str] = Field(default_factory=list)


def _truncate(value: Any, limit: int = 260) -> str:
    try:
        text = " ".join(str(value or "").split())
    except Exception:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _clean_list(values: Iterable[Any], limit: int = 8) -> List[str]:
    out: List[str] = []
    seen = set()
    try:
        for value in values:
            text = _truncate(value, 120)
            key = text.lower()
            if text and key not in seen:
                out.append(text)
                seen.add(key)
            if len(out) >= limit:
                break
    except Exception:
        return out
    return out


def _core_map(result: ScanResult) -> Dict[str, CoreCheckItem]:
    try:
        return {
            str(item.id): item
            for item in (getattr(result, "core_checklist", None) or [])
            if getattr(item, "id", "")
        }
    except Exception:
        return {}


def _risk_matches(result: ScanResult, prefixes: Sequence[str]) -> List[Risk]:
    matches: List[Risk] = []
    try:
        for risk in getattr(result, "risks", None) or []:
            rid = str(getattr(risk, "id", "") or "")
            for prefix in prefixes:
                if rid == prefix or rid.startswith(prefix + "_"):
                    matches.append(risk)
                    break
    except Exception:
        return []
    return matches


def _first_risk(result: ScanResult, prefixes: Sequence[str]) -> Optional[Risk]:
    matches = _risk_matches(result, prefixes)
    if not matches:
        return None
    return sorted(matches, key=lambda r: (LEVEL_ORDER.get(r.risk_level, 0), int(r.score or 0)), reverse=True)[0]


def _evidence_from_risk(risk: Optional[Risk]) -> Tuple[str, str]:
    if risk is None:
        return "", ""
    try:
        ev = getattr(risk, "evidence", None) or Evidence()
        return _truncate(getattr(ev, "page_url", "") or getattr(risk, "page_url", ""), 220), _truncate(
            getattr(ev, "quote", "") or getattr(risk, "report_phrase", "") or getattr(risk, "title", ""),
            260,
        )
    except Exception:
        return "", ""


def _evidence_from_core(item: Optional[CoreCheckItem]) -> Tuple[str, str]:
    if item is None:
        return "", ""
    return "", _truncate(getattr(item, "evidence", "") or getattr(item, "comment", ""), 260)


def _best_level(levels: Sequence[str], fallback: str = RiskLevel.medium.value) -> str:
    best = fallback
    for level in levels:
        if LEVEL_ORDER.get(level, -1) > LEVEL_ORDER.get(best, -1):
            best = level
    return best


def _score_from_sources(risk: Optional[Risk], core: Optional[CoreCheckItem], default: int) -> int:
    try:
        if risk is not None and int(risk.score or 0) > 0:
            return int(risk.score or 0)
    except Exception:
        pass
    if core is not None:
        lvl = str(getattr(core, "risk_level", "") or "")
        if lvl == RiskLevel.critical.value:
            return max(default, 30)
        if lvl == RiskLevel.high.value:
            return max(default, 20)
        if lvl == RiskLevel.medium.value:
            return max(default, 10)
    return default


def _finding(
    *,
    finding_id: str,
    title: str,
    risk: Optional[Risk] = None,
    core: Optional[CoreCheckItem] = None,
    default_level: str = RiskLevel.medium.value,
    default_score: int = 10,
    what_found: str,
    why_it_matters: str,
    recommendation: str,
    liability_kind: str = "",
) -> PDFinding:
    r_url, r_quote = _evidence_from_risk(risk)
    c_url, c_quote = _evidence_from_core(core)
    source_ids: List[str] = []
    if risk is not None:
        source_ids.append(str(getattr(risk, "id", "") or ""))
    if core is not None:
        source_ids.append(str(getattr(core, "id", "") or ""))
    return PDFinding(
        id=finding_id,
        title=title,
        risk_level=_best_level(
            [
                str(getattr(risk, "risk_level", "") or ""),
                str(getattr(core, "risk_level", "") or ""),
            ],
            default_level,
        ),
        score=_score_from_sources(risk, core, default_score),
        what_found=_truncate(what_found or (r_quote or c_quote), 360),
        why_it_matters=_truncate(why_it_matters, 360),
        recommendation=_truncate(recommendation or getattr(risk, "recommendation", "") or "", 360),
        evidence_url=r_url or c_url,
        evidence_quote=r_quote or c_quote,
        liability_hint=LIABILITY_BY_KIND.get(liability_kind, ""),
        source_ids=_clean_list(source_ids, 6),
    )


def _core_risk(core: Dict[str, CoreCheckItem], item_id: str) -> Optional[CoreCheckItem]:
    item = core.get(item_id)
    if item is not None and getattr(item, "status", "") == "risk":
        return item
    return None


def _has_prechecked(result: ScanResult) -> bool:
    try:
        for form in getattr(result, "forms", None) or []:
            consent = getattr(form, "consent", None)
            if consent is not None and getattr(consent, "checkbox_prechecked", None) is True:
                return True
    except Exception:
        return False
    return False


def _document_conflict_types(result: ScanResult) -> List[str]:
    types: List[str] = []
    try:
        analyses = list(getattr(result, "document_checklists", None) or [])
        for doc in getattr(result, "documents", None) or []:
            if getattr(doc, "analysis", None) is not None:
                analyses.append(doc.analysis)
        for analysis in analyses:
            for conflict in getattr(analysis, "conflicts", None) or []:
                ctype = str(getattr(conflict, "type", "") or "")
                if ctype:
                    types.append(ctype)
    except Exception:
        return []
    return types


def build_pd_findings(result: ScanResult) -> List[PDFinding]:
    """Build deterministic commercial findings from ScanResult.

    No LLM, no network, no full document text. The output is stable and small,
    suitable for a client-facing teaser and for an LLM fact bundle.
    """
    if result is None:
        return []
    core = _core_map(result)
    findings: List[PDFinding] = []

    def add(finding: Optional[PDFinding]) -> None:
        if finding is None:
            return
        findings.append(finding)

    r = _first_risk(result, ["R001"])
    c = _core_risk(core, "PP_001")
    if r or c:
        add(
            _finding(
                finding_id="PD-01",
                title="Политика обработки ПДн не подтверждена как опубликованная",
                risk=r,
                core=c,
                default_level=RiskLevel.critical.value,
                default_score=25,
                what_found=(
                    "На сайте обнаружены формы с признаками сбора персональных данных, "
                    "но публичная политика обработки ПДн не подтверждена автоматически."
                ),
                why_it_matters="Политика — базовый публичный документ для раскрытия обработки данных на сайте.",
                recommendation="Опубликовать актуальную политику и связать её с формами сбора данных.",
                liability_kind="policy",
            )
        )

    r = _first_risk(result, ["R026"])
    c = _core_risk(core, "PP_032")
    if r or c:
        add(
            _finding(
                finding_id="PD-03",
                title="Есть признак несоответствия оператора/домена в документах",
                risk=r,
                core=c,
                default_level=RiskLevel.medium.value,
                default_score=15,
                what_found="В документах или контактах есть признак, что указанное юрлицо/домен нужно уточнить.",
                why_it_matters="Клиент должен понимать, кто является оператором ПДн и куда направлять обращения.",
                recommendation="Проверить реквизиты оператора, домены и контактные данные во всех документах сайта.",
                liability_kind="policy",
            )
        )

    r = _first_risk(result, ["R007"])
    c = _core_risk(core, "PP_003") or _core_risk(core, "PP_004")
    if r or c:
        add(
            _finding(
                finding_id="PD-04",
                title="Оператор или его реквизиты раскрыты неполно",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=15,
                what_found="Автоматическая проверка не нашла достаточные сведения об операторе/реквизитах.",
                why_it_matters="Неполная идентификация оператора снижает прозрачность обработки ПДн.",
                recommendation="Проверить название, ИНН/ОГРН, адрес и контакт для обращений субъектов ПДн.",
                liability_kind="policy",
            )
        )

    for fid, item_id, title, why in (
        ("PD-05", "PP_009", "Цели обработки ПДн описаны неполно", "Цели должны быть конкретными и соотноситься с формами сайта."),
        ("PD-07", "PP_011", "Категории персональных данных описаны неполно", "Перечень данных должен покрывать фактически собираемые поля форм."),
        ("PD-08", "PP_015", "Сроки обработки/хранения требуют проверки", "Пользователь должен понимать, как долго могут обрабатываться его данные."),
        ("PD-09", "PP_018", "Порядок отзыва согласия требует проверки", "Отзыв согласия должен быть понятным и практически исполнимым."),
    ):
        c = _core_risk(core, item_id)
        if c:
            add(
                _finding(
                    finding_id=fid,
                    title=title,
                    core=c,
                    default_level=RiskLevel.high.value,
                    default_score=10,
                    what_found=getattr(c, "comment", "") or title,
                    why_it_matters=why,
                    recommendation="Актуализировать политику/согласие под фактические формы и процессы сайта.",
                    liability_kind="policy",
                )
            )

    r = _first_risk(result, ["R009"])
    c = _core_risk(core, "PP_012")
    if r or c:
        add(
            _finding(
                finding_id="PD-06",
                title="Документы не полностью совпадают с фактическими формами сайта",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=20,
                what_found="Состав данных в формах и раскрытие в документах требуют сопоставления.",
                why_it_matters="Если сайт собирает телефон, email, файл или чувствительные сведения, это должно быть отражено в документах.",
                recommendation="Сверить каждое поле формы с политикой и текстом согласия.",
                liability_kind="policy",
            )
        )

    r = _first_risk(result, ["R014"])
    c = _core_risk(core, "PP_019") or _core_risk(core, "PP_024")
    if r or c:
        add(
            _finding(
                finding_id="PD-10",
                title="Сторонние сервисы раскрыты неполно",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=15,
                what_found="На сайте обнаружены сторонние сервисы/трекеры, раскрытие которых в политике требует проверки.",
                why_it_matters="Аналитика, CRM, чаты, рассылки и платёжные сервисы могут быть обработчиками или получателями данных.",
                recommendation="Добавить в документы категории/провайдеров третьих лиц и цели передачи данных.",
                liability_kind="policy",
            )
        )

    r = _first_risk(result, ["R015"])
    c = _core_risk(core, "PP_020")
    if r or c:
        conflict_types = _document_conflict_types(result)
        fid = "PD-12" if any("cross_border_denied" in x for x in conflict_types) else "PD-11"
        title = (
            "Есть возможный конфликт по трансграничной передаче"
            if fid == "PD-12"
            else "Трансграничная передача требует раскрытия/проверки"
        )
        add(
            _finding(
                finding_id=fid,
                title=title,
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=25,
                what_found="Обнаружены иностранные сервисы или признаки их использования, а раскрытие трансграничной передачи требует проверки.",
                why_it_matters="Иностранные сервисы могут создавать отдельный блок требований к документам и уведомлениям.",
                recommendation="Проверить фактическую передачу данных, провайдеров, страны и формулировки политики.",
                liability_kind="cross_border",
            )
        )

    r = _first_risk(result, ["R011"])
    c = _core_risk(core, "Cookie_001")
    if r or c:
        add(
            _finding(
                finding_id="PD-13",
                title="Cookie-баннер не подтверждён при наличии трекеров",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=15,
                what_found="Автоматическая проверка видит cookies/трекеры, но не подтверждает корректный cookie-баннер.",
                why_it_matters="Cookie-механика влияет на прозрачность обработки технических идентификаторов и маркетинговых cookies.",
                recommendation="Проверить баннер, ссылку на политику и момент запуска аналитических/маркетинговых cookies.",
                liability_kind="policy",
            )
        )

    r = _first_risk(result, ["R012"])
    c = _core_risk(core, "Cookie_006")
    if r or c:
        add(
            _finding(
                finding_id="PD-14",
                title="Аналитические/маркетинговые cookies могут запускаться до согласия",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=20,
                what_found="Найдены cookies или сетевые запросы к аналитическим/маркетинговым сервисам до активного выбора пользователя.",
                why_it_matters="Этот блок часто требует технической донастройки сайта, а не только текста политики.",
                recommendation="Настроить delayed loading для необязательных cookies и проверить CMP/cookie-баннер.",
                liability_kind="policy",
            )
        )

    r = _first_risk(result, ["R003"])
    c = _core_risk(core, "R003")
    if r or c:
        add(
            _finding(
                finding_id="PD-16",
                title="У формы с ПДн не подтверждён отдельный механизм согласия",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=20,
                what_found="На сайте есть формы с телефоном/email/именем или сообщением, но отдельное согласие рядом с формой не подтверждено.",
                why_it_matters="Согласие должно быть осознанным и отделённым от иных условий, особенно для заявок и обратной связи.",
                recommendation="Добавить отдельный чекбокс/текст согласия и кликабельные ссылки на документы рядом с формой.",
                liability_kind="consent",
            )
        )

    r = _first_risk(result, ["R005"])
    c = _core_risk(core, "Consent_011") if _has_prechecked(result) else None
    if r or c:
        add(
            _finding(
                finding_id="PD-17",
                title="Чекбокс согласия может быть проставлен заранее",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=20,
                what_found="В форме найден признак заранее выбранного чекбокса согласия.",
                why_it_matters="Предзаполненное согласие снижает качество подтверждения волеизъявления пользователя.",
                recommendation="Сделать чекбокс согласия пустым по умолчанию и отделить его от рекламных согласий.",
                liability_kind="consent",
            )
        )

    r = _first_risk(result, ["R019"])
    c = _core_risk(core, "R019")
    if r or c:
        add(
            _finding(
                finding_id="PD-19",
                title="Форма подписки требует отдельного рекламного согласия",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=20,
                what_found="На сайте есть признаки формы подписки/маркетинговой коммуникации без отдельного рекламного согласия.",
                why_it_matters="Маркетинговые рассылки обычно требуют отдельного согласия и понятного отказа от рассылки.",
                recommendation="Разделить согласие на ПДн и согласие на рекламу, указать каналы и способ отказа.",
                liability_kind="ads",
            )
        )

    r = _first_risk(result, ["R020"])
    c = _core_risk(core, "PP_027")
    if r or c:
        add(
            _finding(
                finding_id="PD-27",
                title="Специальные категории данных требуют отдельной проверки",
                risk=r,
                core=c,
                default_level=RiskLevel.critical.value,
                default_score=30,
                what_found="Найдены медицинские/чувствительные поля или отраслевой контекст, а раскрытие специальных категорий требует проверки.",
                why_it_matters="Медицинские сведения и иные специальные категории требуют повышенной осторожности в документах и механике согласия.",
                recommendation="Проверить, собираются ли специальные категории, и подготовить отдельный корректный блок согласий/политики.",
                liability_kind="special",
            )
        )

    for prefix, fid, title, what, liability in (
        (
            "R021",
            "PD-26",
            "Данные несовершеннолетних требуют отдельного раскрытия",
            "Есть образовательный/детский контекст или поля, связанные с детьми/родителями.",
            "special",
        ),
        (
            "R022",
            "PD-24",
            "Загрузка файлов/резюме требует отдельного раскрытия",
            "На сайте есть поле загрузки файла или резюме, а обработка таких данных требует проверки.",
            "consent",
        ),
        (
            "R023",
            "PD-23",
            "HR/резюме требуют отдельной цели и сроков хранения",
            "Есть признаки формы кандидата/вакансии или загрузки резюме.",
            "consent",
        ),
        (
            "R025",
            "PD-31",
            "В документах есть шаблонные заглушки",
            "Автоматически обнаружены незаполненные поля или шаблонные формулировки.",
            "policy",
        ),
        (
            "R027",
            "PD-22",
            "Нужно отдельно проверить уведомление Роскомнадзора",
            "Сайт, вероятно, обрабатывает ПДн; наличие и актуальность уведомления РКН требует ручной проверки.",
            "rkn",
        ),
    ):
        r = _first_risk(result, [prefix])
        if r:
            add(
                _finding(
                    finding_id=fid,
                    title=title,
                    risk=r,
                    default_level=str(getattr(r, "risk_level", "") or RiskLevel.medium.value),
                    default_score=int(getattr(r, "score", 0) or 10),
                    what_found=what,
                    why_it_matters="Этот блок может потребовать отдельной юридической настройки документов и механики сайта.",
                    recommendation=getattr(r, "recommendation", "") or "Проверить применимость и актуализировать документы.",
                    liability_kind=liability,
                )
            )

    r = _first_risk(result, ["R017", "R018", "R016"])
    c = _core_risk(core, "R017")
    if r or c:
        add(
            _finding(
                finding_id="PD-40",
                title="Техническая конфигурация сайта требует проверки",
                risk=r,
                core=c,
                default_level=RiskLevel.high.value,
                default_score=15,
                what_found="Найдены признаки риска по HTTPS, mixed content, хостингу или технической инфраструктуре.",
                why_it_matters="Техническая конфигурация влияет на защищённость передачи данных и доказательную базу аудита.",
                recommendation="Проверить HTTPS, mixed content, серверную инфраструктуру и фактическое место обработки данных.",
                liability_kind="technical",
            )
        )

    # Deduplicate by commercial PD id, keeping the strongest version.
    by_id: Dict[str, PDFinding] = {}
    for item in findings:
        prev = by_id.get(item.id)
        if prev is None or (
            LEVEL_ORDER.get(item.risk_level, 0),
            item.score,
        ) > (
            LEVEL_ORDER.get(prev.risk_level, 0),
            prev.score,
        ):
            by_id[item.id] = item

    return sorted(
        by_id.values(),
        key=lambda x: (LEVEL_ORDER.get(x.risk_level, 0), int(x.score or 0), x.id),
        reverse=True,
    )


def score_to_pd_level(score: int) -> str:
    try:
        value = int(score or 0)
    except Exception:
        return RiskLevel.unknown.value
    for threshold, level in PD_LEVEL_BY_SCORE:
        if value >= threshold:
            return level
    return RiskLevel.low.value


def commercial_score(findings: Sequence[PDFinding]) -> Tuple[int, str]:
    """Return capped internal score and level for the compact findings."""
    total = 0
    try:
        total = sum(max(0, int(f.score or 0)) for f in findings)
    except Exception:
        total = 0
    return min(total, 150), score_to_pd_level(total)


def build_fact_bundle(
    result: ScanResult,
    settings: Any = None,
    *,
    top_n: int = 4,
    max_trackers: int = 10,
) -> Dict[str, Any]:
    """Small, token-cheap fact bundle for LLM/client copy.

    The bundle intentionally excludes full document text, raw HTML and JSON. It
    should stay small even for large scans.
    """
    findings = build_pd_findings(result)
    score, level = commercial_score(findings)
    top = findings[: max(1, top_n)]

    docs: List[Dict[str, str]] = []
    try:
        seen = set()
        for doc in getattr(result, "documents", None) or []:
            if not (getattr(doc, "is_accessible", False) or getattr(doc, "link_confirmed", False)):
                continue
            key = (getattr(doc, "doc_type", ""), getattr(doc, "url", ""))
            if key in seen:
                continue
            seen.add(key)
            docs.append(
                {
                    "type": str(getattr(doc, "doc_type", "") or "other"),
                    "url": _truncate(getattr(doc, "url", ""), 220),
                    "status": "text_extracted" if getattr(doc, "text_length", 0) else "found_needs_review",
                }
            )
            if len(docs) >= 12:
                break
    except Exception:
        docs = []

    tracker_names = _clean_list(
        [getattr(t, "provider_name", "") or getattr(t, "matched_domain", "") for t in getattr(result, "trackers", None) or []],
        max_trackers,
    )
    foreign_trackers = _clean_list(
        [
            getattr(t, "provider_name", "") or getattr(t, "matched_domain", "")
            for t in getattr(result, "trackers", None) or []
            if getattr(t, "country_hint", "") == "foreign"
        ],
        max_trackers,
    )

    firm = {
        "name": getattr(settings, "firm_name", "") if settings is not None else "",
        "email": getattr(settings, "firm_email", "") if settings is not None else "",
        "phone": getattr(settings, "firm_phone", "") if settings is not None else "",
        "website": getattr(settings, "firm_website", "") if settings is not None else "",
    }

    return {
        "company_name": _truncate(getattr(result, "company_name", ""), 160),
        "site_url": _truncate(getattr(result, "site_url", "") or getattr(result, "final_url", ""), 220),
        "final_url": _truncate(getattr(result, "final_url", ""), 220),
        "industry": _truncate(getattr(result, "industry", ""), 80),
        "created_at": _truncate(getattr(result, "created_at", ""), 40),
        "pages_checked": int(getattr(result, "pages_checked", 0) or 0),
        "forms_found": len(getattr(result, "forms", None) or []),
        "pd_forms_found": len(
            [f for f in (getattr(result, "forms", None) or []) if getattr(f, "potentially_personal_data_form", False)]
        ),
        "documents": docs,
        "trackers": tracker_names,
        "foreign_trackers": foreign_trackers,
        "cookie_banner_found": bool(getattr(result, "cookie_banner_found", False)),
        "risk_score": score,
        "risk_level": level,
        "confidence": int(getattr(result, "confidence", 0) or 0),
        "top_findings": [f.model_dump() for f in top],
        "hidden_findings_count": max(0, len(findings) - len(top)),
        "all_finding_ids": [f.id for f in findings],
        "firm": firm,
        "limits": [
            "Проверена только публичная часть сайта.",
            "CRM, серверная логика, договоры с обработчиками и реальные журналы согласий не проверялись.",
            "Выводы являются признаками риска и требуют подтверждения юристом.",
        ],
    }
