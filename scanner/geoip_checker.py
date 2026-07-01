"""
Определение IP-адресов сайта, страны сервера (GeoIP) и признаков CDN.

Никакая функция здесь не должна бросать исключение наружу: при любой ошибке
возвращается безопасное значение по умолчанию ("unknown"/0/[]).

Тяжёлые/опциональные зависимости (geoip2) импортируются внутри функций, чтобы
модуль импортировался и без установленной библиотеки.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple


# Домены/хосты известных CDN-провайдеров -> человекочитаемое имя.
# Поиск ведётся как подстрока в URL внешних ресурсов/запросов.
_CDN_HOST_MARKERS = [
    ("cloudflare", "Cloudflare"),
    ("cdnjs.cloudflare", "Cloudflare"),
    ("cloudflareinsights", "Cloudflare"),
    ("akamai", "Akamai"),
    ("akamaihd", "Akamai"),
    ("akamaized", "Akamai"),
    ("fastly", "Fastly"),
    ("fastlylb", "Fastly"),
    ("gcore", "Gcore"),
    ("gcdn", "Gcore"),
    ("selectel", "Selectel"),
    ("selcdn", "Selectel"),
    ("cloudfront", "Amazon CloudFront"),
    ("stackpath", "StackPath"),
    ("jsdelivr", "jsDelivr"),
    ("bunnycdn", "BunnyCDN"),
    ("b-cdn.net", "BunnyCDN"),
    ("cdn77", "CDN77"),
    ("ngenix", "NGENIX"),
    ("edgecast", "EdgeCast"),
    ("azureedge", "Azure CDN"),
]

# Страны, которые считаются «российскими»/не-иностранными.
_RU_COUNTRY_MARKERS = {
    "",
    "unknown",
    "ru",
    "russia",
    "russian federation",
    "российская федерация",
    "россия",
}


def resolve_ips(host: str) -> List[str]:
    """
    Разрешить хост в список IP-адресов через socket.getaddrinfo.

    Возвращает уникальный список строковых IP (IPv4/IPv6). Никогда не бросает.
    """
    if not host:
        return []
    host = host.strip()
    # Убираем возможную схему/порт/путь, если случайно передали URL.
    if "://" in host:
        try:
            from urllib.parse import urlparse

            host = urlparse(host).hostname or host
        except Exception:
            pass
    host = host.split("/")[0]
    if "@" in host:
        host = host.split("@")[-1]
    # Отсекаем порт (но не ломаем IPv6 в скобках).
    if host.startswith("["):
        host = host.strip("[]").split("]")[0]
    elif host.count(":") == 1:
        host = host.split(":")[0]

    if not host:
        return []

    ips: List[str] = []
    try:
        import socket

        infos = socket.getaddrinfo(host, None)
        for info in infos:
            try:
                sockaddr = info[4]
                ip = sockaddr[0] if sockaddr else ""
                if ip and ip not in ips:
                    ips.append(ip)
            except Exception:
                continue
    except Exception:
        return []
    return ips


def lookup_country(ips: List[str], settings) -> Tuple[str, int]:
    """
    Определить страну сервера по списку IP.

    Возвращает (country, confidence 0..100). Реальный lookup выполняется только
    если включён geoip, задан путь к базе и доступна библиотека geoip2.
    В остальных случаях — ("unknown", 0). Никогда не бросает.
    """
    if not ips:
        return ("unknown", 0)

    enable_geoip = bool(getattr(settings, "enable_geoip", False))
    db_path = getattr(settings, "geoip_db_path", "") or ""
    if not (enable_geoip and db_path):
        return ("unknown", 0)

    try:
        import geoip2.database
    except Exception:
        return ("unknown", 0)

    reader = None
    try:
        reader = geoip2.database.Reader(db_path)
        for ip in ips:
            try:
                # Приватные адреса не имеют осмысленной страны.
                try:
                    from scanner import utils as _utils

                    if _utils.is_private_ip(ip):
                        continue
                except Exception:
                    pass
                resp = reader.country(ip)
                country = ""
                if resp is not None and getattr(resp, "country", None) is not None:
                    country = resp.country.iso_code or ""
                    if not country:
                        names = getattr(resp.country, "names", {}) or {}
                        country = names.get("en", "") or names.get("ru", "")
                if country:
                    return (country, 80)
            except Exception:
                continue
        return ("unknown", 0)
    except Exception:
        return ("unknown", 0)
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass


def detect_cdn(raw_pages, headers_list: Optional[List[Dict[str, str]]] = None) -> str:
    """
    Best-effort определение CDN по внешним ресурсам/запросам страниц и (опц.)
    HTTP-заголовкам ответов. Возвращает имя провайдера или "" (не определено).
    Никогда не бросает.
    """
    found: List[str] = []

    def _scan_urls(urls) -> None:
        for url in urls or []:
            try:
                low = str(url).lower()
            except Exception:
                continue
            if not low:
                continue
            for marker, name in _CDN_HOST_MARKERS:
                if marker in low and name not in found:
                    found.append(name)

    try:
        for raw in raw_pages or []:
            _scan_urls(getattr(raw, "external_resources", None))
            _scan_urls(getattr(raw, "network_requests", None))
            _scan_urls(getattr(raw, "script_srcs", None))
            _scan_urls(getattr(raw, "iframe_srcs", None))
    except Exception:
        pass

    # Заголовки ответов: Server / CF-Ray / X-Cache / Via и т.п.
    try:
        headers_iter: List[Dict[str, str]] = []
        if headers_list:
            headers_iter = list(headers_list)
        else:
            for raw in raw_pages or []:
                hdrs = getattr(raw, "headers", None)
                if isinstance(hdrs, dict):
                    headers_iter.append(hdrs)
        for hdrs in headers_iter:
            if not isinstance(hdrs, dict):
                continue
            lowered = {str(k).lower(): str(v).lower() for k, v in hdrs.items()}
            server = lowered.get("server", "")
            via = lowered.get("via", "")
            x_cache = lowered.get("x-cache", "")
            blob = " ".join([server, via, x_cache])
            if "cf-ray" in lowered or "cloudflare" in blob:
                if "Cloudflare" not in found:
                    found.append("Cloudflare")
            for marker, name in _CDN_HOST_MARKERS:
                if marker in blob and name not in found:
                    found.append(name)
    except Exception:
        pass

    return found[0] if found else ""


def country_is_foreign(country: str) -> bool:
    """
    True, если страна сервера считается иностранной (не РФ и не «неизвестно»).
    """
    if not country:
        return False
    normalized = country.strip().lower().replace("ё", "е")
    return normalized not in _RU_COUNTRY_MARKERS
