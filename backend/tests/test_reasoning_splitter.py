"""Tests for routing chain-of-thought out of the answer text.

GLM / DeepSeek-R1 / Qwen emit reasoning either via a separate `reasoning_content`
field or inline in `delta.content` wrapped in `<think>...</think>` (and sometimes
leak `<|im_start|>`/`<|endoftext|>` special tokens). The adapter must split the
inline form so reasoning reaches THINKING_CHUNK and the answer reaches TEXT_CHUNK,
and `_scrub_thinking_tags` must strip any residual tags/markers/tokens.
"""

from backend.agent.stream_sanitizer import (
    ThinkingStreamSanitizer as _ThinkingStreamSanitizer,
    scrub_thinking_tags as _scrub_thinking_tags,
)
from backend.llm.base import StreamEventType
from backend.llm.openai_adapter import (
    _ReasoningSplitter,
    _splitter_events,
    _strip_special_tokens,
)


def _feed_all(splitter, deltas):
    """Feed all deltas then flush, returning concatenated (text, reasoning)."""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    for delta in deltas:
        for kind, segment in splitter.feed(delta):
            (reasoning_parts if kind == "reasoning" else text_parts).append(segment)
    for kind, segment in splitter.flush():
        (reasoning_parts if kind == "reasoning" else text_parts).append(segment)
    return "".join(text_parts), "".join(reasoning_parts)


# ---------------------------------------------------------------- _ReasoningSplitter


def test_splitter_no_tags_passes_through_as_text():
    text, reasoning = _feed_all(_ReasoningSplitter(), ["Hello, world"])
    assert reasoning == ""
    assert text == "Hello, world"


def test_splitter_think_tag_split_across_deltas():
    deltas = ["<thi", "nk>reasoning here</th", "ink>answer"]
    text, reasoning = _feed_all(_ReasoningSplitter(), deltas)
    assert reasoning == "reasoning here"
    assert text == "answer"


def test_splitter_reasoning_then_answer_in_one_delta():
    text, reasoning = _feed_all(_ReasoningSplitter(), ["<think>rm</think>answer"])
    assert reasoning == "rm"
    assert text == "answer"


def test_splitter_multiple_think_blocks():
    deltas = ["<think>a</think>mid<think>b</think>"]
    text, reasoning = _feed_all(_ReasoningSplitter(), deltas)
    assert reasoning == "ab"
    assert text == "mid"


def test_splitter_flush_emits_held_remainder():
    # "abc" is shorter than the hold-back window, so it stays buffered until flush.
    splitter = _ReasoningSplitter()
    assert splitter.feed("abc") == []
    text, reasoning = _feed_all(splitter, [])
    assert text == "abc"
    assert reasoning == ""


def test_splitter_close_tag_split_across_deltas():
    deltas = ["<think>reasoning</thi", "nk>tail"]
    text, reasoning = _feed_all(_ReasoningSplitter(), deltas)
    assert reasoning == "reasoning"
    assert text == "tail"


# --------------------------------------------------------- _scrub_thinking_tags


def test_scrub_strips_think_block():
    assert _scrub_thinking_tags("Hello <think>secret</think> world") == "Hello  world"


def test_scrub_strips_think_only_block():
    assert _scrub_thinking_tags("<think>only reasoning</think>") == ""


def test_scrub_strips_legacy_thinking_block():
    result = _scrub_thinking_tags("a <thinking>x</thinking> b")
    assert "x" not in result
    assert "a" in result and "b" in result


def test_scrub_strips_orphan_markers():
    # Unpaired markers (no matching open/close) are stripped individually.
    result = _scrub_thinking_tags("answer <think> more")
    assert "<think>" not in result
    assert "answer" in result and "more" in result

    result = _scrub_thinking_tags("reasoning </think> tail")
    assert "</think>" not in result
    assert "reasoning" in result and "tail" in result


def test_scrub_strips_special_tokens():
    assert _scrub_thinking_tags("Hello <|im_start|>world<|endoftext|>") == "Hello world"


def test_scrub_preserves_plain_text():
    assert _scrub_thinking_tags("just a normal answer") == "just a normal answer"


