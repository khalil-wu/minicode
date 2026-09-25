import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

from backend.conversations.repository import ConversationRepository
from backend.services.workspace_history import preserve_workspace_history, workspace_conversation_ids
from backend.workspace import recent_projects
from backend.ws.handlers import conversation as conversation_handlers
from backend.ws.handlers.workspace import handle_workspace_recent_remove


def test_workspace_snapshot_retains_transcript_context_and_archived_history(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace), title="Keep my task",
        transcript=[{"id": "u1", "role": "user", "content": "Remember this task"}],
        context_snapshot={"history": [{"role": "user", "content": "Full model context"}]})
    record.archived = True
    repo.save_conversation(record)
    other = repo.create_conversation(workspace_root=str(tmp_path / "other"))
    ids = workspace_conversation_ids(repo, str(workspace / "."))
    assert ids == [record.id]
    directory = preserve_workspace_history(repo, str(workspace), ids)
    saved = ConversationRepository(directory).get_conversation(record.id)
    assert saved.title == record.title
    assert saved.transcript == record.transcript
    assert saved.context_snapshot == record.context_snapshot
    assert saved.archived is True
    assert repo.get_conversation(record.id).revision == record.revision
    assert repo.get_conversation(other.id) is not None


def test_remove_active_workspace_saves_before_closing_and_keeps_live_history(tmp_path, monkeypatch):
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
        assert (workspace / ".minicode" / "conversations" / f"{record.id}.json").is_file()
        owner.active_conversation_id = "unbound"
        return True

    monkeypatch.setattr(conversation_handlers, "handle_conversation_create", close_current)
    asyncio.run(handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True}))
    assert store.list() == []
    assert repo.get_conversation(record.id).title == "Preserved"
    assert session.emit_command_result.call_args.kwargs["data"]["closed_active"] is True
    # Opening again only changes the navigation registry; history kept its id.
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


def test_backup_failure_leaves_workspace_and_history_in_place(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / ".minicode").write_text("not a directory")
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    store = recent_projects.RecentProjectStore()
    store.add(str(workspace), "project")
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace))
    session = SimpleNamespace(conversation_repo=repo, active_conversation_id=record.id,
        send_payload=AsyncMock(), emit_command_result=AsyncMock(), send_event=AsyncMock())
    monkeypatch.setattr(conversation_handlers, "_conversation_has_active_run", lambda *_: False)
    asyncio.run(handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True}))
    assert len(store.list()) == 1
    assert session.active_conversation_id == record.id
    assert repo.get_conversation(record.id) is not None
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
        saved = ConversationRepository(workspace / ".minicode" / "conversations").get_conversation(record.id)
        assert saved.title == "Keep me"
        assert session.conversation_repo.get_conversation(record.id) is not None
        result = next(event for event in reversed(connection["websocket"].sent)
                      if event.get("command") == "workspace.recent.remove")
        assert result["level"] == "success"
    finally:
        await _release_session(session)
