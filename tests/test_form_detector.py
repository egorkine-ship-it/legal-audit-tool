"""
Тесты детектора форм и механики согласия (scanner/form_detector.py).

Проверяется:
  * classify_field: телефон / email / имя (по имени поля и HTML-типу);
  * detect_forms: форма phone+email определяется как potentially_personal_data_form;
  * отмеченный чекбокс согласия -> consent.checkbox_prechecked is True;
  * форма с «Нажимая кнопку …» без чекбокса -> consent.button_acceptance_only is True.

Разбор HTML требует bs4, поэтому тесты detect_forms/analyze_consent пропускаются,
если bs4 недоступен (pytest.importorskip).
"""
from __future__ import annotations

import pytest

from scanner import form_detector
from scanner.models import PERSONAL_DATA_FIELD_CATEGORIES


# ---------------------------------------------------------------------------
# classify_field (bs4 не нужен)
# ---------------------------------------------------------------------------
def test_classify_field_phone_by_name():
    assert form_detector.classify_field(name="phone") == "phone"


def test_classify_field_phone_by_type():
    # HTML type=tel -> phone, даже если имя нейтральное.
    assert form_detector.classify_field(name="contact", field_type="tel") == "phone"


def test_classify_field_phone_russian():
    assert form_detector.classify_field(name="", label="Телефон") == "phone"


def test_classify_field_email_by_name():
    assert form_detector.classify_field(name="email") == "email"


def test_classify_field_email_by_type():
    assert form_detector.classify_field(name="user_contact", field_type="email") == "email"


def test_classify_field_name_by_name():
    assert form_detector.classify_field(name="name") == "name"


def test_classify_field_name_russian_label():
    assert form_detector.classify_field(name="", label="Имя") == "name"


def test_classify_field_file_by_type():
    assert form_detector.classify_field(name="doc", field_type="file") == "file"


def test_classify_field_unknown_default():
    assert form_detector.classify_field(name="xyz123", field_type="text") == "unknown"


def test_classify_field_consent_checkbox():
    # Чекбокс рядом со словом «согласие» -> consent.
    got = form_detector.classify_field(
        name="agree", field_type="checkbox", label="Я даю согласие на обработку"
    )
    assert got == "consent"


def test_classify_field_returns_str_never_raises():
    # Не бросает даже на «мусорных» аргументах.
    assert isinstance(form_detector.classify_field(), str)
    assert isinstance(form_detector.classify_field(name="", field_type="", label=""), str)


# ---------------------------------------------------------------------------
# detect_forms (нужен bs4)
# ---------------------------------------------------------------------------
def test_detect_forms_phone_email_is_personal_data():
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <form action="/lead" method="post">
        <label for="nm">Имя</label>
        <input type="text" id="nm" name="name" placeholder="Ваше имя" required>
        <input type="tel" name="phone" placeholder="Телефон">
        <input type="email" name="email" placeholder="E-mail">
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    assert isinstance(forms, list)
    assert len(forms) >= 1

    form = forms[0]
    assert form.potentially_personal_data_form is True

    cats = {f.category for f in form.fields}
    assert "phone" in cats
    assert "email" in cats

    # personal_data_fields — только категории из PERSONAL_DATA_FIELD_CATEGORIES.
    assert set(form.personal_data_fields).issubset(PERSONAL_DATA_FIELD_CATEGORIES)
    assert "phone" in form.personal_data_fields
    assert "email" in form.personal_data_fields

    # form_id формируется как "{source}-{index}".
    assert form.form_id == "html-0"
    assert form.page_url == "https://example.ru/"


def test_detect_forms_prechecked_consent_checkbox():
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <form action="/lead" method="post">
        <input type="text" name="name" placeholder="Имя">
        <input type="tel" name="phone" placeholder="Телефон">
        <label>
          <input type="checkbox" name="agree" checked>
          Я даю согласие на обработку персональных данных
        </label>
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    assert len(forms) >= 1
    consent = forms[0].consent
    assert consent.checkbox_found is True
    assert consent.checkbox_prechecked is True


def test_detect_forms_unchecked_consent_checkbox():
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <form action="/lead" method="post">
        <input type="text" name="name" placeholder="Имя">
        <input type="tel" name="phone" placeholder="Телефон">
        <label>
          <input type="checkbox" name="agree">
          Я даю согласие на обработку персональных данных
        </label>
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    assert len(forms) >= 1
    consent = forms[0].consent
    assert consent.checkbox_found is True
    # Чекбокс найден, но не отмечен -> False (не None).
    assert consent.checkbox_prechecked is False


def test_detect_forms_button_acceptance_only():
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <form action="/lead" method="post">
        <input type="text" name="name" placeholder="Имя">
        <input type="tel" name="phone" placeholder="Телефон">
        <p>Нажимая кнопку «Отправить», вы соглашаетесь на обработку
           персональных данных.</p>
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    assert len(forms) >= 1
    consent = forms[0].consent
    assert consent.checkbox_found is False
    assert consent.button_acceptance_only is True


def test_detect_forms_privacy_link_found():
    pytest.importorskip("bs4")
    html = """
    <html><body>
      <form action="/lead" method="post">
        <input type="text" name="name" placeholder="Имя">
        <input type="tel" name="phone" placeholder="Телефон">
        <label>
          <input type="checkbox" name="agree">
          Согласен с
          <a href="/privacy-policy">политикой обработки персональных данных</a>
        </label>
        <button type="submit">Отправить</button>
      </form>
    </body></html>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="html")
    assert len(forms) >= 1
    consent = forms[0].consent
    assert consent.privacy_link_found is True
    assert any("privacy" in u for u in consent.privacy_link_urls)


def test_detect_forms_source_prefix_in_id():
    pytest.importorskip("bs4")
    html = """
    <form>
      <input type="text" name="name" placeholder="Имя">
      <input type="tel" name="phone" placeholder="Телефон">
      <button type="submit">Ок</button>
    </form>
    """
    forms = form_detector.detect_forms(html, page_url="https://example.ru/", source="modal")
    assert len(forms) >= 1
    assert forms[0].form_id.startswith("modal-")
    assert forms[0].source == "modal"


def test_detect_forms_empty_html_returns_list():
    # Пустой HTML -> пустой список, без исключений.
    assert form_detector.detect_forms("", page_url="https://example.ru/") == []


# ---------------------------------------------------------------------------
# analyze_consent напрямую
# ---------------------------------------------------------------------------
def test_analyze_consent_none_scope_safe():
    # None -> пустой ConsentInfo, без исключений.
    info = form_detector.analyze_consent(None, None, "https://example.ru/")
    assert info.checkbox_found is False
    assert info.checkbox_prechecked is None


def test_analyze_consent_prechecked_via_soup():
    bs4 = pytest.importorskip("bs4")
    from bs4 import BeautifulSoup

    html = """
    <form>
      <input type="checkbox" name="agree" checked>
      Я даю согласие на обработку персональных данных
    </form>
    """
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")
    form_el = soup.find("form")
    info = form_detector.analyze_consent(form_el, soup, "https://example.ru/")
    assert info.checkbox_found is True
    assert info.checkbox_prechecked is True
    assert info.consent_text_found is True
