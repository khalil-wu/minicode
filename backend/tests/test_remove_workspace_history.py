import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest

from backend.atomic_io import canonical_file_path_key
from backend.conversations.repository import ConversationRepository
from backend.services.workspace_history import workspace_conversation_ids
from backend.workspace import recent_projects
from backend.ws.handlers import conversation as conversation_handlers
from backend.ws.handlers.workspace import handle_workspace_recent_remove


def session_for(repository, conversation_id):
    return SimpleNamespace(conversation_repo=repository, active_conversation_id=conversation_id,
        ws_manager=None, is_connected=True, send_payload=AsyncMock(), send_conversation_list=AsyncMock(),
        emit_command_result=AsyncMock(), send_event=AsyncMock())


def test_workspace_conversation_ids_match_real_project_and_legacy_source_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    workspace = tmp_path / "project"
    workspace.mkdir()
    secondary, other = tmp_path / "secondary", tmp_path / "other"
    secondary.mkdir(); other.mkdir()
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace), title="Keep my task")
    legacy = repo.create_conversation(workspace_root=str(secondary), execution_cwd=str(other))
    assert record.project_id is None and legacy.project_id is None
    project = recent_projects.RecentProjectStore().create("Two sources", [str(workspace), str(secondary)])
    owned = repo.create_conversation(workspace_root=str(workspace))
    moved = repo.create_conversation(workspace_root=str(workspace))
    repo.set_conversation_project(moved.id, str(other))
    removed = repo.create_conversation(workspace_root=str(workspace))
    repo.set_conversation_project(removed.id, "")
    # A trailing "/." must resolve to the same workspace identity.
    assert set(workspace_conversation_ids(repo, str(workspace / "."))) == {record.id, legacy.id, owned.id}
    assert owned.project_id == project.path


def test_remove_active_workspace_keeps_owner_pin_cwd_and_live_history(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    store = recent_projects.RecentProjectStore()
    store.add(str(workspace), "project")
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace), title="Preserved")
    repo.set_conversation_pinned(record.id, True)
    session = session_for(repo, record.id)
    monkeypatch.setattr(conversation_handlers, "_conversation_has_active_run", lambda *_: False)

    create = AsyncMock()
    monkeypatch.setattr(conversation_handlers, "handle_conversation_create", create)
    asyncio.run(handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True}))

    assert store.list() == []
    # The conversation stays in the app store, so reopening restores it by id.
    assert repo.get_conversation(record.id).title == "Preserved"
    assert not (workspace / ".minicode").exists()
    assert session.active_conversation_id == record.id
    create.assert_not_awaited()
    cold = ConversationRepository(repo.storage_root).get_conversation(record.id)
    assert cold.project_id == "" and cold.pinned
    assert cold.execution_cwd == record.execution_cwd
    assert session.emit_command_result.call_args.kwargs["data"] == {"path": str(workspace), "removed": True}
    store.add(str(workspace), "project")
    assert workspace_conversation_ids(repo, str(workspace)) == []


def test_remove_running_workspace_keeps_the_run_and_navigation(tmp_path, monkeypatch):
    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    store = recent_projects.RecentProjectStore()
    store.add(str(workspace), "project")
    repo = ConversationRepository(tmp_path / "live")
    record = repo.create_conversation(workspace_root=str(workspace))
    session = session_for(repo, record.id)
    session.agent_task = object()
    task = session.agent_task
    check_run = Mock(side_effect=AssertionError("Project removal does not query or stop a run"))
    monkeypatch.setattr(conversation_handlers, "_conversation_has_active_run", check_run)
    asyncio.run(handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True}))
    assert store.list() == []
    assert session.active_conversation_id == record.id
    assert session.agent_task is task
    assert not (workspace / ".minicode").exists()
    assert session.emit_command_result.call_args.kwargs["level"] == "success"
    check_run.assert_not_called()


@pytest.mark.asyncio
async def test_real_session_removal_preserves_the_current_conversation(tmp_path, monkeypatch):
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
    selected_workspace = session.session_lifecycle.workspace_root
    try:
        await handle_workspace_recent_remove(session, {"path": str(workspace), "preserve_history": True})
        active = session.conversation_repo.get_conversation(session.active_conversation_id)
        assert active.id == record.id
        assert active.project_id == ""
        assert active.workspace_root == str(workspace)
        assert active.execution_cwd == record.execution_cwd
        assert session.session_lifecycle.workspace_root == selected_workspace
        assert recent_projects.RecentProjectStore().list() == []
        # No transcript is ever written into the project directory.
        assert not (workspace / ".minicode").exists()
        assert session.conversation_repo.get_conversation(record.id) is not None
        result = next(event for event in reversed(connection["websocket"].sent)
                      if event.get("command") == "workspace.recent.remove")
        assert result["level"] == "success"
    finally:
        await _release_session(session)