def test_stream_sanitizer_does_not_leak_reasoning_tags_split_across_chunks():
    sanitizer = _ThinkingStreamSanitizer()

    visible = "".join(
        sanitizer.feed(chunk)
        for chunk in ["Answer ", "<thi", "nk>SECRET", "</thin", "k>", "continues"]
    ) + sanitizer.finish()

    assert visible == "Answer continues"
    assert "SECRET" not in visible


# -------------------------------------------------------------- adapter helpers


def test_strip_special_tokens():
    assert _strip_special_tokens("a<|im_start|>b<|endoftext|>c") == "abc"
    assert _strip_special_tokens("<|user|>\n<|assistant|>") == "\n"
    assert _strip_special_tokens("no tokens here") == "no tokens here"


def test_splitter_events_maps_kinds_to_event_types():
    events = _splitter_events([("text", "hi"), ("reasoning", "rm"), ("text", "")])
    assert len(events) == 1  # empty and raw-reasoning segments are dropped
    assert events[0].type == StreamEventType.TEXT_CHUNK
    assert events[0].content == "hi"


def test_adapter_flow_strips_inline_raw_thinking_from_visible_events():
    """Chat-compatible inline CoT stays out of both answer and timeline."""
    splitter = _ReasoningSplitter()
    events = _splitter_events(splitter.feed("<think>r</think>a"))
    events += _splitter_events(splitter.flush())
    reasoning = "".join(e.content for e in events if e.type == StreamEventType.THINKING_CHUNK)
    text = "".join(e.content for e in events if e.type == StreamEventType.TEXT_CHUNK)
    assert reasoning == ""
    assert text == "a"


# ------------------------------------------- adapter HTTP path (end-to-end wiring)

import asyncio  # noqa: E402
import json  # noqa: E402

from backend.config import LLMSettings  # noqa: E402
from backend.llm.openai_adapter import OpenAIAdapter  # noqa: E402


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

    def stream(self, *args: object, **kwargs: object) -> "_FakeStreamResponse":
        return _FakeStreamResponse(self._lines)


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}"


async def _collect_http(lines: list[str]):
    adapter = OpenAIAdapter(LLMSettings(api_key="x"))
    adapter._http_client = _FakeHTTPClient(lines)
    return [e async for e in adapter._emit_chat_http_stream_events({})]


def _reasoning_and_text(events):
    reasoning = "".join(e.content for e in events if e.type == StreamEventType.THINKING_CHUNK)
    text = "".join(e.content for e in events if e.type == StreamEventType.TEXT_CHUNK)
    return reasoning, text


