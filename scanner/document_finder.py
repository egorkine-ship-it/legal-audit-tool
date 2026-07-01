"""
Поиск ссылок на юридические документы сайта (политика ПДн, согласие, cookie,
оферта, пользовательское соглашение) и их предварительная классификация.

Возвращает список DocCandidate. Тяжёлая зависимость bs4 импортируется внутри
функции и обёрнута в try/except (есть регулярный фолбэк по <a ...>). Публичные
функции никогда не бросают исключения.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from scanner import utils
from scanner.models import DOC_TYPES, DocCandidate  # noqa: F401


# ---------------------------------------------------------------------------
# Ключевые слова -> тип документа
# ---------------------------------------------------------------------------
# Порядок важен: более специфичные типы (cookie/ad_consent/consent) проверяются
# раньше, чем общая privacy_policy, чтобы не поглотить их.
DOC_TYPE_KEYWORDS: List[Tuple[str, List[str]]] = [
    (
        "cookie_policy",
        [
            "cookie", "cookies", "куки", "файлы cookie", "политика cookie",
            "политика в отношении cookie", "cookie-policy", "cookie_policy",
            "обработк cookie",
        ],
    ),
    (
        "ad_consent",
        [
            "рекламн", "рассылк", "маркетингов", "получение рекламы",
            "рекламная рассылка", "согласие на рекламу", "согласие на рассылку",
            "информационная рассылка", "e-mail рассылк", "email-рассылк",
        ],
    ),
    (
        "consent",
        [
            "согласие на обработку", "согласие на обработку персональных",
            "согласие субъекта", "даю согласие", "soglasie", "consent",
            "согласие на обработку пдн", "форма согласия",
        ],
    ),
    (
        "offer",
        [
            "оферта", "публичная оферта", "договор оферты", "oferta", "offer",
            "публичн оферт", "публичная-оферта",
        ],
    ),
    (
        "user_agreement",
        [
            "пользовательское соглашение", "user agreement", "user-agreement",
            "условия использования", "terms", "terms of use", "terms of service",
            "правила пользования", "правила использования", "соглашение об использовании",
        ],
    ),
    (
        "privacy_policy",
        [
            "политика конфиденциальности", "политика обработки",
            "политика обработки персональных данных", "конфиденциальность",
            "персональные данные", "обработка персональных данных",
            "защита персональных данных", "privacy", "privacy policy",
            "privacy-policy", "personal data", "personal-data", "politika",
            "политика в отношении обработки", "обработка пдн", "персональных данных",
            "политик",
        ],
    ),
]


# ---------------------------------------------------------------------------
# Типовые пути -> тип документа (создаются как кандидаты source="priority_path")
# ---------------------------------------------------------------------------
DOC_PRIORITY_PATHS: Dict[str, str] = {
    "/privacy": "privacy_policy",
    "/privacy-policy": "privacy_policy",
    "/privacy.html": "privacy_policy",
    "/policy": "privacy_policy",
    "/personal-data": "privacy_policy",
    "/personal-data-policy": "privacy_policy",
    "/politika": "privacy_policy",
    "/politika-konfidentsialnosti": "privacy_policy",
    "/obrabotka-personalnyh-dannyh": "privacy_policy",
    "/consent": "consent",
    "/soglasie": "consent",
    "/cookie": "cookie_policy",
    "/cookies": "cookie_policy",
    "/cookie-policy": "cookie_policy",
    "/oferta": "offer",
    "/offer": "offer",
    "/publichnaya-oferta": "offer",
    "/user-agreement": "user_agreement",
    "/terms": "user_agreement",
    "/agreement": "user_agreement",
}


# Множество всех ключевых слов (для быстрой проверки «похоже ли это на документ»).
def _all_doc_keywords() -> List[str]:
    kws: List[str] = []
    for _dt, words in DOC_TYPE_KEYWORDS:
        kws.extend(words)
    return kws


ALL_DOC_KEYWORDS = _all_doc_keywords()


# ---------------------------------------------------------------------------
# Классификация типа документа
# ---------------------------------------------------------------------------
def classify_doc_type(anchor_text: str, url: str, title: str) -> str:
    """Определить тип документа по тексту ссылки, URL и заголовку.

    Возвращает один из DOC_TYPES; по умолчанию "other". Никогда не бросает.
    """
    try:
        haystack = " ".join(
            [
                anchor_text or "",
                _url_search_blob(url or ""),
                title or "",
            ]
        )
        norm = utils.normalize_for_search(haystack)
        if not norm:
            return "other"
        for doc_type, words in DOC_TYPE_KEYWORDS:
            for kw in words:
                nk = utils.normalize_for_search(kw)
                if nk and nk in norm:
                    return doc_type
        return "other"
    except Exception:
        return "other"


def _url_search_blob(url: str) -> str:
    """Путь URL, пригодный для поиска ключевых слов (дефисы/подчёркивания -> пробелы)."""
    try:
        from urllib.parse import urlparse, unquote

        p = urlparse(url)
        path = unquote(p.path or "")
    except Exception:
        path = url or ""
    # Оставляем и исходный путь (для 'cookie-policy'), и версию с разделителями.
    spaced = re.sub(r"[/_\-.]+", " ", path)
    return url + " " + path + " " + spaced


def _looks_like_document_link(anchor_text: str, url: str, title: str) -> bool:
    """Ссылка похожа на юридический документ (по ключевым словам или расширению)."""
    try:
        # PDF/DOCX-ссылки с релевантными словами считаем документами.
        ext = utils.url_extension(url or "")
        blob = " ".join([anchor_text or "", _url_search_blob(url or ""), title or ""])
        if utils.contains_any(blob, ALL_DOC_KEYWORDS):
            return True
        if ext in ("pdf", "docx", "doc") and utils.contains_any(
            blob, ALL_DOC_KEYWORDS
        ):
            return True
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Извлечение якорей из HTML
# ---------------------------------------------------------------------------
_A_TAG_RE = re.compile(
    r"<a\b([^>]*?)>(.*?)</a>", re.IGNORECASE | re.DOTALL
)
_HREF_RE = re.compile(r"""href\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_TITLE_ATTR_RE = re.compile(r"""title\s*=\s*["']([^"']*)["']""", re.IGNORECASE)
_INNER_TAG_RE = re.compile(r"<[^>]+>")


