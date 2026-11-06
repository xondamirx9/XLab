"""Запуск: сайт, заявки, бот, новости и студия из 20 агентов; данные — в PostgreSQL.

ROLE=all — всё в одном процессе; на двух машинах — web (посетители) и admin (админка, бот, фоновое).
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
from typing import Callable

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand
from aiohttp import web
from psycopg_pool import PoolTimeout

from app.bot import Notifier, build_dispatcher
from app.config import Config, ConfigError
from app.db import Database, Listener
from app.indexnow import IndexNow
from app.news import Broadcaster, NewsStore
from app.rates import RateService
from app.site_content import SiteContent, SiteStore
from app.storage import Storage
from app.studio.llm import LLMFactory
from app.studio.pipeline import Studio
from app.studio.store import StudioStore
from app.studio.worker import StudioWorker
from app.web import StudioContext, create_app

log = logging.getLogger("site")


class SchemaNotReady(Exception):
    """Машина сайта не дождалась, пока машина админки обновит схему базы."""


async def run(cfg: Config) -> None:
    db = Database(cfg.database_url)
    if cfg.role == "web":
        await db.open(migrate=False)         # схему меняет только машина админки — ждём её
        if not await db.wait_schema():
            await db.close()
            raise SchemaNotReady()
    else:
        applied = await db.open()            # схема базы — до последней версии
        if applied:
            log.info("схема базы обновлена: шаги %s", ", ".join(map(str, applied)))
    try:
        await serve(cfg, db)
    finally:
        await db.close()


async def serve(cfg: Config, db: Database) -> None:
    # Фоновое — бот, доставка заявок, рассылки, студия, IndexNow — работает в ролях all и admin. Роль web только
    # отвечает посетителям, а о новой заявке или правке сайта клиента сообщает машине админки через базу (NOTIFY).
    background = cfg.role in ("all", "admin")
    loop = asyncio.get_running_loop()
    sending: set[asyncio.Task[None]] = set()

    def emit(event: str) -> Callable[[], None]:
        def send() -> None:
            async def go() -> None:
                try:
                    await db.notify(event)
                except Exception as exc:     # не дошло — машина админки заберёт при обычном опросе
                    log.warning("событие %s не отправлено: %s", event, type(exc).__name__)
            task = loop.create_task(go())
            sending.add(task)
            task.add_done_callback(sending.discard)
        return send

    storage = Storage(db)
    news = NewsStore(db)
    site = SiteContent(cfg.index_file, SiteStore(db))  # вёрстка из site/index.html, тексты и цены — из базы
    rates = RateService(cfg.data_dir / "rate.json")     # курс доллара NBU для цен в USD на сайте и в боте

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=None)) if cfg.bot_token and background else None
    notifier = Notifier(cfg, storage, bot)
    broadcaster = Broadcaster(news, bot)

    studio_ctx = worker = None
    studio_store: StudioStore | None = None
    studio: Studio | None = None
    if cfg.studio_enabled:
        studio_store = StudioStore(db)
        llm = LLMFactory(model=cfg.studio_model, max_calls=cfg.studio_max_calls, enabled=cfg.claude_enabled)
        studio = Studio(studio_store, llm, debate_rounds=cfg.debate_rounds, trusted_chats=cfg.admin_users)
        worker = StudioWorker(studio, studio_store, bot, cfg.public_url, cfg.studio_concurrency)
        studio_ctx = StudioContext(store=studio_store, studio=studio, news=news,
                                   wake_worker=worker.wake if background else emit("studio"),
                                   wake_news=broadcaster.wake, claude=cfg.claude_enabled)
        if background:
            log.info("студия: %s", f"агенты думают через {cfg.studio_model}" if cfg.claude_enabled
                     else "офлайн-режим (нет ANTHROPIC_API_KEY): сайты собираются без нейросети")
    indexnow = IndexNow(cfg, site=site)   # сообщать Bing и Яндексу о новой версии сайта
    app = create_app(cfg, storage, on_new_order=notifier.wake if background else emit("order"), bot_enabled=bot is not None,
                     studio=studio_ctx, rates=rates, on_site_changed=indexnow.wake, on_admin_action=notifier.alert, site=site)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg.host, cfg.port).start()
    log.info("сайт запущен (роль %s): http://%s:%s", cfg.role, cfg.host, cfg.port)
    if background and bot is None:
        log.warning("BOT_TOKEN не задан: заявки сохраняются, но в Telegram не пересылаются")
    elif bot is not None and not cfg.admin_chat_ids:
        log.warning("ADMIN_CHAT_IDS пуст: напишите боту /start, он покажет номер чата")
    if any(chat < 0 for chat in cfg.admin_chat_ids) and not cfg.admin_users:
        log.warning("в ADMIN_CHAT_IDS есть группа, а ADMIN_USER_IDS пуст: заявки в группу придут, но управлять ботом из неё никто не сможет")
    if cfg.role != "web" and not cfg.publish_enabled:
        log.warning("ADMIN_PASSWORD не задан: публикация из админки выключена")
    if cfg.cloud_proxy:
        log.info("адрес посетителя берём из X-Forwarded-For прокси облачной платформы (TRUST_PROXY=cloud)")

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:          # Windows: остановка по Ctrl+C через KeyboardInterrupt
            pass

    tasks: list[asyncio.Task[object]] = []
    dispatcher = None
    if background:
        tasks += [asyncio.create_task(notifier.run(stop)), asyncio.create_task(broadcaster.run(stop)),
                  asyncio.create_task(indexnow.run(stop))]
        if worker is not None:
            tasks.append(asyncio.create_task(worker.run(stop)))
    if cfg.role == "admin":                  # заявки и правки, принятые машиной сайта, — сразу в работу
        handlers = {"order": notifier.wake, "studio": worker.wake if worker else (lambda: None)}
        tasks.append(asyncio.create_task(Listener(cfg.database_url, handlers).run(stop)))
    if bot is not None:
        dispatcher = build_dispatcher(cfg, storage, news=news, studio_store=studio_store, studio=studio,
                                      wake_news=broadcaster.wake, wake_worker=worker.wake if worker else (lambda: None),
                                      rates=rates, wake_orders=notifier.wake, site=site)
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
        await asyncio.gather(*tasks, *sending, return_exceptions=True)
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
    if not cfg.database_url:
        sys.exit("Ошибка в настройках (.env): не задан DATABASE_URL — адрес базы PostgreSQL, например "
                 "postgresql://codexlab:пароль@localhost:5432/codexlab. Как завести базу — README, шаг 2.")
    try:
        asyncio.run(run(cfg))
    except KeyboardInterrupt:
        pass
    except SchemaNotReady:
        sys.exit("Схема базы старее этой версии сайта, а машина админки за 2 минуты её не обновила. Сначала выкладывается "
                 "машина админки (она меняет схему), потом машина сайта — см. deploy/uzcloud/README.md.")
    except PoolTimeout:
        sys.exit("Не удалось подключиться к базе из DATABASE_URL за 30 секунд: проверьте адрес, пользователя и пароль "
                 "и что PostgreSQL запущен (sudo systemctl status postgresql).")


if __name__ == "__main__":
    main()
