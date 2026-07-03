from __future__ import annotations

import json

from llm.llm_client import _facts_for_texts
from legal.pd_rules import build_fact_bundle, build_pd_findings, commercial_score
from reports.teaser_renderer import render_teaser_html
from scanner.models import (
    CoreCheckItem,
    DocumentResult,
    Evidence,
    FormResult,
    Risk,
    ScanResult,
    TrackerHit,
)


class _Settings:
    firm_name = "Nexora Legal"
    firm_email = "team@nexora.legal"
    firm_phone = "+7 000 000-00-00"
    firm_website = "https://nexora.legal"
    firm_contacts = ""
    logo_path = ""

    def prices(self):
        return {
            "express_docs": "от 45 000 ₽",
            "full_audit": "от 120 000 ₽",
            "turnkey": "по запросу",
        }


_PACKAGES = {
    "packages": [
        {
            "id": "express_docs",
            "price_key": "express_docs",
            "title": "Комплект документов",
            "duration": "3 дня",
            "description": "Политика, согласия и cookie-блок.",
            "items": [],
        },
        {
            "id": "full_audit",
            "price_key": "full_audit",
            "title": "Полный аудит",
            "duration": "7 дней",
            "description": "Проверка документов и механики сайта.",
            "items": [],
        },
    ],
    "order": ["express_docs", "full_audit"],
}


def _result() -> ScanResult:
    return ScanResult(
        scan_id="scan-test",
        created_at="2026-07-03T10:00:00",
        company_name="ООО Аскона",
        site_url="https://example.ru",
        final_url="https://example.ru",
        risk_score=100,
        risk_level="critical",
        confidence=78,
        pages_checked=6,
        forms=[
            FormResult(
                form_id="f1",
                page_url="https://example.ru",
                potentially_personal_data_form=True,
                personal_data_fields=["name", "phone"],
            )
        ],
        documents=[
            DocumentResult(
                doc_id="d1",
                doc_type="privacy_policy",
                url="https://example.ru/policy",
                is_accessible=True,
                link_confirmed=True,
                text="VERY_LONG_DOCUMENT_TEXT " * 1000,
                text_length=24000,
            )
        ],
        trackers=[
            TrackerHit(
                provider_name="Google Tag Manager",
                category="analytics",
                country_hint="foreign",
                matched_domain="googletagmanager.com",
            )
        ],
        foreign_trackers_found=True,
        cookie_banner_found=False,
        core_checklist=[
            CoreCheckItem(
                id="PP_001",
                label="Политика опубликована",
                status="risk",
                risk_level="critical",
                comment="Политика не подтверждена.",
            ),
            CoreCheckItem(
                id="R003",
                label="Согласие у форм",
                status="risk",
                risk_level="high",
                comment="Форма с ПДн без отдельного согласия.",
            ),
            CoreCheckItem(
                id="PP_020",
                label="Трансграничная передача раскрыта",
                status="risk",
                risk_level="high",
                comment="Иностранные сервисы требуют проверки.",
            ),
            CoreCheckItem(
                id="Cookie_001",
                label="Cookie-баннер найден",
                status="risk",
                risk_level="high",
                comment="Cookie-баннер не найден.",
            ),
        ],
        risks=[
            Risk(
                id="R001_NO_PRIVACY_POLICY",
                title="Политика обработки ПДн не найдена",
                risk_level="critical",
                score=35,
                evidence=Evidence(page_url="https://example.ru", quote="Форма заявки: имя, телефон"),
                recommendation="Опубликовать политику обработки ПДн.",
                report_phrase="Политика обработки ПДн не подтверждена.",
            ),
            Risk(
                id="R003_FORM_NO_SEPARATE_CONSENT",
                title="Нет отдельного согласия у формы",
                risk_level="high",
                score=25,
                evidence=Evidence(page_url="https://example.ru", quote="Кнопка отправки заявки без чекбокса"),
                recommendation="Добавить отдельный чекбокс согласия.",
            ),
            Risk(
                id="R015_FOREIGN_TRACKERS_CROSS_BORDER_NOT_DISCLOSED",
                title="Иностранные сервисы требуют проверки",
                risk_level="high",
                score=25,
                evidence=Evidence(page_url="https://example.ru", quote="googletagmanager.com"),
                recommendation="Проверить раскрытие трансграничной передачи.",
            ),
        ],
    )


def test_build_pd_findings_maps_core_risks_to_commercial_items():
    findings = build_pd_findings(_result())
    ids = [f.id for f in findings]

    assert "PD-01" in ids
    assert "PD-16" in ids
    assert "PD-11" in ids
    assert all("признак" in f.what_found.lower() or f.what_found for f in findings)


def test_commercial_score_is_capped_and_level_computed():
    score, level = commercial_score(build_pd_findings(_result()))

    assert score <= 150
    assert level in {"low", "medium", "high", "critical"}


def test_fact_bundle_excludes_full_document_text():
    bundle = build_fact_bundle(_result(), _Settings())
    dumped = json.dumps(bundle, ensure_ascii=False)

    assert "VERY_LONG_DOCUMENT_TEXT" not in dumped
    assert len(dumped) < 12000
    assert bundle["top_findings"]
    assert bundle["hidden_findings_count"] >= 0


def test_llm_client_text_facts_use_compact_bundle():
    facts = _facts_for_texts(_result(), None, _Settings(), _PACKAGES)
    dumped = json.dumps(facts, ensure_ascii=False)

    assert "VERY_LONG_DOCUMENT_TEXT" not in dumped
    assert len(dumped) < 14000
    assert facts["top_risks"]


def test_teaser_html_contains_short_commercial_structure():
    html = render_teaser_html(_result(), _Settings(), _PACKAGES)

    assert "Краткая выжимка" in html
    assert "Приоритетные зоны риска" in html
    assert "ООО Аскона" in html
    assert "Nexora Legal" in html
    assert "Комплект документов" in html
    assert "Полный аудит" in html
    assert "до 700 000 ₽" in html
    assert "VERY_LONG_DOCUMENT_TEXT" not in html


def test_teaser_html_uses_safe_legal_tone():
    html = render_teaser_html(_result(), _Settings(), _PACKAGES).lower()

    forbidden = [
        "вы нарушаете",
        "нарушение установлено",
        "сайт незаконен",
        "штраф неизбежен",
        "вам грозит штраф",
    ]
    for phrase in forbidden:
        assert phrase not in html
    assert "требуют проверки юристом" in html
