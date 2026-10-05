"""Новости для пользователей бота: подписчики, выпуски и рассылка, которая не теряется при перезапуске."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import SQLiteStore, utc_now

log = logging.getLogger("site.news")

SEND_PAUSE = 0.05            # ~20 сообщений в секунду: у Telegram предел около 30
MAX_TITLE = 120
MAX_BODY = 3500              # у Telegram предел 4096 знаков, оставляем место под заголовок
MAX_CAPTION = 1000           # подпись к фото — до 1024 знаков

SCHEMA = """
CREATE TABLE IF NOT EXISTS subscribers (
    chat_id     INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    subscribed  INTEGER NOT NULL DEFAULT 1,     -- 0 = отписался или заблокировал бота
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS news (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    url         TEXT NOT NULL DEFAULT '',
    photo       TEXT NOT NULL DEFAULT '',       -- file_id фото в Telegram
    status      TEXT NOT NULL DEFAULT 'sending',-- sending → done
    sent        INTEGER NOT NULL DEFAULT 0,
    failed      INTEGER NOT NULL DEFAULT 0
);
-- кому выпуск уже ушёл: после перезапуска рассылка продолжается с того же места, без дублей
CREATE TABLE IF NOT EXISTS news_deliveries (
    news_id     INTEGER NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    chat_id     INTEGER NOT NULL,
    ok          INTEGER NOT NULL,
    PRIMARY KEY (news_id, chat_id)
);
"""


@dataclass(frozen=True)
class News:
    id: int
    created_at: str
    title: str
    body: str
    url: str
    photo: str
    status: str
    sent: int
    failed: int


def _row_to_news(row: sqlite3.Row) -> News:
    return News(id=row["id"], created_at=row["created_at"], title=row["title"], body=row["body"], url=row["url"],
                photo=row["photo"], status=row["status"], sent=row["sent"], failed=row["failed"])


def clean_url(url: str) -> str:
    url = url.strip()
    return url if url.startswith(("https://", "http://")) and len(url) <= 500 and " " not in url else ""


class NewsStore(SQLiteStore):
    SCHEMA = SCHEMA

    async def subscribe(self, chat_id: int, name: str = "") -> bool:
        """Подписывает. Возвращает True, если человек раньше подписан не был."""
        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT subscribed FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
            now = utc_now()
            if row is None:
                conn.execute("INSERT INTO subscribers (chat_id, name, subscribed, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                             (chat_id, name[:80], now, now))
                return True
            conn.execute("UPDATE subscribers SET subscribed = 1, updated_at = ? WHERE chat_id = ?", (now, chat_id))
            return not row["subscribed"]
        return await self._run(work)

    async def unsubscribe(self, chat_id: int) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            return conn.execute("UPDATE subscribers SET subscribed = 0, updated_at = ? WHERE chat_id = ? AND subscribed = 1",
                                (utc_now(), chat_id)).rowcount > 0
        return await self._run(work)

    async def is_subscribed(self, chat_id: int) -> bool:
        def work(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT subscribed FROM subscribers WHERE chat_id = ?", (chat_id,)).fetchone()
            return bool(row and row["subscribed"])
        return await self._run(work)

    async def counts(self) -> tuple[int, int]:
        """(подписаны сейчас, всего когда-либо запускали бота)."""
        def work(conn: sqlite3.Connection) -> tuple[int, int]:
            row = conn.execute("SELECT COALESCE(SUM(subscribed), 0) AS active, COUNT(*) AS total FROM subscribers").fetchone()
            return int(row["active"]), int(row["total"])
        return await self._run(work)

    async def add_news(self, title: str, body: str, url: str = "", photo: str = "") -> int:
        def work(conn: sqlite3.Connection) -> int:
            cursor = conn.execute("INSERT INTO news (created_at, title, body, url, photo) VALUES (?, ?, ?, ?, ?)",
                                  (utc_now(), title.strip()[:MAX_TITLE], body.strip()[:MAX_BODY], clean_url(url), photo))
            return int(cursor.lastrowid)
        return await self._run(work)

    async def get(self, news_id: int) -> News | None:
        def work(conn: sqlite3.Connection) -> News | None:
            row = conn.execute("SELECT * FROM news WHERE id = ?", (news_id,)).fetchone()
            return _row_to_news(row) if row else None
        return await self._run(work)

    async def latest(self, limit: int = 3) -> list[News]:
        def work(conn: sqlite3.Connection) -> list[News]:
            return [_row_to_news(r) for r in conn.execute("SELECT * FROM news ORDER BY id DESC LIMIT ?", (limit,))]
        return await self._run(work)

    async def sending(self) -> list[News]:
        def work(conn: sqlite3.Connection) -> list[News]:
            return [_row_to_news(r) for r in conn.execute("SELECT * FROM news WHERE status = 'sending' ORDER BY id")]
        return await self._run(work)

    async def recipients(self, news_id: int, limit: int = 200) -> list[int]:
        """Подписчики, которым этот выпуск ещё не отправлялся."""
        def work(conn: sqlite3.Connection) -> list[int]:
            rows = conn.execute(
                "SELECT s.chat_id FROM subscribers s WHERE s.subscribed = 1 AND NOT EXISTS "
                "(SELECT 1 FROM news_deliveries d WHERE d.news_id = ? AND d.chat_id = s.chat_id) ORDER BY s.chat_id LIMIT ?",
                (news_id, limit)).fetchall()
            return [int(r["chat_id"]) for r in rows]
        return await self._run(work)

    async def record(self, news_id: int, chat_id: int, ok: bool) -> None:
        def work(conn: sqlite3.Connection) -> None:
            if conn.execute("INSERT OR IGNORE INTO news_deliveries (news_id, chat_id, ok) VALUES (?, ?, ?)",
                            (news_id, chat_id, int(ok))).rowcount:
                conn.execute(f"UPDATE news SET {'sent' if ok else 'failed'} = {'sent' if ok else 'failed'} + 1 WHERE id = ?", (news_id,))
        await self._run(work)

    async def finish(self, news_id: int) -> None:
        await self._run(lambda conn: conn.execute("UPDATE news SET status = 'done' WHERE id = ?", (news_id,)))


def news_text(news: News, limit: int = MAX_BODY + MAX_TITLE + 10) -> str:
    text = f"📰 {news.title}\n\n{news.body}" if news.body else f"📰 {news.title}"
    return text[:limit]


def news_markup(news: News) -> InlineKeyboardMarkup | None:
    if not news.url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подробнее", url=news.url)]])


class Broadcaster:
    """Рассылает выпуски подписчикам. Заблокировавших бота отписывает, на «подождите» от Telegram — ждёт."""

    def __init__(self, store: NewsStore, bot: Bot | None, pause: float = SEND_PAUSE) -> None:
        self._store, self._bot, self._pause = store, bot, pause
        self._wake = asyncio.Event()

    def wake(self) -> None:
        self._wake.set()

    async def _send(self, chat_id: int, news: News) -> bool:
        assert self._bot is not None
        for _ in range(3):
            try:
                if news.photo:
                    await self._bot.send_photo(chat_id, news.photo, caption=news_text(news, MAX_CAPTION),
                                               reply_markup=news_markup(news))
                else:
                    await self._bot.send_message(chat_id, news_text(news), reply_markup=news_markup(news))
                return True
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 60))
            except TelegramForbiddenError:         # человек заблокировал бота — больше не пишем
                await self._store.unsubscribe(chat_id)
                return False
            except TelegramAPIError as exc:
                log.warning("выпуск %s не доставлен в чат %s: %s", news.id, chat_id, type(exc).__name__)
                return False
        return False

    async def deliver_pending(self) -> int:
        """Отправляет все незаконченные выпуски. Возвращает число доставленных сообщений."""
        if self._bot is None:
            return 0
        done = 0
        for news in await self._store.sending():
            while chat_ids := await self._store.recipients(news.id):
                for chat_id in chat_ids:
                    ok = await self._send(chat_id, news)
                    await self._store.record(news.id, chat_id, ok)
                    done += int(ok)
                    if self._pause:
                        await asyncio.sleep(self._pause)
            await self._store.finish(news.id)
        return done

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.deliver_pending()
            except Exception:
                log.exception("сбой при рассылке новостей")
            self._wake.clear()
            waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
            _, pending = await asyncio.wait(waiters, timeout=60, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
