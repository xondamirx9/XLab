"""Тарифы и конструктор ТЗ в Telegram — на русском, узбекском и английском.

Бот считает вилку цены так же, как конструктор на сайте: цены берутся из настроек сайта (их меняет админка),
поэтому бот и сайт никогда не расходятся. Готовое ТЗ становится обычной заявкой: администраторы получают её
тем же путём, что и заявки с сайта.
"""
from __future__ import annotations

import json
import logging
import math
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, Message,
                           ReplyKeyboardMarkup, ReplyKeyboardRemove, User)

from app.config import BRAND
from app.rates import Rate, RateService
from app.storage import Storage
from app.web import RateLimiter

log = logging.getLogger("site.brief")

LANGS = ("ru", "uz", "en")
TASHKENT = timezone(timedelta(hours=5))
SITE_DATA = re.compile(r'<script type="application/json" id="site-data">(.*?)</script>', re.S)

# ---------- Цены: по умолчанию те же, что на сайте; настоящие читаются из site/index.html ----------
DEFAULT_PRICING: dict = {
    "currency": "млн сум",
    "types": {"landing": {"price": [3, 9], "weeks": [1, 2]}, "corp": {"price": [7, 22], "weeks": [2, 4]},
              "shop": {"price": [15, 50], "weeks": [3, 8]}, "app": {"price": [25, 90], "weeks": [5, 14]},
              "bot": {"price": [2.5, 12], "weeks": [1, 3]}},
    "features": {"form": [0.3, 1], "tg": [0.3, 1], "pay": [2, 6], "catalog": [3, 9], "cart": [2, 6], "account": [5, 15],
                 "booking": [2, 7], "admin": [1.5, 5], "blog": [1, 3], "crm": [4, 12], "chat": [0.3, 1.5], "map": [0.2, 0.8],
                 "notify": [1, 4], "analytics": [0.3, 1], "seo": [1, 3]},
    "mods": {"page": [0.4, 1.2], "lang": [10, 20], "rush": [20, 30], "brand": [1.5, 5], "content": [1, 4], "contentPart": [0.5, 2]},
}

# ---------- Справочники: подписи на трёх языках, состав и формулировки ТЗ — как на сайте ----------
TYPES: dict[str, dict] = {
    "landing": {"icon": "📄", "label": ("Лендинг", "Lending", "Landing page"),
                "hint": ("Одна страница под товар или услугу", "Bitta mahsulot yoki xizmat uchun bir sahifa", "One page for a product or service"),
                "pages": ["Первый экран с предложением", "Преимущества", "Цены или тарифы", "Отзывы", "Вопросы и ответы", "Форма заявки", "Контакты"],
                "features": ["form", "tg", "analytics"]},
    "corp": {"icon": "🏢", "label": ("Сайт компании", "Kompaniya sayti", "Company website"),
             "hint": ("О компании, услуги, проекты, контакты", "Kompaniya haqida, xizmatlar, loyihalar, aloqa", "About, services, projects, contacts"),
             "pages": ["Главная", "О компании", "Услуги или продукция", "Проекты и кейсы", "Новости", "Контакты"],
             "features": ["form", "admin", "analytics"]},
    "shop": {"icon": "🛍", "label": ("Интернет-магазин", "Internet-do‘kon", "Online store"),
             "hint": ("Каталог, корзина, оплата, доставка", "Katalog, savat, to‘lov, yetkazib berish", "Catalogue, cart, payment, delivery"),
             "pages": ["Главная", "Каталог", "Карточка товара", "Корзина и оформление", "Доставка и оплата", "Контакты"],
             "features": ["catalog", "cart", "pay", "admin", "analytics"]},
    "app": {"icon": "🧩", "label": ("Веб-сервис с кабинетом", "Kabinetli veb-servis", "Web app with accounts"),
            "hint": ("Регистрация, личный кабинет, своя логика", "Ro‘yxatdan o‘tish, shaxsiy kabinet, o‘ziga xos mantiq", "Sign-up, user account, custom logic"),
            "pages": ["Главная", "Регистрация и вход", "Личный кабинет", "Тарифы", "Помощь", "Панель администратора"],
            "features": ["account", "admin", "notify", "analytics"]},
    "bot": {"icon": "🤖", "label": ("Telegram-бот", "Telegram-bot", "Telegram bot"),
            "hint": ("Заявки, запись или продажи прямо в Telegram", "Arizalar, navbatga yozilish yoki savdo — to‘g‘ridan-to‘g‘ri Telegramda",
                     "Leads, bookings or sales right inside Telegram"),
            "pages": ["Приветствие и меню", "Основной сценарий", "Уведомления администратору", "Команды администратора"],
            "features": ["admin", "notify"]},
}

# w — надбавка к сроку (недели); web/bot — где функция доступна; doc и req — строка в ТЗ для студии.
FEATURES: dict[str, dict] = {
    "form": {"w": 0, "web": 1, "bot": 0, "label": ("Форма заявки", "Ariza shakli", "Request form"),
             "req": "Посетитель отправляет заявку через форму; заявка сохраняется, даже если уведомление не доставлено."},
    "tg": {"w": 0, "web": 1, "bot": 0, "label": ("Заявки в Telegram", "Arizalar Telegramga", "Leads to Telegram"),
           "req": "Каждая заявка дублируется администратору в Telegram в течение минуты."},
    "pay": {"w": 0.5, "web": 1, "bot": 1, "label": ("Онлайн-оплата (Click, Payme, Uzum)", "Onlayn to‘lov (Click, Payme, Uzum)", "Online payment (Click, Payme, Uzum)"),
            "req": "Клиент оплачивает заказ онлайн; статус оплаты виден администратору."},
    "catalog": {"w": 1, "web": 1, "bot": 1, "label": ("Каталог с фильтрами и поиском", "Filtr va qidiruvli katalog", "Catalogue with filters and search"),
                "req": "Клиент находит товар через категории, фильтры и поиск по названию."},
    "cart": {"w": 0.5, "web": 1, "bot": 1, "label": ("Корзина и оформление заказа", "Savat va buyurtmani rasmiylashtirish", "Cart and checkout"),
             "req": "Клиент добавляет товары в корзину и оформляет заказ без регистрации."},
    "account": {"w": 2, "web": 1, "bot": 0, "label": ("Личный кабинет", "Shaxsiy kabinet", "User account"),
                "req": "Пользователь регистрируется, входит в кабинет и видит свои данные и историю."},
    "booking": {"w": 1, "web": 1, "bot": 1, "label": ("Онлайн-запись или бронирование", "Onlayn yozilish yoki bron qilish", "Online booking or reservations"),
                "req": "Клиент выбирает свободное время и записывается; занятое время недоступно другим."},
    "admin": {"w": 0.5, "web": 1, "bot": 1, "label": ("Админ-панель", "Admin-panel", "Admin panel"),
              "req": "Администратор сам меняет тексты, фото и цены, без участия программиста."},
    "blog": {"w": 0.5, "web": 1, "bot": 0, "label": ("Блог или новости", "Blog yoki yangiliklar", "Blog or news"),
             "req": "Администратор публикует новости или статьи; у каждой публикации своя страница."},
    "crm": {"w": 1.5, "web": 1, "bot": 1, "label": ("Интеграция с CRM или 1С", "CRM yoki 1C bilan integratsiya", "CRM or 1C integration"),
            "req": "Заявки и заказы автоматически передаются в CRM или 1С заказчика."},
    "chat": {"w": 0, "web": 1, "bot": 0, "label": ("Онлайн-чат", "Onlayn chat", "Live chat"),
             "req": "Посетитель задаёт вопрос в онлайн-чате; оператор отвечает из одного окна."},
    "map": {"w": 0, "web": 1, "bot": 0, "label": ("Карта и адреса", "Xarita va manzillar", "Map and addresses"),
            "req": "На странице контактов есть карта с адресами и кнопкой построения маршрута."},
    "notify": {"w": 0.5, "web": 1, "bot": 1, "label": ("Уведомления: SMS, почта, Telegram", "Bildirishnomalar: SMS, pochta, Telegram", "Notifications: SMS, email, Telegram"),
               "req": "Клиент и администратор получают уведомления о заявке, оплате и смене статуса."},
    "analytics": {"w": 0, "web": 1, "bot": 0, "label": ("Аналитика и цели", "Analitika va maqsadlar", "Analytics and goals"),
                  "req": "Подключены Яндекс Метрика и Google Analytics; настроены цели на заявку и заказ."},
    "seo": {"w": 0.5, "web": 1, "bot": 0, "label": ("Базовое SEO", "Asosiy SEO", "Basic SEO"),
            "req": "У каждой страницы свои заголовок и описание для поисковиков, есть sitemap.xml и robots.txt."},
}

