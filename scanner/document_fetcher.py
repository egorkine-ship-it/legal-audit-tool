"""
Загрузка и первичный разбор одного документа-кандидата.

Возвращает DocumentResult: переиспользует уже загруженный HTML (если страница
документа была скачана при обходе), иначе скачивает документ через
utils.http_get и извлекает текст в зависимости от формата (html/pdf/docx).

Публичная функция fetch_document никогда не бросает исключения.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from scanner import document_parser, utils
from scanner.models import DocCandidate, DocumentResult, Evidence, RawPage


# Evidence-хелпер живёт в scanner.evidence (может быть ещё не реализован в
# соседней группе) — импортируем мягко, иначе используем локальный фолбэк.
def _make_evidence(page_url: str = "", quote: str = "", html_snippet: str = "") -> Evidence:
    try:
        from scanner import evidence as _evidence  # type: ignore

        return _evidence.make_evidence(
            page_url=page_url, quote=quote, html_snippet=html_snippet
        )
    except Exception:
        return Evidence(
            page_url=page_url or "",
            quote=utils.truncate(quote or "", 500),
            html_snippet=utils.truncate(html_snippet or "", 800),
        )


def fetch_document(
    candidate: DocCandidate,
    raw_by_url: Dict[str, RawPage],
    settings,
    fetcher: Optional[Any] = None,
) -> DocumentResult:
    """Загрузить и разобрать документ-кандидат. Возвращает DocumentResult.

    Логика:
      1. Если stripped url кандидата уже есть в raw_by_url и у RawPage есть html —
         переиспользуем: format="html", text = html_to_text(raw.html).
      2. Иначе, если передан fetcher с методом playwright и URL похож на
         HTML-маршрут (не .pdf/.docx/.doc) — рендерим страницу С JS через
         fetcher.fetch(url) (SPA-политики без JS отдают пустой html).
      3. Иначе скачиваем через utils.http_get; формат определяем
         document_parser.detect_format; извлекаем текст.

    Дополнительно: doc.link_confirmed = True, если URL реально открылся
    (2xx на том же зарегистрированном домене) — НЕЗАВИСИМО от того, извлёкся
    ли текст. doc.discovered_by = источник кандидата (priority_path -> "guess").

    Параметр fetcher опционален — старые вызовы без него работают как раньше.
    Никогда не бросает.
    """
    doc = DocumentResult(
        doc_id=uuid.uuid4().hex[:10],
        doc_type=getattr(candidate, "doc_type", "other") or "other",
        url=getattr(candidate, "url", "") or "",
        title=getattr(candidate, "title", "") or "",
    )
    # Как документ был найден: источник кандидата; догадки по типовым путям
    # помечаем как "guess", чтобы в отчёте отличать их от реальных ссылок сайта.
    try:
        src = getattr(candidate, "source", "") or ""
        doc.discovered_by = "guess" if src == "priority_path" else src
    except Exception:
        pass
    try:
        url = doc.url
        if not url:
            doc.is_accessible = False
            doc.requires_manual_review = True
            doc.extraction_error = "empty_url"
            doc.evidence = _make_evidence(page_url="")
            return doc

        raw_by_url = raw_by_url or {}
        try:
            key = utils.strip_fragment(url)
        except Exception:
            key = url

        reused = raw_by_url.get(key)
        if reused is None:
            # Попытка по final_url кандидата тоже (на всякий случай).
            reused = raw_by_url.get(url)

        if reused is not None and getattr(reused, "html", ""):
            _fill_from_raw(doc, reused)
        else:
            # URL не был загружен при обходе — пробуем отрендерить с JS
            # (если доступен Playwright-fetcher), иначе обычный HTTP GET.
            rendered = _try_render_document(doc, url, fetcher)
            if not rendered:
                _fetch_and_parse(doc, url, settings)

        # --- Общие поля на основе извлечённого текста ---
        _finalize_common(doc)
        return doc
    except Exception as exc:  # предохранитель
        doc.is_accessible = False
        doc.requires_manual_review = True
        doc.extraction_error = (doc.extraction_error or "") or f"fetch_error: {exc}"
        if not doc.evidence or not getattr(doc.evidence, "page_url", ""):
            doc.evidence = _make_evidence(page_url=doc.url)
        return doc


# ---------------------------------------------------------------------------
# Подтверждение существования документа по URL
# ---------------------------------------------------------------------------
def _confirm_link(doc: DocumentResult, status_code, ok, final_url: str) -> None:
    """Пометить doc.link_confirmed = True, если URL реально открылся.

    Критерий: ответ ok, итоговый статус 2xx (после редиректов) и итоговый URL
    остался на том же зарегистрированном домене, что и URL документа.
    «Существует» и «текст извлечён» — разные вещи; link_confirmed НЕ зависит от
    извлечения текста. Никогда не бросает.
    """
    try:
        if not ok:
            return
        try:
            code = int(status_code or 0)
        except Exception:
            code = 0
        if not (200 <= code < 300):
            return
        fu = final_url or ""
        if fu and doc.url and not utils.same_registered_domain(fu, doc.url):
            # Редирект увёл на чужой домен — это не документ нашего сайта.
            return
        # Мягкий 404: неизвестный путь редиректит на главную. Если итоговый URL
        # «схлопнулся» до корня сайта, а у документа был непустой путь — это НЕ
        # подтверждение существования документа.
        try:
            from urllib.parse import urlparse

            fp = urlparse(fu)
            dp = urlparse(doc.url)
            final_path = (fp.path or "").strip("/")
            doc_path = (dp.path or "").strip("/")
            if doc_path and not final_path:
                return
        except Exception:
            pass
        doc.link_confirmed = True
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Рендер документа через Playwright-fetcher (SPA-политики требуют JS)
# ---------------------------------------------------------------------------
def _try_render_document(doc: DocumentResult, url: str, fetcher: Optional[Any]) -> bool:
    """Best-effort рендер HTML-документа через fetcher с JS (Playwright).

    Возвращает True, если страница отрендерилась (получен html) и doc заполнен;
    False — если рендер невозможен/не удался (вызывающий откатится к http_get).
    Бинарные форматы (.pdf/.docx/.doc) не рендерим. Никогда не бросает.
    """
    try:
        if fetcher is None:
            return False
        if getattr(fetcher, "method", "") != "playwright":
            return False
        try:
            ext = utils.url_extension(url)
        except Exception:
            ext = ""
        if ext in ("pdf", "docx", "doc"):
            return False
        raw = fetcher.fetch(url)
        if raw is None or not getattr(raw, "html", ""):
            return False
        # Провал навигации (PDF-скачивание -> ERR_ABORTED, сетевые/TLS-ошибки,
        # about:blank) даёт «html», но бесполезный. Если рендер не дал ни текста,
        # ни подтверждённой ссылки — откатываемся к http_get (важно для pdf/docx
        # и для не-html ответов, которые браузер не показывает как документ).
        final_url = getattr(raw, "final_url", "") or ""
        goto_failed = any(
            str(e).startswith("goto:") for e in (getattr(raw, "errors", None) or [])
        )
        if final_url.startswith("about:blank") or goto_failed:
            return False
        _fill_from_raw(doc, raw)
        if doc.text_length == 0 and not doc.link_confirmed:
            # Рендер ничего полезного не дал — пусть http_get попробует байты.
            return False
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Переиспользование уже загруженной страницы
# ---------------------------------------------------------------------------
def _fill_from_raw(doc: DocumentResult, raw: RawPage) -> None:
    doc.format = "html"
    doc.status_code = getattr(raw, "status_code", 0) or 0
    if not doc.title:
        doc.title = getattr(raw, "title", "") or ""
    _confirm_link(
        doc,
        getattr(raw, "status_code", 0),
        getattr(raw, "ok", False),
        getattr(raw, "final_url", "") or "",
    )
    html = getattr(raw, "html", "") or ""
    try:
        text = document_parser.html_to_text(html)
    except Exception:
        text = ""
    doc.text = text or ""
    doc.text_length = len(doc.text)
    doc.is_accessible = bool(getattr(raw, "ok", False)) and doc.text_length > 0
    if doc.text_length == 0:
        doc.text_extraction_failed = True
        doc.extraction_error = doc.extraction_error or "text_extraction_failed"
        doc.requires_manual_review = True
    snippet = html[:800] if html else ""
    doc.evidence = _make_evidence(
        page_url=getattr(raw, "final_url", "") or doc.url,
        quote=doc.text,
        html_snippet=snippet,
    )


# ---------------------------------------------------------------------------
# Скачивание и разбор
# ---------------------------------------------------------------------------
def _fetch_and_parse(doc: DocumentResult, url: str, settings) -> None:
    user_agent = getattr(settings, "user_agent", "") or ""
    timeout_s = getattr(settings, "request_timeout_s", 20) or 20
    max_bytes = getattr(settings, "max_download_bytes", 8 * 1024 * 1024) or (
        8 * 1024 * 1024
    )

    resp = utils.http_get(url, user_agent, timeout_s, max_bytes)
    doc.status_code = getattr(resp, "status_code", 0) or 0
    _confirm_link(
        doc,
        getattr(resp, "status_code", 0),
        getattr(resp, "ok", False),
        getattr(resp, "final_url", "") or "",
    )

    fmt = document_parser.detect_format(
        getattr(resp, "final_url", "") or url,
        getattr(resp, "content_type", "") or "",
    )
    doc.format = fmt

    resp_ok = bool(getattr(resp, "ok", False))
    if not resp_ok and not getattr(resp, "content", b""):
        # Совсем недоступно.
        doc.is_accessible = False
        doc.requires_manual_review = True
        err = getattr(resp, "error", "") or ""
        doc.extraction_error = err or "unreachable"
        doc.evidence = _make_evidence(page_url=getattr(resp, "final_url", "") or url)
        return

    if fmt == "html":
        html_text = getattr(resp, "text", "") or ""
        if not html_text:
            # http_get не декодирует бинарные типы — декодируем сами из bytes.
            text, err = document_parser.extract_text_from_bytes(
                getattr(resp, "content", b""), "html"
            )
        else:
            text = document_parser.html_to_text(html_text)
            err = "" if text else "text_extraction_failed"
        doc.text = text or ""
        doc.text_length = len(doc.text)
        if doc.text_length == 0:
            doc.text_extraction_failed = True
            doc.requires_manual_review = True
            doc.extraction_error = err or "text_extraction_failed"
        doc.is_accessible = resp_ok and doc.text_length > 0
        snippet = (html_text or "")[:800]
        doc.evidence = _make_evidence(
            page_url=getattr(resp, "final_url", "") or url,
            quote=doc.text,
            html_snippet=snippet,
        )
        return

    # Бинарные форматы: pdf / docx.
    content = getattr(resp, "content", b"") or b""
    text, err = document_parser.extract_text_from_bytes(content, fmt)
    doc.text = text or ""
    doc.text_length = len(doc.text)
    if doc.text_length == 0:
        doc.text_extraction_failed = True
        doc.requires_manual_review = True
        doc.extraction_error = err or "text_extraction_failed"
    # Бинарный документ считаем «доступным для анализа» только если из него
    # реально извлёкся текст. Иначе (скан/образ PDF, нет pypdf/docx) он не должен
    # молча считаться присутствующей политикой — помечаем как требующий ручной
    # проверки, а наличие текста = False (см. requires_manual_review выше).
    doc.is_accessible = resp_ok and doc.text_length > 0
    doc.evidence = _make_evidence(
        page_url=getattr(resp, "final_url", "") or url,
        quote=doc.text,
        html_snippet="",
    )


# ---------------------------------------------------------------------------
# Финальные общие поля
# ---------------------------------------------------------------------------
def _finalize_common(doc: DocumentResult) -> None:
    text = doc.text or ""
    try:
        doc.date_detected = document_parser.detect_date(text)
    except Exception:
        doc.date_detected = ""
    try:
        doc.version_detected = document_parser.detect_version(text)
    except Exception:
        doc.version_detected = ""
    try:
        doc.template_placeholder_detected = bool(utils.find_placeholders(text))
    except Exception:
        doc.template_placeholder_detected = False

    # Гарантия наличия evidence с url.
    if not doc.evidence or not getattr(doc.evidence, "page_url", ""):
        doc.evidence = _make_evidence(page_url=doc.url, quote=text)
