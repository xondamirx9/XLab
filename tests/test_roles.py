"""Две машины: роли web и admin, события между ними через базу, ожидание схемы, доверенный прокси."""
from __future__ import annotations

import asyncio
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from app.config import Config, ConfigError
from app.db import MIGRATIONS, Database, Listener
from app.news import NewsStore
from app.storage import Storage
from app.studio.store import StudioStore
from app.web import StudioContext, client_ip, create_app
from pgdb import database_for, test_url

PASSWORD = "очень-длинный-пароль-123"
CADDYFILE = Path(__file__).resolve().parent.parent / "deploy" / "uzcloud" / "Caddyfile"
PAGE = '<!doctype html><html><body><script type="application/json" id="site-data">{}</script><script id="site-js"></script></body></html>'
# Пути для посетителей и для админки: (метод, путь). Каждый должен быть ровно на одной из машин.
PUBLIC = [("GET", "/"), ("GET", "/prices"), ("GET", "/robots.txt"), ("GET", "/sitemap.xml"), ("GET", "/api/rate"),
          ("POST", "/api/order"), ("POST", "/api/event"), ("GET", "/sites/demo/"), ("POST", "/api/sites/demo/lead"),
          ("GET", "/studio/p/token"), ("GET", "/api/news")]
ADMIN = [("POST", "/api/publish"), ("GET", "/council"), ("GET", "/api/council/state"), ("GET", "/api/council/feed"),
         ("POST", "/api/studio/projects"), ("POST", "/api/studio/projects/1/revise"), ("POST", "/api/studio/projects/1/rate"),
         ("POST", "/api/news")]


def load(**env: str) -> Config:
    with mock.patch.dict(os.environ, env, clear=True), mock.patch("app.config.load_env_file"):
        return Config.from_env()


class ConfigTest(unittest.TestCase):
    def test_role(self) -> None:
        self.assertEqual(load().role, "all")                        # по умолчанию — всё в одном процессе
        self.assertEqual(load(ROLE=" Web ").role, "web")
        self.assertEqual(load(ROLE="admin").role, "admin")
        with self.assertRaises(ConfigError):
            load(ROLE="worker")

    def test_trusted_proxies(self) -> None:
        self.assertEqual(load().trusted_proxies, ())
        self.assertEqual(load(TRUSTED_PROXIES="10.0.0.5, 10.1.0.0/24;fd00::1").trusted_proxies, ("10.0.0.5", "10.1.0.0/24", "fd00::1"))
        with self.assertRaises(ConfigError):
            load(TRUSTED_PROXIES="10.0.0.5, caddy")


class ClientIpTest(unittest.TestCase):
    def test_trusted_proxy_on_other_machine(self) -> None:
        request = lambda remote, forwarded="": SimpleNamespace(remote=remote, headers={"X-Forwarded-For": forwarded} if forwarded else {})
        trusted = ("10.0.0.5", "10.2.0.0/24")
        self.assertEqual(client_ip(request("10.0.0.5", "1.1.1.1, 5.6.7.8"), True, trusted=trusted), "5.6.7.8")
        self.assertEqual(client_ip(request("10.2.0.77", "5.6.7.8"), True, trusted=trusted), "5.6.7.8")      # адрес из сети
        self.assertEqual(client_ip(request("10.0.0.6", "5.6.7.8"), True, trusted=trusted), "10.0.0.6")      # чужая машина — не верим
        self.assertEqual(client_ip(request("10.0.0.5", "5.6.7.8"), False, trusted=trusted), "10.0.0.5")     # TRUST_PROXY=0 — не верим никому
        self.assertEqual(client_ip(request("10.0.0.5", "5.6.7.8"), True), "10.0.0.5")                      # список пуст — только 127.0.0.1


class RoutesCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "site").mkdir()
        (self.root / "site" / "index.html").write_text(PAGE, encoding="utf-8")
        self.db = await database_for(self)

    def app(self, role: str) -> web.Application:
        cfg = Config(bot_token="", admin_chat_ids=(111,), admin_password=PASSWORD, host="127.0.0.1", port=0,
                     site_dir=self.root / "site", data_dir=self.root / "data", trust_proxy=False, role=role)
        studio = StudioContext(store=StudioStore(self.db), studio=SimpleNamespace(), news=NewsStore(self.db),
                               wake_worker=lambda: None, wake_news=lambda: None, claude=False)
        return create_app(cfg, Storage(self.db), lambda: None, studio=studio)

    @staticmethod
    async def served(app: web.Application, method: str, path: str) -> bool:
        """Заведён ли такой путь на машине (не «404/405 от маршрутизатора»)."""
        match = await app.router.resolve(make_mocked_request(method, path))
        return match.http_exception is None

    async def test_each_path_lives_on_its_machine(self) -> None:
        web_app, admin, both = self.app("web"), self.app("admin"), self.app("all")
        for method, path in PUBLIC:
            with self.subTest(path=path):
                self.assertTrue(await self.served(web_app, method, path))
                self.assertFalse(await self.served(admin, method, path))
                self.assertTrue(await self.served(both, method, path))
        for method, path in ADMIN:
            with self.subTest(path=path):
                self.assertFalse(await self.served(web_app, method, path))
                self.assertTrue(await self.served(admin, method, path))
                self.assertTrue(await self.served(both, method, path))

    async def test_caddy_sends_admin_paths_to_admin_machine(self) -> None:
        """Caddy машины сайта и роли приложения согласны: новый адрес админки не останется без машины."""
        text = CADDYFILE.read_text(encoding="utf-8")
        rules = [(None, m.group(1).split()) for m in re.finditer(r"^\s*@admin\w* path (.+)$", text, re.M)]
        rules += [(m.group(1), m.group(2).split()) for m in re.finditer(r"@admin\w* \{\s*method (\w+)\s*path (.+)", text)]
        self.assertGreaterEqual(len(rules), 2)

        def to_admin(method: str, path: str) -> bool:
            return any((want is None or want == method) and any(path.startswith(p[:-1]) if p.endswith("*") else path == p
                                                                 for p in paths) for want, paths in rules)

        for role, expected in (("admin", True), ("web", False)):
            for route in self.app(role).router.routes():
                path = route.resource.canonical if route.resource else ""
                if route.method == "HEAD" or path == "/api/health":
                    continue
                with self.subTest(role=role, method=route.method, path=path):
                    self.assertEqual(to_admin(route.method, path), expected)

    async def test_health_names_role(self) -> None:
        for role in ("web", "admin"):
            client = TestClient(TestServer(self.app(role)))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            health = await (await client.get("/api/health")).json()
            self.assertEqual((health["ok"], health["role"]), (True, role))


class EventsCase(unittest.IsolatedAsyncioTestCase):
    async def test_listener_wakes_handlers(self) -> None:
        db = await database_for(self)
        woken: list[str] = []
        got = asyncio.Event()

        def handler(name: str):
            def wake() -> None:
                woken.append(name)
                got.set()
            return wake

        stop = asyncio.Event()
        listener = Listener(test_url(), {"order": handler("order"), "studio": handler("studio")}, retry=0.1)
        task = asyncio.create_task(listener.run(stop))

        async def finish() -> None:
            stop.set()
            await asyncio.wait_for(task, 5)
        self.addAsyncCleanup(finish)
        await asyncio.wait_for(listener.ready.wait(), 10)
        self.assertEqual(sorted(woken), ["order", "studio"])              # подключился — будит всех: вдруг что-то пропустил
        woken.clear()

        got.clear()
        await db.notify("order")
        await asyncio.wait_for(got.wait(), 5)
        self.assertEqual(woken, ["order"])

        got.clear()
        await db.notify("неизвестное")                                     # чужое событие — молча пропускаем
        await db.notify("studio")
        await asyncio.wait_for(got.wait(), 5)
        self.assertEqual(woken, ["order", "studio"])

    async def test_listener_waits_when_database_is_unreachable(self) -> None:
        stop = asyncio.Event()
        listener = Listener("postgresql://nobody:x@127.0.0.1:1/codexlab_test", {"order": lambda: None}, retry=0.05)
        task = asyncio.create_task(listener.run(stop))
        with self.assertLogs("site.db", "WARNING"):
            await asyncio.sleep(0.3)
        self.assertFalse(listener.ready.is_set())
        stop.set()
        await asyncio.wait_for(task, 5)                                     # остановка не ждёт новой попытки


class SchemaWaitCase(unittest.IsolatedAsyncioTestCase):
    async def test_web_machine_waits_for_schema(self) -> None:
        db = await database_for(self)
        latest = MIGRATIONS[-1][0]
        self.assertEqual(await db.schema_version(), latest)
        self.assertTrue(await db.wait_schema(timeout=1, every=0.05))

        web = Database(test_url(), max_size=1)
        self.assertEqual(await web.open(migrate=False), [])                 # машина сайта схему не трогает
        self.addAsyncCleanup(web.close)
        # Схема старее кода: машина сайта ждёт, а когда машина админки обновит её — продолжает.
        await db.run(lambda conn: conn.execute("DELETE FROM schema_migrations WHERE version = %s", (latest,)))

        async def put_back() -> None:                                       # что бы ни случилось — схема снова полная
            await db.run(lambda conn: conn.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT DO NOTHING", (latest,)))
        self.addAsyncCleanup(put_back)
        self.assertFalse(await web.wait_schema(timeout=0.2, every=0.05))
        waiting = asyncio.create_task(web.wait_schema(timeout=5, every=0.05))
        await asyncio.sleep(0.2)
        self.assertFalse(waiting.done())
        await db.run(lambda conn: conn.execute(
            "INSERT INTO schema_migrations (version) VALUES (%s)", (latest,)))
        self.assertTrue(await asyncio.wait_for(waiting, 5))


if __name__ == "__main__":
    unittest.main()
