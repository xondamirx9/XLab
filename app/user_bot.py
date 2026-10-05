"""Бот для пользователей: подписка на новости и заказ собственного сайта у совета из 20 агентов."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Callable

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import Config
from app.news import NewsStore, news_markup, news_text
from app.studio.pipeline import Studio
from app.studio.spec import PRESETS, slugify
from app.studio.store import StudioStore
from app.studio.worker import site_url, watch_url

LANG_TITLES = {"ru": "🇷🇺 Русский", "uz": "🇺🇿 O‘zbekcha", "en": "🇬🇧 English"}
MAX_REVISIONS = 10


class Order(StatesGroup):
    business = State()
    niche = State()
    usp = State()
    style = State()
    style_text = State()
    colors = State()
    contacts = State()
    lang = State()
    confirm = State()
    revise = State()


def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])


def welcome_markup() -> InlineKeyboardMarkup:
    return kb([[("🎨 Создать свой сайт", "site:new")], [("📰 Последние новости", "news:latest"), ("🗂 Мои сайты", "site:mine")]])


def style_markup() -> InlineKeyboardMarkup:
    items = [(p["title"], f"style:{key}") for key, p in PRESETS.items()]
    rows = [items[i:i + 2] for i in range(0, len(items), 2)]
    rows.append([("✍️ Опишу словами", "style:custom")])
    return kb(rows)


def parse_contacts(text: str) -> dict[str, str]:
    """Телефон, Telegram, Instagram, почта и адрес из одного сообщения в свободной форме."""
    out = {"phone": "", "telegram": "", "instagram": "", "email": "", "address": "", "hours": ""}
    rest = text
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", rest):
        out["email"] = m.group(0)
        rest = rest.replace(m.group(0), " ")
    if m := re.search(r"(?:instagram\.com/|инстаграм\s*:?\s*@?|instagram\s*:?\s*@?|insta\s*:?\s*@?)([A-Za-z0-9_.]{2,30})", rest, re.I):
        out["instagram"] = m.group(1)
        rest = rest.replace(m.group(0), " ")
    if m := re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,31})", rest):
        out["telegram"] = m.group(1)
        rest = rest.replace(m.group(0), " ")
    if m := re.search(r"\+?\d[\d\s()-]{7,}\d", rest):
        out["phone"] = " ".join(m.group(0).split())
        rest = rest.replace(m.group(0), " ")
    if m := re.search(r"(\d{1,2}[:.]\d{2}\s*[–-]\s*\d{1,2}[:.]\d{2})", rest):
        out["hours"] = m.group(1)
        rest = rest.replace(m.group(0), " ")
    address = " ".join(re.sub(r"(?i)(телефон|тел\.?|telegram|телеграм|адрес|instagram|инстаграм)\s*:?", " ", rest).replace(",", " , ").split())
    out["address"] = address.strip(" ,;.")[:120]
    return out


def brief_summary(data: dict) -> str:
    contacts = data.get("contacts") or {}
    shown = ", ".join(v for v in contacts.values() if v) or "не указаны"
    style = PRESETS[data["style"]]["title"] if data.get("style") in PRESETS else (data.get("style") or "на вкус агентов")
    return (f"Проверьте бриф:\n\n🏷 {data.get('business')}\n💼 {data.get('niche')}\n⭐ {data.get('description') or '—'}\n"
            f"🎨 Стиль: {style}\n🌈 Цвета: {data.get('colors') or 'на вкус колориста'}\n📞 Контакты: {shown}\n"
            f"🌐 Язык: {LANG_TITLES.get(data.get('lang', 'ru'))}")


def build_user_router(cfg: Config, *, news: NewsStore | None, store: StudioStore | None, studio: Studio | None,
                      wake_worker: Callable[[], None], wake_news: Callable[[], None]) -> Router:
    router = Router()
    is_admin = lambda chat_id: chat_id in cfg.admin_chat_ids

    # ---------- новости ----------
    @router.message(Command("news"))
    async def subscribe(message: Message) -> None:
        if news is None:
            return
        await news.subscribe(message.chat.id, message.from_user.full_name if message.from_user else "")
        await message.answer("🔔 Вы подписаны на новости. Отписаться — /stop.")

    @router.message(Command("stop"))
    async def unsubscribe(message: Message) -> None:
        if news is None:
            return
        await news.unsubscribe(message.chat.id)
        await message.answer("🔕 Вы отписаны от новостей. Вернуться — /news.")

    async def send_latest(message: Message) -> None:
        items = await news.latest(3) if news else []
        if not items:
            await message.answer("Новостей пока нет. Как только появятся — пришлём.")
            return
        for item in reversed(items):
            await message.answer(news_text(item), reply_markup=news_markup(item))

    @router.message(Command("latest"))
    async def latest(message: Message) -> None:
        await send_latest(message)

    @router.callback_query(F.data == "news:latest")
    async def latest_cb(callback: CallbackQuery) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await send_latest(callback.message)

    @router.message(Command("post"))
    async def post(message: Message, command: CommandObject) -> None:
        """Администратор: /post Заголовок, с новой строки — текст. Можно прислать фото с такой подписью."""
        if not is_admin(message.chat.id) or news is None:
            return
        raw = (command.args or "").strip()
        if not raw:
            await message.answer("Формат: /post Заголовок\nТекст новости (можно со ссылкой https://… в последней строке).\n"
                                 "Можно отправить фото с такой подписью.")
            return
        title, _, body = raw.partition("\n")
        url = ""
        lines = body.strip().splitlines()
        if lines and re.fullmatch(r"https?://\S+", lines[-1].strip()):
            url = lines.pop().strip()
        photo = message.photo[-1].file_id if message.photo else ""
        news_id = await news.add_news(title, "\n".join(lines), url, photo)
        active, _ = await news.counts()
        wake_news()
        await message.answer(f"📰 Новость № {news_id} поставлена в рассылку: {active} подписчикам.")

    @router.message(Command("subscribers"))
    async def subscribers(message: Message) -> None:
        if not is_admin(message.chat.id) or news is None:
            return
        active, total = await news.counts()
        await message.answer(f"Подписаны на новости: {active}\nВсего запускали бота: {total}")

    # ---------- заказ сайта ----------
    async def start_order(message: Message, state: FSMContext, chat_id: int) -> None:
        if store is None:
            await message.answer("Студия сайтов сейчас выключена.")
            return
        if not is_admin(chat_id):
            since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
            if cfg.studio_daily_limit == 0 or await store.count_since(chat_id, since) >= cfg.studio_daily_limit:
                await message.answer("На сегодня лимит новых сайтов исчерпан. Попробуйте завтра или внесите правки в готовый — /mysites.")
                return
        await state.clear()
        await state.set_state(Order.business)
        await message.answer("🎨 Соберём вам сайт! Над ним будут работать 20 ИИ-агентов: стратег, арт-директор, колорист, "
                             "копирайтер, критик и другие. Они поспорят о концепции и проверят друг друга.\n\n"
                             "Шаг 1 из 7. Как называется ваш бизнес?\n\n(Отменить — /cancel)")

    @router.message(Command("site"))
    async def site_cmd(message: Message, state: FSMContext) -> None:
        await start_order(message, state, message.chat.id)

    @router.callback_query(F.data == "site:new")
    async def site_cb(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await start_order(callback.message, state, callback.message.chat.id)

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer("Отменено. Начать заново — /site.")

    @router.message(StateFilter(Order.business), F.text)
    async def got_business(message: Message, state: FSMContext) -> None:
        name = " ".join((message.text or "").split())[:60]
        if len(name) < 2:
            await message.answer("Напишите название — хотя бы два символа.")
            return
        await state.update_data(business=name)
        await state.set_state(Order.niche)
        await message.answer("Шаг 2 из 7. Чем вы занимаетесь? Одной-двумя фразами: что продаёте и кому.")

    @router.message(StateFilter(Order.niche), F.text)
    async def got_niche(message: Message, state: FSMContext) -> None:
        await state.update_data(niche=" ".join((message.text or "").split())[:200])
        await state.set_state(Order.usp)
        await message.answer("Шаг 3 из 7. Чем вы лучше конкурентов? Что важно подчеркнуть на сайте?",
                             reply_markup=kb([[("Пропустить", "skip")]]))

    @router.message(StateFilter(Order.usp), F.text)
    async def got_usp(message: Message, state: FSMContext) -> None:
        await state.update_data(description=" ".join((message.text or "").split())[:600])
        await ask_style(message, state)

    async def ask_style(message: Message, state: FSMContext) -> None:
        await state.set_state(Order.style)
        await message.answer("Шаг 4 из 7. Какой стиль вам ближе? Агенты возьмут его за основу и доведут до уникального.",
                             reply_markup=style_markup())

    @router.callback_query(StateFilter(Order.usp), F.data == "skip")
    async def skip_usp(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.update_data(description="")
        if isinstance(callback.message, Message):
            await ask_style(callback.message, state)

    @router.callback_query(StateFilter(Order.style), F.data.startswith("style:"))
    async def got_style(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        key = (callback.data or "").split(":", 1)[1]
        if not isinstance(callback.message, Message):
            return
        if key == "custom":
            await state.set_state(Order.style_text)
            await callback.message.answer("Опишите стиль словами: например «тёмный, дорогой, как бутик часов» или «яркий и дерзкий».")
            return
        if key in PRESETS:
            await state.update_data(style=key)
            await ask_colors(callback.message, state)

    @router.message(StateFilter(Order.style_text), F.text)
    async def got_style_text(message: Message, state: FSMContext) -> None:
        await state.update_data(style=" ".join((message.text or "").split())[:200])
        await ask_colors(message, state)

    async def ask_colors(message: Message, state: FSMContext) -> None:
        await state.set_state(Order.colors)
        await message.answer("Шаг 5 из 7. Есть любимые или фирменные цвета? Например «изумрудный и золото».",
                             reply_markup=kb([[("На вкус колориста", "skip")]]))

    @router.message(StateFilter(Order.colors), F.text)
    async def got_colors(message: Message, state: FSMContext) -> None:
        await state.update_data(colors=" ".join((message.text or "").split())[:200])
        await ask_contacts(message, state)

    @router.callback_query(StateFilter(Order.colors), F.data == "skip")
    async def skip_colors(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.update_data(colors="")
        if isinstance(callback.message, Message):
            await ask_contacts(callback.message, state)

    async def ask_contacts(message: Message, state: FSMContext) -> None:
        await state.set_state(Order.contacts)
        await message.answer("Шаг 6 из 7. Контакты для сайта одним сообщением: телефон, @telegram, Instagram, адрес, часы работы.\n"
                             "Заявки с формы на сайте будут приходить вам сюда, в этот чат.",
                             reply_markup=kb([[("Пропустить", "skip")]]))

    @router.message(StateFilter(Order.contacts), F.text)
    async def got_contacts(message: Message, state: FSMContext) -> None:
        await state.update_data(contacts=parse_contacts(message.text or ""))
        await ask_lang(message, state)

    @router.callback_query(StateFilter(Order.contacts), F.data == "skip")
    async def skip_contacts(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        await state.update_data(contacts={})
        if isinstance(callback.message, Message):
            await ask_lang(callback.message, state)

    async def ask_lang(message: Message, state: FSMContext) -> None:
        await state.set_state(Order.lang)
        await message.answer("Шаг 7 из 7. На каком языке сайт?", reply_markup=kb([[(t, f"lang:{k}") for k, t in LANG_TITLES.items()]]))

    @router.callback_query(StateFilter(Order.lang), F.data.startswith("lang:"))
    async def got_lang(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        lang = (callback.data or "").split(":", 1)[1]
        await state.update_data(lang=lang if lang in LANG_TITLES else "ru")
        await state.set_state(Order.confirm)
        if isinstance(callback.message, Message):
            await callback.message.answer(brief_summary(await state.get_data()),
                                          reply_markup=kb([[("🚀 Запустить совет", "order:go")], [("↩️ Заново", "site:new")]]))

    @router.callback_query(StateFilter(Order.confirm), F.data == "order:go")
    async def confirm(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if store is None or not isinstance(callback.message, Message):
            return
        data = await state.get_data()
        await state.clear()
        brief = {"business": data.get("business", ""), "niche": data.get("niche", ""), "description": data.get("description", ""),
                 "style": data.get("style", ""), "colors": data.get("colors", ""), "lang": data.get("lang", "ru"),
                 "city": "", "contacts": data.get("contacts") or {}}
        chat_id = callback.message.chat.id
        project = await store.create_project(brief, slugify(brief["business"]), chat_id=chat_id)
        wake_worker()
        watch = watch_url(cfg.public_url, project)
        text = ("🚀 Совет из 20 агентов взялся за ваш сайт. Обычно это занимает 5–15 минут — пришлю ссылку, как будет готово.")
        markup = None
        if cfg.public_url.startswith("https://"):
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗣 Смотреть, как спорят агенты", url=watch)]])
        elif cfg.public_url:
            text += f"\n\nСледить за спором агентов: {watch}"
        await callback.message.answer(text, reply_markup=markup)

    # ---------- после сдачи: оценка и правки ----------
    async def own_project(chat_id: int, project_id: int):
        project = await store.get(project_id) if store else None
        if project is None or (project.chat_id != chat_id and not is_admin(chat_id)):
            return None
        return project

    @router.callback_query(F.data.startswith("rate:"))
    async def rate(callback: CallbackQuery, state: FSMContext) -> None:
        if studio is None or not isinstance(callback.message, Message):
            await callback.answer()
            return
        try:
            _, pid, value = (callback.data or "").split(":")
            project_id, rating = int(pid), int(value)
        except ValueError:
            await callback.answer()
            return
        project = await own_project(callback.message.chat.id, project_id)
        if project is None or not 1 <= rating <= 5:
            await callback.answer("Проект не найден")
            return
        await studio.rate(project, rating)
        await callback.answer("Спасибо за оценку!")
        if rating <= 3:
            await state.set_state(Order.revise)
            await state.update_data(project_id=project_id)
            await callback.message.answer("Что улучшить? Опишите одним сообщением — совет внесёт правки и запишет урок на будущее.")
        else:
            await callback.message.answer(f"Спасибо! Оценка {rating}★ укрепила уроки команды — следующие сайты будут ещё лучше.")

    @router.callback_query(F.data.startswith("revise:"))
    async def revise(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if not isinstance(callback.message, Message):
            return
        try:
            project_id = int((callback.data or "").split(":")[1])
        except ValueError:
            return
        project = await own_project(callback.message.chat.id, project_id)
        if project is None:
            return
        await state.set_state(Order.revise)
        await state.update_data(project_id=project_id)
        await callback.message.answer("Опишите правки одним сообщением: что поменять в текстах, цветах, разделах, стиле.")

    @router.message(StateFilter(Order.revise), F.text)
    async def got_revision(message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        project = await own_project(message.chat.id, int(data.get("project_id", 0)))
        if project is None or store is None:
            await message.answer("Проект не найден.")
            return
        if project.version >= MAX_REVISIONS and not is_admin(message.chat.id):
            await message.answer("Лимит правок для этого сайта исчерпан. Напишите нам — доработаем вручную.")
            return
        text = " ".join((message.text or "").split())[:1500]
        merged = f"{project.pending}; {text}" if project.pending else text
        await store.update(project.id, pending=merged)
        wake_worker()
        await message.answer("✏️ Принято! Продюсер раздаёт правки агентам. Пришлю новую версию, как будет готово.")

    @router.message(Command("mysites"))
    async def my_sites(message: Message) -> None:
        await list_sites(message, message.chat.id)

    @router.callback_query(F.data == "site:mine")
    async def my_sites_cb(callback: CallbackQuery) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await list_sites(callback.message, callback.message.chat.id)

    async def list_sites(message: Message, chat_id: int) -> None:
        projects = await store.list_projects(10, chat_id=chat_id) if store else []
        if not projects:
            await message.answer("У вас пока нет сайтов. Создать — /site.")
            return
        labels = {"queued": "в очереди", "running": "агенты работают", "done": "готов", "failed": "сбой"}
        rows = []
        for p in projects:
            line = f"• {p.name} — {labels.get(p.status, p.status)}"
            if p.version:
                line += f", версия {p.version}\n  {site_url(cfg.public_url, p)}"
            rows.append(line)
        buttons = [[("✏️ Правки: " + p.name[:30], f"revise:{p.id}")] for p in projects if p.version][:5]
        await message.answer("Ваши сайты:\n\n" + "\n".join(rows), reply_markup=kb(buttons) if buttons else None)

    # ---------- администратору ----------
    @router.message(Command("studio"))
    async def studio_list(message: Message) -> None:
        if not is_admin(message.chat.id) or store is None:
            return
        projects = await store.list_projects(8)
        if not projects:
            await message.answer("Проектов пока нет.")
            return
        lines = [f"№ {p.id} {p.name} · {p.status} · v{p.version} · ревью {p.review:.1f} · {p.rating or '—'}★ · ${p.cost_usd:.2f}"
                 for p in projects]
        tail = f"\n\nСовет агентов: {cfg.public_url}/council" if cfg.public_url else "\n\nСовет агентов: /council на сайте"
        await message.answer("Последние проекты студии:\n" + "\n".join(lines) + tail)

    @router.message(Command("lessons"))
    async def lessons(message: Message) -> None:
        if not is_admin(message.chat.id) or store is None:
            return
        items = [l for l in await store.all_lessons(10) if l["active"]]
        if not items:
            await message.answer("Уроков пока нет: они появятся после первых проектов.")
            return
        await message.answer("Чему научилась команда:\n\n" + "\n".join(f"• [{l['agent']}] {l['text']} (вес {l['score']:.1f})" for l in items))

    return router
