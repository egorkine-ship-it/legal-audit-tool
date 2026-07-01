"""
Детектор форм на странице и механики согласия рядом с ними.

Задачи модуля:
  * найти формы (тег <form> и эвристические группы полей без обёртки <form>);
  * классифицировать поля по категориям (name/phone/email/... — см. FIELD_CATEGORIES);
  * определить, собирает ли форма персональные данные;
  * определить тип формы (заявка/подписка/заказ/медицина/HR/дети);
  * проанализировать механику согласия рядом с формой (чекбокс, ссылка на
    политику, «нажимая кнопку …» и т.п.).

Юридический тон: модуль НЕ утверждает нарушение. Он лишь фиксирует признаки,
которые затем нейтрально интерпретируются rule_engine / отчётом.

Совместимость: Python 3.9 (Optional/List/Dict, без синтаксиса `X | None`).
Тяжёлая зависимость bs4 импортируется внутри функций (guarded): при её
отсутствии detect_forms/analyze_consent возвращают безопасные значения, а модуль
всё равно импортируется. Ни одна публичная функция не бросает исключений.
"""
from __future__ import annotations

from typing import Any, List, Optional

from scanner import utils
from scanner.models import (
    ConsentInfo,
    Evidence,
    FIELD_CATEGORIES,
    FormField,
    FormResult,
    PERSONAL_DATA_FIELD_CATEGORIES,
)


# ---------------------------------------------------------------------------
# Ключевые слова по категориям полей.
# ---------------------------------------------------------------------------
FIELD_PATTERNS = {
    "name": [
        "name", "fio", "fname", "lname", "firstname", "lastname", "surname",
        "имя", "фамилия", "отчество", "фио", "ваше имя", "как вас зовут",
        "контактное лицо", "имя и фамилия",
    ],
    "phone": [
        "phone", "tel", "telephone", "mobile", "whatsapp", "телефон",
        "тел", "номер телефона", "моб", "мобильный", "контактный телефон",
        "номер",
    ],
    "email": [
        "email", "e-mail", "mail", "почта", "эл. почта", "электронная почта",
        "электронный адрес", "e mail",
    ],
    "address": [
        "address", "city", "street", "zip", "postcode", "region",
        "адрес", "город", "улица", "индекс", "регион", "область",
        "населенный пункт", "дом", "квартира",
    ],
    "birthdate": [
        "birth", "birthday", "birthdate", "bday", "dob", "date_of_birth",
        "дата рождения", "день рождения", "год рождения", "возраст",
    ],
    "message": [
        "message", "comment", "question", "text", "msg", "description",
        "comments", "feedback", "detail",
        "сообщение", "комментарий", "вопрос", "текст", "опишите",
        "ваше сообщение", "ваш вопрос", "пожелания", "детали",
    ],
    "file": [
        "file", "upload", "attach", "attachment", "resume", "cv", "document",
        "файл", "загрузить", "прикрепить", "вложение", "резюме", "документ",
        "фото", "скан",
    ],
    "passport": [
        "passport", "snils", "паспорт", "снилс", "серия паспорта",
        "номер паспорта", "паспортные данные", "документ удостоверяющий",
    ],
    "inn": [
        "inn", "ogrn", "kpp", "инн", "огрн", "кпп", "реквизиты",
        "расчетный счет", "бик",
    ],
    "company": [
        "company", "organization", "org", "firm", "brand",
        "компания", "организация", "название компании", "юридическое лицо",
        "наименование организации", "должность", "position",
    ],
    "medical": [
        "diagnosis", "symptom", "clinic", "doctor", "appointment",
        "диагноз", "симптом", "жалоб", "заболевание", "болезнь", "здоровье",
        "к врачу", "запись к врачу", "прием врача", "анализ", "медицин",
        "клиник", "лечен",
    ],
    "children": [
        "child", "children", "kid", "kids", "son", "daughter",
        "ребенок", "ребенка", "дети", "детей", "сын", "дочь",
        "имя ребенка", "возраст ребенка", "школьник", "класс ученика",
    ],
}


def _norm(value: Optional[str]) -> str:
    """Нормализованная строка для поиска ключевых слов (безопасно)."""
    if not value:
        return ""
    try:
        return utils.normalize_for_search(str(value))
    except Exception:
        return str(value).lower()


