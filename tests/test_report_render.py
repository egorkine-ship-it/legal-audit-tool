"""
Тесты редизайна PDF-отчёта (reports/html_renderer.py + шаблон/стили).

Отчёт должен быть коротким, продающим документом по 152-ФЗ. Проверяем, что
render_report_html возвращает корректный HTML, который:
  * содержит слово уровня риска (RU) и крупный вердикт;
  * капает балл риска на 100 (при risk_score>100 показывает 100, а не 125/>100);
  * содержит блок ответственности с дисклеймером (из data/liability.yml либо
    встроенного запасного списка);
  * содержит единый раздел ядро-чеклиста ровно ОДИН раз (без повтора таблицы
    на каждый документ — это устраняет «34 страницы» старого отчёта);
  * не печатает полный чек-лист документа на каждый документ;
  * дедуплицирует документы и пропускает неподтверждённые «догадки»;
  * маппит отрасль "auto" -> «не указана»;
  * экранирует имя компании с символом '<'.

Функция render_report_html не должна бросать исключение ни при каких входных
данных (публичный API — безопасные значения по умолчанию).
"""
from __future__ import annotations

import os

import yaml

from reports.html_renderer import render_report_html
from scanner.models import (
    ChecklistItemResult,
    CoreCheckItem,
    DocumentAnalysis,
    DocumentResult,
    Evidence,
    Risk,
    ScanResult,
    TechnicalResult,
    TrackerHit,
)


_PACKAGES_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "service_packages.yml",
)


