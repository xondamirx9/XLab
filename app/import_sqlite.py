"""Перенос данных из прежних файлов SQLite в PostgreSQL.

До перехода на PostgreSQL сервер хранил данные в папке DATA_DIR: orders.sqlite3 (заявки), news.sqlite3
(подписчики и новости), studio.sqlite3 (проекты студии, уроки агентов, заявки с сайтов клиентов).
Команда переносит их в базу из DATABASE_URL — один раз, одной транзакцией: не получилось — в базе ничего не
меняется. Номера заявок и проектов сохраняются. В базе, где уже есть данные, команда ничего не делает.

Запуск:  python -m app.import_sqlite            — файлы из DATA_DIR
         python -m app.import_sqlite папка      — файлы из другой папки

На экран выводятся только числа строк — без имён и телефонов.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

from app.config import Config, ConfigError
from app.db import Conn, Database

# Файл → таблицы в порядке, в котором их можно заливать: сначала те, на кого ссылаются.
FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("orders.sqlite3", ("orders", "events")),
    ("news.sqlite3", ("subscribers", "news", "news_deliveries")),
    ("studio.sqlite3", ("projects", "messages", "tasks", "lessons", "lesson_uses", "agent_stats", "leads")),
)
# Таблицы со сквозным номером: после переноса счётчик продолжается за последним перенесённым номером.
IDENTITY = {"orders": "number", "news": "id", "projects": "id", "messages": "id", "tasks": "id", "lessons": "id",
            "leads": "id"}


class ImportRefused(Exception):
    """Перенос не выполнен: текст можно показать человеку как есть."""


def _sqlite_rows(path: Path, table: str) -> tuple[list[str], list[tuple]]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)       # только чтение: старый файл не меняется
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone() is None:
            return [], []
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        return columns, conn.execute(f'SELECT {", ".join(columns)} FROM "{table}"').fetchall()
    finally:
        conn.close()


def _pg_columns(conn: Conn, table: str) -> list[str]:
    return [row["column_name"] for row in conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = %s "
        "ORDER BY ordinal_position", (table,))]


def _copy(conn: Conn, folder: Path) -> dict[str, int]:
    tables = [table for _, names in FILES for table in names]
    busy = [table for table in tables if conn.execute(f'SELECT 1 FROM "{table}" LIMIT 1').fetchone()]
    if busy:
        raise ImportRefused(f"в базе уже есть данные (таблицы: {', '.join(busy)}) — перенос делается только в пустую базу.")

    counts: dict[str, int] = {}
    for filename, names in FILES:
        path = folder / filename
        if not path.is_file():
            continue
        for table in names:
            columns, rows = _sqlite_rows(path, table)
            if not rows:
                continue
            # Столбцы, которых нет в старом файле (например, approved у ранних уроков), берут значение по умолчанию.
            known = set(_pg_columns(conn, table))
            common = [column for column in columns if column in known]
            picks = [columns.index(column) for column in common]
            marks = ", ".join(["%s"] * len(common))
            with conn.cursor() as cur:
                cur.executemany(f'INSERT INTO "{table}" ({", ".join(common)}) VALUES ({marks})',
                                [tuple(row[i] for i in picks) for row in rows])
            counts[table] = len(rows)
    for table, column in IDENTITY.items():
        conn.execute(f"SELECT setval(pg_get_serial_sequence(%s, %s), COALESCE(MAX({column}), 0) + 1, false) FROM \"{table}\"",
                     (table, column))
    return counts


async def import_folder(db: Database, folder: Path) -> dict[str, int]:
    """Переносит файлы SQLite из folder в базу. Возвращает число перенесённых строк по таблицам."""
    if not any((folder / filename).is_file() for filename, _ in FILES):
        raise ImportRefused(f"в папке {folder} нет файлов orders.sqlite3, news.sqlite3 и studio.sqlite3 — переносить нечего.")
    return await db.run(lambda conn: _copy(conn, folder))


async def _main(cfg: Config, folder: Path) -> int:
    db = Database(cfg.database_url, max_size=1)
    await db.open()
    try:
        counts = await import_folder(db, folder)
    except ImportRefused as exc:
        print(f"Перенос не выполнен: {exc}")
        return 1
    finally:
        await db.close()
    for table, count in counts.items():
        print(f"{table}: {count}")
    print("Готово: данные перенесены. Старые файлы не тронуты — удалите их, когда проверите сайт и бота.")
    return 0


def main() -> None:
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        sys.exit(f"Ошибка в настройках (.env): {exc}")
    if not cfg.database_url:
        sys.exit("Ошибка в настройках (.env): не задан DATABASE_URL — куда переносить.")
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.data_dir
    sys.exit(asyncio.run(_main(cfg, folder.resolve())))


if __name__ == "__main__":
    main()
