"""Подмена LLMClient для тестов: заданные ответы, без сети."""

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.domain.requirements import LLM_FIELDS, LLM_LIST_FIELDS
from app.llm.base import LLMResult, LLMUsage, PromptBlock, StopReason

Answer = LLMResult | Exception | Callable[[], LLMResult]


class FakeLLMClient:
    """Отдаёт ответы по очереди (последний повторяется) и запоминает запросы."""

    provider = "fake"
    model = "fake-model"

    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers) or [result()]
        self.calls: list[dict[str, Any]] = []
        self.on_complete: Callable[[], Awaitable[object]] | None = None

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
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "model": model,
                "json_schema": json_schema,
                "prefix": list(prefix),
            }
        )
        if self.on_complete is not None:
            await self.on_complete()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer() if callable(answer) else answer


def response_json(
    extra_requirements: list[tuple[str, str]] | None = None,
    references: list[tuple[str, str]] | None = None,
    **fields: tuple[Any, Any],
) -> str:
    """JSON ответа модели: ``topic=("значение", "цитата")``, остальные поля — null."""
    payload: dict[str, Any] = {
        name: {"value": None, "evidence": None}
        for name in LLM_FIELDS
        if name not in LLM_LIST_FIELDS
    }
    for name, (value, evidence) in fields.items():
        payload[name] = {"value": value, "evidence": evidence}
    payload["extra_requirements"] = [
        {"value": value, "evidence": evidence} for value, evidence in extra_requirements or []
    ]
    payload["references"] = [
        {"value": value, "evidence": evidence} for value, evidence in references or []
    ]
    return json.dumps({"fields": payload}, ensure_ascii=False)


def result(text: str | None = None, stop_reason: StopReason = StopReason.END_TURN) -> LLMResult:
    return LLMResult(
        text=response_json() if text is None else text,
        model="fake-model-2026",
        usage=LLMUsage(input_tokens=1200, output_tokens=150, cached_tokens=100),
        latency_ms=850,
        stop_reason=stop_reason,
    )
