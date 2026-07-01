"""
Общие утилиты: нормализация URL, работа с доменами, безопасный HTTP GET,
ключевые слова и регулярные выражения для эвристик.

Модуль не должен импортировать другие модули проекта (кроме моделей), чтобы
избежать циклических зависимостей.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse

try:
    import tldextract

    # Работаем офлайн: не тянем список публичных суффиксов из сети.
    _EXTRACT = tldextract.TLDExtract(suffix_list_urls=())
except Exception:  # pragma: no cover
    tldextract = None
    _EXTRACT = None


# ---------------------------------------------------------------------------
# URL / домены
# ---------------------------------------------------------------------------
def normalize_url(raw: str) -> str:
    """example.ru -> https://example.ru; чистит пробелы; добавляет схему."""
    if not raw:
        return ""
    raw = raw.strip()
    if not raw:
        return ""
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw):
        raw = "https://" + raw
    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc or parsed.path
    path = parsed.path if parsed.netloc else ""
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, ""))


def to_http(url: str) -> str:
    """Заменить https:// на http:// (фолбэк при недоступности https)."""
    p = urlparse(url)
    return urlunparse(("http", p.netloc, p.path, p.params, p.query, p.fragment))


def get_host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def get_registered_domain(url_or_host: str) -> str:
    """example.co.uk из https://www.example.co.uk/path (best-effort)."""
    host = get_host(url_or_host) or url_or_host.lower()
    host = host.split("/")[0]
    if _EXTRACT is not None:
        ext = _EXTRACT(host)
        if ext.domain and ext.suffix:
            return f"{ext.domain}.{ext.suffix}"
        if ext.domain:
            return ext.domain
    # Фолбэк без tldextract: две последние части.
    parts = host.split(".")
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


def same_registered_domain(a: str, b: str) -> bool:
    ra, rb = get_registered_domain(a), get_registered_domain(b)
    return bool(ra) and ra == rb


def is_external(url: str, base_url: str) -> bool:
    host = get_host(url)
    if not host:
        return False
    return not same_registered_domain(url, base_url)


def absolute_url(base: str, href: str) -> str:
    if not href:
        return ""
    href = href.strip()
    if href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
        return ""
    try:
        return urljoin(base, href)
    except Exception:
        return ""


def strip_fragment(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, ""))


def is_probably_html_url(url: str) -> bool:
    lowered = url.lower().split("?")[0]
    bad_ext = (
        ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico", ".css",
        ".js", ".woff", ".woff2", ".ttf", ".mp4", ".mp3", ".zip", ".rar",
        ".xml", ".json",
    )
    return not lowered.endswith(bad_ext)


def url_extension(url: str) -> str:
    path = urlparse(url).path.lower()
    m = re.search(r"\.([a-z0-9]{1,5})$", path)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Текст
# ---------------------------------------------------------------------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def truncate(text: str, n: int = 500) -> str:
    if text is None:
        return ""
    text = text.strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def normalize_for_search(text: str) -> str:
    """Нижний регистр + ё->е + схлопывание пробелов для поиска ключевых слов."""
    if not text:
        return ""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"\s+", " ", text)
    return text


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    norm = normalize_for_search(text)
    for kw in keywords:
        if normalize_for_search(kw) in norm:
            return True
    return False


def find_first(text: str, keywords: Iterable[str]) -> Optional[str]:
    norm = normalize_for_search(text)
    for kw in keywords:
        nk = normalize_for_search(kw)
        if nk and nk in norm:
            return kw
    return None


def find_all_matches(text: str, keywords: Iterable[str]) -> List[str]:
    norm = normalize_for_search(text)
    found = []
    for kw in keywords:
        nk = normalize_for_search(kw)
        if nk and nk in norm:
            found.append(kw)
    return found


