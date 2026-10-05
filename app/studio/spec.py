"""Спецификация сайта (DesignSpec): что агенты решают, а рендерер превращает в страницу.

Агенты отдают только данные — тексты, цвета и выбор из списков. HTML, CSS и JS пишет рендерер,
поэтому чужой код в сайт клиента попасть не может, а качество вёрстки не зависит от удачи модели.
"""
from __future__ import annotations

import colorsys
import copy
import re
from typing import Any

LANGS = ("ru", "uz", "en")
HEROES = ("aurora", "split", "kinetic", "spotlight", "glass", "editorial")
ARTS = ("blobs", "waves", "grid", "orbits", "rays", "topo")
RADII = {"sharp": "2px", "soft": "10px", "round": "20px", "pill": "32px"}
DENSITIES = {"airy": 1.25, "balanced": 1.0, "compact": 0.82}
EFFECTS = ("grain", "cursor", "tilt", "magnetic", "marquee", "parallax")
SECTION_TYPES = ("features", "stats", "process", "showcase", "testimonials", "pricing", "faq", "cta", "contact")
FEATURE_LAYOUTS = ("bento", "cards", "list")
ICONS = ("spark", "bolt", "shield", "heart", "leaf", "star", "clock", "chat", "cart", "chart", "globe", "camera",
         "cup", "scissors", "home", "car", "book", "code", "palette", "rocket", "check", "gift", "map", "phone")

# Шрифты с кириллицей (Google Fonts) и доступные начертания: лишнее начертание Google отклоняет целиком.
FONTS: dict[str, str] = {
    "Unbounded": "wght@400;500;600;700;800", "Playfair Display": "wght@400;500;600;700;800",
    "Cormorant Garamond": "wght@400;500;600;700", "Manrope": "wght@400;500;600;700;800",
    "Montserrat": "wght@400;500;600;700;800", "Onest": "wght@400;500;600;700",
    "Oswald": "wght@400;500;600;700", "Russo One": "", "Prata": "", "Literata": "wght@400;500;600;700",
    "Comfortaa": "wght@400;500;600;700", "Rubik": "wght@400;500;600;700;800", "Inter Tight": "wght@400;500;600;700;800",
    "Golos Text": "wght@400;500;600;700", "Tenor Sans": "", "Lora": "wght@400;500;600;700",
    "Raleway": "wght@400;500;600;700;800", "PT Serif": "wght@400;700", "Exo 2": "wght@400;500;600;700;800",
    "Jost": "wght@400;500;600;700", "Nunito": "wght@400;600;700;800", "IBM Plex Sans": "wght@400;500;600;700",
    "Roboto Slab": "wght@400;500;600;700", "Yeseva One": "", "Forum": "", "Spectral": "wght@400;500;600;700",
    "Alice": "", "Unna": "wght@400;700", "Ysabeau Office": "wght@400;500;600;700", "Geologica": "wght@400;500;600;700",
}

