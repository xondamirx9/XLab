"""Доступ к Claude для агентов студии.

Каждый ответ агента — JSON по схеме (structured outputs), поэтому код не разбирает свободный текст.
Если ключа нет, Claude отказал, сеть упала или лимит вызовов на проект исчерпан — агент отвечает
запасным детерминированным ответом (offline), и сайт всё равно собирается.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

log = logging.getLogger("site.studio.llm")

DEFAULT_MODEL = "claude-opus-5-5"
# цены за миллион токенов (вход, выход) — только для отчёта о стоимости проекта
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-4-5": (1.0, 5.0),
          "claude-fable-5-1": (10.0, 50.0)}


def obj(**props: Any) -> dict[str, Any]:
    """Схема объекта для structured outputs: все поля обязательны, лишних нет."""
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


STR = {"type": "string"}
INT = {"type": "integer"}
BOOL = {"type": "boolean"}


def enum(values: Any) -> dict[str, Any]:
    return {"type": "string", "enum": list(values)}


class LLM(Protocol):
    online: bool

    async def json(self, *, system: str, prompt: str, schema: dict[str, Any], effort: str,
                   offline: Callable[[], dict[str, Any]]) -> dict[str, Any]: ...


@dataclass
class Usage:
    calls: int = 0
    fallbacks: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0


class OfflineLLM:
    """Без нейросети: всегда запасной ответ. Для проверок и работы без ключа."""

    online = False

    def __init__(self) -> None:
        self.usage = Usage()

    async def json(self, *, system: str, prompt: str, schema: dict[str, Any], effort: str,
                   offline: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        self.usage.calls += 1
        self.usage.fallbacks += 1
        return offline()


class ClaudeLLM:
    """Claude через официальный SDK: адаптивное мышление, уровень усилия на агента, JSON по схеме."""

    online = True

    def __init__(self, model: str = DEFAULT_MODEL, max_calls: int = 120, client: Any = None) -> None:
        import anthropic                       # импорт здесь: без ключа сервер работает и без пакета
        self._anthropic = anthropic
        self._client = client or anthropic.AsyncAnthropic()
        self.model = model
        self.max_calls = max_calls
        self.usage = Usage()

    def _count(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        tokens_in = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        self.usage.input_tokens += tokens_in + cached
        self.usage.output_tokens += usage.output_tokens or 0
        price_in, price_out = PRICES.get(self.model, PRICES[DEFAULT_MODEL])
        self.usage.cost_usd += (tokens_in * price_in + cached * price_in * 0.1 + (usage.output_tokens or 0) * price_out) / 1_000_000

    async def json(self, *, system: str, prompt: str, schema: dict[str, Any], effort: str,
                   offline: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        self.usage.calls += 1
        if self.usage.calls > self.max_calls:               # бюджет проекта исчерпан — дальше без нейросети
            self.usage.fallbacks += 1
            return offline()
        errors = self._anthropic
        try:
            response = await self._client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                thinking={"type": "adaptive"},
                output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                cache_control={"type": "ephemeral"},
                # если классификатор безопасности откажет, запрос сам повторится на рекомендованной модели
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except errors.RateLimitError:
            log.warning("Claude: превышен лимит запросов, агент отвечает без нейросети")
        except errors.APIStatusError as exc:
            log.warning("Claude: ошибка %s, агент отвечает без нейросети", exc.status_code)
        except errors.APIConnectionError:
            log.warning("Claude: нет связи, агент отвечает без нейросети")
        else:
            self._count(response)
            if response.stop_reason in ("refusal", "max_tokens"):
                log.warning("Claude: ответ не получен (%s)", response.stop_reason)
            else:
                text = next((b.text for b in response.content if b.type == "text"), "")
                try:
                    data = json.loads(text)
                    if isinstance(data, dict):
                        return data
                except json.JSONDecodeError:
                    log.warning("Claude: ответ не разобрался как JSON")
        self.usage.fallbacks += 1
        return offline()


@dataclass
class LLMFactory:
    """Каждому проекту — свой счётчик вызовов и стоимости."""

    model: str = DEFAULT_MODEL
    max_calls: int = 120
    enabled: bool = True
    make_client: Callable[[], Any] | None = field(default=None, repr=False)

    def __call__(self) -> LLM:
        if not self.enabled:
            return OfflineLLM()
        try:
            client = self.make_client() if self.make_client else None
            return ClaudeLLM(self.model, self.max_calls, client=client)
        except Exception as exc:                            # нет пакета или ключа — работаем без нейросети
            log.warning("Claude недоступен (%s): агенты работают без нейросети", type(exc).__name__)
            return OfflineLLM()
