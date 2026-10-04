from __future__ import annotations

from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Callable, Iterable

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, StreamEvent
from backend.llm.base import StreamEventType, ToolCallEvent, ToolCallStartEvent
import asyncio
import pytest
from backend.agent.message import UserCommand
from backend.agent.turn_input import TurnInputQueue
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry


class BoundaryAdapter(LLMAdapter):
    """Controlled wire events; all routing/terminal/journal work stays real."""

    def __init__(self, rounds: Iterable[Iterable[StreamEvent] | Callable[[], AsyncIterator[StreamEvent]]]):
        self.rounds = iter(rounds)
        self.request_messages = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.request_messages.append(list(messages))
        events = next(self.rounds)
        if callable(events):
            async for event in events():
                yield event
        else:
            for event in events:
                yield event

    async def simple_chat(self, messages, **kwargs):
        raise AssertionError("Text-boundary validation unexpectedly requested a side model")


class BoundaryReadTool(BaseTool):
    name = "boundary_read"
    description = "Record-only read for actual text/tool item boundaries"
    permission = PermissionLevel.AUTO
    read_only = True

    def get_schema(self):
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}})

    async def execute(self, args, context=None):
        return ToolResult("BOUNDARY_OBSERVATION")


class BoundaryContext(ContextBuilder):
    def __init__(self, root: Path):
        super().__init__(conversation_id="text-boundary", workspace_root=root)
        self.citation_usage = []

    def record_memory_citation_usage(self, rollout_ids):
        self.citation_usage.append(list(rollout_ids))
        return len(rollout_ids)


@dataclass
class BoundaryQuery:
    submission: QuerySubmission
    state: AgentState
    context: BoundaryContext
    journal: ExecutionJournal
    session: AgentSession
    adapter: BoundaryAdapter


def make_boundary_query(tmp_path, rounds, *, settings=None, cancel_event=None):
    adapter = BoundaryAdapter(rounds)
    registry = ToolRegistry()
    registry.register(BoundaryReadTool())
    context = BoundaryContext(tmp_path)
    state = AgentState(user_message="Trace text boundaries", conversation_id="text-boundary")
    journal = ExecutionJournal("text-boundary-run", base_dir=tmp_path / "journal")
    session = AgentSession(
        llm=adapter, tool_registry=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=settings or AgentSettings(max_iterations=3), token_budget=TokenBudget(),
        context_builder=context,
    )
    submission = QuerySubmission(
        user_message=state.user_message, state=state, session=session,
        runtime=AgentLoopSessionContext(
            workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"),
            run_context=RunContext(execution_journal=journal, cancel_event=cancel_event),
            cancel_event=cancel_event,
        ),
    )
    return BoundaryQuery(submission, state, context, journal, session, adapter)


async def collect_boundary_query(query: BoundaryQuery) -> list[AgentEvent]:
    try:
        async with aclosing(QueryEngine().submit(query.submission)) as events:
            return [event async for event in events]
    finally:
        query.session.artifact_store.shutdown()


def _text(content, *, item="answer", phase="final_answer", lifecycle="delta"):
    return StreamEvent(type=StreamEventType.TEXT_CHUNK, content=content, item_id=item, phase=phase, lifecycle=lifecycle)


def _done():
    return StreamEvent(type=StreamEventType.DONE, finish_reason="stop")


def _citation(rollout):
    return f"<minicode-memory-citation><rollout_ids>{rollout}</rollout_ids></minicode-memory-citation>"


def _completed(events):
    return [event.data["item"] for event in events if event.type == "item.completed" and event.data["item"]["type"] == "agent_message"]


@pytest.mark.parametrize("suffix", ["`", "``", "```", "<thi", "<|"])
def test_query_done_releases_held_literal_suffix_to_original_answer_item(tmp_path, suffix):
    expected = "Literal " + suffix
    query = make_boundary_query(tmp_path, [[_text(expected), _done()]])
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == expected
    final = next(item for item in _completed(events) if item["source"] == "model_final")
    assert final["id"] == "answer"
    assert final["text"] == expected
    assert any(event.type == "agent_message.delta" and event.data["item_id"] == "answer" and event.data["delta"] == suffix for event in events)


