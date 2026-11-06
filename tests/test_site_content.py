"""Тексты и цены сайта в базе, вёрстка из git: сведение правок, релиз, публикация с прежним кодом, откат."""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.parse import quote

import psycopg
from aiohttp.test_utils import TestClient, TestServer

from app.brief_bot import SitePricing
from app.config import Config
from app.site_content import KEEP_PUBLICATIONS, SiteContent, SiteStore, code_of, data_block, inject, merge, to_json
from app.site_versions import reset, restore
from app.storage import Storage
from app.web import create_app
from pgdb import database_for

PASSWORD = "очень-длинный-пароль-123"
GIT = {"v": 2, "brand": {"name": "Codex Lab", "phone": "+998 71 000 00 00"},
       "texts": {"heroTitle": "Сайты под ключ", "seoTitle": "Codex Lab"},
       "pricing": {"currency": "млн сум", "types": {"landing": {"price": [3, 9], "weeks": [1, 2]}}},
       "faq": [{"q": "Сроки?", "a": "От недели"}]}


def page(data: dict, js: str = "draw();", css: str = "body{}") -> str:
    """Страница того же устройства, что site/index.html: стили, данные и код в своих блоках."""
    return ('<!doctype html>\n<html lang="ru">\n<head>\n<title>Codex Lab</title>\n'
            f'<style id="site-css">{css}</style>\n</head>\n<body>\n<div id="site"></div>\n'
            f'<script type="application/json" id="site-data">{to_json(data)}</script>\n'
            f'<script id="site-js">{js}</script>\n</body>\n</html>\n')


def edited(data: dict, **changes: object) -> dict:
    """Копия данных с правками по путям вида «texts.heroTitle»."""
    import copy
    out = copy.deepcopy(data)
    for path, value in changes.items():
        node = out
        *parents, last = path.split("__")
        for key in parents:
            node = node.setdefault(key, {})
        node[last] = value
    return out


class MergeTest(unittest.TestCase):
    def test_admin_change_stays_untouched_follows_git(self) -> None:
        admin = edited(GIT, texts__heroTitle="Сайты за неделю")
        new_git = edited(GIT, brand__phone="+998 71 111 11 11", texts__badge="Новое поле")
        result = merge(GIT, admin, new_git)
        self.assertEqual(result["texts"]["heroTitle"], "Сайты за неделю")       # меняли в админке — как в админке
        self.assertEqual(result["brand"]["phone"], "+998 71 111 11 11")         # не трогали — как в новом git
        self.assertEqual(result["texts"]["badge"], "Новое поле")                # новое поле из git появилось само

    def test_lists_are_whole_values(self) -> None:
        admin = edited(GIT, faq=[{"q": "Цена?", "a": "От 3 млн"}])
        new_git = edited(GIT, faq=[{"q": "Сроки?", "a": "От недели"}, {"q": "Гарантия?", "a": "Год"}])
        self.assertEqual(merge(GIT, admin, new_git)["faq"], [{"q": "Цена?", "a": "От 3 млн"}])
        self.assertEqual(merge(GIT, GIT, new_git)["faq"], new_git["faq"])         # список не трогали — новый из git

    def test_removed_keys(self) -> None:
        admin = {k: v for k, v in GIT.items() if k != "faq"}                    # раздел убрали в админке
        self.assertNotIn("faq", merge(GIT, admin, GIT))
        new_git = {k: v for k, v in GIT.items() if k != "faq"}                  # раздел убрали в git, админка не трогала
        self.assertNotIn("faq", merge(GIT, GIT, new_git))
        changed = edited(GIT, faq=[{"q": "Своё", "a": "Своё"}])                 # убрали в git, а в админке меняли — остаётся
        self.assertEqual(merge(GIT, changed, new_git)["faq"], [{"q": "Своё", "a": "Своё"}])


class BlocksTest(unittest.TestCase):
    def test_inject_changes_only_data_and_escapes_script(self) -> None:
        text = page(GIT)
        hostile = edited(GIT, texts__heroTitle="</script><script>alert(1)</script>")
        out = inject(text, hostile)
        self.assertEqual(data_block(out)["texts"]["heroTitle"], "</script><script>alert(1)</script>")
        self.assertEqual(out.count("</script>"), text.count("</script>"))       # из данных блок не закрыть
        self.assertIn('<script id="site-js">draw();</script>', out)

    def test_real_site_page(self) -> None:
        real = (Path(__file__).resolve().parent.parent / "site" / "index.html").read_text(encoding="utf-8")
        data = data_block(real)
        self.assertIsInstance(data, dict)
        self.assertIn("pricing", data)
        css, js = code_of(real)
        self.assertGreater(len(css), 1000)
        self.assertIn("buildDocument", js)                                      # тот самый код, что собирает страницу
        changed = edited(data, texts__heroTitle="Проверка")
        out = inject(real, changed)
        self.assertEqual(data_block(out), changed)
        self.assertEqual(code_of(out), (css, js))                               # код и стили не тронуты

    def test_broken_or_missing_data(self) -> None:
        self.assertIsNone(data_block("<!doctype html><p>нет данных</p>"))
        self.assertIsNone(data_block('<script type="application/json" id="site-data">{oops</script>'))


class LiveCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "index.html"
        self.file.write_text(page(GIT), encoding="utf-8")
        self.store = SiteStore(await database_for(self))
        self.site = SiteContent(self.file, self.store, check_every=0)

    def release(self, data: dict, js: str = "draw2();") -> None:
        """Релиз: новый файл из git — другой код и другие данные."""
        self.file.write_text(page(data, js), encoding="utf-8")

    async def test_without_publications_git_page(self) -> None:
        live = await self.site.page()
        self.assertEqual((live.source, live.text), ("git", page(GIT)))

    async def test_publication_shown_whole_until_release(self) -> None:
        admin = edited(GIT, texts__heroTitle="Сайты за неделю")
        number, whole = await self.site.publish(page(admin))
        self.assertEqual((number, whole), (1, True))
        live = await self.site.page()
        self.assertEqual((live.source, live.text), ("published", page(admin)))

        self.release(edited(GIT, brand__phone="+998 71 111 11 11", texts__badge="Новое"))
        live = await self.site.page()
        self.assertEqual(live.source, "merged")
        self.assertIn('<script id="site-js">draw2();</script>', live.text)    # код — новый, из git
        self.assertEqual(live.data["texts"]["heroTitle"], "Сайты за неделю")  # правка админки уцелела
        self.assertEqual(live.data["brand"]["phone"], "+998 71 111 11 11")     # нетронутое — из нового git
        self.assertEqual(live.data["texts"]["badge"], "Новое")

    async def test_publication_from_old_code_keeps_only_data(self) -> None:
        self.release(GIT)                                                      # пока админка была открыта, вышел релиз
        admin = edited(GIT, texts__heroTitle="Сайты за неделю")
        number, whole = await self.site.publish(page(admin, js="draw();"))   # страница собрана прежним кодом
        self.assertFalse(whole)
        live = await self.site.page()
        self.assertEqual(live.source, "merged")
        self.assertIn('<script id="site-js">draw2();</script>', live.text)    # посетитель получает новый код
        self.assertEqual(live.data["texts"]["heroTitle"], "Сайты за неделю")

    async def test_other_server_sees_publication(self) -> None:
        web = SiteContent(self.file, self.store, check_every=3600)            # сервер сайта на другой машине
        self.assertEqual((await web.page()).source, "git")
        await self.site.publish(page(edited(GIT, texts__heroTitle="Новое")))
        self.assertEqual((await web.page()).source, "git")                   # до проверки базы — прежняя страница
        web.invalidate()
        self.assertEqual((await web.page()).data["texts"]["heroTitle"], "Новое")

    async def test_database_outage_keeps_last_page(self) -> None:
        down = False
        real_latest = self.store.latest_id

        async def latest_id() -> int:
            if down:
                raise psycopg.OperationalError("connection refused")
            return await real_latest()
        self.store.latest_id = latest_id                                       # type: ignore[method-assign]
        await self.site.publish(page(edited(GIT, texts__heroTitle="Из админки")))
        self.assertEqual((await self.site.page()).data["texts"]["heroTitle"], "Из админки")

        down = True                                                            # машину с базой перезагружают
        with self.assertLogs("site.content", "WARNING"):
            live = await self.site.page()                                      # базу спрашивает фон, посетитель не ждёт
            await self.site.checked()
        self.assertEqual(live.data["texts"]["heroTitle"], "Из админки")       # посетитель видит последнее прочитанное
        self.assertEqual((await self.site.page()).data["texts"]["heroTitle"], "Из админки")
        self.release(edited(GIT, texts__badge="Новое"))                        # и релиз в это время тоже доходит
        live = await self.site.page()
        self.assertEqual((live.source, live.data["texts"]["heroTitle"], live.data["texts"]["badge"]),
                         ("merged", "Из админки", "Новое"))

        down = False
        await self.store.add(data=edited(GIT, texts__heroTitle="После"), base=GIT, html=None, template_sha="")
        with self.assertLogs("site.content", "INFO"):
            await self.site.page()
            await self.site.checked()
        self.assertEqual((await self.site.page()).data["texts"]["heroTitle"], "После")

    async def test_visitor_never_waits_for_database_check(self) -> None:
        await self.site.page()
        slow = asyncio.Event()
        real_latest = self.store.latest_id

        async def latest_id() -> int:
            await slow.wait()                                                  # база «висит»
            return await real_latest()
        self.store.latest_id = latest_id                                       # type: ignore[method-assign]
        live = await asyncio.wait_for(self.site.page(), 1)                    # ответ — сразу, из прочитанного
        self.assertEqual(live.source, "git")
        await asyncio.wait_for(self.site.page(), 1)                           # вторая проверка не начинается, пока идёт первая
        slow.set()
        await self.site.checked()

    async def test_bot_prices_follow_publication(self) -> None:
        pricing = SitePricing(self.site)
        self.assertEqual((await pricing.fresh())["types"]["landing"]["price"], [3.0, 9.0])
        await self.site.publish(page(edited(GIT, pricing__types={"landing": {"price": [4, 10], "weeks": [1, 2]}})))
        self.assertEqual((await pricing.fresh())["types"]["landing"]["price"], [4.0, 10.0])
        self.assertEqual(pricing.get()["types"]["landing"]["price"], [4.0, 10.0])

    async def test_history_is_limited_and_restorable(self) -> None:
        for n in range(KEEP_PUBLICATIONS + 2):
            await self.site.publish(page(edited(GIT, texts__heroTitle=f"Версия {n}")))
        history = await self.store.history()
        self.assertEqual(len(history), KEEP_PUBLICATIONS)
        self.assertEqual(history[0]["id"], KEEP_PUBLICATIONS + 2)
        oldest = history[-1]["id"]
        await restore(self.store, oldest)
        self.site.invalidate()
        self.assertEqual((await self.site.page()).data["texts"]["heroTitle"], f"Версия {oldest - 1}")
        with self.assertRaises(LookupError):
            await restore(self.store, 1)                                       # самые старые уже удалены

    async def test_reset_returns_git_texts(self) -> None:
        await self.site.publish(page(edited(GIT, texts__heroTitle="Правка")))
        await reset(self.store, self.file.read_text(encoding="utf-8"))
        self.site.invalidate()
        self.release(edited(GIT, texts__heroTitle="Из git"))                  # и дальше всё берётся из git
        self.assertEqual((await self.site.page()).data["texts"]["heroTitle"], "Из git")


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "site").mkdir()
        self.file = root / "site" / "index.html"
        self.file.write_text(page(GIT), encoding="utf-8")
        cfg = Config(bot_token="", admin_chat_ids=(111,), admin_password=PASSWORD, host="127.0.0.1", port=0,
                     site_dir=root / "site", data_dir=root / "data", trust_proxy=False)
        storage = Storage(await database_for(self))
        self.client = TestClient(TestServer(create_app(cfg, storage, lambda: None)))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def health(self) -> str:
        return (await (await self.client.get("/api/health")).json())["site"]

    async def test_release_keeps_admin_texts(self) -> None:
        headers = {"Authorization": "Bearer " + quote(PASSWORD)}
        admin = edited(GIT, texts__heroTitle="Сайты за неделю")
        self.assertEqual((await self.client.post("/api/publish", data=page(admin), headers=headers)).status, 200)
        self.assertEqual(await self.health(), "published")
        self.assertEqual(data_block(await (await self.client.get("/prices")).text())["texts"]["heroTitle"], "Сайты за неделю")

        self.file.write_text(page(edited(GIT, texts__badge="Новое"), js="draw2();"), encoding="utf-8")   # релиз
        body = await (await self.client.get("/")).text()
        self.assertEqual(await self.health(), "merged")
        self.assertIn("draw2();", body)
        self.assertEqual(data_block(body)["texts"]["heroTitle"], "Сайты за неделю")
        self.assertEqual(data_block(body)["texts"]["badge"], "Новое")

    async def test_page_without_data_is_refused(self) -> None:
        headers = {"Authorization": "Bearer " + quote(PASSWORD)}
        broken = page(GIT).replace(to_json(GIT), "{oops")
        self.assertEqual((await self.client.post("/api/publish", data=broken, headers=headers)).status, 400)
        self.assertEqual(await self.health(), "git")


if __name__ == "__main__":
    unittest.main()
