from __future__ import annotations

import asyncio
import json
import json as json_module
import typing
from types import SimpleNamespace

import pytest

from backend.config import LLMSettings
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    LLMTurnContext,
    SideQueryOptions,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)
from backend.llm.openai_adapter import (
    OpenAIAdapter,
    _OPENAI_RESPONSE_STREAM_EVENT_TYPES,
    _chat_explicit_prompt_cache_messages,
    _estimate_prompt_tokens_for_cache,
    _instruction_text_from_chat_payload,
    _responses_explicit_prompt_cache_input,
    _response_finish_reason,
    _responses_provider_item_from_output,
    _responses_provider_items_from_response,
)


class _AsyncEvents:
    def __init__(self, events: list[object]) -> None:
        self._events = events

    def __aiter__(self):
        self._iterator = iter(self._events)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _jsonable(value):
    if isinstance(value, SimpleNamespace):
        return {key: _jsonable(item) for key, item in vars(value).items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


class _ResponsesCreate:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.requests: list[dict] = []
        self.last_response: _ResponsesHTTPResponse | None = None

    def stream(self, method, url, *, headers, json, timeout=None):
        del method, url, timeout
        record = dict(json)
        record["extra_headers"] = {
            key: value
            for key, value in headers.items()
            if key.startswith("x-") or key in {"session-id", "thread-id"}
        }
        self.requests.append(record)
        lines = [f"data: {json_module.dumps(_jsonable(event))}" for event in self.events]
        context = _ResponsesHTTPContext(lines)
        self.last_response = context.response
        return context


class _StrictResponsesCreate(_ResponsesCreate):
    """Model the installed OpenAI SDK's explicit Responses.create signature."""

    pass


class _ResponsesHTTPResponse:
    status_code = 200
    headers = {}

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.closed = False

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aclose(self):
        self.closed = True


class _ResponsesHTTPContext:
    def __init__(self, lines: list[str]) -> None:
        self._response = _ResponsesHTTPResponse(lines)

    @property
    def response(self) -> _ResponsesHTTPResponse:
        return self._response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        await self._response.aclose()
        return False


class _ChatHTTPClient:
    def __init__(self, *, text: str | None) -> None:
        self.text = text
        self.requests: list[dict] = []

    def stream(self, method, url, *, headers, json, timeout=None):
        del method, url, headers, timeout
        self.requests.append(dict(json))
        chunk = {
            "choices": [
                {
                    "delta": {
                        "content": self.text,
                        "reasoning_content": None,
                        "tool_calls": [],
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": None,
        }
        return _ResponsesHTTPContext(
            [f"data: {json_module.dumps(chunk)}", "data: [DONE]"]
        )


def _adapter(events: list[object]) -> tuple[OpenAIAdapter, _ResponsesCreate]:
    responses = _ResponsesCreate(events)
    return (
        OpenAIAdapter(
            LLMSettings(
                provider="openai",
                api_key="test-key",
                model="gpt-5.4",
                wire_api="responses",
            ),
            http_client=responses,
        ),
        responses,
    )


def _completed_response(*, text: str = "ok", input_tokens: int = 1, output_tokens: int = 1):
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            status="completed",
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
            output_text=text,
            output=[],
        ),
    )


async def _collect(stream):
    return [event async for event in stream]


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (SimpleNamespace(status="completed"), "completed"),
        (
            SimpleNamespace(
                status="incomplete",
                incomplete_details=SimpleNamespace(reason="max_output_tokens"),
            ),
            "max_output_tokens",
        ),
        (SimpleNamespace(status="failed"), "failed"),
        (SimpleNamespace(status="cancelled"), "cancelled"),
        (SimpleNamespace(status=""), ""),
        (None, ""),
    ],
)
def test_response_finish_reason_preserves_responses_terminal_semantics(
    response: object | None,
    expected: str,
) -> None:
    assert _response_finish_reason(response) == expected


def test_main_responses_request_uses_minicode_session_identity_and_cache_clamp() -> None:
    adapter, responses = _adapter([_completed_response()])
    session_id = "\u4f1a\u8bdd" + "s" * 70

    events = asyncio.run(
        _collect(
            adapter.stream_chat(
                [LLMMessage(role="user", content="hello")],
                metadata={
                    "session_id": session_id,
                    "conversation_id": "conv-identity",
                    "minicode_task_id": "task-must-not-become-turn",
                    "x-minicode-window-id": "window-1",
                },
            )
        )
    )

    assert events[-1].type == StreamEventType.DONE
    assert events[-1].finish_reason == "completed"
    assert events[-1].raw["finish_reason"] == "completed"
    assert "".join(
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK
    ) == "ok"
    request = responses.requests[0]
    assert request["prompt_cache_key"] == session_id[:64]
    assert not request["prompt_cache_key"].startswith("minicode-")
    assert request["client_metadata"] == {
        "session_id": session_id,
        "thread_id": "conv-identity",
        "x-minicode-window-id": "window-1",
    }
    assert request["extra_headers"] == {
        "x-client-request-id": "conv-identity",
        "session-id": session_id,
        "thread-id": "conv-identity",
        "x-minicode-window-id": "window-1",
    }
    assert request["tools"] == []
    assert request["tool_choice"] == "auto"
    assert request["parallel_tool_calls"] is True


def test_raw_responses_transport_projects_minicode_body_extensions_directly() -> None:
    responses = _StrictResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            model="gpt-5.4",
            wire_api="responses",
            prompt_cache_retention="24h",
        ),
        http_client=responses,
    )

    events = asyncio.run(
        _collect(
            adapter.stream_chat(
                [LLMMessage(role="user", content="hello")],
                metadata={
                    "session_id": "session-1",
                    "thread_id": "thread-1",
                    "turn_id": "turn-1",
                },
            )
        )
    )

    assert events[-1].type == StreamEventType.DONE
    request = responses.requests[0]
    assert request["prompt_cache_key"] == "session-1"
    assert request["client_metadata"] == {
        "session_id": "session-1",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
    }
    assert request["prompt_cache_retention"] == "24h"


