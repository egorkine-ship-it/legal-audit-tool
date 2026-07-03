"""Client-facing one/two page commercial teaser report.

This renderer is separate from the full internal audit report. It uses the
compact deterministic findings from legal.pd_rules and does not include raw
HTML, full document text or exhaustive checklists.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
from typing import Any, Dict, List, Optional

from legal.pd_rules import PDFinding, build_pd_findings, commercial_score
from scanner.models import RISK_LEVEL_RU, RiskLevel, ScanResult


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSET_LOGO = os.path.join(_ROOT, "assets", "nexora_logo_app.jpg")

_LEVEL_CLASS = {
    RiskLevel.low.value: "low",
    RiskLevel.medium.value: "medium",
    RiskLevel.high.value: "high",
    RiskLevel.critical.value: "critical",
    RiskLevel.unknown.value: "unknown",
}


def _escape(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _truncate(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _logo_data_uri(settings: Any) -> str:
    paths = []
    custom = getattr(settings, "logo_path", "") if settings is not None else ""
    if custom:
        paths.append(custom)
    paths.append(_ASSET_LOGO)
    for path in paths:
        try:
            if not path or not os.path.isfile(path):
                continue
            mime, _ = mimetypes.guess_type(path)
            if not mime or not mime.startswith("image"):
                mime = "image/jpeg"
            with open(path, "rb") as fh:
                data = fh.read()
            if not data:
                continue
            return "data:{};base64,{}".format(mime, base64.b64encode(data).decode("ascii"))
        except Exception:
            continue
    return ""


def _risk_label(level: str) -> str:
    try:
        return RISK_LEVEL_RU.get(RiskLevel(level), level)
    except Exception:
        return level or "не определён"


def _liability_text(label: str) -> str:
    """Return client-facing liability hint, using data/liability.yml when possible."""
    raw = _truncate(label, 220)
    if not raw:
        return "уточняется юристом по итогам полного аудита"
    try:
        from reports.html_renderer import _load_liability  # type: ignore

        data = _load_liability()
        for item in data.get("items") or []:
            item_label = str(item.get("label") or "").strip()
            if item_label.lower() != raw.lower():
                continue
            amount = str(item.get("amount") or "").strip()
            basis = str(item.get("basis") or "").strip()
            details = " · ".join(x for x in [basis, amount] if x)
            return f"{item_label}: {details}" if details else item_label
    except Exception:
        pass
    return raw


def _finding_cards(findings: List[PDFinding]) -> str:
    if not findings:
        return (
            '<div class="empty-state">'
            "Автоматическая проверка не выделила приоритетные зоны риска. "
            "Рекомендуется ручная юридическая проверка документов и механики сайта."
            "</div>"
        )
    parts: List[str] = []
    for idx, f in enumerate(findings[:4], start=1):
        level_cls = _LEVEL_CLASS.get(f.risk_level, "unknown")
        evidence = f.evidence_quote or f.evidence_url
        parts.append(
            '<article class="finding-card">'
            f'<div class="finding-index">{idx:02d}</div>'
            '<div class="finding-main">'
            f'<div class="finding-top"><h3>{_escape(f.title)}</h3>'
            f'<span class="pill {level_cls}">{_escape(_risk_label(f.risk_level))}</span></div>'
            f'<p class="finding-text">{_escape(f.what_found)}</p>'
            f'<p class="fine-line"><strong>Возможная категория ответственности:</strong> '
            f'{_escape(_liability_text(f.liability_hint))}</p>'
            f'<p class="small"><strong>Что сделать:</strong> {_escape(f.recommendation)}</p>'
            + (f'<p class="evidence">Доказательство: {_escape(_truncate(evidence, 210))}</p>' if evidence else "")
            + "</div></article>"
        )
    return "\n".join(parts)


def _packages(packages: Optional[dict], settings: Any) -> List[Dict[str, str]]:
    try:
        from reports.html_renderer import _ordered_packages  # type: ignore

        ordered = _ordered_packages(packages, settings)
    except Exception:
        ordered = []
    out: List[Dict[str, str]] = []
    for p in ordered[:3]:
        try:
            out.append(
                {
                    "title": str(p.get("title") or ""),
                    "duration": str(p.get("duration") or ""),
                    "price": str(p.get("price") or ""),
                    "description": str(p.get("description") or ""),
                }
            )
        except Exception:
            continue
    return out


def _package_cards(packages: Optional[dict], settings: Any) -> str:
    cards = _packages(packages, settings)
    if not cards:
        return ""
    parts = []
    for p in cards:
        meta = " · ".join(x for x in [p["duration"], p["price"]] if x)
        parts.append(
            '<div class="package-card">'
            f'<h3>{_escape(p["title"])}</h3>'
            f'<p>{_escape(p["description"])}</p>'
            + (f'<div class="package-meta">{_escape(meta)}</div>' if meta else "")
            + "</div>"
        )
    return "\n".join(parts)


def _top_stats(result: ScanResult, findings: List[PDFinding], hidden_count: int) -> str:
    docs_found = 0
    try:
        docs_found = len(
            [
                d
                for d in getattr(result, "documents", None) or []
                if getattr(d, "is_accessible", False) or getattr(d, "link_confirmed", False)
            ]
        )
    except Exception:
        docs_found = 0
    stats = [
        ("Страниц проверено", getattr(result, "pages_checked", 0) or 0),
        ("Форм найдено", len(getattr(result, "forms", None) or [])),
        ("Документов найдено", docs_found),
        ("Ещё зон риска", hidden_count),
    ]
    return "".join(
        f'<div class="stat"><span>{_escape(value)}</span><small>{_escape(label)}</small></div>'
        for label, value in stats
    )


def _service_contact(settings: Any) -> str:
    parts = []
    for attr in ("firm_phone", "firm_email", "firm_website"):
        value = getattr(settings, attr, "") if settings is not None else ""
        if value:
            parts.append(str(value))
    return " · ".join(parts)


def render_teaser_html(
    result: ScanResult,
    settings: Any,
    packages: Optional[dict] = None,
) -> str:
    """Render compact commercial proposal HTML. Never raises."""
    try:
        findings = build_pd_findings(result)
        score, level = commercial_score(findings)
        top_findings = findings[:4]
        hidden_count = max(0, len(findings) - len(top_findings))
        level_cls = _LEVEL_CLASS.get(level, "unknown")
        firm_name = getattr(settings, "firm_name", "") or "Nexora Legal"
        logo = _logo_data_uri(settings)
        contact = _service_contact(settings)
        site = getattr(result, "site_url", "") or getattr(result, "final_url", "")
        company = getattr(result, "company_name", "") or site or "Компания"
        date = str(getattr(result, "created_at", "") or "")[:10]
        if not date:
            from datetime import datetime

            date = datetime.now().strftime("%Y-%m-%d")

        package_html = _package_cards(packages, settings)
        stats_html = _top_stats(result, findings, hidden_count)
        cards_html = _finding_cards(top_findings)
        hidden_note = (
            f"Дополнительно автоматическая проверка выделила ещё {hidden_count} "
            "зон риска. Их целесообразно раскрывать уже в полном аудите: с "
            "проверкой документов, механики согласий и технической настройки сайта."
            if hidden_count
            else "Дополнительные зоны риска не выделены автоматически, но ручная проверка юриста всё равно нужна."
        )
        logo_html = (
            f'<img src="{logo}" alt="Nexora Legal" class="logo-img">'
            if logo
            else f'<div class="logo-text">{_escape(firm_name)}</div>'
        )

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Краткое коммерческое предложение</title>
  <style>{_CSS}</style>
</head>
<body>
  <main class="page">
    <section class="hero">
      <header class="topbar">
        <div class="brand">{logo_html}</div>
        <div class="meta">
          <span>Экспресс-анализ сайта</span>
          <strong>{_escape(date)}</strong>
        </div>
      </header>
      <div class="hero-grid">
        <div>
          <p class="eyebrow">Краткая выжимка для первичного обсуждения</p>
          <h1>{_escape(company)}</h1>
          <p class="site">{_escape(site)}</p>
          <p class="lead">
            Мы проверили публичную часть сайта и выделили приоритетные зоны риска
            по документам, формам сбора персональных данных, cookies и сторонним сервисам.
          </p>
        </div>
        <aside class="score-card {level_cls}">
          <small>Предварительный уровень</small>
          <strong>{_escape(_risk_label(level))}</strong>
          <span>{min(score, 100)} / 100</span>
        </aside>
      </div>
      <div class="stats">{stats_html}</div>
    </section>

    <section class="findings">
      <div class="section-head">
        <p class="eyebrow">Что видно уже сейчас</p>
        <h2>Приоритетные зоны риска</h2>
      </div>
      {cards_html}
    </section>

    <section class="next">
      <div class="next-main">
        <p class="eyebrow">Что осталось за рамками этой выжимки</p>
        <h2>Полный разбор нужен для подтверждения выводов</h2>
        <p>{_escape(hidden_note)}</p>
        <p class="disclaimer">
          Суммы ответственности указаны справочно и не складываются автоматически.
          Этот документ не является юридическим заключением: выводы требуют проверки юристом,
          а автоматический анализ видит только публичную часть сайта.
        </p>
      </div>
      <div class="steps">
        <h3>Предлагаем закрыть вопрос в три шага</h3>
        <ol>
          <li>Проверить документы и формы вручную.</li>
          <li>Подготовить корректные тексты политики, согласий и cookie-блока.</li>
          <li>Передать технические правки разработчику и проверить внедрение.</li>
        </ol>
      </div>
    </section>

    <section class="packages">
      <div class="section-head">
        <p class="eyebrow">Варианты работы</p>
        <h2>Как можем помочь</h2>
      </div>
      <div class="package-grid">{package_html}</div>
    </section>

    <footer>
      <strong>{_escape(firm_name)}</strong>
      <span>{_escape(contact)}</span>
    </footer>
  </main>
</body>
</html>"""
    except Exception as exc:
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>"
            "<h1>Краткое коммерческое предложение</h1>"
            "<p>Документ не удалось сформировать полностью. Требуется ручная проверка.</p>"
            f"<p>{_escape(exc)}</p></body></html>"
        )


