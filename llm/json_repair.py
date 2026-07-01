"""
Устойчивый разбор JSON-ответов LLM.

LLM иногда оборачивает JSON в markdown-ограждения (```json … ```), добавляет
пояснительный текст до/после, использует висячие запятые или «умные» кавычки.
Эти утилиты извлекают и лениво разбирают такой JSON. Никогда не бросают
исключение — при неудаче возвращают безопасное значение по умолчанию.
"""
from __future__ import annotations

import json
import re
from typing import Optional


def extract_json_block(text: str) -> str:
    """
    Вернуть наиболее вероятный JSON-объект из текста.

    Снимает ограждения ```json / ``` и берёт подстроку от первой '{' до
    последней '}'. Если фигурных скобок нет — возвращает очищенный исходный
    текст.
    """
    if not text:
        return ""
    s = str(text).strip()

    # Снимаем markdown-ограждения (```json … ``` или ``` … ```).
    fence = re.search(
        r"```(?:json|JSON)?\s*(.*?)```",
        s,
        flags=re.DOTALL,
    )
    if fence:
        candidate = fence.group(1).strip()
        if candidate:
            s = candidate
    else:
        # Иногда есть только открывающее ограждение без закрывающего.
        s = re.sub(r"^```(?:json|JSON)?\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1].strip()
    return s


def _strip_trailing_commas(text: str) -> str:
    """Убрать висячие запятые перед } или ] (частая ошибка LLM)."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _normalize_quotes(text: str) -> str:
    """Заменить «умные»/типографские кавычки на обычные двойные."""
    replacements = {
        "“": '"',  # “
        "”": '"',  # ”
        "„": '"',  # „
        "‟": '"',  # ‟
        "‘": "'",  # ‘
        "’": "'",  # ’
        "«": '"',  # «
        "»": '"',  # »
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def loads_lenient(text: str) -> Optional[dict]:
    """
    Попытаться разобрать JSON-объект из произвольного текста LLM.

    Сначала прямой json.loads по извлечённому блоку; при неудаче — повторные
    попытки с удалением висячих запятых и нормализацией кавычек. Возвращает
    dict или None. Никогда не бросает исключение.
    """
    if not text:
        return None

    block = extract_json_block(text)
    if not block:
        return None

    candidates = []
    candidates.append(block)
    candidates.append(_strip_trailing_commas(block))
    normalized = _normalize_quotes(block)
    candidates.append(normalized)
    candidates.append(_strip_trailing_commas(normalized))

    for cand in candidates:
        if not cand:
            continue
        try:
            parsed = json.loads(cand)
        except Exception:
            continue
        if isinstance(parsed, dict):
            return parsed
        # Если пришёл список — оборачиваем в объект, чтобы вызвать контракт dict.
        if isinstance(parsed, list):
            return {"items": parsed}

    return None


def repair_instructions(bad: str) -> str:
    """
    Сформировать короткое сообщение для повторного запроса к LLM, когда
    предыдущий ответ не удалось разобрать как JSON.
    """
    preview = ""
    if bad:
        preview = str(bad).strip()
        if len(preview) > 600:
            preview = preview[:600] + "…"
    return (
        "Предыдущий ответ не удалось разобрать как JSON. "
        "Верни ТОЛЬКО валидный JSON-объект строго по заданной схеме, "
        "без пояснительного текста, без markdown-ограждений (```), "
        "без комментариев и без висячих запятых. "
        "Используй обычные двойные кавычки.\n\n"
        "Некорректный ответ (для справки):\n" + preview
    )
