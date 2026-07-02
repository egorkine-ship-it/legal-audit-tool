"""
Тесты усечения текста для LLM и фильтра релевантности документов.

Проверяется:
  (a) legal.document_analyzer.analyze_document передаёт в LLM-клиент УСЕЧЁННЫЙ
      текст (<= ~12000 символов), тогда как document.text остаётся полным, а
      эвристический результат по-прежнему вычисляется;
  (b) scanner.document_finder.is_relevant_document / filter_relevant_documents:
      документ того же зарегистрированного домена релевантен (True), чужого —
      нет (False); фильтр выбрасывает off-domain и сохраняет same-domain.

Оба модуля могут собираться параллельно, поэтому импортируются через
importorskip — тест не должен падать на этапе сбора.
"""
from __future__ import annotations

import pytest

from scanner.models import DocCandidate, DocumentResult, ScanContext

document_analyzer = pytest.importorskip("legal.document_analyzer")
document_finder = pytest.importorskip("scanner.document_finder")


# ---------------------------------------------------------------------------
# Фейковый LLM-клиент: захватывает текст, переданный в analyze_document
# ---------------------------------------------------------------------------
class _CapturingLLM:
    """Мок LLM: включён, запоминает текст документа, отданный ему на анализ."""

    enabled = True

    def __init__(self):
        self.captured_text = None
        self.called = False

    def analyze_document(self, doc_text, doc_type, items, facts):
        self.called = True
        self.captured_text = doc_text
        # Возвращаем безвредный пустой payload — слияние оставит эвристику.
        return {"checklist_results": [], "conflicts": []}


def _make_checklist():
    """Минимальный чек-лист: один пункт с эвристическим ключевым словом."""
    return {
        "privacy_policy": [
            {
                "id": "p1",
                "label": "Указаны цели обработки персональных данных",
                "keywords": ["цели обработки"],
                "applies_when": "always",
                "risk_if_missing": "medium",
            }
        ]
    }


# ---------------------------------------------------------------------------
# (a) Усечение текста, передаваемого в LLM
# ---------------------------------------------------------------------------
def test_llm_receives_truncated_text_while_doc_stays_full():
    # Документ на 40000 символов: начинается с содержательного фрагмента
    # (чтобы эвристика сработала), затем длинный «хвост».
    head = "Цели обработки персональных данных: обработка заявок. "
    full_text = head + ("абвгд " * 8000)  # заведомо > 40000 символов
    assert len(full_text) >= 40000

    doc = DocumentResult(
        doc_id="d1",
        doc_type="privacy_policy",
        url="https://example.ru/privacy",
        text=full_text,
        is_accessible=True,
    )
    ctx = ScanContext()
    llm = _CapturingLLM()

    analysis = document_analyzer.analyze_document(
        doc, "privacy_policy", ctx, settings=None,
        checklists=_make_checklist(), llm_client=llm,
    )

    # LLM был вызван и получил усечённый текст.
    assert llm.called is True
    assert llm.captured_text is not None
    cap = getattr(document_analyzer, "_LLM_DOC_CHARS", 12000)
    assert len(llm.captured_text) <= cap
    assert len(llm.captured_text) <= cap + 100  # «<= ~12000»

    # Полный текст документа НЕ изменился.
    assert doc.text == full_text
    assert len(doc.text) >= 40000

    # Эвристический результат по-прежнему вычислен (пункт чек-листа присутствует).
    assert len(analysis.checklist_results) == 1
    assert analysis.checklist_results[0].id == "p1"
    # Эвристика видела полный текст: цель обработки найдена.
    assert analysis.checklist_results[0].status == "found"


# ---------------------------------------------------------------------------
# (b) Фильтр релевантности документов
# ---------------------------------------------------------------------------
def test_is_relevant_document_same_domain_true():
    doc = DocCandidate(url="https://www.example.ru/privacy", doc_type="privacy_policy")
    assert document_finder.is_relevant_document(doc, "https://example.ru/") is True


def test_is_relevant_document_other_domain_false():
    doc = DocCandidate(url="https://partner-site.com/privacy", doc_type="privacy_policy")
    assert document_finder.is_relevant_document(doc, "https://example.ru/") is False


def test_is_relevant_document_missing_url_false():
    doc = DocCandidate(url="", doc_type="privacy_policy")
    assert document_finder.is_relevant_document(doc, "https://example.ru/") is False


def test_filter_relevant_documents_drops_offdomain_keeps_same():
    same = DocCandidate(url="https://example.ru/policy", doc_type="privacy_policy")
    other = DocCandidate(url="https://foreign.org/policy", doc_type="privacy_policy")
    filtered = document_finder.filter_relevant_documents([same, other], "https://example.ru/")
    urls = [d.url for d in filtered]
    assert "https://example.ru/policy" in urls
    assert "https://foreign.org/policy" not in urls
    assert len(filtered) == 1


def test_filter_relevant_documents_never_raises_on_bad_input():
    # Не бросает на None и на «мусорных» объектах.
    assert document_finder.filter_relevant_documents(None, "https://example.ru/") == []
    assert isinstance(
        document_finder.filter_relevant_documents([object()], "https://example.ru/"),
        list,
    )
