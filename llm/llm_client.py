"""
Клиент LLM (нативный Anthropic Messages API ИЛИ OpenAI-совместимый Chat
Completions — по settings.llm_provider) и построители клиентских текстов.

Ключевые принципы:
  * LLM полностью опционален. Если он выключен или недоступен (нет httpx, нет
    ключа, ошибка сети) — используются нейтральные текстовые шаблоны из
    llm/prompts.py.
  * Ни одна публичная функция не бросает исключение — при сбое возвращается
    None или безопасный текст.
  * httpx импортируется внутри метода (guarded), чтобы модуль импортировался
    даже без установленной библиотеки.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from config.settings import Settings
from llm import json_repair, prompts
from scanner.models import ScanContext, ScanResult


# ---------------------------------------------------------------------------
# Мягкая нормализация тона (фолбэк, если legal.legal_basis недоступен)
# ---------------------------------------------------------------------------
_FALLBACK_TONE_REPLACEMENTS = [
    ("вы нарушаете", "выявлены признаки риска"),
    ("нарушение установлено", "выявлены признаки риска"),
    ("установлено нарушение", "выявлены признаки риска"),
    ("это нарушение", "это зона риска"),
    ("является нарушением", "может являться зоной риска"),
    ("незаконно", "требует проверки юристом"),
    ("незаконный", "требующий проверки"),
    ("сайт незаконен", "сайт требует проверки юристом"),
    ("штраф", "правовой риск"),
    ("штрафы", "правовые риски"),
]


def _sanitize_tone(text: str) -> str:
    """
    Смягчить запрещённые формулировки. Пытается использовать
    legal.legal_basis.sanitize_tone; при недоступности — локальный фолбэк.
    """
    if not text:
        return ""
    try:
        from legal.legal_basis import sanitize_tone as _lb_sanitize

        cleaned = _lb_sanitize(text)
        if cleaned:
            return cleaned
    except Exception:
        pass

    result = text
    for bad, good in _FALLBACK_TONE_REPLACEMENTS:
        # Регистронезависимая мягкая замена.
        lowered = result.lower()
        idx = lowered.find(bad)
        while idx != -1:
            result = result[:idx] + good + result[idx + len(bad):]
            lowered = result.lower()
            idx = lowered.find(bad, idx + len(good))
    return result


# ---------------------------------------------------------------------------
# Клиент LLM
# ---------------------------------------------------------------------------
class LLMClient:
    """
    Клиент LLM с двумя режимами (выбор через settings.llm_provider):
      * "anthropic" — нативный Anthropic Messages API (POST /v1/messages,
        заголовок x-api-key + anthropic-version);
      * иначе (по умолчанию) — OpenAI-совместимый Chat Completions
        (POST /chat/completions, Authorization: Bearer).
    """

    _ANTHROPIC_PROVIDERS = {"anthropic", "claude", "native"}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def enabled(self) -> bool:
        try:
            return bool(self.settings.enable_llm) and bool(self.settings.llm_api_key)
        except Exception:
            return False

    def _is_anthropic(self) -> bool:
        provider = str(getattr(self.settings, "llm_provider", "") or "").strip().lower()
        return provider in self._ANTHROPIC_PROVIDERS

    def chat(
        self,
        system: str,
        user: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """
        Выполнить один запрос к LLM (нативный Anthropic или OpenAI-совместимый —
        см. settings.llm_provider). Возвращает текст ответа или None. Никогда не
        бросает исключение.
        """
        if not self.enabled:
            return None
        try:
            import httpx  # guarded: библиотека может быть не установлена
        except Exception:
            return None

        mt = int(max_tokens if max_tokens is not None else self.settings.llm_max_tokens) or 4000
        try:
            if self._is_anthropic():
                return self._chat_anthropic(httpx, system or "", user or "", mt)
            return self._chat_openai(httpx, system or "", user or "", temperature, mt)
        except Exception:
            return None

    # -- OpenAI-совместимый Chat Completions --
    def _chat_openai(
        self, httpx, system: str, user: str,
        temperature: Optional[float], max_tokens: int,
    ) -> Optional[str]:
        base = (self.settings.llm_base_url or "").rstrip("/")
        url = base + "/chat/completions"
        temp = temperature if temperature is not None else self.settings.llm_temperature
        payload: Dict[str, Any] = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temp,
        }
        if max_tokens:
            payload["max_tokens"] = int(max_tokens)
        headers = {
            "Authorization": f"Bearer {self.settings.llm_api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            # Некоторые провайдеры возвращают content частями.
            parts = []
            for part in content:
                if isinstance(part, dict):
                    parts.append(str(part.get("text", "")))
                else:
                    parts.append(str(part))
            content = "".join(parts)
        if content is None:
            return None
        content = str(content).strip()
        return content or None

    # -- Нативный Anthropic Messages API --
    def _chat_anthropic(self, httpx, system: str, user: str, max_tokens: int) -> Optional[str]:
        base = (self.settings.llm_base_url or "").rstrip("/")
        # По умолчанию (или если в base остался openai) используем api.anthropic.com.
        if not base or "openai" in base.lower():
            base = "https://api.anthropic.com"
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")

        # Модель обязана быть Claude-моделью; если случайно осталась openai-строка
        # (напр. дефолтная gpt-4o-mini) — берём разумный дефолт, чтобы не словить 404.
        model = (self.settings.llm_model or "").strip()
        if not model.lower().startswith("claude"):
            model = "claude-sonnet-5"

        payload: Dict[str, Any] = {
            "model": model,
            "max_tokens": int(max_tokens),
            "messages": [{"role": "user", "content": user}],
        }
        if system:
            payload["system"] = system  # system — top-level поле, не сообщение
        # ВАЖНО: НЕ передаём temperature/top_p/top_k/thinking — на актуальных
        # моделях (Opus 4.8/4.7, Sonnet 5, Fable 5) sampling-параметры дают 400.

        headers = {
            "x-api-key": self.settings.llm_api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code < 200 or resp.status_code >= 300:
            return None
        data = resp.json()
        # Отказ модели по соображениям безопасности — не считаем ответом.
        if data.get("stop_reason") == "refusal":
            return None
        # content — список блоков; берём текстовые (пропуская thinking-блоки).
        parts: List[str] = []
        for block in data.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        content = "".join(parts).strip()
        return content or None

    def generate(self, system: str, user: str) -> Optional[str]:
        """Обёртка над chat для генерации свободного текста."""
        return self.chat(system, user)

    def analyze_document(
        self,
        document_text: str,
        doc_type: str,
        items: Optional[List[Dict[str, Any]]],
        facts: Optional[Dict[str, Any]],
    ) -> Optional[dict]:
        """
        Проанализировать документ по чек-листу через LLM.

        Строит сообщения через prompts, вызывает chat, разбирает ответ через
        json_repair. При неудаче разбора — один повтор с инструкцией по
        починке. Проверяет минимальную форму (наличие списка checklist_results).
        Возвращает dict или None. Никогда не бросает исключение.
        """
        if not self.enabled:
            return None
        if not document_text:
            return None

        try:
            system, user = prompts.build_document_analysis_messages(
                document_text, doc_type, items, facts
            )
        except Exception:
            return None

        raw = self.chat(system, user)
        parsed = json_repair.loads_lenient(raw) if raw else None

        if not self._valid_analysis_shape(parsed):
            # Один повтор с явной инструкцией вернуть корректный JSON.
            try:
                repair = json_repair.repair_instructions(raw or "")
            except Exception:
                repair = "Верни валидный JSON строго по схеме."
            retry_user = user + "\n\n" + repair
            raw2 = self.chat(system, retry_user)
            parsed = json_repair.loads_lenient(raw2) if raw2 else None
            if not self._valid_analysis_shape(parsed):
                return None

        return parsed

    @staticmethod
    def _valid_analysis_shape(data: Optional[dict]) -> bool:
        if not isinstance(data, dict):
            return False
        results = data.get("checklist_results")
        return isinstance(results, list)


# ---------------------------------------------------------------------------
# Модульные функции
# ---------------------------------------------------------------------------
def get_client(settings: Settings) -> Optional[LLMClient]:
    """Вернуть LLMClient, если LLM включён в настройках, иначе None."""
    try:
        if settings is not None and getattr(settings, "enable_llm", False):
            return LLMClient(settings)
    except Exception:
        pass
    return None


def _facts_for_texts(
    result: ScanResult,
    ctx: Optional[ScanContext],
    settings: Settings,
    packages: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Собрать словарь фактов для генерации клиентских текстов через LLM."""
    facts: Dict[str, Any] = {}
    try:
        facts = dict(prompts._facts_from_result(result, ctx))
    except Exception:
        facts = {}

    # Реквизиты бюро.
    try:
        facts["firm_name"] = getattr(settings, "firm_name", "") or ""
        contacts_parts = [
            getattr(settings, "firm_contacts", "") or "",
            getattr(settings, "firm_phone", "") or "",
            getattr(settings, "firm_email", "") or "",
            getattr(settings, "firm_website", "") or "",
        ]
        facts["firm_contacts"] = "; ".join(p for p in contacts_parts if p)
    except Exception:
        pass

    # Пакеты с ценами для КП.
    try:
        prices = settings.prices() if settings is not None else {}
    except Exception:
        prices = {}
    pkg_list: List[Dict[str, Any]] = []
    try:
        ordered = prompts._packages_to_ordered(packages)
        for p in ordered:
            price_key = str(p.get("price_key", "")).strip()
            price = prices.get(price_key, "") if isinstance(prices, dict) else ""
            pkg_list.append(
                {
                    "title": str(p.get("title", "")).strip(),
                    "price": price,
                    "duration": str(p.get("duration", "")).strip(),
                }
            )
    except Exception:
        pkg_list = []
    facts["packages"] = pkg_list

    return facts


