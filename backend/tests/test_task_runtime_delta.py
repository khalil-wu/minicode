from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.tasks.manager import TaskManager
from backend.ws.handler import WebSocketSession
from backend.ws.session_lifecycle import SessionLifecycle


@pytest.mark.asyncio
async def test_task_change_does_not_rebuild_session_capabilities():
    session = SimpleNamespace(
        session_id="session-task-delta",
        task_manager=TaskManager(),
        send_event=AsyncMock(),
        run_manager=SimpleNamespace(
            active_task_id="run-active", run_tasks={},
            queued_user_message_snapshot=lambda: [], pending_turn_input_snapshot=lambda: [],
        ),
        active_conversation_id=None,
        selected_model="audit",
        _pending_approval_runtime_items=lambda: [{"request_id": "approval-active"}],
        fork_registry=SimpleNamespace(list=lambda **_: []),
        _runtime_permission_payload=lambda **_: {"profile": "bypass", "sandbox_status": {}},
        permission_context=SimpleNamespace(mode="bypass", source="test", approval_policy="never",
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
