"""
Тесты точечных правок точности (agent A).

Проверяются три исправления, устраняющие ложные «критические» вердикты и
испорченный тон:

  1. R026 (в политике указан контактный домен другой организации/группы).
     Раньше срабатывал как CRITICAL на простом несовпадении email-домена
     (напр. askona.ru vs askonalife.com — одна компания, разные домены), из-за
     чего весь отчёт становился «критическим». Теперь:
       * детерминированный конфликт company_conflict имеет risk="medium" и мягкую
         формулировку «уточнить юрлицо»;
       * правило R026 в legal_rules.yml имеет risk_level=medium, score=15;
       * при единственном таком признаке итоговый скор НЕ попадает в
         «критический» диапазон.

  2. Тон-санитайзер (llm.llm_client._sanitize_tone) больше не задваивает и не
     ломает грамматику: вход «...носят характер нарушения ... факта нарушения»
     раньше давал «...признаки риска ... признаки риска» (ломаный падеж и дубль).

  3. data/liability.yml существует, парсится и содержит справочные пункты с
     полями label/amount.

Тесты самодостаточны и не требуют сети/LLM. Загрузка YAML требует PyYAML —
зависящие от неё тесты пропускаются, если библиотека недоступна.
"""
from __future__ import annotations

import pytest

from config.settings import Settings
from legal import document_analyzer, rule_engine, risk_scoring
from llm.llm_client import _sanitize_tone
from scanner.models import (
    DocumentAnalysis,
    DocumentResult,
    RiskLevel,
    ScanContext,
    TechnicalResult,
)


R026 = "R026_POLICY_OTHER_COMPANY_CONFLICT"


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _load_rules():
    """Загрузить правила из штатного YAML или пропустить тест."""
    pytest.importorskip("yaml")
    settings = Settings()
    rules = rule_engine.load_rules(str(settings.rules_path()))
    if not rules:
        pytest.skip("Не удалось загрузить правила из legal_rules.yml")
    return rules


def _r026_rule(rules):
    for r in rules:
        if r.get("id") == R026:
            return r
    return None


def _ctx_with_company_conflict() -> ScanContext:
    """
    ScanContext, где ЕДИНСТВЕННЫЙ конфликт документа — несовпадение email-домена
    (askona.ru vs askonalife.com), т.е. классический ложноположительный кейс:
    одна компания, разные домены.

    Конфликт строится штатным детектором detect_conflicts, а затем прикрепляется
    к анализу документа — как в реальном пайплайне.
    """
    domain = "askona.ru"
    # is_accessible=False / text_length=0: документ существует и его текст
    # доступен detect_conflicts (читает .text), но он НЕ считается «читаемым»
    # для правил полноты политики (R007/R008/R009 требуют _readable_docs), чтобы
    # изолировать в контексте единственный признак — company_conflict → R026.
    document = DocumentResult(
        doc_id="doc-0",
        doc_type="privacy_policy",
        url="https://askona.ru/policy",
        is_accessible=False,
        link_confirmed=True,
        text=(
            "Политика обработки персональных данных. "
            "По всем вопросам обращайтесь: info@askonalife.com. "
            "Оператор осуществляет обработку персональных данных субъектов."
        ),
        text_length=0,
    )
    ctx = ScanContext(
        site_url="https://askona.ru",
        final_url="https://askona.ru",
        registered_domain=domain,
        documents=[document],
        # HTTPS включён, чтобы не срабатывало R017 и контекст оставался чистым.
        technical=TechnicalResult(https_enabled=True),
    )

    # Штатный детектор конфликтов должен вернуть РОВНО один company_conflict
    # уровня medium с мягкой формулировкой.
    conflicts = document_analyzer.detect_conflicts(document, ctx)
    company_conflicts = [c for c in conflicts if "company" in (c.type or "").lower()]
    assert company_conflicts, "Ожидался company_conflict по несовпадению email-домена"

    document.analysis = DocumentAnalysis(
        document_type="privacy_policy",
        document_url=document.url,
        conflicts=conflicts,
    )
    return ctx, conflicts


# ---------------------------------------------------------------------------
# 1) R026 — теперь medium, а не критический и не единственный драйвер «критического»
# ---------------------------------------------------------------------------
def test_company_conflict_is_medium_and_soft_worded():
    """Детерминированный company_conflict должен быть medium и мягко сформулирован."""
    ctx, conflicts = _ctx_with_company_conflict()
    company = [c for c in conflicts if "company" in (c.type or "").lower()]
    assert len(company) == 1
    c = company[0]
    assert c.risk == "medium"
    assert c.source == "heuristic"
    # Мягкая формулировка «уточнить юрлицо», без «принадлежит другой компании».
    assert "уточнить юрлицо" in (c.comment or "").lower()
    assert "принадлежит другой компании" not in (c.comment or "").lower()