def _extract_anchors(html: str) -> List[Tuple[str, str, str]]:
    """Вернуть список (href, anchor_text, title) из HTML.

    Использует bs4, если доступен; иначе — регулярный фолбэк. Никогда не бросает.
    """
    if not html:
        return []
    # bs4-путь.
    try:
        from bs4 import BeautifulSoup  # type: ignore

        anchors: List[Tuple[str, str, str]] = []
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a"):
            try:
                href = a.get("href") or ""
                text = a.get_text(" ") if hasattr(a, "get_text") else ""
                title = a.get("title") or ""
                anchors.append((href.strip(), utils.clean_text(text), title.strip()))
            except Exception:
                continue
        return anchors
    except Exception:
        pass

    # Регулярный фолбэк.
    anchors = []
    try:
        for m in _A_TAG_RE.finditer(html):
            attrs = m.group(1) or ""
            inner = m.group(2) or ""
            hm = _HREF_RE.search(attrs)
            href = hm.group(1).strip() if hm else ""
            tm = _TITLE_ATTR_RE.search(attrs)
            title = tm.group(1).strip() if tm else ""
            text = utils.clean_text(_INNER_TAG_RE.sub(" ", inner))
            anchors.append((href, text, title))
    except Exception:
        return anchors
    return anchors


