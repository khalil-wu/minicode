"""Tests for provider-native raw stream_event passthrough (citations, usage, finish_reason).

Verifies that:
1. _extract_url_citations extracts url_citation annotations from DONE events
2. The raw dict with citations flows through StreamEvent.DONE
"""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from backend.config import LLMSettings
from backend.llm.base import StreamEvent, StreamEventType, UsageInfo
from backend.llm.base import LLMMessage
from backend.llm.openai_adapter import _extract_url_citations
from backend.llm.openai_adapter import _extract_response_output_items
from backend.llm.openai_adapter import _safe_request_summary
from backend.llm.openai_adapter import OpenAIAdapter


def _annotation(url: str, title: str, start: int = 0, end: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        type="url_citation",
        url=url,
        title=title,
        start_index=start,
        end_index=end,
    )


async def _collect(stream):
    return [event async for event in stream]


def test_extract_url_citations_from_annotations():
    event = SimpleNamespace(
        annotations=[
            _annotation("https://example.com/a", "Source A", 0, 5),
            _annotation("https://example.com/b", "Source B", 6, 10),
        ]
    )
    citations = _extract_url_citations(event)
    assert len(citations) == 2
    assert citations[0]["url"] == "https://example.com/a"
    assert citations[0]["title"] == "Source A"
    assert citations[0]["range"] == [0, 5]
    assert citations[1]["url"] == "https://example.com/b"


def test_extract_url_citations_skips_non_url_annotations():
    event = SimpleNamespace(
        annotations=[
            SimpleNamespace(type="other_annotation", url="https://ignored.example", title="Ignored"),
            _annotation("https://example.com/real", "Real Source"),
        ]
    )
    citations = _extract_url_citations(event)
    assert len(citations) == 1
    assert citations[0]["url"] == "https://example.com/real"


def test_extract_url_citations_empty():
    assert _extract_url_citations(SimpleNamespace(annotations=None)) == []
    assert _extract_url_citations(SimpleNamespace(annotations=[])) == []


def test_extract_url_citations_skips_empty_urls():
    event = SimpleNamespace(
        annotations=[
            _annotation("", "No URL"),
            _annotation("https://example.com/valid", "Valid"),
        ]
    )
    citations = _extract_url_citations(event)
    assert len(citations) == 1
    assert citations[0]["url"] == "https://example.com/valid"


def test_raw_done_carries_citations_field():
    """The DONE event's raw dict should be able to carry citations."""
    raw_done = {
        "provider": "openai_responses",
        "finish_reason": "stop",
        "citations": [
            {"url": "https://example.com", "title": "Test", "range": [0, 5]},
        ],
    }
    event = StreamEvent(
        type=StreamEventType.DONE,
        usage=UsageInfo(),
        finish_reason="stop",
        raw=raw_done,
    )
    assert event.raw["citations"] is not None
    assert len(event.raw["citations"]) == 1
    assert event.raw["citations"][0]["url"] == "https://example.com"
    assert event.finish_reason == "stop"
    assert event.raw["provider"] == "openai_responses"
    assert event.raw["finish_reason"] == "stop"


def test_raw_done_without_citations():
    """DONE events without citations should have empty or missing citations in raw."""
    raw_done = {"provider": "openai_chat_completions", "finish_reason": "stop"}
    event = StreamEvent(
        type=StreamEventType.DONE,
        usage=UsageInfo(),
        finish_reason="stop",
        raw=raw_done,
    )
    assert "citations" not in event.raw
    assert event.raw["finish_reason"] == "stop"
    assert event.usage.input_tokens == 0
    assert event.usage.output_tokens == 0


def test_raw_usage_metadata_preserved():
    """Usage details from the provider should be in raw_done.usage."""
    raw_done = {
        "provider": "openai_responses",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cached_prompt_tokens": 10,
            "reasoning_output_tokens": 12,
        },
        "finish_reason": "stop",
    }
    event = StreamEvent(
        type=StreamEventType.DONE,
        usage=UsageInfo(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=10,
            reasoning_output_tokens=12,
        ),
        finish_reason="stop",
        raw=raw_done,
    )
    assert event.raw["usage"]["input_tokens"] == 100
    assert event.raw["usage"]["output_tokens"] == 50
    assert event.usage.input_tokens == 100
    assert event.usage.output_tokens == 50
    assert event.usage.cache_read_input_tokens == 10
    assert event.usage.reasoning_output_tokens == 12
    assert event.finish_reason == "stop"


