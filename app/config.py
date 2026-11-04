"""Настройки сервера. Читаются из файла .env рядом с проектом и из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.seo import verification_code

MIN_PASSWORD_LENGTH = 12
BRAND = "Codex Lab"          # название студии в боте и сообщениях
ROOT = Path(__file__).resolve().parent.parent


class ConfigError(Exception):
    """Ошибка в настройках: текст можно показать человеку как есть."""


def load_env_file(path: Path) -> None:
    """Читает строки вида КЛЮЧ=значение. Переменные, уже заданные в окружении, не трогает."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _path(value: str, default: str) -> Path:
    path = Path(value or default)
    return path if path.is_absolute() else ROOT / path


@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_chat_ids: tuple[int, ...]      # чаты, куда приходят заявки и уведомления
    admin_password: str
    host: str
    port: int
    site_dir: Path
    data_dir: Path
    trust_proxy: bool
    cloud_proxy: bool = False            # за прокси облачной платформы (Render): адрес посетителя — из X-Forwarded-For
    # студия white-label сайтов и новости
    public_url: str = ""                 # адрес сервера снаружи, для ссылок в Telegram: https://codexlab.uz
    studio_enabled: bool = True
    claude_enabled: bool = False         # есть ANTHROPIC_API_KEY — агенты думают через Claude, иначе офлайн-режим
    studio_model: str = "claude-opus-5-5"
    studio_max_calls: int = 120          # предел вызовов Claude на один проект: защита бюджета
    studio_daily_limit: int = 2          # новых сайтов в сутки на одного пользователя (администраторам — без предела)
    studio_concurrency: int = 1          # сколько сайтов собирается одновременно
    debate_rounds: int = 2
    # подтверждение прав на сайт в Google Search Console, Яндекс Вебмастере и Bing: имя тега <meta> → код
    search_verify: tuple[tuple[str, str], ...] = ()
    indexnow: bool = False               # сообщать Bing и Яндексу о новой версии сайта (app/indexnow.py)
    admin_user_ids: tuple[int, ...] = () # люди-администраторы; личные чаты из admin_chat_ids считаются и так

    @property
    def publish_enabled(self) -> bool:
        return len(self.admin_password) >= MIN_PASSWORD_LENGTH

    @property
    def admin_users(self) -> tuple[int, ...]:
        """Номера людей-администраторов. У группы номер отрицательный, у личного чата он равен номеру человека."""
        return tuple(dict.fromkeys((*self.admin_user_ids, *(chat for chat in self.admin_chat_ids if chat > 0))))

    def is_admin(self, chat_id: int, user_id: int | None) -> bool:
        """Права проверяются у человека: состоять в группе из ADMIN_CHAT_IDS мало, а чужой чат не место для заявок."""
        return user_id in self.admin_users and (chat_id == user_id or chat_id in self.admin_chat_ids)

    @property
    def index_file(self) -> Path:
        return self.site_dir / "index.html"

    @classmethod
    def from_env(cls) -> "Config":
        load_env_file(ROOT / ".env")
        env = os.environ.get

        def id_list(key: str) -> list[int]:
            found: list[int] = []
            for part in env(key, "").replace(";", ",").split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    found.append(int(part))
                except ValueError as exc:
                    raise ConfigError(f"{key}: «{part}» не похоже на номер. Нужны числа через запятую.") from exc
            return found

        ids = id_list("ADMIN_CHAT_IDS")
        user_ids = id_list("ADMIN_USER_IDS")
        if any(user <= 0 for user in user_ids):
            raise ConfigError("ADMIN_USER_IDS: нужны номера людей, а не групп. Номер человека положительный: его покажет /id.")

        try:
            port = int(env("PORT", "8080"))
        except ValueError as exc:
            raise ConfigError("PORT должен быть числом, например 8080.") from exc
        if not 1 <= port <= 65535:
            raise ConfigError("PORT должен быть от 1 до 65535.")

        password = env("ADMIN_PASSWORD", "")
        if password and len(password) < MIN_PASSWORD_LENGTH:
            raise ConfigError(f"ADMIN_PASSWORD короче {MIN_PASSWORD_LENGTH} символов. Задайте длиннее или оставьте пустым.")

        def number(key: str, default: int, low: int, high: int) -> int:
            raw = env(key, str(default)).strip() or str(default)
            try:
                value = int(raw)
            except ValueError as exc:
                raise ConfigError(f"{key} должен быть числом.") from exc
            if not low <= value <= high:
                raise ConfigError(f"{key} должен быть от {low} до {high}.")
            return value

        # TRUST_PROXY: 1 — прокси на этой же машине (Caddy, nginx), cloud — прокси облачной платформы, 0 — без прокси.
        # Не задан и сервер на Render (Render сам выставляет RENDER) — значит, облачная платформа.
        proxy = env("TRUST_PROXY")
        proxy = (proxy if proxy is not None else ("cloud" if env("RENDER", "").strip() else "1")).strip().lower()

        public_url = env("PUBLIC_URL", "").strip().rstrip("/")
        if public_url and not public_url.startswith(("https://", "http://")):
            raise ConfigError("PUBLIC_URL должен начинаться с https://, например https://codexlab.uz")

        verify: list[tuple[str, str]] = []
        for key, tag, where in (("GOOGLE_SITE_VERIFICATION", "google-site-verification", "Google Search Console"),
                                ("YANDEX_VERIFICATION", "yandex-verification", "Яндекс Вебмастера"),
                                ("BING_SITE_VERIFICATION", "msvalidate.01", "Bing Webmaster Tools")):
            raw = env(key, "").strip()
            if not raw:
                continue
            code = verification_code(raw)
            if not code:
                raise ConfigError(f"{key}: не похоже на код из {where}. Нужна строка из атрибута content тега <meta>.")
            verify.append((tag, code))

        # INDEXNOW: 1 — сообщать поисковикам о новой версии сайта, 0 — нет. Не задан — да, если сервер
        # смотрит в интернет: на Render (он сам выставляет RENDER) или задан PUBLIC_URL.
        indexnow = env("INDEXNOW", "").strip().lower()
        indexnow_on = indexnow in ("1", "true", "yes") or (indexnow == "" and bool(env("RENDER", "").strip() or public_url))

        return cls(
            bot_token=env("BOT_TOKEN", "").strip(),
            admin_chat_ids=tuple(dict.fromkeys(ids)),
            admin_user_ids=tuple(dict.fromkeys(user_ids)),
            admin_password=password,
            host=env("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            site_dir=_path(env("SITE_DIR", ""), "site"),
            data_dir=_path(env("DATA_DIR", ""), "data"),
            trust_proxy=proxy not in ("0", "false", "no", ""),
            cloud_proxy=proxy == "cloud",
            public_url=public_url,
            studio_enabled=env("STUDIO_ENABLED", "1").strip() not in ("0", "false", "no"),
            claude_enabled=bool(env("ANTHROPIC_API_KEY", "").strip()) and env("STUDIO_OFFLINE", "0").strip() in ("0", ""),
            studio_model=env("STUDIO_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5",
            studio_max_calls=number("STUDIO_MAX_CALLS", 120, 10, 1000),
            studio_daily_limit=number("STUDIO_DAILY_LIMIT", 2, 0, 100),
            studio_concurrency=number("STUDIO_CONCURRENCY", 1, 1, 8),
            debate_rounds=number("STUDIO_DEBATE_ROUNDS", 2, 1, 4),
            search_verify=tuple(verify),
            indexnow=indexnow_on,
        )
