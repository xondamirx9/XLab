"""Конвейер студии: как 20 агентов вместе делают сайт клиенту.

Брифинг → исследование → концепции → спор и голосование → задачи специалистам (и задачи друг другу) →
сборка → ревью с измеримой автопроверкой → доработки → публикация → ретроспектива с уроками.
Каждая реплика агента записывается в совет: её видно на странице совета и клиенту на странице проекта.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.studio import offline as fb
from app.studio.agents import BY_ID, IDS, system_prompt
from app.studio.llm import BOOL, INT, STR, LLM, arr, enum, obj
from app.studio.quality import check_site, report_text
from app.studio.renderer import render_site
from app.studio.spec import (ARTS, EFFECTS, FEATURE_LAYOUTS, FONTS, HEROES, ICONS, PRESETS, RADII, SECTION_TYPES,
                             DENSITIES, alt_palette, is_dark, normalize)
from app.studio.store import Project, StudioStore

log = logging.getLogger("site.studio")

DEBATERS = ("critic", "ux_architect", "colorist", "typographer", "motion", "cro", "a11y")
REVIEWERS = ("qa", "a11y", "performance", "cro", "critic")
# кто чинит замечания, адресованные не производственным ролям
FIX_OWNER = {"a11y": "colorist", "performance": "motion", "frontend": "ui_designer", "cro": "copywriter", "qa": "editor",
             "critic": "copywriter", "art_director": "ui_designer", "strategist": "copywriter", "researcher": "ux_architect",
             "producer": "copywriter", "curator": "copywriter"}
PRODUCTION_ORDER = ("colorist", "typographer", "ui_designer", "motion", "illustrator", "ux_architect", "copywriter", "editor", "seo")

DELEGATE = arr(obj(to=enum(IDS), task=STR))
PALETTE = obj(bg=STR, surface=STR, text=STR, muted=STR, primary=STR, accent=STR, accent2=STR)
ITEM = obj(icon=enum(ICONS), title=STR, text=STR, value=STR, label=STR, tag=STR, quote=STR, name=STR, role=STR,
           price=STR, period=STR, features=arr(STR), featured=BOOL, q=STR, a=STR)
SECTION = obj(type=enum(SECTION_TYPES), eyebrow=STR, title=STR, text=STR, button=STR, layout=enum(FEATURE_LAYOUTS), items=arr(ITEM))
COPY = obj(eyebrow=STR, hero_title=STR, hero_lead=STR, cta_primary=STR, cta_secondary=STR, trust=arr(STR))
ISSUE = obj(owner=enum(IDS), problem=STR, fix=STR)

Notify = Callable[[Project, str, str], Awaitable[None]]


def _short(value: Any, limit: int = 1800) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= limit else text[:limit] + "…"


def _item_defaults(item: dict[str, Any]) -> dict[str, Any]:
    base = {"icon": "spark", "title": "", "text": "", "value": "", "label": "", "tag": "", "quote": "", "name": "", "role": "",
            "price": "", "period": "", "features": [], "featured": False, "q": "", "a": ""}
    base.update({k: v for k, v in item.items() if k in base})
    return base


def _section_defaults(sec: dict[str, Any]) -> dict[str, Any]:
    return {"type": sec["type"], "eyebrow": sec.get("eyebrow", ""), "title": sec.get("title", ""), "text": sec.get("text", ""),
            "button": sec.get("button", ""), "layout": sec.get("layout", "bento"),
            "items": [_item_defaults(i) for i in sec.get("items", [])]}


@dataclass
class Run:
    """Состояние одного прохода конвейера по проекту."""

    project: Project
    llm: LLM
    spec: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    lessons_used: set[int] = field(default_factory=set)
    agents: set[str] = field(default_factory=set)
    delegated: list[tuple[int, str, str, str]] = field(default_factory=list)   # (task_id, from, to, title)
    feedback: dict[str, list[str]] = field(default_factory=dict)

    @property
    def brief(self) -> dict[str, Any]:
        return self.project.brief



class Studio:
    def __init__(self, store: StudioStore, llm_factory: Callable[[], LLM], sites_dir: Path, *, debate_rounds: int = 2,
                 review_threshold: float = 8.0, max_fix_rounds: int = 2, notify: Notify | None = None) -> None:
        self.store, self.llm_factory, self.sites_dir = store, llm_factory, sites_dir
        self.debate_rounds, self.review_threshold, self.max_fix_rounds = debate_rounds, review_threshold, max_fix_rounds
        self.notify = notify

    # ================= общение =================
    async def say(self, run: Run, agent: str, channel: str, kind: str, content: str, data: dict[str, Any] | None = None,
                  reply_to: str = "") -> None:
        run.agents.add(agent)
        await self.store.post(run.project.id, channel, agent, kind, content, data, reply_to)

    def _context(self, run: Run) -> str:
        brief = {k: v for k, v in run.brief.items() if k != "contacts"}
        decided = {k: v for k, v in run.state.items() if k in ("strategy", "research", "conversion", "concept", "decision",
                                                               "palette", "fonts", "layout", "effects", "plan", "notes")}
        return f"Бриф клиента:\n{_short(brief, 1500)}\n\nЧто команда уже решила:\n{_short(decided, 3500)}"

    async def ask(self, run: Run, agent_id: str, channel: str, task: str, props: dict[str, Any],
                  offline: Callable[[], dict[str, Any]], *, kind: str = "say", reply_to: str = "",
                  show: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
        """Задаёт агенту вопрос и публикует его ответ в совете. Ответ — JSON по схеме."""
        agent = BY_ID[agent_id]
        lessons = await self.store.lessons_for(agent_id)
        run.lessons_used.update(l["id"] for l in lessons)
        transcript = "\n".join(f"{BY_ID[m['agent']].emoji if m['agent'] in BY_ID else '•'} {BY_ID[m['agent']].name if m['agent'] in BY_ID else m['agent']}"
                               f" ({m['channel']}): {m['content']}" for m in await self.store.recent(run.project.id))
        feedback = run.feedback.pop(agent_id, [])
        prompt = (f"{self._context(run)}\n\nПоследние сообщения совета:\n{transcript or '(пока пусто)'}\n\n"
                  + (("Замечания к твоей прошлой работе — исправь их:\n" + "\n".join(f"- {f}" for f in feedback) + "\n\n") if feedback else "")
                  + f"Твоя задача: {task}")
        schema = obj(message=STR, **props)

        def fallback() -> dict[str, Any]:
            data = offline()
            data.setdefault("message", "Готово.")
            return data
        result = await run.llm.json(system=system_prompt(agent, [l["text"] for l in lessons]), prompt=prompt, schema=schema,
                                    effort=agent.effort, offline=fallback)
        if not isinstance(result.get("message"), str):
            result["message"] = "Готово."
        shown = show(result) if show else {k: v for k, v in result.items() if k not in ("message", "delegate")}
        await self.say(run, agent_id, channel, kind, result["message"], shown, reply_to)
        for item in (result.get("delegate") or [])[:2]:
            to, title = item.get("to"), str(item.get("task", "")).strip()
            if to in BY_ID and to != agent_id and title and len(run.delegated) < 6:
                task_id = await self.store.add_task(run.project.id, agent_id, to, title)
                run.delegated.append((task_id, agent_id, to, title))
                await self.say(run, agent_id, "задачи", "task", f"@{BY_ID[to].name}, {title}", {"task_id": task_id}, reply_to=to)
        return result

    async def handle_delegated(self, run: Run) -> None:
        """Агенты выполняют задачи, которые поставили друг другу. Ответы идут в заметки для сборки."""
        pending, run.delegated = run.delegated, []
        async def one(task_id: int, src: str, dst: str, title: str) -> None:
            res = await self.ask(run, dst, "задачи", f"Коллега {BY_ID[src].name} ({BY_ID[src].role}) просит: «{title}». "
                                 "Ответь по существу: что конкретно предлагаешь. В поле note — короткая рекомендация для сборки сайта.",
                                 {"note": STR}, lambda: {"message": f"Принято, {BY_ID[src].name}. Учту в своей части.",
                                                         "note": title}, kind="done", reply_to=src)
            run.state.setdefault("notes", []).append(f"{BY_ID[dst].role}: {res.get('note', '')}"[:300])
            await self.store.finish_task(task_id, res.get("note", "") or res["message"])
        await asyncio.gather(*(one(*t) for t in pending))

    # ================= этапы =================
    async def kickoff(self, run: Run) -> None:
        b = run.brief
        await self.say(run, "producer", "брифинг", "system", f"Новый проект: «{run.project.name}». Собираю команду из 20 агентов.",
                       {"brief": {k: v for k, v in b.items() if k != "contacts"}})
        res = await self.ask(run, "producer", "брифинг", "Открой проект: в двух-трёх фразах объясни команде задачу клиента, "
                             "перечисли 3 цели сайта (goals) и 2 главных риска (risks).",
                             {"goals": arr(STR), "risks": arr(STR)},
                             lambda: {"message": f"Коллеги, у нас «{run.project.name}» — {b.get('niche') or 'новый бизнес'}. "
                                                 "Нужен сайт, который с первого экрана объясняет, чем клиент лучше, и ведёт к заявке.",
                                      "goals": ["Понятно объяснить ценность за 5 секунд", "Получать заявки", "Выделиться среди конкурентов"],
                                      "risks": ["Шаблонный вид", "Слабые тексты без конкретики"]})
        run.state["goals"] = res.get("goals", [])

    async def research(self, run: Run) -> None:
        b = run.brief
        profile = fb.niche_profile(b)
        strategy, research, conversion = await asyncio.gather(
            self.ask(run, "strategist", "исследование", "Сформулируй позиционирование, аудиторию, тон голоса и 3 ключевых сообщения бренда.",
                     {"positioning": STR, "audience": STR, "tone": STR, "key_messages": arr(STR), "delegate": DELEGATE},
                     lambda: {"message": f"Позиционирую «{run.project.name}» через качество и заботу: не «ещё один {b.get('niche') or 'бизнес'}», "
                                         "а место, куда хочется вернуться.",
                              "positioning": "Качество и внимание к деталям", "audience": b.get("audience") or "жители города 25–45 лет",
                              "tone": "тёплый, уверенный, без канцелярита", "key_messages": profile["features"][:3], "delegate": []}),
            self.ask(run, "researcher", "исследование", "Опиши, какие разделы ждут посетители в этой нише (must_have — типы разделов), "
                     "какие клише конкурентов надоели и 2–3 свежие идеи.",
                     {"must_have": arr(enum(SECTION_TYPES)), "cliches": arr(STR), "ideas": arr(STR)},
                     lambda: {"message": "У конкурентов одинаковые стоковые фото и «индивидуальный подход». Предлагаю уйти в конкретику и цифры.",
                              "must_have": ["features", "stats", "testimonials", "faq", "contact"],
                              "cliches": ["стоковые фото улыбающихся людей", "«индивидуальный подход» без доказательств"],
                              "ideas": ["цифры на первом экране", "живые отзывы бегущей строкой"]}),
            self.ask(run, "cro", "исследование", "Определи главное действие посетителя, элементы доверия и как снять возражения.",
                     {"primary_action": STR, "trust": arr(STR), "objections": arr(STR), "delegate": DELEGATE},
                     lambda: {"message": "Главное действие — короткая заявка: имя и телефон. Кнопка должна быть видна всегда.",
                              "primary_action": "Оставить заявку", "trust": ["отзывы", "цифры", "гарантия"],
                              "objections": ["дорого", "долго"], "delegate": []}),
        )
        run.state["strategy"] = {k: strategy.get(k) for k in ("positioning", "audience", "tone", "key_messages")}
        run.state["research"] = {k: research.get(k) for k in ("must_have", "cliches", "ideas")}
        run.state["conversion"] = {k: conversion.get(k) for k in ("primary_action", "trust", "objections")}
        await self.handle_delegated(run)

    async def concepts(self, run: Run) -> list[dict[str, Any]]:
        offline_concepts = fb.concepts(run.brief)
        concept = obj(name=STR, idea=STR, preset=enum(PRESETS), hero=enum(HEROES), art=enum(ARTS), mood=arr(STR))
        res = await self.ask(run, "art_director", "спор", "Предложи ровно 3 непохожие визуальные концепции сайта. Первая — та, за которую ты "
                             "будешь бороться. Учитывай пожелания клиента по стилю и цветам.", {"concepts": arr(concept)},
                             lambda: {"message": "Принёс три концепции. Я за первую: она точнее всего передаёт характер клиента.",
                                      "concepts": [{**c, "mood": PRESETS[c["preset"]]["mood"]} for c in offline_concepts]},
                             kind="propose")
        items = [c for c in res.get("concepts", []) if isinstance(c, dict) and c.get("preset") in PRESETS][:3]
        if len(items) < 2:
            items = [{**c, "mood": PRESETS[c["preset"]]["mood"]} for c in offline_concepts]
        run.state["concepts"] = items
        return items

    async def debate(self, run: Run, concepts: list[dict[str, Any]]) -> dict[str, Any]:
        listing = "\n".join(f"{i + 1}. {c['name']} — {c.get('idea', '')} (пресет {c['preset']}, экран {c.get('hero')}, графика {c.get('art')})"
                            for i, c in enumerate(concepts))
        votes: dict[str, int] = {}
        n = len(concepts)
        for round_no in range(1, self.debate_rounds + 1):
            async def speak(agent_id: str) -> None:
                rng = random.Random(fb.seed_of(run.project.slug, agent_id, str(round_no)))
                default_vote = 1 if agent_id != "critic" else min(n, 2)
                if round_no > 1 and agent_id != "critic":
                    default_vote = votes.get(agent_id, 1)
                stance = "object" if agent_id == "critic" else rng.choice(["agree", "agree", "propose"])
                lines = {"critic": "Первая концепция слишком предсказуема для ниши — нас запомнят хуже. Вторая смелее.",
                         "ux_architect": "Голосую за концепцию, где первый экран не мешает читать оффер.",
                         "colorist": "Под эту палитру хорошо ложится настроение бренда, контраст вытянем.",
                         "typographer": "Шрифтовая пара для первой концепции уже есть в голове — она сильная.",
                         "motion": "Движение можно сделать аккуратным, без укачивания.",
                         "cro": "Главное, чтобы кнопка заявки была видна сразу — в первой это проще.",
                         "a11y": "Поддерживаю, если не будет мелкого серого текста на светлом фоне."}
                res = await self.ask(
                    run, agent_id, "спор",
                    f"Раунд спора {round_no} из {self.debate_rounds}. Концепции:\n{listing}\n"
                    + ("Выскажись со своей профессиональной позиции и проголосуй (vote — номер концепции). " if round_no == 1 else
                       "Прочитай аргументы коллег выше. Можешь возразить конкретному коллеге (reply_to — его id) или поменять голос. ")
                    + "stance: agree — поддерживаешь, object — возражаешь, propose — предлагаешь доработку.",
                    {"stance": enum(["agree", "object", "propose"]), "vote": INT, "reply_to": enum(("",) + IDS), "delegate": DELEGATE},
                    lambda: {"message": lines.get(agent_id, "Поддерживаю."), "stance": stance, "vote": default_vote,
                             "reply_to": "art_director" if agent_id == "critic" else "", "delegate": []},
                    kind="vote", show=lambda r: {"stance": r.get("stance"), "vote": r.get("vote")})
                try:
                    votes[agent_id] = max(1, min(n, int(res.get("vote", 1))))
                except (TypeError, ValueError):
                    votes[agent_id] = 1
            await asyncio.gather(*(speak(a) for a in DEBATERS))
            tally = Counter(votes.values())
            await self.say(run, "producer", "спор", "system", "Голоса: " + ", ".join(f"концепция {k} — {v}" for k, v in sorted(tally.items())),
                           {"tally": dict(tally), "round": round_no})
            if round_no < self.debate_rounds:
                await self.ask(run, "art_director", "спор", "Ответь критикам: защити свою концепцию или признай, что они правы, "
                               "и предложи, как её улучшить.", {"defend": INT},
                               lambda: {"message": "Слышу критику. Предлагаю взять первую концепцию, но сделать графику смелее — "
                                                   "так мы сохраним ясность и уйдём от предсказуемости.", "defend": 1},
                               kind="object", reply_to="critic")
        tally = Counter(votes.values())
        leader = tally.most_common(1)[0][0] if tally else 1
        res = await self.ask(run, "producer", "спор", f"Подведи итог спора и прими решение. Концепции:\n{listing}\nГолоса: {dict(tally)}. "
                             "winner — номер концепции (не обязательно лидер голосования, но объясни решение), "
                             "adjustments — 2–3 доработки по итогам критики.",
                             {"winner": INT, "adjustments": arr(STR)},
                             lambda: {"message": f"Решение: берём концепцию {leader} — за неё больше аргументов. Критику учли в доработках.",
                                      "winner": leader, "adjustments": ["графика смелее", "кнопка заявки всегда на виду"]},
                             kind="decision")
        try:
            winner = max(1, min(n, int(res.get("winner", leader))))
        except (TypeError, ValueError):
            winner = leader
        chosen = concepts[winner - 1]
        run.state["concept"] = chosen
        run.state["decision"] = {"winner": chosen.get("name"), "adjustments": res.get("adjustments", [])}
        if self.notify:
            await self.notify(run.project, "concept", f"Совет выбрал концепцию: «{chosen.get('name')}». Начинаем производство.")
        return chosen

    async def assign(self, run: Run) -> None:
        titles = {"colorist": "собрать палитру", "typographer": "подобрать пару шрифтов", "ui_designer": "композиция и первый экран",
                  "motion": "эффекты и движение", "illustrator": "генеративная графика", "ux_architect": "структура страницы",
                  "copywriter": "тексты всех разделов", "editor": "вычитка и тон", "seo": "SEO-метаданные"}
        for agent_id, title in titles.items():
            task_id = await self.store.add_task(run.project.id, "producer", agent_id, title)
            run.state.setdefault("task_ids", {})[agent_id] = task_id
        await self.say(run, "producer", "задачи", "task", "Раздаю задачи: " + "; ".join(f"@{BY_ID[a].name} — {t}" for a, t in titles.items()),
                       {"tasks": titles})

    # ---------- производство: каждый специалист отвечает за свою часть спецификации ----------
    async def make(self, run: Run, agent_id: str) -> None:
        concept = run.state.get("concept") or {}
        preset = concept.get("preset") if concept.get("preset") in PRESETS else run.spec["style"]["preset"]
        wish = " ".join(run.feedback.get(agent_id, [])).lower()
        base = fb.full_spec({**run.brief, "with_pricing": "цен" in wish or "прайс" in wish}, preset)
        brief = run.brief
        if agent_id == "colorist" and run.spec.get("palette") and ("темн" in wish or "тёмн" in wish or "светл" in wish):
            wants_dark = "светл" not in wish
            if is_dark(run.spec["palette"]["bg"]) != wants_dark:      # без нейросети: переключаем тему палитры
                base["palette"] = alt_palette(run.spec["palette"])
        if agent_id == "colorist":
            res = await self.ask(run, agent_id, "производство", "Собери палитру в HEX (#RRGGBB) под выбранную концепцию и пожелания клиента "
                                 f"по цветам: «{brief.get('colors') or 'нет пожеланий'}». Контраст text/bg ≥ 7:1, muted/bg ≥ 4,5:1.",
                                 {"palette": PALETTE, "delegate": DELEGATE},
                                 lambda: {"message": "Палитра готова: опираюсь на пресет концепции, акцент — под настроение бренда.",
                                          "palette": base["palette"], "delegate": []})
            run.spec["palette"] = res.get("palette") or base["palette"]
            run.spec = normalize(run.spec)
            run.state["palette"] = run.spec["palette"]
        elif agent_id == "typographer":
            res = await self.ask(run, agent_id, "производство", "Выбери шрифт заголовков (display) и текста (body) из списка рендерера.",
                                 {"display": enum(FONTS), "body": enum(FONTS), "delegate": DELEGATE},
                                 lambda: {"message": f"Пара: {base['fonts']['display']} для заголовков и {base['fonts']['body']} для текста.",
                                          **base["fonts"], "delegate": []})
            run.spec["fonts"] = {"display": res.get("display"), "body": res.get("body")}
            run.spec = normalize(run.spec)
            run.state["fonts"] = run.spec["fonts"]
        elif agent_id == "ui_designer":
            res = await self.ask(run, agent_id, "производство", "Выбери первый экран, скругления и плотность под концепцию.",
                                 {"hero": enum(HEROES), "radius": enum(RADII), "density": enum(DENSITIES), "delegate": DELEGATE},
                                 lambda: {"message": f"Первый экран — {concept.get('hero') or base['style']['hero']}, скругления {base['style']['radius']}.",
                                          "hero": concept.get("hero") or base["style"]["hero"], "radius": base["style"]["radius"],
                                          "density": base["style"]["density"], "delegate": []})
            run.spec["style"].update({k: res.get(k) for k in ("hero", "radius", "density")})
            run.spec = normalize(run.spec)
            run.state["layout"] = {k: run.spec["style"][k] for k in ("hero", "radius", "density")}
        elif agent_id == "motion":
            res = await self.ask(run, agent_id, "производство", "Выбери эффекты (не больше 4 тяжёлых) и объясни, зачем каждый.",
                                 {"effects": arr(enum(EFFECTS)), "delegate": DELEGATE},
                                 lambda: {"message": "Беру магнитные кнопки и мягкий параллакс — живо, но не отвлекает.",
                                          "effects": base["style"]["effects"], "delegate": []})
            run.spec["style"]["effects"] = res.get("effects") or base["style"]["effects"]
            run.spec = normalize(run.spec)
            run.state["effects"] = run.spec["style"]["effects"]
        elif agent_id == "illustrator":
            res = await self.ask(run, agent_id, "производство", "Выбери тип генеративной графики, зерно (art_seed, любое целое) "
                                 "и сдвиг оттенка (art_hue, 0–359), чтобы картинка была уникальной.",
                                 {"art": enum(ARTS), "art_seed": INT, "art_hue": INT},
                                 lambda: {"message": f"Графика — {concept.get('art') or base['style']['art']}, зерно своё у каждого клиента.",
                                          "art": concept.get("art") or base["style"]["art"], "art_seed": base["style"]["art_seed"],
                                          "art_hue": base["style"]["art_hue"]})
            run.spec["style"].update({k: res.get(k) for k in ("art", "art_seed", "art_hue")})
            run.spec = normalize(run.spec)
        elif agent_id == "ux_architect":
            plan_item = obj(type=enum(SECTION_TYPES), layout=enum(FEATURE_LAYOUTS), purpose=STR)
            res = await self.ask(run, agent_id, "производство", "Составь порядок разделов страницы после первого экрана (6–9 разделов, "
                                 "без повторов, обязательно cta и contact в конце) и зачем каждый.",
                                 {"sections": arr(plan_item), "delegate": DELEGATE},
                                 lambda: {"message": "Структура: ценность → доказательства → процесс → отзывы → вопросы → заявка.",
                                          "sections": [{"type": s["type"], "layout": s.get("layout", "bento"), "purpose": ""} for s in base["sections"]],
                                          "delegate": []})
            plan, seen = [], set()
            for s in res.get("sections", []):
                if isinstance(s, dict) and s.get("type") in SECTION_TYPES and s["type"] not in seen:
                    seen.add(s["type"])
                    plan.append({"type": s["type"], "layout": s.get("layout") if s.get("layout") in FEATURE_LAYOUTS else "bento"})
            for required in ("cta", "contact"):
                if required not in seen:
                    plan.append({"type": required, "layout": "bento"})
            run.state["plan"] = plan
        elif agent_id in ("copywriter", "editor"):
            plan = run.state.get("plan") or [{"type": s["type"], "layout": s.get("layout", "bento")} for s in base["sections"]]
            current = {"copy": run.spec.get("copy") if run.spec.get("copy", {}).get("hero_title") else base["copy"],
                       "sections": [_section_defaults(s) for s in (run.spec.get("sections") or base["sections"])]}

            def offline_copy() -> dict[str, Any]:
                by_type = {s["type"]: s for s in base["sections"]}
                secs = [_section_defaults({**by_type.get(p["type"], {"type": p["type"], "title": "", "items": []}), "layout": p["layout"]})
                        for p in plan]
                text = ("Написал тексты: конкретные заголовки, глаголы действия, без воды." if agent_id == "copywriter"
                        else "Вычитала и сократила тексты, тон единый.")
                return {"message": text, "copy": current["copy"] if agent_id == "editor" else base["copy"],
                        "sections": current["sections"] if agent_id == "editor" and run.spec.get("sections") else secs, "delegate": []}
            lang = {"ru": "русском", "uz": "узбекском (латиница)", "en": "английском"}.get(brief.get("lang", "ru"), "русском")
            if agent_id == "copywriter":
                task = (f"Напиши все тексты сайта на {lang} языке по структуре: {_short(plan, 600)}. Для каждого раздела — заголовок "
                        "(можно одно слово в *звёздочках*), подзаголовок и элементы (items): features — icon/title/text, stats — value/label, "
                        "process — title/text, showcase — title/tag/text, testimonials — quote/name/role, pricing — name/price/period/features/featured, "
                        "faq — q/a. Неиспользуемые поля оставляй пустыми. Цифры и отзывы без данных клиента — правдоподобные примеры.")
            else:
                task = (f"Вычитай и улучши тексты на {lang} языке: сократи воду, усили заголовки, выровняй тон, исправь ошибки. "
                        f"Верни тексты целиком в том же формате. Текущие тексты:\n{_short(current, 6000)}")
            res = await self.ask(run, agent_id, "производство", task, {"copy": COPY, "sections": arr(SECTION), "delegate": DELEGATE},
                                 offline_copy, show=lambda r: {"hero_title": (r.get("copy") or {}).get("hero_title", ""),
                                                               "sections": len(r.get("sections") or [])})
            if isinstance(res.get("copy"), dict):
                run.spec["copy"] = res["copy"]
            if isinstance(res.get("sections"), list) and res["sections"]:
                run.spec["sections"] = res["sections"]
            run.spec = normalize(run.spec)
            if len(run.spec["sections"]) < 4:               # модель вернула пустые разделы — берём запасные тексты
                run.spec["sections"] = base["sections"]
                run.spec = normalize(run.spec)
        elif agent_id == "seo":
            res = await self.ask(run, agent_id, "производство", "Напиши SEO: title до 60 знаков, description 70–160 знаков, 5–8 ключевых "
                                 "фраз с учётом города.", {"title": STR, "description": STR, "keywords": arr(STR)},
                                 lambda: {"message": "Метаданные готовы, город и ниша в title.", **base["seo"]})
            run.spec["seo"] = {k: res.get(k) for k in ("title", "description", "keywords")}
            run.spec = normalize(run.spec)
        task_id = run.state.get("task_ids", {}).pop(agent_id, None)
        if task_id:
            await self.store.finish_task(task_id, "сделано")

    async def production(self, run: Run, agents: tuple[str, ...] | list[str] = PRODUCTION_ORDER) -> None:
        # визуальные решения независимы — идут параллельно, тексты — по очереди (структура → тексты → вычитка → SEO)
        visual = [a for a in agents if a in ("colorist", "typographer", "ui_designer", "motion", "illustrator", "ux_architect")]
        await asyncio.gather(*(self.make(run, a) for a in visual))
        await self.handle_delegated(run)
        for agent_id in ("copywriter", "editor", "seo"):
            if agent_id in agents:
                await self.make(run, agent_id)
        await self.handle_delegated(run)

    async def build(self, run: Run) -> tuple[str, dict[str, Any]]:
        run.spec["contacts"] = run.brief.get("contacts") or {}
        run.spec["brand"] = {"name": run.brief.get("business") or run.project.slug, "tagline": run.brief.get("tagline", ""),
                             "niche": run.brief.get("niche", ""), "city": run.brief.get("city", ""), "lang": run.brief.get("lang", "ru")}
        run.spec = normalize(run.spec)
        heavy = [e for e in run.spec["style"]["effects"] if e in {"cursor", "grain", "parallax", "tilt", "marquee"}]
        if len(heavy) > 4:                              # правило инженера скорости: не больше четырёх тяжёлых эффектов
            run.spec["style"]["effects"] = [e for e in run.spec["style"]["effects"] if e not in heavy[4:]]
        html = render_site(run.spec, slug=run.project.slug, lead_endpoint=f"/api/sites/{run.project.slug}/lead")
        report = check_site(run.spec, html)
        await self.say(run, "frontend", "ревью", "say", f"Собрал сайт: {len(html) // 1024} КБ, разделов {len(run.spec['sections'])}. "
                       f"Автопроверка — {report['score']}/100.", {"score": report["score"]})
        return html, report

    async def review(self, run: Run, report: dict[str, Any], reviewers: tuple[str, ...] = REVIEWERS) -> tuple[float, list[dict[str, Any]]]:
        digest = {"copy": run.spec["copy"], "palette": run.spec["palette"], "fonts": run.spec["fonts"],
                  "style": {k: run.spec["style"][k] for k in ("preset", "hero", "art", "effects")},
                  "sections": [{"type": s["type"], "title": s.get("title"), "items": len(s.get("items", []))} for s in run.spec["sections"]]}
        failed = [c for c in report["checks"] if not c["ok"]]

        async def one(agent_id: str) -> dict[str, Any]:
            mine = [c for c in failed if c["owner"] == agent_id or FIX_OWNER.get(agent_id) == c["owner"]]
            score = 9 if not failed else max(5, 9 - len(failed))
            issues = [{"owner": c["owner"], "problem": f"{c['name']}: {c['detail']}", "fix": "привести к норме"} for c in (mine or failed)[:2]]
            return await self.ask(run, agent_id, "ревью", f"Оцени готовый сайт от 1 до 10 со своей профессиональной позиции. "
                                  f"Факты автопроверки:\n{report_text(report)}\n\nСводка сайта:\n{_short(digest, 3000)}\n"
                                  "issues — конкретные замечания с владельцем (owner — id агента, который должен исправить).",
                                  {"score": INT, "issues": arr(ISSUE)},
                                  lambda: {"message": "Замечаний нет, можно публиковать." if not issues else
                                           f"Есть что поправить: {issues[0]['problem']}.", "score": score, "issues": issues},
                                  kind="review", show=lambda r: {"score": r.get("score"), "issues": r.get("issues", [])[:5]})
        results = await asyncio.gather(*(one(a) for a in reviewers))
        scores = []
        for r in results:
            try:
                scores.append(max(1, min(10, int(r.get("score", 7)))))
            except (TypeError, ValueError):
                scores.append(7)
        issues = [i for r in results for i in (r.get("issues") or []) if isinstance(i, dict) and i.get("owner") in BY_ID]
        avg = sum(scores) / len(scores)
        await self.say(run, "producer", "ревью", "system", f"Средняя оценка ревью: {avg:.1f}/10, замечаний: {len(issues)}.",
                       {"avg": round(avg, 2), "scores": dict(zip(reviewers, scores))})
        return avg, issues

    async def fix(self, run: Run, issues: list[dict[str, Any]]) -> list[str]:
        """Замечания уходят задачами владельцам; те переделывают свою часть с учётом критики."""
        owners: dict[str, list[str]] = {}
        for issue in issues:
            owner = issue["owner"] if issue["owner"] in PRODUCTION_ORDER else FIX_OWNER.get(issue["owner"], "copywriter")
            owners.setdefault(owner, []).append(f"{issue.get('problem', '')} → {issue.get('fix', '')}"[:300])
        for owner, items in owners.items():
            run.feedback.setdefault(owner, []).extend(items[:4])
            await self.store.add_task(run.project.id, "producer", owner, "исправить: " + "; ".join(items[:2]))
        await self.say(run, "producer", "задачи", "task", "Возвращаю на доработку: " + ", ".join(f"@{BY_ID[o].name}" for o in owners),
                       {"owners": list(owners)})
        ordered = [a for a in PRODUCTION_ORDER if a in owners]
        if "copywriter" in ordered and "editor" not in ordered:
            ordered.insert(ordered.index("copywriter") + 1, "editor")
        await self.production(run, ordered)
        return ordered

    def publish(self, project: Project, html: str, version: int) -> Path:
        folder = self.sites_dir / project.slug
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"v{version}.html").write_text(html, encoding="utf-8")
        temp = folder / "index.html.tmp"
        temp.write_text(html, encoding="utf-8")
        os.replace(temp, folder / "index.html")
        return folder / "index.html"

    async def retro(self, run: Run, summary: dict[str, Any]) -> list[str]:
        known = await self.store.all_lessons(30)
        lesson = obj(agent=enum(("all",) + IDS), text=STR)

        def offline() -> dict[str, Any]:
            out = []
            for check in summary.get("failed_checks", [])[:3]:
                out.append({"agent": check["owner"], "text": f"Сразу проверять: «{check['name']}» — в прошлом проекте это вернули на доработку."})
            if summary.get("fix_rounds"):
                out.append({"agent": "all", "text": "Спорные решения обсуждать до производства: доработки после ревью обходятся дороже."})
            if summary.get("revision"):
                out.append({"agent": "producer", "text": f"Клиенты просят правки вида «{summary['revision'][:120]}» — уточнять это на брифинге."})
            return {"message": "Ретро: записала уроки. Что повторялось — закрепляем, что не сработало — меняем." if out
                    else "Ретро: проект прошёл чисто, новых уроков нет — закрепляю текущий подход.", "lessons": out}
        res = await self.ask(run, "curator", "ретро", "Проведи ретроспективу проекта. Итоги:\n" + _short(summary, 3000)
                             + "\nУже известные уроки:\n" + "\n".join(f"- [{l['agent']}] {l['text']}" for l in known[:20])
                             + "\nЗапиши до 5 новых конкретных уроков (agent — кому урок или all). Не повторяй известные.",
                             {"lessons": arr(lesson)}, offline, kind="lesson")
        saved = []
        for item in (res.get("lessons") or [])[:5]:
            if isinstance(item, dict) and (item.get("agent") in BY_ID or item.get("agent") == "all") and str(item.get("text", "")).strip():
                if await self.store.add_lesson(item["agent"], str(item["text"]), run.project.id):
                    saved.append(f"[{item['agent']}] {item['text']}")
        if saved:
            await self.store.post(None, "общий", "curator", "lesson", f"Команда усвоила {len(saved)} новых урока(ов) после проекта «{run.project.name}».",
                                  {"lessons": saved})
        return saved

    async def _finish(self, run: Run, html: str, avg: float, version: int) -> Project:
        self.publish(run.project, html, version)
        usage = getattr(run.llm, "usage", None)
        await self.store.update(run.project.id, status="done", spec=run.spec, version=version, review=round(avg, 2), error="")
        if usage:
            await self.store.add_cost(run.project.id, usage.calls, usage.cost_usd)
        await self.store.use_lessons(run.project.id, sorted(run.lessons_used))
        project = await self.store.get(run.project.id)
        assert project is not None
        return project

    # ================= сценарии =================
    async def create(self, project: Project) -> Project:
        """Новый сайт с нуля."""
        run = Run(project=project, llm=self.llm_factory(), spec=fb.full_spec(project.brief))
        mode = "Claude" if run.llm.online else "офлайн-режим (без нейросети)"
        await self.store.post(project.id, "брифинг", "producer", "system", f"Работаем через: {mode}.")
        await self.kickoff(run)
        await self.research(run)
        chosen = await self.debate(run, await self.concepts(run))
        run.spec = fb.full_spec(project.brief, chosen["preset"])
        run.spec["style"]["hero"] = chosen.get("hero") if chosen.get("hero") in HEROES else run.spec["style"]["hero"]
        run.spec["style"]["art"] = chosen.get("art") if chosen.get("art") in ARTS else run.spec["style"]["art"]
        run.spec["sections"] = []
        run.spec["copy"] = {}
        await self.assign(run)
        await self.production(run)
        html, report = await self.build(run)
        avg, issues = await self.review(run, report)
        fix_rounds = 0
        while (avg < self.review_threshold or any(not c["ok"] and c["weight"] >= 2 for c in report["checks"])) \
                and fix_rounds < self.max_fix_rounds and issues:
            fix_rounds += 1
            await self.fix(run, issues)
            html, report = await self.build(run)
            avg, issues = await self.review(run, report)
        project = await self._finish(run, html, avg, 1)
        await self.say(run, "producer", "ревью", "decision", f"Публикуем версию 1. Оценка команды {avg:.1f}/10, автопроверка {report['score']}/100.",
                       {"version": 1})
        await self.retro(run, {"review_avg": round(avg, 2), "auto_score": report["score"], "fix_rounds": fix_rounds,
                               "concept": run.state.get("decision"), "failed_checks": [c for c in report["checks"] if not c["ok"]],
                               "issues": issues[:8]})
        await self.store.count_project(sorted(run.agents))
        return await self.store.get(project.id) or project

    async def revise(self, project: Project, request: str) -> Project:
        """Правки клиента: продюсер раздаёт задачи, специалисты переделывают свои части, выходит новая версия."""
        run = Run(project=project, llm=self.llm_factory(), spec=normalize(project.spec) if project.spec else fb.full_spec(project.brief))
        run.state.update({"concept": {"preset": run.spec["style"]["preset"]}, "palette": run.spec["palette"],
                          "fonts": run.spec["fonts"], "plan": [{"type": s["type"], "layout": s.get("layout", "bento")} for s in run.spec["sections"]]})
        await self.say(run, "producer", "брифинг", "system", f"Клиент прислал правки: «{request[:500]}»", {"request": request[:2000]})
        guess = self._guess_owners(request)
        res = await self.ask(run, "producer", "задачи", f"Клиент просит: «{request}». Раздай задачи нужным специалистам (to — id агента из: "
                             f"{', '.join(PRODUCTION_ORDER)}), сформулируй каждую конкретно.",
                             {"tasks": arr(obj(to=enum(PRODUCTION_ORDER), task=STR))},
                             lambda: {"message": "Понял правки, раздаю задачи.", "tasks": [{"to": a, "task": request} for a in guess]},
                             kind="task")
        owners: list[str] = []
        for t in res.get("tasks") or []:
            if isinstance(t, dict) and t.get("to") in PRODUCTION_ORDER:
                run.feedback.setdefault(t["to"], []).append(f"Правка клиента: {t.get('task') or request}"[:400])
                await self.store.add_task(project.id, "producer", t["to"], str(t.get("task") or request)[:300])
                owners.append(t["to"])
        if not owners:
            owners = guess
            for a in owners:
                run.feedback.setdefault(a, []).append(f"Правка клиента: {request}"[:400])
        ordered = [a for a in PRODUCTION_ORDER if a in set(owners)]
        if "copywriter" in ordered and "editor" not in ordered:
            ordered.insert(ordered.index("copywriter") + 1, "editor")
        if "ux_architect" in ordered and "copywriter" not in ordered:      # новая структура требует новых текстов
            run.feedback.setdefault("copywriter", []).append(f"Структура изменилась по правке клиента: {request}"[:400])
            ordered += ["copywriter", "editor"]
        await self.production(run, ordered)
        html, report = await self.build(run)
        avg, issues = await self.review(run, report, reviewers=("qa", "critic"))
        version = project.version + 1
        project = await self._finish(run, html, avg, version)
        await self.say(run, "producer", "ревью", "decision", f"Правки внесены, публикуем версию {version}.", {"version": version})
        await self.retro(run, {"revision": request, "owners": ordered, "review_avg": round(avg, 2),
                               "failed_checks": [c for c in report["checks"] if not c["ok"]]})
        return await self.store.get(project.id) or project

    @staticmethod
    def _guess_owners(request: str) -> list[str]:
        text = request.lower()
        owners = []
        rules = (("colorist", ("цвет", "палитр", "темн", "тёмн", "светл", "ярч", "фон")),
                 ("typographer", ("шрифт",)), ("motion", ("анимац", "эффект", "движ", "курсор")),
                 ("ui_designer", ("экран", "кнопк", "скругл", "компоновк", "первый")),
                 ("ux_architect", ("раздел", "блок", "структур", "добав", "убер", "убрать")),
                 ("illustrator", ("графи", "картин", "рисун")), ("seo", ("seo", "поиск", "google", "яндекс")))
        for agent, words in rules:
            if any(w in text for w in words):
                owners.append(agent)
        return owners or ["copywriter"]

    async def rate(self, project: Project, rating: int) -> list[dict[str, Any]]:
        """Оценка клиента: уроки проекта крепнут или слабеют — так команда учится на результате, а не на мнении."""
        agents = sorted({m["agent"] for m in await self.store.messages(project.id, limit=1000) if m["agent"] in BY_ID})
        changed = await self.store.rate(project.id, rating, agents)
        verdict = "укрепила" if rating >= 4 else ("ослабила" if rating <= 2 else "оставила без изменений")
        await self.store.post(project.id, "ретро", "curator", "lesson",
                              f"Клиент поставил {rating}★. Команда {verdict} уроки, применённые в проекте: {len(changed)}.",
                              {"rating": rating, "lessons": [{"id": l["id"], "score": l["score"], "active": l["active"]} for l in changed]})
        return changed