# Пресеты стилей: стартовая точка, от которой отталкиваются агенты. Пользователь выбирает пресет в боте.
PRESETS: dict[str, dict[str, Any]] = {
    "lux": {"title": "Минимализм люкс", "theme": "dark", "hero": "editorial", "art": "rays", "radius": "sharp",
            "density": "airy", "fonts": ("Cormorant Garamond", "Manrope"),
            "palette": {"bg": "#0E0D0B", "surface": "#191714", "text": "#F3EDE2", "muted": "#A89F90",
                        "primary": "#D4AF6A", "accent": "#8E6F3E", "accent2": "#F3EDE2"},
            "effects": ["grain", "cursor", "parallax", "magnetic"], "mood": ["тишина", "золото", "воздух"]},
    "tech": {"title": "Тёмный техно", "theme": "dark", "hero": "spotlight", "art": "grid", "radius": "soft",
             "density": "balanced", "fonts": ("Unbounded", "Inter Tight"),
             "palette": {"bg": "#07080D", "surface": "#10121B", "text": "#EEF1FF", "muted": "#8B93B5",
                         "primary": "#5EEAD4", "accent": "#8B5CF6", "accent2": "#F472B6"},
             "effects": ["cursor", "tilt", "magnetic", "marquee", "grain"], "mood": ["неон", "точность", "скорость"]},
    "playful": {"title": "Яркий игривый", "theme": "light", "hero": "kinetic", "art": "blobs", "radius": "pill",
                "density": "balanced", "fonts": ("Rubik", "Nunito"),
                "palette": {"bg": "#FFF8F0", "surface": "#FFFFFF", "text": "#1F1A2E", "muted": "#6B6480",
                            "primary": "#FF5A5F", "accent": "#7C3AED", "accent2": "#FFC93C"},
                "effects": ["tilt", "magnetic", "marquee"], "mood": ["энергия", "улыбка", "цвет"]},
    "eco": {"title": "Органика", "theme": "light", "hero": "split", "art": "topo", "radius": "round",
            "density": "airy", "fonts": ("Lora", "Jost"),
            "palette": {"bg": "#F6F1E7", "surface": "#FFFDF8", "text": "#22301F", "muted": "#6A7363",
                        "primary": "#2F6B3F", "accent": "#C8693F", "accent2": "#E6C27A"},
            "effects": ["grain", "parallax", "magnetic"], "mood": ["природа", "тепло", "честность"]},
    "brutal": {"title": "Брутализм", "theme": "light", "hero": "kinetic", "art": "grid", "radius": "sharp",
               "density": "compact", "fonts": ("Russo One", "IBM Plex Sans"),
               "palette": {"bg": "#F2F0E6", "surface": "#FFFFFF", "text": "#0A0A0A", "muted": "#4A4A44",
                           "primary": "#0A0A0A", "accent": "#C6F432", "accent2": "#FF4D00"},
               "effects": ["marquee", "magnetic", "cursor"], "mood": ["дерзость", "контраст", "прямота"]},
    "editorial": {"title": "Журнал", "theme": "light", "hero": "editorial", "art": "orbits", "radius": "sharp",
                  "density": "airy", "fonts": ("Playfair Display", "Golos Text"),
                  "palette": {"bg": "#FAF8F4", "surface": "#FFFFFF", "text": "#151412", "muted": "#6E6A62",
                              "primary": "#B3121B", "accent": "#151412", "accent2": "#E8A917"},
                  "effects": ["parallax", "magnetic", "grain"], "mood": ["история", "вкус", "ритм"]},
    "aurora": {"title": "Нежный градиент", "theme": "light", "hero": "aurora", "art": "waves", "radius": "round",
               "density": "balanced", "fonts": ("Manrope", "Onest"),
               "palette": {"bg": "#F7F5FF", "surface": "#FFFFFF", "text": "#1B1636", "muted": "#6D6890",
                           "primary": "#6D5BFF", "accent": "#FF7EB6", "accent2": "#4CC9F0"},
               "effects": ["tilt", "magnetic", "parallax"], "mood": ["мягкость", "свет", "мечта"]},
    "corporate": {"title": "Деловой", "theme": "light", "hero": "glass", "art": "waves", "radius": "soft",
                  "density": "balanced", "fonts": ("Montserrat", "Inter Tight"),
                  "palette": {"bg": "#F5F7FB", "surface": "#FFFFFF", "text": "#0F1B2D", "muted": "#5B6B82",
                              "primary": "#1D4ED8", "accent": "#0EA5A4", "accent2": "#F59E0B"},
                  "effects": ["tilt", "magnetic"], "mood": ["надёжность", "ясность", "масштаб"]},
}

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---------- цвета ----------
def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02X}" for c in rgb)


def luminance(value: str) -> float:
    def channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in hex_to_rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def mix(a: str, b: str, t: float) -> str:
    ra, rb = hex_to_rgb(a), hex_to_rgb(b)
    return rgb_to_hex(tuple(x + (y - x) * t for x, y in zip(ra, rb)))  # type: ignore[arg-type]


def adjust_lightness(value: str, delta: float) -> str:
    h, l, s = colorsys.rgb_to_hls(*hex_to_rgb(value))
    return rgb_to_hex(colorsys.hls_to_rgb(h, max(0.0, min(1.0, l + delta)), s))


def ensure_contrast(fg: str, bg: str, target: float) -> str:
    """Сдвигает светлоту fg, пока контраст с bg не станет не ниже target (WCAG)."""
    if contrast(fg, bg) >= target:
        return fg
    step = 0.03 if luminance(bg) < 0.5 else -0.03
    current = fg
    for _ in range(40):
        current = adjust_lightness(current, step)
        if contrast(current, bg) >= target:
            return current
    return "#FFFFFF" if luminance(bg) < 0.5 else "#000000"


def is_dark(value: str) -> bool:
    return luminance(value) < 0.2


def on_color(bg: str) -> str:
    """Цвет текста поверх цветной кнопки."""
    return "#0B0B0F" if contrast("#0B0B0F", bg) >= contrast("#FFFFFF", bg) else "#FFFFFF"


