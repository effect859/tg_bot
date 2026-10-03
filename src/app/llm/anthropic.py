"""LLMClient для Anthropic (официальный Python SDK).

Структурированный ответ — через ``output_config.format`` (json_schema). Серверный
запасной вариант модели (fallbacks) намеренно не включён: модель предсказуема, а
расход учитывается по одной модели. Повторы SDK выключены (``max_retries=0``):
один вызов ``complete`` — не больше одного HTTP-запроса, и лимит вызовов модели у
вызывающего кода совпадает с числом реальных запросов. Повторы решает он сам.
Тайм-аут взят с запасом для большого входа; его проверит первый живой вызов.
"""

import time
from typing import Any

import anthropic

from app.llm.base import (
    LLMPermanentError,
    LLMResult,
    LLMTemporaryError,
    LLMUsage,
    StopReason,
)

DEFAULT_TIMEOUT_SECONDS = 300.0
# Не настраивается: повтор SDK обходил бы лимит вызовов модели на заказ.
SDK_MAX_RETRIES = 0
DEFAULT_EFFORT = "medium"

_STOP_REASONS = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}


class AnthropicLLMClient:
    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        effort: str = DEFAULT_EFFORT,
        http_client: anthropic.DefaultAsyncHttpxClient | None = None,
    ) -> None:
        self.model = model
        self._effort = effort
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout,
            max_retries=SDK_MAX_RETRIES,
            http_client=http_client,
        )

    async def complete(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int,
        model: str | None = None,
        json_schema: dict[str, Any] | None = None,
    ) -> LLMResult:
        output_config: dict[str, Any] = {"effort": self._effort}
        if json_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": json_schema}
        started = time.monotonic()
        try:
            response = await self._client.messages.create(
                model=model or self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_config=output_config,
            )
        except anthropic.APIStatusError as exc:
            raise _status_error(exc) from exc
        except anthropic.APIConnectionError as exc:  # в том числе APITimeoutError
            code = "timeout" if isinstance(exc, anthropic.APITimeoutError) else "connection"
            raise LLMTemporaryError(code) from exc
        except anthropic.APIResponseValidationError as exc:
            raise LLMTemporaryError("invalid_api_response") from exc
        except anthropic.AnthropicError as exc:  # учётные данные и прочие ошибки клиента
            raise LLMPermanentError("client_error", type(exc).__name__) from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        usage = response.usage
        return LLMResult(
            text=text,
            model=response.model,
            usage=LLMUsage(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=usage.cache_read_input_tokens or 0,
            ),
            latency_ms=int((time.monotonic() - started) * 1000),
            stop_reason=_STOP_REASONS.get(response.stop_reason or "", StopReason.OTHER),
        )


def _status_error(exc: anthropic.APIStatusError) -> LLMTemporaryError | LLMPermanentError:
    status = exc.status_code
    code = f"http_{status}"
    # Как и сам SDK: 408, 409, 429 и 5xx (в том числе 529 overloaded) — временные.
    if status in (408, 409, 429) or status >= 500:
        return LLMTemporaryError(code)
    return LLMPermanentError(code)
