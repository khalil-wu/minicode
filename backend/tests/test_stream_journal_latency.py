from __future__ import annotations

import asyncio
import threading

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.loop import AgentLoopSessionContext
from backend.agent.message import AgentEvent
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.permissions.checker import PermissionChecker
from backend.tools.registry import ToolRegistry


class SlowJournal:
    def __init__(self, *, fail=False):
        self.started = threading.Event()
        self.release = threading.Event()
        self.entries = []
        self.fail = fail

    def append(self, kind, payload):
        if payload.get("status") == "running":
            self.started.set()
            assert self.release.wait(5)
            if self.fail:
                raise OSError("actual progress write failed")
        self.entries.append((kind, payload))


def recorder(journal):
    return QueryJournalRecorder(journal=journal, metadata={}, state=AgentState(user_message="test"),
        context_builder=ContextBuilder(), turn_kernel=None, conversation_id="owner")


@pytest.mark.asyncio
async def test_live_deltas_do_not_wait_for_disk_and_completion_keeps_journal_order():
    journal = SlowJournal()
    owner = recorder(journal)
    await owner.record_event_async(AgentEvent.agent_message_started())
    await owner.record_event_async(AgentEvent.agent_message_delta("first"))
    assert await asyncio.to_thread(journal.started.wait, 2)
    try:
        await asyncio.sleep(.13)
        await asyncio.wait_for(owner.record_event_async(AgentEvent.agent_message_delta(" second")), 1)
        await asyncio.sleep(.13)
        await asyncio.wait_for(owner.record_event_async(AgentEvent.agent_message_delta(" third")), 1)
        completion = asyncio.create_task(owner.record_event_async(
            AgentEvent.agent_message_completed("first second third", source="model_final")))
        await asyncio.sleep(0)
        assert not completion.done()
    finally:
        journal.release.set()
    await completion
    assert [(payload.get("status"), payload.get("content_delta"), payload.get("content_offset"))
        for _, payload in journal.entries[:2]] == [("running", "first", 0), ("running", " second third", 5)]
    assert journal.entries[-1][1]["content"] == "first second third"
    assert journal.entries[-1][1]["status"] == "completed"


@pytest.mark.asyncio
async def test_progress_write_error_is_reported_at_the_next_durable_boundary():
    journal = SlowJournal(fail=True)
    owner = recorder(journal)
    await owner.record_event_async(AgentEvent.agent_message_started())
    await owner.record_event_async(AgentEvent.agent_message_delta("partial"))
    assert await asyncio.to_thread(journal.started.wait, 2)
    journal.release.set()
    with pytest.raises(OSError, match="actual progress write failed"):
        await owner.record_event_async(AgentEvent.agent_message_completed("partial", source="model_final"))
    assert journal.entries == []


@pytest.mark.asyncio
async def test_cancelling_a_waiter_does_not_cancel_the_owned_progress_write():
    journal = SlowJournal()
    owner = recorder(journal)
    await owner.record_event_async(AgentEvent.agent_message_started())
    await owner.record_event_async(AgentEvent.agent_message_delta("partial"))
    assert await asyncio.to_thread(journal.started.wait, 2)
    waiter = asyncio.create_task(owner.flush_progress())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    journal.release.set()
    await owner.flush_progress()
    assert journal.entries[0][1]["content_delta"] == "partial"


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["completed", "cancelled", "consumer_closed", "write_failed"])
async def test_query_terminal_waits_for_owned_stream_write_and_preserves_failure(tmp_path, monkeypatch, ending):
    journal = ExecutionJournal("stream-owner", base_dir=tmp_path / "journals")
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    started, release = threading.Event(), threading.Event()
    append = journal.append

    def gated_append(kind, payload):
        if kind == "progress" and payload.get("status") == "running":
            started.set()
            assert release.wait(5)
            if ending == "write_failed":
                raise OSError("actual progress write failed")
        return append(kind, payload)

    monkeypatch.setattr(journal, "append", gated_append)
    visible = asyncio.Event()
    events = []

    async def runner(**kwargs):
        kwargs["state"].reply = "visible partial"
        yield AgentEvent.agent_message_started()
        yield AgentEvent.agent_message_delta("visible partial")
        if ending == "cancelled":
            await asyncio.Event().wait()
        yield AgentEvent.agent_message_completed("visible partial", source="model_final")
        yield AgentEvent.done(status="completed")

    submission = QuerySubmission(user_message="stream", session=AgentSession(
        llm=object(), tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(max_iterations=1), token_budget=TokenBudget()),
        runtime=AgentLoopSessionContext(task_id="stream-task",
            run_context=RunContext(agent_runtime=runtime, execution_journal=journal)))

    async def consume():
        stream = QueryEngine(runner=runner).submit(submission)
        async for event in stream:
            events.append(event)
            if event.type == "agent_message.delta":
                visible.set()
                if ending == "consumer_closed":
                    await stream.aclose()
                    return

    task = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(visible.wait(), 2)
        assert await asyncio.to_thread(started.wait, 2)
        if ending == "cancelled":
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not any(event.type in {"agent.run.completed", "done"} for event in events)
        release.set()
        if ending == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task

        entries = journal.read_events()
        expected_status = "completed" if ending == "completed" else "failed" if ending == "write_failed" else "cancelled"
        assert runtime.list_runs()["runs"][0]["status"] == expected_status
        terminals = [entry for entry in entries if entry.event_type == "terminal"]
        assert len(terminals) == 1
        assert terminals[0].payload["status"] == expected_status
        if ending == "write_failed":
            journal_error = next(event for event in events if event.data.get("error_code") == "runtime.terminal_journal_failed")
            assert "actual progress write failed" in journal_error.data["message"]
        else:
            progress = next(entry for entry in entries if entry.payload.get("content_delta") == "visible partial")
            intent = next(entry for entry in entries if entry.payload.get("lifecycle") == "terminal_intent")
            assert progress.seq < intent.seq < terminals[0].seq
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        runtime.close(release_lease=True)
