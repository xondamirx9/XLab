"""Запасные ответы агентов без нейросети.

Нужны, когда ключа Claude нет, лимит вызовов на проект исчерпан или идут проверки. Ответы детерминированы:
тот же бриф даёт тот же сайт, а разные брифы — разные сайты (зерно берётся из названия и описания).
"""
from __future__ import annotations

import hashlib
import random
from typing import Any

from app.studio.spec import ARTS, HEROES, PRESETS

NICHES: list[tuple[tuple[str, ...], dict[str, Any]]] = [
    (("кофе", "кафе", "ресторан", "еда", "пекар", "кухн", "бар", "chaykhana", "osh", "плов"),
     {"icons": ["cup", "heart", "clock", "star", "map", "gift"], "preset": "editorial",
      "features": ["Свежие продукты каждое утро", "Авторские рецепты", "Уютный зал и терраса", "Доставка за 40 минут",
                   "Завтраки весь день", "Сертификаты в подарок"],
      "stats": [("12 000+", "гостей в месяц"), ("4,9", "средняя оценка"), ("40 мин", "доставка по городу")]}),
    (("салон", "красот", "барбер", "маникюр", "космет", "спа", "стрижк"),
     {"icons": ["scissors", "heart", "star", "clock", "gift", "shield"], "preset": "lux",
      "features": ["Мастера с опытом от 5 лет", "Премиальная косметика", "Онлайн-запись за минуту", "Стерильные инструменты",
                   "Программа лояльности", "Подарочные сертификаты"],
      "stats": [("8 лет", "работаем для вас"), ("5 000+", "довольных клиентов"), ("4,9", "рейтинг в картах")]}),
    (("it", "разработ", "софт", "приложен", "стартап", "saas", "ai", "ии", "digital", "технолог"),
     {"icons": ["code", "bolt", "shield", "chart", "rocket", "globe"], "preset": "tech",
      "features": ["Запуск MVP за 6 недель", "Архитектура, которая масштабируется", "Безопасность по умолчанию",
                   "Аналитика с первого дня", "Команда senior-инженеров", "Поддержка 24/7"],
      "stats": [("120+", "запущенных продуктов"), ("99,9%", "аптайм сервисов"), ("6 нед", "до первого релиза")]}),
    (("эко", "органик", "ферм", "растен", "цвет", "сад", "натурал"),
     {"icons": ["leaf", "heart", "shield", "home", "gift", "star"], "preset": "eco",
      "features": ["Только натуральное сырьё", "Своё хозяйство без химии", "Экологичная упаковка", "Доставка в день сбора",
                   "Честный состав", "Подписка со скидкой"],
      "stats": [("100%", "натуральный состав"), ("3 000+", "семей с нами"), ("24 ч", "от грядки до двери")]}),
    (("магазин", "одежд", "бренд", "shop", "товар", "маркет"),
     {"icons": ["cart", "star", "gift", "shield", "bolt", "heart"], "preset": "playful",
      "features": ["Новые коллекции каждый месяц", "Примерка перед оплатой", "Бесплатная доставка от 300 000 сум",
                   "Возврат 14 дней", "Оплата Click и Payme", "Бонусы за покупки"],
      "stats": [("25 000+", "заказов"), ("14 дней", "на возврат"), ("1 день", "доставка по Ташкенту")]}),
    (("строит", "ремонт", "недвиж", "дом", "квартир", "дизайн интерьер", "архитект"),
     {"icons": ["home", "shield", "chart", "clock", "check", "map"], "preset": "corporate",
      "features": ["Смета без скрытых платежей", "Фиксированные сроки в договоре", "Гарантия 5 лет", "Собственная бригада",
                   "3D-визуализация до старта", "Отчёты каждую неделю"],
      "stats": [("15 лет", "на рынке"), ("340", "сданных объектов"), ("5 лет", "гарантии")]}),
    (("школ", "курс", "обучен", "репетит", "академ", "образован"),
     {"icons": ["book", "star", "chart", "chat", "rocket", "check"], "preset": "aurora",
      "features": ["Практика с первого занятия", "Преподаватели-практики", "Маленькие группы до 8 человек",
                   "Сертификат по окончании", "Помощь с трудоустройством", "Пробный урок бесплатно"],
      "stats": [("2 500+", "выпускников"), ("92%", "доходят до конца"), ("8", "человек в группе")]}),
]
DEFAULT_NICHE = {"icons": ["spark", "shield", "clock", "star", "chat", "check"], "preset": "aurora",
                 "features": ["Индивидуальный подход", "Прозрачные цены", "Точно в срок", "Гарантия качества",
                              "Всегда на связи", "Опыт и репутация"],
                 "stats": [("10 лет", "опыта"), ("1 000+", "клиентов"), ("98%", "рекомендуют нас")]}

