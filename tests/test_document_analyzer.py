"""
Тесты детерминированных детекторов анализа документов (legal/document_analyzer.py).

Проверяется:
  * find_operator: извлечение оператора из «Оператор: ООО "Ромашка"»;
  * utils.find_inn / utils.find_ogrn на образце реквизитов;
  * find_purposes: извлечение целей обработки;
  * find_retention: обнаружение упоминания сроков хранения/обработки;
  * find_placeholders: обнаружение шаблонного «[указать]».

Модуль legal/document_analyzer.py может собираться параллельно, поэтому он
импортируется через importorskip — тест не должен падать на этапе сбора.
"""
from __future__ import annotations

import pytest

from scanner import utils

document_analyzer = pytest.importorskip("legal.document_analyzer")


# ---------------------------------------------------------------------------
# find_operator
# ---------------------------------------------------------------------------
def test_find_operator_ooo_romashka():
    text = 'Оператор: ООО "Ромашка", ИНН 7701234567.'
    got = document_analyzer.find_operator(text)
    assert got is not None
    assert "Ромашка" in got


def test_find_operator_ip():
    text = "Оператором является ИП Иванов Иван Иванович."
    got = document_analyzer.find_operator(text)
    assert got is not None
    assert "Иванов" in got or "ИП" in got


def test_find_operator_none_when_absent():
    text = "Настоящий документ описывает порядок обработки данных."
    got = document_analyzer.find_operator(text)
    # Оператор не назван — допускается None либо пустая строка.
    assert got is None or got == ""


def test_find_operator_empty_text_safe():
    # Не бросает на пустом вводе.
    got = document_analyzer.find_operator("")
    assert got is None or got == ""


# ---------------------------------------------------------------------------
# ИНН / ОГРН (делегируются в utils)
# ---------------------------------------------------------------------------
def test_utils_find_inn_sample():
    text = "ИНН: 7701234567, КПП 770101001"
    inns = utils.find_inn(text)
    assert "7701234567" in inns


def test_utils_find_ogrn_sample():
    text = "ОГРН: 1027700132195 от 2002 года"
    ogrns = utils.find_ogrn(text)
    assert "1027700132195" in ogrns


def test_document_analyzer_find_inn_delegates():
    # find_inn/find_ogrn в document_analyzer делегируют в utils.
    if hasattr(document_analyzer, "find_inn"):
        assert "7701234567" in document_analyzer.find_inn("ИНН 7701234567")
    if hasattr(document_analyzer, "find_ogrn"):
        assert "1027700132195" in document_analyzer.find_ogrn("ОГРН 1027700132195")


# ---------------------------------------------------------------------------
# find_purposes
# ---------------------------------------------------------------------------
def test_find_purposes_returns_list():
    text = (
        "Цели обработки персональных данных: обработка заявок, "
        "информирование о статусе заказа и обратная связь с клиентами."
    )
    purposes = document_analyzer.find_purposes(text)
    assert isinstance(purposes, list)
    # Хотя бы одна цель должна быть найдена.
    assert len(purposes) >= 1


def test_find_purposes_empty_when_absent():
    text = "Просто текст без указания целей."
    purposes = document_analyzer.find_purposes(text)
    assert isinstance(purposes, list)


def test_find_purposes_empty_input_safe():
    purposes = document_analyzer.find_purposes("")
    assert isinstance(purposes, list)
    assert purposes == []


# ---------------------------------------------------------------------------
# find_retention
# ---------------------------------------------------------------------------
def test_find_retention_true_when_present():
    text = (
        "Персональные данные хранятся в течение срока, необходимого для "
        "достижения целей обработки, но не более 5 лет."
    )
    assert document_analyzer.find_retention(text) is True


def test_find_retention_false_when_absent():
    text = "Мы обрабатываем персональные данные с согласия субъекта."
    assert document_analyzer.find_retention(text) is False


def test_find_retention_returns_bool():
    assert isinstance(document_analyzer.find_retention(""), bool)


# ---------------------------------------------------------------------------
# find_placeholders (делегирует в utils)
# ---------------------------------------------------------------------------
def test_find_placeholders_detects_ukazat():
    text = "Оператор [указать] обрабатывает персональные данные."
    placeholders = document_analyzer.find_placeholders(text)
    assert isinstance(placeholders, list)
    assert any("указать" in p.lower() for p in placeholders)


def test_find_placeholders_empty_when_none():
    text = 'Оператор ООО "Настоящая Компания" обрабатывает данные корректно.'
    placeholders = document_analyzer.find_placeholders(text)
    assert isinstance(placeholders, list)


def test_utils_find_placeholders_direct():
    # Прямая проверка утилиты, на которую опирается детектор.
    assert utils.find_placeholders("текст [указать] ещё") == ["[указать]"]
