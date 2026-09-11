from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.ws.handlers import conversation, workspace
from backend.ws.command_dispatcher import SessionCommandDispatcher


@pytest.mark.parametrize("command", ["workspace.set", "workspace.switch", "workspace.import", "conversation.create"])
def test_open_project_creates_new_owned_history(monkeypatch, tmp_path, command):
    old_root, new_root = tmp_path / "alpha", tmp_path / "beta"
    old_root.mkdir()
    new_root.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    old = repo.create_conversation(workspace_root=str(old_root), transcript=[{"role": "user", "content": "Alpha task"}], context_snapshot={"project": "alpha"})
    before = repo.get_conversation(old.id)
    session = SimpleNamespace(
        active_conversation_id=old.id, conversation_repo=repo,
        git_branch_for=Mock(return_value="main"), switch_workspace_for_conversation=AsyncMock(return_value=True),
        load_active_conversation_snapshot=Mock(return_value=False), sync_permission_mode_with_active_conversation=Mock(),
        send_payload=AsyncMock(), emit_command_result=AsyncMock(),
        runtime_snapshot=lambda: {"active_conversation_id": session.active_conversation_id},
    )
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    monkeypatch.setattr(conversation, "_broadcast_conversation_lists", AsyncMock(return_value=[]))
    handler = conversation.handle_conversation_create if command == "conversation.create" else workspace.HANDLERS[command]
    asyncio.run(handler(session, {"path": str(new_root), "workspace_root": str(new_root), "permission_mode": "bypass"}))
    created = repo.get_conversation(session.active_conversation_id)
    assert created.id != old.id
    assert created.workspace_root == str(new_root)
    assert created.git_branch == "main"
    assert created.permission_mode == "bypass"
    assert created.transcript == []
    assert created.context_snapshot == {}
    assert ConversationRepository(tmp_path / "conversations").get_conversation(old.id) == before
    switched = session.send_payload.await_args.args[0]
    assert switched["conversation_id"] == created.id
    assert switched["conversation"]["workspace_root"] == str(new_root)
    assert session.emit_command_result.await_args.args[0] == command
    session.load_active_conversation_snapshot.assert_called_once_with(created.id, {}, notify=True, defer_start=True)


def test_failed_workspace_initialization_keeps_previous_owner(monkeypatch, tmp_path):
    old_root, new_root = tmp_path / "alpha", tmp_path / "beta"
    old_root.mkdir()
    new_root.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    old = repo.create_conversation(workspace_root=str(old_root))
    created_ids = []
    async def fail_activation(created, **_kwargs):
        assert session.active_conversation_id == old.id
        created_ids.append(created.id)
        return False
    session = SimpleNamespace(
        active_conversation_id=old.id, conversation_repo=repo,
        git_branch_for=Mock(return_value="main"), switch_workspace_for_conversation=fail_activation,
        send_payload=AsyncMock(), load_active_conversation_snapshot=Mock(),
    )
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    asyncio.run(workspace.handle_workspace_set(session, {"path": str(new_root)}))
    assert session.active_conversation_id == old.id
    assert repo.get_conversation(old.id).workspace_root == str(old_root)
    assert repo.get_conversation(created_ids[0]) is None
    session.send_payload.assert_not_awaited()
    session.load_active_conversation_snapshot.assert_not_called()


def test_invalid_workspace_does_not_create_or_move_conversation(tmp_path):
    repo = SimpleNamespace(create_conversation=Mock())
    session = SimpleNamespace(active_conversation_id="old", conversation_repo=repo, send_event=AsyncMock())
    asyncio.run(conversation.handle_conversation_create(session, {"workspace_root": str(tmp_path / "missing")}))
    repo.create_conversation.assert_not_called()
    assert session.active_conversation_id == "old"
    assert session.send_event.await_args.args[0].data["level"] == "error"


def test_stale_message_cannot_rebind_an_existing_project(monkeypatch, tmp_path):
    old_root, new_root = tmp_path / "alpha", tmp_path / "beta"
    old_root.mkdir()
    new_root.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    old = repo.create_conversation(workspace_root=str(old_root))
    session = SimpleNamespace(conversation_repo=repo, send_event=AsyncMock(), activate_workspace_path=AsyncMock())
    monkeypatch.setattr("backend.workspace.trust.is_workspace_trusted", lambda _: True)
    dispatcher = object.__new__(SessionCommandDispatcher)
    dispatcher._session = session
    assert asyncio.run(dispatcher._handle_user_message_workspace(str(new_root), old.id)) == (False, old.id)
    assert repo.get_conversation(old.id).workspace_root == str(old_root)
    session.activate_workspace_path.assert_not_awaited()
    assert session.send_event.await_args.args[0].data["error_code"] == "workspace_owner_mismatch"
