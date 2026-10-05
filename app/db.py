"""Общая основа для хранилищ на SQLite: запросы идут по одному и в отдельном потоке."""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteStore:
    SCHEMA = ""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    async def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        """Запросы идут по одному и в отдельном потоке, чтобы не тормозить сервер."""
        def call() -> T:
            conn = self._connect()
            try:
                with conn:
                    return work(conn)
            finally:
                conn.close()
        async with self._lock:
            return await asyncio.to_thread(call)

    async def init(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        await self._run(lambda conn: conn.executescript(self.SCHEMA))