def test_http_path_reasoning_content_becomes_thinking_delta():
    lines = [
        _sse({"choices": [{"delta": {"reasoning_content": "reasoning here"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "the answer"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    events = asyncio.run(_collect_http(lines))
    reasoning, text = _reasoning_and_text(events)
    assert reasoning == "reasoning here"
    assert text == "the answer"
    thinking = [e for e in events if e.type == StreamEventType.THINKING_CHUNK]
    assert len(thinking) == 1
    assert thinking[0].raw["provider_reasoning_type"] == "reasoning_content"


def test_http_path_inline_think_is_stripped_across_deltas():
    lines = [
        _sse({"choices": [{"delta": {"content": "<thi"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "nk>chain of thought</th"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {"content": "ink>final answer"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    reasoning, text = _reasoning_and_text(asyncio.run(_collect_http(lines)))
    assert reasoning == ""
    assert text == "final answer"
    thinking = [e for e in asyncio.run(_collect_http(lines)) if e.type == StreamEventType.THINKING_CHUNK]
    assert thinking == []


def test_http_path_reasoning_fields_prefer_first_non_empty_field():
    lines = [
        _sse(
            {
                "choices": [
                    {
                        "delta": {
                            "reasoning_content": "first",
                            "reasoning": "duplicate",
                        },
                        "finish_reason": None,
                    }
                ]
            }
        ),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    events = asyncio.run(_collect_http(lines))
    reasoning, _ = _reasoning_and_text(events)
    assert reasoning == "first"
    thinking = [e for e in events if e.type == StreamEventType.THINKING_CHUNK]
    assert thinking[0].raw["provider_reasoning_type"] == "reasoning_content"


def test_http_path_strips_special_tokens_from_content():
    lines = [
        _sse({"choices": [{"delta": {"content": "Hello <|im_start|>world<|endoftext|>"}, "finish_reason": None}]}),
        _sse({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
        "data: [DONE]",
    ]
    _, text = _reasoning_and_text(asyncio.run(_collect_http(lines)))
    assert text == "Hello world"
    assert "<|" not in text


# --------------------------------------------- adapter Responses API path (GPT)


class _FakeResponsesHTTPClient:
    """Records the Responses request payload and replays SSE lines back."""

    def __init__(self, events: list[dict]) -> None:
        self._lines = [_sse(event) for event in events] + ["data: [DONE]"]
        self.calls: list[dict] = []

    def stream(self, _method: str, _url: str, **kwargs: object) -> "_FakeStreamResponse":
        payload = kwargs.get("json")
        self.calls.append(dict(payload) if isinstance(payload, dict) else {})
        return _FakeStreamResponse(list(self._lines))


def _responses_adapter(
    events: list[dict],
    **settings: object,
) -> tuple[OpenAIAdapter, _FakeResponsesHTTPClient]:
    adapter = OpenAIAdapter(
        LLMSettings(api_key="x", wire_api="responses", **settings)
    )
    client = _FakeResponsesHTTPClient(events)
    adapter._http_client = client
    return adapter, client


async def _collect_responses(events: list[dict]):
    adapter, _client = _responses_adapter(events)
    return [e async for e in adapter.stream_chat([])]


async def _drain_stream(stream):
    return [event async for event in stream]


def _responses_request(**settings: object) -> dict:
    adapter, client = _responses_adapter([], **settings)
    asyncio.run(_drain_stream(adapter.stream_chat([])))
    assert client.calls
    return client.calls[0]


def test_responses_path_reasoning_summary_becomes_thinking_chunk():
    """GPT-5 / o-series stream reasoning via response.reasoning_summary_text.delta."""
    events = [
        {"type": "response.reasoning_summary_text.delta", "delta": "GPT reasoning here"},
        {"type": "response.output_text.delta", "delta": "the answer"},
    ]
    reasoning, text = _reasoning_and_text(asyncio.run(_collect_responses(events)))
    assert reasoning == "GPT reasoning here"
    assert text == "the answer"
    thinking = [e for e in asyncio.run(_collect_responses(events)) if e.type == StreamEventType.THINKING_CHUNK]
    assert thinking[0].raw["provider_reasoning_type"] == "reasoning_summary_text"


def test_responses_path_full_reasoning_text_stays_hidden():
    events = [
        {"type": "response.reasoning_text.delta", "delta": "detailed reasoning"},
        {"type": "response.output_text.delta", "delta": "answer"},
    ]
    reasoning, text = _reasoning_and_text(asyncio.run(_collect_responses(events)))
    assert reasoning == ""
    assert text == "answer"
    thinking = [e for e in asyncio.run(_collect_responses(events)) if e.type == StreamEventType.THINKING_CHUNK]
    assert thinking == []


def test_responses_defaults_to_reasoning_effort_without_summary():
    request = _responses_request(
        reasoning_effort="high",
        reasoning_effort_levels=("high",),
    )
    assert request["reasoning"] == {"effort": "high"}


def test_responses_requests_configured_reasoning_summary_level():
    request = _responses_request(
        reasoning_effort="medium",
        reasoning_effort_levels=("medium",),
        responses_reasoning_summary="detailed",
    )
    assert request["reasoning"] == {"summary": "detailed", "effort": "medium"}


def test_responses_reasoning_summary_can_be_omitted_for_diagnostics():
    request = _responses_request(
        reasoning_effort="low",
        reasoning_effort_levels=("low",),
        responses_reasoning_summary="off",
    )
    assert request["reasoning"] == {"effort": "low"}


def test_provider_metadata_preserves_max_reasoning_effort():
    request = _responses_request(
        model="gpt-5.6",
        reasoning_effort="max",
        reasoning_effort_levels=("max",),
    )
    assert request["reasoning"] == {"effort": "max"}
