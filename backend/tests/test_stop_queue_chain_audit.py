"""Integration regressions for accepted input ownership and explicit Stop."""
from contextlib import nullcontext
from types import SimpleNamespace
import asyncio

import pytest

from backend.agent.message import UserCommand
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.misc import handle_interrupt_command, handle_user_message_queue_steer
from backend.ws.run_manager import SessionRunManager


async def _noop(*args, **kwargs):
    pass


@pytest.fixture
def session(tmp_path):
    events = []
    started = []
    conversation = SimpleNamespace(id="owner", workspace_root="", worktree_path="", transcript=[], context_snapshot={})

    async def send_event(event):
        events.append(event)

    async def start(content, **kwargs):
        started.append(content)

    lock = asyncio.Lock()
    value = SimpleNamespace(
        session_id="stop-audit", active_conversation_id="owner", _conversation_streams={},
        _extension_shutdown_requested=False, connection_generation=1, ws_manager=None,
        conversation_repo=SimpleNamespace(
            _base_dir=tmp_path / "conversations", get_conversation=lambda cid: conversation,
            update_workspace_binding=lambda *args, **kwargs: None,
        ),
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
        "conversation_id": "owner", "content": content,
        "assistant_message_id": message_id, "client_command_id": f"client_{message_id}",
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
    await handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "assistant_input"})
    await dispatch
    assert session.started == []
    assert [(event.data["status"], event.data["message_id"]) for event in session.events if event.type == "done"] == [("cancelled", "assistant_input")]
    assert not durable.has_client_command("client_assistant_input")
    assert session.command_dispatcher._client_command_seen(command)


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
    await entered.wait()
    request = {"conversation_id": "owner", "message_id": "assistant_input"}
    first = asyncio.create_task(handle_interrupt_command(session, request))
    await terminal_entered.wait()
    second = asyncio.create_task(handle_interrupt_command(session, request))
    await asyncio.sleep(0)
    terminal_release.set()
    await asyncio.gather(dispatch, first, second)
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
        stops.append(asyncio.create_task(handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "assistant_input"})))
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


@pytest.mark.asyncio
async def test_stop_retains_queue_without_automatic_dispatch(session):
    active = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    cancel_event = asyncio.Event()
    manager = session.run_manager
    manager.run_tasks["owner"] = active
    manager.run_task_ids["owner"] = "active"
    manager.cancel_events["owner"] = cancel_event
    manager.enqueue_user_message("owner", _command("older queued", "older"))
    session._conversation_streams["owner"] = {"message_id": "active_assistant"}
    await handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "active_assistant"})
    session._cleanup_agent_run(conversation_id="owner", task=active, task_id="active", cancel_event=cancel_event)
    await asyncio.sleep(0)
    assert active.cancelled()
    assert session.started == []
    assert manager.queued_user_message_snapshot("owner")[0]["paused"] is True
    assert manager.dequeue_user_message("owner") is None


def test_paused_queue_survives_new_owner_and_stale_save(tmp_path):
    first = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    second = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    try:
        older = _command("old", "old")
        first.save({"owner": [older]}, {})
        old_snapshot = second.load()[0]
        first.set_user_queue_paused("owner", "stopped-first")
        second.save(old_snapshot, {})
        assert second.claim_user_message("owner") is None
        fresh = _command("explicit new", "new", _queue_explicit_send=second.paused_user_queues()["owner"])
        second.save({"owner": [older, fresh]}, {})
        claimed = second.claim_user_message("owner")
        assert claimed.data["content"] == "explicit new"
        second.settle_user_message("owner", claimed, succeeded=True)
        assert first.claim_user_message("owner") is None
        first.set_user_queue_paused("owner", "")
        assert first.claim_user_message("owner").data["content"] == "old"
    finally:
        first.close()
        second.close()


def test_repeated_old_stop_preserves_new_consent_and_new_turn_stop_pauses_it(tmp_path):
    store = DurableUserMessageQueue(session_id="shared", root_dir=tmp_path)
    try:
        store.set_user_queue_paused("owner", "stopped-A")
        fresh = _command(_queue_explicit_send=store.paused_user_queues()["owner"])
        store.save({"owner": [fresh]}, {})
        store.set_user_queue_paused("owner", "stopped-A")
        claimed = store.claim_user_message("owner")
        assert claimed is not None
        assert store.settle_user_message("owner", claimed, succeeded=False)
        store.set_user_queue_paused("owner", "stopped-B")
        assert store.claim_user_message("owner") is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_interrupt_message_fence_preserves_new_input_on_duplicate_stop(session):
    manager = session.run_manager
    active = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    manager.run_tasks["owner"] = active
    manager.run_task_ids["owner"] = "task-A"
    manager.cancel_events["owner"] = asyncio.Event()
    session._conversation_streams["owner"] = {"message_id": "A"}
    await handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "A"})
    await session.command_dispatcher._handle_command_inner(_command("fresh", "fresh"))
    await handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "A"})
    fresh = manager.dequeue_user_message("owner")
    assert fresh is not None and fresh.data["content"] == "fresh"
    manager.finish_user_message_dispatch("owner", fresh, succeeded=True)

    next_run = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    manager.run_tasks["owner"] = next_run
    manager.run_task_ids["owner"] = "task-B"
    manager.cancel_events["owner"] = asyncio.Event()
    session._conversation_streams["owner"] = {"message_id": "B"}
    await session.command_dispatcher._handle_command_inner(_command("queued while B runs", "C"))
    await handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "B"})
    assert manager.dequeue_user_message("owner") is None
    assert manager.queued_user_message_snapshot("owner")[0]["paused"] is True


@pytest.mark.asyncio
async def test_late_stop_after_terminal_does_not_pause_followups(session):
    manager = session.run_manager
    manager.enqueue_user_message("owner", _command("follow up", "queued"))
    session._conversation_streams["owner"] = {"message_id": "done", "terminal_fenced": True}
    await handle_interrupt_command(session, {"conversation_id": "owner", "message_id": "done"})
    assert not manager.user_queue_paused("owner")
    assert manager.dequeue_user_message("owner") is not None


@pytest.mark.asyncio
async def test_new_input_does_not_resume_old_queue_but_explicit_queue_send_does(session):
    manager = session.run_manager
    manager.enqueue_user_message("owner", _command("old", "old"))
    manager.set_user_queue_paused("owner", "stopped-first")
    await session.command_dispatcher._handle_command_inner(_command("new", "new"))
    assert session.started == ["new"]
    assert manager.user_queue_paused("owner")
    assert manager.dequeue_user_message("owner") is None
    await handle_user_message_queue_steer(session, {"conversation_id": "owner", "message_id": "old"})
    await asyncio.gather(*list(session.command_dispatcher.command_tasks))
    assert session.started == ["new", "old"]
    assert not manager.user_queue_paused("owner")


@pytest.mark.asyncio
async def test_empty_historical_pause_does_not_pause_future_queue(session):
    session.run_manager.set_user_queue_paused("owner", "stopped-first")
    await session.command_dispatcher._handle_command_inner(_command())
    assert session.started == ["work"]
    assert not session.run_manager.user_queue_paused("owner")
