"""
Подсчёт итогового риск-скора, преобразование в уровень и оценка уверенности.

Скоринг детерминированный и объяснимый: итоговый балл — это сумма весов
сработавших правил (с потолком), а уровень риска определяется фиксированными
порогами. Уверенность (confidence) отражает, насколько полными были собранные
данные: наличие документов с текстом, цитат в рисках, найденных cookies/трекеров
и скриншотов повышает уверенность, а ошибки извлечения, недоступные страницы,
отсутствие LLM и неизвестная гео-привязка — понижают.
"""
from __future__ import annotations

from typing import List

from scanner.models import Risk, RiskLevel, ScanResult


# Потолок суммарного скора.
MAX_SCORE = 150


def compute_score(risks: List[Risk]) -> int:
    """Сумма весов сработавших правил, ограниченная сверху MAX_SCORE."""
    if not risks:
        return 0
    total = 0
    try:
        for r in risks:
            try:
                total += int(getattr(r, "score", 0) or 0)
            except Exception:
                continue
    except Exception:
        return 0
    if total < 0:
        total = 0
    return min(total, MAX_SCORE)


def score_to_level(score: int) -> RiskLevel:
    """Порог -> уровень риска.

    0–20   -> low
    21–50  -> medium
    51–80  -> high
    81+    -> critical
    """
    try:
        s = int(score)
    except Exception:
        return RiskLevel.unknown
    if s <= 20:
        return RiskLevel.low
    if s <= 50:
        return RiskLevel.medium
    if s <= 80:
        return RiskLevel.high
    return RiskLevel.critical


def compute_confidence(result: ScanResult) -> int:
    """Оценка уверенности анализа в диапазоне 0..100.

    Стартовая база ~50, далее корректируется по признакам полноты данных.
    Никогда не бросает исключение; при сбое возвращает базовое значение.
    """
    try:
        score = 50

        pages = list(getattr(result, "pages", []) or [])
        documents = list(getattr(result, "documents", []) or [])
        risks = list(getattr(result, "risks", []) or [])
        trackers = list(getattr(result, "trackers", []) or [])
        cookies = list(getattr(result, "cookies", []) or [])

        # --- Плюсы: наличие содержательных данных ---

        # Документы с URL и извлечённым текстом.
        docs_with_text = 0
        extraction_failures = 0
        for d in documents:
            try:
                if getattr(d, "url", "") and getattr(d, "text", ""):
                    docs_with_text += 1
                if getattr(d, "text_extraction_failed", False):
                    extraction_failures += 1
            except Exception:
                continue
        score += min(docs_with_text * 5, 20)

        # Риски с цитатами (evidence.quote) — свидетельство доказательной базы.
        risks_with_quotes = 0
        for r in risks:
            try:
                ev = getattr(r, "evidence", None)
                quote = getattr(ev, "quote", "") if ev is not None else ""
                if quote:
                    risks_with_quotes += 1
            except Exception:
                continue
        score += min(risks_with_quotes * 3, 12)

        # Найденные трекеры / cookies — детектор отработал.
        if trackers:
            score += 5
        if cookies:
            score += 3

        # Скриншоты страниц (визуальное подтверждение).
        has_screenshot = False
        for p in pages:
            try:
                if getattr(p, "screenshot_path", ""):
                    has_screenshot = True
                    break
            except Exception:
                continue
        if has_screenshot:
            score += 5

        # Проверено достаточно страниц.
        if len(pages) >= 3:
            score += 5

        # --- Минусы: пробелы в данных ---

        # Ошибки извлечения текста из документов.
        score -= min(extraction_failures * 6, 18)

        # Недоступные страницы.
        unreachable = 0
        for p in pages:
            try:
                sc = int(getattr(p, "status_code", 0) or 0)
                tlen = int(getattr(p, "text_length", 0) or 0)
                if sc >= 400 or (sc == 0 and tlen == 0):
                    unreachable += 1
            except Exception:
                continue
        score -= min(unreachable * 5, 20)

        # LLM не использовался — часть анализа только эвристическая.
        if not getattr(result, "llm_used", False):
            score -= 10

        # Гео-привязка сервера неизвестна.
        try:
            tech = getattr(result, "technical", None)
            country = getattr(tech, "server_country", "unknown") if tech else "unknown"
            geo_conf = int(getattr(tech, "geoip_confidence", 0) or 0) if tech else 0
            if (not country) or country == "unknown" or geo_conf <= 0:
                score -= 5
        except Exception:
            score -= 5

        # Общий признак ошибок сканирования.
        if getattr(result, "errors", None):
            score -= min(len(result.errors), 5)

        if score < 0:
            score = 0
        if score > 100:
            score = 100
        return score
    except Exception:
        return 50