_CSS = """
@page { size: A4; margin: 12mm; }
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #f5f7fb;
  color: #111827;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
  line-height: 1.38;
}
.page {
  max-width: 980px;
  margin: 0 auto;
  background: #ffffff;
  border: 1px solid #e5e7eb;
}
.hero {
  background: linear-gradient(135deg, #07111f 0%, #12355b 58%, #1e4e8c 100%);
  color: #fff;
  padding: 30px 34px 24px;
}
.topbar, .hero-grid, .next {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 24px;
  align-items: start;
}
.brand { width: 190px; height: 46px; display: flex; align-items: center; }
.logo-img { max-width: 190px; max-height: 46px; object-fit: contain; background: #fff; border-radius: 8px; padding: 8px 12px; }
.logo-text { font-size: 18px; font-weight: 700; letter-spacing: 0; }
.meta { text-align: right; font-size: 12px; color: rgba(255,255,255,.76); }
.meta strong { display: block; color: #fff; margin-top: 4px; }
.eyebrow {
  margin: 0 0 8px;
  font-size: 10px;
  letter-spacing: .08em;
  text-transform: uppercase;
  font-weight: 700;
  color: #8fd0ff;
}
h1, h2, h3, p { margin-top: 0; }
h1 { font-size: 34px; line-height: 1.04; margin-bottom: 6px; letter-spacing: 0; }
h2 { font-size: 22px; line-height: 1.14; margin-bottom: 14px; letter-spacing: 0; }
h3 { font-size: 15px; line-height: 1.2; margin-bottom: 8px; letter-spacing: 0; }
.site { margin-bottom: 18px; color: #c8dcf5; font-size: 13px; }
.lead { max-width: 620px; color: #eef6ff; font-size: 15px; margin-bottom: 0; }
.score-card {
  min-width: 170px;
  border-radius: 12px;
  background: rgba(255,255,255,.12);
  border: 1px solid rgba(255,255,255,.22);
  padding: 18px;
  text-align: left;
}
.score-card small, .score-card span { display: block; color: rgba(255,255,255,.78); font-size: 12px; }
.score-card strong { display: block; margin: 10px 0 4px; font-size: 23px; text-transform: uppercase; }
.score-card.low strong { color: #a7f3d0; }
.score-card.medium strong { color: #fde68a; }
.score-card.high strong { color: #fed7aa; }
.score-card.critical strong { color: #fecaca; }
.stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-top: 24px;
}
.stat {
  background: rgba(255,255,255,.12);
  border: 1px solid rgba(255,255,255,.18);
  border-radius: 10px;
  padding: 12px;
}
.stat span { display: block; font-size: 22px; font-weight: 800; }
.stat small { display: block; color: rgba(255,255,255,.72); font-size: 11px; }
.findings, .next, .packages { padding: 24px 34px 0; }
.section-head .eyebrow, .next .eyebrow { color: #1e4e8c; }
.finding-card {
  display: grid;
  grid-template-columns: 50px 1fr;
  gap: 14px;
  padding: 16px 0;
  border-top: 1px solid #e5e7eb;
  break-inside: avoid;
}
.finding-index {
  width: 42px; height: 42px;
  border-radius: 10px;
  background: #eef5ff;
  color: #12355b;
  display: flex; align-items: center; justify-content: center;
  font-weight: 800;
}
.finding-top {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 12px;
  align-items: start;
}
.pill {
  white-space: nowrap;
  border-radius: 999px;
  padding: 5px 9px;
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
}
.pill.low { background: #d1fae5; color: #065f46; }
.pill.medium { background: #fef3c7; color: #92400e; }
.pill.high { background: #ffedd5; color: #9a3412; }
.pill.critical { background: #fee2e2; color: #991b1b; }
.pill.unknown { background: #e5e7eb; color: #374151; }
.finding-text { margin-bottom: 8px; color: #263241; }
.fine-line {
  margin-bottom: 7px;
  padding: 8px 10px;
  border-radius: 8px;
  background: #fff7ed;
  color: #7c2d12;
  font-size: 12px;
}
.small, .evidence, .disclaimer, footer span { color: #64748b; font-size: 12px; }
.evidence { margin-bottom: 0; }
.next {
  grid-template-columns: 1fr 280px;
  align-items: stretch;
}
.next-main, .steps, .package-card, .empty-state {
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 16px;
  break-inside: avoid;
}
.steps ol { margin: 0; padding-left: 20px; color: #334155; font-size: 13px; }
.steps li { margin-bottom: 7px; }
.package-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.package-card p { color: #475569; font-size: 12px; min-height: 48px; }
.package-meta { color: #12355b; font-weight: 800; font-size: 12px; }
footer {
  margin: 24px 34px 0;
  padding: 15px 0 20px;
  border-top: 1px solid #e5e7eb;
  display: flex;
  justify-content: space-between;
  gap: 20px;
  font-size: 12px;
}
@media print {
  body { background: #fff; }
  .page { border: none; }
  .hero, .finding-card, .next-main, .steps, .package-card { break-inside: avoid; }
}
"""
