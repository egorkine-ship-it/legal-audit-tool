"""
Acceptance-тесты качества HTML/PDF-отчёта.

PDF генерируется из HTML, поэтому здесь проверяем самый важный слой: данные
попадают в продающий отчёт аккуратно, без запрещённых юридических формулировок,
без раздутого score и без небезопасной вставки пользовательского текста.
"""
from __future__ import annotations

from reports.html_renderer import render_report_html
from scanner.models import CoreCheckItem, DocumentResult, Evidence, Risk, ScanResult


class _Settings:
    firm_name = "Nexora Legal"
    firm_email = "team@example.com"
    firm_phone = "+7 000 000-00-00"
    firm_website = "https://example.com"
    logo_path = ""

    def prices(self):
        return {
            "express_docs": "от 30 000 ₽",
            "full_audit": "от 90 000 ₽",
            "turnkey": "от 150 000 ₽",
            "express_audit": "от 20 000 ₽",
        }


def _result() -> ScanResult:
    return ScanResult(
        company_name='ООО "<Тест>"',
        site_url="https://fixture.example",
        final_url="https://fixture.example",
        industry="auto",
        risk_score=143,
        risk_level="critical",
        confidence=68,
        pages_checked=6,
        created_at="2026-07-03",
        core_checklist=[
            CoreCheckItem(
                id="PP_001",
                label="Политика ПДн опубликована",
                status="risk",
                risk_level="high",
                comment="Публичная политика не обнаружена — признак риска.",
                evidence="Форма заявки найдена, ссылка на политику рядом не обнаружена.",
            ),
            CoreCheckItem(
                id="R017",
                label="HTTPS включён",
                status="ok",
                risk_level="low",
                comment="Сайт открывается по HTTPS.",
            ),
        ],
        documents=[
            DocumentResult(
                doc_id="d1",
                doc_type="privacy_policy",
                url="https://fixture.example/privacy",
                is_accessible=True,
                link_confirmed=True,
            )
        ],
        risks=[
            Risk(
                id="R001_NO_PRIVACY_POLICY",
                title="Политика обработки ПДн не найдена",
                risk_level="high",
                score=35,
                page_url="https://fixture.example",
                evidence=Evidence(page_url="https://fixture.example", quote="Форма заявки найдена."),
                report_phrase="На сайте обнаружена форма заявки, политика ПДн не найдена.",
                recommendation="Проверить и актуализировать документы сайта.",
            )
        ],
        executive_summary="Автоматическая проверка выявила признаки возможного несоответствия.",
        commercial_offer_text="Предлагаем проверить документы и механику получения согласий.",
    )


def test_report_uses_careful_legal_tone_and_escapes_user_content():
    html = render_report_html(_result(), _Settings(), None)
    low = html.lower()

    forbidden = [
        "вы нарушаете закон",
        "нарушение 152-фз установлено",
        "у вас точно незаконная обработка",
        "вам грозит штраф",
        "сайт незаконен",
        "штраф неизбежен",
    ]
    for phrase in forbidden:
        assert phrase not in low

    assert "признаки возможного несоответствия" in low or "признак риска" in low
    assert "ООО &#34;&lt;Тест&gt;&#34;" in html or "ООО &quot;&lt;Тест&gt;&quot;" in html
    assert 'ООО "<Тест>"' not in html


def test_report_caps_score_and_keeps_liability_as_reference_block():
    html = render_report_html(_result(), _Settings(), None)
    low = html.lower()

    assert "143" not in html
    assert "&gt;100" not in html
    assert "100 <span" in html
    assert "/ 100" in html
    assert "справочно" in low
    assert "₽" in html
    assert "не является утверждением" in low or "не являются утверждением" in low or "не следует трактовать как утверждение" in low


def test_report_remains_compact_with_single_core_checklist_section():
    html = render_report_html(_result(), _Settings(), None)

    assert html.count("Основные проверки") == 1
    assert html.count('class="core-table"') == 1
    assert "не указана" in html
    assert ">auto<" not in html
