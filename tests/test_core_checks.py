"""
Тесты ядро-чеклиста (legal/core_checks.py).

Проверяются четыре синтетических сценария на детерминированных фикстурах:
  * «рисковый» сайт — нет политики, предустановленный чекбокс, нет HTTPS,
    нет cookie-баннера, маркетинговые cookies до согласия;
  * «чистый» сайт — доступная политика, найденные пункты чек-листа,
    корректные механизмы согласия;
  * «пустой» сайт — без форм, документов, cookies (not_applicable);
  * политика только с подтверждённой ссылкой (link_confirmed) без текста —
    PP_001 получает вердикт unclear.

Дополнительно проверяются инварианты: всегда ровно 21 пункт, фиксированный
порядок, отсутствие исключений даже на некорректном входе.
"""
from __future__ import annotations

from legal.core_checks import compute_core_checks
from scanner.models import (
    ChecklistItemResult,
    ConsentInfo,
    DocumentAnalysis,
    DocumentResult,
    FormResult,
    ScanContext,
    ScanResult,
    TechnicalResult,
)

# Ожидаемый фиксированный порядок пунктов ядро-чеклиста.
EXPECTED_IDS = [
    "PP_001", "PP_002", "PP_003", "PP_004", "PP_009", "PP_011", "PP_012",
    "PP_015", "PP_018", "PP_019", "PP_020", "PP_024", "PP_031", "PP_032",
    "Consent_011", "R003", "R017", "Cookie_001", "Cookie_006", "PP_027",
    "R019",
]

VALID_STATUSES = {"ok", "risk", "unclear", "not_applicable"}


# ---------------------------------------------------------------------------
# Хелперы построения фикстур
# ---------------------------------------------------------------------------
def _by_id(items):
    """Словарь {id: CoreCheckItem} для удобных ассертов."""
    return {item.id: item for item in items}


def _pd_form(consent: ConsentInfo, page_url: str = "https://example.ru/") -> FormResult:
    """Форма, собирающая ПДн, с заданной механикой согласия."""
    return FormResult(
        form_id="html-0",
        page_url=page_url,
        source="html",
        personal_data_fields=["name", "phone"],
        potentially_personal_data_form=True,
        consent=consent,
    )


def _accessible_policy_doc() -> DocumentResult:
    """Доступная политика обработки ПДн с извлечённым текстом."""
    return DocumentResult(
        doc_id="doc-1",
        doc_type="privacy_policy",
        url="https://example.ru/privacy",
        is_accessible=True,
        link_confirmed=True,
        discovered_by="anchor",
        text="Политика обработки персональных данных ООО «Пример». ИНН 7700000000.",
        text_length=70,
    )


def _found_item(item_id: str) -> ChecklistItemResult:
    return ChecklistItemResult(
        id=item_id,
        label=item_id,
        status="found",
        evidence_quote="Цитата из документа для " + item_id,
        comment="Соответствующая формулировка обнаружена в тексте документа.",
    )


def _clean_policy_analysis() -> DocumentAnalysis:
    """Анализ политики, где все интересующие пункты найдены."""
    ids = [
        "PP_003", "PP_004", "PP_009", "PP_011", "PP_012",
        "PP_015", "PP_018", "PP_019", "PP_020", "PP_024",
    ]
    return DocumentAnalysis(
        document_type="privacy_policy",
        document_url="https://example.ru/privacy",
        checklist_results=[_found_item(i) for i in ids],
    )


# ---------------------------------------------------------------------------
# Сценарий (a): «рисковый» сайт
# ---------------------------------------------------------------------------
def _full_risk_fixtures():
    consent = ConsentInfo(
        checkbox_found=True,
        checkbox_prechecked=True,   # предустановленный чекбокс
        consent_text_found=True,
        privacy_link_found=False,
    )
    ctx = ScanContext(
        site_url="http://example.ru",
        final_url="http://example.ru",
        registered_domain="example.ru",
        forms=[_pd_form(consent, page_url="http://example.ru/")],
        has_forms=True,
        has_pd_forms=True,
        trackers_present=True,
        cookies_present=True,
        cookie_banner_found=False,
        marketing_cookies_before_consent=True,
        technical=TechnicalResult(https_enabled=False),
    )
    result = ScanResult(site_url="http://example.ru")
    return ctx, result


def test_full_risk_site_returns_21_items():
    ctx, result = _full_risk_fixtures()
    items = compute_core_checks(ctx, result)
    assert len(items) == 21
    assert [i.id for i in items] == EXPECTED_IDS


def test_full_risk_site_risk_statuses():
    ctx, result = _full_risk_fixtures()
    by_id = _by_id(compute_core_checks(ctx, result))
    assert by_id["PP_001"].status == "risk"        # политики нет, ПДн-формы есть
    assert by_id["Consent_011"].status == "risk"   # предустановленный чекбокс
    assert by_id["R017"].status == "risk"          # нет HTTPS
    assert by_id["Cookie_001"].status == "risk"    # cookies есть, баннера нет
    assert by_id["Cookie_006"].status == "risk"    # маркетинговые cookies до согласия


def test_full_risk_site_statuses_valid_and_source_auto():
    ctx, result = _full_risk_fixtures()
    items = compute_core_checks(ctx, result)
    for item in items:
        assert item.status in VALID_STATUSES
        assert item.source == "auto"
        assert item.comment  # у каждого пункта есть комментарий
        assert len(item.evidence) <= 200


