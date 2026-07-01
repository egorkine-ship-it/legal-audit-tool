"""
Разбор XML-карт сайта (sitemap.xml).

Поддерживает как индекс карт (<sitemapindex>), так и обычный <urlset>.
Работа с XML пространств имён — по локальному имени тега (namespace-agnostic).
Модуль никогда не бросает исключение наружу.
"""
from __future__ import annotations

from typing import List

from config.settings import Settings
from scanner import utils

# Максимальное число дочерних карт, которые разбираем из sitemap-index.
_MAX_INDEX_CHILDREN = 3


def _localname(tag: str) -> str:
    """Локальное имя тега без пространства имён: '{ns}loc' -> 'loc'."""
    if not tag:
        return ""
    if "}" in tag:
        tag = tag.rsplit("}", 1)[1]
    return tag.lower()


def _safe_xml_root(xml_text: str):
    """
    Безопасно распарсить XML недоверенной карты сайта и вернуть корневой элемент
    (или None). Защита от XXE / «billion laughs»: документы с DOCTYPE/ENTITY
    отклоняются, при наличии defusedxml используется он. Никогда не бросает.
    """
    if not xml_text:
        return None
    # Отсекаем внешние/внутренние сущности и DOCTYPE (entity-expansion атаки).
    head = xml_text[:4096].lower()
    if "<!entity" in head or "<!doctype" in head:
        return None

    def _try(text):
        # 1) defusedxml (если установлен) — самый безопасный путь.
        try:
            import defusedxml.ElementTree as DET  # type: ignore

            return DET.fromstring(text)
        except Exception:
            pass
        # 2) stdlib ElementTree. По умолчанию не расширяет внешние сущности,
        #    а внутренние мы уже отклонили выше.
        try:
            import xml.etree.ElementTree as ET

            return ET.fromstring(text)
        except Exception:
            return None

    root = _try(xml_text)
    if root is not None:
        return root
    # Срезаем возможный BOM/мусор перед первым '<'.
    idx = xml_text.find("<")
    if idx > 0:
        return _try(xml_text[idx:])
    return None


def parse_sitemap(xml_text: str) -> List[str]:
    """
    Разобрать XML одной карты сайта и вернуть список URL из <loc>.

    Для sitemap-index это будут URL дочерних карт; для urlset — URL страниц.
    Не различает тип на этом уровне — просто собирает все <loc>.
    Никогда не бросает исключение.
    """
    root = _safe_xml_root(xml_text)
    if root is None:
        return []
    locs: List[str] = []
    try:
        for elem in root.iter():
            if _localname(elem.tag) == "loc":
                value = (elem.text or "").strip()
                if value:
                    locs.append(value)
    except Exception:
        return locs
    return locs


def _classify_root(xml_text: str) -> str:
    """Вернуть 'sitemapindex', 'urlset' или '' по корневому тегу."""
    root = _safe_xml_root(xml_text)
    if root is None:
        return ""
    return _localname(getattr(root, "tag", "") or "")


def fetch_sitemap_urls(
    sitemap_url: str,
    settings: Settings,
    limit: int = 200,
) -> List[str]:
    """
    Загрузить карту сайта и вернуть уникальные URL страниц (до limit).

    Обрабатывает индекс карт: при <sitemapindex> подгружает дочерние карты
    (не более _MAX_INDEX_CHILDREN) и собирает URL из них.
    Никогда не бросает исключение — при любой ошибке возвращает то, что успело
    собраться (или пустой список).
    """
    out: List[str] = []
    seen = set()

    if not sitemap_url:
        return out

    try:
        xml_text = _get_text(sitemap_url, settings)
        if not xml_text:
            return out

        root_kind = _classify_root(xml_text)
        locs = parse_sitemap(xml_text)

        if root_kind == "sitemapindex":
            # Дочерние карты. Ограничиваем число подгрузок.
            children = 0
            for child_url in locs:
                if children >= _MAX_INDEX_CHILDREN:
                    break
                children += 1
                try:
                    child_xml = _get_text(child_url, settings)
                except Exception:
                    child_xml = ""
                if not child_xml:
                    continue
                for u in parse_sitemap(child_xml):
                    if u not in seen:
                        seen.add(u)
                        out.append(u)
                        if len(out) >= limit:
                            return out
            return out[:limit]

        # Обычный urlset (или неизвестный корень — трактуем locs как URL).
        for u in locs:
            if u not in seen:
                seen.add(u)
                out.append(u)
                if len(out) >= limit:
                    break
        return out[:limit]
    except Exception:
        return out[:limit]


def _get_text(url: str, settings: Settings) -> str:
    """Безопасно получить текст карты сайта (в т.ч. если content-type не text)."""
    resp = utils.http_get(
        url,
        settings.user_agent,
        settings.request_timeout_s,
        settings.max_download_bytes,
    )
    text = resp.text or ""
    content = resp.content or b""
    # Многие карты отдаются в gzip (sitemap.xml.gz) или с бинарным content-type.
    is_gzip = content[:2] == b"\x1f\x8b" or url.lower().split("?")[0].endswith(".gz")
    if content and is_gzip:
        try:
            import gzip

            text = gzip.decompress(content).decode("utf-8", errors="replace")
        except Exception:
            text = text or ""
    if not text and content:
        # content-type application/xml/octet-stream и т.п. — utils._decode их не
        # декодирует; пробуем сами.
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = ""
    if not resp.ok:
        # Даже при не-ok статусе, если тело есть, попробуем распарсить — но
        # пустой ok обычно означает отсутствие карты.
        if not text.strip():
            return ""
    return text