# ---------------------------------------------------------------------------
# Основная функция поиска кандидатов
# ---------------------------------------------------------------------------
def find_document_candidates(
    pages,
    raw_pages,
    base_url: str,
    robots,
    sitemap_urls,
    settings,
) -> List[DocCandidate]:
    """Собрать список кандидатов-документов сайта.

    Источники:
      1. Якоря из HTML каждой raw_page, чей текст/href/title похож на документ.
      2. Типовые пути DOC_PRIORITY_PATHS (source="priority_path").
      3. URL из sitemap, содержащие ключевые слова (source="sitemap").

    Дедупликация по url без фрагмента; при конфликте сохраняем наиболее
    специфичный (не "other") тип. Cap ~40. Никогда не бросает.
    """
    candidates: List[DocCandidate] = []
    try:
        raw_list = list(raw_pages or [])
    except Exception:
        raw_list = []

    # --- 1. Якоря из HTML ---
    for raw in raw_list:
        try:
            html = getattr(raw, "html", "") or ""
            page_url = getattr(raw, "final_url", "") or getattr(raw, "url", "") or ""
        except Exception:
            html, page_url = "", ""
        if not html:
            continue
        for href, text, title in _extract_anchors(html):
            try:
                if not href:
                    continue
                abs_url = utils.absolute_url(page_url or base_url, href)
                if not abs_url:
                    continue
                # Только тот же зарегистрированный домен.
                if not utils.same_registered_domain(abs_url, base_url):
                    continue
                if not _looks_like_document_link(text, abs_url, title):
                    continue
                doc_type = classify_doc_type(text, abs_url, title)
                candidates.append(
                    DocCandidate(
                        url=abs_url,
                        doc_type=doc_type,
                        anchor_text=utils.truncate(text, 200),
                        title=utils.truncate(title, 200),
                        source="anchor",
                        page_url=page_url,
                    )
                )
            except Exception:
                continue

    # --- 2. Типовые пути ---
    for path, doc_type in DOC_PRIORITY_PATHS.items():
        try:
            abs_url = utils.absolute_url(base_url, path)
            if not abs_url:
                continue
            candidates.append(
                DocCandidate(
                    url=abs_url,
                    doc_type=doc_type,
                    anchor_text="",
                    title="",
                    source="priority_path",
                    page_url=base_url,
                )
            )
        except Exception:
            continue

    # --- 3. Sitemap ---
    for sm_url in (sitemap_urls or []):
        try:
            if not sm_url:
                continue
            if not utils.same_registered_domain(sm_url, base_url):
                continue
            if not _looks_like_document_link("", sm_url, ""):
                continue
            doc_type = classify_doc_type("", sm_url, "")
            candidates.append(
                DocCandidate(
                    url=sm_url,
                    doc_type=doc_type,
                    anchor_text="",
                    title="",
                    source="sitemap",
                    page_url=base_url,
                )
            )
        except Exception:
            continue

    # --- Дедупликация по url без фрагмента ---
    return _dedupe_candidates(candidates, cap=40)


# Приоритет источника при равенстве типа (более «сильный» источник побеждает).
_SOURCE_RANK = {
    "anchor": 3,
    "priority_path": 2,
    "sitemap": 1,
    "": 0,
}


def _dedupe_candidates(
    candidates: List[DocCandidate], cap: int = 40
) -> List[DocCandidate]:
    """Дедуп по stripped url; при конфликте оставляем не-'other' тип и лучший источник."""
    best: Dict[str, DocCandidate] = {}
    order: List[str] = []
    for cand in candidates:
        try:
            key = utils.strip_fragment(cand.url or "")
        except Exception:
            key = cand.url or ""
        if not key:
            continue
        existing = best.get(key)
        if existing is None:
            best[key] = cand
            order.append(key)
            continue
        # Выбираем более специфичный (не "other") тип.
        cur_specific = existing.doc_type != "other"
        new_specific = cand.doc_type != "other"
        if new_specific and not cur_specific:
            best[key] = cand
        elif new_specific == cur_specific:
            # При равной специфичности предпочитаем более «сильный» источник.
            if _SOURCE_RANK.get(cand.source, 0) > _SOURCE_RANK.get(
                existing.source, 0
            ):
                best[key] = cand

    out = [best[k] for k in order]
    return out[:cap]
