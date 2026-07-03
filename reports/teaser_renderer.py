"""Client-facing commercial teaser PDF.

The document is intentionally not a full legal report. It shows part of the
free automated work, names several priority risk indicators and sells the next
step: a lawyer-reviewed audit and remediation package.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from legal.pd_rules import PDFinding, build_pd_findings
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


def _truncate(value: Any, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _fmt_money(value: int) -> str:
    try:
        n = int(value)
    except Exception:
        n = 0
    return f"{n:,}".replace(",", " ") + " ₽"


def _domain(url: str) -> str:
    try:
        parsed = urlparse(url if "://" in url else "https://" + url)
        host = parsed.netloc or parsed.path
        return host.split("@")[-1].split(":")[0].removeprefix("www.")
    except Exception:
        return str(url or "").replace("https://", "").replace("http://", "").split("/")[0]


def _looks_like_url(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith(("http://", "https://")) or "." in text and " " not in text


def _split_brand_from_domain(domain: str) -> str:
    label = (domain or "").split(".")[0].replace("-", " ").replace("_", " ").strip()
    if not label:
        return domain or "Компания"
    # A small heuristic for common concatenated brand suffixes.
    suffixes = ["life", "shop", "store", "group", "clinic", "school", "online", "studio", "legal"]
    lowered = label.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix) and len(lowered) > len(suffix) + 2:
            label = lowered[: -len(suffix)] + " " + suffix
            break
    return " ".join(part.capitalize() for part in label.split()) or domain


def _client_name(result: ScanResult) -> str:
    raw = str(getattr(result, "company_name", "") or "").strip()
    if raw and not _looks_like_url(raw):
        return raw
    try:
        for page in getattr(result, "pages", None) or []:
            title = str(getattr(page, "title", "") or "").strip()
            if title and len(title) <= 80 and "404" not in title.lower():
                return title.split("|")[0].split(" - ")[0].strip() or title
    except Exception:
        pass
    site = getattr(result, "site_url", "") or getattr(result, "final_url", "") or raw
    return _split_brand_from_domain(_domain(site))


def _logo_data_uri(settings: Any) -> str:
    paths: List[str] = []
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


def _firm_name(settings: Any) -> str:
    value = str(getattr(settings, "firm_name", "") if settings is not None else "").strip()
    if not value or value.lower() in {"юридическое бюро", "law firm"}:
        return "Nexora Legal"
    return value


def _service_contact(settings: Any) -> str:
    parts = []
    for attr in ("firm_phone", "firm_email", "firm_website"):
        value = getattr(settings, attr, "") if settings is not None else ""
        if value:
            parts.append(str(value))
    return " · ".join(parts)


def _liability_catalog() -> Dict[str, Dict[str, str]]:
    try:
        from reports.html_renderer import _load_liability  # type: ignore

        data = _load_liability()
    except Exception:
        data = {}
    out: Dict[str, Dict[str, str]] = {}
    for item in data.get("items") or []:
        try:
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            out[label.lower()] = {
                "label": label,
                "basis": str(item.get("basis") or "").strip(),
                "amount": str(item.get("amount") or "").strip(),
            }
        except Exception:
            continue
    return out


def _amount_to_int(amount: str) -> int:
    text = str(amount or "").lower()
    if "₽" not in text and "руб" not in text:
        return 0
    digits = re.sub(r"[^0-9]", "", text)
    try:
        return int(digits or "0")
    except Exception:
        return 0


def _liability_details(label: str, catalog: Dict[str, Dict[str, str]]) -> Tuple[str, int, bool]:
    raw = _truncate(label, 220)
    if not raw:
        return "уточняется юристом", 0, True
    item = catalog.get(raw.lower())
    if not item:
        return raw, 0, True
    parts = [item["label"]]
    meta = " · ".join(x for x in [item.get("basis", ""), item.get("amount", "")] if x)
    if meta:
        parts.append(meta)
    amount = _amount_to_int(item.get("amount", ""))
    separate = amount == 0
    return ": ".join(parts), amount, separate


def _liability_summary(findings: Sequence[PDFinding]) -> Dict[str, Any]:
    catalog = _liability_catalog()
    total = 0
    separate = 0
    labels: List[str] = []
    for finding in findings:
        text, amount, is_separate = _liability_details(finding.liability_hint, catalog)
        if text and text not in labels:
            labels.append(text)
        total += amount
        if is_separate:
            separate += 1
    suffix = "+" if separate else ""
    if total <= 0 and separate:
        display = "требует расчёта"
    else:
        display = f"до {_fmt_money(total)}{suffix}"
    return {"display": display, "total": total, "separate": separate, "labels": labels}


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


def _finding_cards(findings: Sequence[PDFinding], liability: Dict[str, Any]) -> str:
    catalog = _liability_catalog()
    if not findings:
        return (
            '<div class="empty-state">'
            "Приоритетные зоны риска не выделены автоматически. Нужна ручная проверка юриста."
            "</div>"
        )
    cards: List[str] = []
    for idx, f in enumerate(findings[:3], start=1):
        level_cls = _LEVEL_CLASS.get(f.risk_level, "unknown")
        liability_text, _, _ = _liability_details(f.liability_hint, catalog)
        cards.append(
            '<article class="risk-card">'
            f'<div class="risk-num">{idx}</div>'
            '<div class="risk-copy">'
            f'<div class="risk-head"><h3>{_escape(f.title)}</h3>'
            f'<span class="pill {level_cls}">{_escape(_risk_label(f.risk_level))}</span></div>'
            f'<p>{_escape(_truncate(f.what_found, 190))}</p>'
            f'<div class="risk-money">{_escape(liability_text)}</div>'
            f'<small>Что сделать: {_escape(_truncate(f.recommendation, 170))}</small>'
            "</div></article>"
        )
    return "\n".join(cards)


def _hidden_groups(findings: Sequence[PDFinding]) -> List[str]:
    ids = {f.id for f in findings}
    groups = []
    checks = [
        ({"PD-01", "PD-03", "PD-04", "PD-05", "PD-06", "PD-07", "PD-08", "PD-09", "PD-31"}, "документы и реквизиты оператора"),
        ({"PD-16", "PD-17", "PD-19", "PD-23", "PD-24"}, "формы, согласия и рассылки"),
        ({"PD-10", "PD-11", "PD-12", "PD-13", "PD-14"}, "cookies, трекеры и сторонние сервисы"),
        ({"PD-26", "PD-27", "PD-28"}, "чувствительные категории данных"),
        ({"PD-22", "PD-40"}, "РКН и техническая часть"),
    ]
    for wanted, label in checks:
        if ids & wanted:
            groups.append(label)
    return groups[:5]


def _package_cards(packages: Optional[dict], settings: Any) -> str:
    cards = _packages(packages, settings)
    if not cards:
        cards = [
            {
                "title": "Экспресс-комплект документов",
                "description": "Политика, согласия, cookie-блок и рекомендации по размещению.",
                "duration": "от 3 рабочих дней",
                "price": "",
            },
            {
                "title": "Полный аудит сайта",
                "description": "Юридическая проверка документов, форм, cookies и сторонних сервисов.",
                "duration": "от 7 рабочих дней",
                "price": "",
            },
            {
                "title": "Внедрение под ключ",
                "description": "Документы, техническое ТЗ, проверка внедрения и сопровождение.",
                "duration": "по договорённости",
                "price": "",
            },
        ]
    parts = []
    for p in cards:
        meta = " · ".join(x for x in [p["duration"], p["price"]] if x)
        parts.append(
            '<div class="package-card">'
            f'<h3>{_escape(p["title"])}</h3>'
            f'<p>{_escape(_truncate(p["description"], 110))}</p>'
            + (f'<strong>{_escape(meta)}</strong>' if meta else "")
            + "</div>"
        )
    return "\n".join(parts)


def render_teaser_html(
    result: ScanResult,
    settings: Any,
    packages: Optional[dict] = None,
) -> str:
    """Render a polished two-page commercial proposal. Never raises."""
    try:
        findings = build_pd_findings(result)
        shown = findings[:3]
        hidden_count = max(0, len(findings) - len(shown))
        liability = _liability_summary(findings)
        client = _client_name(result)
        site = getattr(result, "site_url", "") or getattr(result, "final_url", "")
        domain = _domain(site)
        firm = _firm_name(settings)
        contact = _service_contact(settings)
        logo = _logo_data_uri(settings)
        date = str(getattr(result, "created_at", "") or "")[:10]
        if not date:
            from datetime import datetime

            date = datetime.now().strftime("%Y-%m-%d")
        pd_forms = len(
            [f for f in (getattr(result, "forms", None) or []) if getattr(f, "potentially_personal_data_form", False)]
        )
        docs_found = len(
            [
                d
                for d in (getattr(result, "documents", None) or [])
                if getattr(d, "is_accessible", False) or getattr(d, "link_confirmed", False)
            ]
        )
        groups = _hidden_groups(findings)
        groups_html = "".join(f"<li>{_escape(g)}</li>" for g in groups) or "<li>документы, формы, cookies и техническая часть</li>"
        logo_html = (
            f'<img src="{logo}" alt="Nexora Legal">'
            if logo
            else f'<strong>{_escape(firm)}</strong>'
        )

        return f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>Краткое коммерческое предложение</title>
  <style>{_CSS}</style>
</head>
<body>
  <section class="sheet sheet-one">
    <header class="topbar">
      <div class="brand">{logo_html}</div>
      <div class="doc-meta">
        <span>Бесплатная экспресс-проверка</span>
        <strong>{_escape(date)}</strong>
      </div>
    </header>

    <div class="hero">
      <div class="hero-copy">
        <p class="eyebrow">Коммерческое предложение по результатам первичной проверки</p>
        <h1>{_escape(client)}</h1>
        <p class="domain">{_escape(domain)}</p>
        <p class="lead">
          Мы бесплатно проверили публичную часть сайта и показываем часть найденных
          зон риска. Полный список, доказательства и план исправлений раскрываются
          в рамках юридического аудита.
        </p>
      </div>
      <aside class="impact-card">
        <span>Ориентир по открытым категориям ответственности*</span>
        <strong>{_escape(liability["display"])}</strong>
        <small>не является юридической квалификацией и не складывается автоматически</small>
      </aside>
    </div>

    <div class="proof-strip">
      <div><strong>{int(getattr(result, "pages_checked", 0) or 0)}</strong><span>страниц проверено</span></div>
      <div><strong>{len(getattr(result, "forms", None) or [])}</strong><span>форм найдено</span></div>
      <div><strong>{pd_forms}</strong><span>форм с ПДн</span></div>
      <div><strong>{docs_found}</strong><span>документов найдено</span></div>
    </div>

    <div class="section-title">
      <p class="eyebrow">Показываем часть бесплатной работы</p>
      <h2>3 приоритетные зоны, которые стоит разобрать первыми</h2>
    </div>
    <div class="risk-list">{_finding_cards(shown, liability)}</div>
  </section>

  <section class="sheet sheet-two">
    <div class="closing-grid">
      <div class="hidden-card">
        <p class="eyebrow">Что не раскрыто в этой выжимке</p>
        <h2>Ещё {hidden_count} зон риска ждут полного разбора</h2>
        <p>
          В полной версии мы показываем URL, скриншоты/фрагменты, документы,
          чек-лист и конкретные правки. Сейчас оставляем только контур, чтобы
          не превращать бесплатную проверку в полноценное заключение.
        </p>
        <ul>{groups_html}</ul>
      </div>
      <div class="cta-card">
        <p class="eyebrow">Следующий шаг</p>
        <h2>Разберём всё и дадим план исправлений</h2>
        <ol>
          <li>Проверим документы и формы юристом.</li>
          <li>Подготовим тексты политики, согласий и cookie-блока.</li>
          <li>Дадим ТЗ разработчику и проверим внедрение.</li>
        </ol>
      </div>
    </div>

    <div class="liability-box">
      <div>
        <span>Справочный ориентир по всем найденным зонам</span>
        <strong>{_escape(liability["display"])}</strong>
      </div>
      <p>
        Суммы указаны как ориентир по категориям возможной ответственности.
        Они требуют ручной квалификации, не являются обещанием санкций и не
        заменяют юридическое заключение.
      </p>
    </div>

    <div class="section-title compact">
      <p class="eyebrow">Форматы работы</p>
      <h2>Что предлагаем</h2>
    </div>
    <div class="package-grid">{_package_cards(packages, settings)}</div>

    <footer>
      <div>
        <strong>{_escape(firm)}</strong>
        <span>{_escape(contact)}</span>
      </div>
      <div class="footer-note">
        Проверка охватывает только публичную часть сайта: без CRM, CMS, серверной
        логики, договоров с обработчиками и журналов согласий.
      </div>
    </footer>
  </section>
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
@page { size: A4; margin: 0; }
* { box-sizing: border-box; }
body {
  margin: 0;
  background: #eef2f7;
  color: #151923;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
  line-height: 1.34;
}
.sheet {
  width: 210mm;
  min-height: 297mm;
  padding: 18mm 17mm 16mm;
  background: #fbfcfe;
  page-break-after: always;
  overflow: hidden;
}
.sheet-two { page-break-after: auto; background: #f7f9fc; }
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 16px;
}
.brand {
  min-width: 144px;
  height: 42px;
  display: flex;
  align-items: center;
}
.brand img {
  max-width: 144px;
  max-height: 42px;
  object-fit: contain;
  background: #fff;
  border: 1px solid #e6e9ef;
  border-radius: 10px;
  padding: 8px 12px;
}
.doc-meta { text-align: right; color: #677083; font-size: 11px; }
.doc-meta strong { display: block; color: #111827; font-size: 13px; margin-top: 3px; }
.hero {
  display: grid;
  grid-template-columns: 1fr 255px;
  gap: 22px;
  padding: 24px;
  border-radius: 22px;
  background:
    radial-gradient(circle at 92% 20%, rgba(105, 154, 214, .34), transparent 30%),
    linear-gradient(135deg, #07111f 0%, #12355b 58%, #1e4e8c 100%);
  color: #fff;
}
.eyebrow {
  margin: 0 0 8px;
  color: #2c64a6;
  font-size: 9.5px;
  line-height: 1.2;
  letter-spacing: .1em;
  text-transform: uppercase;
  font-weight: 800;
}
.hero .eyebrow { color: #95d4ff; }
h1, h2, h3, p { margin-top: 0; }
h1 {
  margin: 0 0 5px;
  font-size: 38px;
  line-height: 1.02;
  letter-spacing: 0;
}
.domain {
  margin: 0 0 16px;
  color: #c8d9ec;
  font-size: 14px;
}
.lead {
  max-width: 600px;
  margin: 0;
  color: #eef6ff;
  font-size: 14px;
}
.impact-card {
  align-self: stretch;
  padding: 18px;
  border: 1px solid rgba(255,255,255,.22);
  border-radius: 18px;
  background: rgba(255,255,255,.11);
}
.impact-card span, .impact-card small {
  display: block;
  color: rgba(255,255,255,.76);
  font-size: 11px;
}
.impact-card strong {
  display: block;
  margin: 12px 0 10px;
  color: #ffe1d6;
  font-size: 30px;
  line-height: 1.05;
}
.proof-strip {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 14px 0 18px;
}
.proof-strip div {
  padding: 12px;
  border: 1px solid #dfe6ef;
  border-radius: 14px;
  background: #fff;
}
.proof-strip strong { display: block; color: #12355b; font-size: 24px; line-height: 1; }
.proof-strip span { display: block; margin-top: 5px; color: #667085; font-size: 11px; }
.section-title { margin-bottom: 9px; }
.section-title h2 {
  margin: 0;
  font-size: 24px;
  line-height: 1.12;
  letter-spacing: 0;
}
.section-title.compact h2 { font-size: 22px; }
.risk-list {
  display: grid;
  gap: 9px;
}
.risk-card {
  display: grid;
  grid-template-columns: 38px 1fr;
  gap: 12px;
  padding: 13px 14px;
  border: 1px solid #dde5ef;
  border-radius: 16px;
  background: #fff;
  break-inside: avoid;
}
.risk-num {
  width: 34px;
  height: 34px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 11px;
  background: #eaf2ff;
  color: #12355b;
  font-weight: 900;
}
.risk-head {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  align-items: start;
}
.risk-head h3 {
  margin: 0 0 4px;
  font-size: 15px;
  line-height: 1.18;
}
.risk-card p {
  margin-bottom: 7px;
  color: #2a3443;
  font-size: 12.5px;
}
.pill {
  white-space: nowrap;
  border-radius: 999px;
  padding: 4px 8px;
  font-size: 9px;
  font-weight: 900;
  text-transform: uppercase;
}
.pill.low { background: #d1fae5; color: #065f46; }
.pill.medium { background: #eaf2ff; color: #12355b; }
.pill.high { background: #ffedd5; color: #9a3412; }
.pill.critical { background: #fee2e2; color: #991b1b; }
.pill.unknown { background: #e5e7eb; color: #374151; }
.risk-money {
  display: inline-block;
  max-width: 100%;
  margin-bottom: 7px;
  padding: 7px 9px;
  border-radius: 10px;
  background: #fff5ed;
  color: #8a3416;
  font-size: 11px;
  font-weight: 750;
}
.risk-card small {
  display: block;
  color: #667085;
  font-size: 10.5px;
}
.closing-grid {
  display: grid;
  grid-template-columns: 1.1fr .9fr;
  gap: 13px;
  margin-bottom: 13px;
}
.hidden-card, .cta-card, .liability-box, .package-card, .package-note {
  border: 1px solid #dfe6ef;
  border-radius: 18px;
  background: #fff;
  box-shadow: 0 1px 0 rgba(17, 24, 39, .03);
}
.hidden-card, .cta-card { padding: 18px; min-height: 250px; }
.hidden-card h2, .cta-card h2 {
  margin-bottom: 10px;
  font-size: 25px;
  line-height: 1.08;
}
.hidden-card p, .cta-card li, .footer-note {
  color: #4b5565;
  font-size: 12.5px;
}
.hidden-card ul, .cta-card ol {
  margin: 10px 0 0;
  padding-left: 19px;
}
.hidden-card li, .cta-card li { margin-bottom: 6px; }
.liability-box {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 16px;
  align-items: center;
  margin-bottom: 16px;
  padding: 16px 18px;
  background: linear-gradient(135deg, #0b1727 0%, #12355b 100%);
  color: #fff;
}
.liability-box span {
  display: block;
  color: #b9d7f5;
  font-size: 10.5px;
  text-transform: uppercase;
  letter-spacing: .08em;
  font-weight: 800;
}
.liability-box strong {
  display: block;
  margin-top: 6px;
  color: #ffe1d6;
  font-size: 28px;
  line-height: 1.05;
}
.liability-box p {
  margin: 0;
  color: #e6edf7;
  font-size: 11.5px;
}
.package-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px;
}
.package-card, .package-note {
  padding: 14px;
  min-height: 145px;
}
.package-card h3 {
  margin-bottom: 8px;
  font-size: 14.5px;
  line-height: 1.18;
}
.package-card p {
  min-height: 47px;
  margin-bottom: 10px;
  color: #596579;
  font-size: 11px;
}
.package-card strong {
  color: #12355b;
  font-size: 11.5px;
}
footer {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 18px;
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid #dfe6ef;
}
footer strong { display: block; font-size: 13px; color: #111827; }
footer span { display: block; margin-top: 4px; color: #667085; font-size: 11px; }
.footer-note { text-align: right; font-size: 10.5px; }
.empty-state {
  padding: 18px;
  border-radius: 16px;
  background: #fff;
  border: 1px solid #dfe6ef;
  color: #4b5565;
}
@media print {
  body { background: #fff; }
  .sheet { box-shadow: none; }
}
"""