def classify_field(
    name: str = "",
    field_type: str = "",
    label: str = "",
    placeholder: str = "",
) -> str:
    """Определить категорию поля формы.

    Сначала учитываем HTML-тип (type=email/tel/file/date/checkbox), затем ищем
    ключевые слова FIELD_PATTERNS в объединённой строке name+label+placeholder.
    Возвращает одну из FIELD_CATEGORIES; по умолчанию "unknown". Не бросает.
    """
    try:
        ftype = (field_type or "").strip().lower()
        haystack = _norm(" ".join(p for p in (name, label, placeholder) if p))

        # --- 1. По HTML-типу поля ---
        if ftype == "email":
            return "email"
        if ftype == "tel":
            return "phone"
        if ftype == "file":
            return "file"
        if ftype == "date":
            # Дата, если это похоже на дату рождения — birthdate, иначе other.
            if utils.contains_any(haystack, FIELD_PATTERNS["birthdate"]):
                return "birthdate"
            return "other"
        if ftype in ("checkbox", "radio"):
            # Чекбокс/радио: согласие — если рядом «согласие»-слова, иначе other.
            consent_kw = [
                "согласие", "согласен", "согласна", "принимаю", "ознакомлен",
                "consent", "agree", "политик", "персональн", "обработк",
                "рассылк", "оферт",
            ]
            if utils.contains_any(haystack, consent_kw):
                return "consent"
            return "other"

        # --- 2. По ключевым словам name/label/placeholder ---
        # Порядок важен: более специфичные категории раньше общих (например,
        # birthdate раньше message; passport/inn раньше name).
        ordered = [
            "passport", "inn", "birthdate", "medical", "children",
            "email", "phone", "file", "company", "address",
            "name", "message",
        ]
        for category in ordered:
            patterns = FIELD_PATTERNS.get(category, [])
            if patterns and utils.contains_any(haystack, patterns):
                return category

        return "unknown"
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Ключевые слова для флагов типа формы.
# ---------------------------------------------------------------------------
_NEWSLETTER_KW = [
    "подписаться", "подписк", "рассылк", "новости", "subscribe",
    "newsletter", "дайджест",
]
_CALLBACK_KW = [
    "звонок", "перезвон", "callback", "заявк", "обратный звонок",
    "заказать звонок", "перезвоните", "обратная связь",
]
_ORDER_KW = [
    "заказ", "корзин", "оформить", "купить", "оплат", "checkout", "order",
    "cart", "buy", "оформление заказа",
]
_MEDICAL_KW = [
    "медицин", "клиник", "врач", "диагноз", "симптом", "запись на прием",
    "запись к врачу", "здоровье", "лечен", "анализ",
]
_HR_KW = [
    "резюме", "ваканси", "career", "careers", "работа у нас", "отклик",
    "трудоустройство", "соискател", "job",
]
_CHILDREN_KW = [
    "ребен", "дети", "детей", "школьник", "ученик", "возраст ребенка",
    "имя ребенка", "детск",
]


# ---------------------------------------------------------------------------
# Ключевые слова / фразы для анализа согласия.
# ---------------------------------------------------------------------------
_CONSENT_TEXT_KW = [
    "согласие", "согласен", "согласна", "даю согласие",
    "обработка персональных данных", "обработку персональных данных",
    "персональные данные", "персональных данных", "политика", "политику",
    "privacy", "consent",
]
_PRIVACY_LINK_KW = [
    "privacy", "policy", "персональн", "политик", "consent", "согласие",
    "cookie", "куки", "оферт", "конфиденциальн", "personal-data",
    "soglasie", "personal",
]
_CONSENT_LINK_KW = ["согласие", "consent", "soglasie"]
_AD_CONSENT_KW = ["реклам", "рассылк", "маркетинг", "marketing", "рекламн"]
_COOKIE_CONSENT_KW = ["cookie", "куки"]
_BUTTON_ACCEPTANCE_KW = [
    "нажимая кнопку", "нажимая на кнопку", "отправляя форму",
    "нажимая «отправить»", "нажимая \"отправить\"", "нажатием кнопки",
]
_OFFER_KW = ["оферт", "пользовательск соглашен", "пользовательское соглашение"]


# ---------------------------------------------------------------------------
# bs4-хелперы (guarded).
# ---------------------------------------------------------------------------
def _get_soup(html: str):
    """Вернуть BeautifulSoup или None (если bs4 недоступен / ошибка)."""
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return None
    try:
        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            return BeautifulSoup(html, "html.parser")
    except Exception:
        return None


