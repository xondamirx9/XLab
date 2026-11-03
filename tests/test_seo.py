"""Поисковики: robots.txt, sitemap.xml, языковые версии страницы и коды подтверждения."""
from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiohttp.test_utils import TestClient, TestServer

from app import seo
from app.config import Config, ConfigError
from app.storage import Storage
from app.web import create_app

ROOT = Path(__file__).resolve().parent.parent
SITE = (ROOT / "site" / "index.html").read_text(encoding="utf-8")


class PageMetaTest(unittest.TestCase):
    def test_real_page_settings(self) -> None:
        meta = seo.read_meta(SITE)
        self.assertEqual(meta.site, "https://codexlab.uz")
        self.assertEqual(set(meta.titles), {"ru", "uz", "en"})
        self.assertIn("Toshkent", meta.titles["uz"])
        self.assertIn("Tashkent", meta.titles["en"])
        self.assertEqual(set(meta.descriptions), {"ru", "uz", "en"})

    def test_bad_or_missing_settings(self) -> None:
        self.assertEqual(seo.read_meta("<html></html>"), seo.PageMeta())
        broken = '<script type="application/json" id="site-data">{не json</script>'
        self.assertEqual(seo.read_meta(broken), seo.PageMeta())
        evil = '<script type="application/json" id="site-data">{"brand":{"site":"javascript:alert(1)"}}</script>'
        self.assertEqual(seo.read_meta(evil).site, "")

    def test_static_head_links_all_versions(self) -> None:
        head = SITE.partition("</head>")[0]
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/">', head)
        for lang, url in (("ru", "/"), ("uz", "/?lang=uz"), ("en", "/?lang=en"), ("x-default", "/")):
            self.assertIn(f'<link rel="alternate" hreflang="{lang}" href="https://codexlab.uz{url}">', head)
        self.assertNotIn("noindex", head)


class LocalizeTest(unittest.TestCase):
    def test_uzbek_version_head(self) -> None:
        meta = seo.read_meta(SITE)
        page = seo.localize_page(SITE, "uz", meta)
        head, _, body = page.partition("</head>")
        self.assertTrue(page.startswith('<!doctype html>\n<html lang="uz">'))
        self.assertIn(f"<title>{meta.titles['uz']}</title>", head)
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/?lang=uz">', head)
        self.assertIn('<meta property="og:url" content="https://codexlab.uz/?lang=uz">', head)
        self.assertEqual(len(re.findall(r'<link rel="canonical"', head)), 1)
        self.assertEqual(body, SITE.partition("</head>")[2])        # разметка и код страницы не тронуты
        # ссылки hreflang одинаковые у всех версий
        self.assertEqual(re.findall(r'hreflang="[^"]+" href="[^"]+"', head),
                         re.findall(r'hreflang="[^"]+" href="[^"]+"', SITE.partition("</head>")[0]))

    def test_russian_version_unchanged_without_codes(self) -> None:
        self.assertEqual(seo.localize_page(SITE, "ru", seo.read_meta(SITE)), SITE)

    def test_titles_are_escaped(self) -> None:
        page = '<html lang="ru"><head><title>a</title><meta name="description" content="b"></head><body></body></html>'
        meta = seo.PageMeta(titles={"en": 'A <b> & "C"'}, descriptions={"en": 'x" onload="y'})
        out = seo.localize_page(page, "en", meta)
        self.assertIn("<title>A &lt;b&gt; &amp; \"C\"</title>", out)
        self.assertIn('content="x&quot; onload=&quot;y"', out)

    def test_verification_tags(self) -> None:
        out = seo.localize_page(SITE, "ru", seo.read_meta(SITE), {"google-site-verification": "abc123_DEF-456"})
        head = out.partition("</head>")[0]
        self.assertTrue(head.endswith('<meta name="google-site-verification" content="abc123_DEF-456">\n'))

    def test_verification_code_parsing(self) -> None:
        self.assertEqual(seo.verification_code("abc123_DEF-456"), "abc123_DEF-456")
        self.assertEqual(seo.verification_code('<meta name="google-site-verification" content="abc123_DEF-456" />'), "abc123_DEF-456")
        self.assertEqual(seo.verification_code('"><script>'), "")
        self.assertEqual(seo.verification_code("short"), "")


