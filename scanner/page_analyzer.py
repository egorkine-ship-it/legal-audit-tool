"""
Анализ одной загруженной страницы (RawPage) -> PageResult.

Собирает по странице: формы (обычные + из модалок), баннер cookie, cookies до
согласия, трекеры, внешние домены и служебные метрики. Тяжёлую работу выполняют
специализированные детекторы (form_detector, cookie_detector, tracker_detector),
которые импортируются лениво и вызываются в try/except — отсутствие или сбой
любого из них не ломает анализ страницы.

Публичный API:
    analyze_page(raw, base_url, settings, tracker_index) -> PageResult
"""
from __future__ import annotations

from typing import Any, List, Optional

from scanner import utils
from scanner.models import (
    CookieInfo,
    FormResult,
    PageResult,
    RawPage,
    TrackerHit,
)


def analyze_page(
    raw: RawPage,
    base_url: str,
    settings: Any,
    tracker_index: Any,
) -> PageResult:
    """
    Проанализировать одну страницу и вернуть PageResult.

    Все шаги обёрнуты в try/except: сбой отдельного детектора не роняет анализ,
    ошибки складываются в PageResult.errors.
    """
    errors: List[str] = []

    # Базовые поля / URL страницы.
    page_url = ""
    try:
        page_url = raw.final_url or raw.url or ""
    except Exception:
        page_url = ""

    html = ""
    visible_text = ""
    try:
        html = raw.html or ""
    except Exception:
        html = ""
    try:
        visible_text = raw.visible_text or ""
    except Exception:
        visible_text = ""

    # Переносим уже накопленные ошибки загрузки страницы.
    try:
        if raw.errors:
            errors.extend(list(raw.errors))
    except Exception:
        pass

    # --- Формы (обычный HTML + модалки) ---
    forms: List[FormResult] = []
    try:
        from scanner import form_detector

        try:
            html_forms = form_detector.detect_forms(html, page_url, "html", settings)
            if html_forms:
                forms.extend(list(html_forms))
        except Exception as exc:
            errors.append(f"detect_forms(html): {exc}")

        modal_htmls: List[str] = []
        try:
            modal_htmls = list(raw.modal_htmls or [])
        except Exception:
            modal_htmls = []
        for mhtml in modal_htmls:
            if not mhtml:
                continue
            try:
                modal_forms = form_detector.detect_forms(
                    mhtml, page_url, "modal", settings
                )
                if modal_forms:
                    forms.extend(list(modal_forms))
            except Exception as exc:
                errors.append(f"detect_forms(modal): {exc}")
    except Exception as exc:
        errors.append(f"form_detector: {exc}")

    # --- Баннер cookie + классификация cookies ---
    cookie_banner_found = False
    cookie_banner_evidence = ""
    cookies_before_consent: List[CookieInfo] = []
    try:
        from scanner import cookie_detector

        try:
            found, ev = cookie_detector.detect_cookie_banner(html, visible_text)
            cookie_banner_found = bool(found)
            cookie_banner_evidence = ev or ""
        except Exception as exc:
            errors.append(f"detect_cookie_banner: {exc}")

        raw_cookies = []
        try:
            raw_cookies = list(raw.cookies or [])
        except Exception:
            raw_cookies = []
        try:
            classified = cookie_detector.classify_cookies(raw_cookies, tracker_index)
            for c in (classified or []):
                try:
                    if not c.page_url:
                        c.page_url = page_url
                except Exception:
                    pass
                cookies_before_consent.append(c)
        except Exception as exc:
            errors.append(f"classify_cookies: {exc}")
    except Exception as exc:
        errors.append(f"cookie_detector: {exc}")

    # --- Трекеры ---
    trackers: List[TrackerHit] = []
    try:
        from scanner import tracker_detector

        try:
            hits = tracker_detector.detect_trackers(raw, tracker_index)
            for t in (hits or []):
                try:
                    if not t.page_url:
                        t.page_url = page_url
                except Exception:
                    pass
                trackers.append(t)
        except Exception as exc:
            errors.append(f"detect_trackers: {exc}")
    except Exception as exc:
        errors.append(f"tracker_detector: {exc}")

    # --- Внешние домены (уникальные зарегистрированные домены) ---
    external_domains: List[str] = []
    try:
        candidate_urls: List[str] = []
        try:
            candidate_urls.extend(list(raw.links or []))
        except Exception:
            pass
        try:
            candidate_urls.extend(list(raw.external_resources or []))
        except Exception:
            pass

        ref = base_url or page_url
        found_domains: List[str] = []
        for u in candidate_urls:
            if not u or not isinstance(u, str):
                continue
            try:
                if not utils.is_external(u, ref):
                    continue
                dom = utils.get_registered_domain(u)
            except Exception:
                continue
            if dom:
                found_domains.append(dom)
        external_domains = utils.dedupe_keep_order(found_domains)
    except Exception as exc:
        errors.append(f"external_domains: {exc}")

    # --- Служебные метрики ---
    html_length = 0
    text_length = 0
    try:
        html_length = len(html)
    except Exception:
        html_length = 0
    try:
        # Длина текста — по видимому тексту, с фолбэком на text_content.
        text_source = visible_text or (raw.text_content or "")
        text_length = len(text_source)
    except Exception:
        text_length = 0

    links: List[str] = []
    try:
        links = list(raw.links or [])
    except Exception:
        links = []

    network_requests: List[str] = []
    try:
        network_requests = list(raw.network_requests or [])
    except Exception:
        network_requests = []

    status_code = 0
    title = ""
    final_url = ""
    screenshot_path = ""
    fetch_method = "http"
    try:
        status_code = int(raw.status_code or 0)
    except Exception:
        status_code = 0
    try:
        title = raw.title or ""
    except Exception:
        title = ""
    try:
        final_url = raw.final_url or ""
    except Exception:
        final_url = ""
    try:
        screenshot_path = raw.screenshot_path or ""
    except Exception:
        screenshot_path = ""
    try:
        fetch_method = raw.fetch_method or "http"
    except Exception:
        fetch_method = "http"

    url_value = ""
    try:
        url_value = raw.url or page_url or ""
    except Exception:
        url_value = page_url or ""

    try:
        return PageResult(
            url=url_value,
            final_url=final_url,
            title=title,
            status_code=status_code,
            html_length=html_length,
            text_length=text_length,
            links=links,
            external_domains=external_domains,
            network_requests=network_requests,
            cookies_before_consent=cookies_before_consent,
            forms=forms,
            trackers=trackers,
            cookie_banner_found=cookie_banner_found,
            cookie_banner_evidence=cookie_banner_evidence,
            screenshot_path=screenshot_path,
            fetch_method=fetch_method,
            errors=errors,
        )
    except Exception as exc:
        # Крайний фолбэк — минимальный валидный PageResult.
        return PageResult(
            url=url_value or page_url or "",
            errors=errors + [f"page_analyzer: {exc}"],
        )
