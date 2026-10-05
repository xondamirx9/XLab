"""HTTP-часть: сайт, приём заявок, публикация новой версии сайта из админки."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import unquote

from aiohttp import web

from app.config import Config
from app.storage import Storage

log = logging.getLogger("site.web")

MAX_ORDER_BYTES = 64 * 1024
MAX_PUBLISH_BYTES = 12 * 1024 * 1024
MAX_TEXT_CHARS = 20_000
KEEP_BACKUPS = 20
PROJECTS = {"landing": "Лендинг", "corp": "Сайт компании", "shop": "Интернет-магазин",
            "app": "Веб-сервис с кабинетом", "bot": "Telegram-бот"}
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Шаги воронки. «order» сайт прислать не может: его считает сам сервер, когда заявка сохранена.
SITE_EVENTS = ("visit", "brief_start", "brief_done")
TASHKENT = timezone(timedelta(hours=5))


def today() -> str:
    """Сегодняшняя дата по Ташкенту: статистика считается по местным суткам."""
    return datetime.now(TASHKENT).strftime("%Y-%m-%d")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    # Сайт — один файл со встроенными стилями и кодом; шрифты — с Google Fonts; картинки проектов — внутри страницы.
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


class RateLimiter:
    """Не больше `limit` событий за `window` секунд на один ключ (обычно IP-адрес)."""

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit, self.window, self._clock = limit, window, clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str) -> deque[float]:
        hits, edge = self._hits[key], self._clock() - self.window
        while hits and hits[0] <= edge:
            hits.popleft()
        return hits

    def blocked(self, key: str) -> bool:
        return len(self._trim(key)) >= self.limit

    def hit(self, key: str) -> None:
        self._trim(key).append(self._clock())
        if len(self._hits) > 10_000:                      # не даём словарю расти бесконечно
            for stale in [k for k, v in self._hits.items() if not v]:
                del self._hits[stale]


async def _read_limited(request: web.Request, limit: int) -> bytes | None:
    """Читает тело запроса целиком, но не больше limit байт. None — тело слишком большое."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def _json_error(status: int, message: str) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit] if isinstance(value, (str, int, float)) else ""


