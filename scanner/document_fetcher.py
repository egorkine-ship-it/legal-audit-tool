"""
Загрузка и первичный разбор одного документа-кандидата.

Возвращает DocumentResult: переиспользует уже загруженный HTML (если страница
документа была скачана при обходе), иначе скачивает документ через
utils.http_get и извлекает текст в зависимости от формата (html/pdf/docx).

Публичная функция fetch_document никогда не бросает исключения.
"""
from __future__ import annotations

import uuid
from typing import Dict

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
) -> DocumentResult:
    """Загрузить и разобрать документ-кандидат. Возвращает DocumentResult.

    Логика:
      1. Если stripped url кандидата уже есть в raw_by_url и у RawPage есть html —
         переиспользуем: format="html", text = html_to_text(raw.html).
      2. Иначе скачиваем через utils.http_get; формат определяем
         document_parser.detect_format; извлекаем текст.
    Никогда не бросает.
    """
    doc = DocumentResult(
        doc_id=uuid.uuid4().hex[:10],
        doc_type=getattr(candidate, "doc_type", "other") or "other",
        url=getattr(candidate, "url", "") or "",
        title=getattr(candidate, "title", "") or "",
    )
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
# Переиспользование уже загруженной страницы
# ---------------------------------------------------------------------------
def _fill_from_raw(doc: DocumentResult, raw: RawPage) -> None:
    doc.format = "html"
    doc.status_code = getattr(raw, "status_code", 0) or 0
    if not doc.title:
        doc.title = getattr(raw, "title", "") or ""
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
