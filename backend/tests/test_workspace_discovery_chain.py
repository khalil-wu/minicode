from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from watchdog.events import FileModifiedEvent
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.conversations.repository import ConversationRepository
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import validate_tool_input
from backend.tools.fuzzy_search_tool import FuzzySearchTool
from backend.tools.list_files import ListFilesTool
from backend.workspace.context import WorkspaceContext
from backend.workspace.file_state_cache import FileStateCache
from backend.workspace.file_watcher import WorkspaceFileWatcher
from backend.workspace.fuzzy_search import FuzzySearchEngine
from backend.workspace.recent_projects import RecentProjectPersistenceError, RecentProjectStore
from backend.workspace.service import WorkspaceService


@pytest.mark.asyncio
async def test_directory_changed_during_scan_is_visible_on_the_next_listing(tmp_path, monkeypatch):
    (tmp_path / "original.py").write_text("original", encoding="utf-8")
    original_iterdir = Path.iterdir

    def enumerate_then_mutate(directory):
        entries = list(original_iterdir(directory))
        if directory == tmp_path:
            (tmp_path / "late.py").write_text("late", encoding="utf-8")
        return iter(entries)

    context = ToolExecutionContext(permission=PermissionContext(mode="confirm"), workspace_root=tmp_path)
    with monkeypatch.context() as mutation:
        mutation.setattr(Path, "iterdir", enumerate_then_mutate)
        first = await ListFilesTool().execute({"path": "."}, context)
    second = await ListFilesTool().execute({"path": "."}, context)
    assert "late.py" not in first.content
    assert "late.py" in second.content


@pytest.mark.asyncio
async def test_stop_retires_a_watchdog_callback_queued_before_its_loop_admission(tmp_path):
    callbacks = []
    watcher = WorkspaceFileWatcher(tmp_path, lambda *change: callbacks.append(change), stability_threshold=0)
    watcher.start()
    watcher._create_handler().on_any_event(FileModifiedEvent(str(tmp_path / "app.py")))
    watcher.stop()
    await asyncio.sleep(0.03)
    assert callbacks == []
    assert watcher._debounce_tasks == {}


