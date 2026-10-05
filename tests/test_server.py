"""Проверки сервера. Запуск: python -m unittest discover -s tests -v
Telegram здесь подменён локальной заглушкой: настоящие сообщения никому не уходят."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Update
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app.bot import Notifier, build_dispatcher, order_header, stats_text
from app.config import Config
from app.storage import Storage
from app.web import RateLimiter, create_app, today

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
        self.storage = Storage(self.cfg.data_dir / "orders.sqlite3")
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
        health = await (await self.client.get("/api/health")).json()
        self.assertEqual(health, {"app": "varaq-site", "ok": True, "publish": True, "bot": False})

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
        self.assertEqual(self.cfg.index_file.read_text(encoding="utf-8"), new_page)
        backups = list((self.cfg.site_dir / "backups").glob("index-*.html"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), PAGE)             # прежняя версия цела

    async def test_publish_bruteforce_is_blocked(self) -> None:
        for _ in range(5):
            self.assertEqual((await self.client.post("/api/publish", data=PAGE, headers={"Authorization": "Bearer x"})).status, 401)
        blocked = await self.client.post("/api/publish", data=PAGE, headers={"Authorization": "Bearer " + PASSWORD})
        self.assertEqual(blocked.status, 429)
        self.assertEqual(self.cfg.index_file.read_text(encoding="utf-8"), PAGE)

    async def test_publish_disabled_without_password(self) -> None:
        client = await self._client(make_config(self.root, admin_password=""))
        self.assertEqual((await client.post("/api/publish", data=PAGE, headers={"Authorization": "Bearer "})).status, 403)

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
        columns = [row[1] for row in sqlite3.connect(self.cfg.data_dir / "orders.sqlite3").execute("PRAGMA table_info(events)")]
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


if __name__ == "__main__":
    unittest.main()
