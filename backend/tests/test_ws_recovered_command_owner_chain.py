from __future__ import annotations

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from backend.agent.message import UserCommand
from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.command_scope import resolve_command_scope
from backend.ws.durable_user_queue import DurableUserMessageQueue


def _session(tmp_path, queue):
    conversation = SimpleNamespace(workspace_root=str(tmp_path), worktree_path="")
    lock = asyncio.Lock()
    sent = []

    async def send_payload(payload, **_kwargs):
        sent.append(payload)

    async def send_event(event):
        sent.append(event.to_ws_message())

    session = SimpleNamespace(
        session_id="recovered-session", active_conversation_id=None,
        connection_generation=1, is_connected=True,
        _extension_shutdown_requested=False, ws_manager=None,
        run_manager=SimpleNamespace(durable_client_commands=queue, durable_queue=queue),
        conversation_repo=SimpleNamespace(
            get_conversation=lambda _owner: conversation,
            get_conversation_summary=lambda _owner: conversation,
        ),
        session_lifecycle=SimpleNamespace(
            workspace_root=tmp_path, current_workspace_root=lambda: tmp_path,
            workspace_root_for_conversation=lambda _conversation=None: tmp_path,
        ),
        event_outbox=SimpleNamespace(
            bind_client_command=lambda *_args, **_kwargs: nullcontext(),
            bind_connection_generation=lambda _generation: nullcontext(),
        ),
        conversation_lifecycle_lock=lambda: lock,
        resolve_requested_workspace=lambda _requested=None: tmp_path,
        send_payload=send_payload, send_event=send_event,
        emit_command_result=send_payload,
    )
    return session, sent


async def _drain(dispatcher):
    if dispatcher.command_tasks:
        await asyncio.gather(*tuple(dispatcher.command_tasks))


@pytest.mark.asyncio
@pytest.mark.parametrize("command_type", ["subagent.cancel", "preview.launch.start"])
async def test_cold_session_keeps_acked_owner_action_until_restore_then_executes_once(tmp_path, command_type):
    queue_root = tmp_path / "queue"
    original = DurableUserMessageQueue(session_id="recovered-session", root_dir=queue_root)
    command = UserCommand(type=command_type, data={
        "client_command_id": "cmd_owned_action", "conversation_id": "conversation-a",
        "workspace_root": str(tmp_path), "subagent_id": "child", "name": "dev",
    })
    assert original.persist_client_command(command)
    # The server already sent the durable ACK, then exited before dispatch.
    original.close()

    recovered = DurableUserMessageQueue(session_id="recovered-session", root_dir=queue_root)
    session, sent = _session(tmp_path, recovered)
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "receipts")
    observed = []

    async def handle(command):
        if command.type in {"session.restore", "session.sync", "conversation.switch"}:
            session.active_conversation_id = command.data.get("conversation_id", "conversation-a")
            await session.send_payload({"type": "session.restored", "active_conversation_id": session.active_conversation_id})
            return
        scope = resolve_command_scope(session, command.data)
        observed.append((command.type, scope.conversation_id, command.data["client_command_id"]))

    dispatcher._handle_command_inner = handle
    try:
        await dispatcher._replay_pending_client_commands(1)
        await _drain(dispatcher)
        assert observed == []
        assert [item.data["client_command_id"] for item in recovered.pending_client_commands()] == ["cmd_owned_action"]
        assert not dispatcher._client_command_seen(command)

        await dispatcher._handle_command(UserCommand(type="session.restore", data={}))
        await _drain(dispatcher)
        assert observed == [(command_type, "conversation-a", "cmd_owned_action")]
        assert sent[0]["type"] == "session.restored"
        assert recovered.pending_client_commands() == []
        assert dispatcher._client_command_seen(command)

        # Repeated restore and a crash between completion-log write and queue
        # removal must not perform the already committed action again.
        assert recovered.persist_client_command(command)
        await dispatcher._handle_command(UserCommand(type="session.sync", data={}))
        await _drain(dispatcher)
        assert observed == [(command_type, "conversation-a", "cmd_owned_action")]
        assert recovered.pending_client_commands() == []
    finally:
        await _drain(dispatcher)
        recovered.close()


@pytest.mark.asyncio
async def test_recovery_runs_global_commands_and_retains_another_conversations_action(tmp_path):
    queue = DurableUserMessageQueue(session_id="recovered-session", root_dir=tmp_path / "queue")
    commands = [
        UserCommand(type="env.list", data={"client_command_id": "cmd_global"}),
        UserCommand(type="subagent.cancel", data={"client_command_id": "cmd_owner", "conversation_id": "conversation-a", "subagent_id": "child"}),
    ]
    for command in commands:
        assert queue.persist_client_command(command)
    session, _sent = _session(tmp_path, queue)
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "receipts")
    observed = []

    async def handle(command):
        if command.type == "conversation.switch":
            session.active_conversation_id = command.data["conversation_id"]
        else:
            observed.append(command.type)

    dispatcher._handle_command_inner = handle
    try:
        await dispatcher._replay_pending_client_commands(1)
        await _drain(dispatcher)
        assert observed == ["env.list"]
        await dispatcher._handle_command(UserCommand(type="conversation.switch", data={"conversation_id": "conversation-b"}))
        await _drain(dispatcher)
        assert observed == ["env.list"]
        assert [item.data["client_command_id"] for item in queue.pending_client_commands()] == ["cmd_owner"]

        await dispatcher._handle_command(UserCommand(type="conversation.switch", data={"conversation_id": "conversation-a"}))
        await _drain(dispatcher)
        assert observed == ["env.list", "subagent.cancel"]
        assert queue.pending_client_commands() == []
    finally:
        await _drain(dispatcher)
        queue.close()


@pytest.mark.asyncio
async def test_owner_relative_action_persists_selected_owner_before_ack(tmp_path):
    queue = DurableUserMessageQueue(session_id="recovered-session", root_dir=tmp_path / "queue")
    session, sent = _session(tmp_path, queue)
    session.active_conversation_id = "conversation-a"
    dispatcher = SessionCommandDispatcher(session, root_dir=tmp_path / "receipts")
    messages = iter(['{"type":"preview.refresh","client_command_id":"cmd_relative"}'])

    async def receive_text():
        try:
            return next(messages)
        except StopIteration:
            raise WebSocketDisconnect()

    session.ws = SimpleNamespace(receive_text=receive_text)
    dispatcher._schedule_durable_client_command = lambda *_args: None
    try:
        with pytest.raises(WebSocketDisconnect):
            await dispatcher.run(1)
        pending = queue.pending_client_commands()
        assert len(pending) == 1
        assert pending[0].data["owner_conversation_id"] == "conversation-a"
        assert sent == [{"type": "client.command.ack", "client_command_id": "cmd_relative", "command_type": "preview.refresh"}]
    finally:
        queue.close()
