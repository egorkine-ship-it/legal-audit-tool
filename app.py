"""
Экспресс-аудит публичной части сайтов на признаки рисков по 152-ФЗ.

Локальный Streamlit-интерфейс. Запуск:
    streamlit run app.py

ВАЖНО: приложение анализирует только публичную часть сайта и формирует
ПРЕДВАРИТЕЛЬНЫЕ выводы («признаки риска»). Перед отправкой клиенту любой отчёт,
коммерческое предложение и письмо должны быть проверены юристом.
"""
from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import streamlit as st

import auth
from config.settings import Settings, load_settings, save_settings
from database import repositories
from database.db import init_db
from scanner.models import (
    INDUSTRIES,
    RISK_LEVEL_RU,
    SCAN_STATUS_RU,
    RiskLevel,
    ScanInput,
    ScanResult,
    ScanStatus,
)
from scanner.orchestrator import run_scan

st.set_page_config(
    page_title="Экспресс-аудит сайтов · 152-ФЗ",
    page_icon="🛡️",
    layout="wide",
)

RISK_EMOJI = {
    RiskLevel.unknown.value: "⚪️",
    RiskLevel.low.value: "🟢",
    RiskLevel.medium.value: "🟡",
    RiskLevel.high.value: "🟠",
    RiskLevel.critical.value: "🔴",
}


# ---------------------------------------------------------------------------
# Настройки / инициализация
# ---------------------------------------------------------------------------
def get_settings() -> Settings:
    if "settings" not in st.session_state:
        st.session_state["settings"] = load_settings()
    return st.session_state["settings"]


def ensure_db(settings: Settings) -> None:
    try:
        init_db(database_url=settings.database_url, sqlite_path=settings.db_path)
    except Exception as exc:  # pragma: no cover
        st.warning(f"Не удалось инициализировать базу данных: {exc}")


