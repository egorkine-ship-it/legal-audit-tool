"""
E2E acceptance-тесты полного пайплайна сканирования на локальных fixture-сайтах.

Эти тесты не ходят во внешнюю сеть, не используют LLM и не требуют установленного
Chromium: Playwright-fetcher принудительно заменяется на HTTP-fetcher. Цель —
проверить именно склейку оркестратора: discovery -> page/form/document analysis
-> rule engine -> core checks.
"""
from __future__ import annotations

import contextlib
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Tuple

from config.settings import Settings
from scanner import browser, document_finder
from scanner.models import ScanInput
from scanner.orchestrator import run_scan


RouteValue = Tuple[int, str, Dict[str, str]]


class _FixtureHandler(BaseHTTPRequestHandler):
    routes: Dict[str, RouteValue] = {}
    default_status: int = 404
    default_body: str = "<!doctype html><title>404</title><h1>404</h1>"
    default_headers: Dict[str, str] = {"Content-Type": "text/html; charset=utf-8"}

    def do_GET(self):  # noqa: N802 - stdlib API
        path = self.path.split("?", 1)[0]
        status, body, headers = self.routes.get(
            path,
            (self.default_status, self.default_body, self.default_headers),
        )
        raw = body.encode("utf-8")
        self.send_response(status)
        merged = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
        for key, value in merged.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):  # keep pytest output clean
        return None


