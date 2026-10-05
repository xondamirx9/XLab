"""Новости: подписка, рассылка без дублей, отписка заблокировавших бота, команды /start, /stop, /post."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import Update
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.bot import build_dispatcher
from app.news import Broadcaster, NewsStore
from app.storage import Storage
from test_server import make_config


class FakeTelegram:
    """Заглушка Bot API: чаты из blocked отвечают 403 (бот заблокирован)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.blocked: set[int] = set()
        self.server: TestServer | None = None

    async def start(self) -> str:
        async def handle(request: web.Request) -> web.Response:
            fields = dict(await request.post())
            method = request.match_info["method"]
            self.calls.append((method, fields))
            chat_id = int(fields.get("chat_id", 0) or 0)
            if chat_id in self.blocked:
                return web.json_response({"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}, status=403)
            if method == "answerCallbackQuery":
                return web.json_response({"ok": True, "result": True})
            return web.json_response({"ok": True, "result": {"message_id": len(self.calls), "date": 1700000000,
                                                              "chat": {"id": chat_id, "type": "private"}}})
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", handle)
        self.server = TestServer(app)
        await self.server.start_server()
        return str(self.server.make_url("")).rstrip("/")

    async def close(self) -> None:
        if self.server:
            await self.server.close()

    def texts(self, chat_id: int) -> list[str]:
        return [f.get("text", f.get("caption", "")) for m, f in self.calls
                if f.get("chat_id") == str(chat_id) and m in ("sendMessage", "sendPhoto")]


def command(update_id: int, chat_id: int, text: str) -> Update:
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": update_id, "date": 1700000000, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"}, "text": text,
        "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}})


def text_message(update_id: int, chat_id: int, text: str) -> Update:
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": update_id, "date": 1700000000, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"}, "text": text}})


def press(update_id: int, chat_id: int, data: str) -> Update:
    return Update.model_validate({"update_id": update_id, "callback_query": {
        "id": str(update_id), "chat_instance": "1", "data": data,
        "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"},
        "message": {"message_id": 1, "date": 1700000000, "chat": {"id": chat_id, "type": "private"}, "text": "…"}}})


class TelegramCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.telegram = FakeTelegram()
        base = await self.telegram.start()
        self.bot = Bot("123456:TEST", session=AiohttpSession(api=TelegramAPIServer.from_base(base)),
                       default=DefaultBotProperties(parse_mode=None))

    async def asyncTearDown(self) -> None:
        await self.bot.session.close()
        await self.telegram.close()
        self.tmp.cleanup()


class NewsCase(TelegramCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.news = NewsStore(self.root / "news.sqlite3")
        await self.news.init()

    async def test_subscribe_counts(self) -> None:
        self.assertTrue(await self.news.subscribe(1, "Аня"))
        self.assertFalse(await self.news.subscribe(1, "Аня"))                 # повторная подписка — не новая
        await self.news.subscribe(2)
        self.assertTrue(await self.news.unsubscribe(2))
        self.assertFalse(await self.news.unsubscribe(2))
        self.assertEqual(await self.news.counts(), (1, 2))
        self.assertTrue(await self.news.is_subscribed(1))
        self.assertFalse(await self.news.is_subscribed(2))

    async def test_broadcast_once_and_blocked_unsubscribed(self) -> None:
        for chat in (10, 20, 30, 40):
            await self.news.subscribe(chat)
        await self.news.unsubscribe(40)
        self.telegram.blocked.add(30)
        news_id = await self.news.add_news("Новая услуга", "Теперь делаем сайты за 3 дня", "https://varaq.uz/news")
        sender = Broadcaster(self.news, self.bot, pause=0)
        self.assertEqual(await sender.deliver_pending(), 2)
        sent = [f["chat_id"] for m, f in self.telegram.calls if m == "sendMessage"]
        self.assertEqual(sorted(sent), ["10", "20", "30"])                      # отписанному 40 не пишем
        self.assertIn("📰 Новая услуга", self.telegram.texts(10)[0])
        self.assertIn("https://varaq.uz/news", self.telegram.calls[0][1]["reply_markup"])  # кнопка со ссылкой
        self.assertFalse(await self.news.is_subscribed(30))                    # заблокировал бота — отписан
        item = await self.news.get(news_id)
        self.assertEqual((item.status, item.sent, item.failed), ("done", 2, 1))
        self.telegram.calls.clear()
        self.assertEqual(await sender.deliver_pending(), 0)                     # повторно никому не шлём
        self.assertEqual(self.telegram.calls, [])

    async def test_resume_after_restart_has_no_duplicates(self) -> None:
        for chat in (1, 2, 3):
            await self.news.subscribe(chat)
        news_id = await self.news.add_news("Акция", "Скидка")
        await self.news.record(news_id, 1, True)                                # до «перезапуска» успели отправить первому
        await Broadcaster(self.news, self.bot, pause=0).deliver_pending()
        self.assertEqual(sorted(f["chat_id"] for m, f in self.telegram.calls if m == "sendMessage"), ["2", "3"])

    async def test_bot_start_stop_post(self) -> None:
        cfg = make_config(self.root, admin_chat_ids=(111,))
        storage = Storage(self.root / "orders.sqlite3")
        await storage.init()
        woken = []
        dispatcher = build_dispatcher(cfg, storage, news=self.news, wake_news=lambda: woken.append(1))
        await dispatcher.feed_update(self.bot, command(1, 555, "/start"))
        self.assertTrue(await self.news.is_subscribed(555))
        self.assertIn("подписаны на наши новости", self.telegram.texts(555)[-1])
        await dispatcher.feed_update(self.bot, command(2, 555, "/stop"))
        self.assertFalse(await self.news.is_subscribed(555))
        await dispatcher.feed_update(self.bot, command(3, 555, "/post Взлом\nчужой текст"))   # посторонний не рассылает
        self.assertEqual(await self.news.latest(), [])
        await dispatcher.feed_update(self.bot, command(4, 111, "/post Мы открылись\nЖдём вас!\nhttps://varaq.uz"))
        latest = await self.news.latest()
        self.assertEqual((latest[0].title, latest[0].body, latest[0].url), ("Мы открылись", "Ждём вас!", "https://varaq.uz"))
        self.assertEqual(woken, [1])
        await dispatcher.feed_update(self.bot, command(5, 555, "/id"))
        self.assertIn("Номер этого чата: 555", self.telegram.texts(555)[-1])


if __name__ == "__main__":
    unittest.main()
