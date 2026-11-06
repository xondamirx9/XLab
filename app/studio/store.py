"""Хранилище студии: проекты сайтов, сообщения совета агентов, задачи между агентами, уроки и заявки с сайтов."""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from app.db import Conn, PgStore, utc_now

# Таблицы студии — в app/db.py (схема общая для всех хранилищ).


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


def _project(row: dict[str, Any]) -> Project:
    return Project(id=row["id"], token=row["token"], slug=row["slug"], chat_id=row["chat_id"], status=row["status"],
                   brief=json.loads(row["brief_json"]), spec=json.loads(row["spec_json"]), version=row["version"],
                   pending=row["pending"], review=row["review"], rating=row["rating"], calls=row["calls"],
                   cost_usd=row["cost_usd"], error=row["error"], created_at=row["created_at"], updated_at=row["updated_at"])


def _message(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "project_id": row["project_id"], "channel": row["channel"], "agent": row["agent"],
            "kind": row["kind"], "content": row["content"], "data": json.loads(row["data_json"]),
            "reply_to": row["reply_to"], "created_at": row["created_at"]}


def _insert_project(conn: Conn, brief: dict[str, Any], slug_base: str, chat_id: int) -> Project:
    slug, n = slug_base, 1
    while conn.execute("SELECT 1 FROM projects WHERE slug = %s", (slug,)).fetchone():
        n += 1
        slug = f"{slug_base}-{n}"
    now = utc_now()
    row = conn.execute("INSERT INTO projects (token, slug, chat_id, brief_json, created_at, updated_at) "
                       "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                       (secrets.token_urlsafe(16), slug, chat_id, json.dumps(brief, ensure_ascii=False), now, now)).fetchone()
    assert row is not None
    return _project(row)


def _count_lesson(conn: Conn, agent: str) -> None:
    """Счётчик уроков агента: от него зависит его уровень на странице совета."""
    if agent != "all":
        conn.execute("INSERT INTO agent_stats (agent, lessons) VALUES (%s, 1) "
                     "ON CONFLICT (agent) DO UPDATE SET lessons = agent_stats.lessons + 1", (agent,))


class StudioStore(PgStore):
    # ---------- проекты ----------
    async def create_project(self, brief: dict[str, Any], slug_base: str, chat_id: int = 0) -> Project:
        return await self._run(lambda conn: _insert_project(conn, brief, slug_base, chat_id))

    async def create_within_limit(self, brief: dict[str, Any], slug_base: str, chat_id: int, since: str, limit: int) -> Project | None:
        """Создаёт проект, только если у чата меньше limit проектов с момента since. None — лимит исчерпан.

        Проверка и запись идут в одной транзакции: два быстрых нажатия «Запустить» лимит не обойдут.
        """
        def work(conn: Conn) -> Project | None:
            # Замок на чат до конца транзакции: второе нажатие ждёт, пока первое запишет проект, и видит его в счёте.
            conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (f"studio-limit:{chat_id}",))
            used = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE chat_id = %s AND created_at >= %s",
                                (chat_id, since)).fetchone()
            return _insert_project(conn, brief, slug_base, chat_id) if used is not None and used["n"] < limit else None
        return await self._run(work)

    async def get(self, project_id: int) -> Project | None:
        def work(conn: Conn) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
            return _project(row) if row else None
        return await self._run(work)

    async def by_token(self, token: str) -> Project | None:
        def work(conn: Conn) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE token = %s", (token,)).fetchone()
            return _project(row) if row else None
        return await self._run(work)

    async def by_slug(self, slug: str) -> Project | None:
        def work(conn: Conn) -> Project | None:
            row = conn.execute("SELECT * FROM projects WHERE slug = %s", (slug,)).fetchone()
            return _project(row) if row else None
        return await self._run(work)

    async def list_projects(self, limit: int = 50, chat_id: int | None = None) -> list[Project]:
        def work(conn: Conn) -> list[Project]:
            if chat_id is None:
                rows = conn.execute("SELECT * FROM projects ORDER BY id DESC LIMIT %s", (limit,))
            else:
                rows = conn.execute("SELECT * FROM projects WHERE chat_id = %s ORDER BY id DESC LIMIT %s", (chat_id, limit))
            return [_project(r) for r in rows]
        return await self._run(work)

    async def count_since(self, chat_id: int, since: str) -> int:
        def work(conn: Conn) -> int:
            row = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE chat_id = %s AND created_at >= %s", (chat_id, since)).fetchone()
            return int(row["n"]) if row else 0
        return await self._run(work)

    async def update(self, project_id: int, **fields: Any) -> None:
        allowed = {"status", "spec", "version", "pending", "review", "rating", "calls", "cost_usd", "error"}
        sets, values = [], []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(key)
            if key == "spec":
                key, value = "spec_json", json.dumps(value, ensure_ascii=False)
            sets.append(f"{key} = %s")
            values.append(value)
        sets.append("updated_at = %s")
        values.append(utc_now())

        def work(conn: Conn) -> None:
            conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = %s", (*values, project_id))
        await self._run(work)

    async def add_cost(self, project_id: int, calls: int, cost: float) -> None:
        await self._run(lambda conn: conn.execute(
            "UPDATE projects SET calls = calls + %s, cost_usd = cost_usd + %s WHERE id = %s", (calls, cost, project_id)))

    async def next_job(self) -> Project | None:
        """Берёт следующий проект: новый (queued) или с правками клиента (done + pending)."""
        def work(conn: Conn) -> Project | None:
            # SKIP LOCKED: проект, который прямо сейчас берёт другой сборщик, пропускается, а не берётся дважды
            row = conn.execute("SELECT * FROM projects WHERE status = 'queued' OR (status = 'done' AND pending != '') "
                               "ORDER BY updated_at, id LIMIT 1 FOR UPDATE SKIP LOCKED").fetchone()
            if row is None:
                return None
            conn.execute("UPDATE projects SET status = 'running', updated_at = %s WHERE id = %s", (utc_now(), row["id"]))
            return _project(row)
        return await self._run(work)

    async def requeue_running(self) -> int:
        """После перезапуска сервера прерванные проекты возвращаются в очередь."""
        def work(conn: Conn) -> int:
            return conn.execute("UPDATE projects SET status = CASE WHEN version > 0 THEN 'done' ELSE 'queued' END "
                                "WHERE status = 'running'").rowcount
        return await self._run(work)

    # ---------- совет ----------
    async def post(self, project_id: int | None, channel: str, agent: str, kind: str, content: str,
                   data: dict[str, Any] | None = None, reply_to: str = "") -> int:
        def work(conn: Conn) -> int:
            row = conn.execute("INSERT INTO messages (project_id, channel, agent, kind, content, data_json, reply_to, created_at) "
                               "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                               (project_id, channel, agent, kind, content[:4000], json.dumps(data or {}, ensure_ascii=False),
                                reply_to, utc_now())).fetchone()
            conn.execute("INSERT INTO agent_stats (agent, messages) VALUES (%s, 1) "
                         "ON CONFLICT (agent) DO UPDATE SET messages = agent_stats.messages + 1", (agent,))
            assert row is not None
            return int(row["id"])
        return await self._run(work)

    async def messages(self, project_id: int | None = None, after: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        def work(conn: Conn) -> list[dict[str, Any]]:
            if project_id is None:
                rows = conn.execute("SELECT * FROM messages WHERE id > %s ORDER BY id LIMIT %s", (after, limit))
            else:
                rows = conn.execute("SELECT * FROM messages WHERE project_id = %s AND id > %s ORDER BY id LIMIT %s",
                                    (project_id, after, limit))
            return [_message(r) for r in rows]
        return await self._run(work)

    async def recent(self, project_id: int, limit: int = 14) -> list[dict[str, Any]]:
        def work(conn: Conn) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM messages WHERE project_id = %s ORDER BY id DESC LIMIT %s", (project_id, limit)).fetchall()
            return [_message(r) for r in reversed(rows)]
        return await self._run(work)

    async def add_task(self, project_id: int, from_agent: str, to_agent: str, title: str) -> int:
        def work(conn: Conn) -> int:
            row = conn.execute("INSERT INTO tasks (project_id, from_agent, to_agent, title, created_at) "
                               "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                               (project_id, from_agent, to_agent, title[:300], utc_now())).fetchone()
            assert row is not None
            return int(row["id"])
        return await self._run(work)

    async def finish_task(self, task_id: int, result: str) -> None:
        await self._run(lambda conn: conn.execute("UPDATE tasks SET status = 'done', result = %s, done_at = %s WHERE id = %s",
                                                  (result[:2000], utc_now(), task_id)))

    async def tasks(self, project_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        def work(conn: Conn) -> list[dict[str, Any]]:
            if project_id is None:
                rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT %s", (limit,))
            else:
                rows = conn.execute("SELECT * FROM tasks WHERE project_id = %s ORDER BY id", (project_id,))
            return [dict(r) for r in rows]
        return await self._run(work)

    # ---------- уроки и рост агентов ----------
    async def lessons_for(self, agent: str, limit: int = 5) -> list[dict[str, Any]]:
        """Лучшие активные уроки агента и общие уроки команды."""
        def work(conn: Conn) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT * FROM lessons WHERE active = 1 AND approved = 1 AND agent IN (%s, 'all') "
                                "ORDER BY score DESC, id DESC LIMIT %s", (agent, limit))
            return [dict(r) for r in rows]
        return await self._run(work)

    async def use_lessons(self, project_id: int, lesson_ids: list[int]) -> None:
        def work(conn: Conn) -> None:
            for lesson_id in lesson_ids:
                if conn.execute("INSERT INTO lesson_uses (lesson_id, project_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                (lesson_id, project_id)).rowcount:
                    conn.execute("UPDATE lessons SET uses = uses + 1 WHERE id = %s", (lesson_id,))
        await self._run(work)

    async def add_lesson(self, agent: str, text: str, project_id: int | None, score: float = 1.0,
                         approved: bool = True) -> int | None:
        """Записывает урок. Дословный повтор не плодит дубль, а укрепляет уже известный урок.

        approved=False — урок родился из проекта клиента: агентам он не подсказывается, пока его не одобрит
        администратор (/lessons в боте). Такой повтор не трогает существующие уроки, иначе клиент мог бы
        оживить выключенный урок.
        """
        def work(conn: Conn) -> int | None:
            text_clean = " ".join(text.split())[:300]
            if not text_clean:
                return None
            same = conn.execute("SELECT id FROM lessons WHERE agent = %s AND lower(text) = lower(%s)", (agent, text_clean)).fetchone()
            if same:
                if approved:
                    conn.execute("UPDATE lessons SET score = score + 0.5, active = 1 WHERE id = %s", (same["id"],))
                return int(same["id"])
            row = conn.execute("INSERT INTO lessons (agent, text, score, project_id, approved, created_at) "
                               "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                               (agent, text_clean, score, project_id, int(approved), utc_now())).fetchone()
            assert row is not None
            if approved:
                _count_lesson(conn, agent)
            return int(row["id"])
        return await self._run(work)

    async def unapproved_lessons(self, limit: int = 10) -> list[dict[str, Any]]:
        def work(conn: Conn) -> list[dict[str, Any]]:
            return [dict(r) for r in conn.execute("SELECT * FROM lessons WHERE approved = 0 ORDER BY id LIMIT %s", (limit,))]
        return await self._run(work)

    async def review_lesson(self, lesson_id: int, approve: bool) -> bool:
        """Решение администратора по уроку из проекта клиента: одобрить или удалить. False — такого урока на проверке нет."""
        def work(conn: Conn) -> bool:
            row = conn.execute("SELECT agent FROM lessons WHERE id = %s AND approved = 0 FOR UPDATE", (lesson_id,)).fetchone()
            if row is None:
                return False
            if approve:
                conn.execute("UPDATE lessons SET approved = 1 WHERE id = %s", (lesson_id,))
                _count_lesson(conn, row["agent"])
            else:
                conn.execute("DELETE FROM lessons WHERE id = %s", (lesson_id,))
            return True
        return await self._run(work)

    async def rate(self, project_id: int, rating: int, agents: list[str]) -> list[dict[str, Any]]:
        """Оценка клиента меняет вес уроков, которые применялись или родились в проекте.

        5 — уроки крепнут, 1 — слабеют; урок, чей вес упал до нуля, выключается. Возвращает изменённые уроки.
        """
        delta = (rating - 3) * 0.5

        def work(conn: Conn) -> list[dict[str, Any]]:
            # FOR UPDATE: две оценки разом не начислят баллы дважды — вторая дождётся первой и увидит её
            old = conn.execute("SELECT rating FROM projects WHERE id = %s FOR UPDATE", (project_id,)).fetchone()
            first_time = old is not None and not old["rating"]
            conn.execute("UPDATE projects SET rating = %s, updated_at = %s WHERE id = %s", (rating, utc_now(), project_id))
            if not first_time:                       # переоценка не начисляет баллы второй раз
                return []
            ids = {r["lesson_id"] for r in conn.execute("SELECT lesson_id FROM lesson_uses WHERE project_id = %s", (project_id,))}
            ids |= {r["id"] for r in conn.execute("SELECT id FROM lessons WHERE project_id = %s", (project_id,))}
            for lesson_id in ids:
                conn.execute("UPDATE lessons SET score = score + %s WHERE id = %s", (delta, lesson_id))
            conn.execute("UPDATE lessons SET active = 0 WHERE score <= 0")
            for agent in agents:
                conn.execute("INSERT INTO agent_stats (agent, projects, score_sum, score_n) VALUES (%s, 0, %s, 1) "
                             "ON CONFLICT (agent) DO UPDATE SET score_sum = agent_stats.score_sum + %s, score_n = agent_stats.score_n + 1",
                             (agent, rating, rating))
            if not ids:
                return []
            return [dict(r) for r in conn.execute("SELECT * FROM lessons WHERE id = ANY(%s) ORDER BY id", (sorted(ids),))]
        return await self._run(work)

    async def count_project(self, agents: list[str]) -> None:
        def work(conn: Conn) -> None:
            for agent in agents:
                conn.execute("INSERT INTO agent_stats (agent, projects) VALUES (%s, 1) "
                             "ON CONFLICT (agent) DO UPDATE SET projects = agent_stats.projects + 1", (agent,))
        await self._run(work)

    async def agent_stats(self) -> dict[str, dict[str, Any]]:
        def work(conn: Conn) -> dict[str, dict[str, Any]]:
            return {r["agent"]: dict(r) for r in conn.execute("SELECT * FROM agent_stats")}
        return await self._run(work)

    async def all_lessons(self, limit: int = 200) -> list[dict[str, Any]]:
        def work(conn: Conn) -> list[dict[str, Any]]:
            return [dict(r) for r in conn.execute("SELECT * FROM lessons ORDER BY active DESC, score DESC, id DESC LIMIT %s", (limit,))]
        return await self._run(work)

    # ---------- сайты клиентов ----------
    async def save_site(self, project_id: int, version: int, html: str) -> None:
        """Версия сайта клиента. Та же версия ещё раз — заменяется."""
        await self._run(lambda conn: conn.execute(
            "INSERT INTO studio_sites (project_id, version, html, created_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (project_id, version) DO UPDATE SET html = EXCLUDED.html, created_at = EXCLUDED.created_at",
            (project_id, version, html, utc_now())))

    async def site_html(self, slug: str, version: int | None = None) -> str | None:
        """Сайт клиента по адресу: версия или последняя опубликованная. None — нет такого."""
        def work(conn: Conn) -> str | None:
            row = conn.execute(
                "SELECT s.html FROM studio_sites s JOIN projects p ON p.id = s.project_id "
                "WHERE p.slug = %s AND s.version = COALESCE(%s, p.version)", (slug, version)).fetchone()
            return row["html"] if row else None
        return await self._run(work)

    # ---------- заявки с сайтов клиентов ----------
    async def add_lead(self, project_id: int, name: str, phone: str, message: str) -> int:
        def work(conn: Conn) -> int:
            row = conn.execute("INSERT INTO leads (project_id, name, phone, message, created_at) "
                               "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                               (project_id, name, phone, message, utc_now())).fetchone()
            assert row is not None
            return int(row["id"])
        return await self._run(work)

    async def pending_leads(self, max_attempts: int = 30) -> list[dict[str, Any]]:
        def work(conn: Conn) -> list[dict[str, Any]]:
            rows = conn.execute("SELECT l.*, p.chat_id, p.slug, p.brief_json FROM leads l JOIN projects p ON p.id = l.project_id "
                                "WHERE l.delivered = 0 AND l.attempts < %s AND p.chat_id != 0 ORDER BY l.id LIMIT 50", (max_attempts,))
            return [dict(r) for r in rows]
        return await self._run(work)

    async def mark_lead(self, lead_id: int, delivered: bool) -> None:
        await self._run(lambda conn: conn.execute("UPDATE leads SET delivered = %s, attempts = attempts + 1 WHERE id = %s",
                                                  (int(delivered), lead_id)))