def fix_palette(palette: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Доводит палитру до норм доступности. Возвращает палитру и список того, что поправлено."""
    fixed, notes = dict(palette), []
    for key, target in (("text", 7.0), ("muted", 4.5), ("primary", 3.0)):
        before = fixed[key]
        fixed[key] = ensure_contrast(before, fixed["bg"], target)
        if fixed[key] != before:
            notes.append(f"{key}: {before} → {fixed[key]} (контраст {contrast(fixed[key], fixed['bg']):.1f}:1)")
    if contrast(fixed["surface"], fixed["bg"]) < 1.04:
        fixed["surface"] = adjust_lightness(fixed["bg"], 0.05 if is_dark(fixed["bg"]) else -0.03)
    return fixed, notes


def alt_palette(p: dict[str, str]) -> dict[str, str]:
    """Вторая тема (светлая ↔ тёмная) для переключателя на сайте."""
    if is_dark(p["bg"]):
        bg = mix("#FFFFFF", p["primary"], 0.04)
        alt = {"bg": bg, "surface": "#FFFFFF", "text": mix("#0B0B0F", p["bg"], 0.3), "muted": "#5E5A66"}
    else:
        bg = mix("#0B0B0F", p["primary"], 0.06)
        alt = {"bg": bg, "surface": adjust_lightness(bg, 0.05), "text": "#F4F2F8", "muted": "#A9A6B8"}
    alt.update({k: p[k] for k in ("primary", "accent", "accent2")})
    fixed, _ = fix_palette(alt)
    return fixed


# ---------- нормализация ----------
def _text(value: Any, limit: int, default: str = "") -> str:
    if not isinstance(value, (str, int, float)):
        return default
    text = " ".join(str(value).split())[:limit]
    return text or default


def _choice(value: Any, allowed: tuple[str, ...] | dict, default: str) -> str:
    return value if isinstance(value, str) and value in allowed else default


def _items(value: Any, limit: int) -> list[dict]:
    return [v for v in value if isinstance(v, dict)][:limit] if isinstance(value, list) else []


def slugify(text: str) -> str:
    table = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
                           "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
                           "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh",
                           "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya", "ў": "o", "қ": "q",
                           "ғ": "g", "ҳ": "h", "ʻ": "", "‘": "", "'": ""})
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().translate(table)).strip("-")
    return slug[:40] or "site"


def base_spec(preset: str) -> dict[str, Any]:
    p = PRESETS.get(preset, PRESETS["aurora"])
    return {
        "brand": {"name": "", "tagline": "", "niche": "", "city": "", "lang": "ru"},
        "style": {"preset": preset if preset in PRESETS else "aurora", "hero": p["hero"], "art": p["art"],
                  "radius": p["radius"], "density": p["density"], "effects": list(p["effects"]),
                  "mood": list(p["mood"]), "art_seed": 0, "art_hue": 0},
        "palette": dict(p["palette"]),
        "fonts": {"display": p["fonts"][0], "body": p["fonts"][1]},
        "copy": {"eyebrow": "", "hero_title": "", "hero_lead": "", "cta_primary": "Оставить заявку",
                 "cta_secondary": "Подробнее", "trust": []},
        "sections": [],
        "contacts": {"phone": "", "telegram": "", "instagram": "", "email": "", "address": "", "hours": ""},
        "seo": {"title": "", "description": "", "keywords": []},
    }


def normalize(spec: dict[str, Any]) -> dict[str, Any]:
    """Приводит то, что собрали агенты, к безопасному и полному виду. Неизвестное отбрасывается."""
    style_in = spec.get("style") if isinstance(spec.get("style"), dict) else {}
    preset = _choice(style_in.get("preset"), PRESETS, "aurora")
    out = base_spec(preset)

    brand = spec.get("brand") if isinstance(spec.get("brand"), dict) else {}
    out["brand"] = {"name": _text(brand.get("name"), 60, "Ваш бренд"), "tagline": _text(brand.get("tagline"), 120),
                    "niche": _text(brand.get("niche"), 120), "city": _text(brand.get("city"), 60),
                    "lang": _choice(brand.get("lang"), LANGS, "ru")}

    st = out["style"]
    st["hero"] = _choice(style_in.get("hero"), HEROES, st["hero"])
    st["art"] = _choice(style_in.get("art"), ARTS, st["art"])
    st["radius"] = _choice(style_in.get("radius"), RADII, st["radius"])
    st["density"] = _choice(style_in.get("density"), DENSITIES, st["density"])
    if isinstance(style_in.get("effects"), list):
        st["effects"] = [e for e in dict.fromkeys(style_in["effects"]) if e in EFFECTS]
    if isinstance(style_in.get("mood"), list):
        st["mood"] = [_text(m, 30) for m in style_in["mood"] if _text(m, 30)][:5]
    for key, top in (("art_seed", 10_000_000), ("art_hue", 360)):
        try:
            st[key] = int(style_in.get(key, 0)) % top
        except (TypeError, ValueError):
            st[key] = 0

    pal_in = spec.get("palette") if isinstance(spec.get("palette"), dict) else {}
    for key in out["palette"]:
        value = pal_in.get(key)
        if isinstance(value, str) and HEX.match(value.strip()):
            out["palette"][key] = value.strip().upper()
    out["palette"], _ = fix_palette(out["palette"])

    fonts_in = spec.get("fonts") if isinstance(spec.get("fonts"), dict) else {}
    for key in ("display", "body"):
        out["fonts"][key] = _choice(fonts_in.get(key), FONTS, out["fonts"][key])

    copy_in = spec.get("copy") if isinstance(spec.get("copy"), dict) else {}
    c = out["copy"]
    c["eyebrow"] = _text(copy_in.get("eyebrow"), 60)
    c["hero_title"] = _text(copy_in.get("hero_title"), 110, out["brand"]["name"])
    c["hero_lead"] = _text(copy_in.get("hero_lead"), 260)
    c["cta_primary"] = _text(copy_in.get("cta_primary"), 32, c["cta_primary"])
    c["cta_secondary"] = _text(copy_in.get("cta_secondary"), 32, c["cta_secondary"])
    c["trust"] = [_text(t, 40) for t in copy_in.get("trust", []) if _text(t, 40)][:8] if isinstance(copy_in.get("trust"), list) else []

    out["sections"] = [s for s in (_section(raw) for raw in _items(spec.get("sections"), 12)) if s]
    if not any(s["type"] == "contact" for s in out["sections"]):
        out["sections"].append({"type": "contact", "title": "Свяжитесь с нами", "text": ""})

    contacts = spec.get("contacts") if isinstance(spec.get("contacts"), dict) else {}
    out["contacts"] = {k: _text(contacts.get(k), 120) for k in out["contacts"]}
    out["contacts"]["telegram"] = out["contacts"]["telegram"].lstrip("@").replace("https://t.me/", "")[:40]
    out["contacts"]["instagram"] = out["contacts"]["instagram"].lstrip("@").replace("https://instagram.com/", "")[:40]

    seo = spec.get("seo") if isinstance(spec.get("seo"), dict) else {}
    out["seo"] = {"title": _text(seo.get("title"), 70, out["brand"]["name"]),
                  "description": _text(seo.get("description"), 170, c["hero_lead"][:170]),
                  "keywords": [_text(k, 40) for k in seo.get("keywords", []) if _text(k, 40)][:12]
                  if isinstance(seo.get("keywords"), list) else []}
    return out


def _section(raw: dict) -> dict | None:
    kind = raw.get("type")
    if kind not in SECTION_TYPES:
        return None
    sec: dict[str, Any] = {"type": kind, "title": _text(raw.get("title"), 90), "text": _text(raw.get("text"), 300),
                           "eyebrow": _text(raw.get("eyebrow"), 40)}
    if kind == "features":
        sec["layout"] = _choice(raw.get("layout"), FEATURE_LAYOUTS, "bento")
        sec["items"] = [{"icon": _choice(i.get("icon"), ICONS, "spark"), "title": _text(i.get("title"), 60),
                         "text": _text(i.get("text"), 220)} for i in _items(raw.get("items"), 8) if _text(i.get("title"), 60)]
    elif kind == "stats":
        sec["items"] = [{"value": _text(i.get("value"), 12), "label": _text(i.get("label"), 60)}
                        for i in _items(raw.get("items"), 4) if _text(i.get("value"), 12)]
    elif kind == "process":
        sec["items"] = [{"title": _text(i.get("title"), 60), "text": _text(i.get("text"), 220)}
                        for i in _items(raw.get("items"), 6) if _text(i.get("title"), 60)]
    elif kind == "showcase":
        sec["items"] = [{"title": _text(i.get("title"), 60), "tag": _text(i.get("tag"), 30), "text": _text(i.get("text"), 160)}
                        for i in _items(raw.get("items"), 8) if _text(i.get("title"), 60)]
    elif kind == "testimonials":
        sec["items"] = [{"quote": _text(i.get("quote"), 280), "name": _text(i.get("name"), 50), "role": _text(i.get("role"), 60)}
                        for i in _items(raw.get("items"), 8) if _text(i.get("quote"), 280)]
    elif kind == "pricing":
        sec["items"] = [{"name": _text(i.get("name"), 40), "price": _text(i.get("price"), 24), "period": _text(i.get("period"), 24),
                         "features": [_text(f, 80) for f in i.get("features", []) if _text(f, 80)][:8]
                         if isinstance(i.get("features"), list) else [],
                         "featured": bool(i.get("featured"))} for i in _items(raw.get("items"), 4) if _text(i.get("name"), 40)]
    elif kind == "faq":
        sec["items"] = [{"q": _text(i.get("q"), 160), "a": _text(i.get("a"), 600)}
                        for i in _items(raw.get("items"), 10) if _text(i.get("q"), 160)]
    elif kind == "cta":
        sec["button"] = _text(raw.get("button"), 32, "Начать")
    if kind not in ("cta", "contact") and not sec.get("items"):
        return None
    return sec


def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Глубокое слияние: словари сливаются, остальное заменяется."""
    out = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out
