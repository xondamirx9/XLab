"""Тексты и цены сайта — в базе, вёрстка и код — из git (site/index.html).

Файл site/index.html меняют только релизы. Публикация из админки присылает страницу целиком; сервер берёт
из неё данные (блок site-data) и запоминает публикацию в базе (таблица site_publications):
- данные из админки;
- данные файла из git в эту минуту — от них считается, что именно меняли в админке;
- саму страницу — если её собрал тот же код, что сейчас в git.

Что видит посетитель:
- публикаций нет — страницу из git;
- файл в git не менялся с последней публикации — опубликованную страницу целиком: разметка, заголовки и
  разметка для поиска собраны по данным из админки;
- файл в git изменился (релиз) или админка была открыта на прежней версии кода — страницу из git с данными,
  сведёнными так: поле, которое меняли в админке, — как в админке; остальное — как в новой версии из git,
  новые поля из git появляются сами. Страница рисует себя по данным, поэтому посетитель видит всё сразу;
  разметку для поиска обновит следующая публикация из админки.

Несколько серверов с одной базой (сайт и админка на разных машинах) видят новую публикацию в течение
CHECK_EVERY секунд, тот, кто опубликовал, — сразу. Пока базы нет (машину с ней перезагружают), посетители
получают страницу с последней публикацией, которую сервер успел прочитать: сайт не ждёт базу и не падает.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from app import seo
from app.db import Conn, PgStore, utc_now

log = logging.getLogger("site.content")
KEEP_PUBLICATIONS = 20       # столько последних публикаций хранится — к ним можно вернуться
CHECK_EVERY = 5.0            # как часто, в секундах, спрашивать базу, нет ли новой публикации

DATA_START = '<script type="application/json" id="site-data">'
_CSS = re.compile(r'<style id="site-css">(.*?)</style>', re.S)
_JS = re.compile(r'<script id="site-js">(.*?)</script>', re.S)
_MISSING = object()


# ---------- данные страницы ----------
def data_block(page: str) -> dict[str, Any] | None:
    """Данные сайта из страницы (блок site-data) или None, если блока нет или он битый."""
    start = page.find(DATA_START)
    if start < 0:
        return None
    end = page.find("</script>", start)
    if end < 0:
        return None
    try:
        data = json.loads(page[start + len(DATA_START):end])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def to_json(data: Any) -> str:
    """JSON для блока <script>: как JSON.stringify на странице, и «</script>» в тексте не закроет блок."""
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def inject(page: str, data: dict[str, Any]) -> str:
    """Страница с другими данными: меняется только блок site-data, вёрстка и код — как были."""
    start = page.find(DATA_START)
    end = page.find("</script>", start) if start >= 0 else -1
    if start < 0 or end < 0:
        return page
    return page[:start + len(DATA_START)] + to_json(data) + page[end:]


def code_of(page: str) -> tuple[str, str]:
    """Стили и код страницы: по ним видно, той ли версией сайта собрана присланная страница."""
    css, js = _CSS.search(page), _JS.search(page)
    return (css.group(1) if css else "", js.group(1) if js else "")


def merge(base: Any, ours: Any, theirs: Any) -> Any:
    """Сводит правки админки с новой версией из git.

    base — данные из git на момент публикации, ours — данные из админки, theirs — данные из git сейчас.
    Значение, которое в админке поменяли (ours ≠ base), остаётся как в админке; нетронутое берётся из git.
    Словари сводятся по ключам, списки и простые значения — целиком.
    """
    if isinstance(ours, dict) and isinstance(theirs, dict):
        old = base if isinstance(base, dict) else {}
        result: dict[str, Any] = {}
        for key in [*theirs, *(key for key in ours if key not in theirs)]:
            if key not in ours:
                if key not in old:               # новое поле из git
                    result[key] = theirs[key]
                continue                         # иначе поле убрали в админке — не возвращаем
            if key not in theirs:
                if key in old and ours[key] == old[key]:
                    continue                     # поле убрали в git, а в админке его не трогали
                result[key] = ours[key]
                continue
            result[key] = merge(old.get(key, _MISSING), ours[key], theirs[key])
        return result
    if base is not _MISSING and ours == base:
        return theirs
    return ours


# ---------- публикации в базе ----------
@dataclass(frozen=True)
class Publication:
    id: int
    created_at: str
    template_sha: str
    data: dict[str, Any]
    base: dict[str, Any]
    html: str | None

    @property
    def whole(self) -> bool:
        """Есть ли страница целиком (а не только данные)."""
        return self.html is not None


def _publication(row: dict[str, Any]) -> Publication:
    return Publication(id=int(row["id"]), created_at=row["created_at"], template_sha=row["template_sha"],
                       data=json.loads(row["data_json"]), base=json.loads(row["base_json"]), html=row["html"])


class SiteStore(PgStore):
    async def add(self, *, data: dict[str, Any], base: dict[str, Any], html: str | None, template_sha: str) -> int:
        """Новая публикация. Старше KEEP_PUBLICATIONS последних — удаляются."""
        def work(conn: Conn) -> int:
            row = conn.execute("INSERT INTO site_publications (created_at, template_sha, data_json, base_json, html) "
                               "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                               (utc_now(), template_sha, json.dumps(data, ensure_ascii=False),
                                json.dumps(base, ensure_ascii=False), html)).fetchone()
            assert row is not None
            conn.execute("DELETE FROM site_publications WHERE id NOT IN "
                         "(SELECT id FROM site_publications ORDER BY id DESC LIMIT %s)", (KEEP_PUBLICATIONS,))
            return int(row["id"])
        return await self._run(work)

    async def latest_id(self) -> int:
        def work(conn: Conn) -> int:
            row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM site_publications").fetchone()
            return int(row["id"]) if row else 0
        return await self._run(work)

    async def get(self, publication_id: int) -> Publication | None:
        def work(conn: Conn) -> Publication | None:
            row = conn.execute("SELECT * FROM site_publications WHERE id = %s", (publication_id,)).fetchone()
            return _publication(row) if row else None
        return await self._run(work)

    async def history(self) -> list[dict[str, Any]]:
        """Публикации, новые сверху: номер, время, размер, страница целиком или только данные."""
        def work(conn: Conn) -> list[dict[str, Any]]:
            return [dict(row) for row in conn.execute(
                "SELECT id, created_at, html IS NOT NULL AS whole, length(coalesce(html, data_json)) AS size "
                "FROM site_publications ORDER BY id DESC")]
        return await self._run(work)


# ---------- живая страница ----------
@dataclass(frozen=True)
class LivePage:
    text: str
    meta: seo.PageMeta
    modified: float               # время последнего изменения: файла из git или публикации — для sitemap
    source: str                   # git — только файл; published — публикация целиком; merged — файл и правки админки
    key: str                      # меняется вместе со страницей — для кешей

    @property
    def data(self) -> dict[str, Any]:
        return data_block(self.text) or {}


@dataclass(frozen=True)
class _GitPage:
    text: str
    sha: str
    mtime: float
    stamp: tuple[int, int]


class SiteContent:
    """Страница сайта для посетителей, бота и поисковиков: файл из git и последняя публикация из админки."""

    def __init__(self, index_file: Path, store: SiteStore | None, check_every: float = CHECK_EVERY) -> None:
        self._file, self._store, self._every = index_file, store, check_every
        self._git_page: _GitPage | None = None
        self._latest = 0
        self._publication: Publication | None = None
        self._checked = float("-inf")
        self._now = True                 # следующий запрос ждёт ответа базы: страницы ещё нет или только что публиковали
        self._task: asyncio.Task[None] | None = None   # проверка базы в фоне
        self._offline = False            # база не ответила; сайт показывает последнее прочитанное
        self._page: LivePage | None = None

    @property
    def can_publish(self) -> bool:
        return self._store is not None

    def invalidate(self) -> None:
        """Следующий запрос спросит базу сразу и дождётся ответа, а не через CHECK_EVERY."""
        self._now = True

    async def checked(self) -> None:
        """Дождаться проверки базы, начатой в фоне (для проверок)."""
        if self._task is not None:
            await self._task

    def _git(self) -> _GitPage | None:
        try:
            stat = self._file.stat()
        except OSError:
            return None
        stamp = (stat.st_mtime_ns, stat.st_size)
        if self._git_page is None or self._git_page.stamp != stamp:
            raw = self._file.read_bytes()
            self._git_page = _GitPage(text=raw.decode("utf-8", errors="replace"), sha=hashlib.sha256(raw).hexdigest(),
                                      mtime=stat.st_mtime, stamp=stamp)
        return self._git_page

    async def page(self) -> LivePage | None:
        git = self._git()
        if self._store is not None:
            if self._now or self._page is None:
                self._now = False
                await self._check(self._store)
            elif time.monotonic() - self._checked >= self._every and (self._task is None or self._task.done()):
                # Страница уже есть — посетитель её получает сразу, а базу спрашиваем в фоне: пока база
                # не отвечает (машину с ней перезагружают), сайт не ждёт её ни секунды.
                self._task = asyncio.create_task(self._check(self._store))
        key = f"{git.sha if git else '-'}:{self._latest}"
        if self._page is None or self._page.key != key:
            self._page = _compose(git, self._publication, key)
        return self._page

    async def _check(self, store: SiteStore) -> None:
        try:
            latest = await store.latest_id()
            if latest != self._latest:
                self._publication = await store.get(latest) if latest else None
                self._latest = latest
            if self._offline:
                log.info("тексты сайта: связь с базой восстановлена")
            self._offline = False
        except (psycopg.Error, OSError) as exc:
            if not self._offline:
                log.warning("тексты сайта: база не отвечает (%s), показываем последнюю прочитанную публикацию",
                            type(exc).__name__)
            self._offline = True
        except Exception:                # ошибка в коде — в журнал; сайт работает с прочитанным раньше
            log.exception("тексты сайта: проверка базы не удалась")
        finally:
            self._checked = time.monotonic()

    async def publish(self, html: str) -> tuple[int, bool]:
        """Запоминает публикацию из админки. Возвращает (номер, сохранена ли страница целиком).

        ValueError — в странице нет данных сайта.
        """
        if self._store is None:
            raise RuntimeError("публикация без базы невозможна")
        data = data_block(html)
        if data is None:
            raise ValueError("в странице нет данных сайта (блок site-data)")
        git = self._git()
        same_code = git is not None and code_of(html) == code_of(git.text)
        number = await self._store.add(data=data, base=(data_block(git.text) or {}) if git else {},
                                       html=html if same_code else None, template_sha=git.sha if git else "")
        self.invalidate()
        return number, same_code


def _timestamp(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def _compose(git: _GitPage | None, publication: Publication | None, key: str) -> LivePage | None:
    if publication is None:
        if git is None:
            return None
        text, source, modified = git.text, "git", git.mtime
    elif publication.html is not None and (git is None or publication.template_sha == git.sha):
        text, source = publication.html, "published"
        modified = max(git.mtime if git else 0.0, _timestamp(publication.created_at))
    elif git is not None:
        merged = merge(publication.base, publication.data, data_block(git.text) or {})
        text, source = inject(git.text, merged), "merged"
        modified = max(git.mtime, _timestamp(publication.created_at))
    else:
        return None
    return LivePage(text=text, meta=seo.read_meta(text), modified=modified, source=source, key=key)
