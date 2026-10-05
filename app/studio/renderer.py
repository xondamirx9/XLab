"""Рендерер: превращает DesignSpec в готовый одностраничный сайт (один HTML-файл, без внешних скриптов).

Все тексты экранируются. Скрипт на странице всегда один и тот же (assets/runtime.js), поэтому
сервер разрешает только его — по хешу в Content-Security-Policy.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.studio.art import render_art
from app.studio.icons import icon
from app.studio.spec import DENSITIES, FONTS, RADII, alt_palette, is_dark, normalize, on_color

ASSETS = Path(__file__).resolve().parent / "assets"
SITE_CSS = (ASSETS / "site.css").read_text(encoding="utf-8")
RUNTIME_JS = (ASSETS / "runtime.js").read_text(encoding="utf-8")
RUNTIME_HASH = "sha256-" + base64.b64encode(hashlib.sha256(RUNTIME_JS.encode("utf-8")).digest()).decode()
SERIF_FONTS = {"Playfair Display", "Cormorant Garamond", "Prata", "Literata", "Lora", "PT Serif", "Roboto Slab",
               "Yeseva One", "Forum", "Spectral", "Alice", "Unna"}

UI = {
    "ru": {"skip": "К содержанию", "menu": "Меню", "theme": "Сменить тему", "name": "Ваше имя", "phone": "Телефон",
           "message": "Коротко о задаче", "send": "Отправить заявку", "sent": "Спасибо! Мы скоро свяжемся с вами.",
           "failed": "Не получилось отправить. Откроем Telegram или почту.", "invalid": "Укажите имя и телефон.",
           "rights": "Все права защищены", "popular": "Популярный", "hours": "Часы работы", "address": "Адрес",
           "write": "Написать", "call": "Позвонить", "top": "Наверх"},
    "uz": {"skip": "Asosiy qismga", "menu": "Menyu", "theme": "Mavzuni almashtirish", "name": "Ismingiz", "phone": "Telefon",
           "message": "Vazifa haqida qisqacha", "send": "Ariza yuborish", "sent": "Rahmat! Tez orada bog‘lanamiz.",
           "failed": "Yuborib bo‘lmadi. Telegram yoki pochtani ochamiz.", "invalid": "Ism va telefonni kiriting.",
           "rights": "Barcha huquqlar himoyalangan", "popular": "Ommabop", "hours": "Ish vaqti", "address": "Manzil",
           "write": "Yozish", "call": "Qo‘ng‘iroq", "top": "Yuqoriga"},
    "en": {"skip": "Skip to content", "menu": "Menu", "theme": "Switch theme", "name": "Your name", "phone": "Phone",
           "message": "Tell us briefly", "send": "Send request", "sent": "Thank you! We will get back to you soon.",
           "failed": "Could not send. Opening Telegram or email instead.", "invalid": "Please enter your name and phone.",
           "rights": "All rights reserved", "popular": "Popular", "hours": "Opening hours", "address": "Address",
           "write": "Message", "call": "Call", "top": "Back to top"},
}
NAV_LABELS = {
    "ru": {"features": "Преимущества", "process": "Как работаем", "showcase": "Работы", "testimonials": "Отзывы",
           "pricing": "Цены", "faq": "Вопросы", "contact": "Контакты"},
    "uz": {"features": "Afzalliklar", "process": "Jarayon", "showcase": "Ishlar", "testimonials": "Fikrlar",
           "pricing": "Narxlar", "faq": "Savollar", "contact": "Aloqa"},
    "en": {"features": "Benefits", "process": "Process", "showcase": "Work", "testimonials": "Reviews",
           "pricing": "Pricing", "faq": "FAQ", "contact": "Contact"},
}


def e(text: Any) -> str:
    return escape(str(text or ""), quote=True)


def rich(text: str) -> str:
    """Экранирует текст и превращает *слово* в акцент (градиент)."""
    return re.sub(r"\*([^*]{1,60})\*", r"<em>\1</em>", e(text))


def plain(text: str) -> str:
    return text.replace("*", "")


def kinetic(text: str) -> str:
    """Каждое слово заголовка выезжает снизу со своей задержкой."""
    out, n = [], 0
    for part in re.split(r"(\*[^*]{1,60}\*)", text):
        if not part:
            continue
        accent = part.startswith("*") and part.endswith("*")
        for word in plain(part).split():
            inner = f"<em>{e(word)}</em>" if accent else e(word)
            out.append(f'<span class="word"><span style="--w:{n}">{inner}</span></span>')
            n += 1
    return " ".join(out)


def _vars(p: dict[str, str]) -> str:
    return (f"--bg:{p['bg']};--surface:{p['surface']};--text:{p['text']};--muted:{p['muted']};--primary:{p['primary']};"
            f"--accent:{p['accent']};--accent2:{p['accent2']};--on-primary:{on_color(p['primary'])};"
            f"--line:color-mix(in srgb,{p['text']} 13%,transparent);--blend:{'screen' if is_dark(p['bg']) else 'multiply'};"
            f"--grain-op:{'.07' if is_dark(p['bg']) else '.05'};color-scheme:{'dark' if is_dark(p['bg']) else 'light'}")


def _fonts_link(spec: dict) -> str:
    families = []
    for name in dict.fromkeys((spec["fonts"]["display"], spec["fonts"]["body"])):
        axis = FONTS.get(name, "")
        families.append("family=" + quote(name).replace("%20", "+") + (f":{axis}" if axis else ""))
    return "https://fonts.googleapis.com/css2?" + "&amp;".join(families) + "&amp;display=swap"


def _section_id(kind: str) -> str:
    return {"features": "benefits", "showcase": "work", "testimonials": "reviews"}.get(kind, kind)


# ---------- первый экран ----------
def _hero(spec: dict, ui: dict) -> str:
    c, st, pal = spec["copy"], spec["style"], spec["palette"]
    seed = st["art_seed"]
    art = render_art(st["art"], pal, seed, st["art_hue"], uid="hero")
    variant = st["hero"]
    title = kinetic(c["hero_title"])
    chips = "".join(f'<span class="chip"><i></i>{e(m)}</span>' for m in (c["trust"][:3] or st["mood"][:3]))
    eyebrow = f'<span class="eyebrow">{e(c["eyebrow"])}</span>' if c["eyebrow"] else ""
    actions = (f'<div class="hero-actions" data-reveal style="--d:6"><a class="btn btn-primary" href="#contact">{e(c["cta_primary"])}{icon("arrow")}</a>'
               f'<a class="btn btn-ghost" href="#{_first_anchor(spec)}">{e(c["cta_secondary"])}</a></div>')
    lead = f'<p class="hero-lead" data-reveal style="--d:4">{e(c["hero_lead"])}</p>' if c["hero_lead"] else ""
    stat = next((s for s in spec["sections"] if s["type"] == "stats" and s["items"]), None)

    if variant == "aurora":
        return (f'<section class="hero hero-aurora" id="top"><div class="aurora" aria-hidden="true"><span></span><span></span><span></span></div>'
                f'<div class="hero-art">{art}</div><div class="wrap"><div class="hero-copy"><div class="hero-meta" data-reveal>{chips}</div>'
                f'<h1 class="hero-title">{title}</h1>{lead}{actions}</div></div><span class="scroll-hint" aria-hidden="true"></span></section>')
    if variant == "split":
        badges = ""
        if stat:
            a = stat["items"][0]
            badges += f'<div class="badge-float badge-a"><b data-count="{e(a["value"])}">{e(a["value"])}</b><small>{e(a["label"])}</small></div>'
            if len(stat["items"]) > 1:
                b = stat["items"][1]
                badges += f'<div class="badge-float badge-b"><b data-count="{e(b["value"])}">{e(b["value"])}</b><small>{e(b["label"])}</small></div>'
        return (f'<section class="hero hero-split" id="top"><div class="wrap"><div class="hero-copy">{eyebrow}<div class="hero-meta" data-reveal></div>'
                f'<h1 class="hero-title">{title}</h1>{lead}{actions}</div>'
                f'<div style="position:relative;--d:3" data-reveal><div class="frame" data-parallax="0.08">{art}</div>{badges}</div></div></section>')
    if variant == "kinetic":
        mood = st["mood"] or [spec["brand"]["name"]]
        band_items = "".join(f'<span class="trust-item">{icon("spark")}{e(m)}</span>' for m in (mood * 4)[:8])
        band = (f'<div class="band" aria-hidden="true"><div class="marquee" style="--speed:26s"><div class="marquee-track">{band_items}</div>'
                f'<div class="marquee-track">{band_items}</div></div></div>') if "marquee" in st["effects"] else ""
        return (f'<section class="hero hero-kinetic" id="top">{band}<div class="wrap">{eyebrow}<h1 class="hero-title">{title}</h1>'
                f'<div class="hero-row"><div>{lead}{actions}</div><div class="orb" aria-hidden="true">{art}</div></div></div></section>')
    if variant == "spotlight":
        return (f'<section class="hero hero-spotlight" id="top"><div class="hero-art">{art}</div><div class="wrap"><div class="hero-copy">'
                f'<div class="hero-meta" data-reveal>{chips}</div><h1 class="hero-title">{title}</h1>{lead}{actions}</div></div>'
                f'<span class="scroll-hint" aria-hidden="true"></span></section>')
    if variant == "glass":
        return (f'<section class="hero hero-glass" id="top"><div class="hero-art">{art}</div><div class="wrap"><div class="glass" data-reveal>'
                f'{eyebrow}<div class="hero-meta" style="margin-top:18px">{chips}</div><h1 class="hero-title">{title}</h1>{lead}{actions}</div></div></section>')
    # editorial
    year = datetime.now().year
    city = spec["brand"]["city"]
    return (f'<section class="hero hero-editorial" id="top"><div class="wrap"><div class="issue"><span>{e(spec["brand"]["name"])}</span>'
            f'<span>{e(city) + " · " if city else ""}{year}</span></div><div class="hero-copy">{eyebrow}<h1 class="hero-title">{title}</h1>{lead}{actions}</div>'
            f'<div class="arch" data-parallax="0.06" data-reveal>{art}</div></div></section>')


def _first_anchor(spec: dict) -> str:
    for s in spec["sections"]:
        if s["type"] not in ("cta",):
            return _section_id(s["type"])
    return "contact"


def _head(sec: dict, center: bool = False, light: str = "") -> str:
    eyebrow = f'<span class="eyebrow">{e(sec["eyebrow"])}</span>' if sec.get("eyebrow") else ""
    title = f'<h2 class="sec-title">{rich(sec["title"])}</h2>' if sec.get("title") else ""
    text = f'<p class="lead">{e(sec["text"])}</p>' if sec.get("text") else ""
    return f'<div class="sec-head{" center" if center else ""}" data-reveal>{eyebrow}{title}{text}{light}</div>'


# ---------- секции ----------
def _features(sec: dict, spec: dict, ui: dict) -> str:
    items, layout = sec["items"], sec["layout"]
    if layout == "list":
        rows = "".join(f'<div class="row" data-reveal style="--d:{i}"><span class="num">{i + 1:02d}</span><h3>{e(it["title"])}</h3>'
                       f'<p>{e(it["text"])}</p>{icon("arrow")}</div>' for i, it in enumerate(items))
        body = f'<div class="rows">{rows}</div>'
    else:
        cards = []
        for i, it in enumerate(items):
            extra = ""
            if layout == "bento" and i == 0:
                st = spec["style"]
                extra = f'<div class="card-art" aria-hidden="true">{render_art(st["art"], spec["palette"], st["art_seed"] + 7, st["art_hue"], uid="bento")}</div>'
            cards.append(f'<article class="card" data-reveal style="--d:{i}">{extra}<div class="ico-box">{icon(it["icon"])}</div>'
                         f'<h3>{e(it["title"])}</h3><p>{e(it["text"])}</p></article>')
        body = f'<div class="{"bento" if layout == "bento" and len(items) >= 4 else "cards3"}">{"".join(cards)}</div>'
    return f'<section class="sec" id="{_section_id("features")}"><div class="wrap">{_head(sec)}{body}</div></section>'


def _stats(sec: dict, spec: dict, ui: dict) -> str:
    cells = "".join(f'<div class="stat" data-reveal style="--d:{i}"><b data-count="{e(it["value"])}">{e(it["value"])}</b><span>{e(it["label"])}</span></div>'
                    for i, it in enumerate(sec["items"]))
    head = _head(sec) if sec.get("title") else ""
    return f'<section class="sec" id="stats" style="padding-block:calc(var(--space) * 70px)"><div class="wrap">{head}<div class="stats">{cells}</div></div></section>'


def _process(sec: dict, spec: dict, ui: dict) -> str:
    steps = "".join(f'<div class="step card" data-n="{i + 1}" data-reveal><h3>{e(it["title"])}</h3><p>{e(it["text"])}</p></div>'
                    for i, it in enumerate(sec["items"]))
    return f'<section class="sec" id="process"><div class="wrap process">{_head(sec)}<div class="steps">{steps}</div></div></section>'


def _showcase(sec: dict, spec: dict, ui: dict) -> str:
    st, arts = spec["style"], ("blobs", "waves", "orbits", "topo", "rays", "grid")
    cards = []
    for i, it in enumerate(sec["items"]):
        art = render_art(arts[(arts.index(st["art"]) + i) % len(arts)], spec["palette"], st["art_seed"] + 101 * (i + 1), st["art_hue"] + i * 3, uid=f"w{i}")
        tag = f'<span class="chip"><i></i>{e(it["tag"])}</span>' if it["tag"] else ""
        cards.append(f'<article class="card work" data-reveal style="--d:{i}"><div class="work-art">{art}</div>'
                     f'<div class="work-body">{tag}<h3>{e(it["title"])}</h3><p>{e(it["text"])}</p></div></article>')
    return (f'<section class="sec" id="{_section_id("showcase")}"><div class="wrap">{_head(sec)}'
            f'<div class="track" tabindex="0" aria-label="{e(plain(sec["title"]))}">{"".join(cards)}</div></div></section>')


def _testimonials(sec: dict, spec: dict, ui: dict) -> str:
    def card(it: dict) -> str:
        initials = "".join(w[0] for w in it["name"].split()[:2]).upper() or "★"
        role = f'<small>{e(it["role"])}</small>' if it["role"] else ""
        return (f'<figure class="card quote"><blockquote>«{e(it["quote"])}»</blockquote><figcaption><span class="avatar" aria-hidden="true">{e(initials)}</span>'
                f'<span><b>{e(it["name"])}</b>{role}</span></figcaption></figure>')
    items = sec["items"]
    head = f'<div class="wrap">{_head(sec, center=True)}</div>'
    if "marquee" in spec["style"]["effects"] and len(items) >= 3:
        half = (len(items) + 1) // 2
        rows = ""
        for k, chunk in enumerate((items[:half], items[half:] or items[:half])):
            cards = "".join(card(it) for it in chunk * (2 if len(chunk) < 3 else 1))
            rows += (f'<div class="marquee{" reverse" if k else ""}" style="--speed:{40 + k * 8}s"><div class="marquee-track">{cards}</div>'
                     f'<div class="marquee-track" aria-hidden="true">{cards}</div></div>')
        return f'<section class="sec" id="{_section_id("testimonials")}">{head}<div class="quotes">{rows}</div></section>'
    cards = "".join(card(it).replace('class="card quote"', f'class="card quote" data-reveal style="--d:{i};width:auto"')
                    for i, it in enumerate(items))
    return f'<section class="sec" id="{_section_id("testimonials")}">{head}<div class="wrap"><div class="cards3">{cards}</div></div></section>'


def _pricing(sec: dict, spec: dict, ui: dict) -> str:
    plans = []
    for i, it in enumerate(sec["items"]):
        feats = "".join(f"<li>{icon('check')}<span>{e(f)}</span></li>" for f in it["features"])
        tag = f'<span class="tag">{e(ui["popular"])}</span>' if it["featured"] else ""
        btn = "btn-primary" if it["featured"] else "btn-ghost"
        plans.append(f'<article class="card plan{" featured" if it["featured"] else ""}" data-reveal style="--d:{i}">{tag}<h3>{e(it["name"])}</h3>'
                     f'<div class="price">{e(it["price"])}</div><div class="period">{e(it["period"])}</div><ul>{feats}</ul>'
                     f'<a class="btn {btn}" href="#contact">{e(spec["copy"]["cta_primary"])}{icon("arrow")}</a></article>')
    return f'<section class="sec" id="pricing"><div class="wrap">{_head(sec, center=True)}<div class="plans">{"".join(plans)}</div></div></section>'


def _faq(sec: dict, spec: dict, ui: dict) -> str:
    items = "".join(f'<details data-reveal style="--d:{i}"><summary>{e(it["q"])}{icon("plus")}</summary>'
                    f'<div class="answer"><div>{e(it["a"])}</div></div></details>' for i, it in enumerate(sec["items"]))
    return f'<section class="sec" id="faq"><div class="wrap faq">{_head(sec)}<div>{items}</div></div></section>'


def _cta(sec: dict, spec: dict, ui: dict) -> str:
    st = spec["style"]
    art = render_art(st["art"], spec["palette"], st["art_seed"] + 999, st["art_hue"], uid="cta")
    text = f"<p>{e(sec['text'])}</p>" if sec["text"] else ""
    return (f'<section class="sec" id="cta"><div class="wrap"><div class="cta-box" data-reveal>{art}<h2>{rich(sec["title"] or spec["copy"]["hero_title"])}</h2>'
            f'{text}<a class="btn" href="#contact">{e(sec.get("button") or spec["copy"]["cta_primary"])}{icon("arrow")}</a></div></div></section>')


def _contact(sec: dict, spec: dict, ui: dict) -> str:
    ct = spec["contacts"]
    items = []
    if ct["phone"]:
        tel = re.sub(r"[^\d+]", "", ct["phone"])
        items.append(f'<a class="contact-item" href="tel:{e(tel)}">{icon("phone")}<span><small>{e(ui["call"])}</small>{e(ct["phone"])}</span></a>')
    if ct["telegram"]:
        items.append(f'<a class="contact-item" href="https://t.me/{e(ct["telegram"])}" target="_blank" rel="noopener">{icon("telegram")}<span><small>Telegram</small>@{e(ct["telegram"])}</span></a>')
    if ct["instagram"]:
        items.append(f'<a class="contact-item" href="https://instagram.com/{e(ct["instagram"])}" target="_blank" rel="noopener">{icon("instagram")}<span><small>Instagram</small>@{e(ct["instagram"])}</span></a>')
    if ct["email"]:
        items.append(f'<a class="contact-item" href="mailto:{e(ct["email"])}">{icon("mail")}<span><small>E-mail</small>{e(ct["email"])}</span></a>')
    if ct["address"]:
        items.append(f'<div class="contact-item">{icon("map")}<span><small>{e(ui["address"])}</small>{e(ct["address"])}</span></div>')
    if ct["hours"]:
        items.append(f'<div class="contact-item">{icon("clock")}<span><small>{e(ui["hours"])}</small>{e(ct["hours"])}</span></div>')
    form = (f'<form class="form" data-lead-form novalidate data-reveal><div class="field"><input id="f-name" name="name" placeholder=" " autocomplete="name" required maxlength="80">'
            f'<label for="f-name">{e(ui["name"])}</label></div><div class="field"><input id="f-phone" name="phone" type="tel" placeholder=" " autocomplete="tel" required maxlength="30">'
            f'<label for="f-phone">{e(ui["phone"])}</label></div><div class="field"><textarea id="f-msg" name="message" placeholder=" " maxlength="1000"></textarea>'
            f'<label for="f-msg">{e(ui["message"])}</label></div>'
            f'<input class="sr" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">'
            f'<button class="btn btn-primary" type="submit">{e(ui["send"])}{icon("arrow")}</button><p class="form-note" role="status" aria-live="polite"></p></form>')
    head = _head(sec if sec.get("title") else {**sec, "title": NAV_LABELS[spec["brand"]["lang"]]["contact"]})
    return f'<section class="sec" id="contact"><div class="wrap contact"><div>{head}<div class="contact-list">{"".join(items)}</div></div>{form}</div></section>'


RENDERERS = {"features": _features, "stats": _stats, "process": _process, "showcase": _showcase, "testimonials": _testimonials,
             "pricing": _pricing, "faq": _faq, "cta": _cta, "contact": _contact}


def render_site(raw_spec: dict, *, slug: str = "", lead_endpoint: str = "") -> str:
    spec = normalize(raw_spec)
    lang = spec["brand"]["lang"]
    ui, nav_labels = UI[lang], NAV_LABELS[lang]
    pal, st, brand = spec["palette"], spec["style"], spec["brand"]
    alt = alt_palette(pal)
    display_serif = spec["fonts"]["display"] in SERIF_FONTS
    heavy = spec["fonts"]["display"] in {"Russo One", "Yeseva One", "Prata", "Forum", "Alice", "Tenor Sans"}
    root = (f':root{{{_vars(pal)};--font-d:"{spec["fonts"]["display"]}";--font-b:"{spec["fonts"]["body"]}";'
            f'--d-weight:{400 if heavy else (600 if display_serif else 700)};--r:{RADII[st["radius"]]};--r-sm:calc({RADII[st["radius"]]} * .6);'
            f'--r-btn:{"999px" if st["radius"] in ("round", "pill") else RADII[st["radius"]]};--space:{DENSITIES[st["density"]]};'
            f'--gutter:clamp(16px,4vw,40px);--kinetic-case:{"uppercase" if st["preset"] == "brutal" else "none"}}}'
            f'html.alt{{{_vars(alt)}}}')

    nav_links = "".join(f'<a href="#{_section_id(s["type"])}">{e(nav_labels[s["type"]])}</a>'
                        for s in spec["sections"] if s["type"] in nav_labels and s["type"] != "contact")[:2000]
    initial = (brand["name"][:1] or "•").upper()
    header = (f'<header class="nav"><div class="wrap nav-in"><a class="logo" href="#top"><span class="logo-mark" aria-hidden="true">{e(initial)}</span>{e(brand["name"])}</a>'
              f'<nav class="links" aria-label="{e(ui["menu"])}">{nav_links}<a href="#contact">{e(nav_labels["contact"])}</a></nav>'
              f'<button class="icon-btn" type="button" data-theme-toggle aria-label="{e(ui["theme"])}">{icon("sun")}</button>'
              f'<a class="btn btn-primary btn-sm nav-cta" href="#contact">{e(spec["copy"]["cta_primary"])}</a>'
              f'<button class="icon-btn burger" type="button" aria-expanded="false" aria-label="{e(ui["menu"])}">{icon("plus")}</button></div></header>')

    trust = ""
    if spec["copy"]["trust"]:
        items = "".join(f'<span class="trust-item">{icon("spark")}{e(t)}</span>' for t in spec["copy"]["trust"])
        if "marquee" in st["effects"]:
            trust = (f'<div class="trust"><div class="marquee"><div class="marquee-track">{items}</div>'
                     f'<div class="marquee-track" aria-hidden="true">{items}</div></div></div>')
        else:
            trust = f'<div class="trust"><div class="wrap" style="display:flex;flex-wrap:wrap;gap:28px;justify-content:center">{items}</div></div>'

    sections = "".join(RENDERERS[s["type"]](s, spec, ui) for s in spec["sections"])
    ct = spec["contacts"]
    socials = ""
    if ct["telegram"]:
        socials += f'<a class="icon-btn" href="https://t.me/{e(ct["telegram"])}" aria-label="Telegram" target="_blank" rel="noopener">{icon("telegram")}</a>'
    if ct["instagram"]:
        socials += f'<a class="icon-btn" href="https://instagram.com/{e(ct["instagram"])}" aria-label="Instagram" target="_blank" rel="noopener">{icon("instagram")}</a>'
    footer = (f'<footer class="footer"><div class="wrap"><div class="wordmark" aria-hidden="true">{e(brand["name"])}</div><div class="foot-row">'
              f'<span>© {datetime.now().year} {e(brand["name"])}. {e(ui["rights"])}</span><div class="socials">{socials}'
              f'<a class="icon-btn" href="#top" aria-label="{e(ui["top"])}" style="rotate:-90deg">{icon("arrow")}</a></div></div></div></footer>')

    body_cls = " ".join([f"fx-{fx}" for fx in st["effects"]] + (["font-serif"] if display_serif else []))
    data = {"slug": slug, "lead": lead_endpoint, "telegram": ct["telegram"], "email": ct["email"],
            "t": {k: ui[k] for k in ("sent", "failed", "invalid")}}
    data_json = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    seo = spec["seo"]
    ld = {"@context": "https://schema.org", "@type": "LocalBusiness", "name": brand["name"], "description": seo["description"]}
    if ct["phone"]:
        ld["telephone"] = ct["phone"]
    if ct["address"] or brand["city"]:
        ld["address"] = {"@type": "PostalAddress", "streetAddress": ct["address"], "addressLocality": brand["city"]}
    ld_json = json.dumps(ld, ensure_ascii=False).replace("<", "\\u003c")
    keywords = f'<meta name="keywords" content="{e(", ".join(seo["keywords"]))}">' if seo["keywords"] else ""
    favicon = ("data:image/svg+xml," + quote(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="9" fill="{pal["primary"]}"/>'
                                             f'<text x="16" y="22" font-size="17" font-family="sans-serif" font-weight="700" text-anchor="middle" fill="{on_color(pal["primary"])}">{e(initial)}</text></svg>'))

    return (f'<!doctype html>\n<html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
            f'<title>{e(plain(seo["title"]))}</title><meta name="description" content="{e(seo["description"])}">{keywords}'
            f'<meta property="og:title" content="{e(plain(seo["title"]))}"><meta property="og:description" content="{e(seo["description"])}"><meta property="og:type" content="website">'
            f'<meta name="theme-color" content="{pal["bg"]}"><link rel="icon" href="{favicon}">'
            f'<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
            f'<link href="{_fonts_link(spec)}" rel="stylesheet"><script type="application/ld+json">{ld_json}</script>'
            f'<style>{root}{SITE_CSS}</style></head><body class="{body_cls}"><a class="skip" href="#main">{e(ui["skip"])}</a>'
            f'<div class="progress" aria-hidden="true"></div><div class="cursor" aria-hidden="true"></div><div class="cursor-dot" aria-hidden="true"></div>'
            f'{header}<main id="main">{_hero(spec, ui)}{trust}{sections}</main>{footer}'
            f'<script type="application/json" id="wl-data">{data_json}</script><script id="wl-runtime">{RUNTIME_JS}</script></body></html>')


def site_csp() -> str:
    """Политика для сайтов клиентов: из скриптов разрешён только наш runtime, внешние запросы — только на свой сервер."""
    return ("default-src 'self'; script-src '" + RUNTIME_HASH + "'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'")
