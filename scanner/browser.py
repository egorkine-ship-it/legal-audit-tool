"""
Загрузка страниц сайта (fetcher).

Два способа получения страницы:
  * PlaywrightFetcher — headless-браузер (chromium). Исполняет JS, собирает
    сетевые запросы, cookies до взаимодействия, пытается раскрыть модальные окна
    с формами (без отправки чего-либо).
  * HttpFetcher — простой HTTP GET через utils.http_get + разбор bs4 (если есть).

`create_fetcher(settings)` пытается создать PlaywrightFetcher; если Playwright не
установлен или браузер не запускается — откатывается к HttpFetcher.

Все тяжёлые/опциональные импорты (playwright, bs4) выполняются внутри функций,
чтобы модуль импортировался даже без этих библиотек. Никакой fetch не бросает
исключение наружу — ошибки складываются в RawPage.errors.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from config.settings import Settings
from scanner import utils
from scanner.models import RawPage

# Короткий таймаут ожидания «сетевого затишья» после domcontentloaded (мс).
_NETWORKIDLE_TIMEOUT_MS = 3000
# Сколько модальных триггеров максимум кликаем на страницу.
_MAX_MODAL_CLICKS = 5


# ---------------------------------------------------------------------------
# Фабрика
# ---------------------------------------------------------------------------
def create_fetcher(settings: Settings):
    """
    Вернуть объект-fetcher с методами .fetch(url, take_screenshot=False) -> RawPage
    и .close() -> None, а также атрибутами .method in {"playwright", "http"} и
    .launch_error (почему Playwright недоступен; "" — если всё в порядке).

    Пытается поднять Playwright; при любой проблеме — HttpFetcher с заполненным
    launch_error, чтобы причина деградации попала в отчёт.
    """
    launch_error = ""
    try:
        fetcher = PlaywrightFetcher(settings)
        # Проверяем, что браузер реально запустился.
        if getattr(fetcher, "_browser", None) is not None:
            return fetcher
        # Не удалось — запоминаем причину, закрываем ресурсы и падаем в HTTP.
        launch_error = getattr(fetcher, "launch_error", "") or ""
        try:
            fetcher.close()
        except Exception:
            pass
    except Exception as exc:
        launch_error = repr(exc)[:300]
    http_fetcher = HttpFetcher(settings)
    try:
        if launch_error:
            http_fetcher.launch_error = launch_error
    except Exception:
        pass
    return http_fetcher


# ---------------------------------------------------------------------------
# Playwright
# ---------------------------------------------------------------------------
class PlaywrightFetcher:
    """
    Fetcher на базе Playwright sync API. Один браузер на весь жизненный цикл
    (создаётся в __init__), на каждый fetch — свежий контекст.
    """

    method = "playwright"
    launch_error = ""

    def __init__(self, settings: Settings) -> None:
        self.method = "playwright"
        self.settings = settings
        self._playwright = None
        self._browser = None
        # Причина недоступности браузера (для диагностики в отчёте).
        self.launch_error = ""
        # Импорт и запуск браузера — внутри try, чтобы отсутствие библиотеки или
        # невозможность запуска не роняли создание fetcher (create_fetcher
        # проверит self._browser и откатится к HTTP).
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception as exc:
            self._playwright = None
            self._browser = None
            self.launch_error = repr(exc)[:300]
            return
        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)
        except Exception as exc:
            # Не удалось запустить браузер — запоминаем причину и прибираем playwright.
            self.launch_error = repr(exc)[:300]
            self._browser = None
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    # -- публичный API --
    def fetch(self, url: str, take_screenshot: bool = False) -> RawPage:
        raw = RawPage(url=url, fetch_method="playwright")
        if self._browser is None:
            raw.errors.append("playwright: браузер недоступен")
            return raw

        context = None
        page = None
        network_requests: List[str] = []
        try:
            context = self._browser.new_context(user_agent=self.settings.user_agent)

            page = context.new_page()

            # Сбор сетевых запросов.
            def _on_request(request):  # pragma: no cover - зависит от рантайма
                try:
                    network_requests.append(request.url)
                except Exception:
                    pass

            try:
                page.on("request", _on_request)
            except Exception:
                pass

            # Навигация. Сохраняем ответ, чтобы узнать РЕАЛЬНЫЙ HTTP-статус:
            # браузер отрисует и страницу 404 (у неё есть HTML!), но считать её
            # «найденным документом» нельзя.
            nav_status = 0
            try:
                nav_response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.settings.page_timeout_ms,
                )
                if nav_response is not None:
                    try:
                        nav_status = int(nav_response.status or 0)
                    except Exception:
                        nav_status = 0
            except Exception as exc:
                raw.errors.append(f"goto: {exc}")

            # Best-effort ожидание затишья сети. НЕ виснем.
            try:
                page.wait_for_load_state(
                    "networkidle", timeout=_NETWORKIDLE_TIMEOUT_MS
                )
            except Exception:
                pass

            # Прокрутка вниз — подгружаем ленивый контент и ПОДВАЛ (footer с
            # ссылками на политику/оферту/согласие часто рендерится только после
            # скролла). Затем возвращаемся наверх.
            try:
                for _y in (0.35, 0.7, 1.0):
                    page.evaluate(
                        "y => window.scrollTo(0, document.body.scrollHeight * y)", _y
                    )
                    page.wait_for_timeout(350)
                page.evaluate("window.scrollTo(0, 0)")
            except Exception:
                pass

            # Best-effort ожидание гидрации подвала: ссылки на политику/оферту
            # часто дорисовываются JS уже после скролла. НЕ виснем.
            try:
                page.wait_for_selector(
                    "footer a, [class*='footer'] a", timeout=1500
                )
            except Exception:
                pass

            # --- Базовый сбор данных страницы (ДО клика по модалкам) ---
            final_url = url
            try:
                final_url = page.url or url
            except Exception:
                pass
            raw.final_url = final_url

            try:
                raw.title = page.title() or ""
            except Exception:
                raw.title = ""

            try:
                raw.html = page.content() or ""
            except Exception as exc:
                raw.errors.append(f"content: {exc}")
                raw.html = ""

            # Видимый текст.
            try:
                visible = page.evaluate("document.body ? document.body.innerText : ''")
                raw.visible_text = utils.clean_text(visible or "")
            except Exception:
                raw.visible_text = ""
            raw.text_content = raw.visible_text

            # Ссылки, скрипты, iframe, ресурсы.
            try:
                raw.links = self._collect_links(page, final_url)
            except Exception:
                raw.links = []
            try:
                raw.script_srcs = self._collect_attr(page, "script[src]", "src", final_url)
            except Exception:
                raw.script_srcs = []
            try:
                raw.iframe_srcs = self._collect_attr(page, "iframe[src]", "src", final_url)
            except Exception:
                raw.iframe_srcs = []
            try:
                raw.external_resources = self._collect_external_resources(page, final_url)
            except Exception:
                raw.external_resources = []

            # Cookies — ДО любого клика по модалкам.
            try:
                cookies = context.cookies()
                raw.cookies = self._normalize_cookies(cookies)
            except Exception:
                raw.cookies = []

            # Content-type: у HTML-страниц из Playwright точного заголовка нет,
            # но по факту это HTML-документ.
            raw.content_type = "text/html"

            # Скриншот (best-effort).
            if take_screenshot:
                try:
                    path = self._screenshot_path(final_url or url)
                    if path:
                        page.screenshot(path=path, full_page=False)
                        raw.screenshot_path = path
                except Exception as exc:
                    raw.errors.append(f"screenshot: {exc}")

            # --- Модальные окна (после сбора cookies/скриншота) ---
            try:
                raw.modal_htmls = self._open_modals(page)
            except Exception as exc:
                raw.errors.append(f"modals: {exc}")
                raw.modal_htmls = []

            # Сетевые запросы (уникальные, порядок сохраняем).
            raw.network_requests = utils.dedupe_keep_order(network_requests)

            # Реальный статус навигации (0 — если ответ недоступен, например
            # при SPA-переходах; тогда считаем страницу ок при наличии HTML).
            raw.status_code = nav_status if nav_status else (200 if raw.html else 0)
            raw.ok = bool(raw.html) and (nav_status == 0 or 200 <= nav_status < 400)
            raw.fetch_method = "playwright"
            return raw
        except Exception as exc:
            raw.errors.append(f"playwright.fetch: {exc}")
            raw.network_requests = utils.dedupe_keep_order(network_requests)
            return raw
        finally:
            # Контекст всегда закрываем; браузер оставляем для переиспользования.
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass

    def close(self) -> None:
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        finally:
            self._browser = None
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        finally:
            self._playwright = None

    # -- вспомогательные методы --
    def _collect_links(self, page, base: str) -> List[str]:
        hrefs = page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.getAttribute('href'))"
        )
        out: List[str] = []
        for h in hrefs or []:
            if not h:
                continue
            abs_u = utils.absolute_url(base, h)
            if abs_u:
                out.append(abs_u)
        return utils.dedupe_keep_order(out)

    def _collect_attr(self, page, selector: str, attr: str, base: str) -> List[str]:
        vals = page.eval_on_selector_all(
            selector,
            "(els, a) => els.map(e => e.getAttribute(a))",
            attr,
        )
        out: List[str] = []
        for v in vals or []:
            if not v:
                continue
            abs_u = utils.absolute_url(base, v)
            if abs_u:
                out.append(abs_u)
            elif v:
                out.append(v)
        return utils.dedupe_keep_order(out)

    def _collect_external_resources(self, page, base: str) -> List[str]:
        vals = page.eval_on_selector_all(
            "img[src], link[href], script[src]",
            "els => els.map(e => e.getAttribute('src') || e.getAttribute('href'))",
        )
        out: List[str] = []
        for v in vals or []:
            if not v:
                continue
            abs_u = utils.absolute_url(base, v)
            if abs_u:
                out.append(abs_u)
        return utils.dedupe_keep_order(out)

    def _normalize_cookies(self, cookies) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for c in cookies or []:
            try:
                out.append(
                    {
                        "name": c.get("name", ""),
                        "value": c.get("value", ""),
                        "domain": c.get("domain", ""),
                    }
                )
            except Exception:
                continue
        return out

    def _open_modals(self, page) -> List[str]:
        """
        Кликаем по элементам-триггерам модалок (без submit / покупки), собираем
        HTML открывшихся контейнеров. Каждый клик обёрнут в try/except.
        """
        try:
            from scanner import modal_detector
        except Exception:
            return []

        modal_htmls: List[str] = []
        try:
            handles = page.query_selector_all("button, a, [role=button]")
        except Exception:
            return []

        clicks = 0
        for handle in handles or []:
            if clicks >= _MAX_MODAL_CLICKS:
                break
            try:
                text = (handle.inner_text() or "").strip()
            except Exception:
                text = ""
            if not text:
                continue
            try:
                if not modal_detector.is_modal_trigger(text):
                    continue
            except Exception:
                continue

            # Никогда не кликаем submit и «оплатить/купить/оформить заказ».
            try:
                el_type = (handle.get_attribute("type") or "").lower()
            except Exception:
                el_type = ""
            if el_type == "submit":
                continue
            # Единый список «опасных» действий держим в modal_detector, чтобы не
            # расходился с логикой is_modal_trigger (там он тоже исключается).
            try:
                if modal_detector.is_unsafe_click(text):
                    continue
            except Exception:
                continue

            # Пропускаем элементы внутри <form>, чтобы не отправить форму.
            try:
                inside_form = handle.evaluate("e => !!e.closest('form')")
            except Exception:
                inside_form = False
            if inside_form:
                continue

            # Кликаем.
            try:
                handle.click(timeout=2000)
            except Exception:
                continue
            clicks += 1

            # Ждём появления модалки.
            try:
                page.wait_for_timeout(1200)
            except Exception:
                pass

            # Захватываем HTML вероятного контейнера модалки.
            try:
                snippet = self._capture_modal_html(page)
                if snippet:
                    modal_htmls.append(snippet)
            except Exception:
                pass

            # Пытаемся закрыть модалку, чтобы не мешала следующим кликам.
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                self._click_close_button(page)
            except Exception:
                pass
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass

        return modal_htmls

    def _capture_modal_html(self, page) -> str:
        """Найти видимый диалог/модалку (или любой всплывший контейнер с полями)
        и вернуть её outerHTML.

        Стратегия:
          1. Идём по известным селекторам модалок/попапов (dialog/.modal/.popup…)
             и берём первый ВИДИМЫЙ контейнер с непустым outerHTML — но только
             если в нём есть поля ввода (input/textarea/select). Пустая «шапка»
             модалки без полей нам не интересна.
          2. Фолбэк: на JS-сайтах форма нередко рендерится вне .modal (просто
             появляется видимый блок с полями). Ищем самый «свежий» видимый
             контейнер с полями ввода, НЕ обёрнутый в существующий <form> с
             submit'ом мы не трогаем — нам нужен только HTML для анализа.
        """
        selectors = (
            "[role=dialog]",
            ".modal.show",
            ".modal.in",
            ".modal[style*='display: block']",
            ".modal",
            ".popup",
            ".fancybox-container",
            "[class*='modal']",
            "[class*='popup']",
            "[class*='overlay']",
            "[class*='drawer']",
            "[class*='fancybox']",
            "[class*='mfp']",
            "[data-modal]",
        )
        for sel in selectors:
            try:
                handles = page.query_selector_all(sel)
            except Exception:
                handles = None
            for handle in handles or []:
                if handle is None:
                    continue
                try:
                    if not handle.is_visible():
                        continue
                except Exception:
                    # Неизвестная видимость (устаревший/откреплённый узел) —
                    # считаем невидимым, чтобы не принять скрытый контейнер за
                    # открытую модалку.
                    continue
                # Контейнер модалки интересен только если в нём есть поля ввода.
                try:
                    has_input = handle.evaluate(
                        "e => !!e.querySelector('input, textarea, select')"
                    )
                except Exception:
                    has_input = False
                if not has_input:
                    continue
                try:
                    html = handle.evaluate("e => e.outerHTML")
                except Exception:
                    html = ""
                if html:
                    return html

        # --- Фолбэк: любой видимый контейнер с полями, всплывший после клика. ---
        try:
            return self._capture_visible_input_container(page)
        except Exception:
            return ""

    def _capture_visible_input_container(self, page) -> str:
        """Фолбэк-захват: найти видимый контейнер с полями ввода вне .modal.

        Многие JS-сайты открывают форму заявки/звонка не в .modal, а просто
        показывают ранее скрытый блок с input'ами. Берём наиболее компактный
        видимый контейнер (ближайший общий предок полей), чтобы не утащить всю
        страницу. Возвращает outerHTML или "" — никогда не бросает.
        """
        script = """
        () => {
          const isVisible = (el) => {
            if (!el) return false;
            const st = window.getComputedStyle(el);
            if (st.display === 'none' || st.visibility === 'hidden') return false;
            if (parseFloat(st.opacity || '1') === 0) return false;
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
          };
          // Кандидаты — видимые поля ввода, не скрытые/технические.
          const inputs = Array.from(
            document.querySelectorAll('input, textarea, select')
          ).filter((el) => {
            const t = (el.getAttribute('type') || '').toLowerCase();
            if (el.tagName === 'INPUT' &&
                ['hidden','submit','button','image','reset'].includes(t)) {
              return false;
            }
            return isVisible(el);
          });
          if (!inputs.length) return '';
          // Ищем компактный контейнер, охватывающий поле и кнопку рядом.
          for (const inp of inputs) {
            let el = inp;
            for (let i = 0; i < 6 && el; i++) {
              el = el.parentElement;
              if (!el) break;
              if (!isVisible(el)) continue;
              const hasBtn = el.querySelector(
                "button, input[type=submit], input[type=button], [class*=btn], [class*=button]"
              );
              const nInputs = el.querySelectorAll('input, textarea, select').length;
              // Контейнер с полем и кнопкой — вероятная форма/модалка.
              if (hasBtn && nInputs >= 1) {
                return el.outerHTML;
              }
            }
          }
          return '';
        }
        """
        try:
            html = page.evaluate(script)
        except Exception:
            html = ""
        return html or ""

    def _click_close_button(self, page) -> None:
        close_selectors = (
            "[aria-label*='close' i]",
            "[aria-label*='закр' i]",
            ".modal .close",
            ".modal__close",
            ".popup__close",
            "[class*='close']",
            "button[data-dismiss='modal']",
        )
        for sel in close_selectors:
            try:
                handle = page.query_selector(sel)
            except Exception:
                handle = None
            if handle is None:
                continue
            try:
                if handle.is_visible():
                    handle.click(timeout=1000)
                    return
            except Exception:
                continue

    def _screenshot_path(self, url: str) -> str:
        try:
            base_exports = os.path.dirname(self.settings.pdf_dir.rstrip("/"))
            screenshots_dir = os.path.join(base_exports, "screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)
        except Exception:
            try:
                screenshots_dir = self.settings.exports_dir
                os.makedirs(screenshots_dir, exist_ok=True)
            except Exception:
                return ""
        name = _slug_for_file(url) or "page"
        return os.path.join(screenshots_dir, f"{name}.png")


# ---------------------------------------------------------------------------
# HTTP fallback
# ---------------------------------------------------------------------------
class HttpFetcher:
    """Простой fetcher поверх utils.http_get. Без JS, модалок и скриншотов."""

    method = "http"
    launch_error = ""

    def __init__(self, settings: Settings) -> None:
        self.method = "http"
        self.settings = settings
        # Причина, по которой Playwright оказался недоступен (заполняет
        # create_fetcher при фолбэке); "" — если HTTP выбран осознанно.
        self.launch_error = ""

    def fetch(self, url: str, take_screenshot: bool = False) -> RawPage:
        raw = RawPage(url=url, fetch_method="http")
        try:
            resp = utils.http_get(
                url,
                self.settings.user_agent,
                self.settings.request_timeout_s,
                self.settings.max_download_bytes,
            )
        except Exception as exc:
            raw.errors.append(f"http_get: {exc}")
            return raw

        raw.status_code = resp.status_code
        raw.final_url = resp.final_url or url
        raw.ok = bool(resp.ok)
        raw.content_type = resp.content_type or ""
        raw.html = resp.text or ""
        if resp.error:
            raw.errors.append(resp.error)

        base = raw.final_url or url

        # Парсинг bs4 — под guard: если bs4 нет, работаем с тем, что есть.
        soup = None
        try:
            from bs4 import BeautifulSoup  # type: ignore

            if raw.html:
                soup = BeautifulSoup(raw.html, "html.parser")
        except Exception:
            soup = None

        if soup is not None:
            try:
                if soup.title and soup.title.string:
                    raw.title = utils.clean_text(soup.title.string)
            except Exception:
                pass
            try:
                raw.visible_text = utils.clean_text(soup.get_text(" "))
            except Exception:
                raw.visible_text = ""
            raw.text_content = raw.visible_text

            raw.links = self._links_from_soup(soup, base)
            raw.script_srcs = self._attr_from_soup(soup, "script", "src", base)
            raw.iframe_srcs = self._attr_from_soup(soup, "iframe", "src", base)
            raw.external_resources = self._external_resources_from_soup(soup, base)
        else:
            # bs4 отсутствует — минимальный сбор.
            raw.visible_text = utils.clean_text(raw.html) if raw.html else ""
            raw.text_content = raw.visible_text
            raw.links = []
            raw.script_srcs = []
            raw.iframe_srcs = []
            raw.external_resources = []

        # network_requests ≈ script + iframe + img srcs.
        raw.network_requests = utils.dedupe_keep_order(
            list(raw.script_srcs) + list(raw.iframe_srcs) + list(raw.external_resources)
        )

        # Cookies из заголовков ответа (Set-Cookie).
        raw.cookies = self._cookies_from_headers(resp, base)

        return raw

    def close(self) -> None:
        # Нет ресурсов для закрытия.
        return None

    # -- вспомогательные методы --
    def _links_from_soup(self, soup, base: str) -> List[str]:
        out: List[str] = []
        try:
            for a in soup.find_all("a", href=True):
                abs_u = utils.absolute_url(base, a.get("href", ""))
                if abs_u:
                    out.append(abs_u)
        except Exception:
            pass
        return utils.dedupe_keep_order(out)

    def _attr_from_soup(self, soup, tag: str, attr: str, base: str) -> List[str]:
        out: List[str] = []
        try:
            for el in soup.find_all(tag):
                val = el.get(attr)
                if not val:
                    continue
                abs_u = utils.absolute_url(base, val)
                if abs_u:
                    out.append(abs_u)
                else:
                    out.append(val)
        except Exception:
            pass
        return utils.dedupe_keep_order(out)

    def _external_resources_from_soup(self, soup, base: str) -> List[str]:
        out: List[str] = []
        try:
            for el in soup.find_all(["img", "link", "script"]):
                val = el.get("src") or el.get("href")
                if not val:
                    continue
                abs_u = utils.absolute_url(base, val)
                if abs_u:
                    out.append(abs_u)
        except Exception:
            pass
        return utils.dedupe_keep_order(out)

    def _cookies_from_headers(self, resp, base: str) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            headers = resp.headers or {}
        except Exception:
            headers = {}
        # http_get складывает заголовки в dict (последний Set-Cookie перезапишет
        # предыдущие). Разбираем то, что есть.
        raw_cookie = ""
        try:
            raw_cookie = headers.get("set-cookie", "") or ""
        except Exception:
            raw_cookie = ""
        if not raw_cookie:
            return out
        domain = utils.get_host(base)
        # Может содержать несколько cookie, разделённых переносами.
        for part in raw_cookie.split("\n"):
            part = part.strip()
            if not part:
                continue
            cookie = self._parse_set_cookie(part, domain)
            if cookie:
                out.append(cookie)
        return out

    def _parse_set_cookie(self, header_value: str, default_domain: str) -> Optional[Dict[str, Any]]:
        try:
            segments = header_value.split(";")
            if not segments:
                return None
            first = segments[0].strip()
            if "=" not in first:
                return None
            name, value = first.split("=", 1)
            name = name.strip()
            value = value.strip()
            domain = default_domain
            for seg in segments[1:]:
                seg = seg.strip()
                if seg.lower().startswith("domain="):
                    domain = seg.split("=", 1)[1].strip().lstrip(".")
            if not name:
                return None
            return {"name": name, "value": value, "domain": domain}
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Утилиты модуля
# ---------------------------------------------------------------------------
def _slug_for_file(url: str) -> str:
    import re

    host = utils.get_host(url) or "page"
    path = ""
    try:
        from urllib.parse import urlparse

        path = urlparse(url).path or ""
    except Exception:
        path = ""
    base = (host + path).strip("/")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    return slug[:80] if slug else "page"
