"""
Помощники для формирования доказательной базы (Evidence).

Evidence — это связка URL страницы, короткой цитаты, HTML-фрагмента и (опц.)
скриншота, которая подкладывается под каждый вывод в отчёте. Модуль намеренно
лёгкий: без тяжёлых/опциональных зависимостей, только модели проекта и utils.

Все функции безопасны: никогда не бросают исключение наружу и возвращают
разумные значения по умолчанию.
"""
from __future__ import annotations

import re
from typing import Any

from scanner import utils
from scanner.models import Evidence


# Максимальные длины полей доказательства.
_QUOTE_MAX = 500
_HTML_SNIPPET_MAX = 800


def truncate_quote(s: str, n: int = _QUOTE_MAX) -> str:
    """Обрезать цитату до n символов, схлопнув пробелы."""
    try:
        if not s:
            return ""
        cleaned = utils.clean_text(str(s))
        return utils.truncate(cleaned, n)
    except Exception:
        return ""


def _truncate_html(s: str, n: int = _HTML_SNIPPET_MAX) -> str:
    """Обрезать HTML-фрагмент (без агрессивной чистки пробелов внутри тегов)."""
    try:
        if not s:
            return ""
        text = str(s).strip()
        if len(text) <= n:
            return text
        return text[: n - 1].rstrip() + "…"
    except Exception:
        return ""


def make_evidence(
    page_url: str = "",
    quote: str = "",
    html_snippet: str = "",
    screenshot_path: str = "",
    **extra: Any,
) -> Evidence:
    """
    Собрать Evidence с безопасной обрезкой цитаты (<=500) и HTML (<=~800).

    Любые дополнительные именованные аргументы складываются в `extra`.
    """
    try:
        clean_extra = {}
        if extra:
            for key, value in extra.items():
                try:
                    clean_extra[str(key)] = value
                except Exception:
                    continue
        return Evidence(
            page_url=str(page_url or ""),
            quote=truncate_quote(quote or "", _QUOTE_MAX),
            html_snippet=_truncate_html(html_snippet or "", _HTML_SNIPPET_MAX),
            screenshot_path=str(screenshot_path or ""),
            extra=clean_extra,
        )
    except Exception:
        # Гарантируем валидный объект даже при неожиданных входных данных.
        return Evidence()


def snippet_around(text: str, needle: str, radius: int = 200) -> str:
    """
    Найти `needle` в `text` (регистронезависимо, ё->е) и вернуть окно вокруг
    него шириной ~radius символов с каждой стороны. Результат чистится от
    лишних пробелов. Если `needle` не найден — вернуть начало текста.
    """
    try:
        if not text:
            return ""
        raw = str(text)
        if not needle:
            return utils.truncate(utils.clean_text(raw), 2 * radius)

        norm_text = utils.normalize_for_search(raw)
        norm_needle = utils.normalize_for_search(str(needle))
        if not norm_needle:
            return utils.truncate(utils.clean_text(raw), 2 * radius)

        # Позиция в нормализованном тексте приблизительно соответствует позиции
        # в исходном (normalize_for_search меняет регистр/ё и схлопывает
        # пробелы, поэтому окно берём с запасом и потом чистим).
        idx = norm_text.find(norm_needle)
        if idx < 0:
            return utils.truncate(utils.clean_text(raw), 2 * radius)

        start = max(0, idx - radius)
        end = min(len(raw), idx + len(norm_needle) + radius)
        window = raw[start:end]
        prefix = "…" if start > 0 else ""
        suffix = "…" if end < len(raw) else ""
        return prefix + utils.clean_text(window) + suffix
    except Exception:
        try:
            return utils.truncate(utils.clean_text(str(text)), 2 * radius)
        except Exception:
            return ""


_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style|noscript)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)


def first_snippet(html_or_text: str, max_len: int = 400) -> str:
    """
    Вернуть короткую текстовую «шапку» из HTML или произвольного текста:
    первые осмысленные символы, очищенные от тегов и лишних пробелов.
    """
    try:
        if not html_or_text:
            return ""
        raw = str(html_or_text)
        if "<" in raw and ">" in raw:
            raw = _SCRIPT_STYLE_RE.sub(" ", raw)
            raw = _TAG_RE.sub(" ", raw)
        cleaned = utils.clean_text(raw)
        return utils.truncate(cleaned, max_len)
    except Exception:
        try:
            return utils.truncate(utils.clean_text(str(html_or_text)), max_len)
        except Exception:
            return ""
