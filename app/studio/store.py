"""Хранилище студии: проекты сайтов, сообщения совета агентов, задачи между агентами, уроки и заявки с сайтов."""
from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from typing import Any

from app.db import SQLiteStore, utc_now

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    token       TEXT NOT NULL UNIQUE,              -- открывает клиенту страницу хода работ, угадать нельзя
    slug        TEXT NOT NULL UNIQUE,              -- адрес сайта: /sites/<slug>/
    chat_id     INTEGER NOT NULL DEFAULT 0,        -- чат клиента в Telegram (0 — создан из веб-панели)
    status      TEXT NOT NULL DEFAULT 'queued',    -- queued → running → done | failed
    brief_json  TEXT NOT NULL,
    spec_json   TEXT NOT NULL DEFAULT '{}',
    version     INTEGER NOT NULL DEFAULT 0,        -- номер опубликованной версии сайта
    pending     TEXT NOT NULL DEFAULT '',          -- правки клиента, которые ждут очереди
    review      REAL NOT NULL DEFAULT 0,           -- средняя оценка ревьюеров, 1–10
    rating      INTEGER NOT NULL DEFAULT 0,        -- оценка клиента, 1–5 (0 — ещё не ставил)
    calls       INTEGER NOT NULL DEFAULT 0,
    cost_usd    REAL NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS projects_status ON projects (status, id);
CREATE INDEX IF NOT EXISTS projects_chat ON projects (chat_id, created_at);
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    channel     TEXT NOT NULL,                     -- брифинг, исследование, спор, задачи, производство, ревью, ретро, общий
    agent       TEXT NOT NULL,
    kind        TEXT NOT NULL,                     -- say, propose, agree, object, vote, decision, task, done, review, lesson, system
    content     TEXT NOT NULL,
    data_json   TEXT NOT NULL DEFAULT '{}',
    reply_to    TEXT NOT NULL DEFAULT '',          -- кому отвечает (id агента)
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_project ON messages (project_id, id);
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_agent  TEXT NOT NULL,
    to_agent    TEXT NOT NULL,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',      -- open → done
    result      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    done_at     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks (project_id, id);
CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent       TEXT NOT NULL,                     -- id агента или 'all' — урок для всей команды
    text        TEXT NOT NULL,
    score       REAL NOT NULL DEFAULT 1,           -- растёт от хороших оценок, падает от плохих
    uses        INTEGER NOT NULL DEFAULT 0,
    project_id  INTEGER,                           -- из какого проекта родился урок
    active      INTEGER NOT NULL DEFAULT 1,        -- 0 — урок не подтвердился и больше не подсказывается
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS lessons_agent ON lessons (agent, active, score);
CREATE TABLE IF NOT EXISTS lesson_uses (
    lesson_id   INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL,
    PRIMARY KEY (lesson_id, project_id)
);
CREATE TABLE IF NOT EXISTS agent_stats (
    agent       TEXT PRIMARY KEY,
    projects    INTEGER NOT NULL DEFAULT 0,
    score_sum   REAL NOT NULL DEFAULT 0,           -- оценки клиентов проектов, где агент работал
    score_n     INTEGER NOT NULL DEFAULT 0,
    lessons     INTEGER NOT NULL DEFAULT 0,
    messages    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL,
    message     TEXT NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0,
    attempts    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Project:
    id: int
    token: str
    slug: str
    chat_id: int
    status: str
    brief: dict[str, Any]
    spec: dict[str, Any]
    version: int
    pending: str
    review: float
    rating: int
    calls: int
    cost_usd: float
    error: str
    created_at: str
    updated_at: str

    @property
    def name(self) -> str:
        return str(self.brief.get("business") or self.slug)


def _project(row: sqlite3.Row) -> Project:
    return Project(id=row["id"], token=row["token"], slug=row["slug"], chat_id=row["chat_id"], status=row["status"],
                   brief=json.loads(row["brief_json"]), spec=json.loads(row["spec_json"]), version=row["version"],
                   pending=row["pending"], review=row["review"], rating=row["rating"], calls=row["calls"],
                   cost_usd=row["cost_usd"], error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"])


def _message(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "project_id": row["project_id"], "channel": row["channel"], "agent": row["agent"],
            "kind": row["kind"], "content": row["content"], "data": json.loads(row["data_json"]),
            "reply_to": row["reply_to"], "created_at": row["created_at"]}


class StudioStore(SQLiteStore):
    SCHEMA = SCHEMA

    # ---------- проекты ----------
    async def create_project(self, brief: dict[str, Any], slug_base: str, chat_id: int = 0) -> Project:
        def work(conn: sqlite3.Connection) -> Project:
            slug, n = slug_base, 1
            while conn.execute("SELECT 1 FROM projects WHERE slug = ?", (slug,)).fetchone():
                n += 1
                slug = f"{slug_base}-{n}"
            now = utc_now()
            cur = conn.execute("INSERT INTO projects (token, slug, chat_id, brief_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                               (secrets.token_urlsafe(16), slug, chat_id, json.dumps(brief, ensure_ascii=False), now, now))
            return _project(conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone())
        return await self._run(work)

    async def get(self, project_id: int) -> Project | None:
        def work(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
            return _project(row) if row else None
        return await self._run(work)

    async def by_token(self, token: str) -> Project | None:
        def work(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE token = ?", (token,)).fetchone()
            return _project(row) if row else None
        return await self._run(work)

    async def by_slug(self, slug: str) -> Project | None:
        def work(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
            return _project(row) if row else None
        return await self._run(work)

    async def list_projects(self, limit: int = 50, chat_id: int | None = None) -> list[Project]:
        def work(conn: sqlite3.Connection) -> list[Project]:
            if chat_id is None:
                rows = conn.execute("SELECT * FROM projects ORDER BY id DESC LIMIT ?", (limit,))
            else:
                rows = conn.execute("SELECT * FROM projects WHERE chat_id = ? ORDER BY id DESC LIMIT ?", (chat_id, limit))
            return [_project(r) for r in rows]
        return await self._run(work)

    async def count_since(self, chat_id: int, since: str) -> int:
        def work(conn: sqlite3.Connection) -> int:
            return int(conn.execute("SELECT COUNT(*) FROM projects WHERE chat_id = ? AND created_at >= ?", (chat_id, since)).fetchone()[0])
        return await self._run(work)

    async def update(self, project_id: int, **fields: Any) -> None:
        allowed = {"status", "spec", "version", "pending", "review", "rating", "calls", "cost_usd", "error"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            if key == "spec":
                key, value = "spec_json", json.dumps(value, ensure_ascii=False)
            sets.append(f"{key} = ?")
            values.append(value)
        sets.append("updated_at = ?")
        values.append(utc_now())

        def work(conn: sqlite3.Connection) -> None:
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?", (*values, project_id))
        await self._run(work)

    async def add_cost(self, project_id: int, calls: int, cost: float) -> None:
        await self._run(lambda conn: conn.execute(
            "UPDATE projects SET calls = calls + ?, cost_usd = cost_usd + ? WHERE id = ?", (calls, cost, project_id)))

    async def next_job(self) -> Project | None:
        """Берёт следующий проект: новый (queued) или с правками клиента (done + pending)."""
        def work(conn: sqlite3.Connection) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE status = 'queued' OR (status = 'done' AND pending != '') "
                               "ORDER BY updated_at, id LIMIT 1").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE projects SET status = 'running', updated_at = ? WHERE id = ?", (utc_now(), row["id"]))
            return _project(row)
        return await self._run(work)

    async def requeue_running(self) -> int:
        """После перезапуска сервера прерванные проекты возвращаются в очередь."""
        def work(conn: sqlite3.Connection) -> int:
            return conn.execute("UPDATE projects SET status = CASE WHEN version > 0 THEN 'done' ELSE 'queued' END "
                                "WHERE status = 'running'").rowcount
        return await self._run(work)

    # ---------- совет ----------
    async def post(self, project_id: int | None, channel: str, agent: str, kind: str, content: str,
                   data: dict[str, Any] | None = None, reply_to: str = "") -> int:
        def work(conn: sqlite3.Connection) -> int:
            cur = conn.execute("INSERT INTO messages (project_id, channel, agent, kind, content, data_json, reply_to, created_at) "
                               "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                               (project_id, channel, agent, kind, content[:4000], json.dumps(data or {}, ensure_ascii=False),
                                reply_to, utc_now()))
            conn.execute("INSERT INTO agent_stats (agent, messages) VALUES (?, 1) "
                         "ON CONFLICT (agent) DO UPDATE SET messages = messages + 1", (agent,))
            return int(cur.lastrowid)
        return await self._run(work)

    async def messages(self, project_id: int | None = None, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if project_id is None:
                rows = conn.execute("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (after, limit))
            else:
                rows = conn.execute("SELECT * FROM messages WHERE project_id = ? AND id > ? ORDER BY id LIMIT ?",
                                    (project_id, after, limit))
            return [_message(r) for r in rows]
        return await self._run(work)

    async def recent(self, project_id: int, limit: int = 14) -> list[dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM messages WHERE project_id = ? ORDER BY id DESC LIMIT ?", (project_id, limit)).fetchall()
            return [_message(r) for r in reversed(rows)]
        return await self._run(work)

    async def add_task(self, project_id: int, from_agent: str, to_agent: str, title: str) -> int:
        def work(conn: sqlite3.Connection) -> int:
            cur = conn.execute("INSERT INTO tasks (project_id, from_agent, to_agent, title, created_at) VALUES (?, ?, ?, ?, ?)",
                               (project_id, from_agent, to_agent, title[:300], utc_now()))
            return int(cur.lastrowid)
        return await self._run(work)

    async def finish_task(self, task_id: int, result: str) -> None:
        await self._run(lambda conn: conn.execute("UPDATE tasks SET status = 'done', result = ?, done_at = ? WHERE id = ?",
                                                  (result[:2000], utc_now(), task_id)))

    async def tasks(self, project_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            if project_id is None:
                rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
            else:
                rows = conn.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY id", (project_id,))
            return [dict(r) for r in rows]
        return await self._run(work)

    # ---------- уроки и рост агентов ----------
    async def lessons_for(self, agent: str, limit: int = 5) -> list[dict[str, Any]]:
        """Лучшие активные уроки агента и общие уроки команды."""
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM lessons WHERE active = 1 AND agent IN (?, 'all') "
                                "ORDER BY score DESC, id DESC LIMIT ?", (agent, limit))
            return [dict(r) for r in rows]
        return await self._run(work)

    async def use_lessons(self, project_id: int, lesson_ids: list[int]) -> None:
        def work(conn: sqlite3.Connection) -> None:
            for lesson_id in lesson_ids:
                if conn.execute("INSERT OR IGNORE INTO lesson_uses (lesson_id, project_id) VALUES (?, ?)",
                                (lesson_id, project_id)).rowcount:
                    conn.execute("UPDATE lessons SET uses = uses + 1 WHERE id = ?", (lesson_id,))
        await self._run(work)

    async def add_lesson(self, agent: str, text: str, project_id: int | None, score: float = 1.0) -> int | None:
        """Записывает урок. Дословный повтор не плодит дубль, а укрепляет уже известный урок."""
        def work(conn: sqlite3.Connection) -> int | None:
            text_clean = " ".join(text.split())[:300]
            if not text_clean:
                return None
            same = conn.execute("SELECT id FROM lessons WHERE agent = ? AND lower(text) = lower(?)", (agent, text_clean)).fetchone()
            if same:
                conn.execute("UPDATE lessons SET score = score + 0.5, active = 1 WHERE id = ?", (same["id"],))
                return int(same["id"])
            cur = conn.execute("INSERT INTO lessons (agent, text, score, project_id, created_at) VALUES (?, ?, ?, ?, ?)",
                               (agent, text_clean, score, project_id, utc_now()))
            if agent != "all":
                conn.execute("INSERT INTO agent_stats (agent, lessons) VALUES (?, 1) "
                             "ON CONFLICT (agent) DO UPDATE SET lessons = lessons + 1", (agent,))
            return int(cur.lastrowid)
        return await self._run(work)

    async def rate(self, project_id: int, rating: int, agents: list[str]) -> list[dict[str, Any]]:
        """Оценка клиента меняет вес уроков, которые применялись или родились в проекте.

        5 — уроки крепнут, 1 — слабеют; урок, чей вес упал до нуля, выключается. Возвращает изменённые уроки.
        """
        delta = (rating - 3) * 0.5

        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            old = conn.execute("SELECT rating FROM projects WHERE id = ?", (project_id,)).fetchone()
            first_time = old is not None and not old["rating"]
            conn.execute("UPDATE projects SET rating = ?, updated_at = ? WHERE id = ?", (rating, utc_now(), project_id))
            if not first_time:                       # переоценка не начисляет баллы второй раз
                return []
            ids = {r["lesson_id"] for r in conn.execute("SELECT lesson_id FROM lesson_uses WHERE project_id = ?", (project_id,))}
            ids |= {r["id"] for r in conn.execute("SELECT id FROM lessons WHERE project_id = ?", (project_id,))}
            for lesson_id in ids:
                conn.execute("UPDATE lessons SET score = score + ? WHERE id = ?", (delta, lesson_id))
            conn.execute("UPDATE lessons SET active = 0 WHERE score <= 0")
            for agent in agents:
                conn.execute("INSERT INTO agent_stats (agent, projects, score_sum, score_n) VALUES (?, 0, ?, 1) "
                             "ON CONFLICT (agent) DO UPDATE SET score_sum = score_sum + ?, score_n = score_n + 1",
                             (agent, rating, rating))
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            return [dict(r) for r in conn.execute(f"SELECT * FROM lessons WHERE id IN ({marks})", tuple(ids))]
        return await self._run(work)

    async def count_project(self, agents: list[str]) -> None:
        def work(conn: sqlite3.Connection) -> None:
            for agent in agents:
                conn.execute("INSERT INTO agent_stats (agent, projects) VALUES (?, 1) "
                             "ON CONFLICT (agent) DO UPDATE SET projects = projects + 1", (agent,))
        await self._run(work)

    async def agent_stats(self) -> dict[str, dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            return {r["agent"]: dict(r) for r in conn.execute("SELECT * FROM agent_stats")}
        return await self._run(work)

    async def all_lessons(self, limit: int = 200) -> list[dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            return [dict(r) for r in conn.execute("SELECT * FROM lessons ORDER BY active DESC, score DESC, id DESC LIMIT ?", (limit,))]
        return await self._run(work)

    # ---------- заявки с сайтов клиентов ----------
    async def add_lead(self, project_id: int, name: str, phone: str, message: str) -> int:
        def work(conn: sqlite3.Connection) -> int:
            cur = conn.execute("INSERT INTO leads (project_id, name, phone, message, created_at) VALUES (?, ?, ?, ?, ?)",
                               (project_id, name, phone, message, utc_now()))
            return int(cur.lastrowid)
        return await self._run(work)

    async def pending_leads(self, max_attempts: int = 30) -> list[dict[str, Any]]:
        def work(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT l.*, p.chat_id, p.slug, p.brief_json FROM leads l JOIN projects p ON p.id = l.project_id "
                                "WHERE l.delivered = 0 AND l.attempts < ? AND p.chat_id != 0 ORDER BY l.id LIMIT 50", (max_attempts,))
            return [dict(r) for r in rows]
        return await self._run(work)

    async def mark_lead(self, lead_id: int, delivered: bool) -> None:
        await self._run(lambda conn: conn.execute("UPDATE leads SET delivered = ?, attempts = attempts + 1 WHERE id = ?",
                                                  (int(delivered), lead_id)))
