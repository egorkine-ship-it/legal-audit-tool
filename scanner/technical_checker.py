"""
Техническая проверка сайта: HTTPS, редирект http->https, mixed content,
IP-адреса/страна сервера (через geoip_checker), CDN, наличие robots.txt/sitemap.

Функция check_technical никогда не бросает исключение наружу: ошибки шагов
собираются в TechnicalResult.errors, результат остаётся валидным.
"""
from __future__ import annotations

from typing import Dict, List
from urllib.parse import urlparse

from scanner import geoip_checker, utils
from scanner.models import RawPage, TechnicalResult

# Ограничение на число собираемых mixed-content ссылок.
_MIXED_CONTENT_CAP = 20


def _is_https(url: str) -> bool:
    try:
        return urlparse(url or "").scheme.lower() == "https"
    except Exception:
        return str(url or "").lower().startswith("https://")


def check_technical(
    base_url: str,
    final_url: str,
    raw_pages: List[RawPage],
    robots: Dict,
    sitemap_urls: List[str],
    settings,
) -> TechnicalResult:
    """
    Собрать технический профиль сайта. Всегда возвращает TechnicalResult.
    """
    result = TechnicalResult()

    # --- HTTPS / редирект ---
    try:
        result.https_enabled = _is_https(final_url or base_url)
    except Exception as exc:
        result.errors.append(f"https_enabled: {exc}")

    try:
        # base_url формируется из финального URL главной; если он на https,
        # считаем, что сайт обслуживается по https (редирект/принудительный https).
        result.http_to_https_redirect = _is_https(base_url)
    except Exception as exc:
        result.errors.append(f"http_to_https_redirect: {exc}")

    # --- Mixed content ---
    try:
        mixed: List[str] = []
        for raw in raw_pages or []:
            try:
                page_url = getattr(raw, "final_url", "") or getattr(raw, "url", "")
                if not _is_https(page_url):
                    # На http-странице mixed content не имеет смысла проверять.
                    continue
                resources: List[str] = []
                resources.extend(getattr(raw, "external_resources", None) or [])
                resources.extend(getattr(raw, "network_requests", None) or [])
                for res in resources:
                    try:
                        low = str(res).strip().lower()
                    except Exception:
                        continue
                    if low.startswith("http://"):
                        original = str(res).strip()
                        if original not in mixed:
                            mixed.append(original)
                            if len(mixed) >= _MIXED_CONTENT_CAP:
                                break
                if len(mixed) >= _MIXED_CONTENT_CAP:
                    break
            except Exception:
                continue
        result.mixed_content_urls = mixed
        result.mixed_content_found = bool(mixed)
    except Exception as exc:
        result.errors.append(f"mixed_content: {exc}")

    # --- IP-адреса / страна сервера (GeoIP) ---
    try:
        enable_geoip = bool(getattr(settings, "enable_geoip", False))
    except Exception:
        enable_geoip = False

    ips: List[str] = []
    if enable_geoip:
        try:
            host = utils.get_host(final_url or base_url)
            ips = geoip_checker.resolve_ips(host)
        except Exception as exc:
            ips = []
            result.errors.append(f"resolve_ips: {exc}")
    result.ip_addresses = ips

    if enable_geoip:
        try:
            country, confidence = geoip_checker.lookup_country(ips, settings)
            result.server_country = country
            result.geoip_confidence = confidence
        except Exception as exc:
            result.server_country = "unknown"
            result.geoip_confidence = 0
            result.errors.append(f"lookup_country: {exc}")
    else:
        result.server_country = "unknown"
        result.geoip_confidence = 0

    # --- CDN ---
    try:
        result.cdn_detected = geoip_checker.detect_cdn(raw_pages)
    except Exception as exc:
        result.cdn_detected = ""
        result.errors.append(f"detect_cdn: {exc}")

    # --- robots.txt / sitemap ---
    try:
        result.robots_txt_found = bool((robots or {}).get("found"))
    except Exception as exc:
        result.robots_txt_found = False
        result.errors.append(f"robots_txt_found: {exc}")

    try:
        result.sitemap_found = bool(sitemap_urls)
    except Exception as exc:
        result.sitemap_found = False
        result.errors.append(f"sitemap_found: {exc}")

    return result