def _load_packages(settings: Settings) -> dict:
    try:
        import yaml

        with open(settings.packages_path(), "r", encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _get_or_regenerate_pdf(result: ScanResult, settings: Settings) -> Optional[bytes]:
    """
    Байты PDF для скачивания. Если файл на диске отсутствует (эфемерная ФС в
    облаке после перезапуска) — перегенерируем отчёт из сохранённого результата.
    """
    data = _pdf_bytes(result.pdf_path)
    if data:
        return data
    try:
        from reports import pdf_generator

        path = pdf_generator.generate_pdf(result, settings, _load_packages(settings))
        if path:
            try:
                repositories.update_pdf_path(result.scan_id, path, settings)
            except Exception:
                pass
            return _pdf_bytes(path)
    except Exception:
        pass
    return None


def _risk_ru(level: str) -> str:
    """Русское название уровня риска, безопасно при неизвестном значении."""
    try:
        return RISK_LEVEL_RU[RiskLevel(level)]
    except Exception:
        return level or "—"


def risk_badge(level: str) -> str:
    return f"{RISK_EMOJI.get(level, '⚪️')} {_risk_ru(level)}"


def _pdf_bytes(path: str) -> Optional[bytes]:
    try:
        p = Path(path)
        if p.exists() and p.is_file():
            return p.read_bytes()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Отрисовка результата
# ---------------------------------------------------------------------------
def render_result_summary(result: ScanResult) -> None:
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Risk score", result.risk_score)
    c2.metric("Уровень риска", risk_badge(result.risk_level))
    c3.metric("Confidence", f"{result.confidence}/100")
    c4.metric("Проверено страниц", result.pages_checked)

    if result.errors:
        with st.expander(f"⚠️ Замечания сканера ({len(result.errors)})"):
            for e in result.errors:
                st.text(f"• {e}")

    st.info(
        "Автоматическая проверка публичной части сайта не заменяет полноценный "
        "юридический аудит. Выводы предварительные и требуют подтверждения юристом."
    )

    st.subheader("Ключевые признаки риска")
    top = result.top_risks(7)
    if not top:
        st.write("Существенных признаков риска не выявлено автоматически "
                 "(требуется ручная проверка).")
    for r in top:
        with st.container(border=True):
            st.markdown(
                f"**{RISK_EMOJI.get(r.risk_level, '⚪️')} {r.title}** "
                f"— _{_risk_ru(r.risk_level)}_, +{r.score}"
            )
            if r.report_phrase:
                st.write(r.report_phrase)
            if r.recommendation:
                st.caption(f"Рекомендация: {r.recommendation}")

    colf, cold, colt = st.columns(3)
    with colf:
        st.subheader("Формы")
        st.write(f"Найдено форм: **{len(result.forms)}**")
        pd_forms = [f for f in result.forms if f.potentially_personal_data_form]
        st.write(f"Из них с признаками сбора ПДн: **{len(pd_forms)}**")
    with cold:
        st.subheader("Документы")
        st.write(f"Найдено документов: **{len(result.documents)}**")
        types = sorted({d.doc_type for d in result.documents if d.is_accessible})
        st.write(", ".join(types) if types else "—")
    with colt:
        st.subheader("Cookies / трекеры")
        st.write(f"Трекеров: **{len(result.trackers)}**")
        st.write(f"Cookie-баннер: {'да' if result.cookie_banner_found else 'нет'}")
        st.write(f"Иностранные сервисы: {'да' if result.foreign_trackers_found else 'нет'}")


def render_texts_and_downloads(result: ScanResult) -> None:
    st.divider()
    cols = st.columns(3)
    pdf = _get_or_regenerate_pdf(result, get_settings())
    with cols[0]:
        if pdf:
            st.download_button(
                "⬇️ Скачать PDF-отчёт",
                data=pdf,
                file_name=(Path(result.pdf_path).name if result.pdf_path else f"report_{result.scan_id}.pdf"),
                mime="application/pdf",
                key=f"pdf_{result.scan_id}",
                use_container_width=True,
            )
        else:
            st.caption("PDF не сформирован (см. замечания сканера).")
    with cols[1]:
        st.download_button(
            "⬇️ Коммерческое предложение (.txt)",
            data=(result.commercial_offer_text or "").encode("utf-8"),
            file_name=f"kp_{result.scan_id}.txt",
            mime="text/plain",
            key=f"kp_{result.scan_id}",
            use_container_width=True,
        )
    with cols[2]:
        st.download_button(
            "⬇️ Письмо (.txt)",
            data=(result.email_text or "").encode("utf-8"),
            file_name=f"email_{result.scan_id}.txt",
            mime="text/plain",
            key=f"em_{result.scan_id}",
            use_container_width=True,
        )

    with st.expander("📄 Коммерческое предложение (нажмите иконку копирования)"):
        st.code(result.commercial_offer_text or "—", language="markdown")
    with st.expander("✉️ Письмо для первичного контакта"):
        st.code(result.email_text or "—", language="markdown")
    with st.expander("🧾 Резюме"):
        st.write(result.executive_summary or "—")
    with st.expander("🔧 JSON результата"):
        st.json(json.loads(result.model_dump_json()))


# ---------------------------------------------------------------------------
# Вкладка 1: Проверка одного сайта
# ---------------------------------------------------------------------------
def tab_single(settings: Settings) -> None:
    st.header("Проверка одного сайта")
    with st.form("single_form"):
        c1, c2 = st.columns(2)
        with c1:
            url = st.text_input("URL сайта", placeholder="example.ru")
            company = st.text_input("Название компании")
            industry = st.selectbox("Отрасль", INDUSTRIES, index=0)
        with c2:
            email = st.text_input("Email компании (необязательно)")
            comment = st.text_area("Комментарий (необязательно)", height=80)
            max_pages = st.number_input(
                "Лимит страниц", min_value=1, max_value=200, value=int(settings.max_pages)
            )
        cc1, cc2 = st.columns(2)
        use_llm = cc1.checkbox(
            "Использовать LLM для анализа документов",
            value=bool(settings.enable_llm and settings.llm_api_key),
        )
        create_pdf = cc2.checkbox("Создать PDF", value=True)
        submitted = st.form_submit_button("🚀 Запустить проверку", use_container_width=True)

    if submitted:
        if not url.strip():
            st.error("Укажите URL сайта.")
            return
        scan_input = ScanInput(
            company_name=company.strip(),
            site_url=url.strip(),
            industry=industry,
            email=email.strip(),
            comment=comment.strip(),
            max_pages=int(max_pages),
            use_llm=bool(use_llm),
            create_pdf=bool(create_pdf),
        )
        status = st.status("Запуск проверки…", expanded=True)

        def cb(msg: str) -> None:
            status.update(label=msg)
            status.write(msg)

        try:
            result = run_scan(scan_input, settings, progress_cb=cb)
            status.update(label="Проверка завершена", state="complete")
        except Exception as exc:  # предохранитель
            status.update(label="Ошибка проверки", state="error")
            st.error(f"Ошибка: {exc}")
            return

        try:
            repositories.save_scan(result, settings)
        except Exception as exc:
            st.warning(f"Не удалось сохранить результат в базу: {exc}")

        st.session_state["last_result"] = result.scan_id
        render_result_summary(result)
        render_texts_and_downloads(result)


# ---------------------------------------------------------------------------
# Вкладка 2: Массовая проверка
# ---------------------------------------------------------------------------
def _read_table(uploaded) -> "Optional[object]":
    import pandas as pd

    name = (uploaded.name or "").lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(uploaded)
        return pd.read_excel(uploaded)
    except Exception as exc:
        st.error(f"Не удалось прочитать файл: {exc}")
        return None


def tab_bulk(settings: Settings) -> None:
    import pandas as pd

    st.header("Массовая проверка")
    st.caption(
        "Ожидаемые столбцы: company_name, site_url, industry, email, comment. "
        "Обязателен только site_url."
    )
    uploaded = st.file_uploader("CSV или XLSX", type=["csv", "xlsx"])
    if uploaded is None:
        return

    df = _read_table(uploaded)
    if df is None:
        return
    st.subheader("Предпросмотр")
    st.dataframe(df, use_container_width=True, height=240)

    cc1, cc2, cc3 = st.columns(3)
    b_max = cc1.number_input("Лимит страниц на сайт", 1, 200, int(settings.max_pages))
    b_llm = cc2.checkbox("LLM-анализ", value=bool(settings.enable_llm and settings.llm_api_key))
    b_pdf = cc3.checkbox("Создавать PDF", value=True)

    if not st.button("🚀 Запустить массовую проверку", use_container_width=True):
        return

    rows = df.to_dict(orient="records")
    progress = st.progress(0.0)
    log_box = st.empty()
    results: List[ScanResult] = []

    for i, row in enumerate(rows, start=1):
        site = str(row.get("site_url") or row.get("url") or "").strip()
        if not site or site.lower() == "nan":
            continue
        company = str(row.get("company_name") or "").strip()
        industry = str(row.get("industry") or "auto").strip() or "auto"
        if industry not in INDUSTRIES:
            industry = "auto"
        scan_input = ScanInput(
            company_name="" if company.lower() == "nan" else company,
            site_url=site,
            industry=industry,
            email="" if str(row.get("email") or "").lower() == "nan" else str(row.get("email") or "").strip(),
            comment="" if str(row.get("comment") or "").lower() == "nan" else str(row.get("comment") or "").strip(),
            max_pages=int(b_max),
            use_llm=bool(b_llm),
            create_pdf=bool(b_pdf),
        )
        log_box.info(f"[{i}/{len(rows)}] Проверка: {site}")

        def cb(msg: str, _s=site) -> None:
            log_box.info(f"[{i}/{len(rows)}] {_s} · {msg}")

        try:
            res = run_scan(scan_input, settings, progress_cb=cb)
        except Exception as exc:
            res = ScanResult(
                scan_id=f"err{i}", company_name=company, site_url=site,
                risk_level=RiskLevel.unknown.value, errors=[str(exc)],
            )
        try:
            repositories.save_scan(res, settings)
        except Exception:
            pass
        results.append(res)
        progress.progress(i / max(1, len(rows)))

    log_box.success(f"Готово. Проверено сайтов: {len(results)}")
    st.session_state["bulk_results"] = [r.scan_id for r in results]

    # Итоговая таблица
    table = [_summary_row(r) for r in results]
    res_df = pd.DataFrame(table)
    st.subheader("Итоги")
    st.dataframe(res_df, use_container_width=True)

    # Экспорт XLSX
    xlsx = _results_to_xlsx(results)
    st.download_button(
        "⬇️ Скачать результаты (XLSX)",
        data=xlsx,
        file_name=f"scan_results_{datetime.now():%Y%m%d_%H%M}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    # ZIP всех PDF
    zip_bytes = _pdfs_to_zip(results)
    if zip_bytes:
        st.download_button(
            "⬇️ Скачать все PDF (ZIP)",
            data=zip_bytes,
            file_name=f"scan_pdfs_{datetime.now():%Y%m%d_%H%M}.zip",
            mime="application/zip",
        )


def _summary_row(r: ScanResult) -> dict:
    docs = {d.doc_type for d in r.documents if d.is_accessible}
    return {
        "company_name": r.company_name,
        "site_url": r.site_url,
        "industry": r.industry,
        "email": r.email,
        "risk_level": r.risk_level,
        "risk_score": r.risk_score,
        "confidence": r.confidence,
        "top_risks": "; ".join(x.title for x in r.top_risks(3)),
        "forms_found": len(r.forms),
        "privacy_policy_found": "privacy_policy" in docs,
        "consent_found": "consent" in docs,
        "cookie_banner_found": r.cookie_banner_found,
        "trackers_found": len(r.trackers),
        "foreign_trackers_found": r.foreign_trackers_found,
        "pdf_path": r.pdf_path,
        "status": r.status,
    }


def _results_to_xlsx(results: List[ScanResult]) -> bytes:
    import pandas as pd

    rows = []
    for r in results:
        row = _summary_row(r)
        row["email_text"] = r.email_text
        row["commercial_offer_text"] = r.commercial_offer_text
        rows.append(row)
    df = pd.DataFrame(rows)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="results")
    return buf.getvalue()


def _pdfs_to_zip(results: List[ScanResult]) -> Optional[bytes]:
    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for r in results:
            data = _pdf_bytes(r.pdf_path)
            if data:
                zf.writestr(Path(r.pdf_path).name, data)
                count += 1
    return buf.getvalue() if count else None


# ---------------------------------------------------------------------------
# Вкладка 3: История
# ---------------------------------------------------------------------------
def tab_history(settings: Settings) -> None:
    import pandas as pd

    st.header("История проверок")
    try:
        scans = repositories.list_scans(settings, limit=1000)
    except Exception as exc:
        st.error(f"Не удалось загрузить историю: {exc}")
        return

    if not scans:
        st.info("История пуста. Запустите первую проверку во вкладке «Проверка одного сайта».")
        return

    df = pd.DataFrame(scans)
    show_cols = [c for c in [
        "scan_id", "created_at", "company_name", "site_url", "industry",
        "risk_score", "risk_level", "confidence", "top_risks", "status", "pdf_path",
    ] if c in df.columns]
    st.dataframe(df[show_cols], use_container_width=True, height=360)

    st.divider()
    st.subheader("Управление лидом")
    ids = [s["scan_id"] for s in scans]
    sel = st.selectbox(
        "Выберите проверку",
        ids,
        format_func=lambda x: _scan_label(scans, x),
    )
    scan = next((s for s in scans if s["scan_id"] == sel), None)
    if not scan:
        return

    c1, c2, c3 = st.columns([2, 1, 1])
    with c1:
        statuses = [s.value for s in ScanStatus]
        cur = scan.get("status") or ScanStatus.scanned.value
        new_status = st.selectbox(
            "Статус",
            statuses,
            index=statuses.index(cur) if cur in statuses else 0,
            format_func=lambda x: SCAN_STATUS_RU.get(ScanStatus(x), x),
        )
        if st.button("💾 Сохранить статус"):
            try:
                repositories.update_status(sel, new_status, settings)
                st.success("Статус обновлён.")
            except Exception as exc:
                st.error(f"Ошибка: {exc}")
    with c2:
        if st.button("🔍 Подробности", use_container_width=True):
            st.session_state["detail_scan_id"] = sel
            st.info("Откройте вкладку «Подробности проверки».")
    with c3:
        res = repositories.get_scan(sel, settings)
        pdf = _get_or_regenerate_pdf(res, settings) if res else None
        if pdf:
            st.download_button(
                "⬇️ PDF",
                data=pdf,
                file_name=f"report_{sel}.pdf",
                mime="application/pdf",
                use_container_width=True,
                key=f"hist_pdf_{sel}",
            )
        else:
            st.caption("PDF недоступен")


def _scan_label(scans: List[dict], scan_id: str) -> str:
    s = next((x for x in scans if x["scan_id"] == scan_id), None)
    if not s:
        return scan_id
    return f"{s.get('created_at','')} · {s.get('company_name') or s.get('site_url')} · {s.get('risk_level')}"


# ---------------------------------------------------------------------------
# Вкладка 4: Подробности
# ---------------------------------------------------------------------------
def tab_details(settings: Settings) -> None:
    st.header("Подробности проверки")
    try:
        scans = repositories.list_scans(settings, limit=1000)
    except Exception as exc:
        st.error(f"Не удалось загрузить список: {exc}")
        return
    if not scans:
        st.info("Нет данных.")
        return

    ids = [s["scan_id"] for s in scans]
    default = st.session_state.get("detail_scan_id") or st.session_state.get("last_result")
    idx = ids.index(default) if default in ids else 0
    sel = st.selectbox("scan_id", ids, index=idx, format_func=lambda x: _scan_label(scans, x))

    result = repositories.get_scan(sel, settings)
    if result is None:
        st.error("Не удалось загрузить результат.")
        return

    st.subheader(f"{result.company_name or result.site_url}")
    st.caption(f"{result.site_url} → {result.final_url}")
    render_result_summary(result)

    with st.expander("🌐 Проверенные страницы", expanded=False):
        for p in result.pages:
            st.markdown(f"- [{p.title or p.url}]({p.url}) · код {p.status_code} · форм: {len(p.forms)}")

    with st.expander("📝 Формы и согласия"):
        _render_forms(result)

    with st.expander("📚 Документы и чек-листы"):
        _render_documents(result)

    with st.expander("🍪 Cookies и сторонние сервисы"):
        _render_cookies_trackers(result)

    with st.expander("🔒 Техническая проверка"):
        t = result.technical
        st.write({
            "HTTPS": t.https_enabled,
            "Редирект на HTTPS": t.http_to_https_redirect,
            "Mixed content": t.mixed_content_found,
            "IP": ", ".join(t.ip_addresses) or "—",
            "Страна сервера": t.server_country,
            "CDN": t.cdn_detected or "—",
            "robots.txt": t.robots_txt_found,
            "sitemap": t.sitemap_found,
        })

    render_texts_and_downloads(result)


def _render_forms(result: ScanResult) -> None:
    import pandas as pd

    if not result.forms:
        st.write("Формы не обнаружены.")
        return
    rows = []
    for f in result.forms:
        rows.append({
            "страница": f.page_url,
            "тип": f.form_type,
            "поля": ", ".join(f.personal_data_fields) or "—",
            "чекбокс": "да" if f.consent.checkbox_found else "нет",
            "ссылка на политику": "да" if f.consent.privacy_link_found else "нет",
            "предустановлен": {True: "да", False: "нет", None: "?"}.get(f.consent.checkbox_prechecked, "?"),
            "ПДн": "да" if f.potentially_personal_data_form else "нет",
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True)


def _render_documents(result: ScanResult) -> None:
    if not result.documents:
        st.write("Документы не обнаружены.")
        return
    for d in result.documents:
        st.markdown(f"**{d.doc_type}** — [{d.title or d.url}]({d.url}) · формат {d.format} · "
                    f"{'доступен' if d.is_accessible else 'недоступен'}")
        if d.template_placeholder_detected:
            st.warning("Обнаружены признаки шаблонных незаполненных полей.")
        if d.extraction_error:
            st.caption(f"Извлечение текста: {d.extraction_error}")
        an = d.analysis
        if an:
            st.caption(f"Полнота: {an.overall_completeness}/100 · "
                       f"{'LLM' if an.llm_used else 'эвристика'}")
            if an.summary:
                st.write(an.summary)
            missing = [i for i in an.checklist_results
                       if i.status == "not_found" and i.risk_if_missing in ("high", "critical")]
            if missing:
                st.markdown("_Критичные отсутствующие элементы:_")
                for i in missing[:12]:
                    st.markdown(f"- {i.label} ({i.risk_if_missing})")
            if an.conflicts:
                st.markdown("_Конфликты:_")
                for c in an.conflicts:
                    st.markdown(f"- {c.type}: {c.comment}")
        st.divider()


def _render_cookies_trackers(result: ScanResult) -> None:
    import pandas as pd

    st.write(f"Cookie-баннер: **{'найден' if result.cookie_banner_found else 'не найден'}**")
    st.write(f"Маркетинговые cookies/запросы до согласия: "
             f"**{'да' if result.marketing_cookies_before_consent else 'нет'}**")
    if result.trackers:
        st.dataframe(pd.DataFrame([{
            "провайдер": t.provider_name,
            "категория": t.category,
            "страна": t.country_hint,
            "риск": t.legal_risk,
            "домен": t.matched_domain,
        } for t in result.trackers]), use_container_width=True)
    else:
        st.write("Трекеры не обнаружены.")
    if result.cookies:
        st.markdown("_Cookies (до взаимодействия):_")
        st.dataframe(pd.DataFrame([{
            "имя": c.name, "провайдер": c.provider, "категория": c.category,
            "трекинг": c.is_tracking,
        } for c in result.cookies]), use_container_width=True)


# ---------------------------------------------------------------------------
# Вкладка 5: Настройки
# ---------------------------------------------------------------------------
def tab_settings(settings: Settings) -> None:
    st.header("Настройки")
    st.caption(
        "Изменения сохраняются в базе данных и переживают перезапуск сервиса. "
        "Секреты (пароль администратора, SESSION_SECRET, DATABASE_URL) задаются "
        "через переменные окружения хостинга."
    )

    with st.form("settings_form"):
        st.subheader("LLM")
        cp, c1 = st.columns([1, 2])
        llm_provider = cp.text_input("LLM provider", value=settings.llm_provider)
        llm_api_key = c1.text_input("LLM API key", value=settings.llm_api_key, type="password")
        c2, c3 = st.columns(2)
        llm_base_url = c2.text_input("LLM base URL", value=settings.llm_base_url)
        llm_model = c3.text_input("LLM model", value=settings.llm_model)
        enable_llm = st.checkbox("Включить LLM", value=settings.enable_llm)

        st.subheader("Сканирование")
        c4, c5, c6 = st.columns(3)
        max_pages = c4.number_input("Максимум страниц", 1, 200, int(settings.max_pages))
        page_timeout = c5.number_input("Таймаут страницы (мс)", 1000, 120000, int(settings.page_timeout_ms), step=1000)
        delay = c6.number_input("Задержка между страницами (с)", 0.0, 30.0, float(settings.delay_between_pages_s), step=0.5)
        c7, c8, c9 = st.columns(3)
        enable_geoip = c7.checkbox("Включить GeoIP", value=settings.enable_geoip)
        enable_shots = c8.checkbox("Включить скриншоты", value=settings.enable_screenshots)
        geoip_db = c9.text_input("Путь к GeoIP базе (mmdb)", value=settings.geoip_db_path)

        st.subheader("Юридическое бюро")
        c10, c11 = st.columns(2)
        firm_name = c10.text_input("Название бюро", value=settings.firm_name)
        lawyer_name = c11.text_input("ФИО юриста/менеджера", value=settings.lawyer_name)
        firm_address = st.text_input("Адрес", value=settings.firm_address)
        c12, c13, c14 = st.columns(3)
        firm_email = c12.text_input("Email бюро", value=settings.firm_email)
        firm_phone = c13.text_input("Телефон бюро", value=settings.firm_phone)
        firm_website = c14.text_input("Сайт бюро", value=settings.firm_website)
        firm_contacts = st.text_input("Доп. контакты", value=settings.firm_contacts)
        logo_path = st.text_input("Путь к логотипу для PDF (необязательно)", value=settings.logo_path)

        st.subheader("Цены услуг")
        c15, c16 = st.columns(2)
        price_express_audit = c15.text_input("Экспресс-аудит", value=settings.price_express_audit)
        price_express_docs = c16.text_input("Комплект документов для сайта", value=settings.price_express_docs)
        c17, c18 = st.columns(2)
        price_full_audit = c17.text_input("Полный аудит 152-ФЗ", value=settings.price_full_audit)
        price_turnkey = c18.text_input("Сопровождение под ключ", value=settings.price_turnkey)

        saved = st.form_submit_button("💾 Сохранить настройки", use_container_width=True)

    if saved:
        new = settings.model_copy(update=dict(
            llm_provider=llm_provider,
            llm_api_key=llm_api_key, llm_base_url=llm_base_url, llm_model=llm_model,
            enable_llm=enable_llm, max_pages=int(max_pages), page_timeout_ms=int(page_timeout),
            delay_between_pages_s=float(delay), enable_geoip=enable_geoip,
            enable_screenshots=enable_shots, geoip_db_path=geoip_db,
            firm_name=firm_name, lawyer_name=lawyer_name, firm_address=firm_address,
            firm_email=firm_email, firm_phone=firm_phone, firm_website=firm_website,
            firm_contacts=firm_contacts, logo_path=logo_path,
            price_express_audit=price_express_audit, price_express_docs=price_express_docs,
            price_full_audit=price_full_audit, price_turnkey=price_turnkey,
        ))
        save_settings(new)
        st.session_state["settings"] = new
        st.success("Настройки сохранены (в базе данных).")

    # --- Управление доступом администратора ---
    st.divider()
    st.subheader("Администратор и доступ")
    st.caption(f"Логин администратора: **{settings.admin_email}** "
               "(меняется через переменную окружения ADMIN_EMAIL).")
    with st.form("admin_pw_form"):
        st.markdown("**Смена пароля администратора**")
        p1 = st.text_input("Новый пароль (мин. 8 символов)", type="password")
        p2 = st.text_input("Повторите новый пароль", type="password")
        pw_saved = st.form_submit_button("🔑 Обновить пароль")
    if pw_saved:
        if p1 != p2:
            st.error("Пароли не совпадают.")
        else:
            ok, msg = auth.set_admin_password(p1, settings)
            (st.success if ok else st.error)(msg)
            if ok:
                st.session_state["settings"] = load_settings()

    # --- Инфраструктура (только чтение) ---
    with st.expander("ℹ️ Инфраструктура (переменные окружения)"):
        st.write({
            "База данных": "PostgreSQL (DATABASE_URL)" if settings.database_url else f"SQLite: {settings.db_path}",
            "Каталог экспорта (EXPORTS_DIR)": settings.exports_dir,
            "LLM включён": settings.enable_llm,
            "GeoIP включён": settings.enable_geoip,
            "Скриншоты включены": settings.enable_screenshots,
        })
        st.caption("Эти параметры задаются переменными окружения на хостинге и здесь не редактируются.")


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def tab_dashboard(settings: Settings) -> None:
    st.header("Дашборд")
    stats = repositories.dashboard_stats(settings)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Всего проверок", stats.get("total", 0))
    c2.metric("Высокий/критический риск", stats.get("high_critical", 0))
    by = stats.get("by_level", {})
    c3.metric("Критический", by.get("critical", 0))
    c4.metric("Высокий", by.get("high", 0))

    if st.button("➕ Новая проверка", type="primary"):
        st.session_state["nav"] = "Проверка одного сайта"
        st.rerun()

    st.subheader("Последние проверки")
    recent = stats.get("recent", [])
    if not recent:
        st.info("Проверок пока нет. Нажмите «Новая проверка».")
        return
    import pandas as pd

    cols = [c for c in ["created_at", "company_name", "site_url", "industry",
                        "risk_level", "risk_score", "confidence", "status"] if recent and c in recent[0]]
    st.dataframe(pd.DataFrame(recent)[cols], use_container_width=True, height=320)


# ---------------------------------------------------------------------------
# Документация / ограничения
# ---------------------------------------------------------------------------
def tab_docs(settings: Settings) -> None:
    st.header("Документация и ограничения")
    st.markdown(
        """
### Что делает система
Автоматический экспресс-анализ **публично доступной** части сайта на признаки
возможных рисков и несоответствий требованиям законодательства РФ о персональных
данных (152-ФЗ), а также смежных рисков по cookie, рекламным рассылкам, публичным
документам, сторонним сервисам и трансграничной передаче.

### Важное ограничение
Выводы системы — это **признаки риска / зоны риска, требующие проверки**, а **не**
окончательное юридическое заключение и не установление факта нарушения.

Автоматическая проверка видит только публичную часть сайта и **не имеет доступа** к:
CRM, CMS/admin-панели, базе данных, серверной логике, договорам с обработчиками,
внутренним локальным актам, журналам согласий, уведомлениям РКН, фактическому месту
хранения базы, процессам удаления/уточнения ПДн и переписке с субъектами ПДн.

### Как корректно использовать результаты
- Рассматривайте отчёт как **основу для ручной проверки юристом**.
- Перед отправкой клиенту отчёт, коммерческое предложение и письмо **должен проверить
  юрист**.
- Формулировки в отчёте намеренно нейтральны («обнаружены признаки», «требуется
  проверка») — сохраняйте этот тон в коммуникации с клиентом.
- Система **не отправляет** письма и КП автоматически — только формирует тексты.

### Правила сканирования
Проверяются только публичные страницы; авторизация и CAPTCHA не обходятся; формы
не отправляются; нагрузочное тестирование и пентест не выполняются; соблюдаются
лимит страниц и задержки; используется идентифицирующий user-agent.
        """
    )


# ---------------------------------------------------------------------------
# Авторизация
# ---------------------------------------------------------------------------
def _login_screen(settings: Settings) -> None:
    st.title("🛡️ Экспресс-аудит сайтов · 152-ФЗ")
    st.subheader("Вход")
    if not auth.is_configured(settings):
        st.error(
            "Пароль администратора не задан. Установите переменную окружения "
            "**ADMIN_PASSWORD** (и **ADMIN_EMAIL**) на хостинге и перезапустите сервис."
        )
    with st.form("login_form"):
        email = st.text_input("Email", value="")
        password = st.text_input("Пароль", type="password")
        submitted = st.form_submit_button("Войти", use_container_width=True)
    if submitted:
        if auth.check_credentials(email, password, settings):
            st.session_state["authenticated"] = True
            st.session_state["auth_email"] = email.strip()
            st.rerun()
        else:
            st.error("Неверный email или пароль.")
    st.caption("Доступ только для авторизованных сотрудников бюро.")


def _is_authenticated() -> bool:
    return bool(st.session_state.get("authenticated"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
MENU = [
    "Дашборд",
    "Проверка одного сайта",
    "Массовая проверка",
    "История",
    "Подробности проверки",
    "Настройки",
    "Документация",
]

PAGES = {
    "Дашборд": tab_dashboard,
    "Проверка одного сайта": tab_single,
    "Массовая проверка": tab_bulk,
    "История": tab_history,
    "Подробности проверки": tab_details,
    "Настройки": tab_settings,
    "Документация": tab_docs,
}


def main() -> None:
    settings = get_settings()
    ensure_db(settings)

    if not _is_authenticated():
        _login_screen(settings)
        return

    # --- Боковое меню ---
    st.sidebar.markdown(f"### 🛡️ {settings.firm_name or 'Экспресс-аудит'}")
    st.sidebar.caption(f"Вы вошли как: {st.session_state.get('auth_email', settings.admin_email)}")
    if st.sidebar.button("Выйти"):
        st.session_state["authenticated"] = False
        st.session_state.pop("auth_email", None)
        st.rerun()

    if not (settings.enable_llm and settings.llm_api_key):
        st.sidebar.warning("LLM не настроена — только эвристический анализ.")

    nav = st.session_state.get("nav", MENU[0])
    if nav not in MENU:
        nav = MENU[0]
    choice = st.sidebar.radio("Меню", MENU, index=MENU.index(nav))
    st.session_state["nav"] = choice

    st.sidebar.divider()
    st.sidebar.caption(
        "Автоматическая проверка не заменяет юридический аудит. "
        "Выводы — признаки риска, требующие проверки юристом."
    )

    page = PAGES.get(choice, tab_dashboard)
    page(settings)


if __name__ == "__main__":
    main()
