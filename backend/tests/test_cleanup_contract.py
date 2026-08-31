import asyncio
from pathlib import Path
from types import SimpleNamespace

from backend.agent.execution_journal import ExecutionJournal
from backend.agent.tool_events import cancelled_pending_tool_events
from backend.agent.tool_stream_tracker import StreamingToolTracker
from backend.llm.base import ToolCallEvent
from backend.tasks.manager import TaskManager


def test_cleanup_journal_fact_is_first_class(tmp_path) -> None:
    journal = ExecutionJournal("cleanup-contract", base_dir=tmp_path)
    event = journal.append_cleanup(
        {
            "resource_kind": "background_command",
            "resource_id": "cmd-1",
            "requested": True,
            "completed": False,
            "pending": 1,
            "reason": "cancel_requested",
        }
    )

    assert event.event_type == "cleanup"
    assert journal.read_events()[0].payload["pending"] == 1


def test_task_manager_cancellation_intent_remains_terminal_after_late_return() -> None:
    async def scenario() -> None:
        release = asyncio.Event()

        async def cancellation_resistant() -> str:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()
                return "late result"

        manager = TaskManager()
        managed = manager.create("background", cancellation_resistant())
        await asyncio.sleep(0)
        assert manager.cancel(managed.id)
        assert managed.cleanup_pending is True
        release.set()
        await asyncio.wait_for(manager.wait(managed.id), timeout=1)
        assert managed.status == "cancelled"
        assert managed.cleanup_pending is False
        await manager.cancel_all_and_wait()

    asyncio.run(scenario())


def test_execution_journal_tool_pair_is_idempotent_and_reopens_on_a_later_turn(
    tmp_path: Path,
) -> None:
    journal = ExecutionJournal("tool-pair-contract", base_dir=tmp_path)

    first_use = journal.append_tool_use(
        {
            "id": "reused-call",
            "name": "read_file",
            "args": {"path": "first.py"},
        }
    )
    first_result = journal.append_tool_result(
        {
            "id": "reused-call",
            "content": "first",
            "status": "success",
        },
        tool_name="read_file",
    )
    duplicate = journal.append_tool_result(
        {
            "id": "reused-call",
            "content": "duplicate",
            "status": "failed",
        },
        tool_name="read_file",
    )
    second_use = journal.append_tool_use(
        {
            "id": "reused-call",
            "name": "read_file",
            "args": {"path": "second.py"},
        }
    )
    second_result = journal.append_tool_result(
        {
            "id": "reused-call",
            "content": "second",
            "status": "cancelled",
        },
        tool_name="read_file",
    )

    assert first_use is not None
    assert first_result is not None
    assert duplicate is None
    assert second_use is not None
    assert second_result is not None
    assert journal.unresolved_tool_uses() == []
    assert [event.event_type for event in journal.read_events()] == [
        "tool_use",
        "tool_result",
        "tool_use",
        "tool_result",
    ]
    assert [item["content"] for item in journal.reconstruct_history() if item["role"] == "tool"] == [
        "first",
        "second",
    ]

    reopened = ExecutionJournal("tool-pair-contract", base_dir=tmp_path)
    assert reopened.unresolved_tool_uses() == []
    assert len(reopened.read_events()) == 4


def test_cancelled_complete_tool_call_is_projected_once_for_duplicate_ids() -> None:
    tracker = StreamingToolTracker()
    tracker.add_tool(
        ToolCallEvent(id="call-1", name="read_file", arguments={})
    )
    stream_state = SimpleNamespace(
        tool_calls=[
            SimpleNamespace(id="call-1", name="read_file"),
            SimpleNamespace(id="call-1", name="read_file"),
        ]
    )

    events = cancelled_pending_tool_events(
        stream_state,
        tracker,
        iteration_id="iteration-1",
    )

    assert len(events) == 1
    assert events[0].type == "tool_result"
    assert events[0].data["id"] == "call-1"
    assert events[0].data["status"] == "cancelled"
