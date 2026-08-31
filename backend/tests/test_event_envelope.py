"""Tests for the EventEnvelope stamping mechanism."""

from __future__ import annotations

import asyncio
from pathlib import Path

from backend.agent.event_envelope import EventEnvelope
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


def test_envelope_stamps_task_id_and_seq() -> None:
    envelope = EventEnvelope(task_id="task_abc", conversation_id="conv_123")
    e1 = AgentEvent.agent_message_completed("hello")
    e2 = AgentEvent.tool_call("tc_1", "read_file", {"path": "a.py"})

    envelope.stamp(e1)
    envelope.stamp(e2)

    assert e1.data.get("task_id") == "task_abc"
    assert e1.data.get("conversation_id") == "conv_123"
    assert e1.data.get("seq") == 1
    assert e2.data.get("task_id") == "task_abc"
    assert e2.data.get("seq") == 2


def test_envelope_captures_turn_id_from_run_started() -> None:
    envelope = EventEnvelope(task_id="task_abc", conversation_id="conv_123")
    assert envelope.turn_id == ""

    run_started = AgentEvent.agent_run_started({"run_id": "run_xyz"})
    envelope.stamp(run_started)

    assert envelope.turn_id == "run_xyz"
    assert run_started.data.get("turn_id") == "run_xyz"

    # Subsequent events should get the captured turn_id.
    text = AgentEvent.agent_message_completed("hello")
    envelope.stamp(text)
    assert text.data.get("turn_id") == "run_xyz"


def test_envelope_does_not_override_existing_turn_id() -> None:
    envelope = EventEnvelope(task_id="task_abc", conversation_id="conv_123")
    # Capture turn_id from run.started
    envelope.stamp(AgentEvent.agent_run_started({"run_id": "run_xyz"}))

    # tool_call already has its own turn_id — setdefault must not override.
    tool_event = AgentEvent.tool_call(
        "tc_1", "read_file", {"path": "a.py"}, turn_id="custom_turn"
    )
    envelope.stamp(tool_event)
    assert tool_event.data.get("turn_id") == "custom_turn"


def test_envelope_skips_adapter_internal_events() -> None:
    envelope = EventEnvelope(task_id="task_abc", conversation_id="conv_123")
    internal = AgentEvent(type="tool_call_start", data={"id": "tc_1"})
    envelope.stamp(internal)
    assert "task_id" not in internal.data
    assert "seq" not in internal.data


def test_envelope_stamps_error_and_done_events() -> None:
    envelope = EventEnvelope(task_id="task_abc", conversation_id="conv_123")
    envelope.stamp(AgentEvent.agent_run_started({"run_id": "run_xyz"}))

    err = AgentEvent.error("something went wrong")
    done = AgentEvent.done(status="failed", reason="error")

    envelope.stamp(err)
    envelope.stamp(done)

    assert err.data.get("turn_id") == "run_xyz"
    assert err.data.get("task_id") == "task_abc"
    assert err.data.get("seq") == 2
    assert done.data.get("turn_id") == "run_xyz"
    assert done.data.get("seq") == 3


def test_envelope_seq_is_monotonic() -> None:
    envelope = EventEnvelope(task_id="t", conversation_id="c")
    seqs: list[int] = []
    for _ in range(5):
        e = AgentEvent.agent_message_completed("x")
        envelope.stamp(e)
        seqs.append(e.data["seq"])
    assert seqs == [1, 2, 3, 4, 5]


def test_tool_result_serializes_activity_kind() -> None:
    event = AgentEvent.tool_result(
        "tc_read",
        "Read README.md",
        result_kind="file",
        activity_kind="file_read",
    )

    assert event.data["result_kind"] == "file"
    assert event.data["activity_kind"] == "file_read"


def test_query_engine_stamps_envelope_on_events(tmp_path: Path) -> None:
    """Integration: QueryEngine.submit stamps task_id, turn_id, seq."""

    async def fake_runner(**kwargs):
        yield AgentEvent.agent_run_started({"run_id": "run_test_1"})
        yield AgentEvent.agent_message_completed("hello")
        yield AgentEvent.tool_call("tc_1", "read_file", {"path": "a.py"})
        yield AgentEvent.tool_result("tc_1", "ok")
        yield AgentEvent.done(status="completed")

    engine = QueryEngine(runner=fake_runner)
    state = AgentState(user_message="test", max_iterations=1)
    state.conversation_id = "conv_test"

    submission = QuerySubmission(
        user_message="test",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=object(),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(),
            token_budget=TokenBudget(),
        ),
        state=state,
        runtime=AgentLoopSessionContext(task_id="task_123"),
    )

    async def collect():
        return [event async for event in engine.submit(submission)]

    events = asyncio.run(collect())

    # All events should have seq and task_id.
    seqs = [e.data.get("seq") for e in events if e.type != "done" or True]
    assert all(s is not None and isinstance(s, int) for s in seqs)
    assert seqs == sorted(seqs)

    # QueryEngine owns the durable run id and stamps it on every event.
    for event in events:
        assert event.data.get("task_id") == "task_123"
        assert event.data.get("conversation_id") == "conv_test"

    # The first event (agent.run.started) captures turn_id.
    run_started = next(e for e in events if e.type == "agent.run.started")
    assert run_started.data.get("turn_id") == run_started.data.get("run_id")

    # Subsequent events should have the captured turn_id.
    completed = next(e for e in events if e.type == "item.completed")
    assert completed.data.get("turn_id") == run_started.data.get("run_id")