@contextlib.contextmanager
def _fixture_server(
    routes: Dict[str, RouteValue],
    default_status: int = 404,
    default_body: str = "<!doctype html><title>404</title><h1>404</h1>",
):
    class Handler(_FixtureHandler):
        pass

    Handler.routes = routes
    Handler.default_status = default_status
    Handler.default_body = default_body

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield "http://{}:{}".format(host, port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _settings(tmp_path, max_pages: int = 10) -> Settings:
    return Settings(
        enable_llm=False,
        enable_geoip=False,
        enable_screenshots=False,
        max_pages=max_pages,
        delay_between_pages_s=0,
        request_timeout_s=5,
        page_timeout_ms=5000,
        db_path=str(tmp_path / "database.sqlite"),
        exports_dir=str(tmp_path / "exports"),
        pdf_dir=str(tmp_path / "exports" / "pdf"),
    )


def _force_http_fetcher(monkeypatch):
    monkeypatch.setattr(
        browser,
        "create_fetcher",
        lambda settings: browser.HttpFetcher(settings),
    )


def _scan(base_url: str, settings: Settings) -> object:
    return run_scan(
        ScanInput(
            company_name="Fixture Ltd",
            site_url=base_url,
            industry="auto",
            max_pages=settings.max_pages,
            use_llm=False,
            use_agent=False,
            create_pdf=False,
        ),
        settings,
    )


def _risk_ids(result) -> set:
    return {getattr(r, "id", "") for r in getattr(result, "risks", [])}


def _present_docs(result, doc_type: str):
    return [
        d for d in getattr(result, "documents", [])
        if getattr(d, "doc_type", "") == doc_type and document_finder.document_is_present(d)
    ]


def test_full_scan_finds_footer_documents_forms_widgets_and_core_checks(tmp_path, monkeypatch):
    """Happy path: footer legal links + contact form + embedded form widget."""
    _force_http_fetcher(monkeypatch)
    home = """
    <!doctype html>
    <html><head>
      <title>Fixture Legal Site</title>
      <script src="https://www.googletagmanager.com/gtm.js?id=GTM-TEST"></script>
      <script src="https://forms.tildacdn.com/js/tilda-forms-1.0.min.js"></script>
    </head>
    <body>
      <h1>Услуги компании Fixture</h1>
      <div class="cookie-banner">Мы используем cookie <button>Принять</button></div>
      <a href="/contacts">Контакты и заявка</a>
      <footer>
        <a href="/legal/privacy">Политика обработки персональных данных</a>
        <a href="/legal/consent">Согласие на обработку персональных данных</a>
        <a href="/legal/cookie">Политика cookie</a>
        <a href="/legal/offer">Публичная оферта</a>
      </footer>
    </body></html>
    """
    contacts = """
    <!doctype html><html><body>
      <h1>Контакты</h1>
      <form action="/lead" method="post">
        <label>Ваше имя <input name="name"></label>
        <label>Телефон <input name="phone" type="tel"></label>
        <label>Email <input name="email" type="email"></label>
        <label><input type="checkbox" name="pd">
          Даю согласие на обработку персональных данных и ознакомлен с
          <a href="/legal/privacy">политикой обработки персональных данных</a>
        </label>
        <button type="submit">Отправить заявку</button>
      </form>
    </body></html>
    """
    privacy = """
    <!doctype html><html><body>
      <h1>Политика обработки персональных данных</h1>
      <p>Оператором является ООО «Fixture», ИНН 7701234567, ОГРН 1027700132195.</p>
      <p>Адрес оператора: г. Москва, ул. Тестовая, д. 1.</p>
      <p>Контакт по вопросам обработки персональных данных: privacy@example.ru.</p>
      <p>Цели обработки: обработка заявок, обратная связь, заключение и исполнение договора,
      аналитика сайта и улучшение сайта.</p>
      <p>Категории субъектов: пользователи сайта, клиенты, представители контрагентов.</p>
      <p>Перечень персональных данных: имя, телефон, email, IP-адрес, cookie ID.</p>
      <p>Действия с персональными данными: сбор, запись, систематизация, хранение,
      использование, передача, удаление, уничтожение.</p>
      <p>Срок обработки: до достижения целей обработки или до отзыва согласия.</p>
      <p>Отзыв согласия направляется на privacy@example.ru.</p>
      <p>Используются cookies, Яндекс.Метрика и Google Tag Manager. Трансграничная
      передача может осуществляться при использовании иностранных сервисов.</p>
    </body></html>
    """
    consent = """
    <!doctype html><html><body>
      <h1>Согласие на обработку персональных данных</h1>
      <p>Оператор ООО «Fixture» обрабатывает имя, телефон и email для обработки заявки.
      Согласие действует до достижения цели или до отзыва на privacy@example.ru.</p>
    </body></html>
    """
    cookie = """
    <!doctype html><html><body>
      <h1>Политика cookie</h1>
      <p>Мы используем необходимые, аналитические и маркетинговые cookies для статистики
      посещений и улучшения сайта. Провайдеры: Google Tag Manager, Яндекс.Метрика.</p>
    </body></html>
    """
    offer = """
    <!doctype html><html><body>
      <h1>Публичная оферта</h1>
      <p>Исполнитель ООО «Fixture» оказывает услуги по заявкам пользователей.</p>
    </body></html>
    """
    routes = {
        "/": (200, home, {}),
        "/contacts": (200, contacts, {}),
        "/legal/privacy": (200, privacy, {}),
        "/legal/consent": (200, consent, {}),
        "/legal/cookie": (200, cookie, {}),
        "/legal/offer": (200, offer, {}),
        "/robots.txt": (404, "", {"Content-Type": "text/plain; charset=utf-8"}),
        "/sitemap.xml": (404, "", {"Content-Type": "application/xml; charset=utf-8"}),
    }

    with _fixture_server(routes) as base_url:
        result = _scan(base_url, _settings(tmp_path))

    assert result.pages_checked >= 3
    assert result.fetch_method == "http"
    assert len(result.core_checklist) == 21
    assert _present_docs(result, "privacy_policy")
    assert _present_docs(result, "consent")
    assert _present_docs(result, "cookie_policy")
    assert any(f.potentially_personal_data_form for f in result.forms)
    assert any(f.source == "widget" for f in result.forms)
    assert result.cookie_banner_found is True
    assert any(t.provider_name == "Google Tag Manager" for t in result.trackers)
    assert result.llm_used is False
    assert result.agent_audit_used is False
    # HTTP fixture intentionally has no TLS; the technical risk should be explicit.
    assert "R017_NO_HTTPS" in _risk_ids(result)


def test_soft_404_catch_all_does_not_count_guessed_privacy_policy(tmp_path, monkeypatch):
    """Catch-all 200 shell must not suppress "privacy policy missing" risk."""
    _force_http_fetcher(monkeypatch)
    home = """
    <!doctype html><html><body>
      <h1>Каталог услуг</h1>
      <form>
        <input name="phone" type="tel" placeholder="Телефон">
        <button type="submit">Отправить заявку</button>
      </form>
    </body></html>
    """
    shell = """
    <!doctype html><html><body>
      <h1>Каталог услуг</h1>
      <p>Раздел находится в разработке. Здесь опубликован обычный каталог услуг.</p>
    </body></html>
    """
    routes = {
        "/": (200, home, {}),
        "/robots.txt": (404, "", {"Content-Type": "text/plain; charset=utf-8"}),
        "/sitemap.xml": (404, "", {"Content-Type": "application/xml; charset=utf-8"}),
    }

    with _fixture_server(routes, default_status=200, default_body=shell) as base_url:
        result = _scan(base_url, _settings(tmp_path))

    assert any(f.potentially_personal_data_form for f in result.forms)
    assert not _present_docs(result, "privacy_policy")
    assert "R001_NO_PRIVACY_POLICY" in _risk_ids(result)
    assert any(
        c.id == "PP_001" and c.status == "risk"
        for c in getattr(result, "core_checklist", [])
    )


def test_unavailable_site_returns_manual_review_result_without_crashing(tmp_path, monkeypatch):
    """Network failure should produce a partial result, not an exception."""
    _force_http_fetcher(monkeypatch)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    host, port = sock.getsockname()
    sock.close()

    result = _scan("http://{}:{}".format(host, port), _settings(tmp_path, max_pages=3))

    assert result.requires_manual_review is True
    assert result.risk_level == "unknown"
    assert result.errors
    assert "Сайт недоступен" in " ".join(result.errors)
