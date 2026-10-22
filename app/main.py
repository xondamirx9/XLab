"""Запуск: сайт, заявки, бот, новости и студия из 20 агентов работают в одном процессе."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import web

from app.bot import Notifier, build_dispatcher
from app.config import Config, ConfigError
from app.news import Broadcaster, NewsStore
from app.rates import RateService
from app.storage import Storage
from app.studio.llm import LLMFactory
from app.studio.pipeline import Studio
from app.studio.store import StudioStore
from app.studio.worker import StudioWorker
from app.web import StudioContext, create_app

log = logging.getLogger("site")


async def run(cfg: Config) -> None:
    storage = Storage(cfg.data_dir / "orders.sqlite3")
    await storage.init()

    news = NewsStore(cfg.data_dir / "news.sqlite3")
    await news.init()
    rates = RateService(cfg.data_dir / "rate.json")     # курс доллара NBU для цен в USD на сайте и в боте

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=None)) if cfg.bot_token else None
    notifier = Notifier(cfg, storage, bot)
    broadcaster = Broadcaster(news, bot)

    studio_ctx = worker = None
    studio_store: StudioStore | None = None
    studio: Studio | None = None
    if cfg.studio_enabled:
        studio_store = StudioStore(cfg.data_dir / "studio.sqlite3")
        await studio_store.init()
        sites_dir = cfg.data_dir / "sites"
        llm = LLMFactory(model=cfg.studio_model, max_calls=cfg.studio_max_calls, enabled=cfg.claude_enabled)
        studio = Studio(studio_store, llm, sites_dir, debate_rounds=cfg.debate_rounds, trusted_chats=cfg.admin_chat_ids)
        worker = StudioWorker(studio, studio_store, bot, cfg.public_url, cfg.studio_concurrency)
        studio_ctx = StudioContext(store=studio_store, studio=studio, news=news, sites_dir=sites_dir, wake_worker=worker.wake,
                                   wake_news=broadcaster.wake, claude=cfg.claude_enabled)
        log.info("студия: %s", f"агенты думают через {cfg.studio_model}" if cfg.claude_enabled
                 else "офлайн-режим (нет ANTHROPIC_API_KEY): сайты собираются без нейросети")
    app = create_app(cfg, storage, on_new_order=notifier.wake, bot_enabled=bot is not None, studio=studio_ctx, rates=rates)

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

    tasks = [asyncio.create_task(notifier.run(stop)), asyncio.create_task(broadcaster.run(stop))]
    if worker is not None:
        tasks.append(asyncio.create_task(worker.run(stop)))
    dispatcher = None
    if bot is not None:
        dispatcher = build_dispatcher(cfg, storage, news=news, studio_store=studio_store, studio=studio,
                                      wake_news=broadcaster.wake, wake_worker=worker.wake if worker else (lambda: None),
                                      rates=rates, wake_orders=notifier.wake)
        await set_commands(bot, studio=studio_store is not None)
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


async def set_commands(bot: Bot, *, studio: bool) -> None:
    """Меню команд в Telegram на трёх языках. Не получилось — бот всё равно работает, просто без подсказок."""
    menu = {
        None: [("start", "Главное меню"), ("prices", "Тарифы и цены"), ("brief", "Собрать ТЗ с ценой"), ("lang", "Язык")],
        "uz": [("start", "Bosh menyu"), ("prices", "Tariflar va narxlar"), ("brief", "Narxi bilan TZ tuzish"), ("lang", "Til")],
        "en": [("start", "Main menu"), ("prices", "Pricing"), ("brief", "Build a brief with a price"), ("lang", "Language")],
    }
    if studio:
        menu[None] += [("site", "Сайт от ИИ-агентов"), ("mysites", "Мои сайты")]
    try:
        for language, items in menu.items():
            await bot.set_my_commands([BotCommand(command=c, description=d) for c, d in items], language_code=language)
    except TelegramAPIError as exc:
        log.warning("меню команд не обновлено: %s", type(exc).__name__)


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
