"""Integration regressions for accepted input ownership and explicit Stop."""
from contextlib import nullcontext
from types import SimpleNamespace
import asyncio

import pytest

from backend.agent.message import UserCommand
from backend.conversations.repository import ConversationRepository
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.misc import handle_interrupt_command, handle_user_message_queue_steer
from backend.ws.run_manager import SessionRunManager
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin


async def _noop(*args, **kwargs):
    pass


@pytest.fixture
def session(tmp_path):
    events = []
    started = []
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation(conversation_id="conv_stop_owner", memory_mode="disabled")
    assert conversation.id == "conv_stop_owner"

    async def send_event(event):
        events.append(event)

    async def start(content, **kwargs):
        started.append(content)

    lock = asyncio.Lock()
    value = SimpleNamespace(
        session_id="stop-audit", active_conversation_id="conv_stop_owner", _conversation_streams={},
        window_instance_id="", window_owner_id="", _window_binding_token="",
        _extension_shutdown_requested=False, connection_generation=1, ws_manager=None,
        conversation_repo=repository,
        session_lifecycle=SimpleNamespace(
            is_shutting_down=False, schedule_task_runtime_update=lambda: None,
            workspace_root_for_conversation=lambda conv=None: None,
        ),
        event_outbox=SimpleNamespace(
            bind_connection_generation=lambda generation: nullcontext(),
            bind_client_command=lambda *args, **kwargs: nullcontext(),
        ),
        conversation_lifecycle_lock=lambda: lock,
        cancel_pending_approvals=_noop, cleanup_tasks=set(), _cleanup_tasks=set(),
        task_manager=SimpleNamespace(cancel=lambda task_id: None),
        start_agent_run=start, send_event=send_event, git_branch_for=lambda path: "",
        events=events, started=started,
    )
    value.session_lifecycle = SessionLifecycle(value)
    value.session_lifecycle.workspace_root_for_conversation = lambda conv=None: None
    value.session_lifecycle.schedule_task_runtime_update = lambda: None
    value.guardian_host_for_conversation = lambda owner, **kwargs: SessionApprovalRuntimeMixin.guardian_host_for_conversation(value, owner, **kwargs)
    manager = value.run_manager = SessionRunManager(value)
    manager._notification_wakes_closed = True
    value.command_dispatcher = SessionCommandDispatcher(value, root_dir=tmp_path / "commands")
    value.running_agent_task_for = lambda cid: manager.run_tasks.get(cid)
    value.cancel_agent_runs = lambda **kwargs: manager.cancel(**kwargs)
    value.schedule_next_queued_user_message = lambda cid: WebSocketSession.schedule_next_queued_user_message(value, cid)
    value._register_agent_run = lambda **kwargs: WebSocketSession._register_agent_run(value, **kwargs)
    value._cleanup_agent_run = lambda **kwargs: WebSocketSession._cleanup_agent_run(value, **kwargs)
    yield value
    manager._unsubscribe_parent_notifications()
    manager.close_durable_queue()


def _command(content="work", message_id="assistant_input", **data):
    return UserCommand(type="user_message", data={
        "conversation_id": "conv_stop_owner", "content": content,
        "assistant_message_id": message_id, "client_command_id": f"client_{message_id}",
        "_client_queue_key": "stop-audit",
        **data,
    })


@pytest.mark.asyncio
async def test_stop_during_workspace_admission_is_durable(session, tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def activate(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    session.activate_workspace_path = activate
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda path: True)
    command = _command(workspace_root=str(tmp_path))
    durable = session.run_manager.durable_client_commands
    assert durable.persist_client_command(command)
    dispatch = asyncio.create_task(session.command_dispatcher._run_durable_client_command("client_assistant_input", 1))
    await asyncio.wait_for(entered.wait(), 3)
    await handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "assistant_input"})
    await asyncio.wait_for(dispatch, 5)
    assert session.started == []
    assert [(event.data["status"], event.data["message_id"]) for event in session.events if event.type == "done"] == [("cancelled", "assistant_input")]
    assert not durable.has_client_command("client_assistant_input")
    assert session.command_dispatcher._client_command_seen(command)
    assert durable.cancelled_client_command("client_assistant_input")["command"]["data"]["content"] == "work"


