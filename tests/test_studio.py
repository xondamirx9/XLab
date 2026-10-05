"""Студия: безопасная спецификация, рендерер, конвейер 20 агентов, обучение на оценках, веб-API и мастер в боте."""
from __future__ import annotations

import base64
import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from aiohttp.test_utils import TestClient, TestServer

from app.bot import build_dispatcher
from app.storage import Storage
from app.studio.agents import AGENTS, BY_ID
from app.studio.llm import OfflineLLM
from app.studio.offline import full_spec
from app.studio.pipeline import Studio
from app.studio.quality import check_site
from app.studio.renderer import RUNTIME_HASH, render_site
from app.studio.spec import PRESETS, contrast, normalize, slugify
from app.studio.store import StudioStore
from app.studio.worker import StudioWorker
from app.user_bot import parse_contacts
from app.web import StudioContext, create_app
from test_news import TelegramCase, command, press, text_message
from test_server import PASSWORD, make_config

BRIEF = {"business": "Bodom Coffee", "niche": "кофейня", "description": "Спешелти-кофе и десерты в центре Ташкента",
         "style": "editorial", "colors": "", "lang": "ru", "contacts": {"phone": "+998 90 123 45 67", "telegram": "bodom_coffee"}}


class RecordingLLM(OfflineLLM):
    """Как офлайн-режим, но запоминает запросы и даёт подменить ответ конкретного агента."""

    def __init__(self, overrides: dict[str, Callable[[dict], dict]] | None = None) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, str]] = []          # (агент, system, prompt)
        self.overrides = overrides or {}

    async def json(self, *, system: str, prompt: str, schema: dict[str, Any], effort: str, offline: Callable[[], dict]) -> dict:
        agent = next(a.id for a in AGENTS if f"Твоя роль: {a.emoji} {a.name}," in system)
        self.calls.append((agent, system, prompt))
        data = offline()
        if agent in self.overrides:
            data = self.overrides[agent](data)
        return data


class SpecCase(unittest.TestCase):
    def test_normalize_rejects_garbage_and_fixes_contrast(self) -> None:
        spec = normalize({"style": {"preset": "hack", "hero": "evil", "effects": ["cursor", "rm -rf"], "art_seed": "x"},
                          "palette": {"bg": "#FFFFFF", "text": "#EEEEEE", "muted": "red", "primary": "#FFFF00"},
                          "fonts": {"display": "Comic Sans", "body": "Onest"},
                          "sections": [{"type": "script", "title": "x"}, {"type": "faq", "title": "Q", "items": [{"q": "Да?", "a": "Да."}]}]})
        self.assertEqual(spec["style"]["preset"], "aurora")
        self.assertIn(spec["style"]["hero"], ("aurora",))
        self.assertEqual(spec["style"]["effects"], ["cursor"])
        self.assertEqual(spec["style"]["art_seed"], 0)
        self.assertGreaterEqual(contrast(spec["palette"]["text"], spec["palette"]["bg"]), 7)   # светлый текст на белом исправлен
        self.assertNotEqual(spec["fonts"]["display"], "Comic Sans")
        self.assertEqual([s["type"] for s in spec["sections"]], ["faq", "contact"])          # неизвестный раздел выброшен

    def test_render_escapes_everything(self) -> None:
        evil = '<script>alert(1)</script>"><img src=x onerror=alert(2)>'
        spec = full_spec({**BRIEF, "business": evil, "description": evil})
        spec["copy"]["hero_title"] = evil
        spec["sections"][0]["items"][0]["title"] = evil
        html = render_site(spec, slug="x")
        self.assertNotIn("<script>alert", html)
        self.assertNotIn("<img src=x", html)
        self.assertEqual(len(re.findall(r"<script(?! type=\"application/(?:ld\+)?json\")", html)), 1)   # исполняемый скрипт один

    def test_runtime_hash_matches_page_script(self) -> None:
        html = render_site(full_spec(BRIEF), slug="bodom")
        script = re.search(r'<script id="wl-runtime">(.*?)</script>', html, re.S).group(1)
        digest = "sha256-" + base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
        self.assertEqual(digest, RUNTIME_HASH)                                     # иначе CSP заблокирует скрипт

    def test_every_preset_renders_and_passes_checks(self) -> None:
        seen = set()
        for preset in PRESETS:
            spec = normalize(full_spec(BRIEF, preset))
            html = render_site(spec, slug="bodom")
            report = check_site(spec, html)
            failed = [c["name"] for c in report["checks"] if not c["ok"]]
            self.assertEqual(failed, [], preset)
            self.assertEqual(html.count("<h1"), 1)
            seen.add(re.search(r'class="hero hero-(\w+)"', html).group(1))
        self.assertGreaterEqual(len(seen), 5)                                       # стили действительно разные

    def test_uniqueness_from_seed(self) -> None:
        a = render_site(full_spec({**BRIEF, "business": "Alpha"}, "aurora"))
        b = render_site(full_spec({**BRIEF, "business": "Beta"}, "aurora"))
        art = lambda html: re.search(r'<svg class="art.*?</svg>', html, re.S).group(0)
        self.assertNotEqual(art(a), art(b))

    def test_slugify_and_contacts(self) -> None:
        self.assertEqual(slugify("Кофейня «Бодом»!"), "kofeynya-bodom")
        self.assertEqual(slugify("***"), "site")
        parsed = parse_contacts("тел +998 90 123-45-67, @bodom_coffee, instagram.com/bodom.uz, hi@bodom.uz, Ташкент, Шота Руставели 12, 9:00–22:00")
        self.assertEqual(parsed["phone"], "+998 90 123-45-67")
        self.assertEqual(parsed["telegram"], "bodom_coffee")
        self.assertEqual(parsed["instagram"], "bodom.uz")
        self.assertEqual(parsed["email"], "hi@bodom.uz")
        self.assertEqual(parsed["hours"], "9:00–22:00")
        self.assertIn("Шота Руставели 12", parsed["address"])

    def test_twenty_agents(self) -> None:
        self.assertEqual(len(AGENTS), 20)
        self.assertEqual(len(BY_ID), 20)


class PipelineCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StudioStore(self.root / "studio.sqlite3")
        await self.store.init()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def studio(self, llm: RecordingLLM) -> Studio:
        return Studio(self.store, lambda: llm, self.root / "sites")

    async def test_full_run_debate_tasks_and_publish(self) -> None:
        llm = RecordingLLM()
        project = await self.store.create_project(BRIEF, "bodom-coffee", chat_id=7)
        done = await self.studio(llm).create(project)
        self.assertEqual((done.status, done.version), ("done", 1))
        html = (self.root / "sites" / "bodom-coffee" / "index.html").read_text(encoding="utf-8")
        self.assertIn("Bodom Coffee", html)
        self.assertIn("/api/sites/bodom-coffee/lead", html)
        messages = await self.store.messages(project.id, limit=1000)
        speakers = {m["agent"] for m in messages}
        self.assertGreaterEqual(len(speakers), 18)                                  # почти вся команда высказалась
        channels = {m["channel"] for m in messages}
        self.assertTrue({"брифинг", "исследование", "спор", "задачи", "производство", "ревью", "ретро"} <= channels)
        votes = [m for m in messages if m["kind"] == "vote"]
        self.assertEqual(len(votes), 7 * 2)                                         # 7 спорщиков × 2 раунда
        self.assertTrue(any(m["kind"] == "object" for m in messages))               # был спор, а не только согласие
        self.assertTrue(any(m["kind"] == "decision" for m in messages))
        tasks = await self.store.tasks(project.id)
        self.assertGreaterEqual(len(tasks), 9)
        self.assertTrue(all(t["status"] == "done" for t in tasks))

    async def test_agents_delegate_tasks_to_each_other(self) -> None:
        def strategist(data: dict) -> dict:
            return {**data, "delegate": [{"to": "copywriter", "task": "придумай слоган про утренний кофе"}]}
        llm = RecordingLLM({"strategist": strategist})
        project = await self.store.create_project(BRIEF, "bodom")
        await self.studio(llm).create(project)
        tasks = await self.store.tasks(project.id)
        delegated = [t for t in tasks if t["from_agent"] == "strategist"]
        self.assertEqual([(t["to_agent"], t["status"]) for t in delegated], [("copywriter", "done")])
        replies = [m for m in await self.store.messages(project.id, limit=1000) if m["kind"] == "done" and m["agent"] == "copywriter"]
        self.assertEqual(replies[0]["reply_to"], "strategist")

    async def test_review_sends_work_back_until_fixed(self) -> None:
        state = {"bad": True}

        def copywriter(data: dict) -> dict:
            if state["bad"]:                                                         # первый вариант — слишком длинный заголовок
                state["bad"] = False
                data["copy"] = {**data["copy"], "hero_title": "Очень " * 20 + "длинный заголовок"}
            return data
        llm = RecordingLLM({"copywriter": copywriter})
        project = await self.store.create_project(BRIEF, "bodom")
        done = await self.studio(llm).create(project)
        writes = [c for c in llm.calls if c[0] == "copywriter"]
        self.assertEqual(len(writes), 2)                                            # вернули на доработку
        self.assertIn("Заголовок первого экрана короткий", writes[1][2])            # с конкретным замечанием
        self.assertLessEqual(len(done.spec["copy"]["hero_title"]), 70)

    async def test_lessons_learned_and_used_next_time(self) -> None:
        studio = self.studio(RecordingLLM())
        first = await studio.create(await self.store.create_project(BRIEF, "first"))
        await studio.revise(first, "сделайте цвета темнее")
        lessons = await self.store.all_lessons()
        self.assertTrue(lessons)                                                   # ретро после правок записало урок
        lesson = lessons[0]
        llm = RecordingLLM()
        await self.studio(llm).create(await self.store.create_project(BRIEF, "second"))
        prompts = [system for agent, system, _ in llm.calls if agent == lesson["agent"]]
        self.assertTrue(any(lesson["text"] in p for p in prompts))                  # урок попал в подсказку агента

    async def test_rating_strengthens_or_kills_lessons(self) -> None:
        studio = self.studio(RecordingLLM())
        project = await studio.create(await self.store.create_project(BRIEF, "p1"))
        lesson_id = await self.store.add_lesson("colorist", "Проверять контраст кнопок", project.id)
        await studio.rate(project, 5)
        self.assertEqual((await self.store.all_lessons())[0]["score"], 2.0)
        await studio.rate(project, 1)                                               # переоценка не начисляет второй раз
        self.assertEqual((await self.store.all_lessons())[0]["score"], 2.0)
        bad = await studio.create(await self.store.create_project(BRIEF, "p2"))
        await self.store.add_lesson("critic", "Спорить ради спора", bad.id)
        await studio.rate(bad, 1)
        by_text = {l["text"]: l for l in await self.store.all_lessons()}
        self.assertEqual(by_text["Спорить ради спора"]["active"], 0)                # не подтвердился — выключен
        self.assertNotIn("Спорить ради спора", [l["text"] for l in await self.store.lessons_for("critic")])
        stats = await self.store.agent_stats()
        self.assertEqual(stats["colorist"]["lessons"], 1)
        self.assertGreater(stats["producer"]["score_n"], 0)
        self.assertIsNotNone(lesson_id)

    async def test_revision_makes_new_version(self) -> None:
        studio = self.studio(RecordingLLM())
        project = await studio.create(await self.store.create_project({**BRIEF, "style": "aurora"}, "rev"))
        light_bg = project.spec["palette"]["bg"]
        revised = await studio.revise(project, "сделайте цвета темнее и добавьте раздел с ценами")
        self.assertEqual(revised.version, 2)
        self.assertNotEqual(revised.spec["palette"]["bg"], light_bg)
        self.assertIn("pricing", [s["type"] for s in revised.spec["sections"]])
        self.assertTrue((self.root / "sites" / "rev" / "v1.html").is_file())        # прежняя версия сохранена
        self.assertTrue((self.root / "sites" / "rev" / "v2.html").is_file())


class StudioWebCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.cfg = make_config(self.root)
        storage = Storage(self.root / "orders.sqlite3")
        await storage.init()
        self.store = StudioStore(self.root / "studio.sqlite3")
        await self.store.init()
        self.studio = Studio(self.store, RecordingLLM, self.root / "sites")
        self.woken = 0

        def wake() -> None:
            self.woken += 1
        ctx = StudioContext(store=self.store, studio=self.studio, news=None, sites_dir=self.root / "sites",
                            wake_worker=wake, wake_news=lambda: None)
        self.client = TestClient(TestServer(create_app(self.cfg, storage, lambda: None, studio=ctx)))
        await self.client.start_server()
        self.auth = {"Authorization": "Bearer " + quote(PASSWORD)}

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmp.cleanup()

    async def test_create_build_serve_and_lead(self) -> None:
        self.assertEqual((await self.client.post("/api/studio/projects", json={"business": "X"})).status, 401)
        created = await self.client.post("/api/studio/projects", json={"business": "Nur Beauty", "niche": "салон красоты",
                                                                       "contacts": {"phone": "+998901112233"}}, headers=self.auth)
        project = (await created.json())["project"]
        self.assertEqual((project["slug"], project["status"], self.woken), ("nur-beauty", "queued", 1))
        self.assertEqual((await self.client.get("/sites/nur-beauty/")).status, 404)     # пока не собран
        job = await self.store.next_job()
        await StudioWorker(self.studio, self.store, None).process(job)
        page = await self.client.get("/sites/nur-beauty/")
        self.assertEqual(page.status, 200)
        self.assertIn(RUNTIME_HASH, page.headers["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", page.headers["Content-Security-Policy"].split("style-src")[0])
        self.assertIn("Nur Beauty", await page.text())
        self.assertEqual((await self.client.get("/sites/nur-beauty/v1")).status, 200)
        self.assertEqual((await self.client.get("/sites/..%2F..%2Fstudio.sqlite3/")).status, 404)

        lead = "/api/sites/nur-beauty/lead"
        self.assertEqual((await self.client.post(lead, json={"name": "Аня", "phone": "12"})).status, 400)
        self.assertEqual((await self.client.post(lead, json={"name": "Бот", "phone": "+998901234567", "website": "spam"})).status, 200)
        self.assertEqual((await self.client.post(lead, json={"name": "Аня", "phone": "+998 90 765 43 21", "message": "Запись"})).status, 200)
        self.assertEqual((await self.client.post("/api/sites/nope/lead", json={"name": "Аня", "phone": "+998901234567"})).status, 404)
        leads = await self.store.pending_leads()
        self.assertEqual(leads, [])                                                       # проект из веб-панели: chat_id = 0
        rows = await self.store._run(lambda c: c.execute("SELECT name FROM leads").fetchall())
        self.assertEqual([r["name"] for r in rows], ["Аня"])                             # бот-ловушка не сохранилась

    async def test_council_api_requires_password_but_project_page_by_token(self) -> None:
        project = await self.store.create_project(BRIEF, "bodom")
        await self.studio.create(project)
        self.assertEqual((await self.client.get("/api/council/state")).status, 401)
        state = await (await self.client.get("/api/council/state", headers=self.auth)).json()
        self.assertEqual(len(state["agents"]), 20)
        feed = await (await self.client.get(f"/api/council/feed?project={project.id}", headers=self.auth)).json()
        self.assertGreater(len(feed["messages"]), 30)
        public = await (await self.client.get(f"/api/studio/p/{project.token}/feed")).json()
        self.assertEqual(public["project"]["name"], "Bodom Coffee")
        self.assertNotIn("token", public["project"])
        self.assertNotIn("chat_id", public["project"])
        self.assertEqual((await self.client.get("/api/studio/p/wrongtoken123/feed")).status, 404)
        self.assertEqual((await self.client.get("/council")).status, 200)
        self.assertEqual((await self.client.get(f"/studio/p/{project.token}")).status, 200)
        rate = await self.client.post(f"/api/studio/projects/{project.id}/rate", json={"rating": 5}, headers=self.auth)
        self.assertEqual(rate.status, 200)
        self.assertEqual((await self.store.get(project.id)).rating, 5)


class WizardCase(TelegramCase):
    async def test_order_site_through_bot(self) -> None:
        cfg = make_config(self.root, admin_chat_ids=(111,))
        storage = Storage(self.root / "orders.sqlite3")
        await storage.init()
        store = StudioStore(self.root / "studio.sqlite3")
        await store.init()
        studio = Studio(store, RecordingLLM, self.root / "sites")
        woken = []
        dispatcher = build_dispatcher(cfg, storage, studio_store=store, studio=studio, wake_worker=lambda: woken.append(1))
        chat = 777
        steps = [command(1, chat, "/site"), text_message(2, chat, "Bodom Coffee"), text_message(3, chat, "Кофейня в центре"),
                 press(4, chat, "skip"), press(5, chat, "style:lux"), text_message(6, chat, "чёрный и золото"),
                 text_message(7, chat, "+998 90 123 45 67 @bodom_coffee"), press(8, chat, "lang:uz"), press(9, chat, "order:go")]
        for update in steps:
            await dispatcher.feed_update(self.bot, update)
        projects = await store.list_projects()
        self.assertEqual(len(projects), 1)
        brief = projects[0].brief
        self.assertEqual((brief["business"], brief["style"], brief["colors"], brief["lang"]), ("Bodom Coffee", "lux", "чёрный и золото", "uz"))
        self.assertEqual(brief["contacts"]["telegram"], "bodom_coffee")
        self.assertEqual(projects[0].chat_id, chat)
        self.assertEqual(woken, [1])
        self.assertIn("взялся за ваш сайт", self.telegram.texts(chat)[-1])

        worker = StudioWorker(studio, store, self.bot, "https://varaq.uz")
        done = await worker.process(await store.next_job())
        self.assertEqual(done.version, 1)
        final = self.telegram.texts(chat)[-1]
        self.assertIn("https://varaq.uz/sites/bodom-coffee/", final)

        await dispatcher.feed_update(self.bot, press(10, 999, f"rate:{done.id}:5"))       # чужой проект оценить нельзя
        self.assertEqual((await store.get(done.id)).rating, 0)
        await dispatcher.feed_update(self.bot, press(11, chat, f"rate:{done.id}:2"))
        self.assertEqual((await store.get(done.id)).rating, 2)
        await dispatcher.feed_update(self.bot, text_message(12, chat, "Добавьте раздел с ценами"))  # после низкой оценки — правки
        self.assertEqual((await store.get(done.id)).pending, "Добавьте раздел с ценами")
        revised = await worker.process(await store.next_job())
        self.assertEqual((revised.version, revised.pending), (2, ""))

        await store.add_lead(done.id, "Аня", "+998901234567", "Хочу столик")
        self.assertEqual(await worker.deliver_leads(), 1)
        self.assertIn("Заявка с вашего сайта", self.telegram.texts(chat)[-1])

    async def test_daily_limit(self) -> None:
        cfg = make_config(self.root, admin_chat_ids=(111,), studio_daily_limit=1)
        storage = Storage(self.root / "orders.sqlite3")
        await storage.init()
        store = StudioStore(self.root / "studio.sqlite3")
        await store.init()
        await store.create_project(BRIEF, "one", chat_id=42)
        dispatcher = build_dispatcher(cfg, storage, studio_store=store)
        await dispatcher.feed_update(self.bot, command(1, 42, "/site"))
        self.assertIn("лимит", self.telegram.texts(42)[-1])
        await dispatcher.feed_update(self.bot, command(2, 111, "/site"))                 # администратору можно
        self.assertIn("Шаг 1 из 7", self.telegram.texts(111)[-1])


if __name__ == "__main__":
    unittest.main()


class ClaudeCase(unittest.IsolatedAsyncioTestCase):
    """Запрос к Claude собирается правильно, а сбои превращаются в запасной ответ, а не в падение."""

    def fake(self, text: str = '{"message": "Привет", "score": 9}', stop: str = "end_turn", error: Exception | None = None):
        calls: list[dict] = []

        class Usage:
            input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens = 1000, 200, 0, 0

        class Block:
            type = "text"

        class Response:
            content, stop_reason, usage = [Block()], stop, Usage()

        Block.text = text

        class Messages:
            async def create(self, **kwargs):
                calls.append(kwargs)
                if error:
                    raise error
                return Response()

        class Client:
            class beta:
                messages = Messages()
        return Client(), calls

    async def test_request_shape_and_cost(self) -> None:
        from app.studio.llm import ClaudeLLM, obj, INT, STR
        client, calls = self.fake()
        llm = ClaudeLLM("claude-opus-5-5", client=client)
        schema = obj(message=STR, score=INT)
        result = await llm.json(system="sys", prompt="p", schema=schema, effort="low", offline=lambda: {"message": "офлайн"})
        self.assertEqual(result, {"message": "Привет", "score": 9})
        sent = calls[0]
        self.assertEqual(sent["model"], "claude-opus-5-5")
        self.assertEqual(sent["thinking"], {"type": "adaptive"})
        self.assertEqual(sent["output_config"], {"effort": "low", "format": {"type": "json_schema", "schema": schema}})
        self.assertEqual((sent["fallbacks"], sent["betas"]), ("default", ["server-side-fallback-2026-07-01"]))
        self.assertAlmostEqual(llm.usage.cost_usd, (1000 * 4 + 200 * 20) / 1_000_000)

    async def test_failures_fall_back(self) -> None:
        import anthropic
        from app.studio.llm import ClaudeLLM, obj, STR
        offline = lambda: {"message": "офлайн"}
        for client, _ in (self.fake(stop="refusal"), self.fake(text="не json"),
                          self.fake(error=anthropic.APIConnectionError(request=__import__("httpx2").Request("POST", "https://x")))):
            llm = ClaudeLLM(client=client)
            self.assertEqual(await llm.json(system="", prompt="", schema=obj(message=STR), effort="low", offline=offline), {"message": "офлайн"})
            self.assertEqual(llm.usage.fallbacks, 1)
        client, calls = self.fake()
        llm = ClaudeLLM(client=client, max_calls=1)
        await llm.json(system="", prompt="", schema=obj(message=STR), effort="low", offline=offline)
        self.assertEqual(await llm.json(system="", prompt="", schema=obj(message=STR), effort="low", offline=offline), {"message": "офлайн"})
        self.assertEqual(len(calls), 1)                                              # бюджет проекта не превышен
