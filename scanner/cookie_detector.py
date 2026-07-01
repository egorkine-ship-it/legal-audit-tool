"""
Детектор cookie-баннера и классификация cookie-файлов.

- detect_cookie_banner: консервативная эвристика наличия уведомления о cookie.
- classify_cookies: сопоставление имён cookie с каталогом трекеров.
- cookies_before_consent: признак установки маркетинговых/аналитических cookie
  (или обращений к провайдерам) ДО согласия, поскольку кнопку «принять» мы не
  нажимаем.

Правила проекта:
- Python 3.9 совместимость (typing.Optional/List/Dict/Tuple, без `X | None`).
- Ни одна публичная функция не бросает исключение наружу.
- Нейтральная формулировка: только «признаки риска», без утверждений о нарушении.
- Переиспользуются помощники из scanner.utils.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from scanner import utils
from scanner.models import CookieInfo


# Ключевые слова для эвристики cookie-баннера (нормализуются при сравнении).
COOKIE_BANNER_KEYWORDS = [
    "cookie",
    "куки",
    "файлы cookie",
    "используя сайт",
    "продолжая использовать",
    "мы используем cookie",
    "метрическ",
]

# Слова-действия «принять/согласиться» рядом с упоминанием cookie.
_ACCEPT_WORDS = [
    "принять",
    "принимаю",
    "согласен",
    "согласна",
    "соглашаюсь",
    "accept",
    "agree",
    "ок",
    "okay",
    "хорошо",
    "понятно",
    "разрешить",
    "allow",
]

# Фразы «продолжая пользоваться сайтом …».
_CONTINUE_PHRASES = [
    "используя сайт",
    "используя данный сайт",
    "используя этот сайт",
    "продолжая пользоваться",
    "продолжая использовать",
    "оставаясь на сайте",
    "продолжая просмотр",
]

# Признаки cookie/consent в упоминании cookie (для проверки самого факта cookie).
_COOKIE_MENTION = ["cookie", "куки", "cookies"]

# Подстроки классов/идентификаторов баннерных контейнеров.
_BANNER_CONTAINER_HINTS = [
    "cookie",
    "consent",
    "cookiebanner",
    "cookie-banner",
    "cookie-consent",
    "cookie-notice",
    "cookie-policy",
    "gdpr",
]


def _mentions_cookie(norm_text: str) -> bool:
    for kw in _COOKIE_MENTION:
        if kw in norm_text:
            return True
    return False


def _has_banner_container(html: str) -> bool:
    """Есть ли контейнер с class/id, намекающим на cookie/consent баннер."""
    if not html:
        return False
    lowered = html.lower()
    # Ищем связку атрибута class/id со значением-подсказкой в пределах окна.
    import re

    try:
        for m in re.finditer(r'(?:class|id)\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', lowered):
            value = m.group(1).strip("\"'")
            for hint in _BANNER_CONTAINER_HINTS:
                if hint in value:
                    return True
    except Exception:
        return False
    return False


def detect_cookie_banner(html: str, visible_text: str) -> Tuple[bool, str]:
    """
    Консервативная эвристика наличия cookie-уведомления.

    Возвращает (found, evidence_snippet). True, если текст/HTML упоминают
    cookie/куки И выполняется хотя бы одно из:
      - рядом есть слово-действие «принять/согласен/accept/ок/хорошо»;
      - присутствует фраза «используя сайт»/«продолжая пользоваться»;
      - есть контейнер с class/id, содержащим cookie/consent.

    Никогда не бросает исключение.
    """
    try:
        html = html or ""
        visible_text = visible_text or ""

        norm_text = utils.normalize_for_search(visible_text)
        norm_html = utils.normalize_for_search(html)

        mentions = _mentions_cookie(norm_text) or _mentions_cookie(norm_html)
        if not mentions:
            return False, ""

        # Условие A: слово-действие «принять/согласиться» в тексте или HTML.
        has_accept = utils.contains_any(visible_text, _ACCEPT_WORDS) or utils.contains_any(
            html, _ACCEPT_WORDS
        )
        # Условие B: фразы «используя сайт»/«продолжая пользоваться».
        has_continue = utils.contains_any(visible_text, _CONTINUE_PHRASES) or utils.contains_any(
            html, _CONTINUE_PHRASES
        )
        # Условие C: баннерный контейнер по class/id.
        has_container = _has_banner_container(html)

        if not (has_accept or has_continue or has_container):
            return False, ""

        evidence = _cookie_evidence_snippet(visible_text, html)
        return True, evidence
    except Exception:
        return False, ""


def _cookie_evidence_snippet(visible_text: str, html: str) -> str:
    """Короткий фрагмент вокруг упоминания cookie/куки для доказательства."""
    try:
        source = visible_text if visible_text and utils.contains_any(
            visible_text, _COOKIE_MENTION
        ) else html
        cleaned = utils.clean_text(source)
        norm = utils.normalize_for_search(cleaned)
        idx = -1
        for needle in ("cookie", "куки", "cookies"):
            pos = norm.find(needle)
            if pos != -1:
                idx = pos
                break
        if idx == -1:
            return utils.truncate(cleaned, 300)
        start = max(0, idx - 120)
        end = min(len(cleaned), idx + 180)
        return utils.truncate(cleaned[start:end], 300)
    except Exception:
        return ""


def _cookie_name_map(tracker_index: Dict[str, Any]) -> List[Dict[str, str]]:
    """Список записей tracking_cookie_names как словарей name/provider/category."""
    out: List[Dict[str, str]] = []
    try:
        entries = (tracker_index or {}).get("tracking_cookie_names") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not name:
                continue
            out.append(
                {
                    "name": str(name),
                    "provider": str(entry.get("provider", "") or ""),
                    "category": str(entry.get("category", "") or "") or "unknown",
                }
            )
    except Exception:
        return out
    return out


def _match_cookie_name(cookie_name: str, name_map: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """
    Сопоставить имя cookie с каталогом (префикс/подстрока).

    Каталожные имена часто задают префиксы (``_ym_``, ``_ga``, ``tmr_``),
    поэтому сравниваем и по началу имени, и по вхождению подстроки.
    """
    if not cookie_name:
        return None
    low = cookie_name.lower()
    # Сначала более точное совпадение: префикс/равенство.
    best: Optional[Dict[str, str]] = None
    best_len = -1
    for entry in name_map:
        cat_name = entry["name"].lower()
        if not cat_name:
            continue
        if low == cat_name or low.startswith(cat_name) or cat_name in low:
            if len(cat_name) > best_len:
                best = entry
                best_len = len(cat_name)
    return best


def classify_cookies(cookies: List[Dict[str, Any]], tracker_index: Dict[str, Any]) -> List[CookieInfo]:
    """
    Классифицировать cookie по каталогу трекеров.

    Для каждого cookie ({name, value, domain}) сопоставляет имя со списком
    ``tracking_cookie_names``; при совпадении проставляет category/provider и
    is_tracking. Иначе category="unknown". value_preview усечён.

    Никогда не бросает исключение.
    """
    results: List[CookieInfo] = []
    if not cookies:
        return results
    name_map = _cookie_name_map(tracker_index)

    for cookie in cookies:
        try:
            if isinstance(cookie, dict):
                name = str(cookie.get("name", "") or "")
                domain = str(cookie.get("domain", "") or "")
                value = cookie.get("value", "")
            else:
                name = str(getattr(cookie, "name", "") or "")
                domain = str(getattr(cookie, "domain", "") or "")
                value = getattr(cookie, "value", "")

            value_preview = utils.truncate(str(value) if value is not None else "", 60)

            info = CookieInfo(
                name=name,
                domain=domain,
                value_preview=value_preview,
                category="unknown",
                provider="",
                is_tracking=False,
            )

            match = _match_cookie_name(name, name_map)
            if match is not None:
                info.category = match.get("category") or "unknown"
                info.provider = match.get("provider") or ""
                info.is_tracking = info.category in ("analytics", "marketing")

            results.append(info)
        except Exception:
            # Проблема с одним cookie не должна ломать остальные.
            continue

    return results


def _tracking_provider_domains(tracker_index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Домены провайдеров, обращение к которым до согласия является признаком риска
    (категория analytics/marketing ИЛИ should_trigger_cookie_check).

    Возвращает список {"domain": str, "provider": str}.
    """
    out: List[Dict[str, Any]] = []
    try:
        providers = (tracker_index or {}).get("providers") or []
    except Exception:
        return out
    for prov in providers:
        if not isinstance(prov, dict):
            continue
        try:
            category = str(prov.get("category", "") or "")
            trigger = bool(prov.get("should_trigger_cookie_check", False))
            if category not in ("analytics", "marketing") and not trigger:
                continue
            name = str(prov.get("provider_name", "") or "")
            domains = prov.get("domains") or []
            if isinstance(domains, str):
                domains = [domains]
            for dom in domains:
                if dom:
                    out.append({"domain": str(dom), "provider": name})
        except Exception:
            continue
    return out