def test_openai_explicit_cache_requires_the_official_minimum_prefix() -> None:
    short = "stable rules"
    input_items, enabled = _responses_explicit_prompt_cache_input(short, [])
    assert enabled is False
    assert input_items == []

    long_stable = "stable rules " + ("x" * (4 * 1_024))
    input_items, enabled = _responses_explicit_prompt_cache_input(long_stable, [])
    assert enabled is True
    assert input_items[0]["role"] == "developer"
    block = input_items[0]["content"][0]
    assert block["prompt_cache_breakpoint"] == {"mode": "explicit"}


def test_responses_authoritative_continuation_is_not_diagnostic_truncated() -> None:
    encrypted = "x" * 120_001
    summary = [SimpleNamespace(type="summary_text", text=f"summary-{index}") for index in range(17)]
    item = SimpleNamespace(
        type="reasoning",
        id="rs-long",
        encrypted_content=encrypted,
        summary=summary,
    )
    captured = _responses_provider_item_from_output(item)
    assert captured is not None
    assert captured["encrypted_content"] == encrypted
    assert len(captured["summary"]) == 17

    function_item = SimpleNamespace(
        type="function_call",
        id="fc-raw",
        call_id="call-raw",
        name="lookup",
        arguments='{ "z": 1, "a": 2 }',
    )
    function_capture = _responses_provider_item_from_output(function_item)
    assert function_capture is not None
    assert function_capture["arguments"] == '{ "z": 1, "a": 2 }'


def test_responses_authoritative_continuation_keeps_all_output_items() -> None:
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="function_call",
                id=f"fc-{index}",
                call_id=f"call-{index}",
                name="lookup",
                arguments="{}",
            )
            for index in range(65)
        ]
    )
    captured = _responses_provider_items_from_response(response)
    assert len(captured) == 65


def test_openai_explicit_cache_uses_utf8_bytes_for_multilingual_prefix() -> None:
    """CJK stable text must not be rejected by a Unicode code-point estimate."""

    stable = "中" * 1_366  # 4,098 UTF-8 bytes -> ceil(bytes / 4) == 1,025.
    assert _estimate_prompt_tokens_for_cache(stable) == 1_025

    input_items, enabled = _responses_explicit_prompt_cache_input(stable, [])

    assert enabled is True
    assert input_items[0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }


def test_openai_chat_explicit_cache_uses_utf8_bytes_for_multilingual_prefix() -> None:
    stable = "界" * 1_366

    messages, enabled = _chat_explicit_prompt_cache_messages(
        [{"role": "system", "content": stable}, {"role": "user", "content": "hi"}]
    )

    assert enabled is True
    assert messages[0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }


def test_official_gpt56_responses_keeps_implicit_latest_message_cache_on_main_wire() -> None:
    responses = _StrictResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="responses",
        ),
        http_client=responses,
    )
    stable = "stable rules " + ("x" * (4 * 1_024))

    events = asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(role="system", content=stable),
                    LLMMessage(role="user", content="hello"),
                ],
                metadata={"session_id": "session-explicit"},
            )
        )
    )

    assert events[-1].type == StreamEventType.DONE
    request = responses.requests[0]
    assert "instructions" not in request
    assert request["input"][0]["role"] == "developer"
    assert request["input"][0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }
    assert "prompt_cache_options" not in request.get("extra_body", {})
    summary = events[-1].raw["request_summary"]
    assert summary["instructions_len"] == len(stable)
    assert summary["instructions_hash"]
    assert "prompt_cache_options_mode" not in summary["request_params"]
    assert summary["request_params"]["prompt_cache_breakpoint_present"] is True
    assert summary["request_params"]["prompt_cache_breakpoint_count"] == 1
    assert stable not in json.dumps(summary, ensure_ascii=False)


def test_official_gpt56_responses_side_query_uses_explicit_only_mode() -> None:
    responses = _StrictResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="responses",
        ),
        http_client=responses,
    )
    stable = "stable side rules " + ("x" * (4 * 1_024))

    asyncio.run(
        adapter.side_query(
            [
                LLMMessage(role="system", content=stable),
                LLMMessage(role="user", content="summarize"),
            ],
            options=SideQueryOptions(
                operation="compact",
                session_id="side-explicit",
                enable_prompt_cache=True,
            ),
        )
    )

    request = responses.requests[0]
    assert request["prompt_cache_options"] == {"mode": "explicit"}


def test_official_gpt56_chat_keeps_implicit_latest_message_cache_on_main_wire() -> None:
    client = _ChatHTTPClient(text=None)

    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="chat",
        ),
        http_client=client,
    )
    stable = "stable chat rules " + ("x" * (4 * 1_024))

    events = asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(role="system", content=stable),
                    LLMMessage(role="user", content="hello"),
                ],
                metadata={"session_id": "chat-main"},
            )
        )
    )

    assert events[-1].type == StreamEventType.DONE
    request = client.requests[0]
    assert request["prompt_cache_key"] == "chat-main"
    assert "prompt_cache_options" not in request
    sent_messages = request["messages"]
    assert isinstance(sent_messages, list)
    assert sent_messages[0]["content"][0]["prompt_cache_breakpoint"] == {
        "mode": "explicit"
    }


def test_official_gpt56_chat_side_query_uses_explicit_only_mode() -> None:
    client = _ChatHTTPClient(text="ok")

    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="chat",
        ),
        http_client=client,
    )
    stable = "stable chat side rules " + ("x" * (4 * 1_024))

    result = asyncio.run(
        adapter.side_query(
            [
                LLMMessage(role="system", content=stable),
                LLMMessage(role="user", content="summarize"),
            ],
            options=SideQueryOptions(
                operation="compact",
                session_id="chat-side",
                enable_prompt_cache=True,
            ),
        )
    )

    assert result == "ok"
    assert client.requests[0]["prompt_cache_options"] == {"mode": "explicit"}