class FilesTest(unittest.TestCase):
    def test_robots(self) -> None:
        text = seo.robots_txt("https://codexlab.uz")
        self.assertIn("User-agent: *\nAllow: /\n", text)
        for path in ("/api/", "/council", "/studio/"):
            self.assertIn(f"Disallow: {path}\n", text)
        self.assertIn("Sitemap: https://codexlab.uz/sitemap.xml\n", text)

    def test_sitemap(self) -> None:
        xml = seo.sitemap_xml("https://codexlab.uz", 1793664000)   # 2026-11-03 UTC
        self.assertEqual(xml.count("<url>"), 3)
        for loc in ("https://codexlab.uz/", "https://codexlab.uz/?lang=uz", "https://codexlab.uz/?lang=en"):
            self.assertIn(f"<loc>{loc}</loc>", xml)
        self.assertEqual(xml.count("<lastmod>2026-11-03</lastmod>"), 3)
        self.assertEqual(xml.count('hreflang="x-default"'), 3)
        import xml.etree.ElementTree as ET
        ET.fromstring(xml)                                           # правильный XML


class ServerSeoTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "site").mkdir()
        (self.root / "site" / "index.html").write_text(SITE, encoding="utf-8")
        self.storage = Storage(self.root / "data" / "orders.sqlite3")
        await self.storage.init()

    async def client(self, **over) -> TestClient:
        base = dict(bot_token="", admin_chat_ids=(), admin_password="", host="127.0.0.1", port=0,
                    site_dir=self.root / "site", data_dir=self.root / "data", trust_proxy=False)
        base.update(over)
        client = TestClient(TestServer(create_app(Config(**base), self.storage, lambda: None)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def test_language_versions(self) -> None:
        client = await self.client()
        root = await client.get("/")
        self.assertEqual(await root.text(), SITE)
        uz = await client.get("/?lang=uz")
        self.assertEqual(uz.status, 200)
        self.assertEqual(uz.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("frame-ancestors 'none'", uz.headers["Content-Security-Policy"])
        text = await uz.text()
        self.assertTrue(text.startswith('<!doctype html>\n<html lang="uz">'))
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/?lang=uz">', text)
        en = await (await client.get("/?lang=en")).text()
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/?lang=en">', en)
        for odd in ("/?lang=ru", "/?lang=de", "/?lang=%3Cscript%3E"):
            self.assertEqual(await (await client.get(odd)).text(), SITE, odd)

    async def test_page_change_is_picked_up(self) -> None:
        client = await self.client()
        await client.get("/?lang=en")
        changed = SITE.replace('"seoTitle":"Codex Lab — website and Telegram bot development, Tashkent"', '"seoTitle":"New English title"')
        self.assertNotEqual(changed, SITE)
        path = self.root / "site" / "index.html"
        path.write_text(changed, encoding="utf-8")
        os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 5_000_000_000))
        self.assertIn("<title>New English title</title>", await (await client.get("/?lang=en")).text())

    async def test_robots_and_sitemap(self) -> None:
        client = await self.client()
        robots = await client.get("/robots.txt")
        self.assertEqual(robots.status, 200)
        self.assertEqual(robots.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertIn("Sitemap: https://codexlab.uz/sitemap.xml", await robots.text())
        sitemap = await client.get("/sitemap.xml")
        self.assertEqual(sitemap.headers["Content-Type"], "application/xml; charset=utf-8")
        self.assertIn("<loc>https://codexlab.uz/?lang=uz</loc>", await sitemap.text())

    async def test_robots_without_site_address(self) -> None:
        (self.root / "site" / "index.html").write_text("<!doctype html><html><head></head><body></body></html>", encoding="utf-8")
        client = await self.client(public_url="https://example.uz")
        self.assertIn("Sitemap: https://example.uz/sitemap.xml", await (await client.get("/robots.txt")).text())

    async def test_verification_codes_on_every_version(self) -> None:
        client = await self.client(search_verify=(("google-site-verification", "abc123_DEF-456"), ("yandex-verification", "0123456789abcdef")))
        for path in ("/", "/?lang=uz"):
            head = (await (await client.get(path)).text()).partition("</head>")[0]
            self.assertIn('<meta name="google-site-verification" content="abc123_DEF-456">', head, path)
            self.assertIn('<meta name="yandex-verification" content="0123456789abcdef">', head, path)


class ConfigVerifyTest(unittest.TestCase):
    def env(self, **values: str) -> Config:
        with mock.patch.dict(os.environ, values, clear=True), mock.patch("app.config.load_env_file"):
            return Config.from_env()

    def test_codes_from_environment(self) -> None:
        self.assertEqual(self.env().search_verify, ())
        cfg = self.env(GOOGLE_SITE_VERIFICATION='<meta name="google-site-verification" content="abc123_DEF-456" />')
        self.assertEqual(cfg.search_verify, (("google-site-verification", "abc123_DEF-456"),))
        with self.assertRaises(ConfigError):
            self.env(YANDEX_VERIFICATION='"><script>')


if __name__ == "__main__":
    unittest.main()