def _load_packages() -> dict:
    """Загрузить пакеты из data/service_packages.yml (как это делает приложение)."""
    try:
        with open(_PACKAGES_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _StubSettings:
    """Минимальный объект настроек с интерфейсом, ожидаемым рендером."""

    firm_name = "Тестовое бюро"
    logo_path = ""  # логотипа нет -> используется firm_name

    def prices(self):
        return {
            "express_docs": "от 30 000 ₽",
            "full_audit": "от 90 000 ₽",
            "turnkey": "",
            "express_audit": "",
        }


def _rich_result() -> ScanResult:
    """Насыщенный ScanResult: смешанные статусы, риски, 2 документа, трекеры."""
    return ScanResult(
        company_name="ООО <Ромашка>",  # содержит '<' для проверки экранирования
        site_url="https://example.ru",
        industry="auto",               # должно превратиться в «не указана»
        risk_score=125,                # > 100 -> в отчёте должно быть 100
        risk_level="high",
        confidence=72,
        pages_checked=8,
        created_at="2026-07-02",
        core_checklist=[
            CoreCheckItem(id="PP_001", label="Политика опубликована",
                          status="risk", comment="Политика не найдена — признак риска."),
            CoreCheckItem(id="R017", label="HTTPS включён",
                          status="ok", comment="Сайт работает по HTTPS."),
            CoreCheckItem(id="PP_020", label="Трансграничная передача раскрыта",
                          status="unclear", comment="Требует проверки."),
            CoreCheckItem(id="PP_027", label="Спец. категории (медицина)",
                          status="not_applicable", comment="Не применимо."),
        ],
        risks=[
            Risk(id="R001", title="Отсутствует политика обработки ПДн",
                 risk_level="high", score=80,
                 report_phrase="Публичная политика обработки ПДн не обнаружена.",
                 recommendation="Опубликовать политику."),
            Risk(id="R011", title="Отсутствует cookie-баннер",
                 risk_level="medium", score=40,
                 report_phrase="Cookie-баннер на сайте не обнаружен.",
                 recommendation="Добавить cookie-баннер."),
        ],
        trackers=[
            TrackerHit(provider_name="Google Analytics", category="analytics",
                       country_hint="foreign", legal_risk="high",
                       matched_domain="google-analytics.com"),
        ],
        foreign_trackers_found=True,
        technical=TechnicalResult(https_enabled=True, server_country="RU"),
        documents=[
            DocumentResult(
                doc_id="d1", doc_type="privacy_policy",
                url="https://example.ru/policy", is_accessible=True,
                analysis=DocumentAnalysis(
                    overall_completeness=60,
                    checklist_results=[
                        ChecklistItemResult(id="PP_003", label="Оператор идентифицирован",
                                            status="not_found"),
                        ChecklistItemResult(id="PP_015", label="Сроки хранения указаны",
                                            status="unclear"),
                        ChecklistItemResult(id="PP_009", label="Цели обработки перечислены",
                                            status="found"),
                    ],
                ),
            ),
            # Второй уникальный документ (другой тип).
            DocumentResult(
                doc_id="d2", doc_type="cookie_policy",
                url="https://example.ru/cookie", is_accessible=True,
                analysis=DocumentAnalysis(overall_completeness=90),
            ),
            # Дубликат первого (тот же тип+URL) — должен быть отброшен.
            DocumentResult(
                doc_id="d1-dup", doc_type="privacy_policy",
                url="https://example.ru/policy", is_accessible=True,
            ),
            # Неподтверждённая «догадка» — должна быть пропущена в карточках.
            DocumentResult(
                doc_id="g1", doc_type="offer",
                url="https://example.ru/guess", is_accessible=False,
                link_confirmed=False,
            ),
        ],
        executive_summary="Краткое резюме проверки сайта.",
        commercial_offer_text="Готовы помочь привести сайт в соответствие.",
    )


def test_render_returns_nonempty_html():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    assert isinstance(html, str)
    assert html.strip()
    assert html.lstrip().startswith("<!DOCTYPE html>")
    assert "</html>" in html


def test_contains_risk_level_word():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # RU-слово уровня риска для "high" — «высокий».
    assert "высокий" in html


def test_score_capped_at_100():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # Балл 125 должен показываться как 100, а не как 125.
    assert "125" not in html
    # Не должно быть текстового «>100» (в т.ч. экранированного) — балл капнут.
    assert "&gt;100" not in html
    # Крупный вердикт показывает «100 / 100».
    assert "100 <span" in html
    assert "/ 100" in html


def test_liability_block_present_with_disclaimer():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # Заголовок блока ответственности.
    assert "Справочно: возможная ответственность" in html
    # Хотя бы одна крупная сумма из встроенного/файлового списка.
    assert "₽" in html
    # Дисклеймер блока ответственности присутствует (нейтральный тон «справочно»).
    assert "справочно" in html.lower()


def test_core_checklist_section_present_exactly_once():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # Раздел ядро-чеклиста присутствует.
    assert "Основные проверки" in html
    # И присутствует РОВНО ОДИН раз (единая сводная таблица, а не повтор
    # на каждый документ — это устраняет 34-страничный отчёт).
    assert html.count("Основные проверки") == 1
    # Таблица ядро-чеклиста (class="core-table") встречается ровно один раз.
    assert html.count('class="core-table"') == 1


def test_no_per_document_checklist_repetition():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # Метка пункта чек-листа документа не должна дублироваться в каждой карточке.
    # «Оператор идентифицирован» — незакрытый (not_found) пункт: он показывается
    # в краткой карточке документа, но НЕ повторяется как полная таблица.
    assert html.count("Оператор идентифицирован") <= 1
    # Пункт со статусом found НЕ выводится среди «незакрытых» пунктов карточки.
    assert "Цели обработки перечислены" not in html


def test_documents_deduped_and_guesses_skipped():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # Оба уникальных доступных документа показаны (privacy_policy + cookie_policy).
    assert "Политика обработки персональных данных" in html
    assert "Политика cookie" in html
    # Неподтверждённая «догадка» (offer, /guess) — не показана.
    assert "example.ru/guess" not in html
    # Дубликат privacy_policy не создаёт второй карточки: URL встречается только
    # один раз в карточках документов.
    assert html.count("example.ru/policy") == 1


def test_industry_auto_mapped_to_ne_ukazana():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    assert "не указана" in html
    # Сырое «auto» не выводится как значение отрасли.
    assert ">auto<" not in html


def test_company_name_with_angle_bracket_is_escaped():
    html = render_report_html(_rich_result(), _StubSettings(), None)
    # Имя компании с '<' должно быть экранировано, а не вставлено как тег.
    assert "ООО &lt;Ромашка&gt;" in html
    assert "ООО <Ромашка>" not in html


def test_packages_rendered_with_prices():
    html = render_report_html(_rich_result(), _StubSettings(), _load_packages())
    # Пакеты услуг из service_packages.yml с подставленными ценами.
    assert "Экспресс-комплект документов для сайта" in html
    assert "от 30 000 ₽" in html


def test_never_raises_on_empty_result():
    # Пустой результат не должен приводить к исключению.
    html = render_report_html(ScanResult(site_url="https://x.test"), _StubSettings(), None)
    assert isinstance(html, str)
    assert html.strip()


def test_never_raises_on_bad_settings():
    # Некорректный объект настроек (без prices/logo_path) — тоже безопасно.
    html = render_report_html(_rich_result(), object(), None)
    assert isinstance(html, str)
    assert html.strip()
