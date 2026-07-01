"""
Тесты LLM-агента обхода сайта (llm/site_agent.py).

httpx подменяется через sys.modules фейковым модулем со скриптованными
ответами: первый ответ — tool_use open_page, второй — финальный JSON.

Проверяется:
  * цикл инструментов: fetcher реально вызывается, tool_result уходит в API,
    steps_used считается;
  * заземление ответа: чужие домены фильтруются, уже найденные документы
    дедуплицируются (включая хвостовой '/');
  * доменная защита инструмента: чужой домен и не-http(s) схема дают
    tool_result с is_error, fetcher не вызывается, цикл продолжается;
  * исключение fetcher -> is_error, без падения;
  * LLM выключен / нет ключа -> used=False, ни одного запроса;
  * мусорный ответ -> used=True, missed_candidates == [], без исключений;
  * OpenAI-совместимый фолбэк: та же валидация без tool use.

Модуль llm/site_agent.py может собираться параллельно, поэтому импортируется
через importorskip — тесты не должны падать на этапе сбора.
"""
from __future__ import annotations

import json
import sys

import pytest

from config.settings import Settings
from scanner.models import DocumentResult, RawPage

site_agent = pytest.importorskip("llm.site_agent")


BASE = "https://example.ru"


# ---------------------------------------------------------------------------
# Фейки: httpx и fetcher
# ---------------------------------------------------------------------------
class _FakeResponse:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data


