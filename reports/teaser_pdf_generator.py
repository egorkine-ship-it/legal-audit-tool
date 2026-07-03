"""PDF generation for the compact commercial teaser report."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from reports.pdf_generator import _ensure_pdf_dir, _slugify, html_to_pdf
from reports.teaser_renderer import render_teaser_html


def _output_path(result: Any, settings: Any) -> str:
    pdf_dir = _ensure_pdf_dir(settings)
    scan_id = (getattr(result, "scan_id", "") or "").strip()
    if not scan_id:
        scan_id = getattr(result, "company_name", "") or getattr(result, "site_url", "") or "site"
    date_str = datetime.now().strftime("%Y%m%d")
    filename = "teaser_{}_{}.pdf".format(_slugify(scan_id), date_str)
    return os.path.join(pdf_dir, filename)


def generate_teaser_pdf(result: Any, settings: Any, packages: Optional[dict] = None) -> Optional[str]:
    """Generate the short commercial proposal PDF. Never raises."""
    try:
        html = render_teaser_html(result, settings, packages)
    except Exception:
        html = ""
    if not html:
        return None

    try:
        out_path = _output_path(result, settings)
    except Exception:
        out_path = os.path.join(
            _ensure_pdf_dir(settings),
            "teaser_{}.pdf".format(datetime.now().strftime("%Y%m%d%H%M%S")),
        )

    try:
        ok = html_to_pdf(html, out_path, settings)
    except Exception:
        ok = False
    if ok and os.path.isfile(out_path):
        return out_path
    return None
