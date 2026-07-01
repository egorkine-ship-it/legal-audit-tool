"""
Тесты «укрепления» обнаружения документов (discovery hardening).

Проверяют без сети (синтетические данные):
  * find_document_candidates подхватывает ссылки из pages[].links (source="page_link");
  * fetch_document умеет рендерить документ через переданный Playwright-fetcher;
  * link_confirmed выставляется независимо от извлечения текста;
  * create_fetcher сохраняет причину недоступности Playwright (launch_error);
  * discover_urls не отбрасывает ссылку с ключевым словом в тексте по расширению.
"""
from __future__ import annotations

import sys
from typing import List, Optional

from scanner import browser, crawler, document_fetcher, document_finder, utils
from scanner.models import DocCandidate, PageResult, RawPage


# ---------------------------------------------------------------------------
# Вспомогательные объекты
# ---------------------------------------------------------------------------
class _Settings:
    """Минимальный контейнер настроек (модули читают поля через getattr)."""

    user_agent = "test-agent"
    request_timeout_s = 5
    max_download_bytes = 1024 * 1024
    max_pages = 20
    enable_screenshots = False


class _FakePlaywrightFetcher:
    """Поддельный fetcher с методом playwright: возвращает заранее заданный RawPage."""

    method = "playwright"

    def __init__(self, raw: Optional[RawPage]) -> None:
        self._raw = raw
        self.calls: List[str] = []

    def fetch(self, url: str, take_screenshot: bool = False) -> Optional[RawPage]:
        self.calls.append(url)
        raw = self._raw
        if raw is not None and not raw.final_url:
            raw.final_url = url
        return raw

    def close(self) -> None:
        return None


_POLICY_HTML = (
    "<h1>Политика обработки персональных данных</h1><p>"
    + "текст " * 100
    + "</p>"
)


# ---------------------------------------------------------------------------
# 1. page_link: кандидаты из PageResult.links (html пустой)
# ---------------------------------------------------------------------------
def test_page_link_candidate_from_page_links_with_url_keyword():
    """URL с ключевым словом попадает в кандидаты из pages[].links (page_link)."""
    pages = [
        PageResult(
            url="https://x.ru/",
            links=[
                "https://x.ru/politika-konfidencialnosti",
                # Опаковый URL без ключевого слова — по одному URL не распознаётся.
                "https://x.ru/legal/doc-42",
                # Чужой домен — игнорируется.
                "https://other.com/politika-konfidencialnosti",
            ],
        )
    ]
    raw_pages = [RawPage(url="https://x.ru/", html="")]

    cands = document_finder.find_document_candidates(
        pages, raw_pages, "https://x.ru", None, [], _Settings()
    )
    by_url = {c.url: c for c in cands}

    cand = by_url.get("https://x.ru/politika-konfidencialnosti")
    assert cand is not None
    assert cand.source == "page_link"
    assert cand.doc_type == "privacy_policy"

    # Без текста якоря опаковый URL не классифицируется как документ.
    assert "https://x.ru/legal/doc-42" not in by_url
    # Чужой домен не попадает в кандидаты.
    assert "https://other.com/politika-konfidencialnosti" not in by_url


def test_opaque_url_found_via_anchor_text():
    """Опаковый URL (/legal/doc-42) находится по ТЕКСТУ якоря в html."""
    html = (
        "<html><body><footer>"
        '<a href="/legal/doc-42">Политика конфиденциальности</a>'
        "</footer></body></html>"
    )
    raw_pages = [
        RawPage(url="https://x.ru/", final_url="https://x.ru/", html=html, ok=True)
    ]

    cands = document_finder.find_document_candidates(
        [], raw_pages, "https://x.ru", None, [], _Settings()
    )
    by_url = {c.url: c for c in cands}

    cand = by_url.get("https://x.ru/legal/doc-42")
    assert cand is not None
    assert cand.source == "anchor"
    assert cand.doc_type == "privacy_policy"


def test_anchor_preferred_over_page_link_on_dedupe():
    """При дублировании URL кандидат-якорь (с текстом) побеждает page_link."""
    html = (
        "<html><body>"
        '<a href="/politika-konfidencialnosti">Политика конфиденциальности</a>'
        "</body></html>"
    )
    pages = [
        PageResult(url="https://x.ru/", links=["https://x.ru/politika-konfidencialnosti"])
    ]
    raw_pages = [
        RawPage(url="https://x.ru/", final_url="https://x.ru/", html=html, ok=True)
    ]

    cands = document_finder.find_document_candidates(
        pages, raw_pages, "https://x.ru", None, [], _Settings()
    )
    matched = [c for c in cands if c.url == "https://x.ru/politika-konfidencialnosti"]
    assert len(matched) == 1
    assert matched[0].source == "anchor"
    assert matched[0].anchor_text != ""


# ---------------------------------------------------------------------------
# 2. Инъекция fetcher в fetch_document (рендер с JS)
# ---------------------------------------------------------------------------
def test_fetch_document_renders_via_playwright_fetcher():
    """URL нет в raw_by_url -> документ рендерится через переданный fetcher."""
    url = "https://x.ru/legal/doc-42"
    raw = RawPage(
        url=url, final_url=url, ok=True, status_code=200, html=_POLICY_HTML
    )
    fake = _FakePlaywrightFetcher(raw)
    cand = DocCandidate(
        url=url,
        doc_type="privacy_policy",
        anchor_text="Политика конфиденциальности",
        source="anchor",
    )

    doc = document_fetcher.fetch_document(cand, {}, _Settings(), fetcher=fake)

    assert fake.calls == [url]
    assert doc.is_accessible is True
    assert doc.link_confirmed is True
    assert doc.discovered_by == "anchor"
    assert doc.text_length > 0
    assert "персональных данных" in doc.text.lower()


