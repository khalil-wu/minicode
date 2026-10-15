import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.tasks.manager import TaskManager
from backend.agent.runtime import AgentRuntime
from backend.ws.handler import WebSocketSession
from backend.ws.run_manager import SessionRunManager
from backend.ws.session_lifecycle import SessionLifecycle
from backend.permissions.context import PermissionContext


@pytest.mark.asyncio
async def test_task_change_does_not_rebuild_session_capabilities():
    session = SimpleNamespace(
        session_id="session-task-delta",
        ws_manager=None,
        task_manager=TaskManager(),
        send_event=AsyncMock(),
        run_manager=SimpleNamespace(
            active_task_id="run-active", run_tasks={},
            queued_user_message_snapshot=lambda: [], pending_turn_input_snapshot=lambda: [],
        ),
        active_conversation_id=None,
        _conversation_streams={},
        selected_model="audit",
        _pending_approval_runtime_items=lambda: [{"request_id": "approval-active"}],
        fork_registry=SimpleNamespace(list=lambda **_: []),
        _runtime_permission_payload=lambda **_: {"profile": "bypass", "sandbox_status": {}},
        permission_context=PermissionContext(mode="bypass", source="test", approval_policy="never",
            sandbox_mode="danger-full-access", requirements_source=""),
        _mcp_summary=lambda: {},
        runtime_capability_summary=Mock(return_value={"version": 12}),
        event_outbox=SimpleNamespace(runtime_snapshot=lambda: {}),
    )
    session.runtime_snapshot = lambda **options: WebSocketSession.runtime_snapshot(session, **options)
    full = session.runtime_snapshot()
    session.runtime_capability_summary.reset_mock()
    lifecycle = SessionLifecycle(session)
    lifecycle.schedule_task_runtime_update()
    await lifecycle._task_runtime_update_task

    event = session.send_event.call_args.args[0]
    assert event.type == "task.update"
    assert event.data == {"partial": True, "session": {key: value for key, value in full.items() if key != "capabilities"}}
    session.runtime_capability_summary.assert_not_called()

    session.run_manager.active_task_id = None
    session._pending_approval_runtime_items = lambda: []
    lifecycle.schedule_task_runtime_update()
    await lifecycle._task_runtime_update_task
    settled = session.send_event.call_args.args[0].data["session"]
    assert settled["active_task_id"] is None
    assert settled["active_stream_conversation_ids"] == []
    assert settled["pending_approval_count"] == 0
    assert settled["pending_approvals"] == []


@pytest.mark.asyncio
async def test_explicit_runtime_update_still_publishes_full_snapshot():
    snapshot = {"session_id": "session-task-delta", "permission_mode": "confirm", "capabilities": {"version": 12}}
    session = SimpleNamespace(session_id="session-task-delta", send_event=AsyncMock(), runtime_snapshot=Mock(return_value=snapshot))
    await SessionLifecycle(session).send_task_runtime_update()
    assert session.send_event.call_args.args[0].data == {"session": snapshot}


@pytest.mark.asyncio
async def test_cleanup_change_during_snapshot_delivery_publishes_the_latest_state():
    entered, release = asyncio.Event(), asyncio.Event()
    current = [{"id": "child", "status": "completed", "cleanup_pending": True}]
    events = []
    async def send_event(event):
        events.append(event)
        if len(events) == 1:
            entered.set()
            await release.wait()
    session = SimpleNamespace(session_id="coalesced-update", send_event=send_event,
        runtime_snapshot=lambda **_: {"active_subagents": list(current)})
    lifecycle = SessionLifecycle(session)
    lifecycle.schedule_task_runtime_update()
    publishing = lifecycle._task_runtime_update_task
    await entered.wait()
    current.clear()
    lifecycle.schedule_task_runtime_update()
    release.set()
    await publishing
    assert len(events) == 2
    assert events[0].data["session"]["active_subagents"][0]["cleanup_pending"]
    assert events[1].data == {"partial": True, "session": {"active_subagents": []}}