GOALS = {"lead": ("Получать заявки и звонки", "Ariza va qo‘ng‘iroqlar olish", "Get requests and calls"),
         "sell": ("Продавать онлайн", "Onlayn sotish", "Sell online"),
         "present": ("Рассказать о компании", "Kompaniya haqida aytib berish", "Present the company"),
         "catalog": ("Показать каталог и цены", "Katalog va narxlarni ko‘rsatish", "Show the catalogue and prices"),
         "booking": ("Принимать записи и брони", "Yozilish va bronlarni qabul qilish", "Take bookings and reservations"),
         "support": ("Отвечать на частые вопросы", "Ko‘p beriladigan savollarga javob berish", "Answer common questions")}
SITE_LANGS = {"ru": ("Русский", "Ruscha", "Russian"), "uz": ("O‘zbekcha", "O‘zbekcha", "Uzbek"), "en": ("English", "Inglizcha", "English")}
BRANDING = {"yes": ("Есть логотип и фирменный стиль", "Logotip va firma uslubi bor", "We have a logo and brand identity"),
            "logo": ("Есть только логотип", "Faqat logotip bor", "We only have a logo"),
            "none": ("Нет, нужно разработать", "Yo‘q, ishlab chiqish kerak", "No, it needs to be designed")}
CONTENT = {"yes": ("Тексты и фото готовы", "Matn va suratlar tayyor", "Copy and photos are ready"),
           "part": ("Есть частично", "Qisman bor", "Partly ready"),
           "none": ("Нет, нужна помощь", "Yo‘q, yordam kerak", "No, we need help")}
DEADLINE = {"asap": ("Как можно скорее", "Iloji boricha tezroq", "As soon as possible"),
            "month": ("В течение месяца", "Bir oy ichida", "Within a month"),
            "quarter": ("За 2–3 месяца", "2–3 oyda", "In 2–3 months"),
            "calm": ("Не горит", "Shoshilinch emas", "No rush")}
# Границы бюджета — в млн сум; подпись в долларах считается по курсу.
BUDGET = {"b1": (0, 5), "b2": (5, 15), "b3": (15, 40), "b4": (40, math.inf), "unknown": None}
UNKNOWN_BUDGET = ("Не знаю, подскажите", "Bilmayman, maslahat bering", "Not sure, advise me")

