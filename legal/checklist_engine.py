"""
Эвристический движок чек-листов анализа документов сайта.

Загружает чек-листы из data/document_checklists.yml, определяет применимость
пунктов к конкретному сайту (applies_when) и проверяет каждый пункт:
специальными детерминированными обработчиками (special) либо общим поиском по
ключевым словам в тексте документа.

Тон всех комментариев — нейтральный: «признак риска» / «требует проверки», без
утверждений о нарушении. Модуль никогда не бросает исключение наружу.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from scanner import utils
from scanner.models import (
    ChecklistItemResult,
    ChecklistStatus,
    DocumentResult,
    ScanContext,
)

# Опциональный помощник для аккуратных сниппетов-цитат. Модуль scanner.evidence
# относится к другой группе и может отсутствовать при изолированном импорте —
# поэтому импорт защищён, а при отсутствии используется локальный fallback.
try:  # pragma: no cover - зависит от наличия соседнего модуля
    from scanner import evidence as _evidence
except Exception:  # pragma: no cover
    _evidence = None


# ---------------------------------------------------------------------------
# Загрузка чек-листов
# ---------------------------------------------------------------------------
def load_checklists(path: str) -> Dict[str, List[dict]]:
    """Загрузить чек-листы из YAML. Возвращает dict вида {doc_type: [item,...]}."""
    try:
        import yaml

        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        checklists = data.get("checklists")
        if isinstance(checklists, dict):
            return checklists
        return {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Применимость пункта (applies_when)
# ---------------------------------------------------------------------------
def applies(applies_when: str, ctx: ScanContext) -> bool:
    """Определить, применим ли пункт к текущему сайту по токену applies_when."""
    token = (applies_when or "always").strip().lower()
    if token in ("", "always"):
        return True
    mapping = {
        "has_pd_forms": ctx.has_pd_forms,
        "has_forms": ctx.has_forms,
        "trackers_present": ctx.trackers_present,
        "foreign_trackers_present": ctx.foreign_trackers_present,
        "cookies_present": ctx.cookies_present,
        "newsletter_form": ctx.newsletter_form,
        "file_upload": ctx.file_upload,
        "medical": ctx.medical,
        "children": ctx.children,
        "hr": ctx.hr,
        "ecommerce": ctx.ecommerce,
        "finance": ctx.finance,
        "publishes_people": ctx.publishes_people,
    }
    return bool(mapping.get(token, True))


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------
def _snippet_around(text: str, needle: str, radius: int = 200) -> str:
    """Вернуть окно текста вокруг первого вхождения needle (без регистра)."""
    if not text:
        return ""
    if _evidence is not None:
        try:
            snip = _evidence.snippet_around(text, needle, radius=radius)
            if snip:
                return snip
        except Exception:
            pass
    # Локальный fallback без зависимости от scanner.evidence.
    try:
        norm_text = utils.normalize_for_search(text)
        norm_needle = utils.normalize_for_search(needle or "")
        if norm_needle:
            idx = norm_text.find(norm_needle)
        else:
            idx = -1
        if idx < 0:
            return utils.truncate(utils.clean_text(text), 2 * radius)
        start = max(0, idx - radius)
        end = min(len(text), idx + len(needle) + radius)
        return utils.truncate(utils.clean_text(text[start:end]), 2 * radius + 40)
    except Exception:
        return utils.truncate(utils.clean_text(text), 2 * radius)


def _first_matched_keyword(text: str, keywords: List[str]) -> Optional[str]:
    try:
        return utils.find_first(text, keywords or [])
    except Exception:
        return None


def _result(
    item: dict,
    status: str,
    comment: str = "",
    evidence_quote: str = "",
    evidence_location: str = "",
    confidence: int = 0,
    source: str = "heuristic",
) -> ChecklistItemResult:
    """Собрать ChecklistItemResult с безопасными значениями по умолчанию."""
    return ChecklistItemResult(
        id=str(item.get("id", "")),
        label=str(item.get("label", "")),
        status=status,
        evidence_quote=utils.truncate(evidence_quote or "", 500),
        evidence_location=evidence_location or "",
        comment=comment or "",
        risk_if_missing=str(item.get("risk_if_missing", "medium") or "medium"),
        confidence=int(confidence),
        source=source,
    )


def _pd_forms(ctx: ScanContext) -> list:
    return [f for f in ctx.forms if getattr(f, "potentially_personal_data_form", False)]


# ---------------------------------------------------------------------------
# Главная функция: прогон чек-листа по документу
# ---------------------------------------------------------------------------
def run_heuristic_checklist(
    items: List[dict],
    document: DocumentResult,
    ctx: ScanContext,
) -> List[ChecklistItemResult]:
    """Проверить документ по списку пунктов чек-листа (эвристика + спец-обработчики)."""
    results: List[ChecklistItemResult] = []
    if not items:
        return results

    doc_url = ""
    doc_text = ""
    try:
        doc_url = document.url or ""
        doc_text = document.text or ""
    except Exception:
        doc_url = ""
        doc_text = ""

    for item in items:
        try:
            if not isinstance(item, dict):
                continue
            applies_when = str(item.get("applies_when", "always"))
            if not applies(applies_when, ctx):
                results.append(
                    _result(
                        item,
                        ChecklistStatus.not_applicable.value,
                        comment="Пункт не применим к данному сайту в рамках автоматической проверки.",
                        evidence_location=doc_url,
                        confidence=0,
                    )
                )
                continue

            special = item.get("special")
            if special:
                handler = _SPECIAL_HANDLERS.get(str(special))
                if handler is not None:
                    results.append(handler(item, document, ctx, doc_text, doc_url))
                    continue
                # Неизвестный спец-обработчик — уходим на нейтральную ручную проверку.
                results.append(_handler_needs_manual(item, document, ctx, doc_text, doc_url))
                continue

            # Общий поиск по ключевым словам.
            keywords = item.get("keywords") or []
            if not isinstance(keywords, list):
                keywords = []
            if doc_text and keywords and utils.contains_any(doc_text, keywords):
                kw = _first_matched_keyword(doc_text, keywords)
                quote = _snippet_around(doc_text, kw or "", radius=180)
                results.append(
                    _result(
                        item,
                        ChecklistStatus.found.value,
                        comment="Соответствующая формулировка обнаружена в тексте документа.",
                        evidence_quote=quote,
                        evidence_location=doc_url,
                        confidence=70,
                    )
                )
            else:
                results.append(
                    _result(
                        item,
                        ChecklistStatus.not_found.value,
                        comment="Соответствующая формулировка в тексте документа не обнаружена — требует проверки.",
                        evidence_location=doc_url,
                        confidence=55,
                    )
                )
        except Exception:
            # Ни один пункт не должен ломать проверку документа.
            try:
                results.append(
                    _result(
                        item if isinstance(item, dict) else {},
                        ChecklistStatus.unclear.value,
                        comment="Пункт не удалось проверить автоматически — требует ручной проверки.",
                        evidence_location=doc_url,
                        confidence=0,
                    )
                )
            except Exception:
                pass
    return results


# ---------------------------------------------------------------------------
# Специальные обработчики (детерминированные; используют ctx + document)
# ---------------------------------------------------------------------------
def _handler_doc_accessible(item, document, ctx, doc_text, doc_url):
    accessible = bool(getattr(document, "is_accessible", False)) and bool(doc_text)
    if accessible:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Документ загружается и содержит извлекаемый текст.",
            evidence_quote=utils.truncate(utils.clean_text(doc_text), 300),
            evidence_location=doc_url,
            confidence=80,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment="Документ не удалось открыть или из него не извлекается текст — требует проверки.",
        evidence_location=doc_url,
        confidence=60,
    )


def _handler_policy_near_forms(item, document, ctx, doc_text, doc_url):
    forms = _pd_forms(ctx)
    if not forms:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Формы, предполагающие сбор персональных данных, не обнаружены.",
            evidence_location=doc_url,
        )
    near = False
    for f in forms:
        consent = getattr(f, "consent", None)
        if consent is not None and getattr(consent, "privacy_link_found", False):
            near = True
            break
    if near:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Рядом с формой обнаружена ссылка на политику обработки персональных данных.",
            evidence_location=doc_url,
            confidence=70,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment="Рядом с формой сбора персональных данных ссылка на политику не обнаружена — зона риска.",
        evidence_location=doc_url,
        confidence=60,
    )


def _handler_inn_ogrn(item, document, ctx, doc_text, doc_url):
    inns = utils.find_inn(doc_text) if doc_text else []
    ogrns = utils.find_ogrn(doc_text) if doc_text else []
    if inns or ogrns:
        parts = []
        if inns:
            parts.append("ИНН: " + ", ".join(inns[:3]))
        if ogrns:
            parts.append("ОГРН/ОГРНИП: " + ", ".join(ogrns[:3]))
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="В документе обнаружены реквизиты оператора.",
            evidence_quote="; ".join(parts),
            evidence_location=doc_url,
            confidence=80,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment="Реквизиты (ИНН/ОГРН) в документе не обнаружены — требует проверки.",
        evidence_location=doc_url,
        confidence=60,
    )


def _handler_policy_matches_forms(item, document, ctx, doc_text, doc_url):
    """Покрывает ли перечень ПДн в документе поля, которые собирают формы."""
    categories = list(ctx.personal_data_categories or [])
    if not categories:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Формы, собирающие персональные данные, не обнаружены — сопоставление не выполнялось.",
            evidence_location=doc_url,
        )
    if not doc_text:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment="Текст документа недоступен для сопоставления с полями форм — требует проверки.",
            evidence_location=doc_url,
            confidence=40,
        )
    # Ключевые слова, ассоциированные с категориями полей форм.
    category_keywords = {
        "name": ["фамили", "имя", "фио", "отчеств"],
        "phone": ["телефон", "номер телефона", "контактн"],
        "email": ["email", "e-mail", "электронн", "адрес электронной"],
        "address": ["адрес", "место жительства", "почтов"],
        "birthdate": ["дата рождения", "возраст"],
        "message": ["сообщени", "текст обращени", "комментар", "иные данные", "иные сведения"],
        "file": ["файл", "документ", "вложени", "резюме"],
        "passport": ["паспорт", "документ, удостоверяющ"],
        "inn": ["инн"],
        "company": ["наименование организаци", "компани", "организац"],
        "medical": ["состояни здоровья", "здоровь", "медицинск", "специальны категори"],
        "children": ["несовершеннолетн", "ребен", "дет", "родител"],
    }
    covered: List[str] = []
    missing: List[str] = []
    for cat in categories:
        kws = category_keywords.get(cat, [cat])
        if utils.contains_any(doc_text, kws):
            covered.append(cat)
        else:
            missing.append(cat)
    if not missing:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Перечень данных в документе покрывает категории полей, обнаруженные в формах.",
            evidence_quote="Покрытые категории: " + ", ".join(covered),
            evidence_location=doc_url,
            confidence=65,
        )
    if covered:
        return _result(
            item,
            ChecklistStatus.unclear.value,
            comment=(
                "Часть категорий полей форм не удалось однозначно соотнести с перечнем "
                "данных в документе — требует проверки: " + ", ".join(missing)
            ),
            evidence_location=doc_url,
            confidence=45,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment=(
            "Перечень данных в документе не удалось соотнести с полями форм сайта — "
            "зона риска: " + ", ".join(missing)
        ),
        evidence_location=doc_url,
        confidence=50,
    )


def _handler_third_parties(item, document, ctx, doc_text, doc_url):
    keywords = item.get("keywords") or [
        "третьи лиц", "обработчик", "по поручению", "хостинг", "crm",
        "аналитик", "платёжн", "платежн", "доставк",
    ]
    if doc_text and utils.contains_any(doc_text, keywords):
        kw = _first_matched_keyword(doc_text, keywords)
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Упоминание третьих лиц/обработчиков обнаружено в документе.",
            evidence_quote=_snippet_around(doc_text, kw or "", 180),
            evidence_location=doc_url,
            confidence=65,
        )
    comment = "Раскрытие третьих лиц/обработчиков в документе не обнаружено — требует проверки."
    if ctx.trackers_present:
        comment = (
            "На сайте обнаружены сторонние сервисы, однако раскрытие третьих лиц/"
            "обработчиков в документе не найдено — зона риска."
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment=comment,
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_cross_border(item, document, ctx, doc_text, doc_url):
    keywords = item.get("keywords") or [
        "трансгранич", "за пределы российской федерации", "иностранн", "иностранное государство",
    ]
    mentioned = bool(doc_text) and utils.contains_any(doc_text, keywords)
    if mentioned:
        kw = _first_matched_keyword(doc_text, keywords)
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Тема трансграничной передачи персональных данных раскрыта в документе.",
            evidence_quote=_snippet_around(doc_text, kw or "", 180),
            evidence_location=doc_url,
            confidence=65,
        )
    if ctx.foreign_trackers_present:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment=(
                "Обнаружены сторонние сервисы с иностранной инфраструктурой, однако "
                "трансграничная передача в документе не раскрыта — зона риска."
            ),
            evidence_location=doc_url,
            confidence=55,
        )
    return _result(
        item,
        ChecklistStatus.unclear.value,
        comment=(
            "Признаков иностранных сервисов не обнаружено; раскрытие трансграничной "
            "передачи не требуется либо требует ручной проверки."
        ),
        evidence_location=doc_url,
        confidence=40,
    )


def _handler_cookie_providers(item, document, ctx, doc_text, doc_url):
    providers = []
    try:
        for t in ctx.trackers:
            name = getattr(t, "provider_name", "")
            if name:
                providers.append(name)
    except Exception:
        providers = []
    providers = utils.dedupe_keep_order(providers)
    if not providers:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Сторонние сервисы/провайдеры не обнаружены.",
            evidence_location=doc_url,
        )
    if not doc_text:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment="Текст документа недоступен для проверки упоминания провайдеров — требует проверки.",
            evidence_location=doc_url,
            confidence=40,
        )
    mentioned = [p for p in providers if utils.contains_any(doc_text, [p])]
    missing = [p for p in providers if p not in mentioned]
    if mentioned and not missing:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Обнаруженные провайдеры упомянуты в документе.",
            evidence_quote="Упомянуты: " + ", ".join(mentioned[:5]),
            evidence_location=doc_url,
            confidence=65,
        )
    if mentioned:
        return _result(
            item,
            ChecklistStatus.unclear.value,
            comment=(
                "Часть обнаруженных провайдеров не упомянута в документе — требует проверки: "
                + ", ".join(missing[:5])
            ),
            evidence_location=doc_url,
            confidence=45,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment=(
            "Обнаруженные провайдеры не упомянуты в документе — зона риска: "
            + ", ".join(missing[:5])
        ),
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_placeholders(item, document, ctx, doc_text, doc_url):
    """found == GOOD, когда в документе НЕТ шаблонных плейсхолдеров."""
    placeholders = []
    try:
        placeholders = utils.find_placeholders(doc_text) if doc_text else []
    except Exception:
        placeholders = []
    if getattr(document, "template_placeholder_detected", False):
        placeholders = placeholders or ["шаблонный плейсхолдер"]
    if not placeholders:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Незаполненных шаблонных полей в документе не обнаружено.",
            evidence_location=doc_url,
            confidence=70,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment=(
            "В документе обнаружены незаполненные шаблонные поля — зона риска: "
            + ", ".join(str(p) for p in placeholders[:5])
        ),
        evidence_quote=", ".join(str(p) for p in placeholders[:5]),
        evidence_location=doc_url,
        confidence=65,
    )


def _handler_company_conflict(item, document, ctx, doc_text, doc_url):
    """found == GOOD, когда нет расхождения оператора/домена с данными сайта."""
    conflict = False
    detail = ""
    try:
        reg_domain = utils.get_registered_domain(ctx.registered_domain or ctx.final_url or "")
        if reg_domain and doc_text:
            emails = utils.find_emails(doc_text)
            for em in emails:
                host = em.split("@", 1)[-1]
                em_domain = utils.get_registered_domain(host)
                # Игнорируем общедоступные почтовые сервисы.
                if em_domain in _PUBLIC_MAIL_DOMAINS:
                    continue
                if em_domain and em_domain != reg_domain:
                    conflict = True
                    detail = em
                    break
    except Exception:
        conflict = False
    if not conflict:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Явного расхождения оператора/домена с данными сайта не обнаружено.",
            evidence_location=doc_url,
            confidence=55,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment=(
            "Обнаружен возможный признак расхождения между реквизитами в документе и "
            "доменом сайта — требует проверки: " + detail
        ),
        evidence_quote=detail,
        evidence_location=doc_url,
        confidence=45,
    )


def _handler_doc_date(item, document, ctx, doc_text, doc_url):
    date_detected = getattr(document, "date_detected", "") or ""
    version_detected = getattr(document, "version_detected", "") or ""
    if not (date_detected or version_detected):
        keywords = item.get("keywords") or ["утвержд", "дата", "версия", "редакция", "от 20"]
        if doc_text and utils.contains_any(doc_text, keywords):
            kw = _first_matched_keyword(doc_text, keywords)
            return _result(
                item,
                ChecklistStatus.found.value,
                comment="В документе присутствует упоминание даты/версии/редакции.",
                evidence_quote=_snippet_around(doc_text, kw or "", 120),
                evidence_location=doc_url,
                confidence=55,
            )
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment="Дата утверждения/обновления или версия документа не обнаружены — требует проверки.",
            evidence_location=doc_url,
            confidence=55,
        )
    parts = []
    if date_detected:
        parts.append("дата: " + date_detected)
    if version_detected:
        parts.append("версия: " + version_detected)
    return _result(
        item,
        ChecklistStatus.found.value,
        comment="В документе обнаружены дата/версия.",
        evidence_quote="; ".join(parts),
        evidence_location=doc_url,
        confidence=75,
    )


def _handler_consent_separate(item, document, ctx, doc_text, doc_url):
    """Согласие оформлено отдельно, а не объединено с офертой/соглашением."""
    merged = False
    try:
        for f in ctx.forms:
            consent = getattr(f, "consent", None)
            if consent is not None and getattr(consent, "merged_with_offer", False):
                merged = True
                break
    except Exception:
        merged = False
    if merged:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment=(
                "Обнаружены признаки объединения согласия с офертой/пользовательским "
                "соглашением — зона риска."
            ),
            evidence_location=doc_url,
            confidence=55,
        )
    # Проверим текст документа на признаки объединения.
    if doc_text and utils.contains_any(
        doc_text, ["настоящая оферта", "публичная оферта", "пользовательское соглашение"]
    ) and utils.contains_any(doc_text, ["согласие на обработку", "даю согласие"]):
        return _result(
            item,
            ChecklistStatus.unclear.value,
            comment=(
                "В документе одновременно присутствуют признаки оферты/соглашения и "
                "согласия — требует проверки на предмет объединения."
            ),
            evidence_location=doc_url,
            confidence=45,
        )
    return _result(
        item,
        ChecklistStatus.found.value,
        comment="Явных признаков объединения согласия с офертой/соглашением не обнаружено.",
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_consent_prechecked(item, document, ctx, doc_text, doc_url):
    """not_found, когда рядом с формой обнаружен предустановленный чекбокс."""
    prechecked = False
    try:
        for f in ctx.forms:
            consent = getattr(f, "consent", None)
            if consent is not None and getattr(consent, "checkbox_prechecked", None) is True:
                prechecked = True
                break
    except Exception:
        prechecked = False
    if prechecked:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment=(
                "Рядом с формой обнаружен предустановленный (заранее отмеченный) чекбокс "
                "согласия — зона риска."
            ),
            evidence_location=doc_url,
            confidence=65,
        )
    return _result(
        item,
        ChecklistStatus.found.value,
        comment="Предустановленных чекбоксов согласия рядом с формами не обнаружено.",
        evidence_location=doc_url,
        confidence=60,
    )


def _handler_consent_link_clickable(item, document, ctx, doc_text, doc_url):
    has_link = False
    try:
        for f in _pd_forms(ctx):
            consent = getattr(f, "consent", None)
            if consent is not None and (
                getattr(consent, "privacy_link_found", False)
                or getattr(consent, "privacy_link_urls", None)
            ):
                has_link = True
                break
    except Exception:
        has_link = False
    if not _pd_forms(ctx):
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Формы, предполагающие сбор персональных данных, не обнаружены.",
            evidence_location=doc_url,
        )
    if has_link:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Рядом с формой обнаружена ссылка на согласие/политику; доступность ссылки требует проверки.",
            evidence_location=doc_url,
            confidence=55,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment="Кликабельная ссылка на согласие/политику рядом с формой не обнаружена — требует проверки.",
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_button_acceptance(item, document, ctx, doc_text, doc_url):
    """not_found, когда согласие выражено только фразой 'нажимая кнопку'."""
    button_only = False
    try:
        for f in ctx.forms:
            consent = getattr(f, "consent", None)
            if consent is not None and getattr(consent, "button_acceptance_only", False):
                button_only = True
                break
    except Exception:
        button_only = False
    if button_only:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment=(
                "Согласие оформлено только формулировкой об акцепте по нажатию кнопки, "
                "без отдельного чекбокса — зона риска."
            ),
            evidence_location=doc_url,
            confidence=60,
        )
    return _result(
        item,
        ChecklistStatus.found.value,
        comment="Признаков подмены согласия формулировкой 'нажимая кнопку' не обнаружено.",
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_newsletter_present(item, document, ctx, doc_text, doc_url):
    if ctx.newsletter_form:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Форма подписки/маркетинговой коммуникации обнаружена на сайте.",
            evidence_location=doc_url,
            confidence=70,
        )
    return _result(
        item,
        ChecklistStatus.not_applicable.value,
        comment="Форма подписки/рассылки не обнаружена.",
        evidence_location=doc_url,
    )


def _handler_cookie_banner(item, document, ctx, doc_text, doc_url):
    if not ctx.cookies_present:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Cookies/сторонние сервисы не обнаружены.",
            evidence_location=doc_url,
        )
    if ctx.cookie_banner_found:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="Cookie-баннер обнаружен на сайте.",
            evidence_location=doc_url,
            confidence=70,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment=(
            "На сайте обнаружены cookies/сторонние сервисы, однако cookie-баннер не "
            "обнаружен — зона риска."
        ),
        evidence_location=doc_url,
        confidence=60,
    )


def _handler_cookie_banner_notice(item, document, ctx, doc_text, doc_url):
    if not ctx.cookies_present:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Cookies/сторонние сервисы не обнаружены.",
            evidence_location=doc_url,
        )
    if ctx.cookie_banner_found:
        return _result(
            item,
            ChecklistStatus.found.value,
            comment="В баннере присутствует упоминание cookies — точность формулировки требует проверки.",
            evidence_location=doc_url,
            confidence=55,
        )
    return _result(
        item,
        ChecklistStatus.not_found.value,
        comment="Понятное уведомление о cookies в баннере не обнаружено — требует проверки.",
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_cookie_banner_action(item, document, ctx, doc_text, doc_url):
    if not ctx.cookies_present:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Cookies/сторонние сервисы не обнаружены.",
            evidence_location=doc_url,
        )
    return _result(
        item,
        ChecklistStatus.unclear.value,
        comment=(
            "Наличие активного действия согласия (кнопки) в cookie-баннере требует "
            "ручной проверки."
        ),
        evidence_location=doc_url,
        confidence=40,
    )


def _handler_cookie_banner_link(item, document, ctx, doc_text, doc_url):
    if not ctx.cookies_present:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Cookies/сторонние сервисы не обнаружены.",
            evidence_location=doc_url,
        )
    return _result(
        item,
        ChecklistStatus.unclear.value,
        comment="Наличие ссылки на политику в cookie-баннере требует ручной проверки.",
        evidence_location=doc_url,
        confidence=40,
    )


def _handler_cookies_before_consent(item, document, ctx, doc_text, doc_url):
    """not_found, когда маркетинговые/аналитические cookies уходят до согласия."""
    if not ctx.cookies_present:
        return _result(
            item,
            ChecklistStatus.not_applicable.value,
            comment="Cookies/сторонние сервисы не обнаружены.",
            evidence_location=doc_url,
        )
    if ctx.marketing_cookies_before_consent:
        return _result(
            item,
            ChecklistStatus.not_found.value,
            comment=(
                "Обнаружены признаки установки аналитических/маркетинговых cookies или "
                "запросов к трекерам до получения согласия — зона риска."
            ),
            evidence_quote=utils.truncate(ctx.cookies_before_consent_evidence or "", 300),
            evidence_location=doc_url,
            confidence=65,
        )
    return _result(
        item,
        ChecklistStatus.found.value,
        comment="Признаков установки маркетинговых/аналитических cookies до согласия не обнаружено.",
        evidence_location=doc_url,
        confidence=55,
    )


def _handler_cookie_banner_conflict(item, document, ctx, doc_text, doc_url):
    return _result(
        item,
        ChecklistStatus.unclear.value,
        comment=(
            "Соответствие заявлений cookie-баннера фактически используемым cookies "
            "требует ручной проверки."
        ),
        evidence_location=doc_url,
        confidence=40,
    )


def _handler_needs_manual(item, document, ctx, doc_text, doc_url):
    return _result(
        item,
        ChecklistStatus.unclear.value,
        comment="Пункт требует ручной проверки юристом.",
        evidence_location=doc_url,
        confidence=30,
    )


_PUBLIC_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yandex.ru", "ya.ru", "mail.ru", "bk.ru",
    "inbox.ru", "list.ru", "rambler.ru", "outlook.com", "hotmail.com",
    "icloud.com", "yahoo.com", "proton.me", "protonmail.com",
}


_SPECIAL_HANDLERS: Dict[str, Any] = {
    "doc_accessible": _handler_doc_accessible,
    "policy_near_forms": _handler_policy_near_forms,
    "inn_ogrn": _handler_inn_ogrn,
    "policy_matches_forms": _handler_policy_matches_forms,
    "third_parties": _handler_third_parties,
    "cross_border": _handler_cross_border,
    "cookie_providers": _handler_cookie_providers,
    "placeholders": _handler_placeholders,
    "company_conflict": _handler_company_conflict,
    "doc_date": _handler_doc_date,
    "consent_separate": _handler_consent_separate,
    "consent_prechecked": _handler_consent_prechecked,
    "consent_link_clickable": _handler_consent_link_clickable,
    "button_acceptance": _handler_button_acceptance,
    "newsletter_present": _handler_newsletter_present,
    "cookie_banner": _handler_cookie_banner,
    "cookie_banner_notice": _handler_cookie_banner_notice,
    "cookie_banner_action": _handler_cookie_banner_action,
    "cookie_banner_link": _handler_cookie_banner_link,
    "cookies_before_consent": _handler_cookies_before_consent,
    "cookie_banner_conflict": _handler_cookie_banner_conflict,
    "needs_manual": _handler_needs_manual,
}
