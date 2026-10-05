"""Настройки сервера. Читаются из файла .env рядом с проектом и из переменных окружения."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

MIN_PASSWORD_LENGTH = 12
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
    admin_chat_ids: tuple[int, ...]
    admin_password: str
    host: str
    port: int
    site_dir: Path
    data_dir: Path
    trust_proxy: bool
    # студия white-label сайтов и новости
    public_url: str = ""                 # адрес сервера снаружи, для ссылок в Telegram: https://varaq.uz
    studio_enabled: bool = True
    claude_enabled: bool = False         # есть ANTHROPIC_API_KEY — агенты думают через Claude, иначе офлайн-режим
    studio_model: str = "claude-opus-5-5"
    studio_max_calls: int = 120          # предел вызовов Claude на один проект: защита бюджета
    studio_daily_limit: int = 2          # новых сайтов в сутки на одного пользователя (администраторам — без предела)
    studio_concurrency: int = 1          # сколько сайтов собирается одновременно
    debate_rounds: int = 2

    @property
    def publish_enabled(self) -> bool:
        return len(self.admin_password) >= MIN_PASSWORD_LENGTH

    @property
    def index_file(self) -> Path:
        return self.site_dir / "index.html"

    @classmethod
    def from_env(cls) -> "Config":
        load_env_file(ROOT / ".env")
        env = os.environ.get

        ids: list[int] = []
        for part in env("ADMIN_CHAT_IDS", "").replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError as exc:
                raise ConfigError(f"ADMIN_CHAT_IDS: «{part}» не похоже на номер чата. Нужны числа через запятую.") from exc

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

        public_url = env("PUBLIC_URL", "").strip().rstrip("/")
        if public_url and not public_url.startswith(("https://", "http://")):
            raise ConfigError("PUBLIC_URL должен начинаться с https://, например https://varaq.uz")

        return cls(
            bot_token=env("BOT_TOKEN", "").strip(),
            admin_chat_ids=tuple(dict.fromkeys(ids)),
            admin_password=password,
            host=env("HOST", "127.0.0.1").strip() or "127.0.0.1",
            port=port,
            site_dir=_path(env("SITE_DIR", ""), "site"),
            data_dir=_path(env("DATA_DIR", ""), "data"),
            trust_proxy=env("TRUST_PROXY", "1").strip() not in ("0", "false", "no", ""),
            public_url=public_url,
            studio_enabled=env("STUDIO_ENABLED", "1").strip() not in ("0", "false", "no"),
            claude_enabled=bool(env("ANTHROPIC_API_KEY", "").strip()) and env("STUDIO_OFFLINE", "0").strip() in ("0", ""),
            studio_model=env("STUDIO_MODEL", "claude-opus-5-5").strip() or "claude-opus-5-5",
            studio_max_calls=number("STUDIO_MAX_CALLS", 120, 10, 1000),
            studio_daily_limit=number("STUDIO_DAILY_LIMIT", 2, 0, 100),
            studio_concurrency=number("STUDIO_CONCURRENCY", 1, 1, 8),
            debate_rounds=number("STUDIO_DEBATE_ROUNDS", 2, 1, 4),
        )
