"""
Тесты rule engine (legal/rule_engine.py).

Проверяется срабатывание ключевых правил на детерминированных контекстах:
  * R003 — форма с ПДн без отдельного механизма согласия;
  * R011 — есть аналитические/маркетинговые трекеры, но нет cookie-баннера;
  * R001 — есть формы с ПДн, но нет публичной политики обработки ПДн;
  * R015 — есть иностранный трекер (Google), PP_020 не найден.

Правила загружаются из data/legal_rules.yml (через Settings().rules_path()).
Модели ScanContext/FormResult/ConsentInfo/TrackerHit конструируются напрямую.

Загрузка YAML требует PyYAML — тесты, зависящие от загруженных правил,
пропускаются, если правила загрузить не удалось (importorskip("yaml")).
"""
from __future__ import annotations

import pytest

from config.settings import Settings
from legal import rule_engine
from scanner.models import (
    ConsentInfo,
    FormResult,
    ScanContext,
    TechnicalResult,
    TrackerHit,
)


# ---------------------------------------------------------------------------
# Идентификаторы правил в data/legal_rules.yml (полные id).
# ---------------------------------------------------------------------------
R001 = "R001_NO_PRIVACY_POLICY"
R003 = "R003_FORM_NO_SEPARATE_CONSENT"
R011 = "R011_TRACKERS_NO_COOKIE_BANNER"
R015 = "R015_FOREIGN_TRACKERS_CROSS_BORDER_NOT_DISCLOSED"


# ---------------------------------------------------------------------------
# Хелперы построения моделей.
# ---------------------------------------------------------------------------
def _load_rules():
    """Загрузить правила из штатного YAML или пропустить тест."""
    pytest.importorskip("yaml")
    settings = Settings()
    rules = rule_engine.load_rules(str(settings.rules_path()))
    if not rules:
        pytest.skip("Не удалось загрузить правила из legal_rules.yml")
    return rules


def _pd_form_without_consent(page_url="https://example.ru/") -> FormResult:
    """Форма, собирающая ПДн, без чекбокса и без текста согласия."""
    return FormResult(
        form_id="html-0",
        page_url=page_url,
        source="html",
        personal_data_fields=["name", "phone"],
        potentially_personal_data_form=True,
        consent=ConsentInfo(
            checkbox_found=False,
            checkbox_prechecked=None,
            consent_text_found=False,
            privacy_link_found=False,
        ),
    )


def _analytics_tracker(country_hint="RU") -> TrackerHit:
    return TrackerHit(
        provider_name="Яндекс.Метрика" if country_hint == "RU" else "Google Analytics",
        category="analytics",
        country_hint=country_hint,
        matched_domain="mc.yandex.ru" if country_hint == "RU" else "google-analytics.com",
        matched_on="script",
        page_url="https://example.ru/",
    )


def _rule_ids(risks):
    return {r.id for r in risks}


# ---------------------------------------------------------------------------
# R003 — форма с ПДн без согласия
# ---------------------------------------------------------------------------
def test_r003_form_without_consent_fires():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        registered_domain="example.ru",
        forms=[_pd_form_without_consent()],
        has_forms=True,
        has_pd_forms=True,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R003 in _rule_ids(risks)


def test_r003_does_not_fire_when_consent_present():
    rules = _load_rules()
    form = FormResult(
        form_id="html-0",
        page_url="https://example.ru/",
        source="html",
        personal_data_fields=["name", "phone"],
        potentially_personal_data_form=True,
        consent=ConsentInfo(
            checkbox_found=True,
            checkbox_prechecked=False,
            consent_text_found=True,
            privacy_link_found=True,
        ),
    )
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        forms=[form],
        has_forms=True,
        has_pd_forms=True,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R003 not in _rule_ids(risks)


# ---------------------------------------------------------------------------
# R011 — трекеры без cookie-баннера
# ---------------------------------------------------------------------------
def test_r011_trackers_without_cookie_banner_fires():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        trackers=[_analytics_tracker("RU")],
        trackers_present=True,
        cookies_present=True,
        cookie_banner_found=False,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R011 in _rule_ids(risks)


def test_r011_does_not_fire_with_cookie_banner():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        trackers=[_analytics_tracker("RU")],
        trackers_present=True,
        cookies_present=True,
        cookie_banner_found=True,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R011 not in _rule_ids(risks)


# ---------------------------------------------------------------------------
# R001 — формы с ПДн и нет политики
# ---------------------------------------------------------------------------
def test_r001_pd_forms_no_privacy_policy_fires():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        forms=[_pd_form_without_consent()],
        has_forms=True,
        has_pd_forms=True,
        has_privacy_policy=False,
        documents=[],
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R001 in _rule_ids(risks)


def test_r001_does_not_fire_with_privacy_policy_flag():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        forms=[_pd_form_without_consent()],
        has_forms=True,
        has_pd_forms=True,
        has_privacy_policy=True,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R001 not in _rule_ids(risks)


# ---------------------------------------------------------------------------
# R015 — иностранный трекер, трансграничная передача не раскрыта
# ---------------------------------------------------------------------------
def test_r015_foreign_tracker_cross_border_not_disclosed_fires():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        trackers=[_analytics_tracker("foreign")],
        trackers_present=True,
        foreign_trackers_present=True,
        cookies_present=True,
        # Документов с анализом нет -> PP_020 трактуется как not_found.
        documents=[],
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert R015 in _rule_ids(risks)


# ---------------------------------------------------------------------------
# Общие свойства apply_rules
# ---------------------------------------------------------------------------
def test_apply_rules_empty_rules_returns_empty():
    ctx = ScanContext(site_url="https://example.ru")
    assert rule_engine.apply_rules(ctx, []) == []


def test_apply_rules_returns_sorted_by_severity():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        forms=[_pd_form_without_consent()],
        has_forms=True,
        has_pd_forms=True,
        trackers=[_analytics_tracker("foreign")],
        trackers_present=True,
        foreign_trackers_present=True,
        cookies_present=True,
        cookie_banner_found=False,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    assert len(risks) >= 1

    from scanner.models import RISK_LEVEL_ORDER, RiskLevel

    def _rank(risk):
        try:
            level = RiskLevel(risk.risk_level)
        except Exception:
            level = RiskLevel.medium
        return (RISK_LEVEL_ORDER.get(level, 0), risk.score)

    ranks = [_rank(r) for r in risks]
    assert ranks == sorted(ranks, reverse=True)


def test_apply_rules_risk_fields_populated():
    rules = _load_rules()
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        forms=[_pd_form_without_consent()],
        has_forms=True,
        has_pd_forms=True,
        technical=TechnicalResult(https_enabled=True),
    )
    risks = rule_engine.apply_rules(ctx, rules)
    by_id = {r.id: r for r in risks}
    assert R003 in by_id
    r = by_id[R003]
    # Метаданные из YAML должны быть перенесены в Risk.
    assert r.title
    assert r.risk_level in {"low", "medium", "high", "critical"}
    assert r.score > 0
    assert r.report_phrase