def _text_of(el) -> str:
    try:
        return utils.clean_text(el.get_text(" ", strip=True))
    except Exception:
        return ""


def _attr(el, name: str) -> str:
    try:
        val = el.get(name)
    except Exception:
        return ""
    if val is None:
        return ""
    if isinstance(val, (list, tuple)):
        return " ".join(str(v) for v in val)
    return str(val)


def _label_for_control(el, soup) -> str:
    """Найти подпись поля: <label for>, обёртка <label>, aria-label, title."""
    try:
        aria = _attr(el, "aria-label").strip()
        if aria:
            return utils.clean_text(aria)

        el_id = _attr(el, "id").strip()
        if el_id and soup is not None:
            try:
                lab = soup.find("label", attrs={"for": el_id})
                if lab is not None:
                    txt = _text_of(lab)
                    if txt:
                        return txt
            except Exception:
                pass

        # Обёртка <label>...control...</label>
        try:
            parent = el.find_parent("label")
            if parent is not None:
                txt = _text_of(parent)
                if txt:
                    return txt
        except Exception:
            pass

        title = _attr(el, "title").strip()
        if title:
            return utils.clean_text(title)
    except Exception:
        pass
    return ""


def _is_truthy_attr(el, name: str) -> bool:
    """HTML boolean-атрибут: присутствует => True (в т.ч. required="")."""
    try:
        if not el.has_attr(name):
            return False
        val = el.get(name)
        if val is None:
            return True
        sval = str(val).strip().lower()
        return sval in ("", name.lower(), "true", "required", "checked")
    except Exception:
        return False


def _make_evidence(page_url: str, quote: str, html_snippet: str) -> Evidence:
    """Построить Evidence через scanner.evidence.make_evidence (guarded)."""
    try:
        from scanner import evidence as _ev

        return _ev.make_evidence(
            page_url=page_url, quote=quote, html_snippet=html_snippet
        )
    except Exception:
        return Evidence(
            page_url=page_url or "",
            quote=utils.truncate(quote or "", 500),
            html_snippet=utils.truncate(html_snippet or "", 800),
        )


# ---------------------------------------------------------------------------
# Извлечение полей формы.
# ---------------------------------------------------------------------------
def _collect_fields(scope_el, soup) -> List[FormField]:
    fields: List[FormField] = []
    try:
        controls = scope_el.find_all(["input", "textarea", "select"])
    except Exception:
        controls = []

    for el in controls:
        try:
            tag = (getattr(el, "name", "") or "").lower()
            input_type = _attr(el, "type").strip().lower()

            if tag == "input":
                # Пропускаем скрытые/технические поля и submit-кнопки.
                if input_type in ("hidden", "submit", "button", "image", "reset"):
                    continue
                field_type = input_type or "text"
            elif tag == "textarea":
                field_type = "textarea"
            elif tag == "select":
                field_type = "select"
            else:
                continue

            fname = _attr(el, "name").strip()
            placeholder = _attr(el, "placeholder").strip()
            label = _label_for_control(el, soup)
            required = _is_truthy_attr(el, "required") or (
                _attr(el, "aria-required").strip().lower() == "true"
            )

            category = classify_field(fname, field_type, label, placeholder)

            raw_html = ""
            try:
                raw_html = utils.truncate(str(el), 300)
            except Exception:
                raw_html = ""

            fields.append(
                FormField(
                    name=fname,
                    field_type=field_type,
                    label=utils.truncate(label, 200),
                    placeholder=utils.truncate(placeholder, 200),
                    category=category,
                    required=bool(required),
                    raw_html=raw_html,
                )
            )
        except Exception:
            continue
    return fields


