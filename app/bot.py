"""Telegram-бот: пересылает заявки администраторам и отвечает на служебные команды."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, Message

from app.config import Config
from app.storage import Order, Storage

log = logging.getLogger("site.bot")

MAX_ATTEMPTS = 30            # после этого заявка остаётся в базе, но пересылать её перестаём
RETRY_EVERY = 60             # секунд между повторными попытками
MESSAGE_LIMIT = 3900         # у Telegram предел 4096 знаков в сообщении
TASHKENT = timezone(timedelta(hours=5))


def _when(created_at: str) -> str:
    try:
        return datetime.fromisoformat(created_at).astimezone(TASHKENT).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return created_at


def _share(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.0f}%".replace(".", ",") if whole else "—"


def stats_text(week: dict[str, int], month: dict[str, int]) -> str:
    """Воронка за 7 и 30 дней: сколько зашло, начало ТЗ, собрало ТЗ, отправило заявку."""
    rows = [("Открыли сайт", "visit"), ("Начали ТЗ", "brief_start"), ("Собрали ТЗ", "brief_done"), ("Отправили заявку", "order")]
    lines = ["Воронка сайта: 7 дней / 30 дней"]
    for title, key in rows:
        lines.append(f"{title}: {week.get(key, 0)} / {month.get(key, 0)}")
    visits, done, orders = month.get("visit", 0), month.get("brief_done", 0), month.get("order", 0)
    lines.append("")
    lines.append(f"За 30 дней до готового ТЗ дошли {_share(done, visits)} посетителей (цель — 5%).")
    lines.append(f"Заявку отправили {_share(orders, done)} собравших ТЗ (цель — 50%).")
    lines.append("Считаются сеансы, а не люди: имена, адреса и устройства не записываются.")
    return "\n".join(lines)


def order_header(order: Order) -> str:
    lines = [f"Новая заявка № {order.number}", f"Проект: {order.project}", f"Имя: {order.name}"]
    if order.phone:
        lines.append(f"Телефон: {order.phone}")
    if order.telegram:
        lines.append(f"Telegram: @{order.telegram}")
    if order.estimate:
        lines.append(f"Ориентир: {order.estimate}")
    lines.append(f"Получена: {_when(order.created_at)} (Ташкент)")
    return "\n".join(lines)


class Notifier:
    """Пересылает сохранённые заявки. Не получилось сейчас — повторит позже, заявка не пропадёт."""

    def __init__(self, cfg: Config, storage: Storage, bot: Bot | None) -> None:
        self._cfg, self._storage, self._bot = cfg, storage, bot
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def _send_to(self, chat_id: int, order: Order) -> bool:
        assert self._bot is not None
        header = order_header(order)
        try:
            if len(header) + len(order.text) + 2 <= MESSAGE_LIMIT:
                await self._bot.send_message(chat_id, f"{header}\n\n{order.text}")
            else:   # длинное ТЗ уходит файлом, чтобы Telegram не обрезал текст
                await self._bot.send_message(chat_id, header)
                file = BufferedInputFile(order.text.encode("utf-8"), filename=f"TZ-{order.number}.txt")
                await self._bot.send_document(chat_id, file, caption=f"ТЗ к заявке № {order.number}")
            return True
        except TelegramRetryAfter as exc:
            log.warning("Telegram просит подождать %s с; заявка %s уйдёт позже", exc.retry_after, order.number)
        except TelegramAPIError as exc:
            log.warning("заявка %s не доставлена в чат %s: %s", order.number, chat_id, type(exc).__name__)
        return False

    async def deliver_pending(self) -> int:
        """Пробует переслать всё недоставленное. Возвращает число доставленных заявок."""
        if self._bot is None or not self._cfg.admin_chat_ids:
            return 0
        done = 0
        for order in await self._storage.pending(MAX_ATTEMPTS):
            results = [await self._send_to(chat_id, order) for chat_id in self._cfg.admin_chat_ids]
            delivered = any(results)          # хватит одного администратора: заявка увидена
            await self._storage.mark(order.number, delivered)
            done += int(delivered)
        return done

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.deliver_pending()
            except Exception:                 # цикл доставки не должен умирать из-за одной ошибки
                log.exception("сбой при пересылке заявок")
            self._wake.clear()
            waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
            _, pending = await asyncio.wait(waiters, timeout=RETRY_EVERY, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()


def build_dispatcher(cfg: Config, storage: Storage) -> Dispatcher:
    router = Router()
    is_admin = lambda message: message.chat.id in cfg.admin_chat_ids

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        if is_admin(message):
            await message.answer(
                "Здравствуйте! Заявки с сайта приходят в этот чат.\n\n"
                "/last — последние пять заявок\n/stats — воронка сайта за 7 и 30 дней\n/forget 12 — удалить заявку № 12 вместе с контактами клиента"
            )
        else:
            await message.answer(
                f"Здравствуйте! Это служебный бот сайта.\n\nНомер этого чата: {message.chat.id}\n"
                "Чтобы получать сюда заявки, впишите этот номер в ADMIN_CHAT_IDS в файле .env и перезапустите сервер."
            )

    @router.message(Command("last"))
    async def last(message: Message) -> None:
        if not is_admin(message):
            return
        orders = await storage.last(5)
        if not orders:
            await message.answer("Заявок пока нет.")
            return
        rows = []
        for order in orders:
            contact = order.phone or (f"@{order.telegram}" if order.telegram else "без контакта")
            mark = "" if order.delivered else " (не доставлена)"
            rows.append(f"№ {order.number}{mark} · {_when(order.created_at)}\n{order.project} · {order.name} · {contact}")
        await message.answer("\n\n".join(rows))

    @router.message(Command("stats"))
    async def stats(message: Message) -> None:
        if not is_admin(message):
            return
        now = datetime.now(TASHKENT)
        day = lambda back: (now - timedelta(days=back)).strftime("%Y-%m-%d")
        await message.answer(stats_text(await storage.totals(day(6)), await storage.totals(day(29))))

    @router.message(Command("forget"))
    async def forget(message: Message, command: CommandObject) -> None:
        if not is_admin(message):
            return
        arg = (command.args or "").strip()
        if not arg.isdigit():
            await message.answer("Напишите номер заявки: /forget 12")
            return
        removed = await storage.forget(int(arg))
        await message.answer(f"Заявка № {arg} удалена." if removed else f"Заявки № {arg} нет.")

    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    return dispatcher