@pytest.mark.asyncio
async def test_remove_updates_all_windows_keeps_execution_and_clears_only_its_section_key(tmp_path, monkeypatch):
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    root, other = tmp_path / "project", tmp_path / "other"
    root.mkdir(); other.mkdir()
    store = recent_projects.RecentProjectStore()
    project = store.create("Remove me", [str(root)])
    kept = store.create("Keep me", [str(other)])
    repo, foreign = ConversationRepository(tmp_path / "live"), ConversationRepository(tmp_path / "foreign")
    owned = repo.create_conversation(workspace_root=str(root), context_snapshot={"original": "saved hidden checkpoint"})
    repo.set_conversation_pinned(owned.id, True)
    before = repo.get_conversation(owned.id).to_dict()
    moved = repo.create_conversation(workspace_root=str(root))
    repo.set_conversation_project(moved.id, kept.path)
    foreign_chat = foreign.create_conversation(workspace_root=str(root))
    section = repo.thread_sections.create("Keep this section")
    repo.thread_sections.move_project(project.path, section["id"])
    repo.thread_sections.move("project:" + canonical_file_path_key(kept.path), section["id"])
    sessions = [session_for(repo, owned.id), session_for(ConversationRepository(repo.storage_root), moved.id), session_for(foreign, foreign_chat.id)]
    manager = SimpleNamespace(iter_sessions=lambda: sessions)
    for session in sessions: session.ws_manager = manager
    await handle_workspace_recent_remove(sessions[0], {"path": project.path, "preserve_history": True})
    assert [session.active_conversation_id for session in sessions] == [owned.id, moved.id, foreign_chat.id]
    for index, session in enumerate(sessions):
        assert session.send_conversation_list.await_count == (2 if index < 2 else 1)
        payload = session.send_payload.await_args.args[0]
        assert payload["type"] == "workspace.recent.list"
        assert [item["path"] for item in payload["projects"]] == [kept.path]
    cold = ConversationRepository(repo.storage_root).get_conversation(owned.id).to_dict()
    assert cold["project_id"] == ""
    for key in ("pinned", "pinned_at", "execution_cwd", "workspace_root", "context_snapshot", "transcript", "updated_at", "content_revision"):
        assert cold[key] == before[key]
    assert ConversationRepository(foreign.storage_root).get_conversation(foreign_chat.id).project_id == ""
    assert repo.get_conversation(moved.id).project_id == kept.path
    assert repo.get_conversation(moved.id).execution_cwd == str(root)
    expected = ["project:" + canonical_file_path_key(kept.path)]
    assert repo.thread_sections.list()[0]["item_keys"] == expected
    store.add(project.path, "Re-added")
    assert repo.thread_sections.list()[0]["item_keys"] == expected


@pytest.mark.asyncio
async def test_real_metadata_write_failure_keeps_project_and_retry_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(recent_projects, "DEFAULT_STORE_PATH", tmp_path / "recent.json")
    root = tmp_path / "project"
    root.mkdir()
    project = recent_projects.RecentProjectStore().create("Keep on failure", [str(root)])
    repo = ConversationRepository(tmp_path / "live")
    chat = repo.create_conversation(workspace_root=str(root))
    session = session_for(repo, chat.id)
    write = repo._safe_write_text
    manifest = repo._manifest_path_for(chat.id)
    def fail_manifest(path, content, encoding="utf-8"):
        if path == manifest:
            raise OSError("Actual metadata write failed")
        return write(path, content, encoding=encoding)
    monkeypatch.setattr(repo, "_safe_write_text", fail_manifest)
    with pytest.raises(OSError, match="Actual metadata write failed"):
        await handle_workspace_recent_remove(session, {"path": project.path, "preserve_history": True})
    assert recent_projects.RecentProjectStore().list()[0].path == project.path
    assert ConversationRepository(repo.storage_root).get_conversation(chat.id).project_id == project.path
    session.emit_command_result.assert_not_awaited()
    session.send_payload.assert_not_awaited()
    monkeypatch.setattr(repo, "_safe_write_text", write)
    await handle_workspace_recent_remove(session, {"path": project.path, "preserve_history": True})
    assert recent_projects.RecentProjectStore().list() == []
    assert ConversationRepository(repo.storage_root).get_conversation(chat.id).project_id == ""
    assert session.active_conversation_id == chat.id
