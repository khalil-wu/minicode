from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent import runtime as runtime_module
from backend.agent.message import UserCommand
from backend.agent.runtime import AgentRuntime
from backend.ws.handler import WebSocketSession
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.run_manager import SessionRunManager


@pytest.mark.asyncio
async def test_new_parent_notification_is_rescheduled_after_an_inflight_wake(monkeypatch):
    manager = SessionRunManager(SimpleNamespace(session_id=""))
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    attempted = []

    async def dispatch(conversation_id):
        generation = manager._notification_request_generation[conversation_id]
        attempted.append(generation)
        if len(attempted) == 1:
            first_started.set()
            await release_first.wait()
        manager._notification_attempt_generation[conversation_id] = generation
        if len(attempted) == 2:
            second_started.set()

    monkeypatch.setattr(manager, "_dispatch_parent_notification_wake", dispatch)
    manager._watched_notification_conversations.add("parent")
    try:
        manager._request_parent_notification_wake_on_loop("parent")
        await asyncio.wait_for(first_started.wait(), 1)
        manager._request_parent_notification_wake_on_loop("parent")
        release_first.set()
        await asyncio.wait_for(second_started.wait(), 1)
        assert attempted == [1, 2]
    finally:
        await manager.shutdown_notification_wakes()


@pytest.mark.asyncio
async def test_pending_mailbox_scheduler_retains_child_session_until_it_settles(monkeypatch):
    monkeypatch.setattr(runtime_module, "CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.01)
    runtime = object.__new__(AgentRuntime)
    runtime._subagent_mailbox_suspended = set()
    runtime._subagent_mailbox_resumers = {}
    release = asyncio.Event()
    started = asyncio.Event()

    async def scheduling():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(scheduling())
    runtime._subagent_mailbox_turns = {"child": (task, asyncio.get_running_loop().create_future())}
    await started.wait()
    try:
        assert not await runtime.close_subagent_session("child")
        assert runtime._subagent_mailbox_turns["child"][0] is task
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_cancelled_queue_waiter_releases_ownership_and_starts_the_next_input():
    spawned = []
    first_started, second_started = asyncio.Event(), asyncio.Event()
    session = SimpleNamespace(session_id="", ws_manager=None, send_event=AsyncMock(),
        running_agent_task_for=lambda _: None,
        session_lifecycle=SimpleNamespace(is_shutting_down=False, workspace_root_for_conversation=lambda _: "",
                                          recovered_input_workspace_ready=lambda _: True),
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda _: None),
        event_outbox=SimpleNamespace(bind_connection_generation=lambda _: nullcontext(),
            bind_client_command=lambda *args, **kwargs: nullcontext()))
    manager = session.run_manager = SessionRunManager(session)
    session.schedule_next_queued_user_message = lambda owner: WebSocketSession.schedule_next_queued_user_message(session, owner)

    async def handle(command):
        if command.data["content"] == "first":
            manager.mark_queue_owned_run("parent", "run-1")
            first_started.set()
        else:
            session.running_agent_task_for = lambda _: object()
            second_started.set()
        return True

    session.command_dispatcher = SimpleNamespace(_handle_command=handle,
        _validate_executor_input_workspace=AsyncMock(return_value=True),
        track_execution_command_task=lambda task, **kwargs: spawned.append(task))
    manager.enqueue_user_message("parent", UserCommand(type="user_message", data={
        "content": "first", "assistant_message_id": "first-assistant"}))
    manager.enqueue_user_message("parent", UserCommand(type="user_message", data={
        "content": "second", "assistant_message_id": "second-assistant"}))
    try:
        session.schedule_next_queued_user_message("parent")
        await asyncio.wait_for(first_started.wait(), 1)
        await asyncio.sleep(0)
        spawned[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await spawned[0]
        assert not manager.is_queue_dispatching("parent")
        assert manager.is_queue_owned_run("run-1")

        manager.finish_queue_owned_run("run-1", succeeded=True)
        await asyncio.wait_for(second_started.wait(), 1)
        await spawned[-1]
        assert not manager.is_queue_owned_run("run-1")
        assert "parent" not in manager._queue_owned_runs
    finally:
        await manager.shutdown_notification_wakes()


@pytest.mark.asyncio
async def test_forgetting_a_stopped_conversation_releases_its_queue_waiter():
    manager = SessionRunManager(SimpleNamespace(session_id=""))
    try:
        manager._run_contexts["parent"] = object()
        manager.mark_queue_owned_run("parent", "run-1")
        waiting = asyncio.create_task(manager.wait_for_queue_owned_run("parent"))
        await asyncio.sleep(0)
        manager.forget_conversation("parent")
        assert await waiting is False
        assert "parent" not in manager._run_contexts
        assert "parent" not in manager._queue_owned_runs
        assert "run-1" not in manager._queue_owned_run_releases
    finally:
        await manager.shutdown_notification_wakes()


@pytest.mark.asyncio
async def test_empty_user_message_receives_an_explicit_rejection():
    session = SimpleNamespace(_extension_shutdown_requested=False, active_conversation_id="parent",
                              ws_manager=None, send_event=AsyncMock())
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = session

    await dispatcher._handle_command_inner(UserCommand(type="user_message", data={"content": ""}))

    result = session.send_event.await_args.args[0]
    assert result.type == "command.result"
    assert result.data["data"]["admitted"] is False
    assert "empty" in result.data["message"]


@pytest.mark.asyncio
async def test_queue_error_is_reported_again_when_first_delivery_fails():
    emitter = AsyncMock(side_effect=[False, True])
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = SimpleNamespace(send_payload=emitter, active_conversation_id="")
    dispatcher._reported_durable_queue_load_errors = {}
    evidence = {"path": "queue.json", "reason": "malformed_json"}

    with pytest.raises(ConnectionError):
        await dispatcher._report_durable_queue_load_error(evidence)
    await dispatcher._report_durable_queue_load_error(evidence)
    await dispatcher._report_durable_queue_load_error(evidence)
    assert emitter.await_count == 2


@pytest.mark.asyncio
async def test_queue_error_retries_after_actual_outbox_disconnect(tmp_path):
    from fastapi import WebSocketDisconnect
    from backend.ws.event_outbox import EventOutbox

    failed_socket = SimpleNamespace(send_json=AsyncMock(side_effect=WebSocketDisconnect()))
    outbox = EventOutbox(session_id="queue-window", websocket=failed_socket, replay_root=tmp_path,
        replay_limit=10, cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=())
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = SimpleNamespace(send_payload=outbox.send_payload, active_conversation_id="")
    dispatcher._reported_durable_queue_load_errors = {}
    evidence = {"path": "queue.json", "reason": "malformed_json"}
    with pytest.raises(ConnectionError):
        await dispatcher._report_durable_queue_load_error(evidence)
    assert not outbox.connected and not dispatcher._reported_durable_queue_load_errors

    new_socket = SimpleNamespace(send_json=AsyncMock())
    outbox.websocket = new_socket
    outbox.connection_generation += 1
    outbox.connected = True
    await dispatcher._report_durable_queue_load_error(evidence)
    payload = new_socket.send_json.await_args.args[0]
    assert payload["type"] == "error" and payload["error_code"] == "user_message.queue.restore"
    assert payload["error_details"]["path"] == "queue.json" and "conversation_id" not in payload
    await outbox.drain_delivery()
    await outbox.drain_persistence()


@pytest.mark.asyncio
async def test_queue_errors_deduplicate_per_source_and_reappear_after_repair():
    sender = AsyncMock(return_value=True)
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = SimpleNamespace(send_payload=sender, active_conversation_id="")
    dispatcher._reported_durable_queue_load_errors = {}
    first = {"path": "client.json", "reason": "malformed_json"}
    second = {"path": "user.json", "reason": "malformed_json"}
    for _ in range(2):
        await dispatcher._report_durable_queue_load_error(first, source="client_queue")
        await dispatcher._report_durable_queue_load_error(second, source="user_queue")
    assert sender.await_count == 2
    await dispatcher._report_durable_queue_load_error(None, source="client_queue")
    await dispatcher._report_durable_queue_load_error(first, source="client_queue")
    assert sender.await_count == 3


@pytest.mark.asyncio
async def test_queue_error_does_not_claim_delivery_from_an_older_connection():
    sender = AsyncMock(return_value=True)
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = SimpleNamespace(send_payload=sender, active_conversation_id="", connection_generation=2)
    dispatcher._reported_durable_queue_load_errors = {}
    evidence = {"path": "queue.json", "reason": "malformed_json"}
    with pytest.raises(ConnectionError, match="older connection"):
        await dispatcher._report_durable_queue_load_error(evidence, source="client_queue", connection_generation=1)
    assert not dispatcher._reported_durable_queue_load_errors
    await dispatcher._report_durable_queue_load_error(evidence, source="client_queue", connection_generation=2)
    assert sender.await_args.kwargs["connection_generation"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fields,reason", [
    ({"async_request_id": 42}, "input_metadata_rejected"),
    ({"permission_mode": "invalid"}, "permission_mode_rejected"),
])
async def test_invalid_input_settings_receive_a_correlated_admission_rejection(fields, reason):
    session = SimpleNamespace(_extension_shutdown_requested=False, active_conversation_id="",
        ws_manager=None, send_event=AsyncMock(), _conversation_streams={})
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    command = UserCommand(type="user_message", data={"content": "Keep this draft", "attachments": [],
        "client_command_id": "request-1", "_client_queue_key": "fixture",
        "user_message_id": "user-1", "assistant_message_id": "assistant-1", **fields})

    await dispatcher._handle_command_inner(command)
    events = [call.args[0] for call in session.send_event.await_args_list]
    receipt = next(event for event in events if event.type == "command.result")
    assert receipt.data["client_command_id"] == "request-1"
    assert receipt.data["data"]["admitted"] is False
    assert receipt.data["data"]["user_message_id"] == "user-1"
    assert receipt.data["data"]["reason"] == reason
    assert any(event.type == "done" for event in events)


