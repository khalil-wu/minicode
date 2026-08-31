"""Agent-message lifecycle contract for provider text."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from backend.agent.loop import run_agent_loop
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry


# NOTE: real adapters (Anthropic, OpenAI chat-completions, Responses) can emit
# text without a phase.  The projection owns that ambiguity: it streams a
# provisional item immediately, then commits it as model_final if no tool
# boundary arrives.
class _UnphasedTextLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        del messages, tools
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Hello ")
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="world")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        del messages
        return ""


class _FinalPhaseTextLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        del messages, tools
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="Hello ",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(
            type=StreamEventType.TEXT_CHUNK,
            content="world",
            phase="final_answer",
            raw={"message_phase": "final_answer"},
        )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        del messages
        return ""


class _SplitThinkingFinalLLM(LLMAdapter):
    async def stream_chat(self, messages, tools=None):
        del messages, tools
        for content in ("<thi", "nking>SECRET", "</thinking>", "Visible answer"):
            yield StreamEvent(
                type=StreamEventType.TEXT_CHUNK,
                content=content,
                phase="final_answer",
                raw={"message_phase": "final_answer"},
            )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        del messages
        return ""


def _run(llm: LLMAdapter, *, live: bool) -> list:
    async def run() -> list:
        workspace = tempfile.mkdtemp()
        events = []
        async for event in run_agent_loop(
            user_message="say hi",
            llm=llm,
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=workspace),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=Path(workspace),
            ),
            agent_settings=AgentSettings(max_iterations=2, live_text_streaming=live),
            permission_context=PermissionContext(mode="bypass"),
        ):
            events.append(event)
        return events

    return asyncio.run(run())


def _completed_text(events: list) -> str:
    completed = [
        event.data["item"]
        for event in events
        if event.type == "item.completed"
        and isinstance(event.data.get("item"), dict)
        and event.data["item"].get("type") == "agent_message"
    ]
    assert len(completed) == 1
    return str(completed[0].get("text") or "")


def test_phaseless_provider_fallback_commits_one_completed_item() -> None:
    # Compatible providers are not required to label text phases.  The
    # provisional item is visible while the provider is still running and is
    # committed exactly once when DONE proves it is the final answer.
    events = _run(_UnphasedTextLLM(), live=True)

    lifecycle = [
        event.type
        for event in events
        if event.type in {"item.started", "agent_message.delta", "item.completed"}
    ]
    assert lifecycle == [
        "item.started",
        "agent_message.delta",
        "agent_message.delta",
        "item.completed",
    ]
    # An unlabelled item is provisional: the tool boundary may still reclassify
    # it as commentary.  Publishing that at the start keeps the renderer from
    # showing narration as a committed answer and then retracting it.
    started = next(event for event in events if event.type == "item.started")
    assert started.data["item"]["source"] == "pending"
    assert _completed_text(events) == "Hello world"
    assert any(event.type == "done" for event in events)


def test_explicit_final_phase_uses_started_delta_completed_order() -> None:
    events = _run(_FinalPhaseTextLLM(), live=True)
    lifecycle = [
        event.type
        for event in events
        if event.type in {"item.started", "agent_message.delta", "item.completed"}
    ]

    assert lifecycle == [
        "item.started",
        "agent_message.delta",
        "agent_message.delta",
        "item.completed",
    ]
    started = next(event for event in events if event.type == "item.started")
    deltas = [event for event in events if event.type == "agent_message.delta"]
    completed = next(event for event in events if event.type == "item.completed")
    # A provider-declared final phase travels with the item start so live answer
    # text streams in the answer surface instead of the work log.  Deltas stay
    # text-only; the authoritative source is still the completed item.
    assert started.data["item"]["source"] == "model_final"
    assert all("source" not in event.data for event in deltas)
    assert completed.data["item"]["source"] == "model_final"
    assert _completed_text(events) == "Hello world"


def test_disabled_live_streaming_emits_only_completed_item() -> None:
    events = _run(_FinalPhaseTextLLM(), live=False)

    lifecycle = [
        event.type
        for event in events
        if event.type in {"item.started", "agent_message.delta", "item.completed"}
    ]
    assert lifecycle == ["item.started", "item.completed"]
    assert _completed_text(events) == "Hello world"


def test_split_thinking_tags_never_reach_completed_item() -> None:
    events = _run(_SplitThinkingFinalLLM(), live=True)

    answer = _completed_text(events)
    assert answer == "Visible answer"
    assert "SECRET" not in answer
