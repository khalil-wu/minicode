"""Regression test: streamed token usage must survive the finish_reason chunk.

With stream_options.include_usage, an OpenAI-compatible gateway sends the token
counts in a trailing chunk (choices: [], usage: {...}) AFTER the finish_reason
chunk. An earlier implementation broke out of the stream loop on finish_reason,
dropping that trailing chunk and leaving usage at zero — which silently broke
token tracking and the context-budget logic that consumes it.
"""

import asyncio
import json

from backend.config import LLMSettings
from backend.llm.base import StreamEventType
from backend.llm.errors import classify_llm_error
from backend.llm.openai_adapter import OpenAIAdapter


class _FakeStreamResponse:
    """Mimics the httpx streaming response context manager."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.status_code = 200

    async def __aenter__(self) -> "_FakeStreamResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeHTTPClient:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def stream(self, *args: object, **kwargs: object) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._lines)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}"


async def _collect(adapter: OpenAIAdapter, payload: dict):
    return [ev async for ev in adapter._emit_chat_http_stream_events(payload)]


def test_usage_captured_from_trailing_chunk_after_finish_reason():
    # Realistic gateway sequence: content -> finish_reason -> usage-only -> DONE.
    lines = [
        _sse({"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        _sse({
            "choices": [],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 3},
            },
        }),
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))

    done = [e for e in events if e.type == StreamEventType.DONE]
    assert len(done) == 1
    usage = done[0].usage
    assert usage.input_tokens == 42
    assert usage.output_tokens == 7
    assert usage.cache_read_input_tokens == 10
    assert usage.reasoning_output_tokens == 3


def test_deepseek_prompt_cache_usage_fields_are_captured():
    lines = [
        _sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        _sse({
            "choices": [],
            "usage": {
                "prompt_cache_hit_tokens": 16000,
                "prompt_cache_miss_tokens": 7000,
                "completion_tokens": 5,
            },
        }),
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))

    done = [e for e in events if e.type == StreamEventType.DONE]
    assert len(done) == 1
    usage = done[0].usage
    assert usage.input_tokens == 23000
    assert usage.output_tokens == 5
    assert usage.cache_read_input_tokens == 16000
    assert done[0].raw["usage"]["prompt_cache_hit_tokens"] == 16000
    assert done[0].raw["usage"]["prompt_cache_miss_tokens"] == 7000
    assert done[0].raw["usage"]["cached_prompt_tokens"] == 16000


def test_prompt_cache_write_tokens_are_reported_separately():
    lines = [
        _sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        _sse({
            "choices": [],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 4,
                "prompt_tokens_details": {
                    "cached_tokens": 60,
                    "cache_write_tokens": 30,
                },
            },
        }),
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))
    done = [event for event in events if event.type == StreamEventType.DONE][0]

    assert done.usage.cache_read_input_tokens == 60
    assert done.usage.cache_creation_input_tokens == 30
    assert done.raw["usage"]["cache_creation_input_tokens"] == 30


def test_text_still_streams_and_terminates_without_usage_chunk():
    # No trailing usage chunk: must still finish cleanly with zero usage.
    lines = [
        _sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))

    text = "".join(e.content for e in events if e.type == StreamEventType.TEXT_CHUNK)
    assert text == "hi"
    done = [e for e in events if e.type == StreamEventType.DONE]
    assert len(done) == 1
    assert done[0].usage.input_tokens == 0


def test_finish_reason_is_terminal_when_gateway_omits_done_sentinel():
    lines = [
        _sse({"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))

    assert not [event for event in events if event.type == StreamEventType.ERROR]
    done = [event for event in events if event.type == StreamEventType.DONE]
    assert len(done) == 1
    assert done[0].finish_reason == "stop"
    assert done[0].raw["terminal_fallback"] == "eof_after_finish_reason"


def test_empty_eof_remains_a_retryable_stream_error():
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient([])

    events = asyncio.run(_collect(adapter, {}))

    errors = [event for event in events if event.type == StreamEventType.ERROR]
    assert len(errors) == 1
    assert "ended before [DONE]" in errors[0].content
    assert classify_llm_error(errors[0].content).retryable is True
    assert errors[0].raw["event_type"] == "eof_without_terminal"
    assert "request_summary" in errors[0].raw


def test_invalid_provider_usage_counters_do_not_pollute_accounting():
    lines = [
        _sse({"choices": [{"delta": {"content": "ok"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        _sse({
            "choices": [],
            "usage": {
                "prompt_tokens": -7,
                "completion_tokens": 1.5,
                "prompt_tokens_details": {"cached_tokens": True},
                "completion_tokens_details": {"reasoning_tokens": "NaN"},
                "cost_usd": "Infinity",
            },
        }),
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))
    done = next(event for event in events if event.type == StreamEventType.DONE)

    assert done.usage.input_tokens == 0
    assert done.usage.output_tokens == 0
    assert done.usage.cache_read_input_tokens == 0
    assert done.usage.reasoning_output_tokens == 0
    assert done.usage.cost_usd == 0.0


def test_chat_tool_calls_fail_closed_when_finish_reason_says_stop():
    lines = [
        _sse({
            "choices": [{
                "delta": {
                    "tool_calls": [{
                        "index": 0,
                        "id": "call-mismatch",
                        "function": {
                            "name": "run_command",
                            "arguments": '{"command":"pwd"}',
                        },
                    }],
                },
                "finish_reason": "stop",
            }],
        }),
        "data: [DONE]",
    ]
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)

    events = asyncio.run(_collect(adapter, {}))

    assert events[-1].type == StreamEventType.ERROR
    assert events[-1].raw["event_type"] == "tool_finish_reason_mismatch"
    assert not [
        event
        for event in events
        if event.type == StreamEventType.TOOL_CALL and event.tool_calls_final
    ]
