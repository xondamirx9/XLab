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


class StructuredDataTest(unittest.TestCase):
    def test_site_and_studio_graph(self) -> None:
        import json
        head = SITE.partition("</head>")[0]
        found = re.search(r'<script type="application/ld\+json">(.*?)</script>', head, re.S)
        data = json.loads(found.group(1))
        web, org = data["@graph"]
        self.assertEqual(web["@type"], "WebSite")
        self.assertEqual((web["url"], web["name"]), ("https://codexlab.uz/", "Codex Lab"))
        self.assertEqual(web["alternateName"], ["codexlab", "codexlab.uz"])     # «codexlab» одним словом — как на логотипе
        self.assertEqual(web["publisher"], {"@id": org["@id"]})
        self.assertEqual(org["@type"], "ProfessionalService")
        self.assertEqual(org["@id"], "https://codexlab.uz/#org")
        self.assertEqual(org["alternateName"], ["codexlab", "codexlab.uz"])
        self.assertEqual(org["logo"], "https://codexlab.uz/brand/logo-for-light.png")
        self.assertNotIn("sameAs", org)                                          # Telegram в контактах пока не указан
        self.assertEqual(org["address"], {"@type": "PostalAddress", "streetAddress": "ул. Кораташ, 106", "addressLocality": "Ташкент",
                                          "postalCode": "100021", "addressCountry": "UZ"})
        self.assertEqual(org["geo"], {"@type": "GeoCoordinates", "latitude": 41.3141551, "longitude": 69.2376133})
        self.assertEqual(org["hasMap"], "https://maps.app.goo.gl/rtPo7oZBKVTv1oSXA")

    def test_about_page_has_address_and_map(self) -> None:
        page = seo.localize_page(SITE, "ru", seo.read_meta(SITE), page="about")
        markup = page.partition("</head>")[2].partition('<script type="application/json" id="site-data">')[0]
        self.assertIn("ул. Кораташ, 106", markup)
        # живую карту ставит код страницы, когда сайт на своём сервере; в разметке — рисунок карты и адрес встраивания
        self.assertIn('data-embed="https://www.google.com/maps?q=41.3141551,69.2376133&amp;z=17&amp;hl=ru&amp;output=embed"', markup)
        self.assertNotIn("<iframe", markup)
        self.assertIn('href="https://www.google.com/maps/dir/?api=1&amp;destination=41.3141551,69.2376133"', markup)
        self.assertIn('href="https://maps.app.goo.gl/rtPo7oZBKVTv1oSXA"', markup)
        home = seo.localize_page(SITE, "ru", seo.read_meta(SITE), page="home")
        self.assertNotIn("<iframe", home.partition("</head>")[2].partition('<script type="application/json" id="site-data">')[0])   # карта — только на «О нас»


