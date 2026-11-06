"""Проверки сервера. Запуск: python -m unittest discover -s tests -v
Telegram здесь подменён локальной заглушкой: настоящие сообщения никому не уходят."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.bot import Notifier, build_dispatcher, order_header, stats_text
from app.config import Config, ConfigError
from app.storage import Storage
from app.web import RateLimiter, client_ip, create_app, today
from app.site_content import SiteStore
from pgdb import database_for

PASSWORD = "очень-длинный-пароль-123"
PAGE = '<!doctype html><html><body><script type="application/json" id="site-data">{}</script><script id="site-js"></script></body></html>'


def make_config(root: Path, **over) -> Config:
    base = dict(bot_token="", admin_chat_ids=(111,), admin_password=PASSWORD, host="127.0.0.1", port=0,
                site_dir=root / "site", data_dir=root / "data", trust_proxy=False)
    base.update(over)
    return Config(**base)


def order_body(client_id="order-0001", **brief):
    data = {"type": "landing", "name": "Азиз", "phone": "+998 90 123 45 67", "tg": ""}
    data.update(brief)
    return {"id": client_id, "brief": data, "text": "ТЕХНИЧЕСКОЕ ЗАДАНИЕ\n1. Цель",
            "estimate": {"lo": 3, "hi": 9, "wl": 1, "wh": 2}}


class FakeTelegram:
    """Локальная заглушка Telegram Bot API: запоминает вызовы, может «падать»."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = False
        self.server: TestServer | None = None

    async def start(self) -> str:
        async def handle(request: web.Request) -> web.Response:
            method = request.match_info["method"]
            if request.content_type.startswith("multipart/"):
                form = await request.post()
                fields = {k: (v if isinstance(v, str) else f"<file {v.filename}>") for k, v in form.items()}
            else:
                fields = dict(await request.post())
            self.calls.append((method, fields))
            if self.fail:
                return web.json_response({"ok": False, "error_code": 500, "description": "Internal Server Error"}, status=500)
            chat = {"id": int(fields.get("chat_id", 0)), "type": "private"}
            return web.json_response({"ok": True, "result": {"message_id": len(self.calls), "date": 1700000000, "chat": chat}})
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self.server:
            await self.server.close()


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "site").mkdir()
        (self.root / "site" / "index.html").write_text(PAGE, encoding="utf-8")
        self.cfg = make_config(self.root)
        self.storage = Storage(await database_for(self))
        await self.storage.init()
        self.woken = 0
        self.client = await self._client(self.cfg)

    async def _client(self, cfg: Config) -> TestClient:
        def wake() -> None:
            self.woken += 1
        client = TestClient(TestServer(create_app(cfg, self.storage, wake)))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        return client

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    # ---------- сайт и состояние ----------
    async def test_index_and_health(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("site-js", await response.text())
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("frame-src https://www.google.com https://maps.google.com;", response.headers["Content-Security-Policy"])   # карта на «О нас»
        health = await (await self.client.get("/api/health")).json()
        self.assertEqual(health, {"app": "codexlab-site", "ok": True, "role": "all", "publish": True, "bot": False, "site": "git"})

    async def test_brand_files(self) -> None:
        brand = self.root / "site" / "brand"
        brand.mkdir()
        (brand / "og.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (brand / "favicon.ico").write_bytes(b"\x00\x00\x01\x00")
        (brand / "notes.txt").write_text("личное", encoding="utf-8")
        response = await self.client.get("/brand/og.png")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertIn("max-age", response.headers["Cache-Control"])
        self.assertIn("nosniff", response.headers["X-Content-Type-Options"])
        self.assertEqual((await self.client.get("/favicon.ico")).status, 200)
        for path in ["/brand/notes.txt", "/brand/missing.png", "/brand/..%2Findex.html", "/brand/%2E%2E%2Fsite%2Findex.html", "/brand/"]:
            self.assertEqual((await self.client.get(path)).status, 404, path)

    async def test_unknown_path_is_404_and_no_file_leak(self) -> None:
        for path in ["/data/orders.sqlite3", "/../.env", "/.env", "/site/backups/"]:
            self.assertEqual((await self.client.get(path)).status, 404, path)

    # ---------- заявки ----------
    async def test_order_saved_and_duplicate_ignored(self) -> None:
        first = await self.client.post("/api/order", json=order_body())
        self.assertEqual(first.status, 200)
        self.assertEqual(await first.json(), {"ok": True, "number": 1})
        again = await self.client.post("/api/order", json=order_body())           # тот же номер заявки с сайта
        self.assertEqual((await again.json())["number"], 1)
        other = await self.client.post("/api/order", json=order_body("order-0002", name="Дильноза", phone="", tg="@dilnoza_uz"))
        self.assertEqual((await other.json())["number"], 2)
        orders = await self.storage.last()
        self.assertEqual([o.number for o in orders], [2, 1])
        self.assertEqual(orders[0].telegram, "dilnoza_uz")
        self.assertEqual(orders[1].estimate, "3–9 млн сум, 1–2 нед.")
        self.assertEqual(self.woken, 2)                                            # дубль бота не будил

    async def test_order_validation(self) -> None:
        cases = {
            "нет контакта": order_body(phone="", tg=""),
            "короткий телефон": order_body(phone="12345", tg=""),
            "нет имени": order_body(name="  "),
            "плохой номер заявки": order_body("x"),
        }
        for label, body in cases.items():
            self.assertEqual((await self.client.post("/api/order", json=body)).status, 400, label)
        empty_text = order_body(); empty_text["text"] = "   "
        self.assertEqual((await self.client.post("/api/order", json=empty_text)).status, 400)
        self.assertEqual((await self.client.post("/api/order", data="не json", headers={"Content-Type": "application/json"})).status, 400)
        self.assertEqual((await self.client.post("/api/order", json=["список"])).status, 400)
        huge = order_body(); huge["text"] = "я" * 40_000
        self.assertEqual((await self.client.post("/api/order", json=huge)).status, 413)
        self.assertEqual(await self.storage.last(), [])

    async def test_order_rate_limit(self) -> None:
        for i in range(5):
            self.assertEqual((await self.client.post("/api/order", json=order_body(f"order-10{i:02d}"))).status, 200)
        self.assertEqual((await self.client.post("/api/order", json=order_body("order-1099"))).status, 429)

    # ---------- публикация ----------
    async def test_publish_requires_password(self) -> None:
        new_page = PAGE.replace("{}", '{"v":2}')
        bearer = lambda p: {"Authorization": "Bearer " + __import__("urllib.parse").parse.quote(p)}
        self.assertEqual((await self.client.post("/api/publish", data=new_page)).status, 401)
        self.assertEqual((await self.client.post("/api/publish", data=new_page, headers=bearer("не тот пароль"))).status, 401)
        self.assertEqual((await self.client.post("/api/publish", data="<p>не сайт</p>", headers=bearer(PASSWORD))).status, 400)
        ok = await self.client.post("/api/publish", data=new_page.encode("utf-8"), headers=bearer(PASSWORD))
        self.assertEqual(ok.status, 200)
        self.assertEqual(await (await self.client.get("/")).text(), new_page)       # посетитель видит опубликованное
        self.assertEqual(self.cfg.index_file.read_text(encoding="utf-8"), PAGE)     # файл из git не тронут: его меняют релизы
        self.assertEqual(len(await SiteStore(self.storage.db).history()), 1)        # публикация — в базе

    async def test_publish_bruteforce_is_blocked(self) -> None:
        for _ in range(5):
            self.assertEqual((await self.client.post("/api/publish", data=PAGE, headers={"Authorization": "Bearer x"})).status, 401)
        blocked = await self.client.post("/api/publish", data=PAGE, headers={"Authorization": "Bearer " + PASSWORD})
        self.assertEqual(blocked.status, 429)
        self.assertEqual(await SiteStore(self.storage.db).history(), [])            # ничего не опубликовано

    async def test_publish_disabled_without_password(self) -> None:
        client = await self._client(make_config(self.root, admin_password=""))
        self.assertEqual((await client.post("/api/publish", data=PAGE, headers={"Authorization": "Bearer "})).status, 403)

    def test_client_ip_trusts_only_local_proxy(self) -> None:
        request = lambda remote, forwarded="": SimpleNamespace(remote=remote, headers={"X-Forwarded-For": forwarded} if forwarded else {})
        self.assertEqual(client_ip(request("127.0.0.1", "1.1.1.1, 5.6.7.8"), True), "5.6.7.8")   # последний адрес дописал наш прокси
        self.assertEqual(client_ip(request("203.0.113.9", "5.6.7.8"), True), "203.0.113.9")      # напрямую извне — заголовку не верим
        self.assertEqual(client_ip(request("127.0.0.1", "5.6.7.8"), False), "127.0.0.1")
        # IPv6 — одна сеть /64 на клиента: перебор адресов внутри неё лимиты не обходит
        self.assertEqual(client_ip(request("2001:db8:1:2::1"), True), client_ip(request("2001:db8:1:2:ffff::9"), True))
        self.assertNotEqual(client_ip(request("2001:db8:1:2::1"), True), client_ip(request("2001:db8:1:3::1"), True))
        self.assertEqual(client_ip(request("::ffff:5.6.7.8"), True), "5.6.7.8")

    def test_client_ip_behind_cloud_platform(self) -> None:
        request = lambda remote, forwarded="": SimpleNamespace(remote=remote, headers={"X-Forwarded-For": forwarded} if forwarded else {})
        cloud = lambda remote, forwarded="": client_ip(request(remote, forwarded), True, True)
        self.assertEqual(cloud("10.204.5.7", "203.0.113.5"), "203.0.113.5")
        self.assertEqual(cloud("10.204.5.7", "203.0.113.5, 162.158.90.1, 10.10.0.4"), "203.0.113.5")      # Cloudflare и балансировщик пропущены
        self.assertEqual(cloud("10.204.5.7", "198.51.100.77, 203.0.113.5, 172.70.1.2"), "203.0.113.5")    # подделку клиента слева не берём
        self.assertEqual(cloud("10.204.5.7", "2001:db8:1:2::9, 2606:4700::1"), "2001:db8:1:2::/64")
        self.assertEqual(cloud("10.204.5.7", "мусор, 203.0.113.5"), "203.0.113.5")
        self.assertEqual(cloud("10.204.5.7", "203.0.113.5, мусор"), "10.204.5.7")                         # мусор справа — заголовку не верим
        self.assertEqual(cloud("10.204.5.7"), "10.204.5.7")
        self.assertEqual(cloud("198.51.100.4", "203.0.113.5"), "198.51.100.4")      # пришёл не из сети платформы — заголовку не верим
        self.assertEqual(client_ip(request("10.204.5.7", "203.0.113.5"), True), "10.204.5.7")              # режим выключен

    def test_trust_proxy_setting(self) -> None:
        def load(**env: str) -> Config:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("app.config.load_env_file"):
                return Config.from_env()
        self.assertEqual((load().trust_proxy, load().cloud_proxy), (True, False))                           # по умолчанию — Caddy или nginx
        self.assertEqual((load(RENDER="true").trust_proxy, load(RENDER="true").cloud_proxy), (True, True))   # Render узнаём сами
        self.assertFalse(load(RENDER="true", TRUST_PROXY="1").cloud_proxy)                                   # явная настройка важнее
        self.assertTrue(load(TRUST_PROXY="cloud").cloud_proxy)
        self.assertEqual((load(TRUST_PROXY="0").trust_proxy, load(TRUST_PROXY="0").cloud_proxy), (False, False))

    def test_rate_limiter_window(self) -> None:
        now = [0.0]
        limiter = RateLimiter(limit=2, window=10, clock=lambda: now[0])
        limiter.hit("a"); limiter.hit("a")
        self.assertTrue(limiter.blocked("a"))
        self.assertFalse(limiter.blocked("b"))
        now[0] = 10.1
        self.assertFalse(limiter.blocked("a"))                                      # окно прошло

    # ---------- Telegram ----------
    async def _bot(self, telegram: FakeTelegram) -> Bot:
        base = await telegram.start()
        self.addAsyncCleanup(telegram.close)
        bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(base)),
                  default=DefaultBotProperties(parse_mode=None))
        self.addAsyncCleanup(bot.session.close)
        return bot

    async def test_old_orders_are_purged(self) -> None:
        await self.client.post("/api/order", json=order_body("order-old1"))
        await self.client.post("/api/order", json=order_body("order-new1"))
        db = await database_for(self)                                                # первая заявка — давняя
        await db.run(lambda conn: conn.execute("UPDATE orders SET created_at = '2020-01-01T00:00:00+00:00' WHERE number = 1"))
        self.assertEqual(await Notifier(make_config(self.root, order_retention_days=0), self.storage, None).purge_expired(), 0)
        self.assertIsNotNone(await self.storage.get(1))                              # срок 0 — ничего не удаляем
        self.assertEqual(await Notifier(self.cfg, self.storage, None).purge_expired(), 1)
        self.assertIsNone(await self.storage.get(1))                                 # старая удалена
        self.assertIsNotNone(await self.storage.get(2))                              # свежая на месте

    async def test_notifier_delivers_and_retries(self) -> None:
        telegram = FakeTelegram()
        bot = await self._bot(telegram)
        cfg = make_config(self.root, admin_chat_ids=(111, 222))
        notifier = Notifier(cfg, self.storage, bot)
        await self.client.post("/api/order", json=order_body())

        telegram.fail = True                                                         # Telegram недоступен
        self.assertEqual(await notifier.deliver_pending(), 0)
        self.assertFalse((await self.storage.get(1)).delivered)                      # заявка не потеряна
        self.assertEqual((await self.storage.get(1)).attempts, 1)

        telegram.fail = False
        telegram.calls.clear()
        self.assertEqual(await notifier.deliver_pending(), 1)
        self.assertTrue((await self.storage.get(1)).delivered)
        self.assertEqual([(m, f["chat_id"]) for m, f in telegram.calls], [("sendMessage", "111"), ("sendMessage", "222")])
        text = telegram.calls[0][1]["text"]
        self.assertIn("Новая заявка № 1", text)
        self.assertIn("Телефон: +998 90 123 45 67", text)
        self.assertIn("ТЕХНИЧЕСКОЕ ЗАДАНИЕ", text)
        self.assertNotIn("parse_mode", telegram.calls[0][1])                         # текст клиента не разбирается как разметка

        telegram.calls.clear()
        self.assertEqual(await notifier.deliver_pending(), 0)                        # повторно не шлём
        self.assertEqual(telegram.calls, [])

    async def test_long_brief_goes_as_file(self) -> None:
        telegram = FakeTelegram()
        bot = await self._bot(telegram)
        notifier = Notifier(self.cfg, self.storage, bot)
        body = order_body(); body["text"] = "Строка технического задания.\n" * 300    # ~8700 знаков
        await self.client.post("/api/order", json=body)
        self.assertEqual(await notifier.deliver_pending(), 1)
        self.assertEqual([m for m, _ in telegram.calls], ["sendMessage", "sendDocument"])
        self.assertIn("<file TZ-1.txt>", telegram.calls[1][1].values())             # ТЗ приложено файлом

    async def test_bot_commands(self) -> None:
        telegram = FakeTelegram()
        bot = await self._bot(telegram)
        dispatcher = build_dispatcher(self.cfg, self.storage)
        await self.client.post("/api/order", json=order_body())

        def update(update_id: int, chat_id: int, text: str) -> Update:
            return Update.model_validate({"update_id": update_id, "message": {
                "message_id": update_id, "date": 1700000000, "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"}, "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}})

        await dispatcher.feed_update(bot, update(1, 555, "/start"))                  # посторонний узнаёт номер чата
        self.assertIn("Номер этого чата: 555", telegram.calls[-1][1]["text"])
        before = len(telegram.calls)
        await dispatcher.feed_update(bot, update(2, 555, "/last"))                   # но заявок не видит
        await dispatcher.feed_update(bot, update(3, 555, "/forget 1"))
        self.assertEqual(len(telegram.calls), before)
        self.assertIsNotNone(await self.storage.get(1))

        await dispatcher.feed_update(bot, update(4, 111, "/last"))                   # администратор видит
        self.assertIn("№ 1 (не доставлена)", telegram.calls[-1][1]["text"])
        self.assertIn("Азиз", telegram.calls[-1][1]["text"])
        await dispatcher.feed_update(bot, update(5, 111, "/forget"))
        self.assertIn("/forget 12", telegram.calls[-1][1]["text"])
        await dispatcher.feed_update(bot, update(6, 111, "/forget 1"))
        self.assertIn("удалена", telegram.calls[-1][1]["text"])
        self.assertIsNone(await self.storage.get(1))

    async def test_funnel_events_are_counted_without_personal_data(self) -> None:
        for name in ("visit", "visit", "visit", "brief_start", "brief_done"):
            self.assertEqual((await self.client.post("/api/event", json={"name": name})).status, 200)
        # заявку считает сервер: прислать «order» с сайта нельзя, как и выдуманное событие
        for bad in ({"name": "order"}, {"name": "hack"}, {"name": 5}, ["visit"], {}):
            self.assertEqual((await self.client.post("/api/event", json=bad)).status, 400, bad)
        self.assertEqual((await self.client.post("/api/event", data="не json")).status, 400)
        await self.client.post("/api/order", json=order_body())
        await self.client.post("/api/order", json=order_body())                      # дубль заявки счётчик не увеличивает
        totals = await self.storage.totals(today())
        self.assertEqual(totals, {"visit": 3, "brief_start": 1, "brief_done": 1, "order": 1})
        db = await database_for(self)
        columns = await db.run(lambda conn: [row["column_name"] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'events' ORDER BY ordinal_position")])
        self.assertEqual(columns, ["day", "name", "count"])                           # в таблице только день, шаг и число

    async def test_funnel_event_rate_limit(self) -> None:
        for _ in range(30):
            self.assertEqual((await self.client.post("/api/event", json={"name": "visit"})).status, 200)
        self.assertEqual((await self.client.post("/api/event", json={"name": "visit"})).status, 429)
        self.assertEqual((await self.storage.totals(today()))["visit"], 30)

    async def test_stats_command(self) -> None:
        telegram = FakeTelegram()
        bot = await self._bot(telegram)
        dispatcher = build_dispatcher(self.cfg, self.storage)
        for _ in range(40):
            await self.storage.bump("visit", today())
        for _ in range(4):
            await self.storage.bump("brief_done", today())
        await self.storage.bump("order", today())
        await self.storage.bump("visit", "2020-01-01")                                # старые дни в сводку не попадают

        def update(update_id: int, chat_id: int, text: str) -> Update:
            return Update.model_validate({"update_id": update_id, "message": {
                "message_id": update_id, "date": 1700000000, "chat": {"id": chat_id, "type": "private"},
                "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"}, "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}]}})

        before = len(telegram.calls)
        await dispatcher.feed_update(bot, update(1, 555, "/stats"))                   # посторонний сводку не получает
        self.assertEqual(len(telegram.calls), before)
        await dispatcher.feed_update(bot, update(2, 111, "/stats"))
        text = telegram.calls[-1][1]["text"]
        self.assertIn("Открыли сайт: 40 / 40", text)
        self.assertIn("Собрали ТЗ: 4 / 4", text)
        self.assertIn("дошли 10% посетителей", text)
        self.assertIn("отправили 25% собравших ТЗ", text)
        self.assertIn("—", stats_text({}, {}))                                        # пустая статистика не делит на ноль

    async def test_header_has_tashkent_time(self) -> None:
        await self.client.post("/api/order", json=order_body())
        header = order_header(await self.storage.get(1))
        self.assertIn("(Ташкент)", header)
        self.assertIn("Ориентир: 3–9 млн сум, 1–2 нед.", header)


class AdminAccessCase(unittest.IsolatedAsyncioTestCase):
    """Права в боте проверяются у человека, а уведомления об админских действиях доходят до чатов."""

    GROUP = -100500
    ADMIN = 111
    MEMBER = 555

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "site").mkdir()
        (self.root / "site" / "index.html").write_text(PAGE, encoding="utf-8")
        self.cfg = make_config(self.root, admin_chat_ids=(self.GROUP,), admin_user_ids=(self.ADMIN,), trust_proxy=True)
        self.storage = Storage(await database_for(self))
        await self.storage.init()
        self.alerts: list[str] = []
        self.client = TestClient(TestServer(create_app(self.cfg, self.storage, lambda: None, on_admin_action=self.alerts.append)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.telegram = FakeTelegram()
        url = await self.telegram.start()
        self.addAsyncCleanup(self.telegram.close)
        self.bot = Bot("123456:TEST", default=DefaultBotProperties(parse_mode=None), session=AiohttpSession(api=TelegramAPIServer.from_base(url)))
        self.addAsyncCleanup(self.bot.session.close)

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def update(update_id: int, chat_id: int, user_id: int, text: str) -> Update:
        chat_type = "private" if chat_id > 0 else "supergroup"
        return Update.model_validate({"update_id": update_id, "message": {
            "message_id": update_id, "date": 1700000000, "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": user_id, "is_bot": False, "first_name": "Тест"}, "text": text,
            "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}})

    def test_who_is_admin(self) -> None:
        cfg = self.cfg
        self.assertTrue(cfg.is_admin(self.GROUP, self.ADMIN))             # администратор в служебной группе
        self.assertTrue(cfg.is_admin(self.ADMIN, self.ADMIN))             # и в личном чате с ботом
        self.assertFalse(cfg.is_admin(self.GROUP, self.MEMBER))           # участник группы — нет
        self.assertFalse(cfg.is_admin(self.MEMBER, self.MEMBER))          # посторонний в личке — нет
        self.assertFalse(cfg.is_admin(-999, self.ADMIN))                  # в чужой группе заявки не показываем
        self.assertFalse(cfg.is_admin(self.GROUP, None))                  # сообщение без автора
        old_style = make_config(self.root, admin_chat_ids=(111, -5))      # настройка без ADMIN_USER_IDS
        self.assertTrue(old_style.is_admin(111, 111))                     # личный чат работает как раньше
        self.assertTrue(old_style.is_admin(-5, 111))                      # и в группе этот человек остаётся администратором
        self.assertFalse(old_style.is_admin(-5, 222))                     # а остальные участники группы — нет
        self.assertEqual(old_style.admin_users, (111,))

    async def test_group_member_gets_no_admin_commands(self) -> None:
        await self.client.post("/api/order", json=order_body())
        dispatcher = build_dispatcher(self.cfg, self.storage)
        for number, text in enumerate(("/last", "/stats", "/forget 1"), 1):
            await dispatcher.feed_update(self.bot, self.update(number, self.GROUP, self.MEMBER, text))
        self.assertEqual(self.telegram.calls, [])
        self.assertIsNotNone(await self.storage.get(1))                   # заявка не удалена
        await dispatcher.feed_update(self.bot, self.update(4, self.GROUP, self.ADMIN, "/last"))
        self.assertIn("Азиз", self.telegram.calls[-1][1]["text"])

    async def test_group_start_and_id(self) -> None:
        dispatcher = build_dispatcher(self.cfg, self.storage)
        await dispatcher.feed_update(self.bot, self.update(1, self.GROUP, self.MEMBER, "/start"))
        self.assertIn("только администраторам", self.telegram.calls[-1][1]["text"])
        await dispatcher.feed_update(self.bot, self.update(2, self.GROUP, self.MEMBER, "/id"))
        self.assertIn(f"Ваш личный номер: {self.MEMBER}", self.telegram.calls[-1][1]["text"])

    async def test_total_password_limit_stops_guessing_from_many_addresses(self) -> None:
        wrong = {"Authorization": "Bearer не-тот-пароль"}
        for number in range(30):                                          # каждый раз новый адрес: лимит на адрес не срабатывает
            response = await self.client.post("/api/publish", data=PAGE, headers={**wrong, "X-Forwarded-For": f"8.8.{number}.1"})
            self.assertEqual(response.status, 401)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Подбор пароля", self.alerts[0])
        right = {"Authorization": f"Bearer {quote(PASSWORD)}", "X-Forwarded-For": "8.8.200.1"}
        blocked = await self.client.post("/api/publish", data=PAGE, headers=right)
        self.assertEqual(blocked.status, 429)                             # по ответу нельзя понять, что пароль верный
        self.assertEqual(len(self.alerts), 1)

    async def test_publish_is_reported(self) -> None:
        headers = {"Authorization": f"Bearer {quote(PASSWORD)}", "X-Forwarded-For": "8.8.4.4"}
        self.assertEqual((await self.client.post("/api/publish", data=PAGE, headers=headers)).status, 200)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn("Адрес: 8.8.4.4", self.alerts[0])
        self.assertIn("Публикация №1: страница целиком", self.alerts[0])  # номер — чтобы вернуть прежнюю

    async def test_alert_waits_until_telegram_is_back(self) -> None:
        notifier = Notifier(self.cfg, self.storage, self.bot)
        notifier.alert("Сайт опубликован")
        self.telegram.fail = True
        await notifier.deliver_alerts()
        self.telegram.fail = False
        await notifier.deliver_alerts()
        await notifier.deliver_alerts()                                   # второй раз не дублирует
        self.assertEqual([fields["text"] for method, fields in self.telegram.calls], ["Сайт опубликован"] * 2)
        self.assertEqual(self.telegram.calls[-1][1]["chat_id"], str(self.GROUP))


class AdminConfigCase(unittest.TestCase):
    def test_admin_user_ids_are_read_and_checked(self) -> None:
        with mock.patch.dict(os.environ, {"ADMIN_CHAT_IDS": "-100500", "ADMIN_USER_IDS": "111, 222"}), mock.patch("app.config.load_env_file"):
            self.assertEqual(Config.from_env().admin_user_ids, (111, 222))
        with mock.patch.dict(os.environ, {"ADMIN_USER_IDS": "-100500"}), mock.patch("app.config.load_env_file"):
            with self.assertRaises(ConfigError):
                Config.from_env()


if __name__ == "__main__":
    unittest.main()
