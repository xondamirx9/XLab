"""Измеримые проверки готового сайта. Ревьюеры-агенты получают этот отчёт как факты, а не догадки."""
from __future__ import annotations

import re
from typing import Any

from app.studio.renderer import plain
from app.studio.spec import contrast, on_color

MAX_HTML_KB = 260


def check_site(spec: dict[str, Any], html: str) -> dict[str, Any]:
    """Возвращает {score: 0–100, checks: [{name, ok, detail, owner}]}. owner — чья это зона."""
    pal, copy, seo = spec["palette"], spec["copy"], spec["seo"]
    sections = spec["sections"]
    kinds = [s["type"] for s in sections]
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, owner: str, weight: int = 1) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "owner": owner, "weight": weight})

    c_text, c_muted = contrast(pal["text"], pal["bg"]), contrast(pal["muted"], pal["bg"])
    c_button = contrast(on_color(pal["primary"]), pal["primary"])
    add("Контраст основного текста", c_text >= 7, f"{c_text:.1f}:1 (норма 7:1)", "colorist", 3)
    add("Контраст второстепенного текста", c_muted >= 4.5, f"{c_muted:.1f}:1 (норма 4,5:1)", "colorist", 2)
    add("Текст на кнопке читается", c_button >= 4.5, f"{c_button:.1f}:1 (норма 4,5:1)", "colorist", 2)
    add("Акцент виден на фоне", contrast(pal["primary"], pal["bg"]) >= 3, f"{contrast(pal['primary'], pal['bg']):.1f}:1 (норма 3:1)", "colorist")

    title_len = len(plain(copy["hero_title"]))
    add("Заголовок первого экрана короткий", 8 <= title_len <= 70, f"{title_len} знаков (норма 8–70)", "copywriter", 2)
    add("Есть подзаголовок первого экрана", 40 <= len(copy["hero_lead"]) <= 240, f"{len(copy['hero_lead'])} знаков (норма 40–240)", "copywriter")
    accents = copy["hero_title"].count("*") // 2
    add("Градиентный акцент в заголовке — один", accents <= 2, f"акцентов: {accents}", "copywriter")

    add("Разделов достаточно", len(sections) >= 5, f"разделов: {len(sections)} (норма от 5)", "ux_architect", 2)
    add("Есть призыв к действию", "cta" in kinds, "раздел cta " + ("есть" if "cta" in kinds else "отсутствует"), "cro", 2)
    add("Есть форма заявки и контакты", "contact" in kinds, "раздел contact есть" if "contact" in kinds else "нет раздела contact", "cro", 2)
    contacts = [v for v in spec["contacts"].values() if v]
    add("Указан хотя бы один контакт", bool(contacts), f"контактов: {len(contacts)}", "cro")
    features = next((s for s in sections if s["type"] == "features"), None)
    add("Преимуществ от трёх", bool(features) and len(features["items"]) >= 3,
        f"преимуществ: {len(features['items']) if features else 0}", "copywriter")
    empty = [s["type"] for s in sections if s["type"] not in ("cta", "contact", "stats") and not s.get("title")]
    add("У разделов есть заголовки", not empty, "без заголовка: " + ", ".join(empty) if empty else "у всех есть", "copywriter")
    dupes = len(kinds) - len(set(kinds))
    add("Разделы не повторяются", dupes == 0, f"повторов: {dupes}", "ux_architect")

    add("SEO-заголовок до 60 знаков", 10 <= len(seo["title"]) <= 60, f"{len(seo['title'])} знаков", "seo")
    add("SEO-описание 70–160 знаков", 70 <= len(seo["description"]) <= 160, f"{len(seo['description'])} знаков", "seo")
    h1 = len(re.findall(r"<h1[\s>]", html))
    add("Один заголовок h1", h1 == 1, f"h1: {h1}", "frontend")
    size_kb = len(html.encode("utf-8")) / 1024
    add("Вес страницы", size_kb <= MAX_HTML_KB, f"{size_kb:.0f} КБ (норма до {MAX_HTML_KB})", "performance")
    heavy = {"cursor", "grain", "parallax", "tilt", "marquee"} & set(spec["style"]["effects"])
    add("Эффектов в меру", len(heavy) <= 4, f"тяжёлых эффектов: {len(heavy)} (норма до 4)", "motion")
    add("Шрифты заголовков и текста разные", spec["fonts"]["display"] != spec["fonts"]["body"],
        f"{spec['fonts']['display']} / {spec['fonts']['body']}", "typographer")

    total = sum(c["weight"] for c in checks)
    passed = sum(c["weight"] for c in checks if c["ok"])
    return {"score": round(passed * 100 / total), "checks": checks}


def report_text(report: dict[str, Any]) -> str:
    lines = [f"Автопроверка: {report['score']}/100"]
    for c in report["checks"]:
        lines.append(f"{'✅' if c['ok'] else '❌'} {c['name']}: {c['detail']} [{c['owner']}]")
    return "\n".join(lines)
