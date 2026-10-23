from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.conversations.sidebar_order import project_key
from backend.workspace.recent_projects import RecentProjectStore
from backend.ws.handler import WebSocketSession
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.handlers.sidebar import handle_project_reorder, handle_project_thread_reorder, handle_recent_reorder, handle_sort_mode


def seed(repository, workspace, names):
    return [repository.create_conversation(conversation_id=f"conv-sort-{name}", title=name,
        workspace_root=str(workspace), execution_cwd=str(workspace), transcript=[{"id": f"user-{name}", "role": "user", "content": f"Original {name}"}],
        context_snapshot={"original": name}).id for name in names]


def order(repository):
    return repository.get_sidebar_order(repository.list_conversations())["sidebar_order"]


def test_source_project_occurrence_reorder_keeps_hidden_slots_and_all_record_bytes(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Alpha"
    alpha, hidden, bravo, charlie = seed(repository, project, ["alpha", "hidden", "bravo", "charlie"])
    other = seed(repository, tmp_path / "Other", ["other"])[0]
    repository.set_conversation_pinned(other, True)
    repository.set_conversation_read_state(alpha, True)
    repository.set_sidebar_sort_mode("manual", {str(project): [alpha, hidden, bravo, charlie]})
    repository.set_archived(hidden, True)
    before = {item.id: item.to_dict() for item in [repository.get_conversation(identifier) for identifier in [alpha, hidden, bravo, charlie, other]]}
    generation_bytes = {path.name: path.read_bytes() for path in repository.storage_root.glob("*.g*")}
    pinned = repository.get_pinned_order(repository.list_conversations())
    recent = order(repository)["recent_thread_ids"]
    revision = repository.list_conversations_with_revision()[1]
    repository.reorder_sidebar_threads(str(project), charlie, alpha, [alpha, bravo, charlie])
    expected = [charlie, hidden, alpha, bravo]
    assert order(repository)["project_thread_orders"][project_key(str(project))] == expected
    assert order(repository)["recent_thread_ids"] == recent
    assert repository.get_pinned_order(repository.list_conversations()) == pinned
    assert repository.list_conversations_with_revision()[1] > revision
    assert {identifier: repository.get_conversation(identifier).to_dict() for identifier in before} == before
    assert {path.name: path.read_bytes() for path in repository.storage_root.glob("*.g*")} == generation_bytes
    cold = ConversationRepository(repository.storage_root)
    cold.set_archived(hidden, False)
    assert order(cold)["project_thread_orders"][project_key(str(project))] == expected
    assert cold.get_conversation(alpha).has_unread_turn


def test_source_common_sort_menu_restores_manual_order_and_does_not_change_project_or_pinned_order(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Project"
    alpha, bravo, charlie = seed(repository, project, ["alpha", "bravo", "charlie"])
    assert order(repository)["sort_mode"] == "updated_at"
    repository.set_sidebar_sort_mode("manual", {str(project): [charlie, bravo, alpha]})
    repository.reorder_sidebar_threads(str(project), alpha, charlie, [charlie, bravo, alpha])
    repository.reorder_sidebar_projects(str(project), None, [str(project)], [str(project)])
    manual = order(repository)
    repository.set_sidebar_sort_mode("updated_at", {})
    repository.set_sidebar_sort_mode("manual", {str(project): [bravo, charlie, alpha]})
    assert order(repository)["project_thread_orders"] == manual["project_thread_orders"]
    assert order(repository)["project_order"] == manual["project_order"]
    assert repository.get_pinned_order(repository.list_conversations())["pinned_sort_mode"] == "manual"


def test_source_recent_occurrence_order_is_independent_from_project_occurrence(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Project"
    alpha, bravo, charlie = seed(repository, project, ["alpha", "bravo", "charlie"])
    repository.set_sidebar_sort_mode("manual", {str(project): [alpha, bravo, charlie]})
    recent = order(repository)["recent_thread_ids"]
    repository.reorder_sidebar_threads(None, recent[-1], recent[0], recent)
    assert order(repository)["recent_thread_ids"][0] == recent[-1]
    assert order(repository)["project_thread_orders"][project_key(str(project))] == [alpha, bravo, charlie]


def test_source_project_order_always_reorders_and_preserves_hidden_slots_without_reopening_mru(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    projects = [tmp_path / name for name in ["Alpha", "Hidden", "Bravo", "Charlie"]]
    store = RecentProjectStore(tmp_path / "recent_projects.json")
    for project in projects:
        project.mkdir()
        store.add(str(project), project.name)
    alpha, hidden, bravo, charlie = map(str, projects)
    repository.reorder_sidebar_projects(charlie, alpha, [alpha, hidden, bravo, charlie], [alpha, hidden, bravo, charlie])
    repository.reorder_sidebar_projects(charlie, None, [charlie, alpha, hidden, bravo], [alpha, hidden, bravo, charlie])
    store.remove(hidden)
    original_mru = store._store_path.read_bytes()
    repository.reorder_sidebar_projects(charlie, alpha, [alpha, bravo, charlie], [item.path for item in store.list()])
    assert order(repository)["project_order"] == list(map(project_key, [charlie, hidden, alpha, bravo]))
    assert order(repository)["sort_mode"] == "updated_at"
    assert store._store_path.read_bytes() == original_mru
    assert order(ConversationRepository(repository.storage_root))["project_order"] == order(repository)["project_order"]


def test_source_new_chat_and_project_move_prepend_only_their_actual_manual_occurrences(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project, other = tmp_path / "Project", tmp_path / "Other"
    alpha, bravo = seed(repository, project, ["alpha", "bravo"])
    old_other = seed(repository, other, ["other"])[0]
    repository.set_sidebar_sort_mode("manual", {str(project): [alpha, bravo], str(other): [old_other]})
    new = seed(repository, project, ["new"])[0]
    assert order(repository)["project_thread_orders"][project_key(str(project))] == [new, alpha, bravo]
    assert order(repository)["recent_thread_ids"][0] == new
    recent = order(repository)["recent_thread_ids"]
    before = repository.get_conversation(alpha)
    repository.set_conversation_project(alpha, str(other))
    after = repository.get_conversation(alpha)
    assert order(repository)["project_thread_orders"][project_key(str(other))] == [alpha, old_other]
    assert order(repository)["recent_thread_ids"] == recent
    assert (after.workspace_root, after.execution_cwd, after.updated_at) == (before.workspace_root, before.execution_cwd, before.updated_at)


@pytest.mark.parametrize("scenario", ["recent-mode", "other-project", "archived", "pinned", "duplicate", "missing-target"])
def test_source_rejects_unavailable_occurrence_without_mutating_saved_state(tmp_path, scenario):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Project"
    alpha, bravo = seed(repository, project, ["alpha", "bravo"])
    extra = seed(repository, tmp_path / "Other", ["other"])[0]
    if scenario != "recent-mode":
        repository.set_sidebar_sort_mode("manual", {str(project): [alpha, bravo]})
    if scenario == "archived":
        repository.set_archived(bravo, True)
    if scenario == "pinned":
        repository.set_conversation_pinned(bravo, True)
    before, revision = order(repository), repository.list_conversations_with_revision()[1]
    visible = [alpha, extra] if scenario == "other-project" else [alpha, alpha] if scenario == "duplicate" else [alpha, bravo]
    with pytest.raises(ValueError):
        repository.reorder_sidebar_threads(str(project), alpha, "missing" if scenario == "missing-target" else None, visible)
    assert order(repository) == before
    assert repository.list_conversations_with_revision()[1] == revision


def test_sort_write_and_read_metadata_writer_keep_both_changes(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Project"
    alpha, bravo = seed(repository, project, ["alpha", "bravo"])
    repository.set_sidebar_sort_mode("manual", {str(project): [alpha, bravo]})
    other = ConversationRepository(repository.storage_root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        operations = [pool.submit(repository.reorder_sidebar_threads, str(project), bravo, alpha, [alpha, bravo]),
                      pool.submit(other.set_conversation_read_state, alpha, True)]
        for operation in operations:
            operation.result(timeout=5)
    assert order(repository)["project_thread_orders"][project_key(str(project))] == [bravo, alpha]
    assert repository.get_conversation(alpha).has_unread_turn


class Session:
    def __init__(self, repository, name):
        self.conversation_repo, self.session_id = repository, name
        self.active_conversation_id, self.is_connected, self.ws_manager = "original-reader", True, None
        self.events, self.results = [], []

    async def send_conversation_list(self):
        self.events.append(self.conversation_repo.get_sidebar_order(self.conversation_repo.list_conversations()))
        return True

    async def emit_command_result(self, command, message, **fields):
        self.results.append({"command": command, **fields})

    async def send_event(self, event):
        self.events.append(event.to_ws_message())


@pytest.mark.asyncio
async def test_real_sidebar_handlers_broadcast_authoritative_scopes_without_changing_any_reader(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    project, other = tmp_path / "Project", tmp_path / "Other"
    project.mkdir(); other.mkdir()
    alpha, bravo = seed(repository, project, ["alpha", "bravo"])
    store = RecentProjectStore(tmp_path / "recent_projects.json")
    store.add(str(project), "Project"); store.add(str(other), "Other")
    monkeypatch.setattr("backend.ws.handlers.sidebar.RecentProjectStore", lambda: store)
    origin, peer = Session(repository, "sort-origin"), Session(repository, "sort-peer")
    manager = SimpleNamespace(iter_sessions=lambda: [origin, peer])
    origin.ws_manager = peer.ws_manager = manager
    await handle_sort_mode(origin, {"mode": "manual", "project_thread_seeds": {str(project): [alpha, bravo]}})
    await handle_project_thread_reorder(origin, {"project_id": str(project), "moved_conversation_id": bravo, "before_conversation_id": alpha, "visible_conversation_ids": [alpha, bravo]})
    await handle_project_reorder(origin, {"moved_project_id": str(other), "before_project_id": str(project), "visible_project_ids": [str(project), str(other)]})
    recent = order(repository)["recent_thread_ids"]
    await handle_recent_reorder(origin, {"moved_conversation_id": recent[-1], "before_conversation_id": recent[0], "visible_conversation_ids": recent})
    assert [item["command"] for item in origin.results] == ["sidebar.sort.set", "sidebar.project.thread.reorder", "sidebar.project.reorder", "sidebar.recent.reorder"]
    assert all(item["level"] == "success" for item in origin.results)
    assert origin.events[-1] == peer.events[-1] == {"sidebar_order": order(repository)}
    assert origin.active_conversation_id == peer.active_conversation_id == "original-reader"


def test_actual_inventory_producer_restores_saved_project_order_and_common_mode(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Project"
    alpha, bravo = seed(repository, project, ["alpha", "bravo"])
    repository.set_sidebar_sort_mode("manual", {str(project): [alpha, bravo]})
    repository.reorder_sidebar_threads(str(project), bravo, alpha, [alpha, bravo])
    session = object.__new__(WebSocketSession)
    session.conversation_runtime = SimpleNamespace(active_conversation_id=None, active_conversation=None)
    session.session_lifecycle = SessionLifecycle(session)
    session.ws_manager = None
    session.runtime_snapshot = lambda: {"session_id": "real-sidebar-inventory"}
    session.send_payload = AsyncMock(return_value=True)
    session.conversation_repo = ConversationRepository(repository.storage_root)
    assert asyncio.run(session.send_conversation_list()) is True
    payload = session.send_payload.await_args.args[0]
    assert payload["sidebar_order"] == order(repository)
    assert payload["sidebar_order"]["project_thread_orders"][project_key(str(project))] == [bravo, alpha]
    assert payload["inventory_revision"] == repository.list_conversations_with_revision()[1]


def test_new_workspace_binding_uses_the_manual_project_occurrence_without_retiming_other_threads(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    project = tmp_path / "Project"
    alpha = seed(repository, project, ["alpha"])[0]
    unbound = repository.create_conversation(conversation_id="conv-sort-unbound")
    repository.set_sidebar_sort_mode("manual", {str(project): [alpha]})
    before = repository.get_conversation(alpha).to_dict()
    repository.update_workspace_binding(unbound.id, workspace_root=str(project))
    assert order(repository)["project_thread_orders"][project_key(str(project))] == [unbound.id, alpha]
    assert repository.get_conversation(alpha).to_dict() == before
