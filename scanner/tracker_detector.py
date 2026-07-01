"""
Детектор сторонних сервисов/трекеров на странице.

По каталогу data/tracker_domains.yml (загружается через load_tracker_index)
ищет совпадения провайдеров в URL скриптов/iframe/сетевых запросов и по
inline-паттернам в HTML. Возвращает список TrackerHit с юридически значимыми
флагами (раскрытие в политике, cookie-проверка, трансграничная передача).

Правила проекта:
- Python 3.9 совместимость (typing.Optional/List/Dict, без `X | None`).
- Ни одна публичная функция не бросает исключение наружу.
- Тяжёлые/опциональные зависимости (yaml) импортируются под защитой.
- Нейтральная формулировка: только «признаки риска», без утверждений о нарушении.
"""
from __future__ import annotations

from typing import Any, Dict, List

from scanner.models import RawPage, TrackerHit


def load_tracker_index(path: str) -> Dict[str, Any]:
    """
    Загрузить каталог трекеров из YAML-файла.

    Возвращает словарь вида
    ``{"providers": [...], "tracking_cookie_names": [...]}``.
    При любой ошибке (нет файла, нет PyYAML, битый YAML) возвращает пустой
    каталог — приложение не должно падать.
    """
    empty: Dict[str, Any] = {"providers": [], "tracking_cookie_names": []}
    if not path:
        return empty
    try:
        import yaml  # опциональная зависимость
    except Exception:
        return empty
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except Exception:
        return empty
    if not isinstance(data, dict):
        return empty
    providers = data.get("providers")
    cookie_names = data.get("tracking_cookie_names")
    return {
        "providers": providers if isinstance(providers, list) else [],
        "tracking_cookie_names": cookie_names if isinstance(cookie_names, list) else [],
    }


def provider_names(tracker_index: Dict[str, Any]) -> List[str]:
    """Список человекочитаемых имён провайдеров из каталога (без падений)."""
    out: List[str] = []
    try:
        providers = (tracker_index or {}).get("providers") or []
        for prov in providers:
            if not isinstance(prov, dict):
                continue
            name = prov.get("provider_name")
            if name:
                out.append(str(name))
    except Exception:
        return out
    return out


def _as_str_list(value: Any) -> List[str]:
    """Привести значение (список/строка/None) к списку непустых строк."""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        out: List[str] = []
        for item in value:
            if item is None:
                continue
            s = str(item)
            if s:
                out.append(s)
        return out
    return []


def _build_hit(provider: Dict[str, Any], matched_domain: str, matched_on: str,
               page_url: str) -> TrackerHit:
    """Собрать TrackerHit, безопасно копируя поля провайдера."""
    def _get_bool(key: str, default: bool) -> bool:
        val = provider.get(key, default)
        return bool(val) if val is not None else default

    def _get_str(key: str, default: str) -> str:
        val = provider.get(key, default)
        return str(val) if val is not None else default

    return TrackerHit(
        provider_name=_get_str("provider_name", "") or "unknown",
        category=_get_str("category", "other") or "other",
        country_hint=_get_str("country_hint", "unknown") or "unknown",
        legal_risk=_get_str("legal_risk", "low") or "low",
        should_be_disclosed_in_policy=_get_bool("should_be_disclosed_in_policy", True),
        should_trigger_cookie_check=_get_bool("should_trigger_cookie_check", False),
        should_trigger_cross_border_check=_get_bool("should_trigger_cross_border_check", False),
        matched_domain=matched_domain or "",
        matched_on=matched_on or "",
        page_url=page_url or "",
    )


def detect_trackers(raw: RawPage, tracker_index: Dict[str, Any]) -> List[TrackerHit]:
    """
    Найти сторонние сервисы/трекеры на странице.

    Множество поиска по URL: script_srcs + iframe_srcs + network_requests.
    Совпадение по домену: подстрока ``domain`` из каталога встречается в каком-либо
    URL → TrackerHit с matched_on по источнику (script/iframe/network) и
    matched_domain=этот домен. Иначе, если подстрока ``pattern`` встречается в
    raw.html → matched_on="html".

    Дедупликация по (provider_name, matched_domain). Никогда не бросает.
    """
    hits: List[TrackerHit] = []
    try:
        providers = (tracker_index or {}).get("providers") or []
    except Exception:
        return hits
    if not isinstance(providers, list) or not providers:
        return hits

    try:
        page_url = (getattr(raw, "final_url", "") or getattr(raw, "url", "")) or ""
    except Exception:
        page_url = ""

    # Источники URL с пометкой типа для matched_on.
    sources: List = []
    try:
        sources.append(("script", _as_str_list(getattr(raw, "script_srcs", None))))
        sources.append(("iframe", _as_str_list(getattr(raw, "iframe_srcs", None))))
        sources.append(("network", _as_str_list(getattr(raw, "network_requests", None))))
    except Exception:
        sources = []

    try:
        html = getattr(raw, "html", "") or ""
    except Exception:
        html = ""
    html_lower = html.lower()

    seen = set()

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        try:
            domains = _as_str_list(provider.get("domains"))
            patterns = _as_str_list(provider.get("patterns"))

            matched_domain = ""
            matched_on = ""

            # 1) Поиск по доменам в URL script/iframe/network.
            for source_kind, urls in sources:
                for dom in domains:
                    dom_l = dom.lower()
                    if not dom_l:
                        continue
                    for url in urls:
                        if dom_l in url.lower():
                            matched_domain = dom
                            matched_on = source_kind
                            break
                    if matched_domain:
                        break
                if matched_domain:
                    break

            # 2) Если по доменам не нашли — ищем inline-паттерны в HTML.
            if not matched_domain and html_lower:
                for pat in patterns:
                    pat_l = pat.lower()
                    if pat_l and pat_l in html_lower:
                        matched_on = "html"
                        break

            if not matched_on:
                continue

            hit = _build_hit(provider, matched_domain, matched_on, page_url)
            key = (hit.provider_name, hit.matched_domain)
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
        except Exception:
            # Проблема с одним провайдером не должна ломать остальные.
            continue

    return hits
