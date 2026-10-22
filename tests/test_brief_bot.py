"""Тарифы и конструктор ТЗ в боте: цены с сайта, доллары по курсу NBU, полный проход до заявки, три языка."""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from aiogram.types import Update

from app.bot import build_dispatcher
from app.brief_bot import DEFAULT_PRICING, brief_doc, estimate, money, number, read_pricing, to_usd
from app.news import NewsStore
from app.rates import Rate
from app.storage import Storage
from test_news import TelegramCase, command, press, text_message
from test_server import make_config

RATE = Rate(usd=12650.43, date="2026-10-20", source="nbu", fetched=time.time())


class FixedRates:
    def __init__(self, rate: Rate | None = RATE) -> None:
        self.rate = rate

    async def usd(self) -> Rate | None:
        return self.rate


def site_page(pricing: dict) -> str:
    data = json.dumps({"v": 2, "pricing": pricing}, ensure_ascii=False)
    return f'<!doctype html><html><body><script type="application/json" id="site-data">{data}</script><script id="site-js"></script></body></html>'


def with_language(update: Update, code: str) -> Update:
    """То же сообщение или нажатие, но от человека с другим языком Telegram."""
    if update.message:
        sender = update.message.from_user.model_copy(update={"language_code": code})
        return update.model_copy(update={"message": update.message.model_copy(update={"from_user": sender})})
    sender = update.callback_query.from_user.model_copy(update={"language_code": code})
    return update.model_copy(update={"callback_query": update.callback_query.model_copy(update={"from_user": sender})})


def contact(update_id: int, chat_id: int, phone: str) -> Update:
    return Update.model_validate({"update_id": update_id, "message": {
        "message_id": update_id, "date": 1700000000, "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Тест"},
        "contact": {"phone_number": phone, "first_name": "Тест", "user_id": chat_id}}})


def username(update: Update, name: str) -> Update:
    sender = (update.message or update.callback_query).from_user.model_copy(update={"username": name})
    if update.message:
        return update.model_copy(update={"message": update.message.model_copy(update={"from_user": sender})})
    return update.model_copy(update={"callback_query": update.callback_query.model_copy(update={"from_user": sender})})


