"""
Генерация PDF-отчёта из HTML.

Стратегия:
  1. Сформировать HTML через `reports.html_renderer.render_report_html`.
  2. Попытаться отрендерить PDF через WeasyPrint.
  3. При ошибке (ImportError/OSError и пр.) — через Playwright (sync API).
  4. Если оба недоступны — сохранить HTML рядом (`.html`) и вернуть False.

Тяжёлые зависимости (weasyprint, playwright) импортируются локально внутри
функций и обёрнуты в try/except, поэтому модуль импортируется даже без них.
Ни одна публичная функция не бросает исключение.
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any, Optional

from reports.html_renderer import render_report_html


def _slugify(value: str) -> str:
    """Безопасное имя файла из произвольной строки (латиница/цифры/дефис)."""
    if not value:
        return "site"
    value = value.strip().lower()
    # Транслитерация простых кириллических символов, чтобы имя файла было ASCII.
    translit = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    value = "".join(translit.get(ch, ch) for ch in value)
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value[:60] or "site"


def _ensure_pdf_dir(settings: Any) -> str:
    """Убедиться, что каталог для PDF существует; вернуть путь к нему."""
    pdf_dir = getattr(settings, "pdf_dir", "") or ""
    if not pdf_dir:
        pdf_dir = os.path.join(os.getcwd(), "exports", "pdf")
    try:
        os.makedirs(pdf_dir, exist_ok=True)
    except Exception:
        pass
    return pdf_dir


def _output_path(result: Any, settings: Any) -> str:
    pdf_dir = _ensure_pdf_dir(settings)
    scan_id = (getattr(result, "scan_id", "") or "").strip()
    if not scan_id:
        scan_id = _slugify(getattr(result, "company_name", "") or getattr(result, "site_url", ""))
    date_str = datetime.now().strftime("%Y%m%d")
    filename = "{}_{}.pdf".format(_slugify(scan_id) if scan_id else "report", date_str)
    return os.path.join(pdf_dir, filename)


def html_to_pdf(html: str, out_path: str, settings: Any) -> bool:
    """Сконвертировать HTML в PDF по адресу out_path. Вернуть True при успехе.

    Порядок попыток: WeasyPrint -> Playwright. При полном сбое HTML пишется
    рядом (out_path с расширением .html) и возвращается False.
    """
    if not html:
        return False

    # Гарантируем существование целевого каталога.
    try:
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        pass

    # 1) WeasyPrint
    try:
        import weasyprint  # type: ignore

        weasyprint.HTML(string=html).write_pdf(out_path)
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return True
    except ImportError:
        pass
    except OSError:
        pass
    except Exception:
        pass

    # 2) Playwright (sync API)
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html, wait_until="load")
                page.pdf(path=out_path, format="A4", print_background=True)
            finally:
                browser.close()
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 0:
            return True
    except Exception:
        pass

    # 3) Полный фолбэк — сохраняем HTML рядом.
    try:
        html_path = out_path[:-4] + ".html" if out_path.lower().endswith(".pdf") else out_path + ".html"
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
    except Exception:
        pass
    return False


def generate_pdf(result: Any, settings: Any, packages: Optional[dict]) -> Optional[str]:
    """Сформировать PDF-отчёт. Вернуть путь к PDF, либо None при неудаче.

    Никогда не бросает исключение.
    """
    try:
        html = render_report_html(result, settings, packages)
    except Exception:
        html = ""
    if not html:
        return None

    try:
        out_path = _output_path(result, settings)
    except Exception:
        out_path = os.path.join(_ensure_pdf_dir(settings), "report_{}.pdf".format(datetime.now().strftime("%Y%m%d%H%M%S")))

    try:
        ok = html_to_pdf(html, out_path, settings)
    except Exception:
        ok = False

    if ok and os.path.isfile(out_path):
        return out_path
    return None