@pytest.mark.asyncio
async def test_parent_final_keeps_global_detached_pending_and_cleanup_work_in_runtime_snapshot(tmp_path, monkeypatch):
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", enable_lease_heartbeat=False)
    monkeypatch.setattr("backend.ws.handler.default_runtime_if_initialized", lambda: runtime)
    monkeypatch.setattr("backend.ws.run_manager.default_runtime_if_initialized", lambda: runtime)
    session = SimpleNamespace(
        session_id="upgrade-window", task_manager=TaskManager(), active_conversation_id=None,
        ws_manager=None, _conversation_streams={},
        conversation_repo=SimpleNamespace(_base_dir=tmp_path / "conversations"),
        selected_model="audit", send_event=AsyncMock(), cleanup_tasks=set(),
        _pending_approval_runtime_items=lambda: [], fork_registry=SimpleNamespace(list=lambda **_: []),
        _runtime_permission_payload=lambda **_: {"profile": "bypass", "sandbox_status": {}},
        permission_context=PermissionContext(mode="bypass", source="test", approval_policy="never",
            sandbox_mode="danger-full-access", requirements_source=""),
        _mcp_summary=lambda: {}, event_outbox=SimpleNamespace(runtime_snapshot=lambda: {}),
    )
    session.runtime_snapshot = lambda **options: WebSocketSession.runtime_snapshot(session, **options)
    session.session_lifecycle = SessionLifecycle(session)
    session.run_manager = SessionRunManager(session)
    release = asyncio.Event()
    worker = asyncio.create_task(release.wait())
    try:
        parent = runtime.start_run(conversation_id="closed-parent-conversation", session_id="another-window")
        child = runtime.start_subagent(subagent_id="detached-child", parent_run_id=parent.run_id,
            agent_type="explore", background=True, detach_from_parent=True, cancel_with_parent=False)
        runtime.register_subagent_task(child.subagent_id, worker, parent_run_id=parent.run_id)
        pending = runtime.start_subagent(subagent_id="never-opened-pending", parent_run_id=parent.run_id,
            agent_type="explore", pending=True)
        runtime._subagents.pop(pending.subagent_id)
        cleanup = runtime.start_subagent(subagent_id="cleanup-only", parent_run_id=parent.run_id, agent_type="explore")
        runtime._mark_subagent_cleanup(cleanup.subagent_id, reason="test cleanup still owns resources")
        terminal_cleanup = runtime.complete_subagent(cleanup.subagent_id,
            agent_path=cleanup.agent_path, mailbox_epoch=cleanup.mailbox_epoch)
        assert terminal_cleanup.status == "completed"
        runtime._subagents.pop(cleanup.subagent_id)
        runtime.complete_run(parent.run_id)
        await asyncio.sleep(0)
        await session.session_lifecycle._task_runtime_update_task
        snapshot = session.send_event.call_args.args[0].data["session"]
        assert snapshot["running_tasks"] == [] and snapshot["active_stream_conversation_ids"] == []
        assert snapshot["active_subagents"] == [
            {"id": "cleanup-only", "status": "completed", "cleanup_pending": True},
            {"id": "detached-child", "status": "running", "cleanup_pending": False},
            {"id": "never-opened-pending", "status": "pending", "cleanup_pending": False},
        ]
        terminal_child = runtime.complete_subagent(child.subagent_id,
            agent_path=child.agent_path, mailbox_epoch=child.mailbox_epoch)
        assert terminal_child.status == "completed"
        await asyncio.sleep(0)
        await session.session_lifecycle._task_runtime_update_task
        assert session.send_event.call_args.args[0].data["session"]["active_subagents"][1] == {
            "id": child.subagent_id, "status": "completed", "cleanup_pending": True,
        }
        release.set()
        await worker
        runtime.release_subagent_task(child.subagent_id, expected_task=worker)
        await asyncio.sleep(0)
        await session.session_lifecycle._task_runtime_update_task
        assert session.send_event.call_args.args[0].data["session"]["active_subagents"] == [
            {"id": cleanup.subagent_id, "status": "completed", "cleanup_pending": True},
            {"id": pending.subagent_id, "status": "pending", "cleanup_pending": False},
        ]
        assert "input_prompt" not in str(snapshot) and "resume_config" not in str(snapshot)
    finally:
        release.set()
        await worker
        await session.run_manager.shutdown_notification_wakes()
        session.run_manager.close_durable_queue()
        runtime.close(release_lease=True)
