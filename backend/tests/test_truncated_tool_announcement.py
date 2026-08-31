"""A truncated tool announcement must not stay pending for the whole run."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from backend.agent.loop import run_agent_loop
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallStartEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry


class _TruncatedToolCallLLM(LLMAdapter):
    """Announce a tool block, then stop on the output limit before its args."""

    def __init__(self) -> None:
        self.attempts = 0

    async def stream_chat(self, messages, tools=None):
        del messages, tools
        self.attempts += 1
        if self.attempts == 1:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="我来写文件。")
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL_START,
                tool_call_start=ToolCallStartEvent(id="call_truncated", name="write_file"),
            )
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="length")
            return
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Done")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        del messages
        return ""


def _run(llm: LLMAdapter) -> list:
    async def run() -> list:
        workspace = tempfile.mkdtemp()
        events = []
        async for event in run_agent_loop(
            user_message="write the page",
            llm=llm,
            tool_registry=ToolRegistry(),
            artifact_store=ArtifactStore(storage_dir=workspace),
            permission_checker=PermissionChecker(
                settings=PermissionSettings(),
                workspace_root=Path(workspace),
            ),
            agent_settings=AgentSettings(max_iterations=3, live_text_streaming=True),
            permission_context=PermissionContext(mode="bypass"),
        ):
            events.append(event)
        return events

    return asyncio.run(run())


def test_truncated_tool_announcement_is_settled_as_cancelled() -> None:
    events = _run(_TruncatedToolCallLLM())

    announced = [
        event
        for event in events
        if event.type == "tool_call" and event.data.get("id") == "call_truncated"
    ]
    assert announced, "the truncated tool block should still be announced live"
    assert announced[0].data.get("status") == "pending"

    settled = [
        event
        for event in events
        if event.type == "tool_result" and event.data.get("id") == "call_truncated"
    ]
    # Without an explicit settlement the card stays pending for the rest of the
    # run and the turn projection reports a bare failure with no arguments and
    # no reason.
    assert len(settled) == 1
    assert settled[0].data.get("status") == "cancelled"
    assert settled[0].data.get("summary")
    assert any(event.type == "done" for event in events)