# ---------- Тексты бота ----------
T: dict[str, tuple[str, str, str]] = {
    "welcome": (
        "Здравствуйте! Это бот студии {brand} — делаем сайты и Telegram-ботов.\n\n"
        "💰 Тарифы — цены на лендинг, сайт компании, магазин, веб-сервис и бота: в сумах или в долларах по курсу NBU.\n"
        "📝 Конструктор ТЗ — ответьте на несколько вопросов, и я соберу техническое задание с вилкой цены и передам его студии.",
        "Assalomu alaykum! Bu {brand} studiyasining boti — saytlar va Telegram-botlar yaratamiz.\n\n"
        "💰 Tariflar — lending, kompaniya sayti, internet-do‘kon, veb-servis va bot narxlari: so‘mda yoki NBU kursi bo‘yicha dollarda.\n"
        "📝 TZ konstruktori — bir nechta savolga javob bering, men narx oralig‘i bilan texnik topshiriq tuzib, studiyaga yuboraman.",
        "Hello! This is the {brand} studio bot — we build websites and Telegram bots.\n\n"
        "💰 Pricing — prices for a landing page, company website, online store, web app and bot, in UZS or in US dollars at the NBU rate.\n"
        "📝 Brief builder — answer a few questions and I’ll put together a project brief with a price range and pass it to the studio."),
    "welcome_studio": ("🎨 Сайт от ИИ-агентов — 20 агентов соберут для вас готовый сайт.",
                       "🎨 AI-agentlardan sayt — 20 ta agent siz uchun tayyor sayt yig‘adi.",
                       "🎨 AI-built website — 20 AI agents will build a ready-made site for you."),
    "welcome_news": ("🔔 Вы подписаны на наши новости — отписаться можно командой /stop.",
                     "🔔 Siz yangiliklarimizga obuna bo‘ldingiz — obunani /stop buyrug‘i bilan bekor qilish mumkin.",
                     "🔔 You’re subscribed to our news — unsubscribe any time with /stop."),
    "btn_prices": ("💰 Тарифы и цены", "💰 Tariflar va narxlar", "💰 Pricing"),
    "btn_brief": ("📝 Собрать ТЗ", "📝 TZ tuzish", "📝 Build a brief"),
    "btn_studio": ("🎨 Сайт от ИИ-агентов", "🎨 AI-agentlardan sayt", "🎨 AI-built website"),
    "btn_news": ("📰 Новости", "📰 Yangiliklar", "📰 News"),
    "btn_mysites": ("🗂 Мои сайты", "🗂 Saytlarim", "🗂 My sites"),
    "btn_lang": ("🌐 Язык", "🌐 Til", "🌐 Language"),
    "lang_choose": ("🌐 Выберите язык:", "🌐 Tilni tanlang:", "🌐 Choose a language:"),
    "lang_set": ("Готово, говорим по-русски.", "Tayyor, o‘zbek tilida gaplashamiz.", "Done, we’ll talk in English."),

    "prices": ("💰 Тарифы {brand}\nЦены — вилкой, без «от» и звёздочек: точную сумму фиксируем в договоре до начала работ.",
               "💰 {brand} tariflari\nNarxlar oraliq bilan — «…dan» va yulduzchalarsiz: aniq summani ish boshlanishidan oldin shartnomada belgilaymiz.",
               "💰 {brand} pricing\nPrice ranges with no “from” and no fine print: we fix the exact amount in the contract before work starts."),
    "prices_pick": ("Выберите тариф, чтобы посмотреть, что в него входит.", "Tarkibini ko‘rish uchun tarifni tanlang.", "Pick a plan to see what’s included."),
    "rate_live": ("Курс {src}: 1 USD = {x} сум, {d}.", "{src} kursi: 1 USD = {x} so‘m, {d}.", "{src} rate: 1 USD = {x} UZS, {d}."),
    "rate_none": ("Курс NBU сейчас недоступен, поэтому цены в сумах.", "NBU kursi hozir mavjud emas, shuning uchun narxlar so‘mda.",
                  "The NBU rate is unavailable right now, so prices are shown in UZS."),
    "included": ("Что входит", "Nimalar kiradi", "What’s included"),
    "detail_after": ("Точную вилку под ваш проект посчитает конструктор ТЗ.", "Loyihangiz uchun aniq oraliqni TZ konstruktori hisoblaydi.",
                     "The brief builder will work out a precise range for your project."),
    "btn_brief_this": ("📝 Собрать ТЗ на этот тариф", "📝 Shu tarif bo‘yicha TZ tuzish", "📝 Build a brief for this plan"),
    "btn_all": ("⬅️ Все тарифы", "⬅️ Barcha tariflar", "⬅️ All plans"),
    "btn_usd": ("💵 Показать в долларах", "💵 Dollarda ko‘rsatish", "💵 Show in US dollars"),
    "btn_uzs": ("🇺🇿 Показать в сумах", "🇺🇿 So‘mda ko‘rsatish", "🇺🇿 Show in UZS"),

    "intro": ("📝 Конструктор ТЗ\nОтветьте на несколько вопросов — соберу техническое задание с вилкой цены и передам его студии. "
              "Любой вопрос можно пропустить, отменить — /cancel.\n\nЧто нужно сделать?",
              "📝 TZ konstruktori\nBir nechta savolga javob bering — narx oralig‘i bilan texnik topshiriq tuzib, studiyaga yuboraman. "
              "Istalgan savolni o‘tkazib yuborish mumkin, bekor qilish — /cancel.\n\nNima qilish kerak?",
              "📝 Brief builder\nAnswer a few questions and I’ll put together a project brief with a price range and pass it to the studio. "
              "You can skip any question; cancel with /cancel.\n\nWhat do you need?"),
    "step": ("Шаг {n} из {total}", "{total} qadamdan {n}-qadam", "Step {n} of {total}"),
    "ask_company": ("Как называется компания или проект?", "Kompaniya yoki loyiha qanday nomlanadi?", "What’s the name of the company or project?"),
    "ask_about": ("Чем занимаетесь? Пары предложений достаточно: что продаёте и кому.",
                  "Nima bilan shug‘ullanasiz? Bir-ikki gap yetarli: nima va kimga sotasiz.",
                  "What do you do? A couple of sentences is enough: what you sell and to whom."),
    "ask_goals_site": ("Зачем нужен сайт? Можно выбрать несколько, затем нажмите «Дальше».",
                       "Sayt nima uchun kerak? Bir nechtasini tanlab, «Davom etish»ni bosing.",
                       "What is the site for? Pick any, then tap “Next”."),
    "ask_goals_bot": ("Зачем нужен бот? Можно выбрать несколько, затем нажмите «Дальше».",
                      "Bot nima uchun kerak? Bir nechtasini tanlab, «Davom etish»ni bosing.",
                      "What is the bot for? Pick any, then tap “Next”."),
    "ask_features": ("Какие функции нужны? Типовой набор уже отмечен — уберите лишнее, добавьте своё.\n\n💰 Ориентир сейчас: {estimate}",
                     "Qaysi funksiyalar kerak? Odatdagi to‘plam belgilab qo‘yilgan — ortiqchasini olib tashlang, o‘zingiznikini qo‘shing.\n\n💰 Hozirgi taxmin: {estimate}",
                     "Which features do you need? The standard set is already ticked — remove what you don’t need and add your own.\n\n💰 Current estimate: {estimate}"),
    "ask_langs_site": ("На каких языках будет сайт? Каждый дополнительный язык добавляет к цене.",
                       "Sayt qaysi tillarda bo‘ladi? Har bir qo‘shimcha til narxni oshiradi.",
                       "Which languages will the site be in? Each extra language adds to the price."),
    "ask_langs_bot": ("На каких языках будет бот? Каждый дополнительный язык добавляет к цене.",
                      "Bot qaysi tillarda bo‘ladi? Har bir qo‘shimcha til narxni oshiradi.",
                      "Which languages will the bot speak? Each extra language adds to the price."),
    "ask_brand": ("Есть логотип и фирменный стиль?", "Logotip va firma uslubi bormi?", "Do you have a logo and brand identity?"),
    "ask_content": ("Тексты и фото готовы?", "Matn va suratlar tayyormi?", "Are the copy and photos ready?"),
    "ask_deadline": ("Когда нужен запуск?", "Qachon ishga tushirish kerak?", "When do you need to launch?"),
    "ask_budget": ("Какой бюджет? Честный ответ поможет сразу предложить подходящий объём.",
                   "Budjet qancha? Haqiqiy javob darhol mos hajmni taklif qilishga yordam beradi.",
                   "What’s your budget? An honest answer helps us propose the right scope straight away."),
    "ask_name": ("Как к вам обращаться?", "Sizga qanday murojaat qilaylik?", "What should we call you?"),
    "ask_phone": ("Оставьте телефон: нажмите кнопку ниже или напишите номер. Удобнее Telegram — нажмите «Пропустить», ответим сюда.",
                  "Telefon raqamingizni qoldiring: pastdagi tugmani bosing yoki raqamni yozing. Telegram qulayroq bo‘lsa — «O‘tkazib yuborish»ni bosing, shu yerga javob beramiz.",
                  "Leave your phone number: tap the button below or type it. Prefer Telegram? Tap “Skip” and we’ll reply here."),
    "ask_phone_required": ("Нужен телефон: у вашего аккаунта Telegram нет имени пользователя, и иначе мы не сможем ответить. "
                           "Нажмите кнопку ниже или напишите номер.",
                           "Telefon kerak: Telegram akkauntingizda foydalanuvchi nomi yo‘q, aks holda javob bera olmaymiz. "
                           "Pastdagi tugmani bosing yoki raqamni yozing.",
                           "We need a phone number: your Telegram account has no username, so otherwise we can’t reply. "
                           "Tap the button below or type the number."),
    "name_bad": ("Напишите имя — хотя бы два символа.", "Ismni yozing — kamida ikki belgi.", "Type a name — at least two characters."),
    "phone_bad": ("Проверьте номер: в нём не хватает цифр.", "Raqamni tekshiring: raqamlar yetishmayapti.", "Check the number: some digits are missing."),
    "phone_saved": ("Записал: {phone}", "Yozib oldim: {phone}", "Saved: {phone}"),
    "phone_skipped": ("Хорошо, ответим в Telegram.", "Yaxshi, Telegramda javob beramiz.", "Okay, we’ll reply on Telegram."),
    "btn_skip": ("Пропустить", "O‘tkazib yuborish", "Skip"),
    "btn_next": ("Дальше ➡️", "Davom etish ➡️", "Next ➡️"),
    "btn_phone": ("📱 Отправить номер", "📱 Raqamni yuborish", "📱 Share my number"),

    "summary": ("🧾 ТЗ готово. Проверьте и отправьте студии:", "🧾 TZ tayyor. Tekshiring va studiyaga yuboring:",
                "🧾 Your brief is ready. Check it and send it to the studio:"),
    "l_type": ("Проект", "Loyiha", "Project"),
    "l_company": ("Компания", "Kompaniya", "Company"),
    "l_about": ("О бизнесе", "Biznes haqida", "Business"),
    "l_goals": ("Задачи", "Vazifalar", "Goals"),
    "l_features": ("Функции", "Funksiyalar", "Features"),
    "l_langs": ("Языки", "Tillar", "Languages"),
    "l_brand": ("Логотип и стиль", "Logotip va uslub", "Logo and identity"),
    "l_content": ("Тексты и фото", "Matn va suratlar", "Copy and photos"),
    "l_deadline": ("Запуск", "Ishga tushirish", "Launch"),
    "l_budget": ("Бюджет", "Budjet", "Budget"),
    "l_contact": ("Контакт", "Aloqa", "Contact"),
    "l_estimate": ("Ориентир", "Taxminiy narx", "Estimate"),
    "not_set": ("уточним вместе", "birga aniqlaymiz", "to be agreed"),
    "over": ("⚠️ Выше бюджета — предложим первую версию, которая в него уложится.",
             "⚠️ Budjetdan yuqori — unga sig‘adigan birinchi versiyani taklif qilamiz.",
             "⚠️ Above your budget — we’ll propose a first version that fits it."),
    "open_q": ("Открытых вопросов: {n} — разберём при первом контакте.", "Ochiq savollar: {n} ta — birinchi muloqotda hal qilamiz.",
               "Open questions: {n} — we’ll go through them when we first talk."),
    "btn_send": ("✅ Отправить заявку", "✅ Arizani yuborish", "✅ Send request"),
    "btn_restart": ("🔄 Начать заново", "🔄 Boshidan boshlash", "🔄 Start over"),
    "sent": ("✅ Заявка № {n} отправлена! Студия получила ТЗ и ответит в течение рабочего дня.",
             "✅ {n}-sonli ariza yuborildi! Studiya TZni oldi va bir ish kuni ichida javob beradi.",
             "✅ Request No. {n} sent! The studio has your brief and will reply within one business day."),
    "too_many": ("Слишком много заявок подряд. Попробуйте через час.", "Ketma-ket juda ko‘p ariza. Bir soatdan keyin urinib ko‘ring.",
                 "Too many requests in a row. Please try again in an hour."),
    "cancelled": ("Отменено. Собрать ТЗ заново — /brief, тарифы — /prices.", "Bekor qilindi. TZni qaytadan tuzish — /brief, tariflar — /prices.",
                  "Cancelled. Build a brief again with /brief, see pricing with /prices."),
    "expired": ("Кнопка устарела — начните заново: /brief", "Tugma eskirgan — qaytadan boshlang: /brief", "This button has expired — start again: /brief"),
}
UNIT = {"ru": "млн сум", "uz": "mln so‘m", "en": "mln UZS"}


