"""Запуск: сайт, приём заявок и бот работают в одном процессе."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

from app.bot import Notifier, build_dispatcher
from app.config import Config, ConfigError
from app.storage import Storage
from app.web import create_app

log = logging.getLogger("site")


async def run(cfg: Config) -> None:
    storage = Storage(cfg.data_dir / "orders.sqlite3")
    await storage.init()

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=None)) if cfg.bot_token else None
    notifier = Notifier(cfg, storage, bot)
    app = create_app(cfg, storage, on_new_order=notifier.wake, bot_enabled=bot is not None)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    log.info("сайт запущен: http://%s:%s", cfg.host, cfg.port)
    if bot is None:
        log.warning("BOT_TOKEN не задан: заявки сохраняются, но в Telegram не пересылаются")
    elif not cfg.admin_chat_ids:
        log.warning("ADMIN_CHAT_IDS пуст: напишите боту /start, он покажет номер чата")
    if not cfg.publish_enabled:
        log.warning("ADMIN_PASSWORD не задан: публикация из админки выключена")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:          # Windows: остановка по Ctrl+C через KeyboardInterrupt
            pass

    tasks = [asyncio.create_task(notifier.run(stop))]
    dispatcher = None
    if bot is not None:
        dispatcher = build_dispatcher(cfg, storage)
        tasks.append(asyncio.create_task(dispatcher.start_polling(bot, handle_signals=False)))

    try:
        await stop.wait()
    finally:
        log.info("останавливаемся")
        stop.set()
        if dispatcher is not None:
            try:
                await dispatcher.stop_polling()
            except RuntimeError:             # опрос ещё не успел начаться
                pass
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        if bot is not None:
            await bot.session.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        sys.exit(f"Ошибка в настройках (.env): {exc}")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