STYLE_WORDS = {"lux": ("люкс", "премиум", "элегант", "дорог", "минимал"), "tech": ("техно", "неон", "футур", "тёмн", "темн", "кибер"),
               "playful": ("ярк", "игрив", "весел", "детск", "фан"), "eco": ("эко", "природ", "органи", "натурал"),
               "brutal": ("брутал", "дерзк", "смел", "панк"), "editorial": ("журнал", "редакц", "классич", "газет"),
               "aurora": ("нежн", "градиент", "пастел", "мягк"), "corporate": ("делов", "строг", "корпорат", "бизнес")}


PRICING = {"type": "pricing", "eyebrow": "Цены", "title": "Простые и *честные* цены", "text": "Без скрытых платежей.",
           "items": [{"name": "Старт", "price": "от 300 000", "period": "сум", "features": ["Консультация", "Базовый пакет"]},
                     {"name": "Оптимальный", "price": "от 700 000", "period": "сум", "featured": True,
                      "features": ["Всё из «Старт»", "Приоритет", "Гарантия"]},
                     {"name": "Премиум", "price": "от 1 500 000", "period": "сум", "features": ["Всё из «Оптимальный»", "Личный менеджер"]}]}


def seed_of(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:8], 16)


def niche_profile(brief: dict) -> dict[str, Any]:
    text = f"{brief.get('niche', '')} {brief.get('description', '')} {brief.get('business', '')}".lower()
    for keys, profile in NICHES:
        if any(k in text for k in keys):
            return profile
    return DEFAULT_NICHE


def pick_preset(brief: dict) -> str:
    style = str(brief.get("style", "")).lower()
    if style in PRESETS:
        return style
    for preset, words in STYLE_WORDS.items():
        if any(w in style for w in words):
            return preset
    return niche_profile(brief)["preset"]


def concepts(brief: dict) -> list[dict[str, Any]]:
    rng = random.Random(seed_of(brief.get("business", ""), "concepts"))
    first = pick_preset(brief)
    others = [p for p in PRESETS if p != first]
    rng.shuffle(others)
    out = []
    for i, preset in enumerate([first, *others[:2]]):
        p = PRESETS[preset]
        out.append({"name": f"{p['title']}: {', '.join(p['mood'][:2])}", "preset": preset,
                    "idea": f"Сайт в духе «{p['title'].lower()}»: {', '.join(p['mood'])}. Первый экран — {p['hero']}, графика — {p['art']}.",
                    "hero": p["hero"] if i == 0 else rng.choice(HEROES), "art": p["art"] if i == 0 else rng.choice(ARTS)})
    return out