@pytest.mark.asyncio
async def test_repeated_pending_stop_does_not_cancel_terminal_delivery(session, tmp_path, monkeypatch):
    entered = asyncio.Event()
    terminal_entered = asyncio.Event()
    terminal_release = asyncio.Event()

    async def activate(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    original_send = session.send_event

    async def send(event):
        if event.type == "done":
            terminal_entered.set()
            await terminal_release.wait()
        await original_send(event)

    session.activate_workspace_path = activate
    session.send_event = send
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda path: True)
    command = _command(workspace_root=str(tmp_path))
    session.run_manager.durable_client_commands.persist_client_command(command)
    dispatch = asyncio.create_task(session.command_dispatcher._run_durable_client_command("client_assistant_input", 1))
    await asyncio.wait_for(entered.wait(), 3)
    request = {"conversation_id": "conv_stop_owner", "message_id": "assistant_input"}
    first = asyncio.create_task(handle_interrupt_command(session, request))
    await asyncio.wait_for(terminal_entered.wait(), 3)
    second = asyncio.create_task(handle_interrupt_command(session, request))
    await asyncio.sleep(0)
    terminal_release.set()
    await asyncio.wait_for(asyncio.gather(dispatch, first, second), 5)
    assert len([event for event in session.events if event.type == "done"]) == 1
    assert not session.run_manager.durable_client_commands.has_client_command("client_assistant_input")


@pytest.mark.asyncio
async def test_stop_before_runner_first_step_delivers_terminal_and_consumes_command(session):
    runner_entered = []
    stops = []

    async def runner(*args, **kwargs):
        runner_entered.append(True)
        await asyncio.Event().wait()

    def create(kind, coro):
        stops.append(asyncio.create_task(handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "assistant_input"})))
        return SimpleNamespace(id="task_input", task=asyncio.create_task(coro))

    session._run_agent = runner
    session.task_manager.create = create
    session.start_agent_run = lambda *args, **kwargs: WebSocketSession.start_agent_run(session, *args, **kwargs)
    command = _command()
    durable = session.run_manager.durable_client_commands
    assert durable.persist_client_command(command)
    await session.command_dispatcher._run_durable_client_command("client_assistant_input", 1)
    await asyncio.gather(*stops)
    assert runner_entered == []
    assert [(event.type, event.data.get("status")) for event in session.events if event.type == "done"] == [("done", "cancelled")]
    assert session._conversation_streams == {}
    assert not durable.has_client_command("client_assistant_input")
    assert session.command_dispatcher._client_command_seen(command)
    cancellation = durable.cancelled_client_command("client_assistant_input")
    assert cancellation["reason"] == "user_interrupted"
    assert cancellation["command"]["data"]["content"] == "work"
    assert cancellation["command"]["data"]["user_message_id"]
    assert cancellation["client_queue_key"] == durable.path.stem
    cold = DurableUserMessageQueue(session_id=session.session_id, root_dir=durable.path.parent)
    try:
        assert cold.cancelled_client_command("client_assistant_input") == cancellation
        assert cold.pending_client_commands() == []
        assert cold.claim_client_command("client_assistant_input") is None
        assert cold.persist_client_command(command)
        assert cold.pending_client_commands() == []
    finally:
        cold.close()
    await session.command_dispatcher._run_durable_client_command("client_assistant_input", 1)
    assert runner_entered == []
    assert session.events[-1].type == "command.result"
    assert session.events[-1].data["data"] == {
        "admitted": False, "cancelled": True, "reason": "user_interrupted",
        "conversation_id": "conv_stop_owner", "user_message_id": cancellation["command"]["data"]["user_message_id"],
        "assistant_message_id": "assistant_input",
    }


@pytest.mark.asyncio
async def test_stop_retains_queue_without_automatic_dispatch(session):
    active = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    cancel_event = asyncio.Event()
    manager = session.run_manager
    manager.run_tasks["conv_stop_owner"] = active
    manager.run_task_ids["conv_stop_owner"] = "active"
    manager.cancel_events["conv_stop_owner"] = cancel_event
    manager.enqueue_user_message("conv_stop_owner", _command("older queued", "older"))
    session._conversation_streams["conv_stop_owner"] = {"message_id": "active_assistant"}
    await handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "active_assistant"})
    session._cleanup_agent_run(conversation_id="conv_stop_owner", task=active, task_id="active", cancel_event=cancel_event)
    await asyncio.sleep(0)
    assert active.cancelled()
    assert session.started == []
    assert manager.queued_user_message_snapshot("conv_stop_owner")[0]["paused"] is True
    assert manager.dequeue_user_message("conv_stop_owner") is None


