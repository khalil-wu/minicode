"""Sidechain journal, parent outbox, cancel/detach, and checkpoint durability."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from backend.agent.checkpoint import (
    CheckpointCorruptionError,
    load_latest_checkpoint,
    save_checkpoint,
)
from backend.agent.context import ContextBuilder
from backend.agent.mailbox_delivery import (
    format_parent_notification_message as _format_parent_notification_message,
    inject_parent_notifications as _inject_parent_notifications,
)
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.execution_journal import (
    ExecutionJournal,
    ExecutionJournalCorruptionError,
    ExecutionJournalError,
    load_agent_transcript,
)
from backend.agent.parent_notification_outbox import (
    ParentNotificationOutbox,
    ParentNotificationOutboxCorruptionError,
    enqueue_parent_notification,
)
from backend.agent.runtime import AgentRuntime
from backend.llm.base import ToolCallEvent
from backend.tools.agent_tools import _subagent_metadata


def _subagent_fence(runtime: AgentRuntime, subagent_id: str) -> dict[str, object]:
    record = runtime.get_subagent(subagent_id)
    assert record is not None
    return {"agent_path": record.agent_path, "mailbox_epoch": record.mailbox_epoch}


def test_execution_journal_append_reconstruct_and_close(tmp_path: Path) -> None:
    journal = ExecutionJournal("agent-a", base_dir=tmp_path / "sidechains")
    journal.append("user_prompt", {"content": "inspect module"})
    journal.append(
        "tool_use",
        {
            "tool_call": {
                "id": "call_1",
                "name": "read_file",
                "arguments": {"path": "a.py"},
            }
        },
    )
    journal.append(
        "tool_result",
        {
            "tool_call_id": "call_1",
            "tool_name": "read_file",
            "content": "print('hi')",
        },
    )
    journal.append(
        "tool_use",
        {
            "tool_call": {
                "id": "call_2",
                "name": "grep",
                "arguments": {"pattern": "hi"},
            }
        },
    )
    assert [item["tool_call_id"] for item in journal.unresolved_tool_uses()] == ["call_2"]
    closed = journal.close_unresolved_tool_uses(reason="cancelled")
    assert len(closed) == 1
    assert closed[0].payload["status"] == "cancelled"
    assert closed[0].payload["synthetic"] is True
    assert journal.unresolved_tool_uses() == []
    journal.append_terminal(status="cancelled", summary="stopped early", reason="cancelled")

    transcript = load_agent_transcript("agent-a", base_dir=tmp_path / "sidechains")
    history = transcript["history"]
    assert history[0]["role"] == "user"
    assert any(item.get("tool_call_id") == "call_2" for item in history if item["role"] == "tool")
    assert transcript["events"][-1]["event_type"] == "terminal"


def test_execution_journal_append_once_rejects_payload_rebinding(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal(
        "agent-append-once",
        base_dir=tmp_path / "sidechains",
    )
    payload = {
        "content": "first prompt",
        "context_snapshot": {"history": [{"role": "user", "content": "first prompt"}]},
    }

    first = journal.append_once(
        "user_prompt",
        payload,
        event_id="stable-user-prompt",
    )
    replay = journal.append_once(
        "user_prompt",
        payload,
        event_id="stable-user-prompt",
    )

    assert replay == first
    assert len(journal.read_events()) == 1
    with pytest.raises(ExecutionJournalError, match="changed payload"):
        journal.append_once(
            "user_prompt",
            {
                "content": "different prompt",
                "context_snapshot": {
                    "history": [{"role": "user", "content": "different prompt"}]
                },
            },
            event_id="stable-user-prompt",
        )
    assert journal.read_events() == [first]


def test_execution_journal_corruption_blocks_read_and_append(tmp_path: Path) -> None:
    journal = ExecutionJournal("agent-corrupt", base_dir=tmp_path / "sidechains")
    journal.append("user_prompt", {"content": "preserve me"})
    with journal.path.open("a", encoding="utf-8") as handle:
        handle.write("{not-json\n")
    corrupt_bytes = journal.path.read_bytes()

    with pytest.raises(ExecutionJournalCorruptionError):
        journal.read_events()
    with pytest.raises(ExecutionJournalCorruptionError):
        journal.append("system", {"content": "must not append"})

    assert journal.path.read_bytes() == corrupt_bytes


def test_execution_journal_reuses_validated_chain_for_owned_appends(
    tmp_path: Path,
    monkeypatch,
) -> None:
    journal = ExecutionJournal("agent-cached-chain", base_dir=tmp_path / "sidechains")
    journal.append("user_prompt", {"content": "one"})
    original_read = journal._read_events_unlocked
    full_reads = 0

    def tracked_read():
        nonlocal full_reads
        full_reads += 1
        return original_read()

    monkeypatch.setattr(journal, "_read_events_unlocked", tracked_read)

    journal.append("assistant", {"content": "two"})
    journal.append("system", {"content": "three"})
    events = journal.read_events()

    assert full_reads == 0
    assert [event.seq for event in events] == [1, 2, 3]


def test_execution_journal_rejects_noncontiguous_sequence(tmp_path: Path) -> None:
    journal = ExecutionJournal("agent-sequence", base_dir=tmp_path / "sidechains")
    journal.append("user_prompt", {"content": "one"})
    journal.append("assistant", {"content": "two"})
    records = [
        json.loads(line)
        for line in journal.path.read_text(encoding="utf-8").splitlines()
    ]
    records[1]["seq"] = 1
    journal.path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ExecutionJournalCorruptionError, match="not contiguous"):
        journal.read_events()


def test_legacy_and_current_reasoning_effort_keys_resume_identically() -> None:
    assert _subagent_metadata({"effort": "low"})["effort"] == "low"
    assert _subagent_metadata({"reasoning_effort": "high"})["effort"] == "high"
    assert _subagent_metadata({
        "effort": "low",
        "reasoning_effort": "high",
    })["effort"] == "high"


def test_transcript_only_process_text_is_replayable_but_not_provider_history(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal("agent-process", base_dir=tmp_path / "sidechains")
    journal.append("user_prompt", {"content": "inspect"})
    journal.append("system", {
        "kind": "process_text",
        "content": "Reading the implementation",
        "transcript_only": True,
    })
    journal.append("system", {"content": "provider-visible system fact"})

    transcript = load_agent_transcript("agent-process", base_dir=tmp_path / "sidechains")

    assert any(
        event["payload"].get("content") == "Reading the implementation"
        for event in transcript["events"]
    )
    assert transcript["history"] == [
        {"role": "user", "content": "inspect"},
        {"role": "system", "content": "provider-visible system fact"},
    ]


def test_parent_outbox_idempotency_ack_and_replay(tmp_path: Path) -> None:
    first = enqueue_parent_notification(
        parent_run_id="run_parent",
        conversation_id="conv_1",
        subagent_id="sub_1",
        payload={"status": "completed", "content": "done"},
        base_dir=tmp_path / "outbox",
    )
    second = enqueue_parent_notification(
        parent_run_id="run_parent",
        conversation_id="conv_1",
        subagent_id="sub_1",
        payload={"status": "completed", "content": "done again"},
        base_dir=tmp_path / "outbox",
    )
    assert first.notification_id == second.notification_id
    assert second.status == "pending"

    outbox = ParentNotificationOutbox(
        parent_run_id="run_parent",
        conversation_id="conv_1",
        base_dir=tmp_path / "outbox",
    )
    delivered = outbox.mark_delivered(first.notification_id)
    assert delivered is not None
    assert delivered.status == "delivered"
    # delivered means injected into an in-memory prompt. It remains durable
    # and replayable until a non-retry provider request acknowledges it.
    assert [item.notification_id for item in outbox.replayable()] == [first.notification_id]
    acked = outbox.ack(first.notification_id)
    assert acked is not None
    assert acked.status == "acked"
    assert outbox.pending() == []
    assert outbox.replayable() == []

    failed = enqueue_parent_notification(
        parent_run_id="run_parent",
        conversation_id="conv_1",
        subagent_id="sub_2",
        payload={"status": "failed"},
        base_dir=tmp_path / "outbox",
    )
    outbox.mark_failed(failed.notification_id, "delivery boom")
    replayable = outbox.replayable()
    assert [item.subagent_id for item in replayable] == ["sub_2"]


def test_parent_outbox_corruption_fails_closed_without_overwriting(tmp_path: Path) -> None:
    outbox = ParentNotificationOutbox(
        parent_run_id="run-corrupt",
        conversation_id="conv-corrupt",
        base_dir=tmp_path / "outbox",
    )
    outbox.enqueue(subagent_id="sub-1", payload={"content": "durable result"})
    outbox.path.write_text("{not-json", encoding="utf-8")
    corrupt_bytes = outbox.path.read_bytes()

    with pytest.raises(ParentNotificationOutboxCorruptionError):
        outbox.list_notifications()
    with pytest.raises(ParentNotificationOutboxCorruptionError):
        outbox.enqueue(subagent_id="sub-2")

    assert outbox.path.read_bytes() == corrupt_bytes


def test_parent_outbox_rejects_cross_conversation_entries(tmp_path: Path) -> None:
    outbox = ParentNotificationOutbox(
        parent_run_id="run-owner",
        conversation_id="conv-owner",
        base_dir=tmp_path / "outbox",
    )
    item = outbox.enqueue(subagent_id="sub-owner")
    payload = json.loads(outbox.path.read_text(encoding="utf-8"))
    payload["notifications"][0]["conversation_id"] = "conv-other"
    outbox.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ParentNotificationOutboxCorruptionError):
        outbox.ack(item.notification_id)


def test_parent_outbox_rejects_reused_notification_id(tmp_path: Path) -> None:
    outbox = ParentNotificationOutbox(
        parent_run_id="run-ids",
        conversation_id="conv-ids",
        base_dir=tmp_path / "outbox",
    )
    outbox.enqueue(
        subagent_id="sub-1",
        idempotency_key="completion:sub-1",
        notification_id="notification-fixed",
    )

    with pytest.raises(ValueError, match="notification_id is already used"):
        outbox.enqueue(
            subagent_id="sub-2",
            idempotency_key="completion:sub-2",
            notification_id="notification-fixed",
        )

    assert [item.subagent_id for item in outbox.list_notifications()] == ["sub-1"]


def test_runtime_detach_survives_parent_cancel(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    parent = runtime.start_run(conversation_id="conv", role="main")
    linked = runtime.start_subagent(
        subagent_id="linked",
        parent_run_id=parent.run_id,
        agent_type="explore",
        cancel_with_parent=True,
    )
    detached = runtime.start_subagent(
        subagent_id="detached",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        detach_from_parent=True,
    )
    assert linked.cancel_with_parent is True
    assert detached.detach_from_parent is True
    assert detached.cancel_with_parent is False

    linked_cancel = asyncio.Event()
    detached_cancel = asyncio.Event()

    async def _scenario() -> None:
        async def _hang() -> None:
            await asyncio.Event().wait()

        linked_task = asyncio.create_task(_hang())
        detached_task = asyncio.create_task(_hang())
        runtime.register_subagent_task(
            "linked",
            linked_task,
            cancel_event=linked_cancel,
            parent_run_id=parent.run_id,
        )
        runtime.register_subagent_task(
            "detached",
            detached_task,
            cancel_event=detached_cancel,
            parent_run_id=parent.run_id,
        )

        cancelled = runtime.cancel_child_subagent_tasks(parent.run_id)
        assert "linked" in cancelled
        assert "detached" not in cancelled
        assert linked_cancel.is_set()
        assert not detached_cancel.is_set()

        linked_task.cancel()
        detached_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await linked_task
        with pytest.raises(asyncio.CancelledError):
            await detached_task

    asyncio.run(_scenario())


def test_task_level_parent_cancel_preserves_detached_child(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm-task-cancel")
    parent = runtime.start_run(
        conversation_id="conv-task-cancel",
        role="main",
        task_id="parent-task",
    )
    runtime.start_subagent(
        subagent_id="task-linked",
        parent_run_id=parent.run_id,
        agent_type="explore",
        cancel_with_parent=True,
    )
    runtime.start_subagent(
        subagent_id="task-detached",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        detach_from_parent=True,
    )

    async def _scenario() -> None:
        async def _hang() -> None:
            await asyncio.Event().wait()

        linked_event = asyncio.Event()
        detached_event = asyncio.Event()
        linked_task = asyncio.create_task(_hang())
        detached_task = asyncio.create_task(_hang())
        runtime.register_subagent_task(
            "task-linked",
            linked_task,
            cancel_event=linked_event,
            parent_run_id=parent.run_id,
            owner_task_id="parent-task",
        )
        runtime.register_subagent_task(
            "task-detached",
            detached_task,
            cancel_event=detached_event,
            parent_run_id=parent.run_id,
            owner_task_id="parent-task",
        )

        cancelled = runtime.cancel_child_subagent_tasks_for_task("parent-task")
        assert cancelled == ["task-linked"]
        assert linked_event.is_set()
        assert not detached_event.is_set()
        assert linked_task.cancelled() or linked_task.cancelling()
        assert not detached_task.done()

        detached_task.cancel()
        await asyncio.gather(linked_task, detached_task, return_exceptions=True)

    asyncio.run(_scenario())


def test_runtime_store_result_enqueues_parent_notification(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    parent = runtime.start_run(conversation_id="conv-notify", role="main")
    runtime.start_subagent(
        subagent_id="worker-1",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        detach_from_parent=True,
    )
    result = runtime.store_subagent_result(
        "worker-1",
        status="completed",
        content="ok",
        duration_ms=12,
        iterations=2,
        tool_call_count=1,
        **_subagent_fence(runtime, "worker-1"),
    )
    assert result.status == "completed"
    # Outbox is conversation-scoped so later parent turns can drain it.
    pending = runtime.list_parent_notifications(conversation_id="conv-notify")
    assert len(pending) == 1
    assert pending[0]["subagent_id"] == "worker-1"
    assert pending[0]["status"] == "pending"
    acked = runtime.ack_parent_notification(
        pending[0]["notification_id"],
        conversation_id="conv-notify",
    )
    assert acked is not None
    assert acked["status"] == "acked"


def test_runtime_store_result_skips_outbox_for_sync_subagent(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    parent = runtime.start_run(conversation_id="conv-sync", role="main")
    runtime.start_subagent(
        subagent_id="sync-1",
        parent_run_id=parent.run_id,
        agent_type="explore",
    )
    runtime.store_subagent_result(
        "sync-1",
        status="completed",
        content="sync tool result already returned",
        **_subagent_fence(runtime, "sync-1"),
    )
    assert runtime.list_parent_notifications(conversation_id="conv-sync") == []


def test_subagent_metadata_defaults_and_flags() -> None:
    default = _subagent_metadata({})
    assert default["cancel_with_parent"] is True
    assert default["detach_from_parent"] is False

    detached = _subagent_metadata({"detach_from_parent": True})
    assert detached["detach_from_parent"] is True
    assert detached["cancel_with_parent"] is False

    cancel_false = _subagent_metadata({"cancel_with_parent": False})
    assert cancel_false["cancel_with_parent"] is False
    assert cancel_false["detach_from_parent"] is True


def test_context_reconcile_closes_dangling_tool_calls() -> None:
    ctx = ContextBuilder()
    ctx.append_user("hello")
    ctx.append_assistant_tool_calls(
        [
            ToolCallEvent(id="call_open", name="bash", arguments={"command": "echo hi"}),
        ]
    )
    inserted = ctx.reconcile_dangling_tool_calls()
    assert inserted == 1
    roles = [msg.role for msg in ctx._history]
    assert roles[-1] == "tool"
    assert ctx._history[-1].tool_call_id == "call_open"
    assert "did not complete" in str(ctx._history[-1].content)


def test_checkpoint_corruption_blocks_stale_fallback(tmp_path: Path) -> None:
    path = save_checkpoint(
        session_id="sess-1",
        user_message="continue",
        iterations=2,
        reply="partial",
        messages=[{"role": "user", "content": "continue"}],
        tool_calls=[],
        active_skills=[],
        disabled_tools=set(),
        stopped_reason="interrupted",
        last_mutation_index=0,
        base_dir=tmp_path,
        run_id="run-1",
        conversation_id="conv-1",
        resume_payload={"run_id": "run-1"},
    )
    assert path.name.count("-") >= 1
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 4
    assert payload["sequence"] >= 1
    assert payload["checksum"]

    # A newer corrupt checkpoint must not silently rewind to older state.
    corrupt = path.parent / f"{int(payload['timestamp'] * 1000) + 5}-000999.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    with pytest.raises(CheckpointCorruptionError, match="unreadable"):
        load_latest_checkpoint("sess-1", base_dir=tmp_path)


def test_checkpoint_bad_checksum_fails_closed(tmp_path: Path) -> None:
    path = save_checkpoint(
        session_id="sess-2",
        user_message="x",
        iterations=1,
        reply="",
        messages=[],
        tool_calls=[],
        active_skills=[],
        disabled_tools=set(),
        stopped_reason="interrupted",
        last_mutation_index=0,
        base_dir=tmp_path,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    data["checksum"] = "deadbeef"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(CheckpointCorruptionError, match="verification failed"):
        load_latest_checkpoint("sess-2", base_dir=tmp_path)


def test_runtime_execution_journal_root(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    journal = runtime.execution_journal("child-1")
    journal.append("user_prompt", {"content": " dig "})
    transcript = runtime.load_agent_transcript("child-1")
    assert transcript["history"][0]["content"] == "dig"
    assert (tmp_path / "sidechains" / "child-1" / "events.jsonl").exists()


def test_parent_notification_format_and_inject_marks_delivered(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    parent = runtime.start_run(conversation_id="conv-inject", role="main")
    runtime.start_subagent(
        subagent_id="bg-1",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        detach_from_parent=True,
        prompt_summary="scan package",
    )
    runtime.store_subagent_result(
        "bg-1",
        status="completed",
        content="found 3 candidates",
        duration_ms=40,
        iterations=2,
        tool_call_count=3,
        **_subagent_fence(runtime, "bg-1"),
    )

    pending = runtime.list_parent_notifications(conversation_id="conv-inject", status="pending")
    assert len(pending) == 1
    formatted = _format_parent_notification_message(pending[0])
    assert "<task-notification>" in formatted
    assert "bg-1" in formatted
    assert "found 3 candidates" in formatted

    ctx = ContextBuilder()
    state = AgentState(user_message="continue")
    emitted: list[tuple[str, dict[str, object]]] = []

    async def emit_event(event_type: str, payload: dict[str, object]) -> None:
        emitted.append((event_type, payload))

    injected = asyncio.run(
        _inject_parent_notifications(
            ctx=ctx,
            state=state,
            metadata={"agent_role": "main"},
            runtime=runtime,
            run_context=RunContext(agent_runtime=runtime),
            parent_run_id=parent.run_id,
            conversation_id="conv-inject",
            emit_event=emit_event,
        )
    )
    assert injected == 1
    assert emitted == [
        (
            "parent.notifications",
            {
                "count": 1,
                "parent_run_id": parent.run_id,
                "conversation_id": "conv-inject",
            },
        )
    ]
    assert any("<task-notification>" in str(msg.content) for msg in ctx._history if msg.role == "user")
    remaining = runtime.list_parent_notifications(conversation_id="conv-inject", status="pending")
    assert remaining == []
    delivered = runtime.list_parent_notifications(conversation_id="conv-inject", status="delivered")
    assert len(delivered) == 1
    assert delivered[0]["attempts"] == 1


def test_parent_notification_inject_skips_subagent_turns(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    parent = runtime.start_run(conversation_id="conv-skip", role="main")
    runtime.start_subagent(
        subagent_id="bg-2",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        detach_from_parent=True,
    )
    runtime.store_subagent_result(
        "bg-2",
        status="completed",
        content="done",
        **_subagent_fence(runtime, "bg-2"),
    )
    ctx = ContextBuilder()
    state = AgentState(user_message="worker")
    injected = asyncio.run(
        _inject_parent_notifications(
            ctx=ctx,
            state=state,
            metadata={"agent_mode": "subagent", "agent_role": "subagent"},
            runtime=runtime,
            run_context=RunContext(agent_runtime=runtime),
            parent_run_id=parent.run_id,
            conversation_id="conv-skip",
        )
    )
    assert injected == 0
    assert runtime.list_parent_notifications(conversation_id="conv-skip", status="pending")


def test_parent_notification_inject_retries_failed_delivery(tmp_path: Path) -> None:
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm")
    parent = runtime.start_run(conversation_id="conv-retry", role="main")
    runtime.start_subagent(
        subagent_id="bg-retry",
        parent_run_id=parent.run_id,
        agent_type="explore",
        background=True,
        detach_from_parent=True,
    )
    runtime.store_subagent_result(
        "bg-retry",
        status="completed",
        content="retry result",
        **_subagent_fence(runtime, "bg-retry"),
    )
    pending = runtime.list_parent_notifications(conversation_id="conv-retry", status="pending")
    assert len(pending) == 1
    runtime.parent_outbox(conversation_id="conv-retry").mark_failed(
        pending[0]["notification_id"], "temporary delivery failure"
    )

    ctx = ContextBuilder()
    injected = asyncio.run(_inject_parent_notifications(
        ctx=ctx,
        state=AgentState(user_message="continue"),
        metadata={"agent_role": "main"},
        runtime=runtime,
        run_context=RunContext(agent_runtime=runtime),
        parent_run_id=parent.run_id,
        conversation_id="conv-retry",
    ))

    assert injected == 1
    assert any("retry result" in str(msg.content) for msg in ctx._history)
    delivered = runtime.list_parent_notifications(conversation_id="conv-retry", status="delivered")
    assert len(delivered) == 1
    assert delivered[0]["last_error"] == "temporary delivery failure"