def t(key: str, lang: str, **kw: object) -> str:
    text = T[key][LANGS.index(lang)]
    return text.format(**kw) if kw else text


def word(pair: tuple[str, str, str], lang: str) -> str:
    return pair[LANGS.index(lang)]


def lang_of(user: User | None) -> str:
    """Язык по настройкам Telegram: узбекский и английский узнаём, остальным — русский."""
    code = (user.language_code or "").lower() if user else ""
    return "uz" if code.startswith("uz") else ("en" if code.startswith("en") else "ru")


class ChatPrefs:
    """Язык и валюта, которые выбрал человек. Держим в памяти: после перезапуска язык снова берётся из Telegram."""

    def __init__(self) -> None:
        self._prefs: dict[int, dict[str, str]] = {}

    def lang(self, chat_id: int, user: User | None) -> str:
        return self._prefs.get(chat_id, {}).get("lang") or lang_of(user)

    def cur(self, chat_id: int) -> str:
        return self._prefs.get(chat_id, {}).get("cur", "uzs")

    def set(self, chat_id: int, **values: str) -> None:
        self._prefs.setdefault(chat_id, {}).update(values)


# ---------- Цены и расчёт ----------
def _pair(value: object, fallback: list) -> list:
    if (isinstance(value, list) and len(value) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value)
            and 0 <= value[0] <= value[1] <= 100_000):
        return [float(value[0]), float(value[1])]
    return [float(fallback[0]), float(fallback[1])]


def read_pricing(index_file: Path) -> dict:
    """Цены из настроек сайта. Битые или пропавшие значения заменяются ценами по умолчанию."""
    try:
        found = SITE_DATA.search(index_file.read_text(encoding="utf-8"))
        raw = json.loads(found.group(1)).get("pricing", {}) if found else {}
    except (OSError, ValueError, AttributeError):
        raw = {}
    raw = raw if isinstance(raw, dict) else {}
    get = lambda name: raw.get(name) if isinstance(raw.get(name), dict) else {}
    types, features, mods = get("types"), get("features"), get("mods")
    currency = raw.get("currency") if isinstance(raw.get("currency"), str) and raw.get("currency").strip() else DEFAULT_PRICING["currency"]
    return {
        "currency": currency.strip()[:16],
        "types": {k: {"price": _pair((types.get(k) or {}).get("price") if isinstance(types.get(k), dict) else None, v["price"]),
                      "weeks": _pair((types.get(k) or {}).get("weeks") if isinstance(types.get(k), dict) else None, v["weeks"])}
                  for k, v in DEFAULT_PRICING["types"].items()},
        "features": {k: _pair(features.get(k), v) for k, v in DEFAULT_PRICING["features"].items()},
        "mods": {k: _pair(mods.get(k), v) for k, v in DEFAULT_PRICING["mods"].items()},
    }


class SitePricing:
    """Перечитывает цены, только когда админка опубликовала новую версию сайта."""

    def __init__(self, index_file: Path) -> None:
        self._file = index_file
        self._stamp: int | None = None
        self._data = read_pricing(index_file)

    def get(self) -> dict:
        try:
            stamp = self._file.stat().st_mtime_ns
        except OSError:
            return self._data
        if stamp != self._stamp:
            self._stamp, self._data = stamp, read_pricing(self._file)
        return self._data


def _half_up(x: float) -> float:
    return math.floor(x * 2 + 0.5) / 2


def estimate(brief: dict, pricing: dict) -> dict | None:
    """Вилка цены (млн сум) и срок (недели) — та же формула, что estimate() на сайте."""
    kind = brief.get("type")
    if kind not in TYPES:
        return None
    base, mods = pricing["types"][kind], pricing["mods"]
    lo, hi = base["price"]
    wl, wh = base["weeks"]
    for key in brief.get("features", []):
        if key not in FEATURES or key in TYPES[kind]["features"]:     # входящее в базовый набор уже в цене
            continue
        lo += pricing["features"][key][0]
        hi += pricing["features"][key][1]
        wh += FEATURES[key]["w"]
    extra_langs = max(0, len(brief.get("langs") or ["ru"]) - 1)
    lo *= 1 + extra_langs * mods["lang"][0] / 100
    hi *= 1 + extra_langs * mods["lang"][1] / 100
    if kind != "bot":
        if brief.get("brand") == "none":
            lo, hi, wh = lo + mods["brand"][0], hi + mods["brand"][1], wh + 1
        if brief.get("content") == "none":
            lo, hi, wh = lo + mods["content"][0], hi + mods["content"][1], wh + 0.5
        elif brief.get("content") == "part":
            lo, hi = lo + mods["contentPart"][0], hi + mods["contentPart"][1]
    if brief.get("deadline") == "asap":
        lo *= 1 + mods["rush"][0] / 100
        hi *= 1 + mods["rush"][1] / 100
        wh = max(wl, math.ceil((wl + wh) / 2))
    lo = _half_up(lo)
    hi = max(lo, _half_up(hi))
    wl = math.floor(wl + 0.5)
    wh = max(wl, math.ceil(wh))
    limits = BUDGET.get(brief.get("budget") or "")
    return {"lo": lo, "hi": hi, "wl": wl, "wh": int(wh), "over": bool(limits and limits[1] < lo)}


# ---------- Деньги и сроки текстом ----------
def number(x: float, lang: str) -> str:
    """12650.5 → «12 650,5» (ru, uz) или «12,650.5» (en): как toLocaleString на сайте."""
    x = round(x, 1)
    text = f"{int(x):,}" if x == int(x) else f"{x:,.1f}"
    return text if lang == "en" else text.replace(",", "\u00a0").replace(".", ",")


def to_usd(mln: float, usd: float) -> int:
    """Как на сайте: до 1000 $ — с шагом 10, до 10 000 $ — 50, дальше — 100."""
    x = mln * 1e6 / usd
    step = 10 if x < 1000 else (50 if x < 10000 else 100)
    return max(step, int(math.floor(x / step + 0.5) * step))


