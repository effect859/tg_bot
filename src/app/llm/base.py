"""Интерфейс LLM-клиента. Реализация под конкретного провайдера — на этапе генерации."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class LLMUsage:
    input_tokens: int
    output_tokens: int
    cached_tokens: int = 0


@dataclass(frozen=True, slots=True)
class LLMResult:
    text: str
    model: str
    usage: LLMUsage
    latency_ms: int


class LLMClient(Protocol):
    provider: str

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        model: str | None = None,
    ) -> LLMResult:
        """Один запрос к модели. Расход токенов вызывающий код пишет в llm_calls."""
        ...