class LocalizeTest(unittest.TestCase):
    def test_uzbek_version_head(self) -> None:
        meta = seo.read_meta(SITE)
        page = seo.localize_page(SITE, "uz", meta)
        head, _, body = page.partition("</head>")
        self.assertTrue(page.startswith('<!doctype html>\n<html lang="uz" data-page="home">'))
        self.assertIn(f"<title>{meta.titles['uz']}</title>", head)
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/?lang=uz">', head)
        self.assertIn('<meta property="og:url" content="https://codexlab.uz/?lang=uz">', head)
        self.assertEqual(len(re.findall(r'<link rel="canonical"', head)), 1)
        # ссылки hreflang у главной — те же, что в шапке файла
        self.assertEqual(re.findall(r'hreflang="[^"]+" href="[^"]+"', head),
                         re.findall(r'hreflang="[^"]+" href="[^"]+"', SITE.partition("</head>")[0]))

    def test_settings_and_code_untouched(self) -> None:
        start = '<script type="application/json" id="site-data">'
        for name in seo.PAGES:
            page = seo.localize_page(SITE, "en", seo.read_meta(SITE), page=name)
            self.assertEqual(page.partition(start)[2], SITE.partition(start)[2], name)

    def test_each_page_keeps_only_its_blocks(self) -> None:
        meta = seo.read_meta(SITE)
        expect = {"home": ["top", "value", "examples", "projects", "process", "priceTeaser", "final"],
                  "examples": ["examplesHead", "examples", "templates", "final"],
                  "about": ["about", "projects", "process", "location", "faq", "final"],
                  "partners": ["partnersHead", "partnerList", "final"],
                  "prices": ["pricesHead", "prices", "faq", "final"],
                  "brief": ["briefHead", "brief", "faq"]}
        for name, ids in expect.items():
            page = seo.localize_page(SITE, "ru", meta, page=name)
            markup = page.partition("</head>")[2].partition('<script type="application/json" id="site-data">')[0]
            found = re.findall(r'<section data-pages="[^"]*" class="[^"]*" id="([^"]+)"', markup)
            self.assertEqual(found, ids, name)
            self.assertNotIn("<!--pg:", markup)                     # пометки для сервера посетителю не нужны
            self.assertIn(f'<html lang="ru" data-page="{name}">', page)

    def test_page_head_per_page_and_language(self) -> None:
        meta = seo.read_meta(SITE)
        page = seo.localize_page(SITE, "uz", meta, page="prices")
        head = page.partition("</head>")[0]
        self.assertIn(f"<title>{meta.pages['prices']['uz']['t']}</title>", head)
        self.assertIn("Codex Lab", meta.pages["prices"]["uz"]["t"])
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/prices?lang=uz">', head)
        self.assertIn('<meta property="og:url" content="https://codexlab.uz/prices?lang=uz">', head)
        self.assertEqual(re.findall(r'hreflang="([^"]+)" href="([^"]+)"', head), [
            ("ru", "https://codexlab.uz/prices"), ("uz", "https://codexlab.uz/prices?lang=uz"),
            ("en", "https://codexlab.uz/prices?lang=en"), ("x-default", "https://codexlab.uz/prices")])
        self.assertEqual(set(meta.pages), set(seo.PAGES))
        for name in seo.PAGES:
            self.assertEqual(set(meta.pages[name]), set(seo.LANGS), name)

    def test_page_of_path(self) -> None:
        self.assertEqual([seo.page_of_path(x) for x in ("/", "/index.html", "/prices", "/prices/", "/about", "/brief", "/examples", "/partners", "/templates", "/nope")],
                         ["home", "home", "prices", "prices", "about", "brief", "examples", "partners", "", ""])

    def test_partners_page_is_in_search(self) -> None:
        meta = seo.read_meta(SITE)
        self.assertEqual(meta.hidden, frozenset())                  # на «Партнёрах» — NOIZ Media и SavdEx
        page = seo.localize_page(SITE, "ru", meta, page="partners")
        self.assertIn('<meta name="robots" content="index, follow, max-image-preview:large">', page.partition("</head>")[0])
        for name in ("NOIZ Media", "SavdEx", 'href="https://noizmedia.uz/"', 'href="https://savdex.uz/"'):
            self.assertIn(name, page.partition("</head>")[2])
        xml = seo.sitemap_xml("https://codexlab.uz", None, meta.hidden)
        self.assertIn("<loc>https://codexlab.uz/partners?lang=en</loc>", xml)
        self.assertEqual(xml.count("<url>"), 18)                     # шесть страниц на трёх языках

    def test_empty_page_is_hidden_from_search(self) -> None:
        site = SITE.replace('"partners":{"ru":', '"partners":{"soon":1,"ru":', 1)   # пометка soon: страница пока пустая
        meta = seo.read_meta(site)
        self.assertEqual(meta.hidden, frozenset({"partners"}))
        head = seo.localize_page(site, "ru", meta, page="partners").partition("</head>")[0]
        self.assertIn('<meta name="robots" content="noindex, follow">', head)
        other = seo.localize_page(site, "ru", meta, page="about").partition("</head>")[0]
        self.assertIn('<meta name="robots" content="index, follow, max-image-preview:large">', other)
        xml = seo.sitemap_xml("https://codexlab.uz", None, meta.hidden)
        self.assertNotIn("/partners", xml)
        self.assertEqual(xml.count("<url>"), 15)

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
        self.assertEqual(xml.count("<url>"), 18)                     # шесть страниц на трёх языках
        for loc in ("https://codexlab.uz/", "https://codexlab.uz/?lang=uz", "https://codexlab.uz/prices",
                    "https://codexlab.uz/about?lang=en", "https://codexlab.uz/examples?lang=uz", "https://codexlab.uz/partners", "https://codexlab.uz/brief"):
            self.assertIn(f"<loc>{loc}</loc>", xml)
        self.assertEqual(xml.count("<lastmod>2026-11-03</lastmod>"), 18)
        self.assertEqual(xml.count('hreflang="x-default"'), 18)
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
        home = await root.text()
        self.assertIn('<html lang="ru" data-page="home">', home)
        self.assertNotIn('id="briefHead"', home.partition('<script type="application/json" id="site-data">')[0])
        uz = await client.get("/?lang=uz")
        self.assertEqual(uz.status, 200)
        self.assertEqual(uz.headers["Content-Type"], "text/html; charset=utf-8")
        self.assertIn("frame-ancestors 'none'", uz.headers["Content-Security-Policy"])
        text = await uz.text()
        self.assertTrue(text.startswith('<!doctype html>\n<html lang="uz" data-page="home">'))
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/?lang=uz">', text)
        en = await (await client.get("/?lang=en")).text()
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/?lang=en">', en)
        for odd in ("/?lang=ru", "/?lang=de", "/?lang=%3Cscript%3E"):
            self.assertEqual(await (await client.get(odd)).text(), home, odd)

    async def test_page_addresses(self) -> None:
        client = await self.client()
        for name, path in seo.PAGES.items():
            response = await client.get(path)
            self.assertEqual(response.status, 200, path)
            text = await response.text()
            self.assertIn(f'data-page="{name}"', text.partition("<head>")[0], path)
            canonical = "https://codexlab.uz" + path
            self.assertIn(f'<link rel="canonical" href="{canonical}">', text, path)
        about_uz = await (await client.get("/about?lang=uz")).text()
        self.assertIn('<link rel="canonical" href="https://codexlab.uz/about?lang=uz">', about_uz)
        moved = await client.get("/prices/?lang=en", allow_redirects=False)
        self.assertEqual((moved.status, moved.headers["Location"]), (301, "/prices?lang=en"))
        self.assertEqual((await client.get("/pricing")).status, 404)

    async def test_etag(self) -> None:
        client = await self.client()
        first = await client.get("/about")
        etag = first.headers["ETag"]
        again = await client.get("/about", headers={"If-None-Match": etag})
        self.assertEqual(again.status, 304)
        other = await client.get("/prices", headers={"If-None-Match": etag})
        self.assertEqual(other.status, 200)

    async def test_page_change_is_picked_up(self) -> None:
        client = await self.client()
        await client.get("/?lang=en")
        changed = SITE.replace('Codex Lab — website and Telegram bot development, Tashkent', 'New English title')
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

    async def test_verification_file(self) -> None:
        verify = self.root / "site" / "verify"
        verify.mkdir()
        (verify / "googleb0b3f0f8668cc8e6.html").write_text("google-site-verification: googleb0b3f0f8668cc8e6.html", encoding="utf-8")
        (verify / "notes.html").write_text("личное", encoding="utf-8")
        client = await self.client()
        found = await client.get("/googleb0b3f0f8668cc8e6.html")
        self.assertEqual(found.status, 200)
        self.assertEqual(await found.text(), "google-site-verification: googleb0b3f0f8668cc8e6.html")
        self.assertTrue(found.headers["Content-Type"].startswith("text/html"))
        for path in ("/google0000000000000000.html", "/notes.html", "/googleb0b3f0f8668cc8e6.htm", "/verify/googleb0b3f0f8668cc8e6.html",
                     "/google..%2Fverify%2Fnotes.html", "/yandex_0123456789abcdef.html"):
            self.assertEqual((await client.get(path)).status, 404, path)

    async def test_repository_has_google_file(self) -> None:
        name = "googleb0b3f0f8668cc8e6.html"
        self.assertEqual((ROOT / "site" / "verify" / name).read_text(encoding="utf-8"), f"google-site-verification: {name}")

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
