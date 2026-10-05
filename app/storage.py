"""Хранилище заявок в SQLite. Заявка сначала сохраняется, потом пересылается: при сбое Telegram она не теряется."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.db import SQLiteStore, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    number      INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   TEXT NOT NULL UNIQUE,          -- номер, который сайт присваивает заявке: защита от дублей
    created_at  TEXT NOT NULL,                 -- время в UTC
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    telegram    TEXT NOT NULL,
    project     TEXT NOT NULL,
    estimate    TEXT NOT NULL,
    text        TEXT NOT NULL,                 -- готовое ТЗ
    brief_json  TEXT NOT NULL,                 -- ответы клиента как есть
    delivered   INTEGER NOT NULL DEFAULT 0,    -- 1 = переслано в Telegram
    attempts    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS orders_pending ON orders (delivered, number);
CREATE TABLE IF NOT EXISTS events (
    day    TEXT NOT NULL,                      -- дата по Ташкенту, ГГГГ-ММ-ДД
    name   TEXT NOT NULL,                      -- шаг воронки
    count  INTEGER NOT NULL DEFAULT 0,         -- только число: без имён, адресов и устройств
    PRIMARY KEY (day, name)
);
"""


@dataclass(frozen=True)
class Order:
    number: int
    client_id: str
    created_at: str
    name: str
    phone: str
    telegram: str
    project: str
    estimate: str
    text: str
    delivered: bool
    attempts: int


def _row_to_order(row: sqlite3.Row) -> Order:
    return Order(
        number=row["number"], client_id=row["client_id"], created_at=row["created_at"], name=row["name"],
        phone=row["phone"], telegram=row["telegram"], project=row["project"], estimate=row["estimate"],
        text=row["text"], delivered=bool(row["delivered"]), attempts=row["attempts"],
    )


class Storage(SQLiteStore):
    SCHEMA = SCHEMA

    async def add_order(self, *, client_id: str, name: str, phone: str, telegram: str, project: str,
                        estimate: str, text: str, brief_json: str) -> tuple[int, bool]:
        """Возвращает (номер заявки, создана ли новая). Повтор с тем же client_id новой заявки не создаёт."""
        def work(conn: sqlite3.Connection) -> tuple[int, bool]:
            found = conn.execute("SELECT number FROM orders WHERE client_id = ?", (client_id,)).fetchone()
            if found:
                return found["number"], False
            cursor = conn.execute(
                "INSERT INTO orders (client_id, created_at, name, phone, telegram, project, estimate, text, brief_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (client_id, utc_now(), name, phone, telegram, project,
                 estimate, text, brief_json),
            )
            return int(cursor.lastrowid), True
        return await self._run(work)

    async def pending(self, max_attempts: int, limit: int = 20) -> list[Order]:
        def work(conn: sqlite3.Connection) -> list[Order]:
            rows = conn.execute(
                "SELECT * FROM orders WHERE delivered = 0 AND attempts < ? ORDER BY number LIMIT ?", (max_attempts, limit)
            ).fetchall()
            return [_row_to_order(row) for row in rows]
        return await self._run(work)

    async def mark(self, number: int, delivered: bool) -> None:
        def work(conn: sqlite3.Connection) -> None:
            conn.execute("UPDATE orders SET delivered = ?, attempts = attempts + 1 WHERE number = ?", (int(delivered), number))
        await self._run(work)

    async def last(self, limit: int = 5) -> list[Order]:
        def work(conn: sqlite3.Connection) -> list[Order]:
            rows = conn.execute("SELECT * FROM orders ORDER BY number DESC LIMIT ?", (limit,)).fetchall()
            return [_row_to_order(row) for row in rows]
        return await self._run(work)

    async def get(self, number: int) -> Order | None:
        def work(conn: sqlite3.Connection) -> Order | None:
            row = conn.execute("SELECT * FROM orders WHERE number = ?", (number,)).fetchone()
            return _row_to_order(row) if row else None
        return await self._run(work)

    async def forget(self, number: int) -> bool:
        """Удаляет заявку вместе с контактами клиента (по его просьбе)."""
        def work(conn: sqlite3.Connection) -> bool:
            return conn.execute("DELETE FROM orders WHERE number = ?", (number,)).rowcount > 0
        return await self._run(work)

    async def bump(self, name: str, day: str) -> None:
        """Прибавляет единицу к счётчику шага воронки за день."""
        def work(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO events (day, name, count) VALUES (?, ?, 1) "
                "ON CONFLICT (day, name) DO UPDATE SET count = count + 1", (day, name))
        await self._run(work)

    async def totals(self, since_day: str) -> dict[str, int]:
        """Суммы по шагам воронки начиная с дня since_day включительно."""
        def work(conn: sqlite3.Connection) -> dict[str, int]:
            rows = conn.execute("SELECT name, SUM(count) AS total FROM events WHERE day >= ? GROUP BY name", (since_day,)).fetchall()
            return {row["name"]: int(row["total"]) for row in rows}
        return await self._run(work)