@pytest.mark.parametrize(
    ("provider", "base_url", "model"),
    [
        pytest.param("openai", "https://api.openai.com/v1", "gpt-5.5", id="older-openai-model"),
        pytest.param("openai", "https://gateway.example/v1", "gpt-5.6", id="custom-gateway"),
        pytest.param("custom", "https://api.openai.com/v1", "gpt-5.6", id="custom-provider"),
        pytest.param("custom", "https://api.deepseek.com/v1", "deepseek-chat", id="deepseek"),
    ],
)
def test_explicit_cache_is_not_sent_outside_the_official_gpt56_contract(
    provider: str,
    base_url: str,
    model: str,
) -> None:
    responses = _ResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider=provider,
            api_key="test-key",
            base_url=base_url,
            model=model,
            wire_api="responses",
        ),
        http_client=responses,
    )
    stable = "stable rules " + ("x" * (4 * 1_024))

    asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(role="system", content=stable),
                    LLMMessage(role="user", content="hello"),
                ],
                metadata={"session_id": "session-implicit"},
            )
        )
    )

    request = responses.requests[0]
    assert request["instructions"] == stable
    assert "prompt_cache_options" not in request
    assert "prompt_cache_breakpoint" not in json.dumps(request, ensure_ascii=False)


def test_short_official_gpt56_prefix_keeps_implicit_cache() -> None:
    responses = _ResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="responses",
        ),
        http_client=responses,
    )

    asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(role="system", content="short stable rules"),
                    LLMMessage(role="user", content="hello"),
                ],
                metadata={"session_id": "session-short"},
            )
        )
    )

    request = responses.requests[0]
    assert request["prompt_cache_key"] == "session-short"
    assert request["instructions"] == "short stable rules"
    assert "prompt_cache_options" not in request
    assert "prompt_cache_breakpoint" not in json.dumps(request, ensure_ascii=False)


def test_explicit_cache_diagnostics_keep_dynamic_suffix_out_of_stable_hash() -> None:
    responses = _ResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="responses",
        ),
        http_client=responses,
    )
    stable = "stable rules " + ("x" * (4 * 1_024))

    first = asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(
                        role="system",
                        content=stable + "\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\nworkspace-a",
                    ),
                    LLMMessage(role="user", content="hello"),
                ],
                metadata={"session_id": "session-diagnostic"},
            )
        )
    )[-1].raw["request_summary"]
    second = asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(
                        role="system",
                        content=stable + "\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\nworkspace-b",
                    ),
                    LLMMessage(role="user", content="hello"),
                ],
                metadata={"session_id": "session-diagnostic"},
            )
        )
    )[-1].raw["request_summary"]

    assert first["instructions_hash"] == second["instructions_hash"]
    assert first["instructions_full_hash"] != second["instructions_full_hash"]
    assert first["instructions_len"] == second["instructions_len"]


def test_openai_chat_explicit_cache_keeps_dynamic_suffix_unmarked() -> None:
    short_messages, enabled = _chat_explicit_prompt_cache_messages(
        [{"role": "system", "content": "short"}, {"role": "user", "content": "hi"}]
    )
    assert enabled is False
    assert short_messages[0]["role"] == "system"

    stable = "stable rules " + ("x" * (4 * 1_024))
    messages, enabled = _chat_explicit_prompt_cache_messages(
        [
            {"role": "system", "content": stable + "\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\nworkspace"},
            {"role": "user", "content": "hi"},
        ]
    )
    assert enabled is True
    assert messages[0]["content"][0]["prompt_cache_breakpoint"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in messages[1]["content"][0]
    reconstructed = _instruction_text_from_chat_payload(messages)
    assert reconstructed == stable + "\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\nworkspace"


def test_side_responses_stream_uses_standalone_identity_completed_output_and_usage() -> None:
    citation = SimpleNamespace(
        type="url_citation",
        url="https://example.test/source",
        title="Example source",
        start_index=0,
        end_index=5,
    )
    completed = SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(
            status="completed",
            usage=SimpleNamespace(input_tokens=7, output_tokens=3),
            output_text="",
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text='{"ok":true}',
                            annotations=[citation],
                        )
                    ],
                )
            ],
        ),
    )
    adapter, responses = _adapter(
        [
            SimpleNamespace(type="response.output_text.delta", delta="partial"),
            SimpleNamespace(
                type="response.output_text.done",
                text="partial",
                annotations=[citation],
            ),
            completed,
        ]
    )
    usage = UsageInfo()
    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="summarize")],
            options=SideQueryOptions(
                operation="compact",
                session_id="summary-session",
                thread_id="summary-thread",
                max_tokens=128,
                disable_reasoning=True,
                enable_prompt_cache=False,
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
                output_schema_name="summary_schema",
            ),
            turn_context=LLMTurnContext(usage=usage),
        )
    )

    assert result == (
        '{"ok":true}\n\nSources:\n'
        "- Example source: https://example.test/source"
    )
    assert usage.input_tokens == 7
    assert usage.output_tokens == 3
    request = responses.requests[0]
    assert request["stream"] is True
    assert request["store"] is False
    assert request["tools"] == []
    assert request["tool_choice"] == "auto"
    assert request["parallel_tool_calls"] is True
    assert request["max_output_tokens"] == 128
    assert "prompt_cache_key" not in request
    assert request["client_metadata"] == {
        "session_id": "summary-session",
        "thread_id": "summary-thread",
    }
    assert request["extra_headers"] == {
        "x-client-request-id": "summary-thread",
        "session-id": "summary-session",
        "thread-id": "summary-thread",
    }
    assert "prompt_cache_retention" not in request
    assert request["text"]["format"]["name"] == "summary_schema"
    assert request["text"]["format"]["strict"] is True


def test_side_responses_cache_opt_out_removes_key_options_and_breakpoint() -> None:
    responses = _ResponsesCreate([_completed_response()])
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            base_url="https://api.openai.com/v1",
            model="gpt-5.6",
            wire_api="responses",
        ),
        http_client=responses,
    )
    stable = "stable side rules " + ("x" * (4 * 1_024))

    asyncio.run(
        adapter.side_query(
            [
                LLMMessage(role="system", content=stable),
                LLMMessage(role="user", content="summarize"),
            ],
            options=SideQueryOptions(
                operation="compact",
                session_id="side-cache-disabled",
                enable_prompt_cache=False,
            ),
        )
    )

    request = responses.requests[0]
    assert request["instructions"] == stable
    assert "prompt_cache_key" not in request
    assert "prompt_cache_options" not in request
    assert "prompt_cache_breakpoint" not in json.dumps(request, ensure_ascii=False)