class PricingCase(unittest.TestCase):
    def test_prices_come_from_site_settings(self) -> None:
        with self.subTest("цены, которые поменяли в админке"):
            path = Path(self._tmp()) / "index.html"
            changed = json.loads(json.dumps(DEFAULT_PRICING))
            changed["types"]["landing"]["price"] = [4, 10]
            changed["features"]["pay"] = ["x", 3]                                   # битое значение — берём по умолчанию
            path.write_text(site_page(changed), encoding="utf-8")
            pricing = read_pricing(path)
            self.assertEqual(pricing["types"]["landing"]["price"], [4.0, 10.0])
            self.assertEqual(pricing["features"]["pay"], [2.0, 6.0])
        with self.subTest("сайта нет — цены по умолчанию"):
            self.assertEqual(read_pricing(Path(self._tmp()) / "missing.html")["types"]["bot"]["price"], [2.5, 12.0])

    def _tmp(self) -> str:
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name

    def test_estimate_matches_site_formula(self) -> None:
        pricing = read_pricing(Path("missing"))
        landing = {"type": "landing", "features": ["form", "tg", "analytics"], "langs": ["ru"]}
        self.assertEqual(estimate(landing, pricing), {"lo": 3.0, "hi": 9.0, "wl": 1, "wh": 2, "over": False})
        rich = {"type": "shop", "features": ["catalog", "cart", "pay", "admin", "analytics", "crm"], "langs": ["ru", "uz"],
                "brand": "none", "content": "part", "deadline": "asap", "budget": "b2"}
        # 15+4 = 19…62 → ×1,1…×1,2 → +1,5…5 логотип → +0,5…2 тексты → ×1,2…×1,3 срочность; бюджет 15 ниже 27,5 — «выше бюджета»
        self.assertEqual(estimate(rich, pricing), {"lo": 27.5, "hi": 106.0, "wl": 3, "wh": 7, "over": True})
        self.assertIsNone(estimate({"type": "spaceship"}, pricing))

    def test_dollars_like_on_site(self) -> None:
        self.assertEqual((to_usd(3, RATE.usd), to_usd(9, RATE.usd), to_usd(90, RATE.usd)), (240, 710, 7100))
        pricing = read_pricing(Path("missing"))
        self.assertEqual(money(3, 9, "ru", "usd", RATE, pricing), "240–710 USD")
        self.assertEqual(money(2.5, 12, "ru", "uzs", RATE, pricing), "2,5–12 млн сум")
        self.assertEqual(money(2.5, 12, "en", "uzs", RATE, pricing), "2.5–12 mln UZS")
        self.assertEqual(money(25, 90, "en", "usd", RATE, pricing), "2,000–7,100 USD")
        self.assertEqual(money(3, 9, "ru", "usd", None, pricing), "3–9 млн сум")      # курса нет — показываем сумы
        self.assertEqual(number(12650.43, "ru"), "12\u00a0650,4")

    def test_brief_doc_for_studio_is_russian(self) -> None:
        brief = {"type": "bot", "company": "Navbat", "about": "", "goals": ["booking"], "features": ["admin", "notify", "booking"],
                 "langs": ["ru", "uz"], "deadline": "month", "budget": "b1", "name": "Азиз", "phone": "+998 90 123 45 67", "tg": "aziz"}
        doc = brief_doc(brief, read_pricing(Path("missing")), RATE, "usd")
        for part in ("ТЕХНИЧЕСКОЕ ЗАДАНИЕ", "на разработку: telegram-бот", "Сценарии бота", "Принимать записи и брони",
                     "ФТ-3. Клиент выбирает свободное время", "Языки: Русский, O‘zbekcha.", "Бюджет: До 5 млн сум",
                     "Ориентир студии: 5–23 млн сум, 1–4 недели", "Клиент смотрел цены в долларах: 400–1\u00a0800 USD",
                     "Коротко описать бизнес", "Перевод текстов", "Telegram: @aziz", "Codex Lab"):
            self.assertIn(part, doc)
        self.assertNotIn("Дизайн и материалы", doc)                                  # боту логотип и фото не нужны


