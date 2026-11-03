"""Поисковики: robots.txt, sitemap.xml и свой адрес у каждой языковой версии сайта.

Сайт — одна страница на трёх языках. Русская версия живёт по адресу «/», узбекская и английская —
по «/?lang=uz» и «/?lang=en»: у каждой свои язык страницы, заголовок, описание и канонический адрес,
а ссылки hreflang связывают их между собой. Текст страницы на нужном языке рисует её же код,
заголовок и описание сервер подставляет сразу — их видят и поисковики, и превью в мессенджерах.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.sax.saxutils import quoteattr

LANGS = ("ru", "uz", "en")                    # первый — основной, по адресу «/»
SITE_URL = re.compile(r"https?://[A-Za-z0-9.-]+(:\d{1,5})?(/[A-Za-z0-9._~/-]*)?")
VERIFY_CODE = re.compile(r"[A-Za-z0-9_-]{8,128}")
# Закрыто от поиска: служебное API, «Совет агентов» по паролю и страницы проектов клиентов по секретной ссылке.
PRIVATE_PATHS = ("/api/", "/council", "/studio/")

_DATA = re.compile(r'<script type="application/json" id="site-data">(.*?)</script>', re.S)


@dataclass(frozen=True)
class PageMeta:
    """То, что сервер берёт из настроек самой страницы: адрес сайта, заголовок и описание на каждом языке."""

    site: str = ""
    titles: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)


def read_meta(page: str) -> PageMeta:
    """Настройки страницы (блок site-data). Непонятные или пустые значения пропускаем: тогда остаётся русский текст."""
    found = _DATA.search(page)
    try:
        data = json.loads(found.group(1)) if found else {}
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}

    def text(*keys: str) -> str:
        value: object = data
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        return " ".join(value.split()) if isinstance(value, str) else ""

    site = text("brand", "site").rstrip("/")
    titles = {"ru": text("texts", "seoTitle"), "uz": text("uz", "seoTitle"), "en": text("en", "seoTitle")}
    descriptions = {"ru": text("texts", "seoDescription"), "uz": text("uz", "seoDescription"),
                    "en": text("en", "seoDescription")}
    return PageMeta(site=site if SITE_URL.fullmatch(site) else "",
                    titles={k: v for k, v in titles.items() if v},
                    descriptions={k: v for k, v in descriptions.items() if v})


def page_url(base: str, lang: str) -> str:
    """Адрес языковой версии: русская — «/», остальные — «/?lang=uz»."""
    return f"{base}/" if lang == LANGS[0] else f"{base}/?lang={lang}"


def verification_code(raw: str) -> str:
    """Код подтверждения из Search Console или Яндекс Вебмастера. Можно вставить и весь тег <meta …> — возьмём content."""
    raw = raw.strip()
    found = re.search(r'content\s*=\s*["\']([^"\']*)["\']', raw)
    code = (found.group(1) if found else raw).strip()
    return code if VERIFY_CODE.fullmatch(code) else ""


def localize_page(page: str, lang: str, meta: PageMeta, verify: dict[str, str] | None = None) -> str:
    """Шапка страницы для языковой версии и теги подтверждения прав на сайт. Меняется только <head>."""
    head, end, body = page.partition("</head>")
    if not end:
        return page
    if lang != LANGS[0] and lang in LANGS:
        attr = lambda value: html.escape(value, quote=True)
        head = re.sub(r'<html lang="[A-Za-z-]*"', f'<html lang="{lang}"', head, count=1)
        title = meta.titles.get(lang)
        if title:
            head = re.sub(r"<title>.*?</title>", lambda _: f"<title>{html.escape(title, quote=False)}</title>", head, count=1, flags=re.S)
            head = re.sub(r'<meta property="og:title" content="[^"]*">', lambda _: f'<meta property="og:title" content="{attr(title)}">', head, count=1)
        description = meta.descriptions.get(lang)
        if description:
            for name in ('name="description"', 'property="og:description"'):
                head = re.sub(rf'<meta {name} content="[^"]*">', lambda _, n=name: f'<meta {n} content="{attr(description)}">', head, count=1)
        if meta.site:
            url = attr(page_url(meta.site, lang))
            head = re.sub(r'<link rel="canonical" href="[^"]*">', lambda _: f'<link rel="canonical" href="{url}">', head, count=1)
            head = re.sub(r'<meta property="og:url" content="[^"]*">', lambda _: f'<meta property="og:url" content="{url}">', head, count=1)
    for name, code in (verify or {}).items():
        head += f'<meta name="{html.escape(name, quote=True)}" content="{html.escape(code, quote=True)}">\n'
    return head + end + body


def robots_txt(base: str) -> str:
    lines = ["User-agent: *", "Allow: /", *(f"Disallow: {path}" for path in PRIVATE_PATHS), "", f"Sitemap: {base}/sitemap.xml", ""]
    return "\n".join(lines)


def sitemap_xml(base: str, modified: float | None = None) -> str:
    """Три языковые версии страницы; у каждой — ссылки на две другие (hreflang) и на основную для остальных языков."""
    lastmod = datetime.fromtimestamp(modified, timezone.utc).strftime("%Y-%m-%d") if modified else ""
    links = "".join(f"    <xhtml:link rel=\"alternate\" hreflang=\"{lang}\" href={quoteattr(page_url(base, lang))}/>\n" for lang in LANGS)
    links += f"    <xhtml:link rel=\"alternate\" hreflang=\"x-default\" href={quoteattr(page_url(base, LANGS[0]))}/>\n"
    urls = "".join(
        "  <url>\n"
        f"    <loc>{html.escape(page_url(base, lang), quote=False)}</loc>\n"
        + (f"    <lastmod>{lastmod}</lastmod>\n" if lastmod else "")
        + links
        + "  </url>\n"
        for lang in LANGS
    )
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
            + urls + "</urlset>\n")
