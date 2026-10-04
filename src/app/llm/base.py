"""Интерфейс LLM-клиента: провайдеро-независимый контракт одного запроса к модели.

Реализация для Anthropic — app.llm.anthropic. Клиент только передаёт запрос и
переводит ответ провайдера в общие типы; что делать с ответом (проверка, повтор,
учёт в llm_calls) решает вызывающий код.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal, Protocol

CacheTTL = Literal["5m", "1h"]


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    # Прочитано из кэша промпта.
    cached_tokens: int = 0
    # Записано в кэш промпта (у провайдера — отдельно от input_tokens).
    cache_creation_tokens: int = 0


@dataclass(frozen=True, slots=True)
class PromptBlock:
    """Часть запроса перед ``prompt``. ``cache_ttl`` — точка кэша промпта после блока.

    Кэш — совпадение префикса запроса: блоки, одинаковые у нескольких запросов,
    идут первыми, а точки кэша ставятся на границах этих общих частей.
    """

    text: str
    cache_ttl: CacheTTL | None = None


class StopReason(StrEnum):
    """Почему модель закончила ответ (общие значения для всех провайдеров)."""

    END_TURN = "end_turn"
    # Ответ обрезан по max_tokens: структурированный ответ может быть неполным.
    MAX_TOKENS = "max_tokens"
    # Модель отказалась отвечать (политика безопасности провайдера).
    REFUSAL = "refusal"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    usage: LLMUsage
    latency_ms: int
    stop_reason: StopReason = StopReason.END_TURN


class LLMError(Exception):
    """Запрос к модели не выполнен. ``code`` — короткий код для логов и llm_calls."""

    temporary: bool = False

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


class LLMTemporaryError(LLMError):
    """Повтор имеет смысл: лимит запросов, перегрузка, ошибка сервера, сеть, тайм-аут."""

    temporary = True


class LLMPermanentError(LLMError):
    """Повтор с тем же запросом даст тот же отказ: запрос, ключ, модель, права."""


class LLMClient(Protocol):
    provider: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
        prefix: Sequence[PromptBlock] = (),
    ) -> LLMResult:
        """Один запрос к модели.

        ``prefix`` — блоки запроса перед ``prompt`` (с точками кэша промпта); пустой —
        запрос из одного ``prompt``. ``json_schema`` — ответ строго по схеме (текст
        результата — JSON). Ответ, полученный от провайдера, возвращается всегда, в
        том числе обрезанный или с отказом — см. ``stop_reason``. Ошибки запроса —
        LLMTemporaryError и LLMPermanentError. Расход токенов вызывающий код пишет в llm_calls.
        """
        ...