def test_fetch_document_backward_compatible_without_fetcher():
    """Старый вызов без fetcher работает как раньше (переиспользование raw_by_url)."""
    url = "https://x.ru/privacy"
    raw = RawPage(
        url=url, final_url=url, ok=True, status_code=200, html=_POLICY_HTML
    )
    cand = DocCandidate(url=url, doc_type="privacy_policy", source="anchor")

    doc = document_fetcher.fetch_document(cand, {url: raw}, _Settings())

    assert doc.is_accessible is True
    assert doc.link_confirmed is True
    assert doc.discovered_by == "anchor"


def test_fetch_document_pdf_candidate_skips_render(monkeypatch):
    """Бинарный формат (.pdf) не рендерится через fetcher — идёт по HTTP-пути."""
    url = "https://x.ru/files/politika.pdf"
    seen = {}

    def _fake_http_get(u, ua, timeout_s=20, max_bytes=0, allow_redirects=True):
        seen["url"] = u
        resp = utils.HttpResponse()
        resp.url = u
        resp.final_url = u
        resp.status_code = 200
        resp.ok = True
        resp.content = b"%PDF-1.4 fake"
        resp.content_type = "application/pdf"
        return resp

    monkeypatch.setattr(document_fetcher.utils, "http_get", _fake_http_get)

    fake = _FakePlaywrightFetcher(None)
    cand = DocCandidate(url=url, doc_type="privacy_policy", source="anchor")
    doc = document_fetcher.fetch_document(cand, {}, _Settings(), fetcher=fake)

    # Рендер не использовался, скачивание пошло через http_get.
    assert fake.calls == []
    assert seen.get("url") == url
    assert doc.format == "pdf"
    # Текст из поддельного PDF не извлечётся, но ссылка подтверждена (2xx).
    assert doc.link_confirmed is True


def test_fetch_document_discovered_by_guess_for_priority_path():
    """Кандидаты source='priority_path' помечаются как discovered_by='guess'."""
    url = "https://x.ru/privacy"
    raw = RawPage(url=url, final_url=url, ok=True, status_code=200, html=_POLICY_HTML)
    cand = DocCandidate(url=url, doc_type="privacy_policy", source="priority_path")

    doc = document_fetcher.fetch_document(cand, {url: raw}, _Settings())

    assert doc.discovered_by == "guess"


# ---------------------------------------------------------------------------
# 3. link_confirmed независим от извлечения текста
# ---------------------------------------------------------------------------
def test_link_confirmed_true_when_text_extraction_fails():
    """Страница открылась (2xx), но текст не извлёкся: найдено, но нужна ручная проверка."""
    url = "https://x.ru/legal/doc-42"
    raw = RawPage(
        url=url,
        final_url=url,
        ok=True,
        status_code=200,
        html="<div><script>var a = 1;</script></div>",
    )
    fake = _FakePlaywrightFetcher(raw)
    cand = DocCandidate(url=url, doc_type="privacy_policy", source="anchor")

    doc = document_fetcher.fetch_document(cand, {}, _Settings(), fetcher=fake)

    assert doc.is_accessible is False
    assert doc.link_confirmed is True
    assert doc.requires_manual_review is True


def test_link_confirmed_false_on_foreign_redirect():
    """Редирект на чужой домен не подтверждает существование документа сайта."""
    url = "https://x.ru/privacy"
    raw = RawPage(
        url=url,
        final_url="https://parked-domain.com/lander",
        ok=True,
        status_code=200,
        html=_POLICY_HTML,
    )
    cand = DocCandidate(url=url, doc_type="privacy_policy", source="anchor")

    doc = document_fetcher.fetch_document(cand, {url: raw}, _Settings())

    assert doc.link_confirmed is False


# ---------------------------------------------------------------------------
# 4. launch_error: причина недоступности Playwright сохраняется
# ---------------------------------------------------------------------------
def test_create_fetcher_captures_launch_error(monkeypatch):
    """Если Playwright не импортируется — HttpFetcher с непустым launch_error."""
    # None в sys.modules -> `from playwright.sync_api import ...` бросает ImportError.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    fetcher = browser.create_fetcher(_Settings())
    try:
        assert getattr(fetcher, "method", "") == "http"
        assert getattr(fetcher, "launch_error", "") != ""
        assert len(fetcher.launch_error) <= 300
    finally:
        try:
            fetcher.close()
        except Exception:
            pass


def test_http_fetcher_has_empty_launch_error_by_default():
    fetcher = browser.HttpFetcher(_Settings())
    assert fetcher.launch_error == ""


# ---------------------------------------------------------------------------
# 5. discover_urls: текстовый keyword-хит не отбрасывается по расширению
# ---------------------------------------------------------------------------
def test_discover_urls_keeps_pdf_link_with_keyword_text():
    html = (
        "<html><body>"
        '<a href="/files/politika.pdf">Политика конфиденциальности</a>'
        '<a href="/legal/rules.xml">Пользовательское соглашение</a>'
        '<a href="/feed.xml">RSS</a>'
        "</body></html>"
    )
    raw_home = RawPage(
        url="https://x.ru/", final_url="https://x.ru/", html=html, ok=True
    )

    urls = crawler.discover_urls("https://x.ru", raw_home, None, [], _Settings())

    # Ссылка с ключевым словом в тексте сохраняется даже для .pdf.
    assert "https://x.ru/files/politika.pdf" in urls
    # И даже для «не-HTML» расширения (.xml), если текст — про документ.
    assert "https://x.ru/legal/rules.xml" in urls
    # Без текстового ключевого слова «не-HTML» ссылки по-прежнему отбрасываются.
    assert "https://x.ru/feed.xml" not in urls