def _submit_text(scope_el) -> str:
    """Текст кнопки отправки формы (submit/button)."""
    try:
        # <button ...>text</button>
        for btn in scope_el.find_all("button"):
            btype = _attr(btn, "type").strip().lower()
            if btype in ("reset",):
                continue
            txt = _text_of(btn)
            if txt:
                return utils.truncate(txt, 120)
        # <input type=submit value="...">
        for inp in scope_el.find_all("input"):
            itype = _attr(inp, "type").strip().lower()
            if itype in ("submit", "button", "image"):
                val = _attr(inp, "value").strip() or _attr(inp, "alt").strip()
                if val:
                    return utils.truncate(val, 120)
        # ссылка-кнопка
        for a in scope_el.find_all("a"):
            cls = _norm(_attr(a, "class"))
            if "btn" in cls or "button" in cls or "submit" in cls:
                txt = _text_of(a)
                if txt:
                    return utils.truncate(txt, 120)
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Анализ согласия рядом с формой.
# ---------------------------------------------------------------------------
def analyze_consent(scope_el, page_soup, page_url: str = "") -> ConsentInfo:
    """Проанализировать механику согласия внутри формы и рядом с ней.

    Область анализа: сам scope_el (форма/контейнер) плюс несколько его
    непосредственных следующих соседей (текст согласия часто расположен под
    формой). Никогда не бросает исключение — при ошибке возвращает пустой
    ConsentInfo.
    """
    info = ConsentInfo()
    if scope_el is None:
        return info
    try:
        # --- Собираем область: сам scope + следующие siblings ---
        scopes = [scope_el]
        try:
            sib = scope_el
            for _ in range(3):
                sib = sib.find_next_sibling()
                if sib is None:
                    break
                if getattr(sib, "name", None):
                    scopes.append(sib)
        except Exception:
            pass

        # --- Чекбоксы ---
        checkbox_els = []
        for sc in scopes:
            try:
                for inp in sc.find_all("input"):
                    if _attr(inp, "type").strip().lower() == "checkbox":
                        checkbox_els.append(inp)
                for el in sc.find_all(attrs={"role": "checkbox"}):
                    checkbox_els.append(el)
            except Exception:
                continue

        info.checkbox_found = len(checkbox_els) > 0

        if info.checkbox_found:
            prechecked = False
            for cb in checkbox_els:
                try:
                    if cb.has_attr("checked"):
                        prechecked = True
                        break
                    if _attr(cb, "aria-checked").strip().lower() == "true":
                        prechecked = True
                        break
                except Exception:
                    continue
            info.checkbox_prechecked = bool(prechecked)
        else:
            info.checkbox_prechecked = None

        # --- Текст области (для эвристик по фразам) ---
        texts = []
        for sc in scopes:
            t = _text_of(sc)
            if t:
                texts.append(t)
        area_text = utils.clean_text(" ".join(texts))
        norm_text = _norm(area_text)

        # --- Наличие текста согласия / политики рядом ---
        info.consent_text_found = utils.contains_any(area_text, _CONSENT_TEXT_KW)

        # --- Ссылки (privacy / policy / согласие / оферта / cookie) ---
        privacy_urls: List[str] = []
        privacy_found = False
        consent_link_found = False
        ad_consent_found = False
        cookie_consent_found = False
        for sc in scopes:
            try:
                anchors = sc.find_all("a")
            except Exception:
                anchors = []
            for a in anchors:
                href = _attr(a, "href").strip()
                atext = _text_of(a)
                combined = href + " " + atext
                if utils.contains_any(combined, _PRIVACY_LINK_KW):
                    privacy_found = True
                    if href and not href.lower().startswith(
                        ("mailto:", "tel:", "javascript:", "#")
                    ):
                        privacy_urls.append(href)
                if utils.contains_any(combined, _CONSENT_LINK_KW):
                    consent_link_found = True
                if utils.contains_any(combined, _AD_CONSENT_KW):
                    ad_consent_found = True
                if utils.contains_any(combined, _COOKIE_CONSENT_KW):
                    cookie_consent_found = True

        # Реклама/рассылка/маркетинг может упоминаться и в тексте, не только в ссылке.
        if utils.contains_any(area_text, _AD_CONSENT_KW):
            ad_consent_found = True
        if utils.contains_any(area_text, _COOKIE_CONSENT_KW):
            cookie_consent_found = True

        info.privacy_link_found = privacy_found
        info.privacy_link_urls = utils.dedupe_keep_order(privacy_urls)
        info.consent_link_found = consent_link_found
        info.ad_consent_found = ad_consent_found
        info.cookie_consent_found = cookie_consent_found

        # --- «Нажимая кнопку …» без чекбокса ---
        button_acceptance = utils.contains_any(area_text, _BUTTON_ACCEPTANCE_KW)
        info.button_acceptance_only = bool(
            button_acceptance and not info.checkbox_found
        )

        # --- Согласие «смешано» с офертой/пользовательским соглашением ---
        info.merged_with_offer = bool(
            info.consent_text_found
            and utils.contains_any(area_text, _OFFER_KW)
        )

        # --- Доказательства + confidence ---
        info.evidence_text = utils.truncate(area_text, 500)
        try:
            info.evidence_html_snippet = utils.truncate(str(scope_el), 800)
        except Exception:
            info.evidence_html_snippet = ""

        confidence = 0
        if info.checkbox_found:
            confidence += 30
        if info.consent_text_found:
            confidence += 25
        if info.privacy_link_found:
            confidence += 25
        if info.button_acceptance_only:
            confidence += 10
        if info.ad_consent_found or info.cookie_consent_found:
            confidence += 10
        info.confidence = min(confidence, 95)

        return info
    except Exception:
        return info