def full_spec(brief: dict, preset: str | None = None) -> dict[str, Any]:
    """Полная спецификация сайта из брифа — то, что агенты собирают по частям."""
    preset = preset or pick_preset(brief)
    profile = niche_profile(brief)
    name = brief.get("business") or "Ваш бренд"
    niche = brief.get("niche") or "услуги"
    city = brief.get("city") or "Ташкент"
    rng = random.Random(seed_of(name, niche, brief.get("description", "")))
    icons, features = profile["icons"], profile["features"]
    items = [{"icon": icons[i % len(icons)], "title": features[i],
              "text": f"{features[i]} — то, за что нас выбирают. Мы держим планку на каждом шаге и отвечаем за результат."}
             for i in range(min(6, len(features)))]
    desc = brief.get("description") or f"{niche} в городе {city}"
    p = PRESETS[preset]
    sections = [
        {"type": "features", "layout": rng.choice(["bento", "bento", "cards", "list"]), "eyebrow": "Почему мы",
         "title": f"Всё, чтобы вы *возвращались* к {name}", "text": desc, "items": items},
        {"type": "stats", "items": [{"value": v, "label": l} for v, l in profile["stats"]]},
        {"type": "process", "eyebrow": "Как это работает", "title": "Четыре шага до *результата*",
         "text": "Понятный путь без сюрпризов: вы всегда знаете, что происходит и что будет дальше.",
         "items": [{"title": "Знакомство", "text": "Обсуждаем задачу и ожидания, отвечаем на вопросы."},
                   {"title": "Предложение", "text": "Готовим решение с ценой и сроками — без мелкого шрифта."},
                   {"title": "Работа", "text": "Делаем и держим вас в курсе на каждом этапе."},
                   {"title": "Результат", "text": "Сдаём работу и остаёмся на связи после."}]},
        {"type": "showcase", "eyebrow": "Работы", "title": "Чем мы *гордимся*", "text": "Несколько историй наших клиентов.",
         "items": [{"title": f"Проект {chr(65 + i)}", "tag": t, "text": "Задача, решение и результат — коротко и по делу."}
                   for i, t in enumerate(["Новинка", "Хит", "Кейс", "Отзыв"])]},
        {"type": "testimonials", "eyebrow": "Отзывы", "title": "Нам *доверяют*",
         "items": [{"quote": q, "name": n, "role": r} for q, n, r in [
             ("Всё сделали быстро и аккуратно, общаться было одно удовольствие. Рекомендую друзьям.", "Мадина Каримова", "клиент"),
             ("Лучший сервис в городе: внимательно, честно и без лишних слов.", "Тимур Ахмедов", "постоянный клиент"),
             ("Результат превзошёл ожидания. Отдельное спасибо за терпение к моим правкам.", "Анна Ким", "клиент"),
             ("Работают как часы. Уже третий раз обращаемся — и снова довольны.", "Шахзод Усманов", "партнёр")]]},
        {"type": "faq", "eyebrow": "Вопросы", "title": "Частые *вопросы*",
         "items": [{"q": "Сколько это стоит?", "a": "Цена зависит от задачи. Оставьте заявку — назовём точную сумму в течение дня."},
                   {"q": "Как быстро вы начинаете?", "a": "Обычно в течение одного-двух дней после согласования."},
                   {"q": "Есть ли гарантия?", "a": "Да, мы отвечаем за результат и фиксируем условия письменно."},
                   {"q": "Как с вами связаться?", "a": "Через форму на сайте, по телефону или в Telegram — как вам удобнее."}]},
        {"type": "cta", "title": "Готовы *начать*?", "text": "Оставьте заявку — ответим в течение рабочего дня.", "button": "Оставить заявку"},
        {"type": "contact", "eyebrow": "Контакты", "title": "Давайте *обсудим*", "text": "Ответим быстро и по делу."},
    ]
    if brief.get("with_pricing"):
        sections.insert(5, PRICING)
    contacts = brief.get("contacts") or {}
    return {
        "brand": {"name": name, "tagline": brief.get("tagline", ""), "niche": niche, "city": city, "lang": brief.get("lang", "ru")},
        "style": {"preset": preset, "hero": p["hero"], "art": p["art"], "radius": p["radius"], "density": p["density"],
                  "effects": list(p["effects"]), "mood": list(p["mood"]), "art_seed": rng.randrange(10_000_000), "art_hue": rng.randrange(360)},
        "palette": dict(p["palette"]),
        "fonts": {"display": p["fonts"][0], "body": p["fonts"][1]},
        "copy": {"eyebrow": f"{niche} · {city}", "hero_title": f"{name} — *{p['mood'][0]}* в каждой детали",
                 "hero_lead": f"{desc}. Делаем так, чтобы вам хотелось вернуться.", "cta_primary": "Оставить заявку",
                 "cta_secondary": "Узнать больше", "trust": [f["title"] for f in items[:5]]},
        "sections": sections,
        "contacts": contacts,
        "seo": {"title": f"{name} — {niche}, {city}", "description": f"{name}: {desc}. Оставьте заявку на сайте.",
                "keywords": [name, niche, city]},
    }