def test_query_engine_rejects_concurrent_prompts_on_one_agent_session(
    tmp_path: Path,
) -> None:
    release = asyncio.Event()

    async def blocking_runner(**kwargs):
        await release.wait()
        yield AgentEvent.done(status="completed")

    shared_session = AgentSession(
        llm=object(),
        tool_registry=ToolRegistry(),
        artifact_store=object(),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(),
        token_budget=TokenBudget(),
    )
    engine = QueryEngine(runner=blocking_runner)

    async def scenario() -> None:
        first = engine.submit(
            QuerySubmission(
                user_message="first",
                session=shared_session,
                state=AgentState(user_message="first", max_iterations=1),
            )
        )
        await anext(first)
        second = engine.submit(
            QuerySubmission(
                user_message="second",
                session=shared_session,
                state=AgentState(user_message="second", max_iterations=1),
            )
        )
        try:
            await anext(second)
        except RuntimeError as exc:
            assert "already processing" in str(exc)
        else:
            raise AssertionError("concurrent prompt should be rejected")
        finally:
            await second.aclose()
            release.set()
            await first.aclose()
        assert shared_session.active_turn is False

    asyncio.run(scenario())


def test_query_engine_does_not_complete_after_published_error(tmp_path: Path) -> None:
    async def fake_runner(**kwargs):
        yield AgentEvent.agent_run_started({"run_id": "run_error_then_done"})
        yield AgentEvent.error("provider stream failed", error_type="api")
        yield AgentEvent.done(status="failed", reason="api_error")

    engine = QueryEngine(runner=fake_runner)
    state = AgentState(user_message="test", max_iterations=1)
    state.conversation_id = "conv_error_then_done"
    submission = QuerySubmission(
        user_message="test",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=object(),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(),
            token_budget=TokenBudget(),
        ),
        state=state,
        runtime=AgentLoopSessionContext(task_id="task_error_then_done"),
    )

    async def collect():
        return [event async for event in engine.submit(submission)]

    events = asyncio.run(collect())
    completed = next(event for event in events if event.type == "agent.run.completed")
    done = next(event for event in events if event.type == "done")
    assert completed.data["status"] == "failed"
    assert done.data["status"] == "failed"
    assert done.data["reason"] == "api_error"


def test_query_engine_consumer_close_finalizes_running_record(tmp_path: Path) -> None:
    async def fake_runner(**kwargs):
        yield AgentEvent.agent_message_completed("never reached before close")
        await asyncio.sleep(60)

    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
    )
    engine = QueryEngine(runner=fake_runner)
    state = AgentState(user_message="test", max_iterations=1)
    state.conversation_id = "conv_close"
    submission = QuerySubmission(
        user_message="test",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=object(),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(),
            token_budget=TokenBudget(),
        ),
        state=state,
        runtime=AgentLoopSessionContext(
            task_id="task_close",
            run_context=RunContext(agent_runtime=runtime),
        ),
    )

    async def close_after_first_event() -> str:
        stream = engine.submit(submission)
        first = await anext(stream)
        run_id = str(first.data["run_id"])
        await stream.aclose()
        return run_id

    run_id = asyncio.run(close_after_first_event())
    record = runtime.get_run(run_id)
    assert record is not None
    assert record.status == "cancelled"
    assert record.summary == "consumer_closed"


def test_query_engine_stamps_envelope_on_cancelled(tmp_path: Path) -> None:
    """Cancellation is a dedicated done state, not an error flash."""

    async def fake_runner(**kwargs):
        yield AgentEvent.agent_run_started({"run_id": "run_cancel"})
        yield AgentEvent.agent_message_completed(
            "partial", source="partial", status="partial"
        )
        raise asyncio.CancelledError()

    engine = QueryEngine(runner=fake_runner)
    state = AgentState(user_message="test", max_iterations=1)
    state.conversation_id = "conv_cancel"

    submission = QuerySubmission(
        user_message="test",
        session=AgentSession(
            llm=object(),
            tool_registry=ToolRegistry(),
            artifact_store=object(),
            permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
            agent_settings=AgentSettings(),
            token_budget=TokenBudget(),
        ),
        state=state,
        runtime=AgentLoopSessionContext(task_id="task_cancel"),
    )

    async def collect():
        events = []
        try:
            async for event in engine.submit(submission):
                events.append(event)
        except asyncio.CancelledError:
            pass
        return events

    events = asyncio.run(collect())

    # User cancellation should not be surfaced as a transient red error.
    error_events = [e for e in events if e.type == "error"]
    done_events = [e for e in events if e.type == "done"]
    assert error_events == []
    assert len(done_events) == 1
    assert done_events[0].data.get("task_id") == "task_cancel"
    started = next(event for event in events if event.type == "agent.run.started")
    assert done_events[0].data.get("turn_id") == started.data.get("run_id")
    assert done_events[0].data.get("status") == "cancelled"
