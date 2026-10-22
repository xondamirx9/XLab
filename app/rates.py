"""Курс доллара для цен в USD. Основной источник — лента курсов NBU (nbu.uz), запасной — ЦБ (cbu.uz).

Курс меняется раз в день, поэтому держим его в памяти час и сохраняем на диск: после перезапуска
или пока NBU недоступен сайт и бот показывают последний полученный курс, а не пустоту.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

import aiohttp

log = logging.getLogger("site.rates")

NBU_URL = "https://nbu.uz/en/exchange-rates/json/"
CBU_URL = "https://cbu.uz/ru/arkhiv-kursov-valyut/json/USD/"
FRESH_FOR = 3600            # секунд: курс обновляется раз в день, чаще ходить незачем
RETRY_AFTER = 300           # после неудачи источники не дёргаем чаще раза в 5 минут
MAX_AGE = 7 * 86400         # курс старше недели уже не показываем: лучше честно сказать, что его нет
MIN_RATE, MAX_RATE = 1000.0, 1_000_000.0
TASHKENT = timezone(timedelta(hours=5))

Fetcher = Callable[[str], Awaitable[object]]     # адрес → разобранный JSON


@dataclass(frozen=True)
class Rate:
    usd: float        # сум за 1 доллар
    date: str         # дата курса, ГГГГ-ММ-ДД
    source: str       # nbu или cbu
    fetched: float    # когда получен, время Unix


def _number(value: object) -> float | None:
    """'12 650,43', '12650.43' или число → 12650.43. Всё, что не похоже на курс сума, отбрасываем."""
    try:
        number = float(str(value).replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None
    return number if MIN_RATE <= number <= MAX_RATE else None


def _day(raw: object) -> str:
    """'20.10.2026', '20.10.2026 09:00:00' или '2026-10-20' → '2026-10-20'. Непонятное — пустая строка."""
    text = str(raw or "").strip()
    for pattern in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d.%m.%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def parse_nbu(data: object) -> tuple[float, str] | None:
    """Лента nbu.uz — список валют; у доллара code = USD. Берём официальный курс (cb_price), который публикует NBU,
    а если его нет — курс продажи в самом NBU (поле так и называется: nbu_cell_price)."""
    items = data if isinstance(data, list) else []
    for item in items:
        if not isinstance(item, dict) or str(item.get("code", "")).upper() != "USD":
            continue
        usd = _number(item.get("cb_price")) or _number(item.get("nbu_cell_price"))
        if usd:
            return usd, _day(item.get("date"))
    return None


def parse_cbu(data: object) -> tuple[float, str] | None:
    """Ответ cbu.uz по одной валюте: [{"Ccy": "USD", "Rate": "12650.43", "Date": "20.10.2026", ...}]."""
    items = data if isinstance(data, list) else [data]
    for item in items:
        if isinstance(item, dict) and str(item.get("Ccy", "")).upper() == "USD":
            usd = _number(item.get("Rate"))
            if usd:
                return usd, _day(item.get("Date"))
    return None


async def http_json(url: str) -> object:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True, headers={"User-Agent": "CodexLab-site/1.0"}) as session:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.json(content_type=None)


SOURCES: tuple[tuple[str, str, Callable[[object], tuple[float, str] | None]], ...] = (
    ("nbu", NBU_URL, parse_nbu),
    ("cbu", CBU_URL, parse_cbu),
)


class RateService:
    """Отдаёт свежий курс; при сбое источников — последний известный, пока ему не больше недели."""

    def __init__(self, cache_file: Path, fetch: Fetcher = http_json, clock: Callable[[], float] = time.time) -> None:
        self._file, self._fetch_json, self._clock = cache_file, fetch, clock
        self._rate = self._load()
        self._failed_at = 0.0
        self._lock = asyncio.Lock()

    async def usd(self) -> Rate | None:
        if self._is_fresh():
            return self._rate
        if self._clock() - self._failed_at < RETRY_AFTER:
            return self._usable()
        async with self._lock:
            if not self._is_fresh():             # пока ждали замок, курс мог обновить соседний запрос
                fresh = await self._fetch()
                if fresh is None:
                    self._failed_at = self._clock()
                else:
                    self._rate = fresh
                    self._save(fresh)
        return self._usable()

    def _is_fresh(self) -> bool:
        return self._rate is not None and self._clock() - self._rate.fetched < FRESH_FOR

    def _usable(self) -> Rate | None:
        return self._rate if self._rate is not None and self._clock() - self._rate.fetched < MAX_AGE else None

    async def _fetch(self) -> Rate | None:
        for source, url, parse in SOURCES:
            try:
                found = parse(await self._fetch_json(url))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
                log.warning("курс %s не получен: %s", source, type(exc).__name__)
                continue
            if found is None:
                log.warning("курс %s: в ответе нет доллара", source)
                continue
            usd, day = found
            return Rate(usd=usd, date=day or datetime.now(TASHKENT).strftime("%Y-%m-%d"), source=source, fetched=self._clock())
        return None

    def _load(self) -> Rate | None:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
            rate = Rate(usd=float(data["usd"]), date=str(data["date"]), source=str(data["source"]), fetched=float(data["fetched"]))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        return rate if MIN_RATE <= rate.usd <= MAX_RATE else None

    def _save(self, rate: Rate) -> None:
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            temp = self._file.with_suffix(".tmp")
            temp.write_text(json.dumps(asdict(rate)), encoding="utf-8")
            os.replace(temp, self._file)
        except OSError as exc:                   # без файла курс всё равно работает из памяти
            log.warning("курс не сохранён на диск: %s", type(exc).__name__)
