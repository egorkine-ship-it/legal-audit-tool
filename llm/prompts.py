"""
Промпты для LLM и нейтральные текстовые шаблоны-фолбэки.

Здесь собраны:
  * системные инструкции и JSON-схема ответа для анализа документа;
  * построители сообщений (system, user) для анализа документа и для
    генерации клиентских текстов (резюме, КП, письмо);
  * ПОЛНЫЕ нейтральные шаблоны на случай, когда LLM выключен/недоступен.

Правила тона (обязательны для всех текстов): деловой, спокойный, без угроз.
Мы НИКОГДА не утверждаем факт нарушения — только «признаки риска», «зона
риска», «требует проверки юристом».
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from scanner.models import (
    DOC_TYPE_RU,
    RISK_LEVEL_RU,
    RiskLevel,
    ScanContext,
    ScanResult,
)


# ---------------------------------------------------------------------------
# Системные инструкции и схема ответа
# ---------------------------------------------------------------------------
SYSTEM_DOC_ANALYSIS = (
    "Ты — ассистент юриста, специализирующегося на защите персональных данных "
    "(152-ФЗ и связанные акты РФ). Ты анализируешь юридический документ с сайта "
    "по заданному чек-листу.\n"
    "Строгие правила:\n"
    "1) Ты НЕ утверждаешь факт нарушения. Используй нейтральные формулировки: "
    "«признак риска», «зона риска», «пункт не обнаружен», «требует проверки "
    "юристом». Запрещены слова: «вы нарушаете», «нарушение установлено», "
    "«незаконно», «штраф», «сайт незаконен».\n"
    "2) Для каждого пункта чек-листа определи статус: found (пункт явно есть в "
    "тексте), not_found (не обнаружен), unclear (упоминается частично/неоднозначно), "
    "not_applicable (не применимо к этому документу).\n"
    "3) Статус found или unclear допустим ТОЛЬКО если ты приводишь дословную "
    "цитату из документа в поле evidence_quote (не длиннее 500 символов). Без "
    "цитаты используй not_found.\n"
    "4) Ничего не выдумывай. Опирайся только на переданный текст документа и на "
    "переданные факты сканирования.\n"
    "5) Верни ТОЛЬКО валидный JSON по заданной схеме, без пояснительного текста "
    "и без markdown-ограждений."
)

RESPONSE_SCHEMA = """{
  "document_type": "string (тип документа, например privacy_policy)",
  "document_url": "string (URL документа, если известен)",
  "overall_completeness": 0,
  "checklist_results": [
    {
      "id": "string (код пункта чек-листа, например PP_003)",
      "label": "string (краткое название пункта)",
      "status": "found | not_found | unclear | not_applicable",
      "evidence_quote": "string (дословная цитата из документа, <= 500 символов; обязательна для found/unclear)",
      "evidence_location": "string (URL документа или раздел)",
      "comment": "string (нейтральный комментарий, без утверждений о нарушении)",
      "risk_if_missing": "low | medium | high | critical",
      "confidence": 0
    }
  ],
  "conflicts": [
    {
      "type": "string (например tracker_not_disclosed, cross_border_denied, company_conflict)",
      "detected_fact": "string (что обнаружено при сканировании)",
      "document_statement": "string (что написано в документе)",
      "risk": "low | medium | high | critical",
      "comment": "string (нейтральный комментарий)"
    }
  ],
  "summary": "string (2-4 нейтральных предложения о полноте документа)",
  "requires_manual_review": true
}"""


# ---------------------------------------------------------------------------
# Построение сообщений: анализ документа
# ---------------------------------------------------------------------------
def _facts_to_lines(facts: Optional[Dict[str, Any]]) -> str:
    """Компактно перечислить факты сканирования для подсказки LLM."""
    facts = facts or {}
    lines: List[str] = []

    pd = facts.get("personal_data_categories") or []
    if isinstance(pd, (list, tuple, set)):
        pd = ", ".join(str(x) for x in pd)
    lines.append(f"- Категории собираемых персональных данных: {pd or 'не выявлены'}")

    trackers = facts.get("trackers") or []
    if isinstance(trackers, (list, tuple, set)):
        trackers = ", ".join(str(x) for x in trackers)
    lines.append(f"- Обнаруженные сторонние сервисы/трекеры: {trackers or 'не выявлены'}")

    lines.append(
        "- Есть зарубежные сторонние сервисы: "
        + ("да" if facts.get("foreign_trackers") else "нет")
    )
    lines.append(
        "- Обнаружен cookie-баннер: "
        + ("да" if facts.get("cookie_banner_found") else "нет")
    )

    company = facts.get("company_name") or ""
    if company:
        lines.append(f"- Название компании (по заявке): {company}")
    domain = facts.get("domain") or ""
    if domain:
        lines.append(f"- Домен сайта: {domain}")
    industry = facts.get("industry") or ""
    if industry:
        lines.append(f"- Отрасль: {industry}")

    return "\n".join(lines)


def _items_to_lines(items: Optional[List[Dict[str, Any]]]) -> str:
    """Перечислить пункты чек-листа: id, label, risk_if_missing, описание."""
    items = items or []
    lines: List[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        item_id = str(it.get("id", "")).strip()
        if not item_id:
            continue
        label = str(it.get("label", "")).strip()
        risk = str(it.get("risk_if_missing", "medium")).strip() or "medium"
        desc = str(it.get("description", "")).strip()
        line = f"- {item_id} | {label} | риск при отсутствии: {risk}"
        if desc:
            line += f" | что проверяем: {desc}"
        lines.append(line)
    return "\n".join(lines)


def build_document_analysis_messages(
    document_text: str,
    doc_type: str,
    items: Optional[List[Dict[str, Any]]],
    facts: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """
    Построить пару (system, user) для анализа документа по чек-листу.

    document_text усечён до ~12000 символов. Возвращает готовые сообщения.
    """
    text = document_text or ""
    if len(text) > 12000:
        text = text[:12000] + "\n…[текст документа усечён]"

    doc_type_ru = DOC_TYPE_RU.get(doc_type, doc_type or "документ")

    user = (
        f"Тип документа: {doc_type} ({doc_type_ru}).\n\n"
        "Факты, выявленные при сканировании сайта (учитывай их при оценке "
        "полноты документа и при поиске расхождений):\n"
        f"{_facts_to_lines(facts)}\n\n"
        "Пункты чек-листа, которые нужно оценить (верни по каждому объект в "
        "checklist_results с тем же id):\n"
        f"{_items_to_lines(items)}\n\n"
        "Правила ответа:\n"
        "- Оцени статус каждого пункта: found / not_found / unclear / "
        "not_applicable.\n"
        "- Статус found или unclear допустим только с дословной цитатой в "
        "evidence_quote (не длиннее 500 символов). Без цитаты — not_found.\n"
        "- Если пункт не относится к данному документу — not_applicable.\n"
        "- В conflicts укажи расхождения между фактами сканирования и текстом "
        "документа (например, сервис используется, но не раскрыт в документе).\n"
        "- Тон нейтральный: «признак риска», «не обнаружено», «требует проверки». "
        "Никаких утверждений о нарушении.\n"
        "- Верни ТОЛЬКО валидный JSON строго по схеме ниже.\n\n"
        "Схема ответа (JSON):\n"
        f"{RESPONSE_SCHEMA}\n\n"
        "Текст документа для анализа:\n"
        "<<<DOCUMENT\n"
        f"{text}\n"
        "DOCUMENT>>>"
    )
    return SYSTEM_DOC_ANALYSIS, user


# ---------------------------------------------------------------------------
# Системная инструкция и построители сообщений для клиентских текстов
# ---------------------------------------------------------------------------
SYSTEM_LEGAL_WRITER = (
    "Ты — юрист юридического бюро, специализирующегося на защите персональных "
    "данных (152-ФЗ). Ты пишешь клиентские тексты по результатам экспресс-"
    "проверки публичной части сайта.\n"
    "Тон: деловой, спокойный, уважительный, без запугивания и давления.\n"
    "Строгие запреты: не утверждай факт нарушения и не пугай санкциями. "
    "Запрещены формулировки «вы нарушаете», «нарушение установлено», "
    "«незаконно», «штраф», «сайт незаконен». Используй нейтральные обороты: "
    "«выявлены признаки риска», «зона риска», «рекомендуется проверить», "
    "«требует подтверждения юристом».\n"
    "Проверка автоматическая и охватывает только публично доступные страницы; "
    "выводы носят предварительный характер. Пиши по-русски."
)


def _facts_from_result(result: ScanResult, ctx: Optional[ScanContext]) -> Dict[str, Any]:
    """Собрать словарь фактов из результата и контекста для клиентских текстов."""
    facts: Dict[str, Any] = {}
    try:
        facts["company_name"] = result.company_name or (ctx.company_name if ctx else "")
        facts["site_url"] = result.site_url or result.final_url
        facts["final_url"] = result.final_url or result.site_url
        facts["industry"] = result.industry
        facts["risk_level"] = RISK_LEVEL_RU.get(
            _safe_level(result.risk_level), result.risk_level
        )
        facts["risk_score"] = result.risk_score
        facts["confidence"] = result.confidence
        facts["pages_checked"] = result.pages_checked
        facts["forms_count"] = len(result.forms)
        facts["documents_found"] = [
            DOC_TYPE_RU.get(d.doc_type, d.doc_type)
            for d in result.documents
            if getattr(d, "is_accessible", False)
        ]
        facts["trackers"] = [t.provider_name for t in result.trackers]
        facts["foreign_trackers"] = result.foreign_trackers_found
        facts["cookie_banner_found"] = result.cookie_banner_found
        facts["personal_data_categories"] = (
            list(ctx.personal_data_categories) if ctx else []
        )
        facts["top_risks"] = [
            {
                "title": r.title,
                "level": RISK_LEVEL_RU.get(_safe_level(r.risk_level), r.risk_level),
                "explanation": r.client_friendly_explanation or r.report_phrase or r.title,
            }
            for r in result.top_risks(5)
        ]
    except Exception:
        pass
    return facts


def _safe_level(value: str) -> RiskLevel:
    try:
        return RiskLevel(value)
    except Exception:
        return RiskLevel.unknown


def _top_risks_lines(facts: Dict[str, Any], limit: int = 3) -> str:
    risks = facts.get("top_risks") or []
    lines: List[str] = []
    for r in risks[:limit]:
        if isinstance(r, dict):
            title = str(r.get("title", "")).strip()
            level = str(r.get("level", "")).strip()
            expl = str(r.get("explanation", "")).strip()
            piece = f"- {title}"
            if level:
                piece += f" (уровень: {level})"
            if expl and expl != title:
                piece += f": {expl}"
            lines.append(piece)
    if not lines:
        lines.append("- Существенных признаков риска по публичной части не выделено.")
    return "\n".join(lines)


def build_summary_messages(facts: Dict[str, Any]) -> Tuple[str, str]:
    """Сообщения для генерации краткого резюме (executive summary)."""
    facts = facts or {}
    user = (
        "Напиши краткое деловое резюме результатов экспресс-проверки сайта "
        "для внутреннего отчёта (5–7 предложений).\n"
        f"Компания: {facts.get('company_name') or 'не указана'}\n"
        f"Сайт: {facts.get('final_url') or facts.get('site_url') or ''}\n"
        f"Отрасль: {facts.get('industry') or ''}\n"
        f"Проверено страниц: {facts.get('pages_checked', 0)}\n"
        f"Найдено форм: {facts.get('forms_count', 0)}\n"
        f"Уровень риска: {facts.get('risk_level', 'не определён')} "
        f"(score {facts.get('risk_score', 0)}, достоверность {facts.get('confidence', 0)}%)\n"
        "Ключевые зоны риска:\n"
        f"{_top_risks_lines(facts, 5)}\n\n"
        "Требования: нейтральный тон, без утверждений о нарушении, укажи, что "
        "выводы предварительные и требуют подтверждения юристом. Верни только "
        "текст резюме без заголовков."
    )
    return SYSTEM_LEGAL_WRITER, user


def build_offer_messages(facts: Dict[str, Any]) -> Tuple[str, str]:
    """Сообщения для генерации коммерческого предложения."""
    facts = facts or {}
    packages = facts.get("packages") or []
    pkg_lines: List[str] = []
    for p in packages:
        if isinstance(p, dict):
            title = str(p.get("title", "")).strip()
            price = str(p.get("price", "")).strip()
            piece = f"- {title}"
            if price:
                piece += f" — {price}"
            pkg_lines.append(piece)
    pkg_block = "\n".join(pkg_lines) if pkg_lines else "- (пакеты услуг уточняются)"

    user = (
        "Составь деловое коммерческое предложение по итогам экспресс-проверки "
        "сайта. Структура: обращение к клиенту → что проверено → 3–5 выявленных "
        "зон риска → почему это важно (нейтрально, без запугивания) → что мы "
        "предлагаем → пакеты услуг с ценами → контакты бюро.\n"
        f"Компания: {facts.get('company_name') or 'клиент'}\n"
        f"Сайт: {facts.get('final_url') or facts.get('site_url') or ''}\n"
        f"Уровень риска: {facts.get('risk_level', 'не определён')}\n"
        "Зоны риска:\n"
        f"{_top_risks_lines(facts, 5)}\n"
        "Пакеты услуг бюро:\n"
        f"{pkg_block}\n"
        f"Бюро: {facts.get('firm_name') or ''}\n"
        f"Контакты бюро: {facts.get('firm_contacts') or ''}\n\n"
        "Требования: деловой спокойный тон, без угроз и без утверждений о "
        "нарушении. Верни только текст предложения."
    )
    return SYSTEM_LEGAL_WRITER, user


def build_email_messages(facts: Dict[str, Any]) -> Tuple[str, str]:
    """Сообщения для генерации короткого письма клиенту."""
    facts = facts or {}
    user = (
        "Напиши короткое деловое письмо клиенту (6–9 строк) по итогам "
        "экспресс-проверки его сайта. Без вложений, приглашает обсудить "
        "результаты.\n"
        f"Компания: {facts.get('company_name') or 'клиент'}\n"
        f"Сайт: {facts.get('final_url') or facts.get('site_url') or ''}\n"
        "Кратко упомяни 3 основные зоны риска:\n"
        f"{_top_risks_lines(facts, 3)}\n"
        f"Бюро: {facts.get('firm_name') or ''}\n"
        f"Контакты: {facts.get('firm_contacts') or ''}\n\n"
        "Требования: деловой спокойный тон, без угроз и без слов «нарушение», "
        "«штраф», «незаконно». Заверши предложением связаться. Верни только "
        "текст письма."
    )
    return SYSTEM_LEGAL_WRITER, user


# ---------------------------------------------------------------------------
# Полные нейтральные шаблоны-фолбэки (когда LLM выключен/недоступен)
# ---------------------------------------------------------------------------
def _fmt_url(result: ScanResult) -> str:
    return result.final_url or result.site_url or ""


def _company_or_default(result: ScanResult, ctx: Optional[ScanContext]) -> str:
    name = result.company_name or (ctx.company_name if ctx else "")
    return name or "Клиент"


def _risk_level_ru(result: ScanResult) -> str:
    return RISK_LEVEL_RU.get(_safe_level(result.risk_level), result.risk_level)


def _top_risks_from_result(result: ScanResult, limit: int = 3) -> List[str]:
    lines: List[str] = []
    try:
        for r in result.top_risks(limit):
            level = RISK_LEVEL_RU.get(_safe_level(r.risk_level), r.risk_level)
            expl = (r.client_friendly_explanation or r.report_phrase or r.title or "").strip()
            title = (r.title or "").strip()
            piece = f"• {title}" if title else "• "
            if level:
                piece += f" (уровень: {level})"
            if expl and expl != title:
                piece += f" — {expl}"
            lines.append(piece)
    except Exception:
        pass
    if not lines:
        lines.append("• Существенных признаков риска по публичной части не выделено.")
    return lines


def summary_template(result: ScanResult, ctx: Optional[ScanContext], settings: Any) -> str:
    """Нейтральный шаблон краткого резюме (без LLM)."""
    company = _company_or_default(result, ctx)
    url = _fmt_url(result)
    level = _risk_level_ru(result)
    risks = _top_risks_from_result(result, 3)

    lines: List[str] = []
    lines.append(
        f"По результатам автоматической экспресс-проверки публичной части сайта "
        f"{url} компании «{company}» подготовлено предварительное заключение."
    )
    lines.append(
        f"Проверено страниц: {result.pages_checked}; обнаружено форм сбора данных: "
        f"{len(result.forms)}; найдено документов: "
        f"{sum(1 for d in result.documents if getattr(d, 'is_accessible', False))}."
    )
    docs_note = "документы обработки персональных данных обнаружены" \
        if any(getattr(d, "is_accessible", False) for d in result.documents) \
        else "публичные документы обработки персональных данных в ходе проверки не обнаружены"
    lines.append(
        f"На страницах {'выявлены' if result.trackers else 'не выявлены'} сторонние "
        f"сервисы и трекеры; {docs_note}."
    )
    lines.append(
        f"Общая оценка зоны риска: {level} (score {result.risk_score}, "
        f"достоверность автоматической оценки {result.confidence}%)."
    )
    lines.append("Наиболее значимые зоны риска, требующие проверки юристом:")
    lines.extend(risks)
    lines.append(
        "Выводы носят предварительный характер, основаны только на публично "
        "доступных страницах и требуют подтверждения юристом."
    )
    return "\n".join(lines)


def _packages_to_ordered(packages: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Развернуть packages из YAML в упорядоченный список согласно order."""
    packages = packages or {}
    items = packages.get("packages") or []
    by_id = {}
    for p in items:
        if isinstance(p, dict) and p.get("id"):
            by_id[p["id"]] = p
    order = packages.get("order") or [p.get("id") for p in items if isinstance(p, dict)]
    ordered: List[Dict[str, Any]] = []
    for pid in order:
        if pid in by_id:
            ordered.append(by_id[pid])
    # Добавим остальные, не попавшие в order.
    for p in items:
        if isinstance(p, dict) and p.get("id") and p["id"] not in {x.get("id") for x in ordered}:
            ordered.append(p)
    return ordered


