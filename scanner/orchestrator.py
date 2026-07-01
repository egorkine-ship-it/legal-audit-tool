"""
Оркестратор полного цикла проверки одного сайта.

Это ГЛАВНЫЙ КОНТРАКТ ВЫЗОВОВ: сигнатуры функций, которые вызываются здесь,
обязаны совпадать с реализацией в соответствующих модулях. Каждый шаг обёрнут в
try/except — падение одного шага не должно ломать всю проверку (см. критерий
«ошибки не ломают приложение»).

Публичный API:
    run_scan(scan_input: ScanInput, settings: Settings, progress_cb=None) -> ScanResult
"""
from __future__ import annotations

import traceback
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Optional

from config.settings import Settings
from scanner import utils
from scanner.models import (
    CookieInfo,
    DocumentAnalysis,
    DocumentResult,
    PageResult,
    RawPage,
    Risk,
    RiskLevel,
    ScanContext,
    ScanInput,
    ScanResult,
    ScanStatus,
    TechnicalResult,
    TrackerHit,
    PERSONAL_DATA_FIELD_CATEGORIES,
)

# Модули сканера / анализа. Импортируются лениво внутри run_scan там, где
# возможны тяжёлые/опциональные зависимости, чтобы отсутствие Playwright и т.п.
# не мешало импорту оркестратора.


ProgressCb = Optional[Callable[[str], None]]


def _emit(cb: ProgressCb, message: str) -> None:
    if cb:
        try:
            cb(message)
        except Exception:
            pass