def money(lo: float, hi: float, lang: str, cur: str, rate: Rate | None, pricing: dict) -> str:
    if cur == "usd" and rate is not None:
        a = to_usd(lo, rate.usd)
        return f"{number(a, lang)}–{number(max(a, to_usd(hi, rate.usd)), lang)} USD"
    unit = pricing["currency"] if lang == "ru" or pricing["currency"] != "млн сум" else UNIT[lang]
    return f"{number(lo, lang)}–{number(hi, lang)} {unit}"


def weeks_text(a: int, b: int, lang: str) -> str:
    span = str(a) if a == b else f"{a}–{b}"
    if lang == "uz":
        return f"{span} hafta"
    if lang == "en":
        return f"{span} week" if b == 1 else f"{span} weeks"
    return f"{span} {plural_ru(b, ('неделя', 'недели', 'недель'))}"


def plural_ru(n: int, forms: tuple[str, str, str]) -> str:
    a, b = abs(n) % 100, abs(n) % 10
    if 10 < a < 20:
        return forms[2]
    return forms[1] if 1 < b < 5 else (forms[0] if b == 1 else forms[2])


def rate_text(rate: Rate | None, lang: str) -> str:
    if rate is None:
        return t("rate_none", lang)
    day = rate.date
    try:
        day = datetime.strptime(rate.date, "%Y-%m-%d").strftime("%d/%m/%Y" if lang == "en" else "%d.%m.%Y")
    except ValueError:
        pass
    return t("rate_live", lang, src="CBU" if rate.source == "cbu" else "NBU", x=number(rate.usd, lang), d=day)


def budget_label(key: str, lang: str, cur: str, rate: Rate | None) -> str:
    limits = BUDGET.get(key)
    if limits is None:
        return word(UNKNOWN_BUDGET, lang)
    lo, hi = limits
    if cur == "usd" and rate is not None:
        amount = lambda mln: f"{number(to_usd(mln, rate.usd), lang)} USD"
    else:
        amount = lambda mln: f"{number(mln, lang)} {UNIT[lang]}"
    if not lo:
        return {"ru": "До {x}", "uz": "{x} gacha", "en": "Up to {x}"}[lang].format(x=amount(hi))
    if hi == math.inf:
        return {"ru": "Больше {x}", "uz": "{x} dan ko‘p", "en": "Over {x}"}[lang].format(x=amount(lo))
    return f"{amount(lo).split(' ')[0]}–{amount(hi)}"


# ---------- ТЗ для студии (всегда по-русски: его читают администраторы) ----------
def open_questions(brief: dict, est: dict | None) -> list[str]:
    """То, что студии предстоит уточнить: пропущенные ответы и решения, которые зависят от клиента."""
    kind, is_bot = brief.get("type"), brief.get("type") == "bot"
    out = []
    if not brief.get("company"):
        out.append("Название компании или проекта")
    if not brief.get("about"):
        out.append("Коротко описать бизнес: что продаёте и кому")
    out.append("Кто целевой клиент")
    if not brief.get("goals"):
        out.append(f"Какие задачи решает {'бот' if is_bot else 'сайт'}")
    out.append("Какое одно действие должен совершить посетитель")
    if not is_bot:
        if not brief.get("brand"):
            out.append("Есть ли логотип и фирменный стиль")
        if not brief.get("content"):
            out.append("Кто готовит тексты и фото")
        out.append("Есть ли домен и хостинг")
    if not brief.get("deadline"):
        out.append("Желаемый срок запуска")
    if not brief.get("budget"):
        out.append("Бюджет проекта")
    if not is_bot and brief.get("brand") == "none":
        out.append("Разработка логотипа и фирменного стиля: объём работ")
    if not is_bot and brief.get("content") == "none":
        out.append("Подготовка текстов и фото: кто пишет и снимает")
    if "pay" in brief.get("features", []):
        out.append("Приём платежей: на какое юрлицо и через какие системы")
    if "crm" in brief.get("features", []):
        out.append("Интеграция: какая CRM или версия 1С, есть ли доступ к API")
    if len(brief.get("langs") or ["ru"]) > 1:
        out.append("Перевод текстов: кто предоставляет")
    if brief.get("budget") == "unknown":
        out.append("Согласовать бюджет после оценки")
    if est and est["over"]:
        out.append("Бюджет ниже ориентира: согласовать, что войдёт в первую версию")
    if brief.get("deadline") == "asap":
        out.append("Сжатый срок: согласовать состав первой версии")
    return out if kind in TYPES else ["Выбрать тип проекта", *out]


def brief_doc(brief: dict, pricing: dict, rate: Rate | None, cur: str) -> str:
    """Текст ТЗ того же вида, что собирает сайт: так заявки из бота и с сайта читаются одинаково."""
    kind = TYPES[brief["type"]]
    is_bot = brief["type"] == "bot"
    est = estimate(brief, pricing)
    ru = lambda pair: pair[0]
    pick = lambda table, key: ru(table[key]) if key in table else "уточним вместе"
    goals = "; ".join(ru(GOALS[g]) for g in brief.get("goals", []) if g in GOALS) or "уточним вместе"
    langs = ", ".join(ru(SITE_LANGS[x]) for x in brief.get("langs") or ["ru"] if x in SITE_LANGS)
    out = ["ТЕХНИЧЕСКОЕ ЗАДАНИЕ", f"на разработку: {ru(kind['label']).lower()}",
           f"Дата: {datetime.now(TASHKENT).strftime('%d.%m.%Y')}", "Источник: Telegram-бот",
           "", "1. Цель и контекст", f"Проект: {ru(kind['label'])}", f"Компания: {brief.get('company') or 'уточним вместе'}",
           f"Чем занимается: {brief.get('about') or 'уточним вместе'}", f"Задачи: {goals}",
           "", f"2. {'Сценарии бота' if is_bot else 'Структура сайта'}", *[f"- {page}" for page in kind["pages"]],
           "", "3. Функциональные требования"]
    reqs = [FEATURES[k]["req"] for k in brief.get("features", []) if k in FEATURES]
    out += [f"ФТ-{i}. {req}" for i, req in enumerate(reqs, 1)] or ["Список функций предложит студия"]
    out += ["", "4. Общие требования"]
    out += (["- Бот отвечает на действие пользователя не дольше 2 секунд.", "- Диалоги не теряются при перезапуске бота.",
             "- Команды администратора доступны только администраторам."] if is_bot else
            ["- Сайт корректно отображается на телефонах от 360 px, планшетах и компьютерах.",
             "- Первый экран загружается не дольше 2,5 секунды на мобильном интернете.",
             "- Сайт работает по HTTPS; формы защищены от спама."])
    out.append(f"- Языки: {langs}.")
    section = 5
    if not is_bot:
        out += ["", f"{section}. Дизайн и материалы", f"Фирменный стиль: {pick(BRANDING, brief.get('brand'))}",
                f"Тексты и фото: {pick(CONTENT, brief.get('content'))}"]
        section += 1
    budget = brief.get("budget")
    out += ["", f"{section}. Сроки и бюджет", f"Запуск: {pick(DEADLINE, brief.get('deadline'))}",
            f"Бюджет: {budget_label(budget, 'ru', 'uzs', None) if budget in BUDGET else 'уточним вместе'}",
            f"Ориентир студии: {money(est['lo'], est['hi'], 'ru', 'uzs', None, pricing)}, {weeks_text(est['wl'], est['wh'], 'ru')}"]
    if cur == "usd" and rate is not None:          # студия увидит, какую сумму в долларах видел клиент
        out.append(f"Клиент смотрел цены в долларах: {money(est['lo'], est['hi'], 'ru', 'usd', rate, pricing)} ({rate_text(rate, 'ru').rstrip('.')})")
    section += 1
    questions = open_questions(brief, est)
    out += ["", f"{section}. Открытые вопросы", *[f"- {q}" for q in questions]]
    section += 1
    out += ["", f"{section}. Контакты заказчика", f"Имя: {brief.get('name') or '—'}", f"Телефон: {brief.get('phone') or '—'}",
            f"Telegram: @{brief['tg']}" if brief.get("tg") else "Telegram: —",
            "", f"Составлено в Telegram-боте {BRAND}"]
    return "\n".join(out)


