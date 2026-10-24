from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.agent.provider_event_projection import project_non_text_provider_event
from backend.agent.provider_protocol import build_llm_request_metadata
from backend.agent.turn_state import AgentTurnState
from backend.config_helpers import LLMSettings
from backend.agent.message import AgentEvent
from backend.llm.base import LLMMessage, StreamEventType, stream_chat_with_request_metadata
from backend.llm.openai_adapter import OpenAIAdapter


class SummaryWire(httpx.AsyncByteStream):
    def __init__(self, frames):
        self.frames = frames
        self.last_type = ""

    async def __aiter__(self):
        for frame in self.frames:
            self.last_type = frame["type"]
            yield ("data: " + json.dumps(frame) + "\n\n").encode()


def summary_frames():
    summaries = ["First complete summary", "Second complete summary"]
    frames = [{"type": "response.output_item.added", "output_index": 0,
        "item": {"id": "reasoning-1", "type": "reasoning", "summary": []}}]
    for index, text in enumerate(summaries):
        owner = {"item_id": "reasoning-1", "summary_index": index}
        frames += [
            {"type": "response.reasoning_summary_part.added", **owner,
                "part": {"type": "summary_text", "text": ""}},
            {"type": "response.reasoning_summary_text.delta", **owner, "delta": text[:6]},
            {"type": "response.reasoning_summary_text.delta", **owner, "delta": text[6:]},
            {"type": "response.reasoning_summary_text.done", **owner, "text": text},
            {"type": "response.reasoning_summary_part.done", **owner,
                "part": {"type": "summary_text", "text": text}},
        ]
    reasoning = {"id": "reasoning-1", "type": "reasoning",
        "summary": [{"type": "summary_text", "text": text} for text in summaries]}
    message = {"id": "answer-1", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Accepted answer", "annotations": []}]}
    frames += [
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning},
        {"type": "response.completed", "response": {
            "id": "response-1", "status": "completed", "output": [reasoning, message],
        }},
    ]
    return frames


async def collect_summary(*, enabled, provider="openai", summary="auto", frames=None):
    wire = SummaryWire(frames if frames is not None else summary_frames())
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    metadata = build_llm_request_metadata(
        metadata={"_concurrent_reasoning_summaries": enabled}, session_id="summary-session",
        task_id="", workspace_root=None, run_id="summary-run", conversation_id="summary-thread",
    )
    turn = AgentTurnState(now_ms=lambda: 1000)
    chunks = []
    events = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(LLMSettings(provider=provider, api_key="fixture",
            base_url="https://fixture.invalid/v1", model="gpt-6.1-sol", wire_api="responses",
            proxy_mode="direct", responses_reasoning_summary=summary), http_client=client)
        async for event in stream_chat_with_request_metadata(adapter, [LLMMessage(role="user", content="Work")], metadata=metadata):
            events.append(event)
            if event.type == StreamEventType.THINKING_CHUNK:
                chunks.append((event.content, event.content_index, wire.last_type))
                async for projected in project_non_text_provider_event(event,
                    stream_state=None, stream_text=None, live_text_streaming=True,
                    tool_tracker=None, tool_registry=None, tool_context=None,
                    process_event_factory=lambda *args, **kwargs: None):
                    if isinstance(projected, AgentEvent):
                        turn.append_thinking(projected.data["content"], {
                            key: projected.data[key] for key in
                            ("source", "visibility", "phase", "provider_reasoning_type", "item_id", "content_index")
                            if key in projected.data
                        })
        await adapter.aclose()
    return requests, chunks, turn.finalize(terminal_status="completed"), events


def test_cutoff_requests_completed_summaries_and_persists_only_completed_parts():
    requests, chunks, snapshot, events = asyncio.run(collect_summary(enabled=True))
    assert requests[0]["stream_options"] == {"reasoning_summary_delivery": "sequential_cutoff"}
    assert "_concurrent_reasoning_summaries" not in json.dumps(requests[0])
    assert chunks == [
        ("First complete summary", 0, "response.reasoning_summary_text.done"),
        ("Second complete summary", 1, "response.reasoning_summary_text.done"),
    ]
    assert [block["content"] for block in snapshot.blocks] == [
        "First complete summary", "Second complete summary",
    ]
    assert [block["content_index"] for block in snapshot.blocks] == [0, 1]
    assert all(block["provider_reasoning_type"] == "reasoning_summary_text" for block in snapshot.blocks)
    assert sum(event.type == StreamEventType.TRANSPORT_ACTIVITY for event in events) >= 6
    assert events[-1].type == StreamEventType.DONE


@pytest.mark.parametrize("enabled,provider", [(False, "openai"), (True, "custom")])
def test_default_and_non_openai_providers_keep_streaming_summary_deltas(enabled, provider):
    requests, chunks, snapshot, events = asyncio.run(collect_summary(enabled=enabled, provider=provider))
    assert "stream_options" not in requests[0]
    assert all(source == "response.reasoning_summary_text.delta" for _, _, source in chunks)
    assert "".join(text for text, _, _ in chunks) == "First complete summarySecond complete summary"
    assert [block["content"] for block in snapshot.blocks] == ["First complete summary", "Second complete summary"]
    assert events[-1].type == StreamEventType.DONE


def test_cutoff_does_not_add_a_delivery_option_when_no_summary_is_requested():
    requests, _, _, _ = asyncio.run(collect_summary(enabled=True, summary="off"))
    assert "stream_options" not in requests[0]


def test_cutoff_ignores_a_completed_part_for_another_active_item():
    frames = summary_frames()
    frames.insert(1, {"type": "response.reasoning_summary_text.done", "item_id": "retired-item",
        "summary_index": 0, "text": "Must not enter the transcript"})
    _, chunks, snapshot, _ = asyncio.run(collect_summary(enabled=True, frames=frames))
    assert all("Must not" not in text for text, _, _ in chunks)
    assert "Must not" not in json.dumps(snapshot.blocks)
