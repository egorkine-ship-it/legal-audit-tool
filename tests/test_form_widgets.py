"""
Тесты обнаружения встраиваемых форм-виджетов (scanner/form_detector.py).

На многих RU-сайтах статический HTML не содержит ни <form>, ни <input> — форму
рисует скрипт провайдера (Tilda, Bitrix24, amoCRM, Envybox, Marquiz, Flexbe,
JivoSite). Такой сайт всё равно собирает персональные данные, поэтому наличие
маркера провайдера в разметке должно давать FormResult с source="widget" и
potentially_personal_data_form=True.

Проверяется:
  (a) Tilda-контейнер "t-form" без <form> -> виджет-форма (source="widget",
      potentially_personal_data_form=True);
  (b) <script src="...forms.amocrm.ru..."> -> определяется amoCRM-виджет;
  (c) страница без маркеров провайдеров и без форм -> нет виджет-форм;
  (d) обычная <form> продолжает определяться (регрессия базового детектора);
  плюс: детектор виджетов не бросает на мусоре, dedupe по провайдеру, RU-тон.

detect_form_widgets работает на сырой строке HTML и НЕ требует bs4. Тесты (a)/(b)
поэтому не пропускаются при отсутствии bs4; тест (d) требует bs4 для разбора
реальной <form> (pytest.importorskip).
"""
from __future__ import annotations

import pytest

from scanner import form_detector


# ---------------------------------------------------------------------------
# detect_form_widgets (bs4 не нужен)
# ---------------------------------------------------------------------------
def test_widget_tilda_without_form_tag():
    # (a) Tilda-форма без обёртки <form> и без <input>.
    html = '<div class="t-form"><div class="t-form__inputs"></div></div>'

    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    assert isinstance(widgets, list)
    assert len(widgets) == 1

    w = widgets[0]
    assert w.source == "widget"
    assert w.potentially_personal_data_form is True
    assert w.personal_data_fields == ["phone", "name"]
    assert w.form_id == "widget-tilda"
    assert w.page_url == "https://example.ru/"

    # И через основную функцию detect_forms (виджет домешивается туда же).
    forms = form_detector.detect_forms(html, page_url="https://example.ru/")
    widget_forms = [f for f in forms if f.source == "widget"]
    assert len(widget_forms) == 1
    assert widget_forms[0].potentially_personal_data_form is True


def test_widget_amocrm_by_script_src():
    # (b) amoCRM-виджет по src скрипта форм.
    html = '<script src="https://forms.amocrm.ru/forms/assets/js/amoforms.js"></script>'

    forms = form_detector.detect_forms(html, page_url="https://example.ru/")
    widget_forms = [f for f in forms if f.source == "widget"]
    assert len(widget_forms) == 1
    assert widget_forms[0].form_id == "widget-amocrm"
    assert widget_forms[0].potentially_personal_data_form is True


def test_no_widget_markers_no_forms():
    # (c) Страница без маркеров провайдеров и без форм -> нет виджет-форм.
    html = (
        "<html><body>"
        "<h1>О компании</h1>"
        "<p>Мы делаем хорошие вещи с 2010 года.</p>"
        "</body></html>"
    )
    assert form_detector.detect_form_widgets(html) == []

    forms = form_detector.detect_forms(html, page_url="https://example.ru/")
    assert [f for f in forms if f.source == "widget"] == []


def test_plain_form_still_detected():
    # (d) Регрессия: обычная <form> продолжает определяться базовым детектором.
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <form action="/lead" method="post">
        <input type="text" name="name" placeholder="Ваше имя">
        <input type="tel" name="phone" placeholder="Телефон">
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    parsed = [f for f in forms if f.source == "html"]
    assert len(parsed) >= 1
    assert parsed[0].potentially_personal_data_form is True
    cats = {f.category for f in parsed[0].fields}
    assert "phone" in cats
    assert "name" in cats
    # На такой странице маркеров виджетов нет.
    assert [f for f in forms if f.source == "widget"] == []


# ---------------------------------------------------------------------------
# Прочие провайдеры и устойчивость
# ---------------------------------------------------------------------------
def test_widget_bitrix24_detected():
    html = '<div class="b24-window"><script src="/bitrix/js/crm/webform.js"></script></div>'
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    assert len(widgets) == 1
    assert widgets[0].form_id == "widget-bitrix24"


def test_widget_envybox_callback_type():
    html = '<div class="envycallback"></div>'
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    assert len(widgets) == 1
    w = widgets[0]
    assert w.form_id == "widget-envybox"
    assert w.form_type == "callback"
    assert w.is_callback is True


def test_widget_jivosite_detected():
    html = '<script src="//code.jivosite.com/widget/abc"></script>'
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    assert len(widgets) == 1
    assert widgets[0].form_id == "widget-jivosite"
    assert widgets[0].form_type == "other"


def test_widget_case_insensitive():
    # Маркеры ищутся регистронезависимо.
    html = '<div class="T-Form"><span>MARQUIZ</span></div>'
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    keys = {w.form_id for w in widgets}
    assert "widget-tilda" in keys
    assert "widget-marquiz" in keys


def test_widget_dedupe_single_per_provider():
    # Несколько маркеров одного провайдера -> ровно один результат по провайдеру.
    html = (
        '<div class="t-form"></div>'
        '<div class="t396__artboard"></div>'
        '<script src="https://tilda.cc/project/xxx"></script>'
    )
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    tilda = [w for w in widgets if w.form_id == "widget-tilda"]
    assert len(tilda) == 1


def test_widget_multiple_providers():
    html = '<div class="t-form"></div><div class="b24-form"></div>'
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    keys = {w.form_id for w in widgets}
    assert "widget-tilda" in keys
    assert "widget-bitrix24" in keys
    assert len(widgets) == 2


def test_widget_empty_and_garbage_safe():
    # Пустой/мусорный ввод -> [], без исключений.
    assert form_detector.detect_form_widgets("") == []
    assert form_detector.detect_form_widgets(None) == []  # type: ignore[arg-type]
    assert isinstance(form_detector.detect_form_widgets("<div>привет</div>"), list)


def test_widget_evidence_no_alarming_tone():
    # Юридический тон: evidence не содержит запретных слов.
    html = '<div class="t-form"></div>'
    widgets = form_detector.detect_form_widgets(html, page_url="https://example.ru/")
    assert len(widgets) == 1
    quote = (widgets[0].evidence.quote or "").lower()
    for bad in ("нарушение", "штраф", "незаконно", "иначе ркн"):
        assert bad not in quote


def test_widget_coexists_with_parsed_form():
    # Реальная <form> и виджет того же провайдера сосуществуют (разные source).
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <div class="t-form">
        <form action="/lead">
          <input type="tel" name="phone" placeholder="Телефон">
          <button type="submit">Отправить</button>
        </form>
      </div>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    sources = {f.source for f in forms}
    assert "html" in sources
    assert "widget" in sources
    # Виджет по провайдеру ровно один (dedupe).
    assert len([f for f in forms if f.form_id == "widget-tilda"]) == 1