def test_side_responses_hosted_search_preserves_minicode_tool_controls() -> None:
    adapter, responses = _adapter([_completed_response(text="search result")])

    result = asyncio.run(
        adapter.side_query(
            [LLMMessage(role="user", content="search")],
            options=SideQueryOptions(
                operation="web_search_tool",
                session_id="search-session",
                disable_reasoning=True,
                enable_prompt_cache=False,
                hosted_web_search=True,
                web_search_allowed_domains=("example.com",),
            ),
        )
    )

    assert result == "search result"
    request = responses.requests[0]
    assert request["tools"] == [
        {
            "type": "web_search",
            "external_web_access": True,
            "filters": {"allowed_domains": ["example.com"]},
        }
    ]
    assert request["tool_choice"] == "auto"
    assert request["parallel_tool_calls"] is True


def test_side_responses_rejects_eof_failed_incomplete_and_error_events() -> None:
    cases = [
        (
            [SimpleNamespace(type="response.output_text.delta", delta="partial")],
            "stream closed before response.completed",
        ),
        (
            [
                SimpleNamespace(
                    type="response.incomplete",
                    response=SimpleNamespace(
                        status="incomplete",
                        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                    ),
                )
            ],
            "Incomplete response returned, reason: max_output_tokens",
        ),
        (
            [
                SimpleNamespace(
                    type="response.failed",
                    error=SimpleNamespace(message="provider failed"),
                )
            ],
            "Responses API response failed: provider failed",
        ),
        (
            [
                SimpleNamespace(
                    type="error",
                    error=SimpleNamespace(message="stream error"),
                )
            ],
            "Responses API error: stream error",
        ),
    ]

    for index, (events, expected) in enumerate(cases):
        adapter, _responses = _adapter(events)
        try:
            asyncio.run(
                adapter.side_query(
                    [LLMMessage(role="user", content="summarize")],
                    options=SideQueryOptions(
                        operation=f"failure-{index}",
                        session_id=f"failure-session-{index}",
                        enable_prompt_cache=False,
                    ),
                )
            )
        except RuntimeError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError(f"terminal case {index} must not return partial text")


def test_main_responses_error_event_preserves_retry_diagnostics() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="error",
                error=SimpleNamespace(
                    message="service temporarily unavailable",
                    code="gateway_busy",
                    type="server_error",
                    status_code=503,
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert len(events) == 1
    assert events[0].type == StreamEventType.ERROR
    assert events[0].content == (
        "Responses API error: service temporarily unavailable"
    )
    assert events[0].raw["status_code"] == 503
    assert events[0].raw["provider_error_code"] == "gateway_busy"
    assert events[0].raw["provider_error_schema_type"] == "server_error"
    assert events[0].raw["provider_error_type"] == "network"
    assert events[0].raw["provider_error_message"] == (
        "service temporarily unavailable"
    )


def test_main_responses_top_level_error_fields_are_not_dropped() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="error",
                message="gateway overloaded",
                code="SERVICE_BUSY",
                status_code=503,
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert events[0].content == "Responses API error: gateway overloaded"
    assert events[0].raw["status_code"] == 503
    assert events[0].raw["provider_error_code"] == "SERVICE_BUSY"
    assert events[0].raw["provider_error_type"] == "network"


def test_main_responses_closes_provider_stream_on_terminal_error() -> None:
    responses = _ResponsesCreate(
        [SimpleNamespace(type="error", error=SimpleNamespace(message="provider failed"))]
    )
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            model="gpt-5.4",
            wire_api="responses",
        ),
        http_client=responses,
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert responses.last_response is not None
    assert responses.last_response.closed is True


def test_main_responses_max_output_incomplete_is_a_recoverable_done() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    status="incomplete",
                    incomplete_details=SimpleNamespace(reason="max_output_tokens"),
                    usage=SimpleNamespace(input_tokens=7, output_tokens=9),
                    output_text="partial answer",
                    output=[],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [event.type for event in events] == [
        StreamEventType.TEXT_CHUNK,
        StreamEventType.DONE,
    ]
    assert events[0].content == "partial answer"
    assert events[-1].finish_reason == "max_output_tokens"
    assert events[-1].usage.input_tokens == 7
    assert events[-1].usage.output_tokens == 9


def test_main_responses_terminal_text_does_not_duplicate_anonymous_stream() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                delta="same text",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="reasoning",
                            id="reasoning-before-message",
                            summary=[],
                        ),
                        SimpleNamespace(
                            type="message",
                            id="provider-assigned-message-id",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="same text")
                            ],
                        ),
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    text_events = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    assert [event.content for event in text_events] == ["same text"]
    assert "text_reconciliation_mismatches" not in events[-1].raw


def test_main_responses_terminal_text_recovers_only_missing_suffix() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                delta="first and sec",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-one",
                            phase="commentary",
                            content=[
                                SimpleNamespace(type="output_text", text="first ")
                            ],
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-two",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(
                                    type="output_text",
                                    text="and second",
                                )
                            ],
                        ),
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    text_events = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    assert [event.content for event in text_events] == ["first and sec", "ond"]
    assert text_events[-1].item_id == "message-two"
    assert text_events[-1].content_index == 0
    assert text_events[-1].phase == "final_answer"


def test_main_responses_terminal_text_conflict_is_not_blindly_appended() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                delta="streamed answer",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-final",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(
                                    type="output_text",
                                    text="different terminal answer",
                                )
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    text_events = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    assert [event.content for event in text_events] == ["streamed answer"]
    assert events[-1].raw["text_reconciliation_mismatches"] == [
        {
            "scope": "response.completed",
            "streamed_chars": len("streamed answer"),
            "terminal_chars": len("different terminal answer"),
            "terminal_segments": 1,
        }
    ]


def test_main_responses_terminal_text_preserves_repeated_messages_when_unstreamed() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-one",
                            phase="commentary",
                            content=[SimpleNamespace(type="output_text", text="same")],
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-two",
                            phase="final_answer",
                            content=[SimpleNamespace(type="output_text", text="same")],
                        ),
                    ],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    text_events = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    assert [event.content for event in text_events] == ["same", "same"]
    assert [event.item_id for event in text_events] == ["message-one", "message-two"]
    assert [event.phase for event in text_events] == ["commentary", "final_answer"]


