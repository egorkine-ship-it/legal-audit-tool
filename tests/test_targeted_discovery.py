"""
Тесты ЦЕЛЕВОГО сбора URL (targeted discovery).

Проверяют без сети (синтетическая главная страница), что discover_urls:
  * берёт реальные документные ссылки (Политика/Оферта) из подвала;
  * берёт страницы форм/контактов (/contacts, /zapis) — совпадение по
    FORM_PAGE_KEYWORDS в URL или тексте ссылки;
  * НЕ берёт каталог/блог/карточки товаров (нет сбора персональных данных);
  * держит итоговый список коротким (целевой сбор, не breadth-crawl).
"""
from __future__ import annotations

from scanner import crawler
from scanner.models import RawPage


class _Settings:
    """Минимальный контейнер настроек (модули читают поля через getattr)."""

    user_agent = "test-agent"
    request_timeout_s = 5
    max_download_bytes = 1024 * 1024
    max_pages = 10
    enable_screenshots = False


def _home_with_mixed_links() -> RawPage:
    """Главная: подвал с документами + контакты/запись + много каталога/блога."""
    catalog_links = "".join(
        f'<a href="/catalog/item-{i}">Товар {i}</a>' for i in range(20)
    )
    blog_links = "".join(
        f'<a href="/blog/post-{i}">Статья {i}</a>' for i in range(20)
    )
    html = (
        "<html><body>"
        "<header>"
        '<a href="/catalog">Каталог</a>'
        '<a href="/blog">Блог</a>'
        '<a href="/contacts">Контакты</a>'
        "</header>"
        "<main>"
        + catalog_links
        + blog_links
        + '<a href="/zapis">Онлайн-запись</a>'
        + "</main>"
        "<footer>"
        '<a href="/legal/doc-42">Политика обработки персональных данных</a>'
        '<a href="/legal/oferta">Публичная оферта</a>'
        "</footer>"
        "</body></html>"
    )
    return RawPage(
        url="https://shop.ru/",
        final_url="https://shop.ru/",
        html=html,
        ok=True,
    )


def test_targeted_discovery_keeps_docs_and_form_pages():
    raw_home = _home_with_mixed_links()

    urls = crawler.discover_urls("https://shop.ru", raw_home, None, [], _Settings())

    # (1) Документные ссылки — по тексту якоря, даже на опаковых маршрутах.
    assert "https://shop.ru/legal/doc-42" in urls
    assert "https://shop.ru/legal/oferta" in urls
    # (3) Страницы форм/контактов — совпадение по FORM_PAGE_KEYWORDS.
    assert "https://shop.ru/contacts" in urls
    assert "https://shop.ru/zapis" in urls


def test_targeted_discovery_skips_catalog_and_blog():
    raw_home = _home_with_mixed_links()

    urls = crawler.discover_urls("https://shop.ru", raw_home, None, [], _Settings())

    # Каталог/блог/карточки товаров не собираем — там нет сбора ПДн.
    assert "https://shop.ru/catalog" not in urls
    assert "https://shop.ru/blog" not in urls
    for u in urls:
        assert "/catalog/" not in u, u
        assert "/blog/" not in u, u


def test_targeted_discovery_list_stays_small():
    raw_home = _home_with_mixed_links()

    urls = crawler.discover_urls("https://shop.ru", raw_home, None, [], _Settings())

    # Целевой сбор: реальные документы + <=4 страницы форм + немного догадок.
    # Даже при десятках ссылок каталога/блога итог остаётся коротким.
    assert len(urls) <= 10
    # Дубликатов быть не должно.
    assert len(urls) == len(set(urls))


def test_targeted_discovery_caps_form_pages():
    """Не более _MAX_FORM_PAGES страниц форм/контактов сверх документов.

    Используем якоря, совпадающие ТОЛЬКО с FORM_PAGE_KEYWORDS (не с
    LINK_KEYWORDS), иначе они попали бы в документные ссылки без лимита.
    """
    # Пути НЕ из PRIORITY_PATHS, чтобы шаг догадок их не переоткрыл.
    form_anchor_paths = [
        ("/our-services", "Услуги"),
        ("/callback", "Обратный звонок"),
        ("/feedback", "Feedback"),
        ("/zakaz", "Заказать"),
        ("/order-now", "Service"),
        ("/booking-page", "Бронирование"),
    ]
    anchors = "".join(
        f'<a href="{path}">{text}</a>' for path, text in form_anchor_paths
    )
    html = f"<html><body><main>{anchors}</main></body></html>"
    raw_home = RawPage(
        url="https://svc.ru/", final_url="https://svc.ru/", html=html, ok=True
    )

    urls = crawler.discover_urls("https://svc.ru", raw_home, None, [], _Settings())

    form_hits = [
        u
        for u in urls
        if any(u.endswith(path) for path, _ in form_anchor_paths)
    ]
    assert len(form_hits) <= crawler._MAX_FORM_PAGES
