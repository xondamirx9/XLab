"""Хранилище заявок в PostgreSQL. Заявка сначала сохраняется, потом пересылается: при сбое Telegram она не теряется."""
from __future__ import annotations

from typing import Any
from dataclasses import dataclass

from app.db import Conn, PgStore, utc_now

# Таблицы orders и events — в app/db.py (схема общая для всех хранилищ).


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


def _row_to_order(row: dict[str, Any]) -> Order:
    return Order(
        number=row["number"], client_id=row["client_id"], created_at=row["created_at"], name=row["name"],
        phone=row["phone"], telegram=row["telegram"], project=row["project"], estimate=row["estimate"],
        text=row["text"], delivered=bool(row["delivered"]), attempts=row["attempts"],
    )


class Storage(PgStore):
    async def add_order(self, *, client_id: str, name: str, phone: str, telegram: str, project: str,
                        estimate: str, text: str, brief_json: str) -> tuple[int, bool]:
        """Возвращает (номер заявки, создана ли новая). Повтор с тем же client_id новой заявки не создаёт."""
        def work(conn: Conn) -> tuple[int, bool]:
            found = conn.execute("SELECT number FROM orders WHERE client_id = %s", (client_id,)).fetchone()
            if found:                        # повтор той же заявки — номер прежний, а счётчик номеров не тратится
                return int(found["number"]), False
            # Два одинаковых запроса точно разом (двойное нажатие) не создадут двух заявок: второй упрётся в UNIQUE
            # и получит номер первой.
            created = conn.execute(
                "INSERT INTO orders (client_id, created_at, name, phone, telegram, project, estimate, text, brief_json) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (client_id) DO NOTHING RETURNING number",
                (client_id, utc_now(), name, phone, telegram, project,
                 estimate, text, brief_json),
            ).fetchone()
            if created:
                return int(created["number"]), True
            found = conn.execute("SELECT number FROM orders WHERE client_id = %s", (client_id,)).fetchone()
            assert found is not None
            return int(found["number"]), False
        return await self._run(work)

    async def pending(self, max_attempts: int, limit: int = 20) -> list[Order]:
        def work(conn: Conn) -> list[Order]:
            rows = conn.execute(
                "SELECT * FROM orders WHERE delivered = 0 AND attempts < %s ORDER BY number LIMIT %s", (max_attempts, limit)
            ).fetchall()
            return [_row_to_order(row) for row in rows]
        return await self._run(work)

    async def mark(self, number: int, delivered: bool) -> None:
        def work(conn: Conn) -> None:
            conn.execute("UPDATE orders SET delivered = %s, attempts = attempts + 1 WHERE number = %s", (int(delivered), number))
        await self._run(work)

    async def last(self, limit: int = 5) -> list[Order]:
        def work(conn: Conn) -> list[Order]:
            rows = conn.execute("SELECT * FROM orders ORDER BY number DESC LIMIT %s", (limit,)).fetchall()
            return [_row_to_order(row) for row in rows]
        return await self._run(work)

    async def get(self, number: int) -> Order | None:
        def work(conn: Conn) -> Order | None:
            row = conn.execute("SELECT * FROM orders WHERE number = %s", (number,)).fetchone()
            return _row_to_order(row) if row else None
        return await self._run(work)

    async def forget(self, number: int) -> bool:
        """Удаляет заявку вместе с контактами клиента (по его просьбе)."""
        def work(conn: Conn) -> bool:
            return conn.execute("DELETE FROM orders WHERE number = %s", (number,)).rowcount > 0
        return await self._run(work)

    async def purge_older(self, cutoff: str) -> int:
        """Удаляет заявки, полученные раньше cutoff (время UTC в формате ISO, как в created_at).
        Так выполняется срок хранения из политики конфиденциальности. Возвращает число удалённых."""
        def work(conn: Conn) -> int:
            return conn.execute("DELETE FROM orders WHERE created_at < %s", (cutoff,)).rowcount
        return await self._run(work)

    async def bump(self, name: str, day: str) -> None:
        """Прибавляет единицу к счётчику шага воронки за день."""
        def work(conn: Conn) -> None:
            conn.execute(
                "INSERT INTO events (day, name, count) VALUES (%s, %s, 1) "
                "ON CONFLICT (day, name) DO UPDATE SET count = events.count + 1", (day, name))
        await self._run(work)

    async def totals(self, since_day: str) -> dict[str, int]:
        """Суммы по шагам воронки начиная с дня since_day включительно."""
        def work(conn: Conn) -> dict[str, int]:
            rows = conn.execute("SELECT name, SUM(count) AS total FROM events WHERE day >= %s GROUP BY name", (since_day,)).fetchall()
            return {row["name"]: int(row["total"]) for row in rows}
        return await self._run(work)