class _FakeHttpx:
    """Подменяет модуль httpx: Client.post отдаёт скриптованные ответы по очереди."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []  # [{"url", "payload", "headers"}]
        outer = self

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def post(self, url, json=None, headers=None):
                outer.requests.append(
                    {"url": url, "payload": json, "headers": headers}
                )
                if not outer._responses:
                    return _FakeResponse({}, status_code=500)
                return outer._responses.pop(0)

        self.Client = _Client


class _FakeFetcher:
    """Отдаёт заранее заготовленные RawPage; запоминает вызовы."""

    def __init__(self, pages=None, raise_on=None):
        self.pages = dict(pages or {})
        self.raise_on = set(raise_on or [])
        self.calls = []

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        if url in self.raise_on:
            raise RuntimeError("boom")
        page = self.pages.get(url)
        if page is not None:
            return page
        return RawPage(url=url)


# ---------------------------------------------------------------------------
# Строители тестовых данных
# ---------------------------------------------------------------------------
def _settings(**overrides):
    values = dict(
        llm_provider="anthropic",
        llm_api_key="test-key",
        llm_base_url="https://api.openai.com/v1",  # проверяем и коэрцию base
        llm_model="gpt-4o-mini",  # не claude-* -> должна коэрцироваться
        enable_llm=True,
    )
    values.update(overrides)
    return Settings(**values)


def _raw_home():
    return RawPage(
        url=BASE,
        final_url=BASE + "/",
        ok=True,
        html=(
            "<html><body><footer>"
            '<a href="/legal">Правовая информация</a>'
            '<a href="/privacy">Политика конфиденциальности</a>'
            "</footer></body></html>"
        ),
        links=[BASE + "/legal", BASE + "/privacy"],
    )


def _legal_page():
    return RawPage(
        url=BASE + "/legal",
        final_url=BASE + "/legal",
        ok=True,
        title="Правовые документы",
        visible_text="Правовая информация. Согласие на рекламную рассылку. " * 50,
        html=(
            "<html><body>"
            '<a href="/consent-marketing">Согласие на рекламную рассылку</a>'
            '<a href="/privacy">Политика конфиденциальности</a>'
            "</body></html>"
        ),
        links=[BASE + "/consent-marketing", BASE + "/privacy"],
    )


def _documents():
    return [
        DocumentResult(
            doc_id="d1",
            doc_type="privacy_policy",
            url=BASE + "/privacy",
            is_accessible=True,
        )
    ]


def _tool_use_response(tool_calls):
    """Ответ API со stop_reason=tool_use и блоками tool_use."""
    content = [
        {"type": "tool_use", "id": tid, "name": "open_page", "input": {"url": u}}
        for tid, u in tool_calls
    ]
    return _FakeResponse({"stop_reason": "tool_use", "content": content})


def _final_response(payload):
    """Финальный текстовый ответ API (end_turn) с JSON внутри."""
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return _FakeResponse(
        {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
    )


def _run(monkeypatch, responses, fetcher=None, settings=None, documents=None):
    fake = _FakeHttpx(responses)
    monkeypatch.setitem(sys.modules, "httpx", fake)
    fetcher = fetcher if fetcher is not None else _FakeFetcher()
    result = site_agent.audit_site(
        BASE,
        _raw_home(),
        documents if documents is not None else _documents(),
        fetcher,
        settings if settings is not None else _settings(),
        max_steps=8,
    )
    return result, fake, fetcher


# ---------------------------------------------------------------------------
# Основной сценарий: цикл инструментов + фильтрация + дедупликация
# ---------------------------------------------------------------------------
def test_agent_tool_loop_filters_offdomain_and_dedupes(monkeypatch):
    final = {
        "missed_documents": [
            # Валидный кандидат на нашем домене — должен остаться.
            {
                "url": BASE + "/consent-marketing",
                "doc_type": "ad_consent",
                "reason": "ссылка в подвале раздела «Правовая информация»",
            },
            # Чужой домен — должен быть отфильтрован (заземление).
            {
                "url": "https://evil.com/privacy",
                "doc_type": "privacy_policy",
                "reason": "выдумано",
            },
            # Уже найденный документ (с хвостовым '/') — дедупликация.
            {
                "url": BASE + "/privacy/",
                "doc_type": "privacy_policy",
                "reason": "уже найден машиной",
            },
        ],
        "notes": "Проверил подвал и раздел правовой информации.",
    }
    fetcher = _FakeFetcher(pages={BASE + "/legal": _legal_page()})
    result, fake, fetcher = _run(
        monkeypatch,
        [_tool_use_response([("toolu_1", BASE + "/legal")]), _final_response(final)],
        fetcher=fetcher,
    )

    assert result["used"] is True
    assert result["steps_used"] == 1
    assert fetcher.calls == [BASE + "/legal"]

    # Остался ровно один заземлённый кандидат.
    assert len(result["missed_candidates"]) == 1
    cand = result["missed_candidates"][0]
    assert cand["url"] == BASE + "/consent-marketing"
    assert cand["doc_type"] == "ad_consent"
    assert cand["reason"]
    assert result["notes"] == "Проверил подвал и раздел правовой информации."


def test_agent_request_shape_and_model_coercion(monkeypatch):
    """Проверяем форму запроса: заголовки, top-level system, коэрцию модели."""
    fetcher = _FakeFetcher(pages={BASE + "/legal": _legal_page()})
    result, fake, fetcher = _run(
        monkeypatch,
        [
            _tool_use_response([("toolu_1", BASE + "/legal")]),
            _final_response({"missed_documents": [], "notes": ""}),
        ],
        fetcher=fetcher,
    )
    assert result["used"] is True
    assert len(fake.requests) == 2

    first = fake.requests[0]
    # llm_base_url содержит openai -> подменяется на api.anthropic.com.
    assert first["url"] == "https://api.anthropic.com/v1/messages"
    assert first["headers"]["x-api-key"] == "test-key"
    assert first["headers"]["anthropic-version"] == "2023-06-01"
    payload = first["payload"]
    # Модель не claude-* -> коэрцируется.
    assert payload["model"] == "claude-sonnet-5"
    assert "temperature" not in payload
    assert isinstance(payload.get("system"), str) and "аудитор" in payload["system"]
    tools = payload["tools"]
    assert len(tools) == 1 and tools[0]["name"] == "open_page"

    # Во втором запросе — ассистентский ход и наш tool_result без is_error.
    second_messages = fake.requests[1]["payload"]["messages"]
    assert second_messages[1]["role"] == "assistant"
    tool_results = second_messages[2]["content"]
    assert len(tool_results) == 1
    tr = tool_results[0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "toolu_1"
    assert not tr.get("is_error")
    # Содержимое tool_result — JSON со ссылками страницы и фрагментом текста.
    body = json.loads(tr["content"])
    assert body["final_url"].startswith(BASE)
    assert any(l["url"].endswith("/consent-marketing") for l in body["links"])
    assert len(body["text_excerpt"]) <= 1500


# ---------------------------------------------------------------------------
# Доменная защита инструмента
# ---------------------------------------------------------------------------
def test_agent_domain_guard_returns_is_error_and_continues(monkeypatch):
    fetcher = _FakeFetcher()
    result, fake, fetcher = _run(
        monkeypatch,
        [
            _tool_use_response(
                [
                    ("toolu_evil", "https://evil.com/hidden"),
                    ("toolu_ftp", "ftp://example.ru/docs.pdf"),
                ]
            ),
            _final_response({"missed_documents": [], "notes": "готово"}),
        ],
        fetcher=fetcher,
    )

    # Fetcher не вызывался: оба URL отклонены до загрузки.
    assert fetcher.calls == []
    # Цикл продолжился и дошёл до финального ответа.
    assert result["used"] is True
    assert result["missed_candidates"] == []
    assert len(fake.requests) == 2

    tool_results = fake.requests[1]["payload"]["messages"][2]["content"]
    assert len(tool_results) == 2
    by_id = {tr["tool_use_id"]: tr for tr in tool_results}
    assert by_id["toolu_evil"]["is_error"] is True
    assert by_id["toolu_ftp"]["is_error"] is True


def test_agent_fetcher_exception_becomes_is_error(monkeypatch):
    fetcher = _FakeFetcher(raise_on={BASE + "/legal"})
    result, fake, fetcher = _run(
        monkeypatch,
        [
            _tool_use_response([("toolu_1", BASE + "/legal")]),
            _final_response({"missed_documents": [], "notes": ""}),
        ],
        fetcher=fetcher,
    )
    assert result["used"] is True
    assert result["steps_used"] == 1
    tr = fake.requests[1]["payload"]["messages"][2]["content"][0]
    assert tr["is_error"] is True
    assert "Ошибка загрузки" in tr["content"]


# ---------------------------------------------------------------------------
# LLM выключен / нет ключа
# ---------------------------------------------------------------------------
def test_agent_disabled_returns_used_false(monkeypatch):
    result, fake, _ = _run(
        monkeypatch, [], settings=_settings(enable_llm=False)
    )
    assert result == {
        "used": False,
        "missed_candidates": [],
        "notes": "",
        "steps_used": 0,
    }
    assert fake.requests == []


def test_agent_no_api_key_returns_used_false(monkeypatch):
    result, fake, _ = _run(monkeypatch, [], settings=_settings(llm_api_key=""))
    assert result["used"] is False
    assert result["missed_candidates"] == []
    assert fake.requests == []


def test_agent_http_error_returns_used_false(monkeypatch):
    result, fake, _ = _run(
        monkeypatch, [_FakeResponse({"error": "overloaded"}, status_code=529)]
    )
    assert result["used"] is False
    assert result["missed_candidates"] == []


# ---------------------------------------------------------------------------
# Мусорные ответы: used может быть True, но кандидатов нет и нет исключений
# ---------------------------------------------------------------------------
def test_agent_garbage_text_response_is_safe(monkeypatch):
    result, fake, _ = _run(
        monkeypatch, [_final_response("Всё в порядке, ничего не пропущено!")]
    )
    assert result["used"] is True
    assert result["missed_candidates"] == []
    assert result["steps_used"] == 0


def test_agent_wrong_shape_json_is_safe(monkeypatch):
    # JSON-объект без missed_documents.
    result, _, _ = _run(
        monkeypatch, [_final_response({"answer": 42, "notes": "странный ответ"})]
    )
    assert result["used"] is True
    assert result["missed_candidates"] == []
    assert result["notes"] == "странный ответ"

    # missed_documents не-список; элементы не-dict; мусорные url.
    result2, _, _ = _run(
        monkeypatch,
        [
            _final_response(
                {
                    "missed_documents": [
                        "просто строка",
                        {"url": "javascript:alert(1)", "doc_type": "offer"},
                        {"url": "", "doc_type": "offer"},
                        {"doc_type": "offer", "reason": "без url"},
                    ],
                    "notes": "",
                }
            )
        ],
    )
    assert result2["used"] is True
    assert result2["missed_candidates"] == []


def test_agent_unknown_doc_type_coerced_to_other(monkeypatch):
    result, _, _ = _run(
        monkeypatch,
        [
            _final_response(
                {
                    "missed_documents": [
                        {
                            "url": BASE + "/some-doc",
                            "doc_type": "весёлый_тип",
                            "reason": "ссылка из подвала",
                        }
                    ],
                    "notes": "",
                }
            )
        ],
    )
    assert result["used"] is True
    assert len(result["missed_candidates"]) == 1
    assert result["missed_candidates"][0]["doc_type"] == "other"


# ---------------------------------------------------------------------------
# OpenAI-совместимый фолбэк (без tool use)
# ---------------------------------------------------------------------------
def test_agent_openai_fallback_same_validation(monkeypatch):
    from llm import llm_client

    answer = json.dumps(
        {
            "missed_documents": [
                {"url": BASE + "/oferta", "doc_type": "offer", "reason": "в подвале"},
                {"url": "https://evil.com/oferta", "doc_type": "offer", "reason": "чужое"},
                {"url": BASE + "/privacy", "doc_type": "privacy_policy", "reason": "дубль"},
            ],
            "notes": "фолбэк без инструментов",
        },
        ensure_ascii=False,
    )
    captured = {}

    def _fake_chat(self, system, user, temperature=None, max_tokens=None):
        captured["system"] = system
        captured["user"] = user
        return answer

    monkeypatch.setattr(llm_client.LLMClient, "chat", _fake_chat)

    result = site_agent.audit_site(
        BASE,
        _raw_home(),
        _documents(),
        _FakeFetcher(),
        _settings(llm_provider="openai-compatible"),
        max_steps=8,
    )
    assert result["used"] is True
    assert result["steps_used"] == 0
    assert len(result["missed_candidates"]) == 1
    assert result["missed_candidates"][0]["url"] == BASE + "/oferta"
    assert result["notes"] == "фолбэк без инструментов"
    # Промпт просит выбирать только из перечисленных ссылок.
    assert "перечисленных ссылок" in captured["system"] or "перечисленных ссылок" in captured["user"]


def test_agent_openai_fallback_llm_unavailable(monkeypatch):
    from llm import llm_client

    monkeypatch.setattr(
        llm_client.LLMClient, "chat", lambda self, *a, **kw: None
    )
    result = site_agent.audit_site(
        BASE,
        _raw_home(),
        _documents(),
        _FakeFetcher(),
        _settings(llm_provider="openai-compatible"),
    )
    assert result["used"] is False
    assert result["missed_candidates"] == []


# ---------------------------------------------------------------------------
# Прочее: некорректные входы не роняют функцию
# ---------------------------------------------------------------------------
def test_agent_bad_inputs_never_raise(monkeypatch):
    fake = _FakeHttpx([])
    monkeypatch.setitem(sys.modules, "httpx", fake)

    # Пустой base_url.
    r1 = site_agent.audit_site("", None, None, None, _settings())
    assert r1["used"] is False

    # settings=None.
    r2 = site_agent.audit_site(BASE, None, None, None, None)
    assert r2["used"] is False

    # documents=None, raw_home=None при включённом LLM: диалог с финальным текстом.
    fake2 = _FakeHttpx([_final_response({"missed_documents": [], "notes": "ок"})])
    monkeypatch.setitem(sys.modules, "httpx", fake2)
    r3 = site_agent.audit_site(BASE, None, None, _FakeFetcher(), _settings())
    assert r3["used"] is True
    assert r3["missed_candidates"] == []