def test_paused_queue_survives_new_owner_and_stale_save(tmp_path):
    first = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    try:
        older = _command("old", "old")
        first.save({"conv_stop_owner": [older]}, {})
        old_snapshot = second.load()[0]
        first.set_user_queue_paused("conv_stop_owner", "stopped-first")
        second.save(old_snapshot, {})
        assert second.claim_user_message("conv_stop_owner") is None
        fresh = _command("explicit new", "new", _queue_explicit_send=second.paused_user_queues()["conv_stop_owner"])
        second.save({"conv_stop_owner": [older, fresh]}, {})
        claimed = second.claim_user_message("conv_stop_owner")
        assert claimed.data["content"] == "explicit new"
        second.settle_user_message("conv_stop_owner", claimed, succeeded=True)
        assert first.claim_user_message("conv_stop_owner") is None
        first.set_user_queue_paused("conv_stop_owner", "")
        assert first.claim_user_message("conv_stop_owner").data["content"] == "old"
    finally:
        first.close()
        second.close()


def test_repeated_old_stop_preserves_new_consent_and_new_turn_stop_pauses_it(tmp_path):
    store = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    try:
        store.set_user_queue_paused("conv_stop_owner", "stopped-A")
        fresh = _command(_queue_explicit_send=store.paused_user_queues()["conv_stop_owner"])
        store.save({"conv_stop_owner": [fresh]}, {})
        store.set_user_queue_paused("conv_stop_owner", "stopped-A")
        claimed = store.claim_user_message("conv_stop_owner")
        assert claimed is not None
        assert store.settle_user_message("conv_stop_owner", claimed, succeeded=False)
        store.set_user_queue_paused("conv_stop_owner", "stopped-B")
        assert store.claim_user_message("conv_stop_owner") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_interrupt_message_fence_preserves_new_input_on_duplicate_stop(session):
    manager = session.run_manager
    active = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    manager.run_tasks["conv_stop_owner"] = active
    manager.run_task_ids["conv_stop_owner"] = "task-A"
    manager.cancel_events["conv_stop_owner"] = asyncio.Event()
    session._conversation_streams["conv_stop_owner"] = {"message_id": "A"}
    await handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "A"})
    await session.command_dispatcher._handle_command_inner(_command("fresh", "fresh"))
    await handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "A"})
    fresh = manager.dequeue_user_message("conv_stop_owner")
    assert fresh is not None and fresh.data["content"] == "fresh"
    manager.finish_user_message_dispatch("conv_stop_owner", fresh, succeeded=True)

    next_run = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    manager.run_tasks["conv_stop_owner"] = next_run
    manager.run_task_ids["conv_stop_owner"] = "task-B"
    manager.cancel_events["conv_stop_owner"] = asyncio.Event()
    session._conversation_streams["conv_stop_owner"] = {"message_id": "B"}
    await session.command_dispatcher._handle_command_inner(_command("queued while B runs", "C"))
    await handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "B"})
    assert manager.dequeue_user_message("conv_stop_owner") is None
    assert manager.queued_user_message_snapshot("conv_stop_owner")[0]["paused"] is True


@pytest.mark.asyncio
async def test_late_stop_after_terminal_does_not_pause_followups(session):
    manager = session.run_manager
    manager.enqueue_user_message("conv_stop_owner", _command("follow up", "queued"))
    session._conversation_streams["conv_stop_owner"] = {"message_id": "done", "terminal_fenced": True}
    await handle_interrupt_command(session, {"conversation_id": "conv_stop_owner", "message_id": "done"})
    assert not manager.user_queue_paused("conv_stop_owner")
    assert manager.dequeue_user_message("conv_stop_owner") is not None


@pytest.mark.asyncio
async def test_new_input_does_not_resume_old_queue_but_explicit_queue_send_does(session):
    manager = session.run_manager
    manager.enqueue_user_message("conv_stop_owner", _command("old", "old"))
    manager.set_user_queue_paused("conv_stop_owner", "stopped-first")
    await session.command_dispatcher._handle_command_inner(_command("new", "new"))
    assert session.started == ["new"]
    assert manager.user_queue_paused("conv_stop_owner")
    assert manager.dequeue_user_message("conv_stop_owner") is None
    await handle_user_message_queue_steer(session, {"conversation_id": "conv_stop_owner", "message_id": "old"})
    await asyncio.gather(*list(session.command_dispatcher.command_tasks))
    assert session.started == ["new", "old"]
    assert not manager.user_queue_paused("conv_stop_owner")


@pytest.mark.asyncio
async def test_empty_historical_pause_does_not_pause_future_queue(session):
    session.run_manager.set_user_queue_paused("conv_stop_owner", "stopped-first")
    await session.command_dispatcher._handle_command_inner(_command())
    assert session.started == ["work"]
    assert not session.run_manager.user_queue_paused("conv_stop_owner")