def estimate_line(est: dict) -> str:
    """Строка «Ориентир» в шапке заявки для администратора — как у заявок с сайта."""
    n = lambda x: f"{x:g}".replace(".", ",")
    return f"{n(est['lo'])}–{n(est['hi'])} млн сум, {est['wl']}–{est['wh']} нед."


# ---------- Клавиатуры ----------
def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=data) for text, data in row] for row in rows])


def main_menu(lang: str, *, studio: bool, news: bool) -> InlineKeyboardMarkup:
    rows = [[(t("btn_prices", lang), "menu:prices"), (t("btn_brief", lang), "menu:brief")]]
    if studio:
        rows.append([(t("btn_studio", lang), "site:new")])
    extra = ([(t("btn_news", lang), "news:latest")] if news else []) + ([(t("btn_mysites", lang), "site:mine")] if studio else [])
    if extra:
        rows.append(extra)
    rows.append([(t("btn_lang", lang), "menu:lang")])
    return kb(rows)


def welcome_text(lang: str, *, studio: bool, news: bool) -> str:
    parts = [t("welcome", lang, brand=BRAND)]
    if studio:
        parts.append(t("welcome_studio", lang))
    text = "\n".join(parts)
    return text + ("\n\n" + t("welcome_news", lang) if news else "")


class Brief(StatesGroup):
    type = State()
    company = State()
    about = State()
    goals = State()
    features = State()
    langs = State()
    brand = State()
    content = State()
    deadline = State()
    budget = State()
    name = State()
    phone = State()
    confirm = State()


ORDER = ("type", "company", "about", "goals", "features", "langs", "brand", "content", "deadline", "budget", "name", "phone")
STATES = {name: getattr(Brief, name) for name in (*ORDER, "confirm")}
CHOICES = {"brand": BRANDING, "content": CONTENT, "deadline": DEADLINE}
SKIP_WORDS = {pair.lower() for pair in T["btn_skip"]}


def steps_for(kind: str) -> list[str]:
    """Боту не нужны логотип и фото: эти шаги пропускаем, как на сайте."""
    return [s for s in ORDER if not (kind == "bot" and s in ("brand", "content"))]


