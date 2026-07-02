"""
Тесты устойчивого обнаружения форм на JS/модальных сайтах.

Проверяется:
  * detect_forms находит форму в HTML модалки БЕЗ тега <form> (source="modal"):
    кластер полей телефон+имя + кнопка -> одна форма, potentially_personal_data_form;
  * detect_forms находит «голую» группу полей на странице без <form>;
  * одиночное поле сбора ПДн (телефон) в модалке достаточно для обнаружения;
  * is_modal_trigger: расширенный список RU-триггеров срабатывает
    («Заказать звонок» / «Оставить заявку» / «Записаться»), а действия
    покупки/оплаты («Купить»/«Оплатить») НЕ считаются триггерами (не кликаются);
  * is_unsafe_click помечает покупку/оплату как небезопасные для автоклика.

Разбор HTML требует bs4, поэтому тесты detect_forms пропускаются, если bs4
недоступен (pytest.importorskip).
"""
from __future__ import annotations

import pytest

from scanner import form_detector, modal_detector


# ---------------------------------------------------------------------------
# is_modal_trigger / is_unsafe_click (bs4 не нужен)
# ---------------------------------------------------------------------------
def test_is_modal_trigger_callback():
    assert modal_detector.is_modal_trigger("Заказать звонок") is True


def test_is_modal_trigger_leave_request():
    assert modal_detector.is_modal_trigger("Оставить заявку") is True


def test_is_modal_trigger_signup():
    assert modal_detector.is_modal_trigger("Записаться") is True


def test_is_modal_trigger_case_and_yo_insensitive():
    # Регистр и ё->е не мешают срабатыванию.
    assert modal_detector.is_modal_trigger("ЗАПИСАТЬСЯ НА ПРИЁМ") is True
    assert modal_detector.is_modal_trigger("получить расчет") is True


def test_is_modal_trigger_new_keywords():
    for text in (
        "Обратный звонок",
        "Оставить заявку",
        "Записаться на приём",
        "Получить консультацию",
        "Бесплатная консультация",
        "Задать вопрос",
        "Рассчитать стоимость",
        "Заказать замер",
        "Вызвать замерщика",
        "Подписаться",
        "Связаться с нами",
        "Перезвоните мне",
        "Жду звонка",
    ):
        assert modal_detector.is_modal_trigger(text) is True, text


def test_buy_pay_are_not_modal_triggers():
    # Покупка/оплата — НЕ триггеры модалки: их нельзя автоматически кликать.
    assert modal_detector.is_modal_trigger("Купить") is False
    assert modal_detector.is_modal_trigger("Оплатить") is False


def test_buy_pay_are_unsafe_clicks():
    assert modal_detector.is_unsafe_click("Купить") is True
    assert modal_detector.is_unsafe_click("Оплатить") is True
    assert modal_detector.is_unsafe_click("Оформить заказ") is True


def test_order_and_pay_stays_excluded_even_with_trigger_word():
    # «Заказать и оплатить» содержит триггер-слово «заказать», но действие
    # покупки должно оставаться исключённым и НЕ считаться триггером модалки.
    assert modal_detector.is_unsafe_click("Заказать и оплатить") is True
    assert modal_detector.is_modal_trigger("Заказать и оплатить") is False


def test_is_modal_trigger_empty_and_garbage_safe():
    assert modal_detector.is_modal_trigger("") is False
    assert modal_detector.is_modal_trigger(None) is False  # type: ignore[arg-type]
    assert modal_detector.is_unsafe_click("") is False


# ---------------------------------------------------------------------------
# detect_forms на HTML модалки без <form> (нужен bs4)
# ---------------------------------------------------------------------------
def test_detect_callback_modal_without_form_tag():
    pytest.importorskip("bs4")
    # Фрагмент модалки обратного звонка БЕЗ обёртки <form>.
    html = (
        '<div class="modal show">'
        '<input name="phone" placeholder="Ваш телефон">'
        '<input name="name" placeholder="Ваше имя">'
        '<button>Отправить заявку</button>'
        '</div>'
    )
    forms = form_detector.detect_forms(
        html, page_url="https://example.ru/", source="modal"
    )
    assert isinstance(forms, list)
    assert len(forms) == 1

    form = forms[0]
    assert form.source == "modal"
    assert form.form_id.startswith("modal-")
    assert form.potentially_personal_data_form is True

    cats = {f.category for f in form.fields}
    assert "phone" in cats
    assert "name" in cats
    assert "phone" in form.personal_data_fields
    assert "name" in form.personal_data_fields


def test_detect_bare_field_group_on_page_without_form_tag():
    pytest.importorskip("bs4")
    # «Голая» группа полей на странице (без <form>): имя + email + кнопка.
    html = (
        '<html><body>'
        '<div class="lead-block">'
        '<input type="text" name="name" placeholder="Ваше имя">'
        '<input type="email" name="email" placeholder="E-mail">'
        '<button type="button">Отправить</button>'
        '</div>'
        '</body></html>'
    )
    forms = form_detector.detect_forms(
        html, page_url="https://example.ru/", source="html"
    )
    assert len(forms) >= 1
    form = forms[0]
    assert form.potentially_personal_data_form is True
    cats = {f.category for f in form.fields}
    assert "name" in cats
    assert "email" in cats


def test_detect_single_phone_callback_modal():
    pytest.importorskip("bs4")
    # Модалка «Перезвоните мне» с единственным полем «телефон» + кнопка.
    html = (
        '<div class="callback-popup">'
        '<input name="phone" type="tel" placeholder="Ваш телефон">'
        '<button>Жду звонка</button>'
        '</div>'
    )
    forms = form_detector.detect_forms(
        html, page_url="https://example.ru/", source="modal"
    )
    assert len(forms) == 1
    form = forms[0]
    assert form.potentially_personal_data_form is True
    assert "phone" in {f.category for f in form.fields}
    assert form.is_callback is True


def test_bare_single_nonpd_input_not_detected_as_form():
    pytest.importorskip("bs4")
    # Одиночное НЕ-ПДн поле (поиск) на обычной странице не должно считаться
    # формой сбора данных: порог для html-источника — 2 поля, если нет ПДн-поля.
    html = (
        '<html><body>'
        '<div class="search-box">'
        '<input type="text" name="q" placeholder="Поиск по сайту">'
        '<button>Найти</button>'
        '</div>'
        '</body></html>'
    )
    forms = form_detector.detect_forms(
        html, page_url="https://example.ru/", source="html"
    )
    assert forms == []


def test_detect_forms_modal_empty_html_safe():
    # Пустой HTML модалки -> [], без исключений.
    assert form_detector.detect_forms("", page_url="https://x.ru/", source="modal") == []
