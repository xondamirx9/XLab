"""Проверки базы PostgreSQL: схема, одновременные запросы, перенос из SQLite.

Нужна тестовая база — см. tests/pgdb.py и README, «Проверки для разработчика».
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app.db import MIGRATIONS, Database, utc_now
from app.import_sqlite import ImportRefused, import_folder
from app.news import NewsStore
from app.storage import Storage
from app.studio.store import StudioStore
from pgdb import database_for, test_url

BRIEF = {"business": "Пекарня", "audience": "соседи"}
SINCE = "2000-01-01T00:00:00+00:00"


def slow_clock() -> str:
    """Часы с задержкой: между чтением и записью в транзакции появляется окно, и одновременные запросы
    гарантированно пересекаются. Без защиты в коде такие проверки падают."""
    time.sleep(0.3)
    return utc_now()


class GuardTest(unittest.TestCase):
    def test_refuses_database_without_test_in_name(self) -> None:
        with mock.patch.dict(os.environ, {"TEST_DATABASE_URL": "postgresql://u:p@localhost/codexlab"}):
            with self.assertRaises(RuntimeError):
                test_url()
        with mock.patch.dict(os.environ, {"TEST_DATABASE_URL": "postgresql://u:p@localhost/codexlab_test"}):
            self.assertTrue(test_url().endswith("/codexlab_test"))


class SchemaCase(unittest.IsolatedAsyncioTestCase):
    async def test_migrations_run_once(self) -> None:
        db = await database_for(self)
        again = Database(test_url(), max_size=1)
        self.assertEqual(await again.open(), [])                       # всё уже сделано — второй запуск ничего не меняет
        await again.close()
        versions = await db.run(lambda conn: [row["version"] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")])
        self.assertEqual(versions, [version for version, _ in MIGRATIONS])

    async def test_time_columns_compare_bytewise(self) -> None:
        db = await database_for(self)
        rows = await db.run(lambda conn: conn.execute(
            "SELECT table_name, column_name, collation_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND column_name IN ('created_at', 'updated_at', 'done_at', 'day')").fetchall())
        self.assertTrue(rows)
        self.assertEqual({row["collation_name"] for row in rows}, {"C"}, rows)   # «раньше / позже» — как в SQLite


class ConcurrencyCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        db = await database_for(self)
        self.storage, self.news, self.studio = Storage(db), NewsStore(db), StudioStore(db)

    def slow(self) -> None:
        for target in ("app.storage.utc_now", "app.studio.store.utc_now"):
            patcher = mock.patch(target, slow_clock)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_same_order_twice_at_once_is_one_order(self) -> None:
        fields = dict(client_id="order-race1", name="Али", phone="+998901234567", telegram="", project="Лендинг",
                      estimate="от 3 млн", text="ТЗ", brief_json="{}")
        self.slow()
        results = await asyncio.gather(*(self.storage.add_order(**fields) for _ in range(4)))
        self.assertEqual({number for number, _ in results}, {1})
        self.assertEqual(sum(created for _, created in results), 1)
        self.assertEqual(len(await self.storage.last(10)), 1)

    async def test_daily_limit_holds_under_fast_clicks(self) -> None:
        self.slow()
        results = await asyncio.gather(*(self.studio.create_within_limit(BRIEF, f"s{i}", 42, SINCE, 2) for i in range(4)))
        self.assertEqual(sum(project is not None for project in results), 2)
        self.assertEqual(await self.studio.count_since(42, SINCE), 2)

    async def test_two_workers_never_take_one_project(self) -> None:
        await self.studio.create_project(BRIEF, "only")
        self.slow()
        taken = await asyncio.gather(self.studio.next_job(), self.studio.next_job())
        self.assertEqual(sum(job is not None for job in taken), 1)

    async def test_double_rating_counts_once(self) -> None:
        project = await self.studio.create_project(BRIEF, "rated", chat_id=5)
        self.slow()
        await asyncio.gather(self.studio.rate(project.id, 5, ["critic"]), self.studio.rate(project.id, 4, ["critic"]))
        self.assertEqual((await self.studio.agent_stats())["critic"]["score_n"], 1)

    async def test_group_chat_ids_fit(self) -> None:
        group = -1001234567890                                          # у групп Telegram номер длиннее 32 бит
        self.assertTrue(await self.news.subscribe(group, "Группа"))
        self.assertFalse(await self.news.subscribe(group))
        news_id = await self.news.add_news("Новость", "Текст")
        self.assertEqual(await self.news.recipients(news_id), [group])
        project = await self.studio.create_project(BRIEF, "group", chat_id=group)
        self.assertEqual((await self.studio.get(project.id)).chat_id, group)


class ImportCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = await database_for(self)

    def _old_files(self) -> None:
        """Файлы SQLite в том виде, в каком их писал прежний сервер."""
        with sqlite3.connect(self.root / "orders.sqlite3") as conn:
            conn.execute("CREATE TABLE orders (number INTEGER PRIMARY KEY AUTOINCREMENT, client_id TEXT NOT NULL UNIQUE, "
                         "created_at TEXT NOT NULL, name TEXT NOT NULL, phone TEXT NOT NULL, telegram TEXT NOT NULL, "
                         "project TEXT NOT NULL, estimate TEXT NOT NULL, text TEXT NOT NULL, brief_json TEXT NOT NULL, "
                         "delivered INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0)")
            conn.execute("INSERT INTO orders VALUES (5, 'order-old', '2026-10-01T10:00:00+00:00', 'Али', '+998901112233', '', "
                         "'Лендинг', 'от 3 млн', 'ТЗ', '{}', 1, 1)")
            conn.execute("CREATE TABLE events (day TEXT NOT NULL, name TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0, "
                         "PRIMARY KEY (day, name))")
            conn.execute("INSERT INTO events VALUES ('2026-10-01', 'visit', 12)")
        with sqlite3.connect(self.root / "news.sqlite3") as conn:
            conn.execute("CREATE TABLE subscribers (chat_id INTEGER PRIMARY KEY, name TEXT NOT NULL DEFAULT '', "
                         "subscribed INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute("INSERT INTO subscribers VALUES (-1009876543210, 'Группа', 1, '2026-10-01', '2026-10-01')")
        for name in ("orders.sqlite3", "news.sqlite3"):
            sqlite3.connect(self.root / name).close()

    async def test_import_keeps_numbers_and_continues_after_them(self) -> None:
        self._old_files()
        counts = await import_folder(self.db, self.root)
        self.assertEqual(counts, {"orders": 1, "events": 1, "subscribers": 1})
        storage, news = Storage(self.db), NewsStore(self.db)
        self.assertEqual((await storage.get(5)).name, "Али")
        self.assertEqual(await storage.totals("2026-10-01"), {"visit": 12})
        self.assertTrue(await news.is_subscribed(-1009876543210))
        number, created = await storage.add_order(client_id="order-new", name="Вали", phone="+998907778899", telegram="",
                                                  project="Магазин", estimate="от 9 млн", text="ТЗ", brief_json="{}")
        self.assertEqual((number, created), (6, True))                  # номера заявок продолжаются, а не начинаются с 1

    async def test_import_refuses_folder_without_files(self) -> None:
        with self.assertRaises(ImportRefused):
            await import_folder(self.db, self.root)


if __name__ == "__main__":
    unittest.main()