def test_main_responses_projects_streamed_refusal_once_as_informative_text() -> None:
    refusal = "I can’t help with that request."
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="message",
                    id="refusal-message",
                    phase="final_answer",
                ),
            ),
            SimpleNamespace(
                type="response.refusal.delta",
                item_id="refusal-message",
                output_index=0,
                content_index=0,
                delta=refusal,
            ),
            SimpleNamespace(
                type="response.refusal.done",
                item_id="refusal-message",
                output_index=0,
                content_index=0,
                refusal=refusal,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=5),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="refusal-message",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="refusal", refusal=refusal)
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    text_events = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    assert [event.content for event in text_events] == [refusal]
    assert text_events[0].phase == "final_answer"
    assert text_events[0].content_kind == "refusal"
    assert text_events[0].raw["provider_refusal"] is True


def test_main_responses_recovers_terminal_only_refusal_with_item_identity() -> None:
    refusal = "I’m unable to provide that content."
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=5),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="terminal-refusal",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="refusal", refusal=refusal)
                            ],
                        )
                    ],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    text_event = next(
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    )
    assert text_event.content == refusal
    assert text_event.item_id == "terminal-refusal"
    assert text_event.phase == "final_answer"
    assert text_event.content_kind == "refusal"
    assert text_event.raw == {
        "provider": "openai_responses",
        "message_phase": "final_answer",
        "recovered_from": "response.completed",
        "provider_refusal": True,
    }


def test_main_responses_preserves_incremental_url_citation_event() -> None:
    citation = SimpleNamespace(
        type="url_citation",
        url="https://example.test/source",
        title="Example source",
        start_index=0,
        end_index=6,
    )
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                item_id="message-cited",
                output_index=0,
                content_index=0,
                delta="Answer",
            ),
            SimpleNamespace(
                type="response.output_text.annotation.added",
                item_id="message-cited",
                output_index=0,
                content_index=0,
                annotation_index=0,
                annotation=citation,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-cited",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="Answer")
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert events[-1].raw["citations"] == [
        {
            "url": "https://example.test/source",
            "title": "Example source",
            "range": [0, 6],
        }
    ]


def test_main_responses_drops_duplicate_sequence_number_before_projection() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=1,
                item_id="message-1",
                output_index=0,
                content_index=0,
                delta="A",
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=1,
                item_id="message-1",
                output_index=0,
                content_index=0,
                delta="A",
            ),
            SimpleNamespace(
                type="response.completed",
                sequence_number=2,
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[SimpleNamespace(type="output_text", text="A")],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ] == ["A"]
    assert events[-1].raw["dropped_sequence_events"] == [
        {
            "event_type": "response.output_text.delta",
            "sequence_number": 1,
            "last_sequence_number": 1,
        }
    ]


def test_main_responses_records_sequence_gap_and_recovers_terminal_suffix() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=4,
                delta="A",
            ),
            SimpleNamespace(
                type="response.completed",
                sequence_number=6,
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-final",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="ABC")
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ] == ["A", "BC"]
    assert events[-1].raw["sequence_gaps"] == [
        {
            "event_type": "response.completed",
            "expected_sequence_number": 5,
            "received_sequence_number": 6,
        }
    ]


def test_main_responses_ignores_frames_after_first_terminal_event() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                sequence_number=0,
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-final",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="done")
                            ],
                        )
                    ],
                ),
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=1,
                item_id="message-final",
                output_index=0,
                content_index=0,
                delta="late",
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ] == ["done"]
    assert events[-1].raw["post_terminal_events"] == [
        {
            "event_type": "response.output_text.delta",
            "sequence_number": 1,
        }
    ]


def test_main_responses_recovers_terminal_only_reasoning_summary() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="reasoning",
                            id="reasoning-1",
                            summary=[
                                SimpleNamespace(
                                    type="summary_text",
                                    text="Checked the relevant paths.",
                                )
                            ],
                            encrypted_content="opaque",
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        ),
                    ],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    thinking = [
        event for event in events if event.type == StreamEventType.THINKING_CHUNK
    ]
    assert [event.content for event in thinking] == [
        "Checked the relevant paths."
    ]
    assert thinking[0].item_id == "reasoning-1"
    assert thinking[0].content_index == 0
    assert thinking[0].raw == {
        "provider_reasoning_type": "reasoning_summary_text",
        "recovered_from": "response.completed",
    }


def test_main_responses_recovers_only_missing_reasoning_summary_suffix() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning-1",
                summary_index=0,
                delta="Checked the ",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="reasoning",
                            id="reasoning-1",
                            summary=[
                                SimpleNamespace(
                                    type="summary_text",
                                    text="Checked the relevant paths.",
                                )
                            ],
                            encrypted_content="opaque",
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        ),
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.THINKING_CHUNK
    ] == ["Checked the ", "relevant paths."]


def test_main_responses_hides_raw_reasoning_text_at_adapter_boundary() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.reasoning_text.delta",
                item_id="reasoning-raw",
                output_index=0,
                content_index=0,
                delta="provider-private reasoning",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event for event in events if event.type == StreamEventType.THINKING_CHUNK
    ] == []
    done = next(event for event in events if event.type == StreamEventType.DONE)
    reasoning_timeline = next(
        entry
        for entry in done.raw["provider_timeline"]
        if entry["event"] == "response.reasoning_text.delta"
    )
    assert reasoning_timeline == {
        "event": "response.reasoning_text.delta",
        "output_index": 0,
        "content_index": 0,
        "item_id": "reasoning-raw",
        "delta_chars": len("provider-private reasoning"),
    }
    assert "provider-private reasoning" not in str(done.raw)


def test_main_responses_non_length_incomplete_remains_an_error() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.incomplete",
                response=SimpleNamespace(
                    status="incomplete",
                    incomplete_details=SimpleNamespace(reason="content_filter"),
                    output=[],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert events[0].content == (
        "Incomplete response returned, reason: content_filter"
    )


def test_main_responses_recovers_final_tool_call_without_done_delta() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            type="function_call",
                            id="fc-terminal",
                            call_id="call-terminal",
                            name="run_command",
                            arguments='{"command":"pwd"}',
                            status="completed",
                        )
                    ],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="run")]))
    )

    tool_event = next(
        event
        for event in events
        if event.type == StreamEventType.TOOL_CALL and event.tool_calls_final
    )
    assert tool_event.tool_calls == [
        ToolCallEvent(
            id="call-terminal",
            name="run_command",
            arguments={"command": "pwd"},
        )
    ]
    assert events[-1].type == StreamEventType.DONE