def client_ip(request: web.Request, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # Берём последний адрес: его дописал наш собственный прокси (Caddy или nginx).
            # Первые значения в этом заголовке клиент может подделать.
            return forwarded.split(",")[-1].strip()
    return request.remote or "unknown"


def _estimate_text(estimate: object) -> str:
    """«3–9 млн сум, 1–2 нед.» из чисел, которые прислал сайт. Чужие строки сюда не попадают."""
    if not isinstance(estimate, dict):
        return ""
    try:
        lo, hi, wl, wh = (float(estimate[k]) for k in ("lo", "hi", "wl", "wh"))
    except (KeyError, TypeError, ValueError):
        return ""
    number = lambda x: f"{x:g}".replace(".", ",")
    return f"{number(lo)}–{number(hi)} млн сум, {number(wl)}–{number(wh)} нед."


def create_app(cfg: Config, storage: Storage, on_new_order: Callable[[], None],
               bot_enabled: bool = False) -> web.Application:
    order_limit = RateLimiter(limit=5, window=600)        # 5 заявок за 10 минут с одного адреса
    order_total = RateLimiter(limit=120, window=3600)     # и не больше 120 в час со всех адресов
    auth_fails = RateLimiter(limit=5, window=900)         # 5 неверных паролей за 15 минут
    event_limit = RateLimiter(limit=30, window=600)       # счётчик воронки: до 30 событий за 10 минут с адреса

    @web.middleware
    async def headers(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
        try:
            response = await handler(request)
        except web.HTTPException as exc:      # 404 и подобные тоже получают защитные заголовки
            for key, value in SECURITY_HEADERS.items():
                exc.headers.setdefault(key, value)
            raise
        for key, value in SECURITY_HEADERS.items():
            response.headers.setdefault(key, value)
        return response

    async def index(request: web.Request) -> web.StreamResponse:
        if not cfg.index_file.is_file():
            return web.Response(status=503, text="Сайт ещё не загружен: положите index.html в папку site.")
        return web.FileResponse(cfg.index_file, headers={"Cache-Control": "no-cache"})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"app": "varaq-site", "ok": True, "publish": cfg.publish_enabled, "bot": bot_enabled},
                                 headers={"Cache-Control": "no-store"})

    async def order(request: web.Request) -> web.Response:
        ip = client_ip(request, cfg.trust_proxy)
        if order_limit.blocked(ip) or order_total.blocked("all"):
            return _json_error(429, "Слишком много заявок. Попробуйте позже.")
        if (request.content_length or 0) > MAX_ORDER_BYTES:
            return _json_error(413, "Заявка слишком большая.")
        raw = await _read_limited(request, MAX_ORDER_BYTES)
        if raw is None:
            return _json_error(413, "Заявка слишком большая.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _json_error(400, "Заявка должна быть в формате JSON.")
        if not isinstance(data, dict) or not isinstance(data.get("brief"), dict):
            return _json_error(400, "В заявке нет ответов клиента.")

        brief = data["brief"]
        client_id = data.get("id") if isinstance(data.get("id"), str) else ""
        text = data.get("text") if isinstance(data.get("text"), str) else ""
        name = _clean(brief.get("name"), 80)
        phone = _clean(brief.get("phone"), 30)
        telegram = _clean(brief.get("tg"), 40).lstrip("@")
        if not CLIENT_ID.match(client_id):
            return _json_error(400, "У заявки нет номера.")
        if not text.strip() or len(text) > MAX_TEXT_CHARS:
            return _json_error(400, "Текст ТЗ пустой или слишком длинный.")
        if not name:
            return _json_error(400, "Не указано имя.")
        if sum(ch.isdigit() for ch in phone) < 9 and not re.fullmatch(r"[A-Za-z0-9_]{4,32}", telegram):
            return _json_error(400, "Нужен телефон или ник в Telegram.")

        order_limit.hit(ip)
        order_total.hit("all")
        number, created = await storage.add_order(
            client_id=client_id, name=name, phone=phone, telegram=telegram,
            project=PROJECTS.get(str(brief.get("type")), "Проект"), estimate=_estimate_text(data.get("estimate")),
            text=text.strip(), brief_json=json.dumps(brief, ensure_ascii=False)[:MAX_ORDER_BYTES],
        )
        if created:
            log.info("заявка сохранена: номер %s", number)       # без имени и контактов: в журнал они не попадают
            await storage.bump("order", today())
            on_new_order()
        return web.json_response({"ok": True, "number": number})

    async def event(request: web.Request) -> web.Response:
        """Счётчик воронки. Хранится только число событий за день: ни адреса, ни устройства не записываются."""
        ip = client_ip(request, cfg.trust_proxy)
        if event_limit.blocked(ip):
            return _json_error(429, "Слишком много событий.")
        raw = await _read_limited(request, 1024)
        try:
            data = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        name = data.get("name") if isinstance(data, dict) else None
        if name not in SITE_EVENTS:
            return _json_error(400, "Неизвестное событие.")
        event_limit.hit(ip)
        await storage.bump(name, today())
        return web.json_response({"ok": True})

    async def publish(request: web.Request) -> web.Response:
        ip = client_ip(request, cfg.trust_proxy)
        if not cfg.publish_enabled:
            return _json_error(403, "Публикация выключена: не задан ADMIN_PASSWORD.")
        if auth_fails.blocked(ip):
            return _json_error(429, "Слишком много попыток. Подождите 15 минут.")
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        password = unquote(token.strip()) if scheme.lower() == "bearer" else ""
        if not hmac.compare_digest(password.encode("utf-8"), cfg.admin_password.encode("utf-8")):
            auth_fails.hit(ip)
            log.warning("публикация: неверный пароль")
            return _json_error(401, "Неверный пароль.")
        if (request.content_length or 0) > MAX_PUBLISH_BYTES:
            return _json_error(413, "Страница слишком большая.")
        raw = await _read_limited(request, MAX_PUBLISH_BYTES)
        if raw is None:
            return _json_error(413, "Страница слишком большая.")
        try:
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _json_error(400, "Страница должна быть в кодировке UTF-8.")
        if not html.lstrip()[:15].lower().startswith("<!doctype html") or 'id="site-js"' not in html or 'id="site-data"' not in html:
            return _json_error(400, "Это не страница сайта.")

        backup = _write_site(cfg.index_file, raw)
        log.info("сайт обновлён из админки; прежняя версия: %s", backup.name if backup else "нет")
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[headers], client_max_size=MAX_PUBLISH_BYTES + 1024)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/api/health", health)
    app.router.add_post("/api/order", order)
    app.router.add_post("/api/event", event)
    app.router.add_post("/api/publish", publish)
    return app


def _write_site(index_file: Path, content: bytes) -> Path | None:
    """Сохраняет прежнюю версию в backups и подменяет файл целиком: посетитель не увидит полузаписанную страницу."""
    index_file.parent.mkdir(parents=True, exist_ok=True)
    backups = index_file.parent / "backups"
    backup: Path | None = None
    if index_file.is_file():
        backups.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
        backup = backups / f"index-{stamp}.html"
        backup.write_bytes(index_file.read_bytes())
        for old in sorted(backups.glob("index-*.html"))[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
    temp = index_file.with_suffix(".html.tmp")
    temp.write_bytes(content)
    os.replace(temp, index_file)
    return backup
