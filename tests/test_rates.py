"""Курс доллара: разбор ответов NBU и ЦБ, кеш, запасной источник, последний известный курс, /api/rate.
Сеть здесь не нужна: вместо nbu.uz и cbu.uz — подменённая функция загрузки."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import aiohttp
from aiohttp.test_utils import TestClient, TestServer

from app.rates import CBU_URL, FRESH_FOR, MAX_AGE, NBU_URL, RETRY_AFTER, RateService, parse_cbu, parse_nbu
from app.storage import Storage
from app.web import create_app
from test_server import PAGE, make_config

NBU = [{"title": "UAE Dirham", "code": "AED", "cb_price": "3445.67", "nbu_buy_price": "", "nbu_cell_price": "", "date": "20.10.2026 09:00:00"},
       {"title": "US Dollar", "code": "USD", "cb_price": "12 650,43", "nbu_buy_price": "12600", "nbu_cell_price": "12700",
        "date": "20.10.2026 09:00:00"}]
CBU = [{"id": 69, "Code": "840", "Ccy": "USD", "Nominal": "1", "Rate": "12648.10", "Date": "19.10.2026"}]


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeSources:
    """Отвечает за nbu.uz и cbu.uz; down — какие источники «лежат»."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.calls: list[str] = []

    async def __call__(self, url: str) -> object:
        self.calls.append(url)
        if url in self.down:
            raise aiohttp.ClientConnectionError("нет связи")
        return {NBU_URL: NBU, CBU_URL: CBU}[url]


class ParseCase(unittest.TestCase):
    def test_nbu_and_cbu_answers(self) -> None:
        self.assertEqual(parse_nbu(NBU), (12650.43, "2026-10-20"))
        self.assertEqual(parse_cbu(CBU), (12648.10, "2026-10-19"))
        no_official = [{**NBU[1], "cb_price": ""}]
        self.assertEqual(parse_nbu(no_official), (12700.0, "2026-10-20"))           # нет курса ЦБ — берём продажу NBU

    def test_garbage_is_not_a_rate(self) -> None:
        for data in (None, {}, "USD", [{"code": "USD", "cb_price": "abc"}], [{"code": "USD", "cb_price": "12"}],
                     [{"code": "EUR", "cb_price": "13900"}]):
            self.assertIsNone(parse_nbu(data), data)
        self.assertIsNone(parse_cbu([{"Ccy": "USD", "Rate": "0"}]))


class ServiceCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "rate.json"
        self.clock, self.sources = Clock(), FakeSources()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    def service(self) -> RateService:
        return RateService(self.file, fetch=self.sources, clock=self.clock)

    async def test_cached_for_an_hour(self) -> None:
        rates = self.service()
        first = await rates.usd()
        self.assertEqual((first.usd, first.source, first.date), (12650.43, "nbu", "2026-10-20"))
        self.clock.now += FRESH_FOR - 1
        await rates.usd()
        self.assertEqual(self.sources.calls, [NBU_URL])                              # повторно на nbu.uz не ходили
        self.clock.now += 2
        await rates.usd()
        self.assertEqual(self.sources.calls, [NBU_URL, NBU_URL])

    async def test_falls_back_to_central_bank(self) -> None:
        self.sources.down.add(NBU_URL)
        rate = await self.service().usd()
        self.assertEqual((rate.usd, rate.source), (12648.10, "cbu"))

    async def test_last_known_rate_survives_outage_and_restart(self) -> None:
        await self.service().usd()
        self.sources.down |= {NBU_URL, CBU_URL}
        self.clock.now += FRESH_FOR + 1
        restarted = self.service()                                                  # курс прочитан с диска
        self.assertEqual((await restarted.usd()).usd, 12650.43)
        calls = len(self.sources.calls)
        await restarted.usd()
        self.assertEqual(len(self.sources.calls), calls)                            # после сбоя не долбим источники
        self.clock.now += RETRY_AFTER + 1
        await restarted.usd()
        self.assertGreater(len(self.sources.calls), calls)
        self.clock.now += MAX_AGE
        self.assertIsNone(await restarted.usd())                                     # недельный курс уже не показываем

    async def test_no_rate_at_all(self) -> None:
        self.sources.down |= {NBU_URL, CBU_URL}
        self.assertIsNone(await self.service().usd())
        self.assertFalse(self.file.exists())

    async def test_api_rate(self) -> None:
        root = Path(self.tmp.name)
        (root / "site").mkdir()
        (root / "site" / "index.html").write_text(PAGE, encoding="utf-8")
        cfg = make_config(root)
        storage = Storage(cfg.data_dir / "orders.sqlite3")
        await storage.init()
        for rates, status in ((None, 503), (self.service(), 200)):
            client = TestClient(TestServer(create_app(cfg, storage, lambda: None, rates=rates)))
            await client.start_server()
            try:
                response = await client.get("/api/rate")
                self.assertEqual(response.status, status)
                if status == 200:
                    self.assertEqual(await response.json(), {"ok": True, "usd": 12650.43, "date": "2026-10-20", "source": "nbu"})
            finally:
                await client.close()
        self.sources.down |= {NBU_URL, CBU_URL}
        broken = RateService(root / "other.json", fetch=self.sources, clock=self.clock)
        client = TestClient(TestServer(create_app(cfg, storage, lambda: None, rates=broken)))
        await client.start_server()
        try:
            self.assertEqual((await client.get("/api/rate")).status, 503)
        finally:
            await client.close()
        self.assertEqual(json.loads(self.file.read_text(encoding="utf-8"))["source"], "nbu")


if __name__ == "__main__":
    unittest.main()