def test_query_nonlive_done_retains_literal_suffix_without_synthetic_delta(tmp_path):
    query = make_boundary_query(tmp_path, [[_text("Literal `"), _done()]], settings=AgentSettings(max_iterations=3, live_text_streaming=False))
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == "Literal `"
    assert not any(event.type == "agent_message.delta" for event in events)
    assert _completed(events)[-1]["text"] == "Literal `"


def test_query_eof_releases_original_item_tail_before_protocol_failure_without_accepting_answer(tmp_path):
    query = make_boundary_query(tmp_path, [[_text("Interrupted <thi")]])
    events = asyncio.run(collect_boundary_query(query))
    tail = next(index for index, event in enumerate(events) if event.type == "agent_message.delta" and event.data["delta"] == "<thi")
    error = next(index for index, event in enumerate(events) if event.type == "error" and event.data.get("error_code") == "provider_terminal_missing")
    assert events[tail].data["item_id"] == "answer"
    assert tail < error
    assert query.state.stopped_reason == "provider_terminal_missing"
    assert not any(item.get("source") == "model_final" and item.get("status") == "completed" for item in _completed(events))


@pytest.mark.parametrize("live", [True, False])
def test_query_distinct_final_item_does_not_inherit_commentary_fence_and_records_real_citation(tmp_path, live):
    query = make_boundary_query(tmp_path, [[
        _text("```md\n<thinking>literal code sample</thinking>\n", item="commentary", phase="commentary"),
        _text("Visible<thinking>private reasoning</thinking>" + _citation("real-rollout"), item="final"), _done(),
    ]], settings=AgentSettings(max_iterations=3, live_text_streaming=live))
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == "Visible"
    assert query.context.citation_usage == [["real-rollout"]]
    assert all("private reasoning" not in event.data.get("delta", "") for event in events if event.type in {"agent_message.delta", "agent.item.delta"})
    final = _completed(events)[-1]
    assert final["text"] == "Visible"
    assert final["id"] == "final"


def test_query_same_item_phase_only_update_preserves_inline_code_citation_literal(tmp_path):
    sample = _citation("literal-rollout")
    query = make_boundary_query(tmp_path, [[
        _text("`literal ", phase=""), _text("", lifecycle="end"), _text(sample + "`"), _done(),
    ]])
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == "`literal " + sample + "`"
    assert query.context.citation_usage == []
    assert _completed(events)[-1]["id"] == "answer"


def test_query_same_item_phase_only_update_does_not_flush_partial_reasoning_opener(tmp_path):
    query = make_boundary_query(tmp_path, [[
        _text("<thi", phase=""), _text("", lifecycle="end"), _text("nking>private reasoning</thinking>Visible"), _done(),
    ]])
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == "Visible"
    assert all("private reasoning" not in event.data.get("delta", "") for event in events if event.type in {"agent_message.delta", "agent.item.delta"})


def test_query_text_tail_precedes_tool_announcement_and_stays_with_commentary_owner(tmp_path):
    query = make_boundary_query(tmp_path, [[
        _text("Inspect <thi", item="preamble", phase=""),
        StreamEvent(type=StreamEventType.TOOL_CALL_START, tool_call_start=ToolCallStartEvent(id="read", name="boundary_read")),
        StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="read", name="boundary_read", arguments={})]),
        _done(),
    ], [_text("Completed", item="result"), _done()]])
    events = asyncio.run(collect_boundary_query(query))
    tail = next(index for index, event in enumerate(events) if event.type == "agent_message.delta" and event.data.get("delta") == "<thi")
    tool = next(index for index, event in enumerate(events) if event.type == "tool_call")
    assert events[tail].data["item_id"] == "preamble"
    assert tail < tool
    assert any(item["id"] == "preamble" and item["source"] == "commentary" and item["text"].endswith("<thi") for item in _completed(events))
    assert query.state.reply == "Completed"