def build_brief_router(cfg_admins: tuple[int, ...], storage: Storage, pricing: SitePricing, prefs: ChatPrefs, *,
                       rates: RateService | None, wake_orders: Callable[[], None], studio: bool, news: bool) -> Router:
    router = Router()
    order_limit = RateLimiter(limit=3, window=3600)           # не больше трёх заявок из бота в час с одного чата

    async def rate_now() -> Rate | None:
        return await rates.usd() if rates is not None else None

    async def show(message: Message, text: str, markup: InlineKeyboardMarkup | None, edit: bool) -> None:
        """Отвечает новым сообщением или правит то, на кнопку которого нажали."""
        if edit:
            try:
                await message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest:          # сообщение не изменилось или слишком старое — пришлём новое
                pass
        await message.answer(text, reply_markup=markup)

    # ---------- тарифы ----------
    async def prices_view(chat_id: int, user: User | None) -> tuple[str, InlineKeyboardMarkup]:
        lang, cur, data, rate = prefs.lang(chat_id, user), prefs.cur(chat_id), pricing.get(), await rate_now()
        rows = [f"{k['icon']} {word(k['label'], lang)} — {money(*data['types'][key]['price'], lang, cur, rate, data)}, "
                f"{weeks_text(*map(int, data['types'][key]['weeks']), lang)}" for key, k in TYPES.items()]
        text = t("prices", lang, brand=BRAND) + "\n\n" + "\n".join(rows)
        if cur == "usd":
            text += "\n\n" + rate_text(rate, lang)
        text += "\n\n" + t("prices_pick", lang)
        buttons = [[(f"{k['icon']} {word(k['label'], lang)}", f"tariff:{key}")] for key, k in TYPES.items()]
        buttons.append([currency_button(lang, cur, "list")])
        return text, kb(buttons)

    def currency_button(lang: str, cur: str, view: str) -> tuple[str, str]:
        return (t("btn_uzs", lang), f"cur:uzs:{view}") if cur == "usd" else (t("btn_usd", lang), f"cur:usd:{view}")

    async def tariff_view(chat_id: int, user: User | None, key: str) -> tuple[str, InlineKeyboardMarkup]:
        lang, cur, data, rate = prefs.lang(chat_id, user), prefs.cur(chat_id), pricing.get(), await rate_now()
        kind, price = TYPES[key], data["types"][key]
        n = len(kind["pages"])
        count = {"ru": f"{n} {plural_ru(n, ('сценарий', 'сценария', 'сценариев') if key == 'bot' else ('раздел', 'раздела', 'разделов'))}",
                 "uz": f"{n} ta {'ssenariy' if key == 'bot' else 'bo‘lim'}",
                 "en": f"{n} {'scenarios' if key == 'bot' else 'sections'}"}[lang]
        lines = [f"{kind['icon']} {word(kind['label'], lang)}", word(kind["hint"], lang), "",
                 f"💰 {money(*price['price'], lang, cur, rate, data)}", f"🗓 {weeks_text(*map(int, price['weeks']), lang)}", "",
                 f"{t('included', lang)}:", f"• {count}", *[f"• {word(FEATURES[f]['label'], lang)}" for f in kind["features"]]]
        if cur == "usd":
            lines += ["", rate_text(rate, lang)]
        lines += ["", t("detail_after", lang)]
        markup = kb([[(t("btn_brief_this", lang), f"brief:type:{key}")], [currency_button(lang, cur, f"t.{key}")],
                     [(t("btn_all", lang), "tariffs")]])
        return "\n".join(lines), markup

    @router.message(Command("prices", "tariffs"))
    async def prices_cmd(message: Message) -> None:
        text, markup = await prices_view(message.chat.id, message.from_user)
        await message.answer(text, reply_markup=markup)

    @router.callback_query(F.data.in_({"menu:prices", "tariffs"}))
    async def prices_cb(callback: CallbackQuery) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            text, markup = await prices_view(callback.message.chat.id, callback.from_user)
            await show(callback.message, text, markup, edit=callback.data == "tariffs")

    @router.callback_query(F.data.startswith("tariff:"))
    async def tariff_cb(callback: CallbackQuery) -> None:
        await callback.answer()
        key = (callback.data or "").split(":", 1)[1]
        if key in TYPES and isinstance(callback.message, Message):
            text, markup = await tariff_view(callback.message.chat.id, callback.from_user, key)
            await show(callback.message, text, markup, edit=True)

    @router.callback_query(F.data.startswith("cur:"))
    async def currency_cb(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or parts[1] not in ("uzs", "usd") or not isinstance(callback.message, Message):
            return
        chat_id, view = callback.message.chat.id, parts[2]
        prefs.set(chat_id, cur=parts[1])
        if view == "list":
            text, markup = await prices_view(chat_id, callback.from_user)
        elif view.startswith("t.") and view[2:] in TYPES:
            text, markup = await tariff_view(chat_id, callback.from_user, view[2:])
        elif view == "sum" and await state.get_state() == Brief.confirm.state:
            text, markup = await summary_view(chat_id, callback.from_user, await state.get_data())
        else:
            return
        await show(callback.message, text, markup, edit=True)

    # ---------- язык ----------
    @router.message(Command("lang"))
    async def lang_cmd(message: Message) -> None:
        await message.answer(t("lang_choose", prefs.lang(message.chat.id, message.from_user)), reply_markup=lang_markup())

    @router.callback_query(F.data == "menu:lang")
    async def lang_menu_cb(callback: CallbackQuery) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await callback.message.answer(t("lang_choose", prefs.lang(callback.message.chat.id, callback.from_user)), reply_markup=lang_markup())

    def lang_markup() -> InlineKeyboardMarkup:
        return kb([[("🇷🇺 Русский", "ui:lang:ru"), ("🇺🇿 O‘zbekcha", "ui:lang:uz"), ("🇬🇧 English", "ui:lang:en")]])

    @router.callback_query(F.data.startswith("ui:lang:"))
    async def lang_set_cb(callback: CallbackQuery) -> None:
        await callback.answer()
        lang = (callback.data or "").rsplit(":", 1)[1]
        if lang not in LANGS or not isinstance(callback.message, Message):
            return
        prefs.set(callback.message.chat.id, lang=lang)
        await callback.message.answer(t("lang_set", lang), reply_markup=main_menu(lang, studio=studio, news=news))

    # ---------- конструктор ТЗ ----------
    async def begin(message: Message, state: FSMContext, user: User | None, kind: str | None = None) -> None:
        await state.clear()
        await state.update_data(oid="tg-" + uuid.uuid4().hex, goals=[], features=[], langs=["ru"])
        if kind in TYPES:
            await choose_type(message, state, user, kind)
            return
        await state.set_state(Brief.type)
        lang = prefs.lang(message.chat.id, user)
        await message.answer(t("intro", lang), reply_markup=kb([[(f"{k['icon']} {word(k['label'], lang)}", f"b:type:{key}")] for key, k in TYPES.items()]))

    async def choose_type(message: Message, state: FSMContext, user: User | None, kind: str) -> None:
        await state.update_data(type=kind, features=list(TYPES[kind]["features"]))
        await ask(message, state, user, "company")

    @router.message(Command("brief"))
    async def brief_cmd(message: Message, state: FSMContext) -> None:
        await begin(message, state, message.from_user)

    @router.callback_query(F.data == "menu:brief")
    async def brief_menu_cb(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await begin(callback.message, state, callback.from_user)

    @router.callback_query(F.data.startswith("brief:type:"))
    async def brief_from_tariff(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await begin(callback.message, state, callback.from_user, (callback.data or "").rsplit(":", 1)[1])

    @router.callback_query(StateFilter(Brief.type), F.data.startswith("b:type:"))
    async def type_cb(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        kind = (callback.data or "").rsplit(":", 1)[1]
        if kind in TYPES and isinstance(callback.message, Message):
            await choose_type(callback.message, state, callback.from_user, kind)

    @router.message(Command("cancel"), StateFilter(Brief))
    async def cancel(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(t("cancelled", prefs.lang(message.chat.id, message.from_user)), reply_markup=ReplyKeyboardRemove())

    def step_view(step: str, data: dict, lang: str, cur: str, rate: Rate | None, user: User | None) -> tuple[str, object]:
        kind = data["type"]
        order = steps_for(kind)
        head = t("step", lang, n=order.index(step) + 1, total=len(order)) + "\n\n"
        is_bot = kind == "bot"
        tick = lambda on, label: ("✅ " if on else "▫️ ") + label
        nxt = [(t("btn_next", lang), "b:next")]
        if step in ("company", "about"):
            return head + t(f"ask_{step}", lang), kb([[(t("btn_skip", lang), "b:skip")]])
        if step == "goals":
            rows = [[(tick(k in data["goals"], word(v, lang)), f"b:goal:{k}")] for k, v in GOALS.items()]
            return head + t("ask_goals_bot" if is_bot else "ask_goals_site", lang), kb([*rows, nxt])
        if step == "features":
            est = estimate(data, pricing.get())
            shown = money(est["lo"], est["hi"], lang, cur, rate, pricing.get()) + ", " + weeks_text(est["wl"], est["wh"], lang)
            keys = [k for k, f in FEATURES.items() if f["bot" if is_bot else "web"]]
            rows = [[(tick(k in data["features"], word(FEATURES[k]["label"], lang)), f"b:feat:{k}")] for k in keys]
            return head + t("ask_features", lang, estimate=shown), kb([*rows, nxt])
        if step == "langs":
            rows = [[(tick(k in data["langs"], word(v, lang)), f"b:lang:{k}") for k, v in SITE_LANGS.items()]]
            return head + t("ask_langs_bot" if is_bot else "ask_langs_site", lang), kb([*rows, nxt])
        if step in CHOICES:
            return head + t(f"ask_{step}", lang), kb([[(word(v, lang), f"b:{step}:{k}")] for k, v in CHOICES[step].items()])
        if step == "budget":
            return head + t("ask_budget", lang), kb([[(budget_label(k, lang, cur, rate), f"b:budget:{k}")] for k in BUDGET])
        if step == "name":
            own = " ".join(((user.full_name if user else "") or "").split())[:60]
            return head + t("ask_name", lang), (kb([[(f"👤 {own}", "b:name:tg")]]) if len(own) >= 2 else None)
        # телефон: обычная клавиатура с кнопкой «Отправить номер»
        has_username = bool(data.get("tg"))
        keys = [[KeyboardButton(text=t("btn_phone", lang), request_contact=True)]]
        if has_username:
            keys.append([KeyboardButton(text=t("btn_skip", lang))])
        markup = ReplyKeyboardMarkup(keyboard=keys, resize_keyboard=True, one_time_keyboard=True)
        return head + t("ask_phone" if has_username else "ask_phone_required", lang), markup

    async def ask(message: Message, state: FSMContext, user: User | None, step: str, edit: bool = False) -> None:
        chat_id = message.chat.id
        if step == "phone":
            await state.update_data(tg=(user.username or "") if user else "")
        data = await state.get_data()
        await state.set_state(STATES[step])
        text, markup = step_view(step, data, prefs.lang(chat_id, user), prefs.cur(chat_id), await rate_now(), user)
        if edit and isinstance(markup, InlineKeyboardMarkup):
            await show(message, text, markup, edit=True)
        else:
            await message.answer(text, reply_markup=markup)

    async def advance(message: Message, state: FSMContext, user: User | None, current: str) -> None:
        data = await state.get_data()
        order = steps_for(data.get("type", ""))
        following = order[order.index(current) + 1:] if current in order else []
        if following:
            await ask(message, state, user, following[0])
        else:
            await state.set_state(Brief.confirm)
            text, markup = await summary_view(message.chat.id, user, await state.get_data())
            await message.answer(text, reply_markup=markup)

    async def current_step(state: FSMContext) -> str | None:
        name = await state.get_state()
        return next((step for step, st in STATES.items() if st.state == name), None)

    plain_text = F.text & ~F.text.startswith("/")

    @router.message(StateFilter(Brief.company, Brief.about), plain_text)
    async def got_text(message: Message, state: FSMContext) -> None:
        step = await current_step(state)
        limit = 120 if step == "company" else 600
        await state.update_data({step: " ".join((message.text or "").split())[:limit]})
        await advance(message, state, message.from_user, step)

    @router.callback_query(StateFilter(Brief.company, Brief.about), F.data == "b:skip")
    async def skip_text(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        step = await current_step(state)
        if isinstance(callback.message, Message):
            await state.update_data({step: ""})
            await advance(callback.message, state, callback.from_user, step)

    @router.callback_query(StateFilter(Brief.goals, Brief.features, Brief.langs), F.data.regexp(r"^b:(goal|feat|lang):\w+$"))
    async def toggle(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        _, what, key = (callback.data or "").split(":")
        field, table = {"goal": ("goals", GOALS), "feat": ("features", FEATURES), "lang": ("langs", SITE_LANGS)}[what]
        step = await current_step(state)
        if key not in table or step != field or not isinstance(callback.message, Message):
            return
        chosen = list((await state.get_data()).get(field, []))
        chosen = [x for x in chosen if x != key] if key in chosen else [*chosen, key]
        await state.update_data({field: chosen})
        await ask(callback.message, state, callback.from_user, step, edit=True)

    @router.callback_query(StateFilter(Brief.goals, Brief.features, Brief.langs), F.data == "b:next")
    async def multi_done(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        step = await current_step(state)
        if not isinstance(callback.message, Message):
            return
        if step == "langs" and not (await state.get_data()).get("langs"):
            await state.update_data(langs=["ru"])          # язык не выбран — сайт на русском, как и на сайте
        await advance(callback.message, state, callback.from_user, step)

    @router.callback_query(StateFilter(Brief.brand, Brief.content, Brief.deadline, Brief.budget), F.data.regexp(r"^b:(brand|content|deadline|budget):\w+$"))
    async def choice(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        _, step, key = (callback.data or "").split(":")
        table = BUDGET if step == "budget" else CHOICES[step]
        if step != await current_step(state) or key not in table or not isinstance(callback.message, Message):
            return
        await state.update_data({step: key})
        await advance(callback.message, state, callback.from_user, step)

    @router.message(StateFilter(Brief.name), plain_text)
    async def got_name(message: Message, state: FSMContext) -> None:
        name = " ".join((message.text or "").split())[:80]
        if len(name) < 2:
            await message.answer(t("name_bad", prefs.lang(message.chat.id, message.from_user)))
            return
        await state.update_data(name=name)
        await advance(message, state, message.from_user, "name")

    @router.callback_query(StateFilter(Brief.name), F.data == "b:name:tg")
    async def name_from_telegram(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if isinstance(callback.message, Message):
            await state.update_data(name=" ".join((callback.from_user.full_name or "").split())[:80])
            await advance(callback.message, state, callback.from_user, "name")

    async def phone_done(message: Message, state: FSMContext, phone: str) -> None:
        lang = prefs.lang(message.chat.id, message.from_user)
        await state.update_data(phone=phone)
        note = t("phone_saved", lang, phone=phone) if phone else t("phone_skipped", lang)
        await message.answer(note, reply_markup=ReplyKeyboardRemove())   # убираем кнопку «Отправить номер»
        await advance(message, state, message.from_user, "phone")

    @router.message(StateFilter(Brief.phone), F.contact)
    async def got_contact(message: Message, state: FSMContext) -> None:
        raw = message.contact.phone_number if message.contact else ""
        await phone_done(message, state, raw if raw.startswith("+") else "+" + raw)

    @router.message(StateFilter(Brief.phone), plain_text)
    async def got_phone(message: Message, state: FSMContext) -> None:
        text, lang = (message.text or "").strip(), prefs.lang(message.chat.id, message.from_user)
        if text.lower() in SKIP_WORDS:
            if (await state.get_data()).get("tg"):
                await phone_done(message, state, "")
            else:
                await ask(message, state, message.from_user, "phone")
            return
        if sum(ch.isdigit() for ch in text) < 9:
            await message.answer(t("phone_bad", lang))
            return
        await phone_done(message, state, " ".join(text.split())[:30])

    # ---------- итог и отправка ----------
    async def summary_view(chat_id: int, user: User | None, data: dict) -> tuple[str, InlineKeyboardMarkup]:
        lang, cur, rate, prices = prefs.lang(chat_id, user), prefs.cur(chat_id), await rate_now(), pricing.get()
        is_bot = data.get("type") == "bot"
        est = estimate(data, prices)
        unset = t("not_set", lang)
        listed = lambda keys, table: ", ".join(word(table[k], lang) for k in keys if k in table) or unset
        feature_names = ", ".join(word(FEATURES[k]["label"], lang) for k in data.get("features", []) if k in FEATURES) or unset
        contact = ", ".join(x for x in (data.get("phone"), f"@{data['tg']}" if data.get("tg") else "") if x)
        rows = [("📁", "l_type", word(TYPES[data["type"]]["label"], lang)), ("🏷", "l_company", data.get("company") or unset),
                ("💼", "l_about", data.get("about") or unset), ("🎯", "l_goals", listed(data.get("goals", []), GOALS)),
                ("⚙️", "l_features", feature_names), ("🌐", "l_langs", listed(data.get("langs") or ["ru"], SITE_LANGS))]
        if not is_bot:
            rows += [("🎨", "l_brand", word(BRANDING[data["brand"]], lang) if data.get("brand") in BRANDING else unset),
                     ("📷", "l_content", word(CONTENT[data["content"]], lang) if data.get("content") in CONTENT else unset)]
        rows += [("🗓", "l_deadline", word(DEADLINE[data["deadline"]], lang) if data.get("deadline") in DEADLINE else unset),
                 ("💳", "l_budget", budget_label(data["budget"], lang, cur, rate) if data.get("budget") in BUDGET else unset),
                 ("👤", "l_contact", f"{data.get('name', '')}, {contact}" if contact else data.get("name", ""))]
        lines = [t("summary", lang), ""] + [f"{icon} {t(key, lang)}: {value}" for icon, key, value in rows]
        lines += ["", f"💰 {t('l_estimate', lang)}: {money(est['lo'], est['hi'], lang, cur, rate, prices)}, {weeks_text(est['wl'], est['wh'], lang)}"]
        if est["over"]:
            lines.append(t("over", lang))
        if cur == "usd":
            lines.append(rate_text(rate, lang))
        lines += ["", t("open_q", lang, n=len(open_questions(data, est)))]
        markup = kb([[(t("btn_send", lang), "b:send")], [currency_button(lang, cur, "sum")], [(t("btn_restart", lang), "menu:brief")]])
        return "\n".join(lines), markup

    @router.callback_query(StateFilter(Brief.confirm), F.data == "b:send")
    async def send(callback: CallbackQuery, state: FSMContext) -> None:
        if not isinstance(callback.message, Message):
            await callback.answer()
            return
        chat_id = callback.message.chat.id
        lang = prefs.lang(chat_id, callback.from_user)
        key = str(chat_id)
        if order_limit.blocked(key) and chat_id not in cfg_admins:
            await callback.answer()
            await callback.message.answer(t("too_many", lang))
            return
        await callback.answer()
        data = await state.get_data()
        prices, rate, cur = pricing.get(), await rate_now(), prefs.cur(chat_id)
        est = estimate(data, prices)
        number_, created = await storage.add_order(
            client_id=data["oid"], name=data.get("name", ""), phone=data.get("phone", ""), telegram=data.get("tg", ""),
            project=f"{TYPES[data['type']]['label'][0]} · из Telegram-бота", estimate=estimate_line(est),
            text=brief_doc(data, prices, rate, cur),
            brief_json=json.dumps({k: v for k, v in data.items() if k != "oid"}, ensure_ascii=False),
        )
        await state.clear()
        if created:
            order_limit.hit(key)
            log.info("заявка из бота сохранена: номер %s", number_)    # без имени и контактов
            wake_orders()
        await callback.message.answer(t("sent", lang, n=number_))

    @router.callback_query(F.data.startswith("b:"))
    async def stale(callback: CallbackQuery) -> None:
        """Кнопка из прошлого шага или прошлого ТЗ: подсказываем, как начать заново, и ничего не меняем."""
        user = callback.from_user
        chat_id = callback.message.chat.id if callback.message else (user.id if user else 0)
        await callback.answer(t("expired", prefs.lang(chat_id, user)), show_alert=False)

    return router