def test_main_responses_reconciles_content_part_events_without_duplicate_text() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.content_part.added",
                item_id="message-1",
                output_index=0,
                content_index=0,
                part=SimpleNamespace(type="output_text", text="Hello"),
            ),
            SimpleNamespace(
                type="response.content_part.done",
                item_id="message-1",
                output_index=0,
                content_index=0,
                part=SimpleNamespace(type="output_text", text="Hello world"),
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(
                                    type="output_text",
                                    text="Hello world",
                                )
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ] == ["Hello", " world"]


def test_main_responses_reconciles_reasoning_done_events_once() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning-1",
                output_index=0,
                summary_index=0,
                delta="Checked",
            ),
            SimpleNamespace(
                type="response.reasoning_summary_text.done",
                item_id="reasoning-1",
                output_index=0,
                summary_index=0,
                text="Checked all paths",
            ),
            SimpleNamespace(
                type="response.reasoning_summary_part.done",
                item_id="reasoning-1",
                output_index=0,
                summary_index=0,
                part=SimpleNamespace(
                    type="summary_text",
                    text="Checked all paths",
                ),
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="reasoning",
                            id="reasoning-1",
                            summary=[
                                SimpleNamespace(
                                    type="summary_text",
                                    text="Checked all paths",
                                )
                            ],
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        ),
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.THINKING_CHUNK
    ] == ["Checked", " all paths"]


def test_main_responses_projects_hosted_tool_lifecycle_as_stable_activity() -> None:
    search_item = SimpleNamespace(
        type="web_search_call",
        id="search-1",
        status="in_progress",
    )
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=search_item,
            ),
            SimpleNamespace(
                type="response.web_search_call.searching",
                item_id="search-1",
                output_index=0,
            ),
            SimpleNamespace(
                type="response.web_search_call.completed",
                item_id="search-1",
                output_index=0,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            phase="final_answer",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="search")]))
    )

    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]
    assert {activity.id for activity in activities} == {"search-1"}
    assert [activity.status for activity in activities] == [
        "running",
        "completed",
    ]
    assert activities[-1].message == "Web search completed"


def test_main_responses_fails_closed_for_unsupported_executable_output_item() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="local_shell_call",
                    id="shell-1",
                    status="in_progress",
                    action=SimpleNamespace(
                        type="exec",
                        command=["do-not-run"],
                    ),
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="run")]))
    )

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert events[0].raw == {
        "provider": "openai_responses",
        "provider_error_type": "protocol",
        "error_type": "api",
        "event_type": "response.output_item.added",
        "output_item_type": "local_shell_call",
    }
    assert "do-not-run" not in str(events[0].raw)


def test_installed_openai_response_stream_union_is_explicitly_classified() -> None:
    from openai.types.responses import ResponseStreamEvent

    annotated_union = typing.get_args(ResponseStreamEvent)[0]
    event_classes = typing.get_args(annotated_union)
    installed_event_types = {
        typing.get_args(event_class.__annotations__["type"])[0]
        for event_class in event_classes
    }

    assert installed_event_types <= _OPENAI_RESPONSE_STREAM_EVENT_TYPES
    assert _OPENAI_RESPONSE_STREAM_EVENT_TYPES - installed_event_types == {
        "response.error"
    }


def test_main_responses_fails_closed_for_unknown_stream_event_without_payload_leak() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.future_event",
                payload="secret-provider-payload",
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert events[0].raw == {
        "provider": "openai_responses",
        "provider_error_type": "protocol",
        "error_type": "api",
        "event_type": "response.future_event",
        "protocol_error_code": "unknown_stream_event",
    }
    assert "secret-provider-payload" not in str(events[0].raw)


@pytest.mark.parametrize(
    ("event", "feature", "secret"),
    [
        (
            SimpleNamespace(
                type="response.audio.delta",
                delta="secret-audio-base64",
                sequence_number=1,
            ),
            "audio_output",
            "secret-audio-base64",
        ),
        (
            SimpleNamespace(
                type="response.custom_tool_call_input.done",
                item_id="custom-1",
                output_index=0,
                input="secret-custom-tool-input",
                sequence_number=1,
            ),
            "custom_tool_execution",
            "secret-custom-tool-input",
        ),
    ],
)
def test_main_responses_fails_closed_for_unprojectable_stream_features(
    event: object,
    feature: str,
    secret: str,
) -> None:
    adapter, _responses = _adapter([event])

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [item.type for item in events] == [StreamEventType.ERROR]
    assert events[0].raw == {
        "provider": "openai_responses",
        "provider_error_type": "protocol",
        "error_type": "api",
        "event_type": getattr(event, "type"),
        "protocol_error_code": "unsupported_stream_feature",
        "feature": feature,
    }
    assert secret not in str(events[0].raw)