# ---------------------------------------------------------------------------
# Регулярные выражения для реквизитов / контактов
# ---------------------------------------------------------------------------
# ОГРН — 13 цифр, ОГРНИП — 15 цифр, ИНН — 10 или 12 цифр. Порядок важен:
# сначала более длинные, чтобы не «съесть» их короткими шаблонами.
RE_OGRNIP = re.compile(r"\b\d{15}\b")
RE_OGRN = re.compile(r"\b\d{13}\b")
RE_INN = re.compile(r"\b\d{12}\b|\b\d{10}\b")
RE_INN_LABELED = re.compile(r"ИНН[\s:№]*?(\d{10}|\d{12})", re.IGNORECASE)
RE_OGRN_LABELED = re.compile(r"ОГРН(?:ИП)?[\s:№]*?(\d{13}|\d{15})", re.IGNORECASE)
RE_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
RE_PHONE = re.compile(
    r"(?:\+7|8|7)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
RE_DATE = re.compile(
    r"\b\d{1,2}[.\-/]\d{1,2}[.\-/]\d{2,4}\b"
    r"|\b\d{1,2}\s+(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*\s+\d{4}"
    r"|\b\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}\b",
    re.IGNORECASE,
)
RE_VERSION = re.compile(
    r"(?:верси[яи]|version|редакци[яи]|ред\.)\s*[:№]?\s*([0-9]+(?:\.[0-9]+)*)",
    re.IGNORECASE,
)

# Шаблонные незаполненные плейсхолдеры.
PLACEHOLDER_PATTERNS = [
    r"\[[^\]]{0,40}\]",              # [указать], [●], [наименование]
    r"\{\{[^}]{0,40}\}\}",           # {{company}}
    r"_{3,}",                        # ______
    r"«?\bромашк\w*\b»?",            # ООО «Ромашка»
    r"наименовани\w*\s+оператора",
    r"адрес\w*\s+оператора",
    r"указать",
    r"вставить",
    r"ваш[аеиуый]*\s+компани[яи]",
    r"пример\w*\s+ооо",
]
RE_PLACEHOLDER = re.compile("|".join(PLACEHOLDER_PATTERNS), re.IGNORECASE)


def find_inn(text: str) -> List[str]:
    labeled = RE_INN_LABELED.findall(text or "")
    if labeled:
        return list(dict.fromkeys(labeled))
    return list(dict.fromkeys(RE_INN.findall(text or "")))


def find_ogrn(text: str) -> List[str]:
    labeled = RE_OGRN_LABELED.findall(text or "")
    if labeled:
        return list(dict.fromkeys(labeled))
    found = list(dict.fromkeys(RE_OGRNIP.findall(text or "") + RE_OGRN.findall(text or "")))
    return found


def find_emails(text: str) -> List[str]:
    return list(dict.fromkeys(RE_EMAIL.findall(text or "")))


def find_phones(text: str) -> List[str]:
    return list(dict.fromkeys(RE_PHONE.findall(text or "")))


def find_placeholders(text: str) -> List[str]:
    if not text:
        return []
    return list(dict.fromkeys(m.group(0).strip() for m in RE_PLACEHOLDER.finditer(text)))


def find_dates(text: str) -> List[str]:
    return list(dict.fromkeys(RE_DATE.findall(text or "")))


# ---------------------------------------------------------------------------
# HTTP GET (для документов, robots, sitemap) — без Playwright
# ---------------------------------------------------------------------------
class HttpResponse:
    """Лёгкий контейнер ответа, чтобы не тянуть pydantic в утилиты."""

    def __init__(self) -> None:
        self.ok: bool = False
        self.status_code: int = 0
        self.url: str = ""
        self.final_url: str = ""
        self.content: bytes = b""
        self.text: str = ""
        self.headers: Dict[str, str] = {}
        self.content_type: str = ""
        self.error: str = ""
        self.truncated: bool = False


def http_get(
    url: str,
    user_agent: str,
    timeout_s: int = 20,
    max_bytes: int = 8 * 1024 * 1024,
    allow_redirects: bool = True,
) -> HttpResponse:
    """
    Безопасный GET с ограничением размера тела. Использует httpx, если доступен,
    иначе requests, иначе urllib. Никогда не бросает исключение — ошибки в .error.
    """
    resp = HttpResponse()
    resp.url = url
    headers = {"User-Agent": user_agent, "Accept-Language": "ru,en;q=0.8"}

    # 1) httpx
    try:
        import httpx

        with httpx.Client(
            follow_redirects=allow_redirects,
            timeout=timeout_s,
            headers=headers,
            verify=False,
        ) as client:
            with client.stream("GET", url) as r:
                resp.status_code = r.status_code
                resp.final_url = str(r.url)
                resp.headers = {k.lower(): v for k, v in r.headers.items()}
                resp.content_type = resp.headers.get("content-type", "")
                chunks = []
                total = 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    total += len(chunk)
                    if total >= max_bytes:
                        resp.truncated = True
                        break
                resp.content = b"".join(chunks)
        resp.ok = 200 <= resp.status_code < 400
        resp.text = _decode(resp.content, resp.content_type)
        return resp
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover
        resp.error = f"httpx: {exc}"

    # 2) requests
    try:
        import requests

        r = requests.get(
            url,
            headers=headers,
            timeout=timeout_s,
            allow_redirects=allow_redirects,
            stream=True,
            verify=False,
        )
        resp.status_code = r.status_code
        resp.final_url = r.url
        resp.headers = {k.lower(): v for k, v in r.headers.items()}
        resp.content_type = resp.headers.get("content-type", "")
        content = b""
        for chunk in r.iter_content(8192):
            content += chunk
            if len(content) >= max_bytes:
                resp.truncated = True
                break
        resp.content = content
        resp.ok = 200 <= resp.status_code < 400
        resp.text = _decode(resp.content, resp.content_type)
        r.close()
        return resp
    except ImportError:
        pass
    except Exception as exc:
        resp.error = f"requests: {exc}"

    # 3) urllib (стандартная библиотека)
    try:
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout_s, context=ctx) as r:
            resp.status_code = getattr(r, "status", 200) or 200
            resp.final_url = r.geturl()
            resp.headers = {k.lower(): v for k, v in dict(r.headers).items()}
            resp.content_type = resp.headers.get("content-type", "")
            resp.content = r.read(max_bytes + 1)
            if len(resp.content) > max_bytes:
                resp.truncated = True
                resp.content = resp.content[:max_bytes]
        resp.ok = 200 <= resp.status_code < 400
        resp.text = _decode(resp.content, resp.content_type)
        return resp
    except Exception as exc:
        resp.error = (resp.error + " | " if resp.error else "") + f"urllib: {exc}"
        return resp


def _decode(content: bytes, content_type: str) -> str:
    if not content:
        return ""
    # Не декодируем бинарные форматы в текст.
    ct = (content_type or "").lower()
    if any(x in ct for x in ("pdf", "octet-stream", "msword", "officedocument", "zip")):
        return ""
    charset = "utf-8"
    m = re.search(r"charset=([\w\-]+)", ct)
    if m:
        charset = m.group(1)
    for enc in (charset, "utf-8", "windows-1251", "cp1251", "latin-1"):
        try:
            return content.decode(enc, errors="strict")
        except Exception:
            continue
    return content.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# IP helpers
# ---------------------------------------------------------------------------
def is_private_ip(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip).is_private
    except ValueError:
        return False


def dedupe_keep_order(items: Iterable[Any]) -> List[Any]:
    seen = set()
    out = []
    for it in items:
        key = it if isinstance(it, (str, int, float, bool)) else str(it)
        if key not in seen:
            seen.add(key)
            out.append(it)
    return out
