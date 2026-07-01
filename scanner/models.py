"""
Pydantic-модели данных для системы экспресс-аудита публичной части сайта.

Это ЕДИНЫЙ КОНТРАКТ данных для всего проекта. Все модули (scanner/*, legal/*,
llm/*, reports/*, database/*) обязаны использовать именно эти модели, чтобы
интерфейсы между модулями оставались согласованными.

Совместимость: код рассчитан на Python 3.9+, поэтому в аннотациях используется
Optional[...] вместо синтаксиса `X | None` (union-оператор `|` в аннотациях
недоступен на 3.9 при вычислении Pydantic).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Перечисления
# ---------------------------------------------------------------------------
class RiskLevel(str, Enum):
    """Уровень риска. `unknown` используется, если сайт не удалось проверить."""

    unknown = "unknown"
    low = "low"        # низкий
    medium = "medium"  # средний
    high = "high"      # высокий
    critical = "critical"  # критический


RISK_LEVEL_RU = {
    RiskLevel.unknown: "не определён",
    RiskLevel.low: "низкий",
    RiskLevel.medium: "средний",
    RiskLevel.high: "высокий",
    RiskLevel.critical: "критический",
}

# Числовой вес уровня для сравнения/сортировки.
RISK_LEVEL_ORDER = {
    RiskLevel.unknown: -1,
    RiskLevel.low: 0,
    RiskLevel.medium: 1,
    RiskLevel.high: 2,
    RiskLevel.critical: 3,
}


class ChecklistStatus(str, Enum):
    found = "found"
    not_found = "not_found"
    unclear = "unclear"
    not_applicable = "not_applicable"


class ScanStatus(str, Enum):
    new = "new"
    scanned = "scanned"
    needs_review = "needs_review"
    approved = "approved"
    sent = "sent"
    interested = "interested"
    rejected = "rejected"
    do_not_contact = "do_not_contact"


SCAN_STATUS_RU = {
    ScanStatus.new: "новый",
    ScanStatus.scanned: "проверен",
    ScanStatus.needs_review: "нужна проверка",
    ScanStatus.approved: "одобрен",
    ScanStatus.sent: "отправлен",
    ScanStatus.interested: "интерес",
    ScanStatus.rejected: "отказ",
    ScanStatus.do_not_contact: "не контактировать",
}


# Категории полей форм.
FIELD_CATEGORIES = [
    "name",
    "phone",
    "email",
    "address",
    "birthdate",
    "message",
    "file",
    "passport",
    "inn",
    "company",
    "medical",
    "children",
    "consent",  # чекбокс согласия
    "other",
    "unknown",
]

# Категории полей, которые считаются признаком сбора персональных данных.
PERSONAL_DATA_FIELD_CATEGORIES = {
    "name",
    "phone",
    "email",
    "address",
    "birthdate",
    "passport",
    "file",
    "message",
    "medical",
    "children",
    "inn",
}

# Типы документов.
DOC_TYPES = [
    "privacy_policy",       # политика обработки/конфиденциальности ПДн
    "consent",              # согласие на обработку ПДн
    "ad_consent",           # согласие на рекламную рассылку
    "cookie_policy",        # политика cookie
    "offer",                # публичная оферта
    "user_agreement",       # пользовательское соглашение
    "special_consent",      # согласие на спец. категории ПДн
    "distribution_consent", # согласие на распространение ПДн
    "other",
]

DOC_TYPE_RU = {
    "privacy_policy": "Политика обработки персональных данных",
    "consent": "Согласие на обработку персональных данных",
    "ad_consent": "Согласие на рекламную рассылку",
    "cookie_policy": "Политика cookie",
    "offer": "Публичная оферта",
    "user_agreement": "Пользовательское соглашение",
    "special_consent": "Согласие на обработку специальных категорий ПДн",
    "distribution_consent": "Согласие на распространение ПДн",
    "other": "Иной документ",
}

INDUSTRIES = [
    "auto",
    "медицина",
    "образование",
    "IT/SaaS",
    "e-commerce",
    "услуги",
    "финансы",
    "HR/рекрутинг",
    "недвижимость",
    "туризм",
    "другое",
]


# ---------------------------------------------------------------------------
# Доказательства
# ---------------------------------------------------------------------------
class Evidence(BaseModel):
    """Доказательная база под вывод: URL, цитата, HTML-фрагмент, скриншот."""

    page_url: str = ""
    quote: str = ""
    html_snippet: str = ""
    screenshot_path: str = ""
    extra: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Формы
# ---------------------------------------------------------------------------
class FormField(BaseModel):
    name: str = ""
    field_type: str = ""      # input type или tag (textarea/select)
    label: str = ""
    placeholder: str = ""
    category: str = "unknown"  # см. FIELD_CATEGORIES
    required: bool = False
    raw_html: str = ""


class ConsentInfo(BaseModel):
    """Результат проверки механики согласия рядом с формой."""

    checkbox_found: bool = False
    # True / False / None(unknown)
    checkbox_prechecked: Optional[bool] = None
    consent_text_found: bool = False
    privacy_link_found: bool = False
    consent_link_found: bool = False
    ad_consent_found: bool = False
    cookie_consent_found: bool = False
    button_acceptance_only: bool = False
    merged_with_offer: bool = False
    privacy_link_urls: List[str] = Field(default_factory=list)
    evidence_text: str = ""
    evidence_html_snippet: str = ""
    confidence: int = 0


class FormResult(BaseModel):
    form_id: str
    page_url: str
    source: str = "html"  # html / form / modal / heuristic
    fields: List[FormField] = Field(default_factory=list)
    personal_data_fields: List[str] = Field(default_factory=list)
    submit_button_text: str = ""
    form_type: str = "other"  # generic / newsletter / callback / order / medical / hr / children
    is_newsletter: bool = False
    is_callback: bool = False
    is_order: bool = False
    is_medical: bool = False
    is_hr: bool = False
    is_children_related: bool = False
    has_file_upload: bool = False
    potentially_personal_data_form: bool = False
    consent: ConsentInfo = Field(default_factory=ConsentInfo)
    evidence: Evidence = Field(default_factory=Evidence)
    confidence: int = 0


# ---------------------------------------------------------------------------
# Трекеры / cookie
# ---------------------------------------------------------------------------
class TrackerHit(BaseModel):
    provider_name: str
    category: str = "other"        # analytics/marketing/chat/crm/calltracking/payment/delivery/cdn/other
    country_hint: str = "unknown"  # RU / foreign / unknown
    legal_risk: str = "low"        # low / medium / high
    should_be_disclosed_in_policy: bool = True
    should_trigger_cookie_check: bool = False
    should_trigger_cross_border_check: bool = False
    matched_domain: str = ""
    matched_on: str = ""           # script / iframe / network / html
    page_url: str = ""


class CookieInfo(BaseModel):
    name: str = ""
    domain: str = ""
    value_preview: str = ""
    category: str = "unknown"  # necessary/functional/analytics/marketing/unknown
    provider: str = ""
    is_tracking: bool = False
    page_url: str = ""


# ---------------------------------------------------------------------------
# Сырые данные страницы (результат работы browser.py)
# ---------------------------------------------------------------------------
class RawPage(BaseModel):
    url: str
    final_url: str = ""
    title: str = ""
    status_code: int = 0
    ok: bool = False
    html: str = ""
    text_content: str = ""
    visible_text: str = ""
    links: List[str] = Field(default_factory=list)
    script_srcs: List[str] = Field(default_factory=list)
    iframe_srcs: List[str] = Field(default_factory=list)
    external_resources: List[str] = Field(default_factory=list)
    network_requests: List[str] = Field(default_factory=list)
    cookies: List[Dict[str, Any]] = Field(default_factory=list)  # cookies до взаимодействия
    modal_htmls: List[str] = Field(default_factory=list)         # HTML модалок после клика
    content_type: str = ""
    screenshot_path: str = ""
    fetch_method: str = "http"  # playwright / http
    errors: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Результат анализа страницы (результат работы page_analyzer.py)
# ---------------------------------------------------------------------------
class PageResult(BaseModel):
    url: str
    final_url: str = ""
    title: str = ""
    status_code: int = 0
    html_length: int = 0
    text_length: int = 0
    links: List[str] = Field(default_factory=list)
    external_domains: List[str] = Field(default_factory=list)
    network_requests: List[str] = Field(default_factory=list)
    cookies_before_consent: List[CookieInfo] = Field(default_factory=list)
    forms: List[FormResult] = Field(default_factory=list)
    trackers: List[TrackerHit] = Field(default_factory=list)
    cookie_banner_found: bool = False
    cookie_banner_evidence: str = ""
    screenshot_path: str = ""
    fetch_method: str = "http"
    errors: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Документы
# ---------------------------------------------------------------------------
class DocCandidate(BaseModel):
    url: str
    doc_type: str = "other"   # предварительная классификация
    anchor_text: str = ""
    title: str = ""
    source: str = ""          # anchor / url / priority_path / sitemap / footer / header
    page_url: str = ""        # страница, где найдена ссылка


class ChecklistItemResult(BaseModel):
    id: str
    label: str
    status: str = ChecklistStatus.not_found.value  # см. ChecklistStatus
    evidence_quote: str = ""
    evidence_location: str = ""
    comment: str = ""
    risk_if_missing: str = "medium"  # low / medium / high / critical
    confidence: int = 0
    source: str = "heuristic"  # heuristic / llm / merged


class Conflict(BaseModel):
    type: str
    detected_fact: str = ""
    document_statement: str = ""
    risk: str = "medium"
    comment: str = ""
    # "heuristic" — выявлено детерминированной проверкой (можно доверять для
    # правил); "llm" — предложено LLM (показываем как справочное, но НЕ даём
    # управлять критичными правилами вроде R026 без корроборации).
    source: str = "heuristic"


class DocumentAnalysis(BaseModel):
    document_type: str = "other"
    document_url: str = ""
    overall_completeness: int = 0  # 0..100
    checklist_results: List[ChecklistItemResult] = Field(default_factory=list)
    conflicts: List[Conflict] = Field(default_factory=list)
    summary: str = ""
    requires_manual_review: bool = False
    llm_used: bool = False


class DocumentResult(BaseModel):
    doc_id: str
    doc_type: str = "other"
    url: str = ""
    title: str = ""
    format: str = "html"  # html / pdf / docx
    status_code: int = 0
    is_accessible: bool = False
    # Документ подтверждён как СУЩЕСТВУЮЩИЙ (ссылка ведёт на 2xx того же домена),
    # даже если текст извлечь не удалось. «Существует» и «текст извлечён» —
    # разные вещи: политика-SPA без текста должна считаться найденной, но
    # требующей ручной проверки, а не «политика отсутствует».
    link_confirmed: bool = False
    # Как документ был найден: anchor / page_link / guess / sitemap / agent.
    discovered_by: str = ""
    text: str = ""
    text_length: int = 0
    date_detected: str = ""
    version_detected: str = ""
    template_placeholder_detected: bool = False
    text_extraction_failed: bool = False
    extraction_error: str = ""
    requires_manual_review: bool = False
    analysis: Optional[DocumentAnalysis] = None
    evidence: Evidence = Field(default_factory=Evidence)


# ---------------------------------------------------------------------------
# Риски
# ---------------------------------------------------------------------------
class Risk(BaseModel):
    id: str
    title: str
    risk_level: str = RiskLevel.medium.value
    score: int = 0
    page_url: str = ""
    evidence: Evidence = Field(default_factory=Evidence)
    legal_meaning: str = ""            # legal_basis / юридическое значение
    recommendation: str = ""
    client_friendly_explanation: str = ""
    report_phrase: str = ""
    requires_manual_review: bool = True


# ---------------------------------------------------------------------------
# Техническая проверка
# ---------------------------------------------------------------------------
class TechnicalResult(BaseModel):
    https_enabled: bool = False
    http_to_https_redirect: bool = False
    mixed_content_found: bool = False
    mixed_content_urls: List[str] = Field(default_factory=list)
    ip_addresses: List[str] = Field(default_factory=list)
    server_country: str = "unknown"
    geoip_confidence: int = 0
    cdn_detected: str = ""
    robots_txt_found: bool = False
    sitemap_found: bool = False
    errors: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Вход и итоговый результат
# ---------------------------------------------------------------------------
class ScanInput(BaseModel):
    company_name: str = ""
    site_url: str
    industry: str = "auto"
    email: str = ""
    comment: str = ""
    max_pages: int = 20
    use_llm: bool = True
    # Агентная перепроверка: LLM-агент реально ходит по сайту (через браузер)
    # и проверяет, что мы ничего не пропустили. Работает только при use_llm.
    use_agent: bool = True
    create_pdf: bool = True


# ---------------------------------------------------------------------------
# Ядро-чеклист: ~20 надёжно автоматизируемых проверок
# ---------------------------------------------------------------------------
CORE_CHECK_STATUS_RU = {
    "ok": "выполнено",
    "risk": "зона риска",
    "unclear": "требует проверки",
    "not_applicable": "не применимо",
}


class CoreCheckItem(BaseModel):
    """Один пункт ядро-чеклиста (детерминированный вердикт + доказательство)."""

    id: str
    label: str
    status: str = "unclear"  # ok | risk | unclear | not_applicable
    risk_level: str = RiskLevel.medium.value
    comment: str = ""
    evidence: str = ""       # цитата/факт, на котором основан вердикт
    source: str = "auto"     # auto | llm-verified


class ScanResult(BaseModel):
    scan_id: str = ""
    created_at: str = ""
    company_name: str = ""
    site_url: str = ""
    final_url: str = ""
    industry: str = "auto"
    email: str = ""
    comment: str = ""

    pages_checked: int = 0
    fetch_method: str = ""  # playwright / http — как рендерилась главная
    homepage_links: int = 0  # сколько ссылок найдено на главной (диагностика)
    core_checklist: List[CoreCheckItem] = Field(default_factory=list)
    agent_audit_notes: str = ""   # заметки LLM-агента после обхода сайта
    agent_audit_used: bool = False
    pages: List[PageResult] = Field(default_factory=list)
    forms: List[FormResult] = Field(default_factory=list)
    documents: List[DocumentResult] = Field(default_factory=list)
    document_checklists: List[DocumentAnalysis] = Field(default_factory=list)
    cookies: List[CookieInfo] = Field(default_factory=list)
    trackers: List[TrackerHit] = Field(default_factory=list)
    technical: TechnicalResult = Field(default_factory=TechnicalResult)
    risks: List[Risk] = Field(default_factory=list)

    risk_score: int = 0
    risk_level: str = RiskLevel.unknown.value
    confidence: int = 0

    cookie_banner_found: bool = False
    marketing_cookies_before_consent: bool = False
    foreign_trackers_found: bool = False

    executive_summary: str = ""
    commercial_offer_text: str = ""
    email_text: str = ""
    pdf_path: str = ""

    llm_used: bool = False
    status: str = ScanStatus.scanned.value
    errors: List[str] = Field(default_factory=list)
    requires_manual_review: bool = True

    def top_risks(self, n: int = 3) -> List["Risk"]:
        """Наиболее значимые риски: по уровню, затем по score."""
        order = RISK_LEVEL_ORDER

        def _rank(r: "Risk") -> tuple:
            try:
                lvl = RiskLevel(r.risk_level)
            except Exception:
                lvl = RiskLevel.unknown
            return (order.get(lvl, 0), r.score)

        return sorted(self.risks, key=_rank, reverse=True)[:n]


class ScanContext(BaseModel):
    """
    Агрегированный контекст сканирования. Собирается оркестратором и передаётся
    в checklist_engine (для applies_when и спец-сопоставлений) и в rule_engine
    (для предикатов правил).
    """

    company_name: str = ""
    site_url: str = ""
    final_url: str = ""
    registered_domain: str = ""
    industry: str = "auto"

    forms: List[FormResult] = Field(default_factory=list)
    documents: List[DocumentResult] = Field(default_factory=list)
    trackers: List[TrackerHit] = Field(default_factory=list)
    cookies: List[CookieInfo] = Field(default_factory=list)
    technical: TechnicalResult = Field(default_factory=TechnicalResult)
    pages: List[PageResult] = Field(default_factory=list)

    # Агрегированные флаги (значения applies_when в чек-листах).
    has_forms: bool = False
    has_pd_forms: bool = False
    personal_data_categories: List[str] = Field(default_factory=list)
    newsletter_form: bool = False
    file_upload: bool = False
    medical: bool = False
    children: bool = False
    hr: bool = False
    ecommerce: bool = False
    finance: bool = False
    publishes_people: bool = False

    trackers_present: bool = False
    foreign_trackers_present: bool = False
    cookies_present: bool = False
    cookie_banner_found: bool = False
    marketing_cookies_before_consent: bool = False
    cookies_before_consent_evidence: str = ""

    # Наличие ключевых документов.
    has_privacy_policy: bool = False
    has_consent_doc: bool = False
    has_ad_consent_doc: bool = False
    has_cookie_policy: bool = False
    has_offer: bool = False
    has_user_agreement: bool = False

    def documents_by_type(self, doc_type: str) -> List["DocumentResult"]:
        return [d for d in self.documents if d.doc_type == doc_type]

    def all_document_text(self) -> str:
        return "\n\n".join(d.text for d in self.documents if d.text)

    def privacy_documents(self) -> List["DocumentResult"]:
        """Документы, которые могут раскрывать обработку ПДн (для сопоставления)."""
        wanted = {"privacy_policy", "cookie_policy", "consent", "user_agreement"}
        return [d for d in self.documents if d.doc_type in wanted and d.text]