def run_scan(
    scan_input: ScanInput,
    settings: Settings,
    progress_cb: ProgressCb = None,
) -> ScanResult:
    """Выполнить полную проверку одного сайта и вернуть ScanResult."""
    from scanner import (
        browser,
        cookie_detector,
        crawler,
        document_finder,
        document_fetcher,
        page_analyzer,
        robots_parser,
        sitemap_parser,
        technical_checker,
        tracker_detector,
    )
    from legal import checklist_engine, document_analyzer, rule_engine, risk_scoring
    from llm import llm_client

    result = ScanResult(
        scan_id=uuid.uuid4().hex[:12],
        created_at=datetime.now().isoformat(timespec="seconds"),
        company_name=scan_input.company_name,
        site_url=scan_input.site_url,
        industry=scan_input.industry,
        email=scan_input.email,
        comment=scan_input.comment,
        status=ScanStatus.scanned.value,
    )

    # --- Загрузка конфигов правил/трекеров/чек-листов ---
    try:
        tracker_index = tracker_detector.load_tracker_index(str(settings.trackers_path()))
    except Exception as exc:
        tracker_index = {"providers": [], "tracking_cookie_names": []}
        result.errors.append(f"tracker_index: {exc}")

    try:
        checklists = checklist_engine.load_checklists(str(settings.checklists_path()))
    except Exception as exc:
        checklists = {}
        result.errors.append(f"checklists: {exc}")

    try:
        rules = rule_engine.load_rules(str(settings.rules_path()))
    except Exception as exc:
        rules = []
        result.errors.append(f"rules: {exc}")

    # --- 1. Нормализация URL + стартовая страница ---
    _emit(progress_cb, "Нормализация URL и загрузка главной страницы…")
    fetcher = browser.create_fetcher(settings)
    raw_pages: List[RawPage] = []
    try:
        base_url, raw_home = crawler.resolve_start_url(scan_input.site_url, fetcher, settings)
        result.final_url = raw_home.final_url or base_url
        # Диагностика: как рендерилась главная и сколько ссылок нашли.
        result.fetch_method = getattr(raw_home, "fetch_method", "") or getattr(fetcher, "method", "")
        try:
            result.homepage_links = len(raw_home.links or [])
        except Exception:
            result.homepage_links = 0
        if raw_home.errors:
            result.errors.extend(raw_home.errors)
        if not raw_home.ok and not raw_home.html:
            # Сайт недоступен: не падаем, помечаем на ручную проверку.
            result.risk_level = RiskLevel.unknown.value
            result.requires_manual_review = True
            result.errors.append("Сайт недоступен: не удалось загрузить главную страницу.")
            _finalize_texts_and_pdf(result, ScanContext(), settings, llm_client, progress_cb,
                                    scan_input=scan_input, create_pdf=scan_input.create_pdf)
            fetcher.close()
            return result

        # --- robots / sitemap ---
        try:
            robots = robots_parser.fetch_robots(base_url, settings)
        except Exception as exc:
            robots = {"found": False, "sitemaps": [], "disallow": [], "text": ""}
            result.errors.append(f"robots: {exc}")

        sitemap_urls: List[str] = []
        try:
            sitemaps = list(robots.get("sitemaps") or [])
            if not sitemaps:
                sitemaps = [utils.absolute_url(base_url, "/sitemap.xml")]
            for sm in sitemaps[:3]:
                sitemap_urls.extend(sitemap_parser.fetch_sitemap_urls(sm, settings, limit=200))
        except Exception as exc:
            result.errors.append(f"sitemap: {exc}")

        # --- 2. Поиск страниц ---
        _emit(progress_cb, "Определение списка публичных страниц…")
        try:
            urls = crawler.discover_urls(base_url, raw_home, robots, sitemap_urls, settings)
        except Exception as exc:
            urls = [base_url]
            result.errors.append(f"discover_urls: {exc}")

        # --- 3. Сканирование страниц ---
        raw_by_url: Dict[str, RawPage] = {}
        page_results: List[PageResult] = []
        raw_home.url = raw_home.url or base_url
        # Главная уже загружена — используем её.
        seen = set()
        ordered = []
        if base_url not in seen:
            ordered.append((base_url, raw_home))
            seen.add(base_url)
        for u in urls:
            if u not in seen:
                ordered.append((u, None))
                seen.add(u)
        ordered = ordered[: max(1, settings.max_pages)]

        import time

        for idx, (u, pre) in enumerate(ordered, start=1):
            _emit(progress_cb, f"Страница {idx}/{len(ordered)}: {u}")
            try:
                raw = pre if pre is not None else fetcher.fetch(
                    u, take_screenshot=settings.enable_screenshots
                )
            except Exception as exc:
                raw = RawPage(url=u, errors=[f"fetch: {exc}"])
            raw_pages.append(raw)
            raw_by_url[utils.strip_fragment(raw.url or u)] = raw
            if raw.final_url:
                raw_by_url.setdefault(utils.strip_fragment(raw.final_url), raw)
            try:
                page = page_analyzer.analyze_page(raw, base_url, settings, tracker_index)
            except Exception as exc:
                page = PageResult(url=u, errors=[f"analyze: {exc}"])
            page_results.append(page)
            if idx < len(ordered) and settings.delay_between_pages_s > 0:
                time.sleep(min(settings.delay_between_pages_s, 10))

        result.pages = page_results
        result.pages_checked = len(page_results)

        # --- Агрегация форм / трекеров / cookies ---
        all_forms = [f for p in page_results for f in p.forms]
        all_trackers = _dedupe_trackers([t for p in page_results for t in p.trackers])
        all_cookies = _dedupe_cookies([c for p in page_results for c in p.cookies_before_consent])
        result.forms = all_forms
        result.trackers = all_trackers
        result.cookies = all_cookies
        result.cookie_banner_found = any(p.cookie_banner_found for p in page_results)

        # cookies до согласия
        try:
            cbc = cookie_detector.cookies_before_consent(
                [c.model_dump() for c in all_cookies],
                [r for raw in raw_pages for r in raw.network_requests],
                tracker_index,
            )
        except Exception as exc:
            cbc = {"has_marketing_before_consent": False, "items": [], "evidence": ""}
            result.errors.append(f"cookies_before_consent: {exc}")
        result.marketing_cookies_before_consent = bool(cbc.get("has_marketing_before_consent"))
        result.foreign_trackers_found = any(t.country_hint == "foreign" for t in all_trackers)

        # --- 4-6. Документы ---
        _emit(progress_cb, "Поиск и разбор документов сайта…")
        try:
            candidates = document_finder.find_document_candidates(
                page_results, raw_pages, base_url, robots, sitemap_urls, settings
            )
        except Exception as exc:
            candidates = []
            result.errors.append(f"document_finder: {exc}")

        documents: List[DocumentResult] = []
        for cand in candidates:
            try:
                doc = document_fetcher.fetch_document(cand, raw_by_url, settings)
            except Exception as exc:
                doc = DocumentResult(
                    doc_id=uuid.uuid4().hex[:10],
                    doc_type=cand.doc_type,
                    url=cand.url,
                    extraction_error=str(exc),
                    requires_manual_review=True,
                )
            documents.append(doc)
        documents = _dedupe_documents(documents)
        result.documents = documents

        # --- Технический блок ---
        _emit(progress_cb, "Техническая проверка (HTTPS, IP, mixed content)…")
        try:
            technical = technical_checker.check_technical(
                base_url, result.final_url, raw_pages, robots, sitemap_urls, settings
            )
        except Exception as exc:
            technical = TechnicalResult(errors=[f"technical: {exc}"])
            result.errors.append(f"technical: {exc}")
        result.technical = technical

        # --- Построение контекста ---
        context = _build_context(scan_input, base_url, result, technical)

        # --- 7. Анализ документов по чек-листам (эвристика + LLM) ---
        _emit(progress_cb, "Анализ документов по чек-листам…")
        client = llm_client.get_client(settings) if (settings.enable_llm and scan_input.use_llm) else None
        analyses: List[DocumentAnalysis] = []
        for doc in documents:
            try:
                analysis = document_analyzer.analyze_document(
                    doc, doc.doc_type, context, settings, checklists, client
                )
            except Exception as exc:
                analysis = DocumentAnalysis(
                    document_type=doc.doc_type,
                    document_url=doc.url,
                    summary=f"Ошибка анализа: {exc}",
                    requires_manual_review=True,
                )
                result.errors.append(f"analyze_document {doc.doc_type}: {exc}")
            doc.analysis = analysis
            analyses.append(analysis)
            if analysis.llm_used:
                result.llm_used = True
        result.document_checklists = analyses

        # Обновляем флаги наличия документов в контексте после разбора.
        context = _build_context(scan_input, base_url, result, technical)

        # --- 9. Rule engine ---
        _emit(progress_cb, "Применение правил (rule engine)…")
        try:
            risks = rule_engine.apply_rules(context, rules)
        except Exception as exc:
            risks = []
            result.errors.append(f"rule_engine: {exc}")
        result.risks = risks

        # --- Risk scoring / confidence ---
        try:
            result.risk_score = risk_scoring.compute_score(risks)
            result.risk_level = risk_scoring.score_to_level(result.risk_score).value
            result.confidence = risk_scoring.compute_confidence(result)
        except Exception as exc:
            result.errors.append(f"risk_scoring: {exc}")

        result.requires_manual_review = True  # авто-проверка всегда требует юриста

        # --- 10-12. Тексты (резюме, КП, письмо) + PDF ---
        _finalize_texts_and_pdf(
            result, context, settings, llm_client, progress_cb,
            scan_input=scan_input, create_pdf=scan_input.create_pdf,
        )

    except Exception as exc:  # общий предохранитель
        result.errors.append("orchestrator: " + str(exc))
        result.errors.append(traceback.format_exc(limit=3))
        result.requires_manual_review = True
    finally:
        try:
            fetcher.close()
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Построение ScanContext
# ---------------------------------------------------------------------------
def _build_context(
    scan_input: ScanInput,
    base_url: str,
    result: ScanResult,
    technical: TechnicalResult,
) -> ScanContext:
    forms = result.forms
    trackers = result.trackers
    industry = (scan_input.industry or "auto").lower()

    pd_categories: List[str] = []
    for f in forms:
        pd_categories.extend(f.personal_data_fields)
    pd_categories = utils.dedupe_keep_order(pd_categories)

    has_pd_forms = any(f.potentially_personal_data_form for f in forms)
    medical = industry.startswith("медиц") or any(f.is_medical for f in forms) or _industry_hint(result, ["диагноз", "симптом", "запись к врачу", "клиник", "прием врача"])
    children = "образован" in industry or any(f.is_children_related for f in forms)
    hr = "hr" in industry or "рекрут" in industry or any(f.is_hr for f in forms)
    ecommerce = "commerce" in industry or any(f.is_order for f in forms) or any(
        t.category in ("payment", "delivery") for t in trackers
    )
    finance = industry.startswith("финанс")
    newsletter_form = any(f.is_newsletter for f in forms)
    file_upload = any(f.has_file_upload for f in forms)

    docs = result.documents
    has = lambda dt: any(d.doc_type == dt and d.is_accessible for d in docs)

    return ScanContext(
        company_name=scan_input.company_name,
        site_url=scan_input.site_url,
        final_url=result.final_url or base_url,
        registered_domain=utils.get_registered_domain(base_url),
        industry=scan_input.industry,
        forms=forms,
        documents=docs,
        trackers=trackers,
        cookies=result.cookies,
        technical=technical,
        pages=result.pages,
        has_forms=len(forms) > 0,
        has_pd_forms=has_pd_forms,
        personal_data_categories=pd_categories,
        newsletter_form=newsletter_form,
        file_upload=file_upload,
        medical=medical,
        children=children,
        hr=hr,
        ecommerce=ecommerce,
        finance=finance,
        publishes_people=_industry_hint(result, ["отзыв", "наши клиенты", "команда", "наши специалисты", "кейс"]),
        trackers_present=len(trackers) > 0,
        foreign_trackers_present=any(t.country_hint == "foreign" for t in trackers),
        cookies_present=len(trackers) > 0 or len(result.cookies) > 0,
        cookie_banner_found=result.cookie_banner_found,
        marketing_cookies_before_consent=result.marketing_cookies_before_consent,
        has_privacy_policy=has("privacy_policy"),
        has_consent_doc=has("consent"),
        has_ad_consent_doc=has("ad_consent"),
        has_cookie_policy=has("cookie_policy"),
        has_offer=has("offer"),
        has_user_agreement=has("user_agreement"),
    )