def offer_template(
    result: ScanResult,
    ctx: Optional[ScanContext],
    settings: Any,
    packages: Optional[Dict[str, Any]],
) -> str:
    """Нейтральный шаблон коммерческого предложения (без LLM)."""
    company = _company_or_default(result, ctx)
    url = _fmt_url(result)
    level = _risk_level_ru(result)
    risks = _top_risks_from_result(result, 5)

    try:
        prices = settings.prices() if settings is not None else {}
    except Exception:
        prices = {}
    firm_name = getattr(settings, "firm_name", "") or "Юридическое бюро"
    firm_contacts = getattr(settings, "firm_contacts", "") or ""
    firm_email = getattr(settings, "firm_email", "") or ""
    firm_phone = getattr(settings, "firm_phone", "") or ""
    firm_website = getattr(settings, "firm_website", "") or ""

    lines: List[str] = []
    lines.append(f"Уважаемые представители компании «{company}»!")
    lines.append("")
    lines.append(
        f"Мы провели автоматическую экспресс-проверку публичной части вашего сайта "
        f"{url} на предмет соответствия требованиям законодательства о персональных "
        f"данных (152-ФЗ)."
    )
    lines.append("")
    lines.append("Что проверено:")
    lines.append(
        f"— публичные страницы (проверено {result.pages_checked}), формы сбора данных "
        f"({len(result.forms)}), доступные документы, cookie и сторонние сервисы, "
        f"технические параметры (HTTPS и др.)."
    )
    lines.append("")
    lines.append("Выявленные зоны риска (предварительно, требуют проверки юристом):")
    lines.extend(risks)
    lines.append("")
    lines.append(
        "Почему это важно: корректно оформленные документы и механика согласий "
        "снижают правовые риски при работе с персональными данными и упрощают "
        "взаимодействие с контролирующими органами. Приведённые выводы носят "
        "предварительный характер и не являются утверждением о нарушении."
    )
    lines.append("")
    lines.append(f"Общая предварительная оценка зоны риска: {level}.")
    lines.append("")
    lines.append("Что мы предлагаем:")
    ordered = _packages_to_ordered(packages)
    if ordered:
        for p in ordered:
            title = str(p.get("title", "")).strip()
            price_key = str(p.get("price_key", "")).strip()
            price = prices.get(price_key, "") if isinstance(prices, dict) else ""
            duration = str(p.get("duration", "")).strip()
            header = f"• {title}"
            if price:
                header += f" — {price}"
            if duration:
                header += f" ({duration})"
            lines.append(header)
            desc = str(p.get("description", "")).strip()
            if desc:
                lines.append(f"  {desc}")
            for it in p.get("items", []) or []:
                lines.append(f"  – {it}")
    else:
        lines.append("• Состав и стоимость услуг уточняются индивидуально.")
    lines.append("")
    lines.append("Контакты:")
    lines.append(firm_name)
    for extra in (firm_contacts, firm_phone, firm_email, firm_website):
        if extra:
            lines.append(extra)
    lines.append("")
    lines.append(
        "Будем рады обсудить результаты проверки и подобрать оптимальный формат "
        "работы."
    )
    return "\n".join(lines)


def email_template(result: ScanResult, ctx: Optional[ScanContext], settings: Any) -> str:
    """Нейтральный шаблон короткого письма клиенту (без LLM)."""
    company = _company_or_default(result, ctx)
    url = _fmt_url(result)
    risks = _top_risks_from_result(result, 3)
    firm_name = getattr(settings, "firm_name", "") or "Юридическое бюро"
    firm_contacts = getattr(settings, "firm_contacts", "") or ""

    lines: List[str] = []
    lines.append(f"Здравствуйте!")
    lines.append("")
    lines.append(
        f"Мы провели экспресс-проверку публичной части сайта {url} "
        f"компании «{company}» на соответствие требованиям о персональных данных."
    )
    lines.append("По результатам выделили несколько зон, которые стоит проверить:")
    lines.extend(risks)
    lines.append("")
    lines.append(
        "Выводы предварительные и требуют подтверждения юристом. Будем рады "
        "обсудить детали и предложить варианты приведения документов и форм "
        "в порядок."
    )
    lines.append("")
    lines.append("С уважением,")
    lines.append(firm_name)
    if firm_contacts:
        lines.append(firm_contacts)
    return "\n".join(lines)