def test_main_responses_projects_code_and_mcp_preparation_without_content_leak() -> None:
    code = "print('secret-code')"
    arguments = '{"query":"secret-query"}'
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="code_interpreter_call",
                    id="code-1",
                    status="in_progress",
                ),
            ),
            SimpleNamespace(
                type="response.code_interpreter_call_code.delta",
                item_id="code-1",
                output_index=0,
                delta=code,
            ),
            SimpleNamespace(
                type="response.code_interpreter_call_code.done",
                item_id="code-1",
                output_index=0,
                code=code,
            ),
            SimpleNamespace(
                type="response.code_interpreter_call.interpreting",
                item_id="code-1",
                output_index=0,
            ),
            SimpleNamespace(
                type="response.code_interpreter_call.completed",
                item_id="code-1",
                output_index=0,
            ),
            SimpleNamespace(
                type="response.output_item.added",
                output_index=1,
                item=SimpleNamespace(
                    type="mcp_call",
                    id="mcp-1",
                    name="lookup",
                    server_label="safe-server",
                ),
            ),
            SimpleNamespace(
                type="response.mcp_call_arguments.delta",
                item_id="mcp-1",
                output_index=1,
                delta=arguments,
            ),
            SimpleNamespace(
                type="response.mcp_call_arguments.done",
                item_id="mcp-1",
                output_index=1,
                arguments=arguments,
            ),
            SimpleNamespace(
                type="response.mcp_call.in_progress",
                item_id="mcp-1",
                output_index=1,
            ),
            SimpleNamespace(
                type="response.mcp_call.completed",
                item_id="mcp-1",
                output_index=1,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="work")]))
    )

    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]
    assert {activity.id for activity in activities} == {"code-1", "mcp-1"}
    assert next(
        activity
        for activity in activities
        if activity.id == "code-1" and activity.message == "Provider code prepared"
    ).detail == f"Code: {len(code)} characters"
    assert next(
        activity
        for activity in activities
        if activity.id == "mcp-1" and activity.message == "MCP tool call prepared: lookup"
    ).detail == (
        f"Server: safe-server · Tool: lookup · Arguments: {len(arguments)} characters"
    )
    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert code not in str(done.raw)
    assert arguments not in str(done.raw)
    assert any(
        item.get("code_chars") == len(code)
        for item in done.raw["provider_timeline"]
    )
    assert any(
        item.get("arguments_chars") == len(arguments)
        for item in done.raw["provider_timeline"]
    )


def test_main_responses_projects_terminal_only_hosted_activities() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            type="web_search_call",
                            id="search-terminal",
                            status="completed",
                        ),
                        SimpleNamespace(
                            type="mcp_call",
                            id="mcp-terminal",
                            name="lookup",
                            server_label="server-a",
                            arguments='{"query":"must-not-project"}',
                            output="must-not-project-output",
                            error=None,
                        ),
                        SimpleNamespace(
                            type="mcp_list_tools",
                            id="mcp-list-terminal",
                            server_label="server-a",
                            tools=[
                                SimpleNamespace(name="one"),
                                SimpleNamespace(name="two"),
                            ],
                            error=None,
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-terminal",
                            content=[
                                SimpleNamespace(type="output_text", text="Done")
                            ],
                        ),
                    ],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="work")]))
    )

    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]
    assert [
        (activity.id, activity.status, activity.message) for activity in activities
    ] == [
        ("search-terminal", "completed", "Web search completed"),
        ("mcp-terminal", "completed", "MCP tool completed: lookup"),
        ("mcp-list-terminal", "completed", "MCP tools loaded — 2 tools"),
    ]
    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert "must-not-project" not in str(done.raw)


def test_main_responses_keeps_terminal_hosted_failure_with_final_answer() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            type="mcp_call",
                            id="mcp-failed",
                            name="lookup",
                            server_label="server-a",
                            arguments='{"secret":"must-not-project"}',
                            output=None,
                            error="provider-private-error-body",
                        ),
                        SimpleNamespace(
                            type="message",
                            id="message-after-failure",
                            content=[
                                SimpleNamespace(
                                    type="output_text",
                                    text="I could not use that source, but here is the answer.",
                                )
                            ],
                        ),
                    ],
                ),
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="work")]))
    )

    activity = next(
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    )
    assert (activity.id, activity.status, activity.message) == (
        "mcp-failed",
        "failed",
        "MCP tool failed: lookup",
    )
    assert "".join(
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK
    ) == "I could not use that source, but here is the answer."
    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert "must-not-project" not in str(done.raw)
    assert "provider-private-error-body" not in str(done.raw)


def test_main_responses_deduplicates_partial_and_terminal_images() -> None:
    final_image = "final-image-base64"
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=SimpleNamespace(
                    type="image_generation_call",
                    id="image-1",
                    status="in_progress",
                ),
            ),
            SimpleNamespace(
                type="response.image_generation_call.partial_image",
                item_id="image-1",
                output_index=0,
                partial_image_index=0,
                partial_image_b64="obsolete-preview-base64",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=2, output_tokens=3),
                    output=[
                        SimpleNamespace(
                            type="image_generation_call",
                            id="image-1",
                            status="completed",
                            result=final_image,
                        ),
                        SimpleNamespace(
                            type="message",
                            id="image-message",
                            content=[
                                SimpleNamespace(
                                    type="output_image",
                                    image_data=final_image,
                                )
                            ],
                        ),
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="draw")]))
    )

    image_events = [
        event for event in events if event.type == StreamEventType.IMAGE_CHUNK
    ]
    assert [event.image_data for event in image_events] == [final_image]
    assert "obsolete-preview-base64" not in str(events[-1].raw)


def test_main_responses_reconciles_out_of_order_text_with_sequence_gap_and_replay() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.content_part.done",
                sequence_number=1,
                item_id="message-1",
                output_index=0,
                content_index=0,
                part=SimpleNamespace(type="output_text", text="Hello world"),
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=3,
                item_id="message-1",
                output_index=0,
                content_index=0,
                delta="world",
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                sequence_number=3,
                item_id="message-1",
                output_index=0,
                content_index=0,
                delta="secret-replayed-frame",
            ),
            SimpleNamespace(
                type="response.output_text.done",
                sequence_number=4,
                item_id="message-1",
                output_index=0,
                content_index=0,
                text="Hello world",
            ),
            SimpleNamespace(
                type="response.completed",
                sequence_number=5,
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            content=[
                                SimpleNamespace(
                                    type="output_text",
                                    text="Hello world",
                                )
                            ],
                        )
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ] == ["Hello world"]
    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert done.raw["sequence_gaps"] == [
        {
            "event_type": "response.output_text.delta",
            "expected_sequence_number": 2,
            "received_sequence_number": 3,
        }
    ]
    assert done.raw["dropped_sequence_events"] == [
        {
            "event_type": "response.output_text.delta",
            "sequence_number": 3,
            "last_sequence_number": 3,
        }
    ]
    assert done.raw["late_text_events"] == [
        {
            "event_type": "response.output_text.delta",
            "item_key_hash": done.raw["late_text_events"][0]["item_key_hash"],
            "chars": len("world"),
        }
    ]
    assert "secret-replayed-frame" not in str(done.raw)


