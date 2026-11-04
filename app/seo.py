"""Поисковики: robots.txt, sitemap.xml, страницы сайта и свой адрес у каждой языковой версии.

Сайт — один файл, в нём пять страниц: главная «/», «/prices», «/about», «/brief», «/templates».
Каждый блок разметки помечен списком страниц, на которых он виден (комментарии <!--pg:…-->);
по адресу страницы сервер отдаёт только её блоки — у каждого адреса своё содержание для поиска.
Дальше страницу рисует её же код, а переходы между страницами идут без перезагрузки.

У каждой страницы три языковые версии: русская — без параметра, узбекская и английская — с
«?lang=uz» и «?lang=en». Сервер подставляет в <head> язык, заголовок, описание, канонический адрес
и ссылки hreflang — их видят и поисковики, и превью в мессенджерах. Заголовки и описания всех
страниц на всех языках страница сама кладёт в блок site-pages при сборке.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.sax.saxutils import quoteattr

LANGS = ("ru", "uz", "en")                    # первый — основной, без параметра в адресе
PAGES = {"home": "/", "prices": "/prices", "about": "/about", "brief": "/brief", "templates": "/templates"}
SITE_URL = re.compile(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?")
VERIFY_CODE = re.compile(r"[A-Za-z0-9_-]{8,128}")
# Закрыто от поиска: служебное API, «Совет агентов» по паролю и страницы проектов клиентов по секретной ссылке.
PRIVATE_PATHS = ("/api/", "/council", "/studio/")

_DATA = re.compile(r'<script type="application/json" id="site-data">(.*?)</script>', re.S)
_PAGES_SEO = re.compile(r'<script type="application/json" id="site-pages">(.*?)</script>', re.S)
_BLOCK = re.compile(r"<!--pg:([a-z ]+)-->(.*?)<!--/pg-->", re.S)
_DATA_START = '<script type="application/json" id="site-data">'


@dataclass(frozen=True)
class PageMeta:
    """То, что сервер берёт из самой страницы: адрес сайта, заголовки и описания страниц на каждом языке."""

    site: str = ""
    titles: dict[str, str] = field(default_factory=dict)            # главная: язык → заголовок
    descriptions: dict[str, str] = field(default_factory=dict)      # главная: язык → описание
    pages: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)   # страница → язык → {t, d}


def _json_block(pattern: re.Pattern[str], page: str) -> dict:
    found = pattern.search(page)
    try:
        data = json.loads(found.group(1)) if found else {}
    except json.JSONDecodeError:
        data = {}
    return data if isinstance(data, dict) else {}


def _clean(value: object) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def read_meta(page: str) -> PageMeta:
    """Настройки страницы (блоки site-data и site-pages). Непонятные или пустые значения пропускаем:
    тогда остаётся то, что уже стоит в <head>."""
    data = _json_block(_DATA, page)

    def text(*keys: str) -> str:
        value: object = data
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        return _clean(value)

    site = text("brand", "site").rstrip("/")
    titles = {"ru": text("texts", "seoTitle"), "uz": text("uz", "seoTitle"), "en": text("en", "seoTitle")}
    descriptions = {"ru": text("texts", "seoDescription"), "uz": text("uz", "seoDescription"),
                    "en": text("en", "seoDescription")}
    pages: dict[str, dict[str, dict[str, str]]] = {}
    for name, langs in _json_block(_PAGES_SEO, page).items():
        if name not in PAGES or not isinstance(langs, dict):
            continue
        for lang, item in langs.items():
            if lang in LANGS and isinstance(item, dict):
                entry = {k: _clean(item.get(k)) for k in ("t", "d") if _clean(item.get(k))}
                if entry:
                    pages.setdefault(name, {})[lang] = entry
    return PageMeta(site=site if SITE_URL.fullmatch(site) else "",
                    titles={k: v for k, v in titles.items() if v},
                    descriptions={k: v for k, v in descriptions.items() if v},
                    pages=pages)


def page_of_path(path: str) -> str:
    """Страница по адресу запроса: «/prices» → prices; «/» и «/index.html» → home; чужой адрес — пусто."""
    path = path.rstrip("/") or "/"
    if path == "/index.html":
        return "home"
    return next((name for name, where in PAGES.items() if where == path), "")


def page_url(base: str, lang: str, page: str = "home") -> str:
    """Адрес страницы на языке: русская — без параметра, остальные — «?lang=uz»."""
    url = base + PAGES.get(page, "/")
    return url if lang == LANGS[0] else f"{url}?lang={lang}"


def verification_code(raw: str) -> str:
    """Код подтверждения из Search Console или Яндекс Вебмастера. Можно вставить и весь тег <meta …> — возьмём content."""
    raw = raw.strip()
    found = re.search(r'content\s*=\s*["\']([^"\']*)["\']', raw)
    code = (found.group(1) if found else raw).strip()
    return code if VERIFY_CODE.fullmatch(code) else ""


def _keep_page_blocks(markup: str, page: str) -> str:
    """Оставляет блоки, помеченные этой страницей; пометки убирает."""
    return _BLOCK.sub(lambda m: m.group(2) if page in m.group(1).split() else "", markup)


def localize_page(document: str, lang: str, meta: PageMeta, verify: dict[str, str] | None = None, page: str = "home") -> str:
    """Документ для адреса страницы на языке: в <head> — язык, заголовок, описание, канонический адрес и hreflang
    этой страницы и коды подтверждения; в разметке — только блоки этой страницы. Настройки и код страницы не трогаем."""
    head, end, body = document.partition("</head>")
    if not end:
        return document
    lang = lang if lang in LANGS else LANGS[0]
    page = page if page in PAGES else "home"
    attr = lambda value: html.escape(value, quote=True)

    head = re.sub(r'<html lang="[A-Za-z-]*"[^>]*>', f'<html lang="{lang}" data-page="{page}">', head, count=1)
    seo = meta.pages.get(page, {}).get(lang, {})
    title = seo.get("t") or (meta.titles.get(lang) if page == "home" else "")
    description = seo.get("d") or (meta.descriptions.get(lang) if page == "home" else "")
    if title:
        head = re.sub(r"<title>.*?</title>", lambda _: f"<title>{html.escape(title, quote=False)}</title>", head, count=1, flags=re.S)
        head = re.sub(r'<meta property="og:title" content="[^"]*">', lambda _: f'<meta property="og:title" content="{attr(title)}">', head, count=1)
    if description:
        for name in ('name="description"', 'property="og:description"'):
            head = re.sub(rf'<meta {name} content="[^"]*">', lambda _, n=name: f'<meta {n} content="{attr(description)}">', head, count=1)
    if meta.site:
        url = attr(page_url(meta.site, lang, page))
        head = re.sub(r'<link rel="canonical" href="[^"]*">', lambda _: f'<link rel="canonical" href="{url}">', head, count=1)
        head = re.sub(r'<meta property="og:url" content="[^"]*">', lambda _: f'<meta property="og:url" content="{url}">', head, count=1)
        links = "".join(f'<link rel="alternate" hreflang="{code}" href="{attr(page_url(meta.site, code, page))}">\n' for code in LANGS)
        links += f'<link rel="alternate" hreflang="x-default" href="{attr(page_url(meta.site, LANGS[0], page))}">\n'
        head = re.sub(r'(?:<link rel="alternate" hreflang="[^"]*" href="[^"]*">\n)+', lambda _: links, head, count=1)
    for name, code in (verify or {}).items():
        head += f'<meta name="{html.escape(name, quote=True)}" content="{html.escape(code, quote=True)}">\n'

    markup, data_start, rest = body.partition(_DATA_START)  # блоки страниц — только в разметке, до настроек и кода
    return head + end + _keep_page_blocks(markup, page) + data_start + rest


def robots_txt(base: str) -> str:
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: {path}" for path in PRIVATE_PATHS), "", f"Sitemap: {base}/sitemap.xml", ""]
    return "\n".join(lines)


def all_urls(base: str) -> list[str]:
    """Все адреса сайта: пять страниц на трёх языках."""
    return [page_url(base, lang, page) for page in PAGES for lang in LANGS]


def sitemap_xml(base: str, modified: float | None = None) -> str:
    """Пять страниц на трёх языках; у каждого адреса — ссылки на две другие версии этой страницы (hreflang)
    и на основную для остальных языков."""
    lastmod = datetime.fromtimestamp(modified, timezone.utc).strftime("%Y-%m-%d") if modified else ""
    urls = []
    for page in PAGES:
        links = "".join(f"    <xhtml:link rel=\"alternate\" hreflang=\"{lang}\" href={quoteattr(page_url(base, lang, page))}/>\n" for lang in LANGS)
        links += f"    <xhtml:link rel=\"alternate\" hreflang=\"x-default\" href={quoteattr(page_url(base, LANGS[0], page))}/>\n"
        for lang in LANGS:
            urls.append("  <url>\n"
                        f"    <loc>{html.escape(page_url(base, lang, page), quote=False)}</loc>\n"
                        + (f"    <lastmod>{lastmod}</lastmod>\n" if lastmod else "")
                        + links
                        + "  </url>\n")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
            + "".join(urls) + "</urlset>\n")
