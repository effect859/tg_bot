"""AnthropicLLMClient без сети (поддельный HTTP-транспорт) и обязательный ключ API.

Настоящий API Anthropic в тестах не вызывается: транспорт отвечает заранее
заданными ответами и запоминает запросы.
"""

import json
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import SecretStr

from app import main_bot
from app.config import ConfigurationError, Settings, require_llm_api_key
from app.llm.anthropic import DEFAULT_TIMEOUT_SECONDS, MAX_CACHE_BREAKPOINTS, AnthropicLLMClient
from app.llm.base import LLMPermanentError, LLMTemporaryError, PromptBlock, StopReason

SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


def _message(text: str = '{"fields": {}}', stop_reason: str = "end_turn") -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1500,
            "output_tokens": 120,
            "cache_read_input_tokens": 300,
            "cache_creation_input_tokens": 0,
        },
    }


def _usage_message(*, cache_read: int | None, cache_creation: int | None) -> dict[str, Any]:
    message = _message()
    message["usage"] = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    return message


class Transport:
    """Отвечает ``responses`` по очереди; запросы — в ``requests``."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(transport: Transport) -> AnthropicLLMClient:
    http_client = anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(transport))
    return AnthropicLLMClient(
        api_key="test-key", model="claude-sonnet-5-5", http_client=http_client
    )


async def _complete(client: AnthropicLLMClient) -> Any:
    return await client.complete(
        system="SYS", prompt="PROMPT", max_tokens=16000, json_schema=SCHEMA
    )


async def test_request_body_and_result() -> None:
    transport = Transport(httpx2.Response(200, json=_message('{"fields": {"a": 1}}')))

    result = await _complete(_client(transport))

    [request] = transport.requests
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == "test-key"
    body = json.loads(request.content)
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 16000
    assert body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "PROMPT"}]
    assert body["output_config"] == {
        "effort": "medium",
        "format": {"type": "json_schema", "schema": SCHEMA},
    }
    # Серверный запасной вариант модели не включён.
    assert "fallbacks" not in body
    assert "anthropic-beta" not in request.headers
    assert result.text == '{"fields": {"a": 1}}'
    assert result.model == "claude-sonnet-5-5"
    assert (result.usage.input_tokens, result.usage.output_tokens) == (1500, 120)
    assert result.usage.cached_tokens == 300
    assert result.usage.cache_creation_tokens == 0
    assert result.stop_reason is StopReason.END_TURN
    assert result.latency_ms >= 0


async def test_model_override() -> None:
    transport = Transport(httpx2.Response(200, json=_message()))

    await _client(transport).complete(
        system="s", prompt="p", max_tokens=10, model="claude-haiku-4-5"
    )

    assert json.loads(transport.requests[0].content)["model"] == "claude-haiku-4-5"


async def test_prefix_blocks_with_cache_breakpoints() -> None:
    transport = Transport(httpx2.Response(200, json=_message()))
    prefix = [
        PromptBlock("CONTEXT", cache_ttl="1h"),
        PromptBlock("S1"),
        PromptBlock("S2", cache_ttl="1h"),
    ]

    await _client(transport).complete(system="s", prompt="TASK", max_tokens=10, prefix=prefix)

    [message] = json.loads(transport.requests[0].content)["messages"]
    assert message == {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": "CONTEXT",
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
            },
            {"type": "text", "text": "S1"},
            {"type": "text", "text": "S2", "cache_control": {"type": "ephemeral", "ttl": "1h"}},
            # Задание — последним блоком, без точки кэша.
            {"type": "text", "text": "TASK"},
        ],
    }


async def test_too_many_cache_breakpoints_rejected_before_request() -> None:
    transport = Transport(httpx2.Response(200, json=_message()))
    prefix = [PromptBlock(f"B{i}", cache_ttl="1h") for i in range(MAX_CACHE_BREAKPOINTS + 1)]

    with pytest.raises(ValueError):
        await _client(transport).complete(system="s", prompt="p", max_tokens=10, prefix=prefix)

    assert transport.requests == []


@pytest.mark.parametrize(
    ("cache_read", "cache_creation", "expected"),
    [(300, 1200, (300, 1200)), (None, None, (0, 0))],
)
async def test_cache_usage_mapping(
    cache_read: int | None, cache_creation: int | None, expected: tuple[int, int]
) -> None:
    message = _usage_message(cache_read=cache_read, cache_creation=cache_creation)
    transport = Transport(httpx2.Response(200, json=message))

    result = await _complete(_client(transport))

    # cached_tokens — чтение из кэша, cache_creation_tokens — запись в кэш.
    assert (result.usage.cached_tokens, result.usage.cache_creation_tokens) == expected


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("max_tokens", StopReason.MAX_TOKENS),
        ("refusal", StopReason.REFUSAL),
        ("pause_turn", StopReason.OTHER),
    ],
)
async def test_stop_reasons(stop_reason: str, expected: StopReason) -> None:
    transport = Transport(httpx2.Response(200, json=_message("{", stop_reason)))

    result = await _complete(_client(transport))

    assert result.stop_reason is expected


def _error(status: int) -> httpx2.Response:
    return httpx2.Response(
        status, json={"type": "error", "error": {"type": "api_error", "message": "x"}}
    )


@pytest.mark.parametrize("status", [408, 409, 429, 500, 503, 529])
async def test_temporary_status_errors(status: int) -> None:
    with pytest.raises(LLMTemporaryError) as caught:
        await _complete(_client(Transport(_error(status))))

    assert caught.value.code == f"http_{status}"
    assert caught.value.temporary


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
async def test_permanent_status_errors(status: int) -> None:
    with pytest.raises(LLMPermanentError) as caught:
        await _complete(_client(Transport(_error(status))))

    assert caught.value.code == f"http_{status}"
    assert not caught.value.temporary


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx2.ReadTimeout("timed out"), "timeout"),
        (httpx2.ConnectError("refused"), "connection"),
    ],
)
async def test_network_errors_are_temporary(error: Exception, code: str) -> None:
    with pytest.raises(LLMTemporaryError) as caught:
        await _complete(_client(Transport(error)))

    assert caught.value.code == code


@pytest.mark.parametrize(
    "failure",
    [
        _error(500),
        _error(529),
        _error(429),
        _error(408),
        httpx2.ReadTimeout("timed out"),
        httpx2.ConnectError("refused"),
    ],
    ids=["500", "529", "429", "408", "timeout", "connection"],
)
async def test_sdk_retry_is_bounded(failure: httpx2.Response | Exception) -> None:
    """Повторов SDK нет: один вызов complete — ровно один HTTP-запрос даже при ошибке,
    которую SDK по умолчанию повторил бы."""
    transport = Transport(failure, httpx2.Response(200, json=_message()))

    with pytest.raises(LLMTemporaryError):
        await _complete(_client(transport))

    assert len(transport.requests) == 1


def test_sdk_retries_disabled_and_timeout() -> None:
    client = AnthropicLLMClient(api_key="test-key", model="claude-sonnet-5-5")

    assert client._client.max_retries == 0
    assert client._client.timeout == DEFAULT_TIMEOUT_SECONDS == 300.0


# --- Ключ API ----------------------------------------------------------------------


def _settings(key: str | None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        bot_token=SecretStr("123:test"),
        anthropic_api_key=None if key is None else SecretStr(key),
    )


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_api_key_is_configuration_error(key: str | None) -> None:
    with pytest.raises(ConfigurationError):
        require_llm_api_key(_settings(key))


def test_api_key_is_returned_trimmed() -> None:
    assert require_llm_api_key(_settings(" sk-test ")) == "sk-test"


async def test_bot_does_not_start_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []
    monkeypatch.setattr(main_bot, "get_settings", lambda: _settings(None))
    monkeypatch.setattr(main_bot, "create_engine", lambda *a: created.append("engine"))

    with pytest.raises(ConfigurationError):
        await main_bot.main()

    assert created == []  # до подключения к БД дело не дошло


def test_default_model_is_sonnet() -> None:
    assert _settings("k").llm_model == "claude-sonnet-5-5"