# ---------------------------------------------------------------------------
# Эвристические группы полей без обёртки <form>.
# ---------------------------------------------------------------------------
def _has_submit_like(el) -> bool:
    try:
        for btn in el.find_all("button"):
            if _attr(btn, "type").strip().lower() != "reset":
                return True
        for inp in el.find_all("input"):
            if _attr(inp, "type").strip().lower() in ("submit", "button", "image"):
                return True
        for a in el.find_all("a"):
            cls = _norm(_attr(a, "class"))
            if "btn" in cls or "button" in cls or "submit" in cls:
                return True
    except Exception:
        return False
    return False


def _count_controls(el) -> int:
    try:
        n = 0
        for c in el.find_all(["input", "textarea", "select"]):
            tag = (getattr(c, "name", "") or "").lower()
            itype = _attr(c, "type").strip().lower()
            if tag == "input" and itype in (
                "hidden", "submit", "button", "image", "reset"
            ):
                continue
            n += 1
        return n
    except Exception:
        return 0


def _find_heuristic_groups(soup) -> List[Any]:
    """Найти контейнеры с >=2 полями и кнопкой, не обёрнутые в <form>."""
    groups: List[Any] = []
    if soup is None:
        return groups
    try:
        candidates = soup.find_all(["div", "section", "aside", "article"])
    except Exception:
        return groups

    # Ограничиваем число обрабатываемых контейнеров: на очень «тяжёлых»
    # страницах (тысячи вложенных div framework-разметки) полный проход с
    # подсчётом контролов в каждом поддереве превращается в O(n^2) и подвешивает
    # проверку. 600 контейнеров с формоподобным содержимым более чем достаточно.
    _MAX_CANDIDATES = 600
    if len(candidates) > _MAX_CANDIDATES:
        candidates = candidates[:_MAX_CANDIDATES]

    seen_ids = set()
    for el in candidates:
        try:
            # Пропускаем контейнеры, находящиеся внутри <form> (уже учтены).
            if el.find_parent("form") is not None:
                continue
            # Пропускаем контейнеры, содержащие <form> (форма будет учтена сама).
            if el.find("form") is not None:
                continue
            if _count_controls(el) < 2:
                continue
            if not _has_submit_like(el):
                continue
            # Избегаем вложенных дублей: не берём контейнер, если один из уже
            # выбранных является его потомком или предком.
            marker = id(el)
            if marker in seen_ids:
                continue
            groups.append(el)
            seen_ids.add(marker)
        except Exception:
            continue

    # Убираем вложенные группы (оставляем самые «внешние» непересекающиеся).
    # Проверяем предков элемента (O(глубина)), а не материализуем descendants
    # каждого другого контейнера (это давало O(n^2) и подвешивало разбор на
    # глубоко вложенной разметке).
    group_ids = {id(el) for el in groups}
    filtered: List[Any] = []
    for el in groups:
        try:
            nested = False
            for parent in el.parents:
                if id(parent) in group_ids:
                    nested = True
                    break
            if not nested:
                filtered.append(el)
        except Exception:
            filtered.append(el)
    return filtered


