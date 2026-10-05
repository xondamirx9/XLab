"""Очередь студии: собирает сайты по одному (или по N), вносит правки клиентов и доставляет заявки с их сайтов."""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.studio.pipeline import Studio
from app.studio.store import Project, StudioStore

log = logging.getLogger("site.studio.worker")


def site_url(public_url: str, project: Project) -> str:
    return f"{public_url}/sites/{project.slug}/" if public_url else f"/sites/{project.slug}/"


def watch_url(public_url: str, project: Project) -> str:
    return f"{public_url}/studio/p/{project.token}" if public_url else f"/studio/p/{project.token}"


def done_markup(public_url: str, project: Project) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=f"{n}⭐", callback_data=f"rate:{project.id}:{n}") for n in range(1, 6)],
            [InlineKeyboardButton(text="✏️ Внести правки", callback_data=f"revise:{project.id}")]]
    if public_url.startswith("https://"):
        rows.insert(0, [InlineKeyboardButton(text="🌐 Открыть сайт", url=site_url(public_url, project)),
                        InlineKeyboardButton(text="🗣 Как спорил совет", url=watch_url(public_url, project))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class StudioWorker:
    def __init__(self, studio: Studio, store: StudioStore, bot: Bot | None, public_url: str = "", concurrency: int = 1) -> None:
        self.studio, self.store, self.bot = studio, store, bot
        self.public_url = public_url.rstrip("/")
        self.concurrency = max(1, concurrency)
        self._wake = asyncio.Event()
        studio.notify = self.progress

    def wake(self) -> None:
        self._wake.set()

    async def tell(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        if self.bot is None or not chat_id:
            return False
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=False)
            return True
        except TelegramAPIError as exc:
            log.warning("сообщение клиенту %s не доставлено: %s", chat_id, type(exc).__name__)
            return False

    async def progress(self, project: Project, kind: str, text: str) -> None:
        await self.tell(project.chat_id, f"«{project.name}»: {text}")

    async def process(self, project: Project) -> Project:
        """Один проход: новый сайт или правки к готовому."""
        try:
            if project.version == 0:
                result = await self.studio.create(project)
                text = (f"🎉 Сайт «{result.name}» готов!\n\n{site_url(self.public_url, result)}\n\n"
                        f"Над ним работали 20 агентов: спорили о концепции, ставили друг другу задачи и дважды проверяли результат. "
                        f"Оценка команды: {result.review:.1f}/10.\n\nОтзывы и цифры на сайте — примеры: пришлите реальные через «Внести правки». "
                        "Поставьте оценку — по ней команда учится и с каждым проектом работает лучше.")
            else:
                request = project.pending
                await self.store.update(project.id, pending="")
                result = await self.studio.revise(project, request)
                text = f"✅ Правки внесены, версия {result.version}:\n{site_url(self.public_url, result)}\n\nКак вам теперь?"
            await self.tell(result.chat_id, text, done_markup(self.public_url, result))
            return result
        except Exception as exc:
            log.exception("проект %s не собран", project.id)
            await self.store.update(project.id, status="done" if project.version else "failed", error=type(exc).__name__)
            await self.store.post(project.id, "общий", "producer", "system", f"Сбой при сборке: {type(exc).__name__}. Проект остановлен.")
            await self.tell(project.chat_id, f"Не получилось собрать «{project.name}» — мы уже разбираемся. Попробуйте позже.")
            failed = await self.store.get(project.id)
            return failed or project

    async def deliver_leads(self) -> int:
        done = 0
        for lead in await self.store.pending_leads():
            text = (f"📩 Заявка с вашего сайта ({lead['slug']})\nИмя: {lead['name']}\nТелефон: {lead['phone']}"
                    + (f"\nСообщение: {lead['message']}" if lead["message"] else ""))
            ok = await self.tell(lead["chat_id"], text)
            await self.store.mark_lead(lead["id"], ok)
            done += int(ok)
        return done

    async def _loop(self, stop: asyncio.Event, lane: int) -> None:
        while not stop.is_set():
            try:
                if lane == 0:
                    await self.deliver_leads()
                job = await self.store.next_job()
                if job is not None:
                    await self.process(job)
                    continue
            except Exception:
                log.exception("сбой очереди студии")
            self._wake.clear()
            waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
            _, pending = await asyncio.wait(waiters, timeout=30, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()

    async def run(self, stop: asyncio.Event) -> None:
        requeued = await self.store.requeue_running()
        if requeued:
            log.info("после перезапуска в очередь вернулось проектов: %s", requeued)
        await asyncio.gather(*(self._loop(stop, lane) for lane in range(self.concurrency)))