@pytest.mark.asyncio
async def test_real_windows_watcher_notifies_a_live_file_change(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old", encoding="utf-8")
    changed = asyncio.Event()
    observed = []

    def on_change(path, kind):
        if path == target:
            observed.append((kind, path.read_text(encoding="utf-8")))
            changed.set()

    watcher = WorkspaceFileWatcher(tmp_path, on_change, stability_threshold=0.01)
    watcher.start()
    try:
        target.write_text("new", encoding="utf-8")
        await asyncio.wait_for(changed.wait(), timeout=3)
        assert observed[-1][1] == "new"
    finally:
        watcher.stop()


@pytest.mark.asyncio
async def test_project_statistics_share_nested_ignore_and_real_source_discovery(tmp_path):
    (tmp_path / "models").mkdir()
    (tmp_path / "models/account.py").write_text("class Account: pass", encoding="utf-8")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/.gitignore").write_text("*.ignored.py\n!keep.ignored.py\n", encoding="utf-8")
    (tmp_path / "pkg/private.ignored.py").write_text("private = True", encoding="utf-8")
    (tmp_path / "pkg/keep.ignored.py").write_text("keep = True", encoding="utf-8")
    context = WorkspaceContext(tmp_path)
    metadata = await context.initialize()
    files = context.get_file_list()
    assert "models/account.py" in files
    assert "pkg/private.ignored.py" not in files
    assert "pkg/keep.ignored.py" in files
    assert metadata.file_count == len(files)


def test_fuzzy_index_excludes_the_actual_application_state_path(tmp_path, monkeypatch):
    store = tmp_path / "managed-storage"
    store.mkdir()
    (store / "audit-fixture-receipt.py").write_text("receipt = True", encoding="utf-8")
    (tmp_path / "audit-fixture-source.py").write_text("source = True", encoding="utf-8")
    monkeypatch.setattr("backend.security.sensitive_files.application_state_roots", lambda: (store,))
    paths = [match.path.name for match in FuzzySearchEngine(tmp_path).search("audit-fixture")]
    assert paths == ["audit-fixture-source.py"]


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_fuzzy_result_limit_is_rejected_at_the_existing_tool_input_boundary(tmp_path, limit):
    assert validate_tool_input(FuzzySearchTool(tmp_path), {"query": "app", "max_results": limit})


def test_hidden_source_is_in_the_editor_project_index_and_actual_read_snapshot(tmp_path):
    (tmp_path / ".storybook").mkdir()
    source = "export const theme = 'dark';\r\n"
    (tmp_path / ".storybook/preview.ts").write_bytes(source.encode("utf-8"))
    (tmp_path / "app.ts").write_text("import {theme} from './.storybook/preview';", encoding="utf-8")
    service = WorkspaceService(lambda: tmp_path)
    indexed = {item.path: item for item in service.project_index(include_dependencies=False).files}
    assert indexed[".storybook/preview.ts"].content == source
    assert indexed[".storybook/preview.ts"].content_hash == service.read_file(".storybook/preview.ts").content_hash


def test_file_state_cache_does_not_reuse_a_removed_and_replaced_file(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("old", encoding="utf-8")
    cache = FileStateCache()
    cache.put(target, "old", stat_result=target.stat())
    target.unlink()
    assert cache.get(target) is None
    target.write_text("new", encoding="utf-8")
    cache.put(target, "new", stat_result=target.stat())
    assert cache.get(target).content == "new"


@pytest.mark.parametrize("operation", ["add", "remove", "clear", "clean"])
def test_recent_project_mutation_reports_failed_persistence_and_keeps_its_saved_records(tmp_path, monkeypatch, operation):
    root = tmp_path / "project"
    root.mkdir()
    path = tmp_path / "recent.json"
    store = RecentProjectStore(path)
    store.add(str(root), "project")
    original = path.read_bytes()
    monkeypatch.setattr("backend.workspace.recent_projects.atomic_write_text", Mock(side_effect=PermissionError("controlled failure")))
    if operation == "clean":
        root.rmdir()
    with pytest.raises(RecentProjectPersistenceError):
        if operation == "add":
            store.add(str(tmp_path / "other"), "other")
        elif operation == "remove":
            store.remove(str(root))
        elif operation == "clear":
            store.clear()
        else:
            store.list(clean=True)
    assert path.read_bytes() == original
    assert [project.name for project in store.list()] == ["project"]


@pytest.mark.parametrize("records", [{}, "invalid", [True], [{}], [{"path": None}], [{"path": ""}]])
def test_invalid_recent_record_shape_is_reported_without_rewriting_it(tmp_path, records):
    path = tmp_path / "recent.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(RecentProjectPersistenceError):
        RecentProjectStore(path)
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_recent_save_failure_does_not_publish_an_imported_workspace(tmp_path, monkeypatch):
    from backend.services.workspace_api_service import import_project_payload

    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", tmp_path / "recent.json")
    monkeypatch.setattr("backend.workspace.recent_projects.atomic_write_text", Mock(side_effect=PermissionError("controlled failure")))
    publish = Mock()
    monkeypatch.setattr("backend.workspace.state.set_active_workspace_root", publish)
    with pytest.raises(RecentProjectPersistenceError):
        await import_project_payload(str(tmp_path))
    publish.assert_not_called()


@pytest.mark.asyncio
async def test_failed_recent_removal_does_not_close_the_initiating_conversation(tmp_path, monkeypatch):
    from backend.ws.handlers import conversation
    from backend.ws.handlers.workspace import handle_workspace_recent_remove

    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr("backend.workspace.recent_projects.DEFAULT_STORE_PATH", tmp_path / "recent.json")
    RecentProjectStore().add(str(root), "project")
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(workspace_root=str(root))
    session = SimpleNamespace(conversation_repo=repo, active_conversation_id=record.id, send_payload=AsyncMock(), emit_command_result=AsyncMock())
    monkeypatch.setattr(conversation, "_conversation_has_active_run", lambda *_: False)
    close = AsyncMock()
    monkeypatch.setattr(conversation, "handle_conversation_create", close)
    monkeypatch.setattr("backend.workspace.recent_projects.atomic_write_text", Mock(side_effect=PermissionError("controlled failure")))
    await handle_workspace_recent_remove(session, {"path": str(root), "preserve_history": True})
    close.assert_not_called()
    assert session.active_conversation_id == record.id
    assert session.emit_command_result.call_args.kwargs["level"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["env.list", "scheduler.list"])
async def test_settings_queries_publish_a_success_receipt_after_their_snapshot(tmp_path, monkeypatch, command):
    from backend.ws.command_scope import CommandScope
    from backend.ws.handlers import mcp

    observed = []

    async def send_payload(payload, **kwargs):
        observed.append(("snapshot", payload))

    async def send_event(event):
        observed.append(("receipt", event.data))

    session = SimpleNamespace(send_payload=send_payload, send_event=send_event)
    monkeypatch.setattr("backend.services.env_vault_service.list_env_entries", lambda: SimpleNamespace(entries=[]))
    monkeypatch.setattr(mcp, "_get_scheduler", lambda _: object())
    scope = CommandScope("owner-a", str(tmp_path), "query-1")
    monkeypatch.setattr(mcp, "_scheduler_command_scope", lambda *_: scope)
    monkeypatch.setattr("backend.services.scheduler_service.list_scheduled_tasks", lambda *args, **kwargs: SimpleNamespace(tasks=[], runs=[]))
    handler = mcp.handle_env_list if command == "env.list" else mcp.handle_scheduler_list
    await handler(session, {})
    assert [kind for kind, _ in observed] == ["snapshot", "receipt"]
    assert observed[1][1]["command"] == command
    assert observed[1][1]["level"] == "success"
    if command == "scheduler.list":
        assert observed[1][1]["data"]["conversation_id"] == "owner-a"
        assert observed[1][1]["data"]["workspace_root"] == str(tmp_path)


@pytest.mark.parametrize("raw_path", ["   ", '""', "''", '"  "'])
def test_empty_project_path_is_rejected_without_activating_the_process_directory(monkeypatch, raw_path):
    from backend.workspace.api import create_workspace_router

    publish = Mock()
    monkeypatch.setattr("backend.workspace.state.set_active_workspace_root", publish)
    app = FastAPI()
    app.include_router(create_workspace_router())
    with TestClient(app) as client:
        validation = client.post("/api/workspace/validate", json={"path": raw_path})
        imported = client.post("/api/workspace/import", json={"path": raw_path})
    assert validation.status_code == 200
    assert validation.json() == {"valid": False, "error": "Project path is required"}
    assert imported.status_code == 422
    assert imported.json()["detail"] == "Project path is required"
    publish.assert_not_called()
