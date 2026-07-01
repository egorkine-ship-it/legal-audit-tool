"""
Разбор robots.txt сайта.

Извлекает объявленные карты сайта (Sitemap:) и запрещённые пути (Disallow:).
Модуль никогда не бросает исключение наружу — при ошибке возвращает безопасный
словарь-заглушку.
"""
from __future__ import annotations

from typing import Dict, List

from config.settings import Settings
from scanner import utils


def fetch_robots(base_url: str, settings: Settings) -> Dict[str, object]:
    """
    Загрузить и разобрать robots.txt.

    Возвращает словарь:
        {
            "found": bool,          # удалось ли получить непустой robots.txt
            "sitemaps": List[str],  # абсолютные URL карт сайта
            "disallow": List[str],  # пути из директив Disallow
            "text": str,            # исходный текст robots.txt
        }
    """
    result: Dict[str, object] = {
        "found": False,
        "sitemaps": [],
        "disallow": [],
        "text": "",
    }
    try:
        robots_url = utils.absolute_url(base_url, "/robots.txt")
        if not robots_url:
            robots_url = (base_url or "").rstrip("/") + "/robots.txt"

        resp = utils.http_get(
            robots_url,
            settings.user_agent,
            settings.request_timeout_s,
            settings.max_download_bytes,
        )

        text = resp.text or ""
        # robots.txt всегда текст; если по какой-то причине text пуст, но есть
        # содержимое (например, странный content-type), пробуем декодировать сами.
        if not text and resp.content:
            try:
                text = resp.content.decode("utf-8", errors="replace")
            except Exception:
                text = ""

        result["text"] = text

        if not resp.ok or not text.strip():
            return result

        sitemaps, disallow = _parse_robots(text, base_url)
        result["found"] = True
        result["sitemaps"] = sitemaps
        result["disallow"] = disallow
        return result
    except Exception:
        # Никогда не бросаем наружу — возвращаем заглушку.
        return result


def _parse_robots(text: str, base_url: str):
    sitemaps: List[str] = []
    disallow: List[str] = []
    for line in text.splitlines():
        raw_line = line.strip()
        if not raw_line or raw_line.startswith("#"):
            continue
        # Отделяем возможный комментарий в конце строки.
        if "#" in raw_line:
            raw_line = raw_line.split("#", 1)[0].strip()
            if not raw_line:
                continue
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key == "sitemap":
            # Sitemap-директивы обычно абсолютные, но нормализуем на всякий случай.
            abs_url = value
            low = value.lower()
            if not (low.startswith("http://") or low.startswith("https://")):
                abs_url = utils.absolute_url(base_url, value) or value
            if abs_url and abs_url not in sitemaps:
                sitemaps.append(abs_url)
        elif key == "disallow":
            if value not in disallow:
                disallow.append(value)
    return sitemaps, disallow
