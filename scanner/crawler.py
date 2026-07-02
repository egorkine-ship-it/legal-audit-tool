"""
Определение стартовой страницы и списка публичных URL для проверки.

Модуль не выполняет тяжёлой работы сам — он опирается на объект `fetcher`
(из scanner.browser), robots/sitemap и ссылки главной страницы, чтобы
построить приоритетный, дедуплицированный список страниц.

Публичный API:
    PRIORITY_PATHS: List[str]
    LINK_KEYWORDS: List[str]
    FORM_PAGE_KEYWORDS: List[str]
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

# Ключевые слова страниц с формами/контактами (в URL или тексте ссылки).
# Только такие «прочие» внутренние ссылки нам интересны: там живут формы
# записи/заявки/обратной связи, где собираются персональные данные. Каталоги,
# блоги и карточки товаров не собирают ПДн и в целевой сбор не попадают.
FORM_PAGE_KEYWORDS: List[str] = [
    "контакт",
    "contact",
    "kontakty",
    "запис",
    "заявк",
    "консультац",
    "услуг",
    "service",
    "обратная связь",
    "feedback",
    "звонок",
    "callback",
    "подписк",
    "бронир",
    "заказать",
]

# Максимум страниц с формами/контактами, добавляемых сверх документных ссылок.
# Раньше здесь брались до 15 ЛЮБЫХ внутренних ссылок — теперь берём только
# релевантные формам/контактам и ограничиваем их числом.
_MAX_FORM_PAGES = 4
# Максимум ДОГАДОК по типовым путям (/privacy, /oferta, ...). Они добавляются
# ПОСЛЕДНИМИ и только чтобы добить бюджет, если реальных ссылок мало — иначе
# 404-догадки съедают весь лимит и до реальных документов дело не доходит.
_MAX_GUESS_PATHS = 8


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


def _homepage_anchors(raw_home: RawPage, base: str) -> List[Tuple[str, str]]:
    """
    Вернуть список (абсолютный_url, текст_ссылки) из главной страницы.

    Текст ссылки берём из HTML (важно: в подвале ссылка часто выглядит как
    <a href="/legal/doc-42">Политика обработки персональных данных</a> — в URL
    ключевого слова нет, а в тексте есть). Если bs4 недоступен — откатываемся к
    raw_home.links (только URL, без текста).
    """
    out: List[Tuple[str, str]] = []
    seen = set()

    def _push(url: str, text: str) -> None:
        if not url:
            return
        try:
            clean = utils.strip_fragment(url)
        except Exception:
            clean = url
        if not clean or clean in seen:
            return
        seen.add(clean)
        out.append((clean, text or ""))

    base_for_abs = ""
    try:
        base_for_abs = getattr(raw_home, "final_url", "") or getattr(raw_home, "url", "") or base
    except Exception:
        base_for_abs = base

    html = ""
    try:
        html = getattr(raw_home, "html", "") or ""
    except Exception:
        html = ""

    if html:
        try:
            from bs4 import BeautifulSoup  # type: ignore

            soup = BeautifulSoup(html, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a.get("href", "") or ""
                try:
                    text = a.get_text(" ", strip=True)
                except Exception:
                    text = ""
                try:
                    abs_u = utils.absolute_url(base_for_abs, href)
                except Exception:
                    abs_u = ""
                if abs_u:
                    _push(abs_u, utils.clean_text(text))
        except Exception:
            pass

    # Добираем ссылки, которые fetcher уже абсолютизировал (текста нет).
    try:
        for link in raw_home.links or []:
            if isinstance(link, str):
                _push(link, "")
    except Exception:
        pass

    return out


def discover_urls(
    base_url: str,
    raw_home: RawPage,
    robots: Any,
    sitemap_urls: Any,
    settings: Any,
) -> List[str]:
    """
    Построить упорядоченный дедуплицированный список URL для проверки.

    Целевой сбор (качество сохраняем, лишнее — не обходим):
      1) внутренние ссылки главной (footer/меню), у которых ключевое слово есть
         в URL ИЛИ в тексте ссылки — это реальные документы/страницы сайта;
      2) sitemap_urls с ключевыми словами;
      3) до _MAX_FORM_PAGES страниц с формами/контактами — только те прочие
         внутренние ссылки, чей URL ИЛИ текст совпал с FORM_PAGE_KEYWORDS
         (контакты/запись/заявка/услуги и т.п.). Каталог/блог/карточки товаров
         больше не обходим — там нет сбора персональных данных;
      4) ДОГАДКИ по типовым путям (/privacy, /oferta, ...) — только чтобы добить
         бюджет, не более _MAX_GUESS_PATHS, чтобы 404-догадки не съедали лимит.

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
        try:
            cmp_key = clean.rstrip("/")
        except Exception:
            cmp_key = clean
        if base_stripped and cmp_key == base_stripped:
            return
        if clean in seen:
            return
        seen.add(clean)
        ordered.append(clean)

    # Реальные ссылки главной (с текстом), разложенные на «документные» и
    # «страницы форм/контактов». Прочие (каталог/блог/товары) не собираем.
    doc_links: List[str] = []
    form_links: List[str] = []
    for abs_url, text in _homepage_anchors(raw_home, base):
        try:
            if base and not utils.same_registered_domain(abs_url, base):
                continue
        except Exception:
            continue
        # Ключевое слово в ТЕКСТЕ ссылки — самый надёжный сигнал документа:
        # такую ссылку не отбрасываем по расширению (политика может лежать
        # в .pdf или на нетипичном маршруте).
        try:
            text_kw = bool(text) and utils.contains_any(text, LINK_KEYWORDS)
        except Exception:
            text_kw = False
        if not text_kw:
            # Фильтр «не-HTML» применяем только к ссылкам без текстового
            # ключевого слова.
            try:
                if not utils.is_probably_html_url(abs_url):
                    continue
            except Exception:
                continue
        blob = abs_url + " " + (text or "")
        try:
            has_kw = text_kw or utils.contains_any(blob, LINK_KEYWORDS)
        except Exception:
            has_kw = text_kw
        if has_kw:
            doc_links.append(abs_url)
            continue
        # Не документ — берём ссылку только если она про форму/контакты
        # (URL или текст совпал с FORM_PAGE_KEYWORDS). Иначе пропускаем.
        try:
            form_kw = utils.contains_any(blob, FORM_PAGE_KEYWORDS)
        except Exception:
            form_kw = False
        if form_kw:
            form_links.append(abs_url)

    # (1) Реальные «документные» ссылки — в первую очередь.
    for link in doc_links:
        _add(link)

    # (2) sitemap_urls с ключевыми словами.
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

    # (3) До _MAX_FORM_PAGES страниц с формами/контактами (целевой сбор).
    added_forms = 0
    for link in form_links:
        if added_forms >= _MAX_FORM_PAGES:
            break
        before = len(ordered)
        _add(link)
        if len(ordered) > before:
            added_forms += 1

    # (4) ДОГАДКИ по типовым путям — последними и с лимитом.
    if base:
        guesses = 0
        for path in PRIORITY_PATHS:
            if path == "/":
                continue
            if len(ordered) >= max_pages or guesses >= _MAX_GUESS_PATHS:
                break
            try:
                abs_url = utils.absolute_url(base, path)
            except Exception:
                abs_url = ""
            before = len(ordered)
            _add(abs_url)
            if len(ordered) > before:
                guesses += 1

    return ordered[:max_pages]