# ---------------------------------------------------------------------------
# Сценарий (b): «чистый» сайт
# ---------------------------------------------------------------------------
def _clean_fixtures():
    consent = ConsentInfo(
        checkbox_found=True,
        checkbox_prechecked=False,
        consent_text_found=True,
        privacy_link_found=True,
        privacy_link_urls=["https://example.ru/privacy"],
    )
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        registered_domain="example.ru",
        forms=[_pd_form(consent)],
        has_forms=True,
        has_pd_forms=True,
        trackers_present=True,
        cookies_present=True,
        cookie_banner_found=True,
        marketing_cookies_before_consent=False,
        medical=False,
        newsletter_form=False,
        has_privacy_policy=True,
        technical=TechnicalResult(https_enabled=True),
    )
    result = ScanResult(
        site_url="https://example.ru",
        documents=[_accessible_policy_doc()],
        document_checklists=[_clean_policy_analysis()],
    )
    return ctx, result


def test_clean_site_ok_statuses():
    ctx, result = _clean_fixtures()
    by_id = _by_id(compute_core_checks(ctx, result))
    ok_ids = [
        "PP_001", "PP_002", "PP_003", "PP_004", "PP_009", "PP_011", "PP_012",
        "PP_015", "PP_018", "PP_019", "PP_020", "PP_024", "PP_031", "PP_032",
        "Consent_011", "R003", "R017", "Cookie_001", "Cookie_006",
    ]
    for item_id in ok_ids:
        assert by_id[item_id].status == "ok", (
            "Ожидался ok у %s, получен %s" % (item_id, by_id[item_id].status)
        )
    # Медицины и подписки нет — пункты не применимы.
    assert by_id["PP_027"].status == "not_applicable"
    assert by_id["R019"].status == "not_applicable"


def test_clean_site_evidence_from_checklist():
    ctx, result = _clean_fixtures()
    by_id = _by_id(compute_core_checks(ctx, result))
    # Цитата из пункта чек-листа переносится в ядро-чеклист.
    assert "PP_009" in by_id["PP_009"].evidence


# ---------------------------------------------------------------------------
# Сценарий (c): «пустой» сайт (без форм, документов, cookies)
# ---------------------------------------------------------------------------
def _empty_fixtures():
    ctx = ScanContext(
        site_url="https://landing.ru",
        final_url="https://landing.ru",
        registered_domain="landing.ru",
        technical=TechnicalResult(https_enabled=True),
    )
    result = ScanResult(site_url="https://landing.ru")
    return ctx, result


def test_empty_site_not_applicable_where_applicable():
    ctx, result = _empty_fixtures()
    items = compute_core_checks(ctx, result)
    assert len(items) == 21
    by_id = _by_id(items)
    na_ids = [
        "PP_001", "PP_002", "PP_003", "PP_004", "PP_009", "PP_011", "PP_012",
        "PP_015", "PP_018", "PP_019", "PP_024", "PP_031", "PP_032",
        "Consent_011", "R003", "Cookie_001", "Cookie_006", "PP_027", "R019",
    ]
    for item_id in na_ids:
        assert by_id[item_id].status == "not_applicable", (
            "Ожидался not_applicable у %s, получен %s" % (item_id, by_id[item_id].status)
        )
    # HTTPS включён — единственный содержательный ok.
    assert by_id["R017"].status == "ok"


# ---------------------------------------------------------------------------
# Сценарий (d): политика только с link_confirmed (текст не извлечён)
# ---------------------------------------------------------------------------
def _link_confirmed_fixtures():
    doc = DocumentResult(
        doc_id="doc-1",
        doc_type="privacy_policy",
        url="https://example.ru/privacy",
        is_accessible=False,
        link_confirmed=True,
        discovered_by="anchor",
        text="",
        text_extraction_failed=True,
    )
    ctx = ScanContext(
        site_url="https://example.ru",
        final_url="https://example.ru",
        registered_domain="example.ru",
        forms=[_pd_form(ConsentInfo(privacy_link_found=True))],
        has_forms=True,
        has_pd_forms=True,
        technical=TechnicalResult(https_enabled=True),
    )
    result = ScanResult(site_url="https://example.ru", documents=[doc])
    return ctx, result


def test_link_confirmed_only_policy_unclear():
    ctx, result = _link_confirmed_fixtures()
    by_id = _by_id(compute_core_checks(ctx, result))
    assert by_id["PP_001"].status == "unclear"
    assert "не извлечён" in by_id["PP_001"].comment
    # Принадлежность политики тоже нельзя подтвердить без текста.
    assert by_id["PP_032"].status == "unclear"


# ---------------------------------------------------------------------------
# Инварианты: никогда не бросает, всегда 21 пункт
# ---------------------------------------------------------------------------
def test_never_raises_on_empty_models():
    items = compute_core_checks(ScanContext(), ScanResult())
    assert len(items) == 21
    assert [i.id for i in items] == EXPECTED_IDS


def test_never_raises_on_none_input():
    items = compute_core_checks(None, None)  # type: ignore[arg-type]
    assert len(items) == 21
    for item in items:
        assert item.status in VALID_STATUSES