# ---------------------------------------------------------------------------
# Основная функция.
# ---------------------------------------------------------------------------
def detect_forms(
    html: str,
    page_url: str = "",
    source: str = "html",
    settings: Any = None,
) -> List[FormResult]:
    """Найти и классифицировать формы в HTML.

    Возвращает список FormResult. Если bs4 недоступен или HTML пуст — []. Ни при
    каких условиях не бросает исключение.
    """
    results: List[FormResult] = []
    soup = _get_soup(html)
    if soup is None:
        return results

    try:
        scopes: List[Any] = []
        try:
            scopes.extend(soup.find_all("form"))
        except Exception:
            pass
        try:
            scopes.extend(_find_heuristic_groups(soup))
        except Exception:
            pass

        for i, scope_el in enumerate(scopes):
            try:
                fr = _build_form_result(scope_el, soup, page_url, source, i)
                if fr is not None:
                    results.append(fr)
            except Exception:
                continue
    except Exception:
        return results

    return results


def _build_form_result(
    scope_el,
    soup,
    page_url: str,
    source: str,
    index: int,
) -> Optional[FormResult]:
    fields = _collect_fields(scope_el, soup)

    # Форма без осмысленных полей ввода не интересна.
    if not fields:
        return None

    categories = [f.category for f in fields]

    personal_data_fields = sorted(
        {c for c in categories if c in PERSONAL_DATA_FIELD_CATEGORIES}
    )
    potentially_personal = bool(personal_data_fields)
    has_file_upload = any(f.category == "file" or f.field_type == "file" for f in fields)

    submit_text = _submit_text(scope_el)

    # --- Текст области для эвристик по типу формы ---
    try:
        form_text = _text_of(scope_el)
    except Exception:
        form_text = ""
    combined_text = " ".join(
        [form_text, submit_text]
        + [f.label for f in fields]
        + [f.placeholder for f in fields]
        + [f.name for f in fields]
    )

    has_email = any(f.category == "email" for f in fields)
    has_phone = any(f.category == "phone" for f in fields)
    non_consent = [
        f for f in fields
        if f.category not in ("consent", "other", "unknown")
    ]
    email_only = has_email and all(
        f.category in ("email", "consent", "other", "unknown") for f in fields
    ) and len(non_consent) <= 1

    is_newsletter = bool(
        email_only and utils.contains_any(combined_text, _NEWSLETTER_KW)
    )
    is_callback = bool(
        has_phone and utils.contains_any(combined_text, _CALLBACK_KW)
    )
    is_order = utils.contains_any(combined_text, _ORDER_KW)
    is_medical = bool(
        any(f.category == "medical" for f in fields)
        or utils.contains_any(combined_text, _MEDICAL_KW)
    )
    is_hr = bool(
        (has_file_upload and utils.contains_any(combined_text, _HR_KW))
        or utils.contains_any(combined_text, _HR_KW)
    )
    is_children = bool(
        any(f.category == "children" for f in fields)
        or utils.contains_any(combined_text, _CHILDREN_KW)
    )

    # --- Тип формы: первый подходящий ---
    if is_newsletter:
        form_type = "newsletter"
    elif is_callback:
        form_type = "callback"
    elif is_order:
        form_type = "order"
    elif is_medical:
        form_type = "medical"
    elif is_hr:
        form_type = "hr"
    elif is_children:
        form_type = "children"
    else:
        form_type = "generic"

    # --- Согласие ---
    try:
        consent = analyze_consent(scope_el, soup, page_url)
    except Exception:
        consent = ConsentInfo()

    # --- Доказательства ---
    try:
        snippet = utils.truncate(str(scope_el), 800)
    except Exception:
        snippet = ""
    quote = utils.truncate(form_text or submit_text, 300)
    evidence = _make_evidence(page_url, quote, snippet)

    # --- Уверенность (40..90) ---
    confidence = 40
    if potentially_personal:
        confidence += 15
    if len(fields) >= 3:
        confidence += 10
    if submit_text:
        confidence += 10
    if consent.checkbox_found or consent.consent_text_found:
        confidence += 10
    if consent.privacy_link_found:
        confidence += 5
    confidence = max(40, min(confidence, 90))

    return FormResult(
        form_id=f"{source}-{index}",
        page_url=page_url or "",
        source=source,
        fields=fields,
        personal_data_fields=personal_data_fields,
        submit_button_text=submit_text,
        form_type=form_type,
        is_newsletter=is_newsletter,
        is_callback=is_callback,
        is_order=is_order,
        is_medical=is_medical,
        is_hr=is_hr,
        is_children_related=is_children,
        has_file_upload=has_file_upload,
        potentially_personal_data_form=potentially_personal,
        consent=consent,
        evidence=evidence,
        confidence=confidence,
    )