@pytest.mark.asyncio
async def test_forwarded_user_input_with_unavailable_attachment_receives_rejection():
    target = SimpleNamespace(id="owner", workspace_root="C:/workspace")
    session = SimpleNamespace(_extension_shutdown_requested=False, active_conversation_id="owner",
        ws_manager=SimpleNamespace(execution_session_for_conversation=lambda _: object()),
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda _: target),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda _: "C:/workspace"),
        attachment_store=SimpleNamespace(get_payload=lambda *args, **kwargs: None), send_event=AsyncMock())
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    command = UserCommand(type="user_message", data={"content": "Use attachment", "conversation_id": "owner",
        "attachments": [{"artifact_id": "missing", "file_name": "source.txt", "size_bytes": 10}],
        "client_command_id": "request-1", "_client_queue_key": "fixture", "user_message_id": "user-1"})

    await dispatcher._handle_command_inner(command)
    receipt = session.send_event.await_args.args[0]
    assert receipt.type == "command.result"
    assert receipt.data["client_command_id"] == "request-1"
    assert receipt.data["data"]["admitted"] is False
    assert receipt.data["data"]["reason"] == "attachments_rejected"


@pytest.mark.asyncio
async def test_stop_waits_for_the_running_task_even_if_task_bookkeeping_fails(monkeypatch):
    release, entered = asyncio.Event(), asyncio.Event()

    async def worker():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(worker())
    await entered.wait()
    session = SimpleNamespace(session_id="", cancel_pending_approvals=AsyncMock(), cleanup_tasks=set(),
        task_manager=SimpleNamespace(cancel=Mock(side_effect=RuntimeError("bookkeeping failed"))),
        session_lifecycle=SimpleNamespace(schedule_task_runtime_update=Mock()))
    manager = SessionRunManager(session)
    manager.run_tasks["parent"] = task
    manager.run_task_ids["parent"] = "task-1"
    monkeypatch.setattr(runtime_module, "default_runtime", lambda: SimpleNamespace(
        stop_subagent_tasks_for_task=AsyncMock(return_value=True)))
    asyncio.get_running_loop().call_later(0.05, release.set)
    try:
        with pytest.raises(ExceptionGroup, match="Stop encountered errors"):
            await manager.cancel(conversation_id="parent")
        assert task.done()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown_notification_wakes()
