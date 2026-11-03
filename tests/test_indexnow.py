"""IndexNow: ключ сайта, запрос поисковикам, когда отправлять, и настройки."""
from __future__ import annotations

import asyncio
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app import indexnow
from app.config import Config
from app.indexnow import IndexNow
from app.storage import Storage
from app.web import create_app

ROOT = Path(__file__).resolve().parent.parent
SITE = (ROOT / "site" / "index.html").read_text(encoding="utf-8")
KEY = "0123456789abcdef0123456789abcdef"


class FakeIndexNow:
    """Заглушка api.indexnow.org: запоминает запросы и отвечает заданным кодом."""

    def __init__(self, status: int = 202) -> None:
        self.status, self.calls = status, []
        self.server: TestServer | None = None

    async def start(self) -> str:
        async def handle(request: web.Request) -> web.Response:
            self.calls.append(await request.json())
            return web.Response(status=self.status)
        app = web.Application()
        app.router.add_post("/indexnow", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("/indexnow"))

    async def close(self) -> None:
        if self.server:
            await self.server.close()


class KeyTest(unittest.TestCase):
    def test_find_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            verify = Path(tmp)
            self.assertEqual(indexnow.find_key(verify), "")
            (verify / "ffffffffffffffffffffffffffffffff.txt").write_text("другое", encoding="utf-8")   # содержимое не совпало
            (verify / "notes.txt").write_text("notes", encoding="utf-8")
            self.assertEqual(indexnow.find_key(verify), "")
            (verify / f"{KEY}.txt").write_text(KEY + "\n", encoding="utf-8")
            self.assertEqual(indexnow.find_key(verify), KEY)

    def test_repository_key(self) -> None:
        key = indexnow.find_key(ROOT / "site" / "verify")
        self.assertRegex(key, r"^[0-9a-f]{32}$")

    def test_payload(self) -> None:
        self.assertEqual(indexnow.payload("https://codexlab.uz", KEY), {
            "host": "codexlab.uz", "key": KEY, "keyLocation": f"https://codexlab.uz/{KEY}.txt",
            "urlList": ["https://codexlab.uz/", "https://codexlab.uz/?lang=uz", "https://codexlab.uz/?lang=en"]})


class IndexNowCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "site" / "verify").mkdir(parents=True)
        (self.root / "site" / "index.html").write_text(SITE, encoding="utf-8")
        (self.root / "site" / "verify" / f"{KEY}.txt").write_text(KEY, encoding="utf-8")
        self.fake = FakeIndexNow()
        self.endpoint = await self.fake.start()
        self.addAsyncCleanup(self.fake.close)

    def config(self, **over) -> Config:
        base = dict(bot_token="", admin_chat_ids=(), admin_password="очень-длинный-пароль-123", host="127.0.0.1", port=0,
                    site_dir=self.root / "site", data_dir=self.root / "data", trust_proxy=False, indexnow=True)
        base.update(over)
        return Config(**base)

    def age(self, seconds: float) -> None:
        path = self.root / "site" / "index.html"
        old = time.time() - seconds
        os.utime(path, (old, old))

    async def run_for(self, pinger: IndexNow, seconds: float, during=None) -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(pinger.run(stop))
        await asyncio.sleep(seconds)
        if during:
            during()
            await asyncio.sleep(seconds)
        stop.set()
        await asyncio.wait_for(task, 2)

    async def test_new_version_is_sent_once(self) -> None:
        pinger = IndexNow(self.config(), endpoint=self.endpoint, start_delay=0.05)
        await self.run_for(pinger, 0.3)
        self.assertEqual(pinger.sent, 1)
        self.assertEqual(self.fake.calls, [indexnow.payload("https://codexlab.uz", KEY)])

    async def test_wake_after_sleep_is_not_sent(self) -> None:
        self.age(2 * 3600)                      # бесплатный Render проснулся: версия старая
        pinger = IndexNow(self.config(), endpoint=self.endpoint, start_delay=0.05)
        await self.run_for(pinger, 0.3)
        self.assertEqual(self.fake.calls, [])

    async def test_publish_is_sent(self) -> None:
        self.age(2 * 3600)
        pinger = IndexNow(self.config(), endpoint=self.endpoint, start_delay=0.05)
        await self.run_for(pinger, 0.2, during=pinger.wake)
        self.assertEqual(len(self.fake.calls), 1)

    async def test_disabled_or_without_key(self) -> None:
        await self.run_for(IndexNow(self.config(indexnow=False), endpoint=self.endpoint, start_delay=0.01), 0.2)
        (self.root / "site" / "verify" / f"{KEY}.txt").unlink()
        self.assertFalse(await IndexNow(self.config(), endpoint=self.endpoint).submit())
        self.assertEqual(self.fake.calls, [])

    async def test_rejected_or_unreachable(self) -> None:
        self.fake.status = 403
        pinger = IndexNow(self.config(), endpoint=self.endpoint)
        self.assertFalse(await pinger.submit())
        self.assertEqual(pinger.sent, 0)
        broken = IndexNow(self.config(), endpoint="http://127.0.0.1:9/indexnow")
        self.assertFalse(await broken.submit())

    async def test_server_serves_key_and_publish_wakes(self) -> None:
        woken = []
        storage = Storage(self.root / "data" / "orders.sqlite3")
        await storage.init()
        client = TestClient(TestServer(create_app(self.config(), storage, lambda: None, on_site_changed=lambda: woken.append(1))))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        key = await client.get(f"/{KEY}.txt")
        self.assertEqual(key.status, 200)
        self.assertEqual(key.headers["Content-Type"], "text/plain; charset=utf-8")
        self.assertEqual(await key.text(), KEY)
        self.assertEqual((await client.get("/ffffffffffffffffffffffffffffffff.txt")).status, 404)
        self.assertEqual((await client.get("/BingSiteAuth.xml")).status, 404)
        (self.root / "site" / "verify" / "BingSiteAuth.xml").write_text("<?xml version=\"1.0\"?><users><user>X</user></users>", encoding="utf-8")
        bing = await client.get("/BingSiteAuth.xml")
        self.assertEqual(bing.headers["Content-Type"], "application/xml; charset=utf-8")
        page = '<!doctype html><html><body><script type="application/json" id="site-data">{}</script><script id="site-js"></script></body></html>'
        published = await client.post("/api/publish", data=page.encode(), headers={"Authorization": "Bearer очень-длинный-пароль-123"})
        self.assertEqual(published.status, 200)
        self.assertEqual(woken, [1])


class ConfigTest(unittest.TestCase):
    def env(self, **values: str) -> Config:
        with mock.patch.dict(os.environ, values, clear=True), mock.patch("app.config.load_env_file"):
            return Config.from_env()

    def test_indexnow_switch(self) -> None:
        self.assertFalse(self.env().indexnow)                              # своя машина без адреса — не отправляем
        self.assertTrue(self.env(RENDER="true").indexnow)
        self.assertTrue(self.env(PUBLIC_URL="https://codexlab.uz").indexnow)
        self.assertFalse(self.env(RENDER="true", INDEXNOW="0").indexnow)
        self.assertTrue(self.env(INDEXNOW="1").indexnow)

    def test_bing_code(self) -> None:
        cfg = self.env(BING_SITE_VERIFICATION='<meta name="msvalidate.01" content="0123456789ABCDEF0123456789ABCDEF" />')
        self.assertEqual(cfg.search_verify, (("msvalidate.01", "0123456789ABCDEF0123456789ABCDEF"),))


if __name__ == "__main__":
    unittest.main()
