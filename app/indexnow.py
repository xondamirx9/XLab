"""IndexNow: сайт сам сообщает Bing, Яндексу и другим поисковикам, что его страницы изменились.

Протокол общий: один запрос на api.indexnow.org, и адреса получают все поисковики, которые его
поддерживают (Bing — а через него DuckDuckGo, Yahoo и поиск ChatGPT, — Яндекс, Seznam, Naver).
Права на сайт подтверждает ключ: файл site/verify/<ключ>.txt с тем же ключом внутри, сервер
отдаёт его из корня сайта. Google IndexNow не читает — ему хватает sitemap.xml и Search Console.

Когда отправляем: после выкладки новой версии (файл страницы свежий — моложе часа; пробуждение
сервиса на бесплатном Render без новой версии не считается) и после публикации из админки.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp

from app import seo
from app.config import Config

log = logging.getLogger("site.indexnow")

ENDPOINT = "https://api.indexnow.org/indexnow"
KEY = re.compile(r"[0-9a-f]{32}")
FRESH = 3600            # страница моложе часа — значит, это новая версия, а не пробуждение после сна
START_DELAY = 60        # пусть Render сначала переключит посетителей на новую версию


def find_key(verify_dir: Path) -> str:
    """Ключ IndexNow: файл <ключ>.txt в site/verify, внутри — тот же ключ."""
    for path in sorted(verify_dir.glob("*.txt")):
        if KEY.fullmatch(path.stem) and path.read_text(encoding="utf-8", errors="replace").strip() == path.stem:
            return path.stem
    return ""


def payload(base: str, key: str) -> dict:
    """Запрос IndexNow: три языковые версии страницы и где лежит ключ."""
    return {"host": urlsplit(base).hostname or "", "key": key, "keyLocation": f"{base}/{key}.txt",
            "urlList": [seo.page_url(base, lang) for lang in seo.LANGS]}


class IndexNow:
    def __init__(self, cfg: Config, endpoint: str = ENDPOINT, start_delay: float = START_DELAY, fresh: float = FRESH) -> None:
        self.cfg, self.endpoint, self.start_delay, self.fresh = cfg, endpoint, start_delay, fresh
        self._wake = asyncio.Event()
        self.sent = 0                       # сколько раз поисковики приняли адреса — для проверок

    def wake(self) -> None:
        """Сайт опубликован из админки: отправить адреса заново."""
        self._wake.set()

    def _base(self) -> str:
        try:
            meta = seo.read_meta(self.cfg.index_file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            return ""
        return meta.site or self.cfg.public_url

    async def submit(self) -> bool:
        key = find_key(self.cfg.site_dir / "verify")
        base = self._base()
        if not key or not base:
            log.info("IndexNow: пропускаем — %s", "нет ключа в site/verify" if not key else "неизвестен адрес сайта")
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True, headers={"User-Agent": "CodexLab-site/1.0"}) as session:
                async with session.post(self.endpoint, json=payload(base, key)) as response:
                    status = response.status
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("IndexNow: поисковики недоступны (%s)", type(exc).__name__)
            return False
        if status in (200, 202):
            self.sent += 1
            log.info("IndexNow: адреса сайта отправлены поисковикам (%s)", status)
            return True
        log.warning("IndexNow: поисковики не приняли адреса, ответ %s", status)
        return False

    def _fresh(self) -> bool:
        try:
            return time.time() - self.cfg.index_file.stat().st_mtime < self.fresh
        except OSError:
            return False

    async def _wait(self, stop: asyncio.Event, timeout: float | None) -> None:
        waiters = [asyncio.create_task(self._wake.wait()), asyncio.create_task(stop.wait())]
        _, pending = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()

    async def run(self, stop: asyncio.Event) -> None:
        if not self.cfg.indexnow:
            return
        await self._wait(stop, self.start_delay)
        published = self._wake.is_set()
        self._wake.clear()
        if not stop.is_set() and (published or self._fresh()):
            await self.submit()
        while not stop.is_set():
            await self._wait(stop, None)
            if stop.is_set():
                return
            self._wake.clear()
            await self.submit()