def _industry_hint(result: ScanResult, keywords: List[str]) -> bool:
    text = " ".join(p.title for p in result.pages)
    return utils.contains_any(text, keywords)


def _dedupe_trackers(trackers: List[TrackerHit]) -> List[TrackerHit]:
    seen = set()
    out = []
    for t in trackers:
        key = (t.provider_name, t.matched_domain)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _dedupe_documents(documents: List[DocumentResult]) -> List[DocumentResult]:
    """
    Убрать шум из списка документов: проба приоритетных путей часто даёт 404,
    что превращается в десятки недоступных «документов». Оставляем все доступные,
    а из недоступных — не более одного на тип, и только для типов, у которых нет
    ни одного доступного документа (как сигнал «искали, но не открылось»).
    """
    # 1) Дедуп по URL: предпочесть доступный и с большим текстом.
    by_url: Dict[str, DocumentResult] = {}
    for d in documents:
        key = utils.strip_fragment(d.url)
        prev = by_url.get(key)
        if prev is None or (d.is_accessible, d.text_length) > (prev.is_accessible, prev.text_length):
            by_url[key] = d
    docs = list(by_url.values())

    accessible = [d for d in docs if d.is_accessible and d.text_length > 0]
    accessible_ids = {id(d) for d in accessible}
    accessible_types = {d.doc_type for d in accessible}

    kept_inacc: Dict[str, DocumentResult] = {}
    for d in docs:
        if id(d) in accessible_ids or d.doc_type in accessible_types:
            continue
        kept_inacc.setdefault(d.doc_type, d)

    return accessible + list(kept_inacc.values())