def test_main_responses_reconciles_snapshot_overlap_and_late_reasoning_delta() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.content_part.added",
                item_id="message-1",
                output_index=0,
                content_index=0,
                part=SimpleNamespace(type="output_text", text="Hello"),
            ),
            SimpleNamespace(
                type="response.output_text.delta",
                item_id="message-1",
                output_index=0,
                content_index=0,
                delta="Hello world",
            ),
            SimpleNamespace(
                type="response.reasoning_summary_part.done",
                item_id="reasoning-1",
                output_index=1,
                summary_index=0,
                part=SimpleNamespace(
                    type="summary_text",
                    text="Checked all paths",
                ),
            ),
            SimpleNamespace(
                type="response.reasoning_summary_text.delta",
                item_id="reasoning-1",
                output_index=1,
                summary_index=0,
                delta="all paths",
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[
                        SimpleNamespace(
                            type="message",
                            id="message-1",
                            content=[
                                SimpleNamespace(
                                    type="output_text",
                                    text="Hello world",
                                )
                            ],
                        ),
                        SimpleNamespace(
                            type="reasoning",
                            id="reasoning-1",
                            summary=[
                                SimpleNamespace(
                                    type="summary_text",
                                    text="Checked all paths",
                                )
                            ],
                        ),
                    ],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hello")]))
    )

    assert [
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ] == ["Hello", " world"]
    assert [
        event.content
        for event in events
        if event.type == StreamEventType.THINKING_CHUNK and event.content
    ] == ["Checked all paths"]
    done = events[-1]
    assert done.raw["text_delta_overlaps"][0]["delta_chars"] == len(
        "Hello world"
    )
    assert done.raw["text_delta_overlaps"][0]["appended_chars"] == len(" world")
    assert done.raw["late_reasoning_events"][0]["chars"] == len("all paths")


def test_main_responses_finalizes_function_call_from_output_item_done() -> None:
    function_item_start = SimpleNamespace(
        type="function_call",
        id="fc-1",
        call_id="call-1",
        name="run_command",
        arguments="",
        status="in_progress",
    )
    function_item_done = SimpleNamespace(
        type="function_call",
        id="fc-1",
        call_id="call-1",
        name="run_command",
        arguments='{"command":"pwd"}',
        status="completed",
    )
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=function_item_start,
            ),
            SimpleNamespace(
                type="response.output_item.done",
                output_index=0,
                item=function_item_done,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=[function_item_done],
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="run")]))
    )

    tool_events = [
        event for event in events if event.type == StreamEventType.TOOL_CALL
    ]
    assert [event.tool_calls_final for event in tool_events] == [False, True]
    assert tool_events[-1].tool_calls == [
        ToolCallEvent(
            id="call-1",
            name="run_command",
            arguments={"command": "pwd"},
        )
    ]
    assert events[-1].type == StreamEventType.DONE


@pytest.mark.parametrize(
    ("terminal_arguments", "protocol_error_code"),
    [
        (None, "terminal_function_call_missing"),
        ('{"command":"different"}', "terminal_function_call_mismatch"),
    ],
)
def test_main_responses_never_executes_streamed_call_denied_by_terminal_response(
    terminal_arguments: str | None,
    protocol_error_code: str,
) -> None:
    streamed_arguments = '{"command":"must-not-run"}'
    terminal_output = []
    if terminal_arguments is not None:
        terminal_output.append(
            SimpleNamespace(
                type="function_call",
                id="fc-1",
                call_id="call-1",
                name="run_command",
                arguments=terminal_arguments,
                status="completed",
            )
        )
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.function_call_arguments.done",
                item_id="fc-1",
                call_id="call-1",
                name="run_command",
                arguments=streamed_arguments,
            ),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(
                    status="completed",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                    output=terminal_output,
                ),
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="run")]))
    )

    assert [
        event.tool_calls_final
        for event in events
        if event.type == StreamEventType.TOOL_CALL
    ] == [False]
    error = events[-1]
    assert error.type == StreamEventType.ERROR
    assert error.raw["protocol_error_code"] == protocol_error_code
    assert streamed_arguments not in str(error.raw)
    if terminal_arguments:
        assert terminal_arguments not in str(error.raw)


def test_main_responses_rejects_non_object_function_arguments() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.function_call_arguments.done",
                item_id="fc-invalid",
                call_id="call-invalid",
                name="run_command",
                arguments='["must-not-run"]',
            )
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="run")]))
    )

    assert [event.type for event in events] == [StreamEventType.ERROR]
    assert events[0].raw["protocol_error_code"] == "invalid_function_arguments"
    assert "must-not-run" not in str(events[0].raw)


def test_main_responses_rejects_function_delta_after_done() -> None:
    adapter, _responses = _adapter(
        [
            SimpleNamespace(
                type="response.function_call_arguments.done",
                item_id="fc-late",
                call_id="call-late",
                name="run_command",
                arguments='{"command":"pwd"}',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                item_id="fc-late",
                call_id="call-late",
                delta='{"command":"must-not-append"}',
            ),
        ]
    )

    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="run")]))
    )

    assert [
        event.tool_calls_final
        for event in events
        if event.type == StreamEventType.TOOL_CALL
    ] == [False]
    error = events[-1]
    assert error.type == StreamEventType.ERROR
    assert error.raw["protocol_error_code"] == "function_delta_after_done"
    assert "must-not-append" not in str(error.raw)


def test_response_completed_without_a_response_object_is_refused() -> None:
    """A terminal frame with no response object must not become a clean DONE.

    ``saw_terminal_response_event`` used to be set before validating the
    payload, so the ``eof_without_terminal`` guard passed and the stream ended
    with a successful DONE carrying zero usage and an empty finish_reason -- a
    silently truncated turn. The unclassified-event branch already refuses to
    "silently turn an unhandled event into a successful response"; match it.
    """

    adapter, _ = _adapter([SimpleNamespace(type="response.completed", response=None)])
    events = asyncio.run(
        _collect(adapter.stream_chat([LLMMessage(role="user", content="hi")], tools=[]))
    )

    assert not any(event.type == StreamEventType.DONE for event in events)
    errors = [event for event in events if event.type == StreamEventType.ERROR]
    assert len(errors) == 1
    assert errors[0].raw["protocol_error_code"] == "terminal_event_without_response"
    assert errors[0].raw["provider_error_type"] == "protocol"