def test_query_retry_discards_old_tail_and_citation_without_relabeling_successful_item(tmp_path):
    query = make_boundary_query(tmp_path, [[
        _text("Rejected" + _citation("rejected-rollout") + "<thi", item="rejected", phase=""),
        StreamEvent(type=StreamEventType.ERROR, content="Connection reset by peer", raw={"exception_type": "ConnectionError", "provider_error_type": "network", "error_type": "network"}),
    ], [_text("Recovered", item="recovered"), _done()]], settings=AgentSettings(max_iterations=3, stream_max_attempts=2, stream_retry_delay_seconds=0))
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == "Recovered"
    assert query.context.citation_usage == []
    assert len(query.adapter.request_messages) == 2
    assert any(item["id"] == "rejected" and item["status"] == "cancelled" and item["text"] == "" for item in _completed(events))
    assert _completed(events)[-1]["id"] == "recovered"
    assert _completed(events)[-1]["text"] == "Recovered"


def test_query_cancel_releases_tail_only_to_original_cancelled_item(tmp_path):
    cancel = asyncio.Event()

    async def cancel_round():
        yield _text("Cancelled <thi", item="cancelled-item", phase="")
        cancel.set()
        yield _text("Must not be projected", item="foreign-item")

    query = make_boundary_query(tmp_path, [cancel_round], cancel_event=cancel)
    async def cancelled_events():
        captured = []
        try:
            async with aclosing(QueryEngine().submit(query.submission)) as stream:
                with pytest.raises(asyncio.CancelledError):
                    async for event in stream:
                        captured.append(event)
            return captured
        finally:
            query.session.artifact_store.shutdown()

    events = asyncio.run(cancelled_events())
    assert any(item["id"] == "cancelled-item" and item["status"] == "cancelled" and item["text"] == "Cancelled <thi" for item in _completed(events))
    assert not any(item["id"] == "foreign-item" for item in _completed(events))
    assert query.context.citation_usage == []


def test_query_new_final_item_receives_no_tail_from_previous_commentary_owner(tmp_path):
    query = make_boundary_query(tmp_path, [[
        _text("Commentary `", item="commentary", phase="commentary"),
        _text("Final", item="final"), _done(),
    ]])
    events = asyncio.run(collect_boundary_query(query))
    original = next(event.data for event in events if event.type == "agent.item" and event.data["source"] == "commentary")
    tail = next(index for index, event in enumerate(events) if event.type == "agent.item.delta" and event.data["item_id"] == original["id"] and event.data["delta"].endswith("`"))
    new_item = next(index for index, event in enumerate(events) if event.type == "item.started" and event.data["item"]["id"] == "final")
    assert tail < new_item
    assert query.state.reply == "Final"
    assert _completed(events)[-1]["text"] == "Final"


@pytest.mark.parametrize("prefix", ["`", "`literal "])
def test_query_unphased_transport_id_continuation_preserves_original_parser_and_logical_item(tmp_path, prefix):
    sample = _citation("literal-transport")
    query = make_boundary_query(tmp_path, [[
        _text(prefix, item="first-transport", phase=""),
        _text(sample + "`", item="later-transport", phase=""), _done(),
    ]])
    events = asyncio.run(collect_boundary_query(query))
    assert query.state.reply == prefix + sample + "`"
    assert query.context.citation_usage == []
    assert _completed(events)[-1]["id"] == "first-transport"


def test_query_steer_releases_held_delimiter_before_cancelling_original_item(tmp_path):
    inputs = TurnInputQueue()

    async def steered_round():
        yield _text("Discarded ", item="steered-item", phase="")
        assert inputs.enqueue_command(UserCommand("user_message", {
            "content": "Use the updated request", "conversation_id": "text-boundary",
            "user_message_id": "steer-user", "assistant_message_id": "steer-answer",
        })) is not None
        yield _text("`", item="steered-item", phase="")
        yield _text("Must not be acquired", item="foreign-item")

    query = make_boundary_query(tmp_path, [steered_round, [_text("Steered reply", item="steer-result"), _done()]])
    query.submission.runtime.run_context.turn_input_queue = inputs
    events = asyncio.run(collect_boundary_query(query))
    assert any(item["id"] == "steered-item" and item["status"] == "cancelled" and item["text"] == "Discarded `" for item in _completed(events))
    assert any(event.type == "agent_message.delta" and event.data["item_id"] == "steered-item" and event.data["delta"] == "`" for event in events)
    assert query.state.reply == "Steered reply"
    assert _completed(events)[-1]["id"] == "steer-result"