def build_client_texts(
    result: ScanResult,
    ctx: Optional[ScanContext],
    settings: Settings,
    client: Optional[LLMClient],
    packages: Optional[Dict[str, Any]],
) -> Tuple[str, str, str]:
    """
    Вернуть тройку (summary, offer, email).

    Пытается сгенерировать тексты через LLM (client.generate + prompts.build_*),
    при любом None/сбое — использует нейтральные шаблоны prompts.*_template.
    Всегда возвращает три непустых нейтральных строки; тон нормализуется.
    """
    facts = _facts_for_texts(result, ctx, settings, packages)

    use_llm = bool(client) and getattr(client, "enabled", False)

    # --- summary ---
    summary: Optional[str] = None
    if use_llm:
        try:
            sys_m, usr_m = prompts.build_summary_messages(facts)
            summary = client.generate(sys_m, usr_m)
        except Exception:
            summary = None
    if not summary:
        try:
            summary = prompts.summary_template(result, ctx, settings)
        except Exception:
            summary = "Подготовлено предварительное заключение по результатам проверки."

    # --- offer ---
    offer: Optional[str] = None
    if use_llm:
        try:
            sys_o, usr_o = prompts.build_offer_messages(facts)
            offer = client.generate(sys_o, usr_o)
        except Exception:
            offer = None
    if not offer:
        try:
            offer = prompts.offer_template(result, ctx, settings, packages)
        except Exception:
            offer = "Готовы предложить услуги по приведению сайта в соответствие."

    # --- email ---
    email: Optional[str] = None
    if use_llm:
        try:
            sys_e, usr_e = prompts.build_email_messages(facts)
            email = client.generate(sys_e, usr_e)
        except Exception:
            email = None
    if not email:
        try:
            email = prompts.email_template(result, ctx, settings)
        except Exception:
            email = "Будем рады обсудить результаты проверки вашего сайта."

    return (
        _sanitize_tone(summary),
        _sanitize_tone(offer),
        _sanitize_tone(email),
    )
