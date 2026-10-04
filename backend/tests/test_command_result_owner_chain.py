from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.websockets import WebSocketState

from backend.ws.command_dispatcher import SessionCommandDispatcher
from backend.ws.event_outbox import EventOutbox


class Socket:
    client_state = WebSocketState.CONNECTED
    application_state = WebSocketState.CONNECTED

    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


def outbox(tmp_path):
    socket = Socket()
    return EventOutbox(
        session_id="command-owner", websocket=socket, replay_root=tmp_path / "replay", replay_limit=50,
        cleanup_tasks=set(), has_active_run=lambda: False,
        requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=set(),
    ), socket


@pytest.mark.asyncio
async def test_command_result_keeps_captured_scope_and_resets_context(tmp_path):
    events, socket = outbox(tmp_path)
    with events.bind_client_command("cmd-origin", "user_message", owner=("conversation-a", "/workspace-a")):
        await asyncio.sleep(0)
        await events.send_payload({"type": "command.result", "command": "skills", "data": {"ui_action": "open_skills_marketplace"}}, log_context="owner")
    assert socket.messages[0]["conversation_id"] == "conversation-a"
    assert socket.messages[0]["workspace_root"] == "/workspace-a"
    assert socket.messages[0]["client_command_id"] == "cmd-origin"
    await events.send_payload({"type": "command.result", "command": "global"}, log_context="unbound")
    assert "conversation_id" not in socket.messages[-1]
    await events.drain_delivery()
    await events.drain_persistence()


@pytest.mark.asyncio
async def test_explicit_result_owner_wins_over_originating_command(tmp_path):
    events, socket = outbox(tmp_path)
    with events.bind_client_command("cmd-switch", "conversation.create", owner=("conversation-a", "/workspace-a")):
        await events.send_payload({"type": "command.result", "command": "conversation.create",
                                   "data": {"conversation_id": "conversation-b", "workspace_root": "/workspace-b"}}, log_context="switch")
    assert socket.messages[0]["conversation_id"] == "conversation-b"
    assert socket.messages[0]["workspace_root"] == "/workspace-b"
    await events.drain_delivery()
    await events.drain_persistence()


@pytest.mark.asyncio
async def test_dispatcher_captures_command_owner_before_capacity_wait(tmp_path):
    events, socket = outbox(tmp_path)
    host = SimpleNamespace(active_conversation_id="conversation-a", root="/workspace-a", event_outbox=events)
    host.session_lifecycle = SimpleNamespace(workspace_root_for_conversation=lambda: host.root)
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._session = host
    dispatcher._user_message_admissions = {}
    dispatcher._command_semaphore = asyncio.Semaphore(0)
    dispatcher._client_command_id = lambda _: "cmd-waiting"

    async def handle(command, **kwargs):
        await events.send_payload({"type": "command.result", "command": "settings", "data": {"ui_action": "open_settings:provider"}}, log_context="settings")
        return True

    dispatcher._handle_command = handle
    command = SimpleNamespace(type="owned_command", data={})
    waiting = asyncio.create_task(dispatcher._dispatch_client_command(command, 1))
    await asyncio.sleep(0)
    host.active_conversation_id, host.root = "conversation-b", "/workspace-b"
    dispatcher._command_semaphore.release()
    assert await waiting
    assert socket.messages[0]["conversation_id"] == "conversation-a"
    assert socket.messages[0]["workspace_root"] == "/workspace-a"
    await events.drain_delivery()
    await events.drain_persistence()


@pytest.mark.asyncio
@pytest.mark.parametrize("data,expected", [
    ({"conversation_id": "conversation-b", "workspace_root": ""}, ("conversation-b", "")),
    ({"conversation_id": "conversation-a"}, ("conversation-a", "/workspace-a")),
    ({"conversation_id": "missing"}, ("missing", "")),
])
async def test_dispatcher_owner_does_not_borrow_active_workspace(tmp_path, data, expected):
    events, socket = outbox(tmp_path)
    conversation = SimpleNamespace(workspace_root="/workspace-a")
    host = SimpleNamespace(active_conversation_id="conversation-b", event_outbox=events,
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda cid: conversation if cid == "conversation-a" else None),
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda conv=None: conv.workspace_root if conv is not None else "/workspace-b"))
    dispatcher = SessionCommandDispatcher.__new__(SessionCommandDispatcher)
    dispatcher._session = host
    dispatcher._user_message_admissions = {}
    dispatcher._command_semaphore = asyncio.Semaphore(1)
    dispatcher._client_command_id = lambda _: "cmd-owner"

    async def handle(command, **kwargs):
        await events.send_payload({"type": "command.result", "command": "owned"}, log_context="owner")
        return True

    dispatcher._handle_command = handle
    await dispatcher._dispatch_client_command(SimpleNamespace(type="owned", data=data), 1)
    assert (socket.messages[0]["conversation_id"], socket.messages[0]["workspace_root"]) == expected
    await events.drain_delivery()
    await events.drain_persistence()