def test_r026_rule_metadata_is_medium():
    """В legal_rules.yml R026 должен иметь risk_level=medium и сниженный score."""
    rules = _load_rules()
    rule = _r026_rule(rules)
    assert rule is not None, "Правило R026 должно присутствовать в legal_rules.yml"
    assert str(rule.get("risk_level", "")).lower() == "medium"
    score = int(rule.get("score", 0) or 0)
    assert 0 < score <= 20, f"score R026 должен быть в диапазоне (0, 20], получено {score}"


def test_r026_fires_but_is_not_critical_and_not_sole_critical_driver():
    """
    R026 срабатывает на детерминированном конфликте, но:
      * его уровень — medium (не critical);
      * при ЕДИНСТВЕННОМ таком признаке итоговый скор не попадает в
        «критический» диапазон.
    """
    rules = _load_rules()
    ctx, _ = _ctx_with_company_conflict()
    risks = rule_engine.apply_rules(ctx, rules)

    by_id = {r.id: r for r in risks}
    assert R026 in by_id, "R026 должен срабатывать на детерминированном конфликте"
    assert by_id[R026].risk_level == RiskLevel.medium.value

    # В изолированном контексте R026 — единственный сработавший риск.
    assert len(risks) == 1, f"Ожидался только R026, получено: {sorted(by_id)}"

    # Итоговый скор/уровень не должен быть критическим.
    score = risk_scoring.compute_score(risks)
    level = risk_scoring.score_to_level(score)
    assert level != RiskLevel.critical, (
        f"R026 в одиночку не должен давать критический уровень (score={score}, level={level})"
    )

    # Дополнительно: даже как единственный вклад, R026 не дотягивает до
    # критического порога (81+) — не является драйвером «критического».
    r026_only_score = risk_scoring.compute_score([by_id[R026]])
    assert risk_scoring.score_to_level(r026_only_score) != RiskLevel.critical


# ---------------------------------------------------------------------------
# 2) Тон-санитайзер — без дублей и с корректной грамматикой
# ---------------------------------------------------------------------------
def test_sanitize_tone_no_doubling_grammar_ok():
    """
    Вход, который раньше давал «признаки риска ... признаки риска» с ломаным
    падежом, теперь нормализуется чисто.
    """
    src = (
        "Выводы носят характер нарушения и не являются "
        "констатацией факта нарушения."
    )
    out = _sanitize_tone(src)
    low = out.lower()

    # Нет дословного дубля.
    assert "признаки риска признаки риска" not in low
    assert "признак риска признак риска" not in low
    # Корректная грамматика родительного падежа после «характер»/«факта».
    assert "характер признака риска" in low
    assert "факта признака риска" in low
    # Ломаный вариант отсутствует.
    assert "характер признаки риска" not in low
    assert "факта признаки риска" not in low
    # Запрещённое слово вычищено.
    assert "нарушени" not in low


def test_sanitize_tone_collapses_preexisting_adjacent_duplicate():
    """Если после замены рядом оказались две одинаковые фразы — схлопнуть в одну."""
    src = "характер нарушения признаки риска"
    out = _sanitize_tone(src).lower()
    assert "признаки риска признаки риска" not in out
    assert "признака риска" in out


def test_sanitize_tone_idempotent_on_clean_text():
    """Уже корректный текст не должен искажаться."""
    src = "Выводы носят характер признака риска и требуют проверки юристом."
    assert _sanitize_tone(src) == src


def test_sanitize_tone_empty_is_safe():
    assert _sanitize_tone("") == ""
    assert _sanitize_tone(None) == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3) data/liability.yml — парсится и содержит справочные пункты
# ---------------------------------------------------------------------------
def test_liability_yaml_parses_and_has_items():
    yaml = pytest.importorskip("yaml")
    settings = Settings()
    # Файл лежит рядом с legal_rules.yml в каталоге data/.
    from pathlib import Path

    liability_path = Path(str(settings.rules_path())).parent / "liability.yml"
    assert liability_path.exists(), f"Нет файла {liability_path}"

    with open(liability_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    assert isinstance(data, dict)
    assert isinstance(data.get("disclaimer", ""), str) and data.get("disclaimer")

    items = data.get("items")
    assert isinstance(items, list) and items, "items должен быть непустым списком"
    for it in items:
        assert isinstance(it, dict)
        assert it.get("label"), "у каждого пункта должен быть label"
        assert it.get("amount"), "у каждого пункта должен быть amount"
        # Суммы — строки (не числа), чтобы не терять формат «до 700 000 ₽».
        assert isinstance(it.get("amount"), str)