def test_extract_response_output_items_keeps_safe_structure_only():
    response = SimpleNamespace(
        output=[
            SimpleNamespace(
                type="reasoning",
                id="rs_1",
                summary=[],
                encrypted_content="opaque-secret-reasoning",
            ),
            SimpleNamespace(
                type="message",
                id="msg_1",
                role="assistant",
                phase="commentary",
                content=[SimpleNamespace(type="output_text", text="do not copy me")],
            ),
            SimpleNamespace(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="read_file",
                arguments='{"file_path":"README.md"}',
            ),
            SimpleNamespace(
                type="web_search_call",
                id="ws_1",
                status="completed",
                action=SimpleNamespace(type="search", query="MiniCode"),
            ),
        ],
    )

    items = _extract_response_output_items(response)

    assert items == [
        {
            "type": "reasoning",
            "index": 0,
            "id": "rs_1",
            "summary_count": 0,
            "has_encrypted_content": True,
        },
        {
            "type": "message",
            "index": 1,
            "id": "msg_1",
            "role": "assistant",
            "phase": "commentary",
            "content_types": ["output_text"],
        },
        {
            "type": "function_call",
            "index": 2,
            "id": "fc_1",
            "call_id": "call_1",
            "name": "read_file",
            "arguments_chars": 25,
        },
        {
            "type": "web_search_call",
            "index": 3,
            "id": "ws_1",
            "status": "completed",
            "action_type": "search",
        },
    ]
    assert "opaque-secret-reasoning" not in str(items)
    assert "do not copy me" not in str(items)


def test_safe_request_summary_keeps_protocol_params_only():
    summary = _safe_request_summary(
        model="gpt-5.4",
        wire_api="responses",
        tools=[{
            "type": "function",
            "function": {
                "name": "safe_tool",
                "description": "do not copy this description",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "private path hint"}},
                },
            },
        }],
        request_params={
            "stream": True,
            "parallel_tool_calls": False,
            "seed": 29,
            "tool_choice": "auto",
            "messages": [{"role": "user", "content": "do not copy"}],
            "metadata": {"api_key": "secret"},
        },
    )

    assert summary["request_params"] == {
        "stream": True,
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "seed": 29,
    }
    assert summary["tool_names"] == ["safe_tool"]
    assert len(summary["tool_schema_hashes"]["safe_tool"]) == 12
    assert "do not copy" not in str(summary)
    assert "private path hint" not in str(summary)
    assert "secret" not in str(summary)


def test_responses_raw_done_includes_safe_provider_timeline():
    response_id = "resp_test_boundary"
    response_id_hash = hashlib.sha256(response_id.encode("utf-8")).hexdigest()[:12]

    async def fake_stream():
        yield SimpleNamespace(
            type="response.created",
            response=SimpleNamespace(
                id=response_id,
                status="in_progress",
                usage=None,
                output=[],
            ),
        )
        yield SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(
                type="function_call",
                id="fc_1",
                call_id="call_1",
                name="shell_command",
                arguments='{"command":"do not copy"}',
            ),
        )
        yield SimpleNamespace(
            type="response.function_call_arguments.delta",
            item_id="fc_1",
            call_id="call_1",
            delta='{"command":"do not copy"}',
        )
        yield SimpleNamespace(
            type="response.function_call_arguments.done",
            item_id="fc_1",
            call_id="call_1",
            name="shell_command",
            arguments='{"command":"do not copy"}',
        )
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                id=response_id,
                status="completed",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                output=[
                    SimpleNamespace(
                        type="function_call",
                        id="fc_1",
                        call_id="call_1",
                        name="shell_command",
                        arguments='{"command":"do not copy"}',
                        status="completed",
                    )
                ],
            ),
        )

    adapter = OpenAIAdapter(
        LLMSettings(api_key="test", model="gpt-5.4", wire_api="responses"),
    )

    async def fake_create(_kwargs, **_options):
        return fake_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    events = asyncio.run(_collect(adapter.stream_chat([LLMMessage(role="user", content="hello")])))
    done = [event for event in events if event.type == StreamEventType.DONE][0]
    timeline = done.raw["provider_timeline"]

    assert timeline == [
        {
            "event": "response.created",
            "response_id_hash": response_id_hash,
            "status": "in_progress",
            "finish_reason": "in_progress",
            "output_items_len": 0,
            "usage_present": False,
        },
        {
            "event": "response.output_item.added",
            "output_index": 0,
            "item_type": "function_call",
            "item_id": "fc_1",
            "call_id": "call_1",
            "name": "shell_command",
        },
        {
            "event": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "call_id": "call_1",
            "delta_chars": 25,
        },
        {
            "event": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "call_id": "call_1",
            "arguments_chars": 25,
        },
        {
            "event": "response.completed",
            "response_id_hash": response_id_hash,
            "status": "completed",
            "finish_reason": "completed",
            "output_items_len": 1,
            "usage_present": True,
        },
    ]
    assert "do not copy" not in str(timeline)
    assert response_id not in str(timeline)
    assert response_id_hash in str(timeline)


