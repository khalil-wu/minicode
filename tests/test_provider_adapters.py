"""Provider adapter contract tests (Phase 6).

These lock the boundary the agent loop relies on: the loop writes a single
provider-neutral LLMMessage history; each adapter is solely responsible for
emitting wire-correct requests. No provider-specific shape leaks into the loop.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

from backend.config import LLMSettings
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    SideQueryOptions,
    StreamEventType,
    ToolCallEvent,
)
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.openai_adapter import (
    OpenAIAdapter,
    _download_remote_image,
    _extract_images_api_images,
)
from backend.tools.base import ToolSchema


async def _collect_events(stream):
    return [event async for event in stream]


class _ClosableAsyncStream:
    """Small SDK-stream double that records the adapter cleanup boundary."""

    def __init__(self, events: list[object]) -> None:
        self._events = iter(events)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._events)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.closed = True


def _json_value(value):
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return {str(key): _json_value(item) for key, item in vars(value).items()}
    return value


class _SDKBackedResponse:
    status_code = 200
    headers = {}

    def __init__(self, fake_call, body, headers):
        self._fake_call = fake_call
        self._body = body
        self._headers = headers
        self._lines = []

    async def __aenter__(self):
        stream = await self._fake_call(**self._body)
        self._stream = stream
        events = [event async for event in stream]
        self._lines = [f"data: {json.dumps(_json_value(event))}" for event in events]
        return self

    async def __aexit__(self, *exc):
        close = getattr(self, "_stream", None)
        if close is not None and hasattr(close, "aclose"):
            await close.aclose()
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    def raise_for_status(self):
        return None


class _SDKBackedClient:
    def __init__(self, fake_call):
        self._fake_call = fake_call

    def stream(self, method, url, *, headers, json, timeout=None):
        del method, url, timeout
        body = dict(json)
        body["extra_headers"] = {
            key: value
            for key, value in headers.items()
            if key.startswith("x-") or key == "anthropic-beta"
        }
        return _SDKBackedResponse(self._fake_call, body, headers)


def _install_anthropic_fake(adapter, fake_call):
    adapter._http_client = _SDKBackedClient(fake_call)


# ── Anthropic: tool trajectory ──────────────────────────────────────────────


def test_owned_openai_and_anthropic_transports_apply_explicit_direct_networking(
    monkeypatch,
) -> None:
    openai_http_kwargs: list[dict[str, object]] = []
    anthropic_http_kwargs: list[dict[str, object]] = []

    class _HTTPClient:
        async def aclose(self) -> None:
            return None

    def openai_http_client(**kwargs):
        openai_http_kwargs.append(dict(kwargs))
        return _HTTPClient()

    def anthropic_http_client(**kwargs):
        anthropic_http_kwargs.append(dict(kwargs))
        return _HTTPClient()

    monkeypatch.setattr(
        "backend.llm.openai_adapter.httpx.AsyncClient",
        openai_http_client,
    )
    openai_adapter = OpenAIAdapter(
        LLMSettings(
            api_key="test",
            base_url="https://openai.example/v1",
            model="gateway-model",
            proxy_mode="direct",
        )
    )

    monkeypatch.setattr("backend.llm.anthropic_adapter.httpx.AsyncClient", anthropic_http_client)
    anthropic_adapter = AnthropicAdapter(
        api_key="test",
        base_url="https://anthropic.example/v1",
        proxy_mode="direct",
    )
    anthropic_adapter._get_http_client()

    assert openai_http_kwargs == [{"trust_env": False}]
    assert len(anthropic_http_kwargs) == 1
    assert all(kwargs.get("proxy") is None for kwargs in anthropic_http_kwargs)
    assert all(kwargs.get("trust_env") is False for kwargs in anthropic_http_kwargs)

    asyncio.run(openai_adapter.aclose())
    asyncio.run(anthropic_adapter.aclose())


def test_remote_images_keep_the_provider_proxy_mode(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    async def download(url: str, *, proxy_mode: str = "inherit"):
        seen.append((url, proxy_mode))
        return ("aW1hZ2U=", "image/png")

    monkeypatch.setattr(
        "backend.llm.openai_adapter._download_remote_image",
        download,
    )

    images = asyncio.run(
        _extract_images_api_images(
            {"data": [{"url": "https://cdn.example/image.png"}]},
            proxy_mode="direct",
        )
    )

    assert images == [("aW1hZ2U=", "image/png")]
    assert seen == [("https://cdn.example/image.png", "direct")]


def test_remote_image_download_does_not_treat_proxy_peer_as_origin(monkeypatch) -> None:
    class _Stream:
        def get_extra_info(self, name):
            return ("127.0.0.1", 8080) if name == "server_addr" else None

    class _Response:
        status_code = 200
        headers = {"content-type": "image/png", "content-length": "4"}
        extensions = {"network_stream": _Stream()}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        async def aiter_bytes(self):
            yield b"body"

    class _Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return _Response()

    monkeypatch.setattr(
        "backend.llm.openai_adapter._proxy_url_for_base_url",
        lambda _url, _mode: "http://127.0.0.1:8080",
    )
    monkeypatch.setattr(
        "backend.llm.openai_adapter.assess_network_url",
        lambda _url: SimpleNamespace(allowed=True, reason=""),
    )
    monkeypatch.setattr(
        "backend.llm.openai_adapter._generated_image_media_type",
        lambda _body: "image/png",
    )
    monkeypatch.setattr("backend.llm.openai_adapter.httpx.AsyncClient", _Client)

    encoded, media_type = asyncio.run(
        _download_remote_image("https://cdn.example/image.png", proxy_mode="inherit")
    )

    assert encoded == "Ym9keQ=="
    assert media_type == "image/png"


def test_anthropic_single_tool_call_trajectory() -> None:
    msgs = [
        LLMMessage(role="system", content="SYS"),
        LLMMessage(role="user", content="read it"),
        LLMMessage(
            role="assistant",
            tool_calls=[
                ToolCallEvent(id="c1", name="read_file", arguments={"path": "a"})
            ],
        ),
        LLMMessage(role="tool", tool_call_id="c1", content="body"),
    ]
    system, api = AnthropicAdapter._convert_messages(msgs)

    assert system == "SYS"
    assert [m["role"] for m in api] == ["user", "assistant", "user"]
    # assistant carries a tool_use block
    assistant = api[1]
    assert any(
        b["type"] == "tool_use" and b["id"] == "c1" for b in assistant["content"]
    )
    # tool result becomes a user tool_result block referencing the same id
    tr = api[2]["content"][0]
    assert tr["type"] == "tool_result"
    assert tr["tool_use_id"] == "c1"


def test_anthropic_parallel_tool_results_merge_into_one_user_message() -> None:
    msgs = [
        LLMMessage(role="user", content="do two"),
        LLMMessage(
            role="assistant",
            tool_calls=[
                ToolCallEvent(id="c1", name="read_file", arguments={"path": "a"}),
                ToolCallEvent(id="c2", name="read_file", arguments={"path": "b"}),
            ],
        ),
        LLMMessage(role="tool", tool_call_id="c1", content="A"),
        LLMMessage(role="tool", tool_call_id="c2", content="B"),
    ]
    _, api = AnthropicAdapter._convert_messages(msgs)

    # Two consecutive tool results collapse into a single user message with two blocks.
    assert [m["role"] for m in api] == ["user", "assistant", "user"]
    merged = api[2]["content"]
    assert [b["tool_use_id"] for b in merged] == ["c1", "c2"]
    assert all(b["type"] == "tool_result" for b in merged)


def test_anthropic_assistant_tool_use_preserves_parallel_order() -> None:
    msgs = [
        LLMMessage(role="user", content="x"),
        LLMMessage(
            role="assistant",
            tool_calls=[
                ToolCallEvent(id="c1", name="t1", arguments={}),
                ToolCallEvent(id="c2", name="t2", arguments={}),
            ],
        ),
    ]
    _, api = AnthropicAdapter._convert_messages(msgs)
    tool_uses = [b for b in api[1]["content"] if b["type"] == "tool_use"]
    assert [b["id"] for b in tool_uses] == ["c1", "c2"]


def test_anthropic_dangling_tool_result_does_not_crash() -> None:
    # A tool_result with no preceding assistant tool_use must not raise; the
    # loop's reconciler is the primary guard, the adapter degrades gracefully.
    msgs = [
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="tool", tool_call_id="orphan", content="X"),
    ]
    _, api = AnthropicAdapter._convert_messages(msgs)
    assert all(m["role"] in {"user", "assistant"} for m in api)


def test_anthropic_stream_preserves_content_index_lifecycle() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-1", usage=SimpleNamespace(input_tokens=1, output_tokens=0)
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="thinking", thinking="", signature="", data=""
            ),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="thinking_delta", thinking="reason", signature=""
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="text"),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="answer"),
        )
        yield SimpleNamespace(type="content_block_stop", index=1)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=2),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)

    async def collect():
        return [
            event
            async for event in adapter.stream_chat(
                [LLMMessage(role="user", content="hi")]
            )
        ]

    events = asyncio.run(collect())
    thinking = [
        event for event in events if event.type == StreamEventType.THINKING_CHUNK
    ]
    text = [event for event in events if event.type == StreamEventType.TEXT_CHUNK]

    assert [event.lifecycle for event in thinking] == ["start", "delta", "end"]
    assert {event.item_id for event in thinking} == {"msg-1:content:0"}
    assert {event.content_index for event in thinking} == {0}
    assert [event.lifecycle for event in text] == ["start", "delta", "end"]
    assert {event.item_id for event in text} == {"msg-1:content:1"}
    assert {event.content_index for event in text} == {1}


def test_anthropic_stream_preserves_initial_block_payloads_and_tool_input() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-initial",
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="thinking",
                thinking="initial reasoning",
                signature="sig",
                data="",
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="text", text="initial answer"),
        )
        yield SimpleNamespace(type="content_block_stop", index=1)
        yield SimpleNamespace(
            type="content_block_start",
            index=2,
            content_block=SimpleNamespace(
                type="tool_use",
                id="tool-initial",
                name="read_file",
                input={"path": "a.txt"},
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=2)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=SimpleNamespace(output_tokens=3),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)

    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="work")])
        )
    )

    assert "".join(
        event.content
        for event in events
        if event.type == StreamEventType.THINKING_CHUNK
    ) == "initial reasoning"
    assert "".join(
        event.content
        for event in events
        if event.type == StreamEventType.TEXT_CHUNK
    ) == "initial answer"
    final_tools = [
        event
        for event in events
        if event.type == StreamEventType.TOOL_CALL and event.tool_calls_final
    ]
    assert final_tools[-1].tool_calls == [
        ToolCallEvent(
            id="tool-initial",
            name="read_file",
            arguments={"path": "a.txt"},
        )
    ]


def test_anthropic_stream_debounces_input_json_delta_into_tool_call_events() -> None:
    adapter = AnthropicAdapter(api_key="test")
    full_arguments = json.dumps(
        {"path": "a.txt", "content": "x" * 200}, ensure_ascii=False
    )
    fragments = [
        full_arguments[:90],
        full_arguments[90:180],
        full_arguments[180:],
    ]

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-streamed-tool",
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="tool_use",
                id="tool-streamed",
                name="write_file",
                input={},
            ),
        )
        for fragment in fragments:
            yield SimpleNamespace(
                type="content_block_delta",
                index=0,
                delta=SimpleNamespace(
                    type="input_json_delta",
                    partial_json=fragment,
                ),
            )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=SimpleNamespace(output_tokens=5),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)

    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="work")])
        )
    )

    starts = [event for event in events if event.type == StreamEventType.TOOL_CALL_START]
    deltas = [event for event in events if event.type == StreamEventType.TOOL_CALL_DELTA]
    finals = [
        event
        for event in events
        if event.type == StreamEventType.TOOL_CALL and event.tool_calls_final
    ]

    assert [event.tool_call_start.id for event in starts] == ["tool-streamed"]
    # Crossing the debounce byte budget must emit accumulated-argument deltas;
    # staying silent here previously hid an undefined-name crash in this path.
    assert deltas, "input_json_delta stream produced no TOOL_CALL_DELTA events"
    assert all(
        event.tool_call_delta.id == "tool-streamed" for event in deltas
    )
    assert full_arguments.startswith(deltas[0].tool_call_delta.partial_arguments)
    assert finals[-1].tool_calls == [
        ToolCallEvent(
            id="tool-streamed",
            name="write_file",
            arguments={"path": "a.txt", "content": "x" * 200},
        )
    ]


def test_anthropic_rejects_tool_calls_with_non_tool_stop_reason() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-mismatch",
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="tool_use",
                id="tool-mismatch",
                name="read_file",
                input={"path": "a.txt"},
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=1),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="work")])
        )
    )

    assert events[-1].type == StreamEventType.ERROR
    assert events[-1].raw["event_type"] == "tool_stop_reason_mismatch"
    assert not [
        event
        for event in events
        if event.type == StreamEventType.TOOL_CALL and event.tool_calls_final
    ]


def test_anthropic_adaptive_thinking_uses_wire_model_and_preserves_output_format() -> None:
    adapter = AnthropicAdapter(
        api_key="test",
        model="claude-primary",
        small_fast_model="claude-opus-5-fast",
        thinking_budget=4_096,
    )
    captured: dict[str, object] = {}

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-adaptive",
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text"),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text='{"ok":true}'),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=2),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        captured.update(kwargs)
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)

    async def collect() -> str:
        return await adapter.side_query(
            [LLMMessage(role="user", content="answer")],
            options=SideQueryOptions(
                operation="structured",
                use_small_fast_model=True,
                disable_reasoning=False,
                output_schema={
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}},
                    "required": ["ok"],
                },
            ),
        )

    result = asyncio.run(collect())

    assert result == '{"ok":true}'
    assert captured["model"] == "claude-opus-5-fast"
    assert captured["thinking"] == {"type": "adaptive"}
    assert captured["output_config"] == {
        "effort": "high",
        "format": {
            "type": "json_schema",
            "schema": {
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        },
    }


def test_anthropic_pause_turn_preserves_native_assistant_content_for_replay() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-pause",
                usage=SimpleNamespace(input_tokens=3, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="thinking",
                thinking="",
                signature="",
                data="",
            ),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="thinking_delta", thinking="reason"),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="signature_delta", signature="sig"),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="text", text="Working "),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="now."),
        )
        yield SimpleNamespace(type="content_block_stop", index=1)
        yield SimpleNamespace(
            type="content_block_start",
            index=2,
            content_block=SimpleNamespace(
                type="server_tool_use",
                id="srv-1",
                name="web_search",
                input={"query": "MiniCode"},
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=2)
        yield SimpleNamespace(
            type="content_block_start",
            index=3,
            content_block=SimpleNamespace(
                type="web_search_tool_result",
                tool_use_id="srv-1",
                content=[
                    SimpleNamespace(
                        type="web_search_result",
                        title="Result",
                        url="https://example.test/result",
                    )
                ],
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=3)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="pause_turn"),
            usage=SimpleNamespace(output_tokens=9),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="research")])
        )
    )

    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert done.finish_reason == "pause_turn"
    assert done.provider_items == [
        {
            "type": "anthropic_message",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "reason",
                    "signature": "sig",
                    "data": "",
                },
                {"type": "text", "text": "Working now."},
                {
                    "type": "server_tool_use",
                    "id": "srv-1",
                    "name": "web_search",
                    "input": {"query": "MiniCode"},
                },
                {
                    "type": "web_search_tool_result",
                    "tool_use_id": "srv-1",
                    "content": [
                        {
                            "type": "web_search_result",
                            "title": "Result",
                            "url": "https://example.test/result",
                        }
                    ],
                },
            ],
        }
    ]
    assert done.raw["search_sources"] == [
        {"title": "Result", "url": "https://example.test/result"}
    ]

    _system, replay = AnthropicAdapter._convert_messages(
        [
            LLMMessage(role="user", content="research"),
            LLMMessage(
                role="assistant",
                content="Working now.",
                provider_items=done.provider_items,
            ),
        ]
    )
    assert replay[-1]["content"] == done.provider_items[0]["content"]


def test_anthropic_native_message_replay_does_not_duplicate_projected_tool_call() -> None:
    native_content = [
        {"type": "text", "text": "I’ll inspect it."},
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "read_file",
            "input": {"path": "a.txt"},
        },
    ]
    _system, api = AnthropicAdapter._convert_messages(
        [
            LLMMessage(role="user", content="inspect"),
            LLMMessage(
                role="assistant",
                content="I’ll inspect it.",
                tool_calls=[
                    ToolCallEvent(
                        id="call-1",
                        name="read_file",
                        arguments={"path": "a.txt"},
                    )
                ],
                provider_items=[
                    {"type": "anthropic_message", "content": native_content}
                ],
            ),
        ]
    )

    assert api[-1]["content"] == native_content
    assert [block["type"] for block in api[-1]["content"]].count("tool_use") == 1


def test_anthropic_compaction_delta_is_retained_for_provider_continuation() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-compaction",
                usage=SimpleNamespace(input_tokens=4, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="compaction",
                content="",
                encrypted_content="",
            ),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="compaction_delta",
                content="summary",
                encrypted_content="opaque",
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="compaction"),
            usage=SimpleNamespace(output_tokens=3),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="continue")])
        )
    )

    done = events[-1]
    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]
    assert [
        (activity.id, activity.status, activity.message)
        for activity in activities
    ] == [
        (
            "msg-compaction:content:0",
            "running",
            "Provider context compaction in progress",
        ),
        (
            "msg-compaction:content:0",
            "completed",
            "Provider context compaction completed",
        ),
    ]
    assert done.finish_reason == "compaction"
    assert done.provider_items == [
        {
            "type": "anthropic_message",
            "content": [
                {
                    "type": "compaction",
                    "content": "summary",
                    "encrypted_content": "opaque",
                }
            ],
        }
    ]


def test_anthropic_message_delta_refreshes_all_cumulative_usage_fields() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-usage",
                usage=SimpleNamespace(
                    input_tokens=10,
                    output_tokens=0,
                    cache_creation_input_tokens=2,
                    cache_read_input_tokens=3,
                    service_tier="priority",
                    inference_geo="us",
                    server_tool_use=SimpleNamespace(
                        web_search_requests=1,
                        web_fetch_requests=0,
                    ),
                ),
            ),
        )
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(
                input_tokens=11,
                output_tokens=5,
                cache_creation_input_tokens=4,
                cache_read_input_tokens=6,
                cache_creation=SimpleNamespace(
                    ephemeral_5m_input_tokens=1,
                    ephemeral_1h_input_tokens=3,
                ),
                server_tool_use=SimpleNamespace(
                    web_search_requests=2,
                    web_fetch_requests=1,
                ),
            ),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="usage")])
        )
    )

    done = events[-1]
    assert done.usage.input_tokens == 11
    assert done.usage.output_tokens == 5
    assert done.usage.cache_creation_input_tokens == 4
    assert done.usage.cache_read_input_tokens == 6
    assert done.raw["usage"] == {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_creation_input_tokens": 4,
        "cache_read_input_tokens": 6,
        "service_tier": "priority",
        "inference_geo": "us",
        "server_tool_use": {
            "web_search_requests": 2,
            "web_fetch_requests": 1,
        },
        "cache_creation": {
            "ephemeral_5m_input_tokens": 1,
            "ephemeral_1h_input_tokens": 3,
        },
        "cache_deleted_input_tokens": 0,
    }


# ── OpenAI: message + schema contract ───────────────────────────────────────


def test_openai_assistant_tool_calls_message_shape() -> None:
    m = LLMMessage(
        role="assistant",
        tool_calls=[
            ToolCallEvent(id="c1", name="read_file", arguments={"path": "a"}),
            ToolCallEvent(id="c2", name="grep_files", arguments={"pattern": "x"}),
        ],
    )
    out = m.to_openai_message()
    assert out["role"] == "assistant"
    assert [tc["id"] for tc in out["tool_calls"]] == ["c1", "c2"]
    assert out["tool_calls"][0]["type"] == "function"
    assert out["tool_calls"][0]["function"]["name"] == "read_file"
    # arguments are JSON-encoded strings, not dicts (OpenAI wire format)
    assert isinstance(out["tool_calls"][0]["function"]["arguments"], str)


def test_openai_tool_role_message_carries_tool_call_id() -> None:
    m = LLMMessage(role="tool", tool_call_id="c1", name="read_file", content="body")
    out = m.to_openai_message()
    assert out["role"] == "tool"
    assert out["tool_call_id"] == "c1"
    assert out["content"] == "body"


def test_openai_tool_schema_is_provider_neutral_without_permission_noise() -> None:
    schema = ToolSchema(
        name="write_file",
        description="Create or replace a file.",
        parameters={"type": "object", "properties": {}},
    )
    tool = schema.to_openai_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "write_file"
    # Phase 1.3: no permission/UI metadata in the model-facing schema.
    assert "Permission:" not in tool["function"]["description"]


def test_anthropic_projects_hosted_activity_container_and_native_citations() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-hosted",
                usage=SimpleNamespace(input_tokens=4, output_tokens=0),
                stop_reason="",
                stop_details=None,
                container=SimpleNamespace(
                    id="container-1",
                    expires_at="2026-08-16T18:00:00Z",
                ),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="server_tool_use",
                id="server-search-1",
                name="web_search",
                input={"query": "MiniCode"},
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="web_search_tool_result",
                tool_use_id="server-search-1",
                content=[
                    SimpleNamespace(
                        type="web_search_result",
                        title="MiniCode source",
                        url="https://example.test/minicode",
                    )
                ],
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=1)
        yield SimpleNamespace(
            type="content_block_start",
            index=2,
            content_block=SimpleNamespace(type="text", text="Result"),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=2,
            delta=SimpleNamespace(
                type="citations_delta",
                citation=SimpleNamespace(
                    type="web_search_result_location",
                    url="https://example.test/minicode",
                    title="MiniCode source",
                    cited_text="Result",
                    encrypted_index="opaque",
                ),
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=2)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(
                stop_reason="end_turn",
                stop_details=None,
                container=SimpleNamespace(
                    id="container-1",
                    expires_at="2026-08-16T18:00:00Z",
                ),
            ),
            usage=SimpleNamespace(output_tokens=5),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="research")])
        )
    )

    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]
    assert [
        (activity.id, activity.status, activity.message) for activity in activities
    ] == [
        ("server-search-1", "running", "Searching the web"),
        (
            "server-search-1",
            "completed",
            "Web search completed — 1 source",
        ),
    ]
    expected_input_characters = len(
        json.dumps({"query": "MiniCode"}, ensure_ascii=False, separators=(",", ":"))
    )
    assert activities[0].detail == f"Input: {expected_input_characters} characters"
    assert activities[1].detail == ""
    assert "MiniCode" not in activities[0].detail
    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert done.raw["container"] == {
        "id": "container-1",
        "expires_at": "2026-08-16T18:00:00Z",
    }
    assert done.raw["citations"] == [
        {
            "url": "https://example.test/minicode",
            "title": "MiniCode source",
            "range": [0, 0],
        }
    ]
    assert done.raw["search_sources"] == [
        {
            "title": "MiniCode source",
            "url": "https://example.test/minicode",
        }
    ]


def test_anthropic_hosted_input_delta_projects_only_final_character_count() -> None:
    adapter = AnthropicAdapter(api_key="test")
    sentinel = "DO_NOT_PROJECT_ANTHROPIC_HOSTED_INPUT"
    serialized_input = json.dumps(
        {"query": sentinel},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-hosted-delta",
                usage=SimpleNamespace(input_tokens=2, output_tokens=0),
                stop_reason="",
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="server_tool_use",
                id="server-search-delta",
                name="web_search",
                input={},
            ),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json=serialized_input[:11],
            ),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="input_json_delta",
                partial_json=serialized_input[11:],
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="web_search_tool_result",
                tool_use_id="server-search-delta",
                content=SimpleNamespace(
                    type="web_search_tool_result_error",
                    error_code="rate_limit",
                    message="must never be projected",
                ),
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=1)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=1),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(adapter.stream_chat([LLMMessage(role="user", content="research")]))
    )
    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]

    assert [(activity.status, activity.detail) for activity in activities] == [
        ("running", ""),
        ("running", f"Input: {len(serialized_input)} characters"),
        ("failed", "Error code: rate_limit"),
    ]
    assert all(
        sentinel not in f"{activity.message} {activity.detail}"
        and "must never be projected" not in f"{activity.message} {activity.detail}"
        for activity in activities
    )


def test_anthropic_projects_mcp_list_errors_uploads_and_document_locations() -> None:
    adapter = AnthropicAdapter(api_key="test")
    input_sentinel = "DO_NOT_PROJECT_MCP_ARGUMENT_BODY"
    file_id_sentinel = "file_DO_NOT_PROJECT_RAW_IDENTIFIER"
    cited_text_sentinel = "DO_NOT_PROJECT_CITED_DOCUMENT_TEXT"
    citations = [
        SimpleNamespace(
            type="char_location",
            cited_text=cited_text_sentinel,
            document_index=0,
            document_title="Architecture notes",
            file_id=file_id_sentinel,
            start_char_index=12,
            end_char_index=44,
        ),
        SimpleNamespace(
            type="page_location",
            cited_text=cited_text_sentinel,
            document_index=0,
            document_title="Architecture notes",
            file_id=file_id_sentinel,
            start_page_number=2,
            end_page_number=3,
        ),
        SimpleNamespace(
            type="content_block_location",
            cited_text=cited_text_sentinel,
            document_index=0,
            document_title="Architecture notes",
            file_id=file_id_sentinel,
            start_block_index=4,
            end_block_index=6,
        ),
    ]

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-mcp-document",
                usage=SimpleNamespace(input_tokens=7, output_tokens=0),
                stop_reason="",
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(
                type="container_upload",
                file_id=file_id_sentinel,
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=0)
        yield SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(
                type="mcp_tool_use",
                id="mcp-document-1",
                name="lookup_document",
                server_name="audit-local",
                input={"query": input_sentinel},
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=1)
        yield SimpleNamespace(
            type="content_block_start",
            index=2,
            content_block=SimpleNamespace(
                type="mcp_tool_result",
                tool_use_id="mcp-document-1",
                is_error=False,
                content=[
                    SimpleNamespace(type="text", text="safe prefix"),
                    SimpleNamespace(
                        type="mcp_tool_result_error",
                        error_code="invalid_arguments",
                        message="DO_NOT_PROJECT_PROVIDER_ERROR_MESSAGE",
                    ),
                ],
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=2)
        yield SimpleNamespace(
            type="content_block_start",
            index=3,
            content_block=SimpleNamespace(
                type="text",
                text="Answer",
                citations=citations,
            ),
        )
        yield SimpleNamespace(type="content_block_stop", index=3)
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="end_turn"),
            usage=SimpleNamespace(output_tokens=4),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="inspect")])
        )
    )
    activities = [
        event.provider_activity
        for event in events
        if event.type == StreamEventType.PROVIDER_ACTIVITY
    ]

    assert [(activity.status, activity.message) for activity in activities] == [
        ("completed", "Container file uploaded"),
        ("running", "Using lookup document"),
        ("failed", "MCP tool failed"),
    ]
    assert activities[0].detail.startswith("File ID: ")
    assert file_id_sentinel not in activities[0].detail
    assert activities[1].detail == (
        "Server: audit-local · Tool: lookup_document · "
        f"Arguments: {len(json.dumps({'query': input_sentinel}, ensure_ascii=False, separators=(',', ':')))} characters"
    )
    assert activities[2].detail == "Error code: invalid_arguments"
    assert all(
        input_sentinel not in f"{activity.message} {activity.detail}"
        and "DO_NOT_PROJECT_PROVIDER_ERROR_MESSAGE" not in f"{activity.message} {activity.detail}"
        for activity in activities
    )

    done = events[-1]
    assert done.type == StreamEventType.DONE
    assert [citation["label"] for citation in done.raw["citations"]] == [
        "Characters 12–44",
        "Pages 2–3",
        "Blocks 4–6",
    ]
    assert [citation["range"] for citation in done.raw["citations"]] == [
        [12, 44],
        [2, 3],
        [4, 6],
    ]
    assert all(
        citation["source"].startswith("anthropic:document:")
        for citation in done.raw["citations"]
    )
    public_raw = json.dumps(done.raw, ensure_ascii=False)
    assert cited_text_sentinel not in public_raw
    assert file_id_sentinel not in public_raw


def test_anthropic_preserves_structured_refusal_details() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-refusal",
                usage=SimpleNamespace(input_tokens=2, output_tokens=0),
                stop_reason="",
                stop_details=None,
                container=None,
            ),
        )
        yield SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(
                stop_reason="refusal",
                stop_details=SimpleNamespace(
                    type="refusal",
                    category="cyber",
                    explanation="The request crosses the allowed boundary.",
                ),
                container=None,
            ),
            usage=SimpleNamespace(output_tokens=1),
        )
        yield SimpleNamespace(type="message_stop")

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="request")])
        )
    )

    done = events[-1]
    assert done.finish_reason == "refusal"
    assert done.raw["refusal"] == {
        "type": "refusal",
        "category": "cyber",
        "explanation": "The request crosses the allowed boundary.",
    }


def test_anthropic_fails_closed_on_mismatched_content_index() -> None:
    adapter = AnthropicAdapter(api_key="test")

    async def event_stream():
        yield SimpleNamespace(
            type="message_start",
            message=SimpleNamespace(
                id="msg-index",
                usage=SimpleNamespace(input_tokens=1, output_tokens=0),
            ),
        )
        yield SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="text", text=""),
        )
        yield SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="text_delta", text="wrong block"),
        )

    async def fake_call(**kwargs):
        return event_stream()

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(
            adapter.stream_chat([LLMMessage(role="user", content="request")])
        )
    )

    assert events[-1].type == StreamEventType.ERROR
    assert events[-1].raw == {
        "provider": "anthropic",
        "provider_error_type": "protocol",
        "error_type": "api",
        "event_type": "content_block_delta",
        "protocol_error_code": "content_index_mismatch",
        "current_content_index": 0,
        "received_content_index": 1,
        "current_content_kind": "text",
    }


def test_anthropic_closes_sdk_stream_after_protocol_error() -> None:
    adapter = AnthropicAdapter(api_key="test")
    stream = _ClosableAsyncStream([SimpleNamespace(type="future_event")])

    async def fake_call(**kwargs):
        return stream

    _install_anthropic_fake(adapter, fake_call)
    events = asyncio.run(
        _collect_events(adapter.stream_chat([LLMMessage(role="user", content="request")]))
    )

    assert events[-1].type == StreamEventType.ERROR
    assert stream.closed is True


def test_openai_chat_closes_sdk_stream_after_parser_failure() -> None:
    stream = _ClosableAsyncStream(
        [
            SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content=123, tool_calls=None),
                        finish_reason=None,
                    )
                ]
            )
        ]
    )

    async def create(**kwargs):
        return stream

    client = SimpleNamespace(
        responses=SimpleNamespace(create=None),
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="openai",
            api_key="test-key",
            model="gpt-5.4",
            wire_api="chat",
        ),
        http_client=_SDKBackedClient(create),
    )

    events = asyncio.run(
        _collect_events(adapter.stream_chat([LLMMessage(role="user", content="request")]))
    )

    assert events[-1].type == StreamEventType.ERROR
    assert stream.closed is True


def test_openai_chat_surfaces_http_failure_as_structured_stream_error(monkeypatch) -> None:
    adapter = OpenAIAdapter(
        LLMSettings(
            provider="custom",
            api_key="test-key",
            base_url="https://gateway.example/v1",
            model="deepseek-v4-flash",
            wire_api="chat",
        ),
        http_client=SimpleNamespace(),
    )
    response = httpx.Response(
        503,
        request=httpx.Request("POST", "https://gateway.example/v1/chat/completions"),
        json={
            "error": {
                "code": "model_not_found",
                "message": "No available channel",
                "type": "new_api_error",
            }
        },
    )

    async def failing_stream(*_args, **_kwargs):
        raise httpx.HTTPStatusError("server error", request=response.request, response=response)
        yield  # pragma: no cover

    monkeypatch.setattr(adapter, "_emit_chat_http_stream_events", failing_stream)
    events = asyncio.run(
        _collect_events(adapter.stream_chat([LLMMessage(role="user", content="request")]))
    )

    assert events[-1].type == StreamEventType.ERROR
    assert events[-1].raw["provider"] == "openai_chat_completions"
    assert events[-1].raw["status_code"] == 503
    assert events[-1].raw["provider_error_code"] == "model_not_found"


def test_anthropic_rejects_unstable_or_reused_tool_use_identity() -> None:
    cases = [
        (
            [
                SimpleNamespace(
                    type="content_block_start",
                    index=0,
                    content_block=SimpleNamespace(
                        type="tool_use", id="", name="read_file", input={}
                    ),
                )
            ],
            "missing_tool_call_id",
        ),
        (
            [
                SimpleNamespace(
                    type="content_block_start",
                    index=0,
                    content_block=SimpleNamespace(
                        type="tool_use", id="call-1", name="", input={}
                    ),
                )
            ],
            "missing_tool_name",
        ),
        (
            [
                SimpleNamespace(
                    type="content_block_start",
                    index=0,
                    content_block=SimpleNamespace(
                        type="tool_use", id="call-1", name="read_file", input={}
                    ),
                ),
                SimpleNamespace(
                    type="content_block_stop", index=0
                ),
                SimpleNamespace(
                    type="content_block_start",
                    index=1,
                    content_block=SimpleNamespace(
                        type="tool_use", id="call-1", name="read_file", input={}
                    ),
                ),
            ],
            "duplicate_tool_call_id",
        ),
    ]

    for stream_events, expected_code in cases:
        adapter = AnthropicAdapter(api_key="test")

        async def event_stream(events=stream_events):
            yield SimpleNamespace(
                type="message_start",
                message=SimpleNamespace(
                    id="msg-tool-identity",
                    usage=SimpleNamespace(input_tokens=1, output_tokens=0),
                ),
            )
            for event in events:
                yield event

        async def fake_call(**kwargs):
            return event_stream()

        _install_anthropic_fake(adapter, fake_call)
        result = asyncio.run(
            _collect_events(
                adapter.stream_chat([LLMMessage(role="user", content="request")])
            )
        )
        assert result[-1].type == StreamEventType.ERROR
        assert result[-1].raw["protocol_error_code"] == expected_code