def _dedupe_cookies(cookies: List[CookieInfo]) -> List[CookieInfo]:
    seen = set()
    out = []
    for c in cookies:
        key = (c.name, c.domain)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# Тексты и PDF
# ---------------------------------------------------------------------------
def _finalize_texts_and_pdf(
    result: ScanResult,
    context: ScanContext,
    settings: Settings,
    llm_client_module,
    progress_cb: ProgressCb,
    scan_input: ScanInput,
    create_pdf: bool,
) -> None:
    from reports import pdf_generator

    _emit(progress_cb, "Формирование резюме, коммерческого предложения и письма…")
    try:
        client = (
            llm_client_module.get_client(settings)
            if (settings.enable_llm and scan_input.use_llm)
            else None
        )
        packages = _load_packages(settings)
        summary, offer, email = llm_client_module.build_client_texts(
            result, context, settings, client, packages
        )
        result.executive_summary = summary
        result.commercial_offer_text = offer
        result.email_text = email
    except Exception as exc:
        result.errors.append(f"build_client_texts: {exc}")

    if create_pdf:
        _emit(progress_cb, "Генерация PDF-отчёта…")
        try:
            packages = _load_packages(settings)
            pdf_path = pdf_generator.generate_pdf(result, settings, packages)
            if pdf_path:
                result.pdf_path = pdf_path
        except Exception as exc:
            result.errors.append(f"pdf_generator: {exc}")


def _load_packages(settings: Settings) -> dict:
    try:
        import yaml

        with open(settings.packages_path(), "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}
