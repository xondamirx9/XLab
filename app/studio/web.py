"""HTTP-часть студии: сайты клиентов, заявки с них, страница совета агентов и служебное API."""
from __future__ import annotations

import hmac
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import unquote

from aiohttp import web

from app.news import NewsStore
from app.studio.agents import AGENTS
from app.studio.pipeline import Studio
from app.studio.renderer import site_csp
from app.studio.spec import slugify
from app.studio.store import Project, StudioStore

PAGE = Path(__file__).resolve().parent / "assets" / "council.html"
SLUG = re.compile(r"^[a-z0-9-]{1,48}$")
TOKEN = re.compile(r"^[A-Za-z0-9_-]{10,40}$")


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


async def _read_limited(request: web.Request, limit: int) -> bytes | None:
    """Тело запроса целиком, но не больше limit байт. None — тело слишком большое."""
    chunks, size = [], 0
    async for chunk in request.content.iter_chunked(16 * 1024):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def agents_payload(stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for a in AGENTS:
        s = stats.get(a.id, {})
        lessons = int(s.get("lessons", 0))
        out.append({"id": a.id, "name": a.name, "emoji": a.emoji, "role": a.role, "level": 1 + lessons // 2,
                    "lessons": lessons, "messages": int(s.get("messages", 0)), "projects": int(s.get("projects", 0)),
                    "rating": round(s["score_sum"] / s["score_n"], 2) if s.get("score_n") else None})
    return out


def project_payload(p: Project, public: bool = False) -> dict[str, Any]:
    data = {"id": p.id, "slug": p.slug, "name": p.name, "status": p.status, "version": p.version, "review": p.review,
            "rating": p.rating, "url": f"/sites/{p.slug}/" if p.version else "", "created_at": p.created_at,
            "style": p.brief.get("style", ""), "niche": p.brief.get("niche", "")}
    if not public:
        data.update({"token": p.token, "calls": p.calls, "cost_usd": round(p.cost_usd, 3), "error": p.error, "chat_id": p.chat_id})
    return data


def setup_studio(app: web.Application, *, store: StudioStore, studio: Studio, news: NewsStore | None, sites_dir: Path,
                 check_admin: Callable[[web.Request], Awaitable[web.Response | None]], client_ip: Callable[[web.Request], str],
                 limiter: Callable[[int, float], Any], wake_worker: Callable[[], None], wake_news: Callable[[], None]) -> None:
    lead_limit = limiter(5, 600)              # 5 заявок за 10 минут с одного адреса на сайты клиентов

    def admin(handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        async def wrapped(request: web.Request) -> web.StreamResponse:
            denied = await check_admin(request)
            return denied if denied is not None else await handler(request)
        return wrapped

    # ---------- сайты клиентов ----------
    async def site(request: web.Request) -> web.StreamResponse:
        slug = request.match_info["slug"]
        version = request.match_info.get("version")
        if not SLUG.match(slug):
            raise web.HTTPNotFound()
        file = sites_dir / slug / (f"v{int(version)}.html" if version else "index.html")
        if not file.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(file, headers={"Cache-Control": "no-cache", "Content-Security-Policy": site_csp(),
                                               "Content-Type": "text/html; charset=utf-8"})

    async def site_redirect(request: web.Request) -> web.StreamResponse:
        slug = request.match_info["slug"]
        if not SLUG.match(slug):
            raise web.HTTPNotFound()
        raise web.HTTPMovedPermanently(f"/sites/{slug}/")

    async def lead(request: web.Request) -> web.Response:
        ip = client_ip(request)
        if lead_limit.blocked(ip):
            return _error(429, "Слишком много заявок. Попробуйте позже.")
        slug = request.match_info["slug"]
        project = await store.by_slug(slug) if SLUG.match(slug) else None
        if project is None or not project.version:
            return _error(404, "Сайт не найден.")
        raw = await _read_limited(request, 8 * 1024)
        if raw is None:
            return _error(413, "Заявка слишком большая.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _error(400, "Заявка должна быть в формате JSON.")
        if not isinstance(data, dict):
            return _error(400, "Пустая заявка.")
        if data.get("website"):                       # скрытое поле: его заполняют только боты
            return web.json_response({"ok": True})
        name, phone, message = _clean(data.get("name"), 80), _clean(data.get("phone"), 30), _clean(data.get("message"), 1000)
        if not name or sum(ch.isdigit() for ch in phone) < 7:
            return _error(400, "Нужны имя и телефон.")
        lead_limit.hit(ip)
        await store.add_lead(project.id, name, phone, message)
        wake_worker()
        return web.json_response({"ok": True})

    # ---------- страница совета ----------
    async def council_page(request: web.Request) -> web.StreamResponse:
        return web.FileResponse(PAGE, headers={"Cache-Control": "no-cache", "Content-Type": "text/html; charset=utf-8"})

    async def project_page(request: web.Request) -> web.StreamResponse:
        if not TOKEN.match(request.match_info["token"]):
            raise web.HTTPNotFound()
        return await council_page(request)

    @admin
    async def council_state(request: web.Request) -> web.Response:
        projects = await store.list_projects(50)
        return web.json_response({
            "ok": True, "agents": agents_payload(await store.agent_stats()), "projects": [project_payload(p) for p in projects],
            "lessons": (await store.all_lessons(100)), "tasks": (await store.tasks(None, 60)),
        }, headers={"Cache-Control": "no-store"})

    @admin
    async def council_feed(request: web.Request) -> web.Response:
        try:
            after = int(request.query.get("after", "0"))
            project_id = int(request.query["project"]) if request.query.get("project") else None
        except ValueError:
            return _error(400, "Неверные параметры.")
        tasks = await store.tasks(project_id) if project_id else []
        return web.json_response({"ok": True, "messages": await store.messages(project_id, after, 300), "tasks": tasks},
                                 headers={"Cache-Control": "no-store"})

    async def public_feed(request: web.Request) -> web.Response:
        token = request.match_info["token"]
        project = await store.by_token(token) if TOKEN.match(token) else None
        if project is None:
            return _error(404, "Проект не найден.")
        try:
            after = int(request.query.get("after", "0"))
        except ValueError:
            after = 0
        return web.json_response({"ok": True, "project": project_payload(project, public=True),
                                  "agents": agents_payload(await store.agent_stats()),
                                  "messages": await store.messages(project.id, after, 300), "tasks": await store.tasks(project.id)},
                                 headers={"Cache-Control": "no-store"})

    # ---------- управление ----------
    async def read_json(request: web.Request, limit: int = 32 * 1024) -> dict[str, Any] | None:
        raw = await _read_limited(request, limit)
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @admin
    async def create_project(request: web.Request) -> web.Response:
        data = await read_json(request)
        if data is None or not _clean(data.get("business"), 60):
            return _error(400, "Нужно название бизнеса (business).")
        brief = brief_from(data)
        project = await store.create_project(brief, slugify(brief["business"]))
        wake_worker()
        return web.json_response({"ok": True, "project": project_payload(project)})

    async def _project_from(request: web.Request) -> Project | None:
        try:
            return await store.get(int(request.match_info["id"]))
        except ValueError:
            return None

    @admin
    async def revise_project(request: web.Request) -> web.Response:
        project = await _project_from(request)
        data = await read_json(request)
        text = _clean((data or {}).get("request"), 1500)
        if project is None or not project.version:
            return _error(404, "Готовый проект не найден.")
        if not text:
            return _error(400, "Опишите правки (request).")
        await store.update(project.id, pending=text)
        wake_worker()
        return web.json_response({"ok": True})

    @admin
    async def rate_project(request: web.Request) -> web.Response:
        project = await _project_from(request)
        data = await read_json(request) or {}
        rating = data.get("rating")
        if project is None or not project.version:
            return _error(404, "Готовый проект не найден.")
        if not isinstance(rating, int) or not 1 <= rating <= 5:
            return _error(400, "Оценка — число от 1 до 5.")
        changed = await studio.rate(project, rating)
        return web.json_response({"ok": True, "lessons_changed": len(changed)})

    # ---------- новости ----------
    @admin
    async def post_news(request: web.Request) -> web.Response:
        if news is None:
            return _error(503, "Новости выключены.")
        data = await read_json(request)
        title = _clean((data or {}).get("title"), 120)
        if not title:
            return _error(400, "Нужен заголовок (title).")
        body = str((data or {}).get("body") or "")[:3500]
        news_id = await news.add_news(title, body, str((data or {}).get("url") or ""))
        wake_news()
        return web.json_response({"ok": True, "id": news_id})

    async def list_news(request: web.Request) -> web.Response:
        if news is None:
            return web.json_response({"ok": True, "news": []})
        items = [{"id": n.id, "title": n.title, "body": n.body, "url": n.url, "created_at": n.created_at} for n in await news.latest(10)]
        return web.json_response({"ok": True, "news": items})

    r = app.router
    r.add_get("/sites/{slug}", site_redirect)
    r.add_get("/sites/{slug}/", site)
    r.add_get(r"/sites/{slug}/v{version:\d{1,4}}", site)
    r.add_post("/api/sites/{slug}/lead", lead)
    r.add_get("/council", council_page)
    r.add_get("/studio/p/{token}", project_page)
    r.add_get("/api/council/state", council_state)
    r.add_get("/api/council/feed", council_feed)
    r.add_get("/api/studio/p/{token}/feed", public_feed)
    r.add_post("/api/studio/projects", create_project)
    r.add_post(r"/api/studio/projects/{id:\d+}/revise", revise_project)
    r.add_post(r"/api/studio/projects/{id:\d+}/rate", rate_project)
    r.add_get("/api/news", list_news)
    r.add_post("/api/news", post_news)


def brief_from(data: dict[str, Any]) -> dict[str, Any]:
    """Бриф из присланных данных: только известные поля, всё обрезано по длине."""
    contacts_in = data.get("contacts") if isinstance(data.get("contacts"), dict) else {}
    lang = data.get("lang") if data.get("lang") in ("ru", "uz", "en") else "ru"
    return {"business": _clean(data.get("business"), 60), "niche": _clean(data.get("niche"), 120),
            "description": _clean(data.get("description"), 600), "audience": _clean(data.get("audience"), 300),
            "style": _clean(data.get("style"), 200), "colors": _clean(data.get("colors"), 200), "city": _clean(data.get("city"), 60),
            "lang": lang, "contacts": {k: _clean(contacts_in.get(k), 120) for k in ("phone", "telegram", "instagram", "email", "address", "hours")}}


def bearer_password(request: web.Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    return unquote(token.strip()) if scheme.lower() == "bearer" else ""


def password_ok(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))
