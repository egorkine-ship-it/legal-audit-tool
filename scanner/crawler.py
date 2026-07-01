"""
Определение стартовой страницы и списка публичных URL для проверки.

Модуль не выполняет тяжёлой работы сам — он опирается на объект `fetcher`
(из scanner.browser), robots/sitemap и ссылки главной страницы, чтобы
построить приоритетный, дедуплицированный список страниц.

Публичный API:
    PRIORITY_PATHS: List[str]
    LINK_KEYWORDS: List[str]
    resolve_start_url(raw_url, fetcher, settings) -> Tuple[str, RawPage]
    discover_urls(base_url, raw_home, robots, sitemap_urls, settings) -> List[str]

Ни одна публичная функция не бросает исключения наружу.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urlunparse

from scanner import utils
from scanner.models import RawPage


# Приоритетные пути (типовые расположения контактов/политик/оферты и т.п.).
# Порядок значим: важные страницы проверяются раньше.
PRIORITY_PATHS: List[str] = [
    "/",
    "/contacts",
    "/contact",
    "/kontakty",
    "/privacy",
    "/privacy-policy",
    "/policy",
    "/personal-data",
    "/personal-data-policy",
    "/politika",
    "/politika-konfidentsialnosti",
    "/privacy.html",
    "/soglasie",
    "/consent",
    "/agreement",
    "/user-agreement",
    "/terms",
    "/oferta",
    "/offer",
    "/publichnaya-oferta",
    "/cookie",
    "/cookies",
    "/cookie-policy",
    "/obrabotka-personalnyh-dannyh",
]

# Ключевые слова, повышающие приоритет ссылки (в URL или тексте ссылки).
LINK_KEYWORDS: List[str] = [
    "политика",
    "конфиденциальность",
    "персональные данные",
    "privacy",
    "policy",
    "согласие",
    "consent",
    "cookie",
    "cookies",
    "оферта",
    "oferta",
    "offer",
    "договор",
    "agreement",
    "terms",
    "пользовательское соглашение",
    "контакты",
    "заявка",
    "запись",
    "консультация",
    "рассылка",
    "подписка",
]

# Максимум «прочих» внутренних ссылок с главной, добавляемых сверх приоритетных.
_MAX_OTHER_INTERNAL = 15


def _base_from_url(url: str) -> str:
    """scheme://host из URL (без пути/квери)."""
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return urlunparse((p.scheme, p.netloc, "", "", "", ""))
    except Exception:
        pass
    return ""


def resolve_start_url(
    raw_url: str,
    fetcher: Any,
    settings: Any,
) -> Tuple[str, RawPage]:
    """
    Нормализовать входной URL, загрузить главную страницу и вернуть
    (base_url, raw_home). При недоступности https пробуем http.

    `raw_home` всегда содержит поле `.errors`; при полном провале возвращается
    объект RawPage с ok=False и заполненным списком ошибок (никаких исключений).
    """
    normalized = ""
    try:
        normalized = utils.normalize_url(raw_url or "")
    except Exception as exc:
        normalized = (raw_url or "").strip()
    if not normalized:
        # Не удалось даже нормализовать — возвращаем пустую страницу-заглушку.
        raw = RawPage(url=raw_url or "", errors=["resolve_start_url: пустой URL"])
        return ("", raw)

    take_screenshot = False
    try:
        take_screenshot = bool(getattr(settings, "enable_screenshots", False))
    except Exception:
        take_screenshot = False

    raw_home: Optional[RawPage] = None
    errors: List[str] = []

    # 1) Пробуем нормализованный (обычно https) вариант.
    try:
        raw_home = fetcher.fetch(normalized, take_screenshot=take_screenshot)
    except Exception as exc:
        errors.append(f"fetch({normalized}): {exc}")
        raw_home = None

    def _is_good(page: Optional[RawPage]) -> bool:
        if page is None:
            return False
        try:
            return bool(page.ok) or bool(page.html)
        except Exception:
            return False

    # 2) Если не получилось — пробуем http-фолбэк.
    if not _is_good(raw_home):
        http_url = ""
        try:
            http_url = utils.to_http(normalized)
        except Exception:
            http_url = ""
        if http_url and http_url != normalized:
            try:
                alt = fetcher.fetch(http_url, take_screenshot=take_screenshot)
            except Exception as exc:
                errors.append(f"fetch({http_url}): {exc}")
                alt = None
            if _is_good(alt):
                raw_home = alt
            elif raw_home is None:
                raw_home = alt

    # 3) Если так и нет объекта — создаём безопасную заглушку.
    if raw_home is None:
        raw_home = RawPage(url=normalized)

    # Аккумулируем накопленные ошибки в raw_home.errors.
    try:
        if errors:
            raw_home.errors = list(raw_home.errors) + errors
    except Exception:
        pass

    # base_url = scheme://host итогового (или входного) URL.
    base_url = ""
    try:
        final = raw_home.final_url or raw_home.url or normalized
        base_url = _base_from_url(final)
    except Exception:
        base_url = ""
    if not base_url:
        base_url = _base_from_url(normalized) or normalized

    # Гарантируем, что у raw_home заполнен url.
    try:
        if not raw_home.url:
            raw_home.url = normalized
    except Exception:
        pass

    return (base_url, raw_home)


