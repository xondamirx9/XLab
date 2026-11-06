"""Тестовая база PostgreSQL для проверок.

Адрес — TEST_DATABASE_URL, по умолчанию postgresql://codexlab:codexlab@localhost:5432/codexlab_test.
Каждая проверка начинает с пустых таблиц, поэтому в имени базы обязательно слово «test»: так проверки
не сотрут настоящую базу, даже если адрес перепутали.
"""
from __future__ import annotations

import os
import unittest

from psycopg.conninfo import conninfo_to_dict

from app.db import Conn, Database

DEFAULT_URL = "postgresql://codexlab:codexlab@localhost:5432/codexlab_test"


def test_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL", "").strip() or DEFAULT_URL
    name = str(conninfo_to_dict(url).get("dbname", ""))
    if "test" not in name:
        raise RuntimeError(f"TEST_DATABASE_URL указывает на базу «{name}»: в имени тестовой базы должно быть слово test. "
                           "Проверки очищают таблицы — на настоящей базе они стёрли бы данные.")
    return url


def _empty_tables(conn: Conn) -> None:
    tables = [row["tablename"] for row in conn.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")]
    if tables:
        conn.execute("TRUNCATE " + ", ".join(f'"{name}"' for name in tables) + " RESTART IDENTITY CASCADE")


async def fresh_database() -> Database:
    """Открытая база с последней схемой и пустыми таблицами. После проверки — await db.close()."""
    db = Database(test_url(), max_size=4)
    await db.open()
    await db.run(_empty_tables)
    return db


async def database_for(case: unittest.IsolatedAsyncioTestCase) -> Database:
    """Одна чистая база на проверку: хранилища одной проверки видят одни и те же данные. Закрывается сама."""
    db = getattr(case, "_pgdb", None)
    if db is None:
        db = await fresh_database()
        case.addAsyncCleanup(db.close)
        setattr(case, "_pgdb", db)
    return db