def test_responses_stream_preserves_citations_when_completed_adds_usage():
    async def fake_stream():
        yield SimpleNamespace(type="response.output_text.delta", delta="Hello [1]")
        yield SimpleNamespace(
            type="response.output_text.done",
            annotations=[_annotation("https://example.com/source", "Source", 6, 9)],
        )
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(input_tokens=11, output_tokens=3, reasoning_output_tokens=2),
                output=[
                    SimpleNamespace(
                        type="reasoning",
                        id="rs_done",
                        summary=[],
                        encrypted_content="opaque",
                    ),
                    SimpleNamespace(
                        type="message",
                        id="msg_done",
                        role="assistant",
                        phase="final_answer",
                        content=[SimpleNamespace(type="output_text", text="Hello [1]")],
                    ),
                ],
            ),
        )

    adapter = OpenAIAdapter(
        LLMSettings(api_key="test", model="gpt-5.4", wire_api="responses"),
    )

    async def fake_create(_kwargs, **_options):
        return fake_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    async def collect():
        return [
            event
            async for event in adapter.stream_chat([LLMMessage(role="user", content="hello")])
        ]

    events = asyncio.run(collect())
    text = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    done = [event for event in events if event.type == StreamEventType.DONE]

    assert [event.content for event in text] == ["Hello [1]"]
    assert len(done) == 1
    assert done[0].raw["citations"] == [
        {"url": "https://example.com/source", "title": "Source", "range": [6, 9]},
    ]
    assert done[0].raw["usage"] == {
        "input_tokens": 11,
        "output_tokens": 3,
        "reasoning_output_tokens": 2,
    }
    assert done[0].raw["model"] == "gpt-5.4"
    assert done[0].raw["request_summary"] == {
        "model": "gpt-5.4",
        "wire_api": "responses",
        "metadata_keys": [],
        "prompt_cache_key_present": False,
        "prompt_cache_key_hash": "",
        "request_params": {
            "stream": True,
            "store": False,
            "include": ["reasoning.encrypted_content"],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "medium"},
        },
        "request_param_keys": [
            "include",
            "model",
            "parallel_tool_calls",
            "reasoning",
            "store",
            "stream",
            "tool_choice",
        ],
        "turn_aborted_marker_present": False,
        "instructions_len": 0,
        "instructions_hash": "",
        "instructions_full_hash": "",
        "instructions_sent_len": 0,
        "tools_len": 0,
        "tools_chars": 0,
        "tools_hash": "",
        "tool_names": [],
        "tool_schema_hashes": {},
        "largest_tools": [],
        "input_items_len": 1,
        "input_items_sent_len": 1,
        "input_items_logical_len": 1,
        "input_chars": 33,
        "largest_input_items": [{"index": 0, "type": "user", "role": "user", "content_hash": "5aa762ae383f", "chars": 33}],
        "duplicate_input_content": [],
        "input_item_counts": {"user": 1},
    }
    assert done[0].raw["output_items"] == [
        {
            "type": "reasoning",
            "index": 0,
            "id": "rs_done",
            "summary_count": 0,
            "has_encrypted_content": True,
        },
        {
            "type": "message",
            "index": 1,
            "id": "msg_done",
            "role": "assistant",
            "phase": "final_answer",
            "content_types": ["output_text"],
        },
    ]
    assert "opaque" not in str(done[0].raw["output_items"])
    assert done[0].raw["safety"] == {
        "redacted_prompt": True,
        "has_encrypted_reasoning": True,
    }
    assert done[0].usage.reasoning_output_tokens == 2


