"""Публикации сайта из админки: список, возврат к прежней и сброс к версии из git.

Запуск:  python -m app.site_versions                — последние публикации, новые сверху
         python -m app.site_versions --restore 12   — снова показывать публикацию №12 (запишется новой, история цела)
         python -m app.site_versions --reset        — убрать правки админки: тексты и цены снова как в git

Сайт видит перемену в течение нескольких секунд, перезапуск не нужен. Хранятся последние 20 публикаций.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys

from app.config import Config, ConfigError
from app.db import Database
from app.site_content import SiteStore, data_block


async def restore(store: SiteStore, publication_id: int) -> int:
    """Делает прежнюю публикацию снова текущей. Возвращает номер новой записи. LookupError — такой нет."""
    old = await store.get(publication_id)
    if old is None:
        raise LookupError(publication_id)
    return await store.add(data=old.data, base=old.base, html=old.html, template_sha=old.template_sha)


async def reset(store: SiteStore, page: str) -> int:
    """Запись «правок админки нет»: данные равны данным страницы из git, поэтому дальше всё берётся из git."""
    data = data_block(page) or {}
    return await store.add(data=data, base=data, html=None, template_sha=hashlib.sha256(page.encode("utf-8")).hexdigest())


async def _main(cfg: Config, args: argparse.Namespace) -> int:
    db = Database(cfg.database_url, max_size=1)
    await db.open()
    store = SiteStore(db)
    try:
        if args.restore is not None:
            try:
                number = await restore(store, args.restore)
            except LookupError:
                print(f"Публикации №{args.restore} нет: смотрите список без параметров.")
                return 1
            print(f"Готово: публикация №{args.restore} снова на сайте (записана как №{number}).")
        elif args.reset:
            try:
                page = cfg.index_file.read_text(encoding="utf-8")
            except OSError:
                print(f"Нет файла {cfg.index_file} — сбрасывать не к чему.")
                return 1
            number = await reset(store, page)
            print(f"Готово: правки админки убраны, тексты и цены — как в git (запись №{number}).")
        else:
            rows = await store.history()
            if not rows:
                print("Публикаций из админки нет: сайт показывает страницу из git.")
            for row in rows:
                kind = "страница целиком" if row["whole"] else "только тексты и цены"
                print(f"№{row['id']}  {row['created_at']}  {kind}, {row['size']} знаков")
    finally:
        await db.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.site_versions", description="Публикации сайта из админки.")
    parser.add_argument("--restore", type=int, metavar="НОМЕР", help="снова показывать публикацию с этим номером")
    parser.add_argument("--reset", action="store_true", help="убрать правки админки: тексты и цены как в git")
    args = parser.parse_args()
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        sys.exit(f"Ошибка в настройках (.env): {exc}")
    if not cfg.database_url:
        sys.exit("Ошибка в настройках (.env): не задан DATABASE_URL.")
    sys.exit(asyncio.run(_main(cfg, args)))


if __name__ == "__main__":
    main()
