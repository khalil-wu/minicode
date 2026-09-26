import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from backend.conversations.repository import ConversationRepository
from backend.services.workspace_history import workspace_conversation_ids
from backend.workspace import recent_projects
from backend.ws.handlers import conversation as conversation_handlers
from backend.ws.handlers.workspace import handle_workspace_recent_remove


def test_workspace_conversation_ids_match_only_the_requested_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace), title="Keep my task")
    repo.create_conversation(workspace_root=str(tmp_path / "other"))
    # A trailing "/." must resolve to the same workspace identity.
    assert workspace_conversation_ids(repo, str(workspace / ".")) == [record.id]


def test_remove_active_workspace_switches_away_and_keeps_live_history(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    store = recent_projects.RecentProjectStore()
    store.add(str(workspace), "project")
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace), title="Preserved")
    session = SimpleNamespace(conversation_repo=repo, active_conversation_id=record.id,
        send_payload=AsyncMock(), emit_command_result=AsyncMock(), send_event=AsyncMock())
    monkeypatch.setattr(conversation_handlers, "_conversation_has_active_run", lambda *_: False)

    async def close_current(owner, data):
        assert data["workspace_root"] == ""
        # Removing the entry never copies transcripts into the project.
        assert not (workspace / ".minicode").exists()
        owner.active_conversation_id = "unbound"
        return True

    monkeypatch.setattr(conversation_handlers, "handle_conversation_create", close_current)
    asyncio.run(handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True}))

    assert store.list() == []
    # The conversation stays in the app store, so reopening restores it by id.
    assert repo.get_conversation(record.id).title == "Preserved"
    assert not (workspace / ".minicode").exists()
    assert session.emit_command_result.call_args.kwargs["data"]["closed_active"] is True
    store.add(str(workspace), "project")
    assert workspace_conversation_ids(repo, str(workspace)) == [record.id]


def test_remove_running_workspace_does_not_change_navigation_or_files(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    store = recent_projects.RecentProjectStore()
    store.add(str(workspace), "project")
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace))
    session = SimpleNamespace(conversation_repo=repo, active_conversation_id=record.id,
        send_payload=AsyncMock(), emit_command_result=AsyncMock(), send_event=AsyncMock())
    monkeypatch.setattr(conversation_handlers, "_conversation_has_active_run", lambda *_: True)
    asyncio.run(handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True}))
    assert len(store.list()) == 1
    assert not (workspace / ".minicode").exists()
    assert session.send_event.call_args.args[0].data["level"] == "error"


@pytest.mark.asyncio
async def test_real_session_removal_switches_to_an_unbound_conversation(tmp_path, monkeypatch):
    from backend.tests.test_ws_cold_connection import _connection, _release_session
    from backend.ws.manager import WebSocketManager

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    recent_projects.RecentProjectStore().add(str(workspace), "project")
    connection = _connection(tmp_path)
    session, _ = await WebSocketManager().connect(**connection)
    record = session.conversation_repo.create_conversation(workspace_root=str(workspace), title="Keep me")
    session.active_conversation_id = record.id
    try:
        await handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True})
        active = session.conversation_repo.get_conversation(session.active_conversation_id)
        assert active.id != record.id
        assert active.workspace_root == ""
        assert session.session_lifecycle.workspace_root is None
        assert recent_projects.RecentProjectStore().list() == []
        # No transcript is ever written into the project directory.
        assert not (workspace / ".minicode").exists()
        assert session.conversation_repo.get_conversation(record.id) is not None
        result = next(event for event in reversed(connection["websocket"].sent)
                      if event.get("command") == "workspace.recent.remove")
        assert result["level"] == "success"
    finally:
        await _release_session(session)
