"""
LLM-агент «третьего пояса» проверки (site agent).

Машинный сканер (пояс 1) находит документы по ссылкам и эвристикам; чек-листы
(пояс 2) проверяют их содержимое. Этот модуль добавляет пояс 3: LLM-агент
реально ходит по сайту через наш fetcher (Playwright/HTTP) и проверяет, что
юридические документы (политика ПДн, согласия, cookie policy, оферта,
пользовательское соглашение) не были пропущены.

Принципы:
  * Строгая заземлённость: агент может открывать ТОЛЬКО страницы того же
    зарегистрированного домена (инструмент open_page отклоняет чужие URL);
    в итоговом ответе учитываются только URL того же домена — оркестратор
    затем сам перепроверяет их повторной загрузкой (второй пояс заземления).
  * Публичная функция audit_site НИКОГДА не бросает исключение; при
    выключенном LLM / отсутствии ключа / сетевых ошибках возвращает
    {"used": False, ...}.
  * Жёсткие лимиты: не более max_steps вызовов инструмента, общий бюджет
    120 секунд (time.monotonic), таймаут одного запроса 60 секунд,
    агрессивная обрезка списков ссылок и текстов.
  * httpx импортируется внутри функции (guarded), как и в llm_client.
  * Режимы: нативный Anthropic Messages API с tool use (llm_provider in
    {"anthropic", "claude", "native"}) ИЛИ одноразовый запрос через
    OpenAI-совместимый LLMClient.chat (без инструментов) — фолбэк.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from llm import json_repair
from scanner import crawler, utils


# ---------------------------------------------------------------------------
# Константы и лимиты
# ---------------------------------------------------------------------------
# Допустимые типы документов в ответе агента.
_ALLOWED_DOC_TYPES = {
    "privacy_policy",
    "consent",
    "ad_consent",
    "cookie_policy",
    "offer",
    "user_agreement",
    "other",
}

# Провайдеры, означающие нативный Anthropic Messages API (зеркало LLMClient).
_ANTHROPIC_PROVIDERS = {"anthropic", "claude", "native"}

_MAX_DOCS_IN_PROMPT = 40          # максимум документов в системном промпте
_MAX_LINKS_IN_PROMPT = 120        # максимум ссылок главной в системном промпте
_MAX_LINKS_IN_TOOL_RESULT = 80    # максимум ссылок в результате open_page
_MAX_MISSED_CANDIDATES = 20       # максимум кандидатов из ответа агента
_TEXT_EXCERPT_CHARS = 1500        # фрагмент видимого текста страницы
_URL_TRUNCATE = 300               # обрезка URL в контексте
_TEXT_TRUNCATE = 120              # обрезка текста ссылки в контексте
_NOTES_TRUNCATE = 2000            # обрезка заметок агента
_MAX_TOKENS = 2000                # max_tokens одного запроса к LLM
_REQUEST_TIMEOUT_S = 60.0         # таймаут одного HTTP-запроса к LLM
_TOTAL_BUDGET_S = 120.0           # общий бюджет времени на весь аудит

# Единственный инструмент агента: открыть страницу проверяемого сайта.
_OPEN_PAGE_TOOL: Dict[str, Any] = {
    "name": "open_page",
    "description": (
        "Открыть страницу проверяемого сайта и получить её итоговый URL, "
        "заголовок, список ссылок и фрагмент видимого текста. Разрешены "
        "только http(s)-URL того же зарегистрированного домена, что и сайт."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Абсолютный URL страницы на проверяемом сайте",
            },
        },
        "required": ["url"],
    },
}


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _empty_result() -> Dict[str, Any]:
    """Пустой безопасный результат аудита."""
    return {"used": False, "missed_candidates": [], "notes": "", "steps_used": 0}


def _is_http_url(url: str) -> bool:
    """URL имеет схему http или https."""
    try:
        return urlparse(url).scheme.lower() in ("http", "https")
    except Exception:
        return False


def _url_key(url: str) -> str:
    """Ключ дедупликации: без фрагмента, без хвостового '/', в нижнем регистре."""
    try:
        clean = utils.strip_fragment(str(url or "").strip())
    except Exception:
        clean = str(url or "").strip()
    return clean.rstrip("/").lower()


def _doc_field(doc: Any, name: str, default: Any = "") -> Any:
    """Достать поле из DocumentResult ИЛИ dict — безопасно."""
    try:
        if isinstance(doc, dict):
            return doc.get(name, default)
        return getattr(doc, name, default)
    except Exception:
        return default


def _known_doc_keys(documents: Any) -> set:
    """Ключи URL уже найденных документов (для дедупликации ответа агента)."""
    keys = set()
    try:
        for doc in documents or []:
            url = str(_doc_field(doc, "url", "") or "")
            if url:
                keys.add(_url_key(url))
    except Exception:
        pass
    return keys


def _docs_summary(documents: Any) -> List[Dict[str, Any]]:
    """Краткая сводка найденных документов для системного промпта."""
    out: List[Dict[str, Any]] = []
    try:
        for doc in (documents or [])[:_MAX_DOCS_IN_PROMPT]:
            url = str(_doc_field(doc, "url", "") or "")
            if not url:
                continue
            out.append(
                {
                    "url": utils.truncate(url, _URL_TRUNCATE),
                    "doc_type": str(_doc_field(doc, "doc_type", "other") or "other"),
                    "is_accessible": bool(_doc_field(doc, "is_accessible", False)),
                }
            )
    except Exception:
        pass
    return out


def _links_payload(raw_page: Any, base_url: str, limit: int) -> List[Dict[str, str]]:
    """Ссылки страницы как [{url, text}] с обрезкой (через crawler._homepage_anchors)."""
    out: List[Dict[str, str]] = []
    if raw_page is None:
        return out
    try:
        anchors = crawler._homepage_anchors(raw_page, base_url)
    except Exception:
        anchors = []
    for url, text in anchors[: max(0, int(limit))]:
        try:
            out.append(
                {
                    "url": utils.truncate(str(url or ""), _URL_TRUNCATE),
                    "text": utils.truncate(str(text or ""), _TEXT_TRUNCATE),
                }
            )
        except Exception:
            continue
    return out


def _build_system_prompt(
    base_url: str,
    docs: List[Dict[str, Any]],
    links: List[Dict[str, str]],
    with_tool: bool,
) -> str:
    """Системный промпт агента (на русском), с фактами и жёстким правилом."""
    try:
        docs_json = json.dumps(docs, ensure_ascii=False)
    except Exception:
        docs_json = "[]"
    try:
        links_json = json.dumps(links, ensure_ascii=False)
    except Exception:
        links_json = "[]"

    if with_tool:
        how = (
            "Открывай страницы инструментом open_page (подвал, разделы "
            "«документы» / «правовая информация»)."
        )
    else:
        how = (
            "Инструментов у тебя нет — выбирай ТОЛЬКО из перечисленных ссылок "
            "главной страницы."
        )

    return (
        "Ты аудитор выгрузки. Машина просканировала сайт " + str(base_url or "")
        + " и нашла документы: " + docs_json
        + "\nСсылки главной страницы (включая подвал): " + links_json
        + "\nТвоя задача: убедиться, что не пропущены юридические документы "
        "(политика обработки персональных данных, согласия на обработку ПДн, "
        "cookie policy, публичная оферта, пользовательское соглашение). "
        + how
        + " ЖЁСТКОЕ ПРАВИЛО: сообщай ТОЛЬКО URL, которые ты реально видел в "
        "списках ссылок или открывал — не выдумывай адреса. "
        "Когда закончишь, верни ТОЛЬКО JSON: "
        '{"missed_documents": [{"url": "...", "doc_type": "...", '
        '"reason": "..."}], "notes": "..."}. '
        "Допустимые значения doc_type: privacy_policy, consent, ad_consent, "
        "cookie_policy, offer, user_agreement, other. Если пропусков нет — "
        'верни {"missed_documents": [], "notes": "..."}.'
    )


def _execute_open_page(block: Dict[str, Any], base_url: str, fetcher: Any) -> Dict[str, Any]:
    """
    Выполнить вызов инструмента open_page и вернуть блок tool_result.

    Отклоняет (is_error) URL чужого домена и не-http(s) схемы. Никогда не
    бросает исключение — ошибки загрузки тоже возвращаются как is_error.
    """
    tool_use_id = str(block.get("id", "") or "")
    try:
        inp = block.get("input") or {}
        page_url = str(inp.get("url", "") or "").strip() if isinstance(inp, dict) else ""
    except Exception:
        page_url = ""

    def _error(message: str) -> Dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": message,
            "is_error": True,
        }

    if not page_url or not _is_http_url(page_url):
        return _error("Ошибка: допустимы только http(s)-URL проверяемого сайта.")
    try:
        same_domain = utils.same_registered_domain(page_url, base_url)
    except Exception:
        same_domain = False
    if not same_domain:
        return _error(
            "Ошибка: URL вне зарегистрированного домена проверяемого сайта — "
            "открывать чужие сайты запрещено."
        )

    try:
        raw = fetcher.fetch(page_url)
    except Exception as exc:
        return _error("Ошибка загрузки страницы: " + utils.truncate(str(exc), 200))
    if raw is None:
        return _error("Ошибка загрузки страницы: пустой ответ fetcher.")

    try:
        final_url = str(getattr(raw, "final_url", "") or getattr(raw, "url", "") or page_url)
    except Exception:
        final_url = page_url
    try:
        title = str(getattr(raw, "title", "") or "")
    except Exception:
        title = ""
    try:
        visible_text = str(getattr(raw, "visible_text", "") or "")
    except Exception:
        visible_text = ""

    payload = {
        "final_url": utils.truncate(final_url, _URL_TRUNCATE),
        "title": utils.truncate(title, 200),
        "links": _links_payload(raw, base_url, _MAX_LINKS_IN_TOOL_RESULT),
        "text_excerpt": visible_text[:_TEXT_EXCERPT_CHARS],
    }
    try:
        content = json.dumps(payload, ensure_ascii=False)
    except Exception:
        content = str(payload)
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": content}


def _parse_agent_answer(
    text: str,
    base_url: str,
    known_keys: set,
) -> Tuple[List[Dict[str, str]], str]:
    """
    Разобрать финальный ответ агента и вернуть (missed_candidates, notes).

    Пояс заземления №1: остаются только http(s)-URL того же
    зарегистрированного домена, не совпадающие с уже найденными документами
    (без фрагмента, без хвостового '/'). Оркестратор затем сам перепроверяет
    кандидатов повторной загрузкой (пояс №2). Никогда не бросает исключение.
    """
    parsed = json_repair.loads_lenient(text) if text else None
    if not isinstance(parsed, dict):
        return [], ""

    notes = ""
    try:
        notes = utils.truncate(str(parsed.get("notes") or ""), _NOTES_TRUNCATE)
    except Exception:
        notes = ""

    raw_missed = parsed.get("missed_documents")
    if not isinstance(raw_missed, list):
        # Ленивый фолбэк: loads_lenient оборачивает списки в {"items": [...]}.
        raw_missed = parsed.get("items")
    if not isinstance(raw_missed, list):
        return [], notes

    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw_missed[:_MAX_MISSED_CANDIDATES]:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or not _is_http_url(url):
            continue
        try:
            if not utils.same_registered_domain(url, base_url):
                continue
        except Exception:
            continue
        key = _url_key(url)
        if not key or key in known_keys or key in seen:
            continue
        seen.add(key)
        doc_type = str(item.get("doc_type") or "").strip()
        if doc_type not in _ALLOWED_DOC_TYPES:
            doc_type = "other"
        reason = utils.truncate(str(item.get("reason") or ""), 300)
        out.append({"url": url, "doc_type": doc_type, "reason": reason})
    return out, notes


# ---------------------------------------------------------------------------
# Режим 1: нативный Anthropic Messages API с tool use
# ---------------------------------------------------------------------------
def _audit_via_anthropic(
    base_url: str,
    raw_home: Any,
    documents: Any,
    fetcher: Any,
    settings: Any,
    max_steps: int,
) -> Dict[str, Any]:
    """Агентный цикл tool use через POST {base}/v1/messages (зеркало llm_client)."""
    result = _empty_result()
    try:
        import httpx  # guarded: библиотека может быть не установлена
    except Exception:
        return result

    base = str(getattr(settings, "llm_base_url", "") or "").rstrip("/")
    # По умолчанию (или если в base остался openai) используем api.anthropic.com.
    if not base or "openai" in base.lower():
        base = "https://api.anthropic.com"
    url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")

    # Модель обязана быть Claude-моделью; если случайно осталась openai-строка
    # (напр. дефолтная gpt-4o-mini) — берём разумный дефолт, чтобы не словить 404.
    model = str(getattr(settings, "llm_model", "") or "").strip()
    if not model.lower().startswith("claude"):
        model = "claude-sonnet-5"

    docs = _docs_summary(documents)
    links = _links_payload(raw_home, base_url, _MAX_LINKS_IN_PROMPT)
    system = _build_system_prompt(base_url, docs, links, with_tool=True)

    headers = {
        "x-api-key": str(getattr(settings, "llm_api_key", "") or ""),
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    messages: List[Dict[str, Any]] = [
        {
            "role": "user",
            "content": (
                "Проверь, не пропустила ли машина юридические документы на "
                "сайте. Открой нужные страницы и верни итоговый JSON."
            ),
        }
    ]

    started = time.monotonic()
    steps = 0
    final_text = ""
    limit_hit = False

    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT_S) as client:
            while True:
                if time.monotonic() - started >= _TOTAL_BUDGET_S:
                    limit_hit = True
                    break
                payload: Dict[str, Any] = {
                    "model": model,
                    "max_tokens": _MAX_TOKENS,
                    "system": system,  # system — top-level поле, не сообщение
                    "tools": [_OPEN_PAGE_TOOL],
                    "messages": messages,
                }
                # ВАЖНО: НЕ передаём temperature/top_p/top_k — на актуальных
                # моделях (Sonnet 5, Opus 4.7/4.8) sampling-параметры дают 400.
                resp = client.post(url, json=payload, headers=headers)
                if resp.status_code < 200 or resp.status_code >= 300:
                    return result  # ошибка API -> used=False
                data = resp.json()
                if not isinstance(data, dict):
                    return result
                content = data.get("content") or []
                if not isinstance(content, list):
                    content = []
                stop_reason = data.get("stop_reason")
                tool_uses = [
                    b for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                ]

                budget_ok = (time.monotonic() - started) < _TOTAL_BUDGET_S
                if stop_reason == "tool_use" and tool_uses and steps < max_steps and budget_ok:
                    # Ассистентский ход возвращаем как есть (включая thinking-блоки).
                    messages.append({"role": "assistant", "content": content})
                    tool_results: List[Dict[str, Any]] = []
                    for block in tool_uses:
                        if steps >= max_steps:
                            tool_results.append(
                                {
                                    "type": "tool_result",
                                    "tool_use_id": str(block.get("id", "") or ""),
                                    "content": "Лимит шагов агента исчерпан.",
                                    "is_error": True,
                                }
                            )
                            continue
                        steps += 1
                        tool_results.append(
                            _execute_open_page(block, base_url, fetcher)
                        )
                    # Все tool_result — одним user-сообщением.
                    messages.append({"role": "user", "content": tool_results})
                    continue

                if stop_reason == "tool_use" and tool_uses:
                    # Модель хочет продолжать, но лимит шагов/времени исчерпан.
                    limit_hit = True
                # Финальный текстовый ответ (пропуская thinking-блоки).
                parts: List[str] = []
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "text":
                        parts.append(str(b.get("text", "")))
                final_text = "".join(parts).strip()
                break
    except Exception:
        # Сетевые/транспортные ошибки -> used=False.
        result["steps_used"] = steps
        return result

    # Диалог состоялся: used=True даже если ответ мусорный (кандидатов нет).
    result["used"] = True
    result["steps_used"] = steps
    candidates, notes = _parse_agent_answer(final_text, base_url, _known_doc_keys(documents))
    result["missed_candidates"] = candidates
    if not notes and limit_hit:
        notes = "Агент остановлен по лимиту шагов/времени; проверка неполная."
    result["notes"] = notes
    return result


# ---------------------------------------------------------------------------
# Режим 2: OpenAI-совместимый фолбэк (без tool use, один запрос)
# ---------------------------------------------------------------------------
def _audit_via_openai(
    base_url: str,
    raw_home: Any,
    documents: Any,
    settings: Any,
) -> Dict[str, Any]:
    """Одноразовый запрос через LLMClient.chat: тот же контекст и тот же JSON."""
    result = _empty_result()
    try:
        from llm import llm_client  # локальный импорт: избегаем лишних зависимостей
    except Exception:
        return result

    try:
        client = llm_client.LLMClient(settings)
        if not client.enabled:
            return result
    except Exception:
        return result

    docs = _docs_summary(documents)
    links = _links_payload(raw_home, base_url, _MAX_LINKS_IN_PROMPT)
    system = _build_system_prompt(base_url, docs, links, with_tool=False)
    user = (
        "Инструментов у тебя нет. Проанализируй перечисленные в системном "
        "промпте документы и ссылки главной страницы. Выбирай ТОЛЬКО из "
        "перечисленных ссылок — не выдумывай адреса. Верни ТОЛЬКО JSON: "
        '{"missed_documents": [{"url": "...", "doc_type": "...", '
        '"reason": "..."}], "notes": "..."}.'
    )

    try:
        raw = client.chat(system, user, max_tokens=_MAX_TOKENS)
    except Exception:
        raw = None
    if raw is None:
        return result  # LLM недоступен -> used=False

    result["used"] = True
    candidates, notes = _parse_agent_answer(raw, base_url, _known_doc_keys(documents))
    result["missed_candidates"] = candidates
    result["notes"] = notes
    return result


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------
def audit_site(
    base_url: str,
    raw_home: Any,
    documents: Any,
    fetcher: Any,
    settings: Any,
    max_steps: int = 8,
) -> Dict[str, Any]:
    """
    Агентная перепроверка сайта: LLM реально обходит страницы через fetcher
    и ищет пропущенные юридические документы.

    Возвращает dict:
      {
        "used": bool,                # агент реально отработал
        "missed_candidates": [       # кандидаты в пропущенные документы
            {"url": str, "doc_type": str, "reason": str}, ...
        ],
        "notes": str,                # заметки агента
        "steps_used": int,           # сколько вызовов инструмента сделано
      }

    НИКОГДА не бросает исключение. Возвращает used=False, если LLM выключен,
    нет API-ключа, нет httpx или произошла ошибка запроса.
    """
    try:
        try:
            enabled = bool(getattr(settings, "enable_llm", False)) and bool(
                getattr(settings, "llm_api_key", "")
            )
        except Exception:
            enabled = False
        if not enabled:
            return _empty_result()

        base = str(base_url or "").strip()
        if not base or not _is_http_url(base):
            return _empty_result()

        try:
            steps = int(max_steps)
        except Exception:
            steps = 8
        if steps < 1:
            steps = 1

        provider = str(getattr(settings, "llm_provider", "") or "").strip().lower()
        if provider in _ANTHROPIC_PROVIDERS:
            return _audit_via_anthropic(base, raw_home, documents, fetcher, settings, steps)
        return _audit_via_openai(base, raw_home, documents, settings)
    except Exception:
        return _empty_result()