def _links_text_map(raw_home: RawPage) -> Dict[str, str]:
    """
    Заглушка для сопоставления ссылки с текстом. RawPage хранит только URL
    ссылок, поэтому для поиска ключевых слов используем сам URL. Возвращаем
    словарь url->url (текст недоступен) — вызывающий код ищет ключевые слова
    в URL.
    """
    return {}


def discover_urls(
    base_url: str,
    raw_home: RawPage,
    robots: Any,
    sitemap_urls: Any,
    settings: Any,
) -> List[str]:
    """
    Построить упорядоченный дедуплицированный список URL для проверки.

    Порядок:
      1) PRIORITY_PATHS как абсолютные URL на base_url;
      2) внутренние ссылки главной (тот же зарег. домен, похоже на HTML),
         в URL которых встречается любой LINK_KEYWORD;
      3) до ~15 прочих внутренних HTML-ссылок главной;
      4) sitemap_urls, в URL которых встречается любой LINK_KEYWORD.

    Исключаем точный base_url. Итог ограничиваем settings.max_pages.
    """
    ordered: List[str] = []
    seen = set()

    try:
        raw_max = getattr(settings, "max_pages", 20)
        max_pages = int(raw_max) if raw_max is not None else 20
    except Exception:
        max_pages = 20
    if max_pages < 1:
        max_pages = 1

    base = (base_url or "").strip()
    base_stripped = ""
    try:
        base_stripped = utils.strip_fragment(base).rstrip("/") if base else ""
    except Exception:
        base_stripped = base.rstrip("/") if base else ""

    def _add(url: str) -> None:
        if not url:
            return
        try:
            clean = utils.strip_fragment(url)
        except Exception:
            clean = url
        if not clean:
            return
        # Исключаем точный base_url (главная уже загружена оркестратором).
        key = clean
        cmp_key = ""
        try:
            cmp_key = clean.rstrip("/")
        except Exception:
            cmp_key = clean
        if base_stripped and cmp_key == base_stripped:
            return
        if key in seen:
            return
        seen.add(key)
        ordered.append(key)

    # (1) PRIORITY_PATHS -> абсолютные URL на base_url.
    if base:
        for path in PRIORITY_PATHS:
            if path == "/":
                # «/» — это и есть главная (base_url); пропускаем.
                continue
            try:
                abs_url = utils.absolute_url(base, path)
            except Exception:
                abs_url = ""
            if abs_url:
                _add(abs_url)

    # Собираем внутренние HTML-ссылки главной страницы.
    home_links: List[str] = []
    try:
        home_links = list(raw_home.links or [])
    except Exception:
        home_links = []

    internal_html_links: List[str] = []
    for link in home_links:
        if not link or not isinstance(link, str):
            continue
        try:
            if not utils.same_registered_domain(link, base or link):
                continue
            if not utils.is_probably_html_url(link):
                continue
        except Exception:
            continue
        internal_html_links.append(link)

    # (2) Внутренние ссылки, содержащие ключевые слова в URL.
    keyword_links: List[str] = []
    other_links: List[str] = []
    for link in internal_html_links:
        try:
            has_kw = utils.contains_any(link, LINK_KEYWORDS)
        except Exception:
            has_kw = False
        if has_kw:
            keyword_links.append(link)
        else:
            other_links.append(link)

    for link in keyword_links:
        _add(link)

    # (3) До ~15 прочих внутренних HTML-ссылок.
    added_other = 0
    for link in other_links:
        if added_other >= _MAX_OTHER_INTERNAL:
            break
        before = len(ordered)
        _add(link)
        if len(ordered) > before:
            added_other += 1

    # (4) sitemap_urls, содержащие ключевые слова.
    sm_list: List[str] = []
    try:
        sm_list = list(sitemap_urls or [])
    except Exception:
        sm_list = []
    for url in sm_list:
        if not url or not isinstance(url, str):
            continue
        try:
            if not utils.contains_any(url, LINK_KEYWORDS):
                continue
        except Exception:
            continue
        _add(url)

    # Ограничиваем общий размер.
    return ordered[:max_pages]