def test_responses_stream_text_chunks_carry_message_phase():
    async def fake_stream():
        yield SimpleNamespace(
            type="response.output_item.added",
            output_index=0,
            item=SimpleNamespace(
                type="message",
                id="msg_final",
                role="assistant",
                phase="final_answer",
            ),
        )
        yield SimpleNamespace(
            type="response.output_text.delta",
            item_id="msg_final",
            output_index=0,
            delta="Final from provider.",
        )
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(input_tokens=7, output_tokens=4),
                output=[],
            ),
        )

    adapter = OpenAIAdapter(
        LLMSettings(api_key="test", model="gpt-5.4", wire_api="responses"),
    )

    async def fake_create(_kwargs, **_options):
        return fake_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    events = asyncio.run(_collect(adapter.stream_chat([LLMMessage(role="user", content="hello")])))
    text = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.content
    ]
    lifecycle = [
        event
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK and event.lifecycle
    ]

    assert [event.content for event in text] == ["Final from provider."]
    assert text[0].phase == "final_answer"
    assert text[0].raw["message_phase"] == "final_answer"
    assert [(event.item_id, event.lifecycle) for event in lifecycle] == [
        ("msg_final", "start"),
        ("msg_final", "delta"),
    ]


def test_responses_raw_done_request_summary_never_copies_prompt_or_secrets():
    async def fake_stream():
        yield SimpleNamespace(
            type="response.completed",
            response=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                output=[],
            ),
        )

    adapter = OpenAIAdapter(
        LLMSettings(api_key="test", model="gpt-5.4", wire_api="responses"),
    )

    async def fake_create(_kwargs, **_options):
        return fake_stream()

    adapter._create_responses_request = fake_create  # type: ignore[method-assign]

    events = asyncio.run(
        _collect(
            adapter.stream_chat(
                [
                    LLMMessage(role="system", content="VERY PRIVATE SYSTEM PROMPT"),
                    LLMMessage(role="user", content="hello"),
                ],
                tools=[{
                    "type": "function",
                    "function": {
                        "name": "secret_tool",
                        "description": "do not leak",
                        "parameters": {"type": "object"},
                    },
                }],
                metadata={"turn_id": "turn-1", "api_key": "secret"},
            )
        )
    )

    done = [event for event in events if event.type == StreamEventType.DONE][0]
    raw_text = str(done.raw)
    summary = done.raw["request_summary"]
    assert summary["instructions_len"] == len("VERY PRIVATE SYSTEM PROMPT")
    assert len(summary["instructions_hash"]) == 12
    assert len(summary["instructions_full_hash"]) == 12
    assert summary["tools_len"] == 1
    assert len(summary["tools_hash"]) == 12
    assert summary["tool_names"] == ["secret_tool"]
    assert len(summary["tool_schema_hashes"]["secret_tool"]) == 12
    assert summary["metadata_keys"] == ["turn_id"]
    assert summary["prompt_cache_key_present"] is False
    assert summary["prompt_cache_key_hash"] == ""
    assert summary["turn_aborted_marker_present"] is False
    assert summary["request_params"] == {
        "stream": True,
        "store": False,
        "include": ["reasoning.encrypted_content"],
        "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "medium"},
    }
    assert "metadata" not in summary
    assert "VERY PRIVATE SYSTEM PROMPT" not in raw_text
    assert "minicode-" not in raw_text
    assert "api_key" not in raw_text
    assert "hidden" not in raw_text
    assert "do not leak" not in raw_text


def test_safe_request_summary_flags_turn_aborted_without_copying_text():
    summary = _safe_request_summary(
        model="gpt-5.4",
        wire_api="responses",
        input_items=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<turn_aborted>\nThe user interrupted the previous turn on purpose.\n</turn_aborted>",
                    }
                ],
            }
        ],
    )

    assert summary["turn_aborted_marker_present"] is True
    assert "interrupted the previous turn" not in str(summary)