class BriefBotCase(TelegramCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        (self.root / "site").mkdir()
        (self.root / "site" / "index.html").write_text(site_page(DEFAULT_PRICING), encoding="utf-8")
        self.cfg = make_config(self.root, admin_chat_ids=(111,))
        self.storage = Storage(self.root / "orders.sqlite3")
        await self.storage.init()
        self.news = NewsStore(self.root / "news.sqlite3")
        await self.news.init()
        self.woken = 0
        self.dispatcher = build_dispatcher(self.cfg, self.storage, news=self.news, rates=FixedRates(), wake_orders=self.wake)
        self.update_id = 0

    def wake(self) -> None:
        self.woken += 1

    async def feed(self, *updates: Update) -> None:
        for update in updates:
            self.update_id += 1
            await self.dispatcher.feed_update(self.bot, update.model_copy(update={"update_id": self.update_id}))

    def last(self, chat: int) -> dict:
        """Последнее сообщение боту в этот чат: отправленное или исправленное."""
        sent = [f for m, f in self.telegram.calls if m in ("sendMessage", "editMessageText") and f.get("chat_id") == str(chat)]
        return sent[-1]

    async def test_start_shows_menu_in_users_language(self) -> None:
        await self.feed(command(0, 501, "/start"))
        self.assertIn("подписаны на наши новости", self.last(501)["text"])
        self.assertIn("menu:prices", self.last(501)["reply_markup"])
        await self.feed(with_language(command(0, 502, "/start"), "en"))
        self.assertIn("Codex Lab studio bot", self.last(502)["text"])
        self.assertIn("Build a brief", self.last(502)["reply_markup"])
        await self.feed(press(0, 502, "ui:lang:uz"))                                 # язык можно сменить вручную
        self.assertIn("o‘zbek tilida", self.last(502)["text"])
        await self.feed(command(0, 502, "/prices"))
        self.assertIn("tariflari", self.last(502)["text"])

    async def test_tariffs_and_currency(self) -> None:
        await self.feed(command(0, 600, "/prices"))
        text = self.last(600)["text"]
        self.assertIn("Лендинг — 3–9 млн сум, 1–2 недели", text)
        self.assertIn("Telegram-бот — 2,5–12 млн сум, 1–3 недели", text)
        await self.feed(press(0, 600, "cur:usd:list"))
        text = self.last(600)["text"]
        self.assertIn("Лендинг — 240–710 USD", text)
        self.assertIn("Курс NBU: 1 USD = 12\u00a0650,4 сум, 20.10.2026.", text)
        await self.feed(press(0, 600, "tariff:shop"))
        text = self.last(600)["text"]
        self.assertIn("1\u00a0200–3\u00a0950 USD", text)
        self.assertIn("• 6 разделов", text)
        self.assertIn("• Онлайн-оплата (Click, Payme, Uzum)", text)
        self.assertIn("brief:type:shop", self.last(600)["reply_markup"])

    async def test_full_brief_becomes_order(self) -> None:
        chat = 700
        await self.feed(command(0, chat, "/brief"))
        self.assertIn("Что нужно сделать?", self.last(chat)["text"])
        await self.feed(press(0, chat, "b:type:landing"), text_message(0, chat, "Tabassum"), press(0, chat, "b:skip"))
        self.assertIn("Шаг 4 из 12", self.last(chat)["text"])
        await self.feed(press(0, chat, "b:goal:lead"), press(0, chat, "b:goal:booking"), press(0, chat, "b:goal:booking"))
        buttons = [b["text"] for row in json.loads(self.last(chat)["reply_markup"])["inline_keyboard"] for b in row]
        self.assertIn("✅ Получать заявки и звонки", buttons)
        self.assertIn("▫️ Принимать записи и брони", buttons)                            # нажали дважды — снялось
        await self.feed(press(0, chat, "b:next"))
        self.assertIn("Ориентир сейчас: 3–9 млн сум, 1–2 недели", self.last(chat)["text"])
        await self.feed(press(0, chat, "b:feat:booking"))                               # +2…7 млн и неделя к сроку
        self.assertIn("Ориентир сейчас: 5–16 млн сум, 1–3 недели", self.last(chat)["text"])
        await self.feed(press(0, chat, "b:next"), press(0, chat, "b:lang:uz"), press(0, chat, "b:next"),
                        press(0, chat, "b:brand:yes"), press(0, chat, "b:content:part"), press(0, chat, "b:deadline:month"),
                        press(0, chat, "b:budget:b2"))
        self.assertIn("Как к вам обращаться?", self.last(chat)["text"])
        await self.feed(text_message(0, chat, "Нигора"))
        self.assertIn("нет имени пользователя", self.last(chat)["text"])                # без ника телефон обязателен
        await self.feed(text_message(0, chat, "Пропустить"))
        self.assertIn("нет имени пользователя", self.last(chat)["text"])
        await self.feed(text_message(0, chat, "12"))
        self.assertIn("не хватает цифр", self.last(chat)["text"])
        await self.feed(contact(0, chat, "998901234567"))
        summary = self.last(chat)["text"]
        for part in ("ТЗ готово", "Компания: Tabassum", "О бизнесе: уточним вместе", "Языки: Русский, O‘zbekcha",
                     "Контакт: Нигора, +998901234567", "Ориентир: 6–21 млн сум, 1–3 недели"):
            self.assertIn(part, summary)
        await self.feed(press(0, chat, "cur:usd:sum"))
        self.assertIn("Ориентир: 470–1\u00a0650 USD", self.last(chat)["text"])
        await self.feed(press(0, chat, "b:send"), press(0, chat, "b:send"))              # второе нажатие — уже без заявки
        orders = await self.storage.last(5)
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual((order.name, order.phone, order.project, order.estimate),
                         ("Нигора", "+998901234567", "Лендинг · из Telegram-бота", "6–21 млн сум, 1–3 нед."))
        self.assertIn("ФТ-4. Клиент выбирает свободное время", order.text)
        self.assertIn("Клиент смотрел цены в долларах", order.text)
        self.assertEqual(self.woken, 1)
        self.assertIn("Заявка № 1 отправлена", [f.get("text", "") for m, f in self.telegram.calls if m == "sendMessage"][-1])

    async def test_bot_brief_in_english_skips_design(self) -> None:
        chat = 800
        steps = [command(0, chat, "/brief"), press(0, chat, "b:type:bot"), press(0, chat, "b:skip"), press(0, chat, "b:skip"),
                 press(0, chat, "b:next"), press(0, chat, "b:next"), press(0, chat, "b:next")]
        await self.feed(*[username(with_language(u, "en"), "navbat_owner") for u in steps])
        self.assertIn("Step 7 of 10", self.last(chat)["text"])                           # логотипа и фото у бота нет
        self.assertIn("When do you need to launch?", self.last(chat)["text"])
        steps = [press(0, chat, "b:deadline:asap"), press(0, chat, "b:budget:unknown"), press(0, chat, "b:name:tg"),
                 text_message(0, chat, "Skip")]
        await self.feed(*[username(with_language(u, "en"), "navbat_owner") for u in steps])
        summary = self.last(chat)["text"]
        self.assertIn("Contact: Тест, @navbat_owner", summary)
        self.assertIn("Estimate: 3–15.5 mln UZS, 1–2 weeks", summary)
        await self.feed(username(with_language(press(0, chat, "b:send"), "en"), "navbat_owner"))
        order = (await self.storage.last(1))[0]
        self.assertEqual((order.telegram, order.phone, order.project), ("navbat_owner", "", "Telegram-бот · из Telegram-бота"))
        self.assertIn("Сжатый срок", order.text)                                         # ТЗ для студии — по-русски

    async def test_cancel_and_stale_buttons(self) -> None:
        chat = 900
        await self.feed(command(0, chat, "/brief"), press(0, chat, "b:type:corp"), command(0, chat, "/cancel"))
        self.assertIn("Отменено", self.last(chat)["text"])
        calls = len(self.telegram.calls)
        await self.feed(press(0, chat, "b:send"))                                        # старая кнопка ничего не отправляет
        self.assertEqual(await self.storage.last(1), [])
        self.assertEqual([m for m, _ in self.telegram.calls[calls:]], ["answerCallbackQuery"])

    async def test_order_limit_per_chat(self) -> None:
        chat = 950
        for _ in range(4):
            await self.feed(command(0, chat, "/brief"), username(press(0, chat, "b:type:landing"), "client"))
            for update in (press(0, chat, "b:skip"), press(0, chat, "b:skip"), press(0, chat, "b:next"), press(0, chat, "b:next"),
                           press(0, chat, "b:next"), press(0, chat, "b:brand:yes"), press(0, chat, "b:content:yes"),
                           press(0, chat, "b:deadline:calm"), press(0, chat, "b:budget:b3"), press(0, chat, "b:name:tg"),
                           text_message(0, chat, "Пропустить"), press(0, chat, "b:send")):
                await self.feed(username(update, "client"))
        self.assertEqual(len(await self.storage.last(10)), 3)
        self.assertIn("Слишком много заявок", self.last(chat)["text"])


if __name__ == "__main__":
    unittest.main()