def cookies_before_consent(
    cookies: List[Dict[str, Any]],
    network_requests: List[str],
    tracker_index: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Признак установки маркетинговых/аналитических cookie ДО согласия.

    Кнопку «принять» мы никогда не нажимаем, поэтому любой tracking-cookie
    (analytics/marketing) или любой сетевой запрос к домену провайдера с
    категорией analytics/marketing (или should_trigger_cookie_check) считается
    признаком «до согласия».

    Возвращает
    ``{"has_marketing_before_consent": bool, "items": List[str], "evidence": str}``.
    Никогда не бросает исключение.
    """
    items: List[str] = []
    try:
        # 1) Классифицируем cookie и берём tracking (analytics/marketing).
        classified = classify_cookies(cookies or [], tracker_index)
        for ci in classified:
            if ci.is_tracking or ci.category in ("analytics", "marketing"):
                label = ci.name or "(cookie)"
                if ci.provider:
                    label = f"{label} ({ci.provider})"
                items.append("cookie: " + label)

        # 2) Сетевые запросы к доменам провайдеров группы риска.
        provider_domains = _tracking_provider_domains(tracker_index)
        reqs = network_requests or []
        matched_provider_urls = set()
        for req in reqs:
            try:
                url_low = str(req or "").lower()
            except Exception:
                continue
            if not url_low:
                continue
            for pd in provider_domains:
                dom = pd["domain"].lower()
                if dom and dom in url_low:
                    provider = pd["provider"] or dom
                    key = provider
                    if key not in matched_provider_urls:
                        matched_provider_urls.add(key)
                        items.append("запрос: " + provider)
                    break

        items = utils.dedupe_keep_order(items)
        has_marketing = len(items) > 0

        evidence = ""
        if has_marketing:
            preview = "; ".join(items[:8])
            evidence = utils.truncate(
                "До подтверждения согласия зафиксированы обращения/файлы, "
                "которые требуют проверки: " + preview,
                480,
            )

        return {
            "has_marketing_before_consent": bool(has_marketing),
            "items": items,
            "evidence": evidence,
        }
    except Exception:
        return {"has_marketing_before_consent": False, "items": [], "evidence": ""}
