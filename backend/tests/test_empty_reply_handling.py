"""Regression tests for the agent loop's empty-reply handling.

Two bugs motivated these:
1. A model returning nothing on the very first turn (no prior tool calls, e.g. a
   proxy returning an empty 200 body) was silently marked "completed" with zero
   output — the user saw a blank "done" with no error. It must now surface an
   explicit error event after the nudge ladder is exhausted.
2. The consecutive-empty-reply counter is reset to 0 on any productive (tool-
   call) turn, so non-consecutive empty replies across a long session no longer
   accumulate toward the forced fallback.
"""

import asyncio
import tempfile
from pathlib import Path

from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.artifact.store import ArtifactStore
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class _EmptyStreamLLM(LLMAdapter):
    """Always yields an empty stream (only DONE) — no text, no tool calls."""

    async def stream_chat(self, messages, tools=None):
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _ToolThenEmptyLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="failed_lookup",
                        name="failing_lookup",
                        arguments={"query": "weather"},
                    )
                ],
            )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _SuccessfulToolThenEmptyLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None):
        self.calls += 1
        if self.calls == 1:
            yield StreamEvent(
                type=StreamEventType.TOOL_CALL,
                tool_calls=[
                    ToolCallEvent(
                        id="successful_lookup",
                        name="successful_lookup",
                        arguments={"query": "weather"},
                    )
                ],
            )
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return ""


class _FailingLookupTool(BaseTool):
    name = "failing_lookup"
    description = "A deterministic failing lookup tool."
    read_only = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )

    async def execute(self, args, context=None):
        return ToolResult(
            content="network unavailable",
            is_error=True,
            status="failed",
        )


class _SuccessfulLookupTool(BaseTool):
    name = "successful_lookup"
    description = "A deterministic successful lookup tool."
    read_only = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={"type": "object", "properties": {"query": {"type": "string"}}},
        )

    async def execute(self, args, context=None):
        return ToolResult(
            content="weather: cloudy",
            status="success",
        )


def _run(llm: LLMAdapter, *, max_iterations: int = 5):
    async def _go():
        td = tempfile.mkdtemp()
        settings = AgentSettings(max_iterations=max_iterations)
        registry = ToolRegistry()
        registry.register(_FailingLookupTool())
        registry.register(_SuccessfulLookupTool())
        events = []
        async for ev in run_agent_loop(
            user_message="你好",
            llm=llm,
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=td),
            permission_checker=PermissionChecker(settings=PermissionSettings(), workspace_root=Path(td)),
            agent_settings=settings,
            permission_context=PermissionContext(mode="bypass"),
        ):
            events.append(ev)
        return events

    return asyncio.run(_go())


def test_first_turn_empty_reply_emits_error_not_silent_completion():
    events = _run(_EmptyStreamLLM())
    types = [getattr(e, "type", None) for e in events]

    error_events = [e for e in events if getattr(e, "type", None) == "error"]
    assert error_events, f"expected an error event, got types: {types}"
    assert error_events[0].data.get("error_type") == "empty_reply"

    # It must NOT have completed an answer item (there was no answer text).
    assert "item.completed" not in types


def test_termination_path_completes_run_record():
    """The empty-reply termination path must still commit the durable run
    record as failed (instead of leaving it stuck in "running") and project
    that commit as ``agent.run.completed``.

    That commit is QueryEngine's terminal transaction
    (``QueryEngine._finalize_query``); ``run_agent_loop`` deliberately only
    publishes ``agent.terminal.intent`` + ``done`` evidence and never emits
    ``agent.run.completed`` itself. Driving the loop kernel directly, as this
    test used to, could therefore never observe the record being committed.
    """
    tmp_path = Path(tempfile.mkdtemp())
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    registry = ToolRegistry()
    registry.register(_FailingLookupTool())
    registry.register(_SuccessfulLookupTool())
    submission = QuerySubmission(
        user_message="你好",
        session=AgentSession(
            llm=_EmptyStreamLLM(),
            tool_registry=registry,
            artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(max_iterations=5),
            token_budget=TokenBudget(),
        ),
        runtime=AgentLoopSessionContext(
            task_id="task-empty-reply",
            run_context=RunContext(agent_runtime=runtime),
            metadata={
                "permission_context": PermissionContext(mode="bypass"),
            },
        ),
    )

    async def _go():
        return [event async for event in QueryEngine().submit(submission)]

    try:
        events = asyncio.run(_go())
        types = [getattr(e, "type", None) for e in events]

        run_completed = [
            e for e in events if getattr(e, "type", None) == "agent.run.completed"
        ]
        assert run_completed, (
            f"expected agent.run.completed on the empty-reply termination path, got: {types}"
        )
        # An empty reply is a failure, not a success.
        assert run_completed[0].data.get("status") == "failed"
        # The durable record must not be left running.
        assert [
            (run["status"], run["terminal_reason"])
            for run in runtime.list_runs(conversation_id="")["runs"]
        ] == [("failed", "empty_reply")]
    finally:
        runtime.close(release_lease=True)


def test_failed_tool_then_empty_reply_terminates_without_fabricating_answer():
    events = _run(_ToolThenEmptyLLM())
    types = [getattr(e, "type", None) for e in events]

    assert "item.completed" not in types
    assert "error" in types
    assert "done" in types
    error = next(e for e in events if getattr(e, "type", None) == "error")
    assert error.data.get("error_type") == "empty_reply"


def test_successful_tool_empty_reply_still_terminates_without_answer_item():
    events = _run(_SuccessfulToolThenEmptyLLM())
    types = [getattr(event, "type", None) for event in events]

    assert "item.completed" not in types
    assert "error" in types
    assert "done" in types
