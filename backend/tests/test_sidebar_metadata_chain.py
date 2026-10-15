from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from backend.conversations.repository import ConversationRepository
from backend.conversations.public_projection import project_public_conversation
from backend.workspace.recent_projects import RecentProjectStore
from backend.ws.handlers.sidebar import handle_pin, handle_read_state, handle_project


def conversation(repo, root):
    return repo.create_conversation(conversation_id="conv-sidebar", title="Original task", workspace_root=str(root), execution_cwd=str(root),
        transcript=[{"id": "u", "role": "user", "content": "Original request"}], context_snapshot={"history": [], "original": "checkpoint"})


def test_sidebar_mutations_survive_cold_read_without_rewriting_transcript_context_or_activity_time(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    original = conversation(repo, tmp_path / "A")
    checkpoint_files = {path.name: path.read_bytes() for path in repo.storage_root.glob("*.g*") if ".meta." not in path.name}
    pinned = repo.set_conversation_pinned("conv-sidebar", True)
    unread = repo.set_conversation_read_state("conv-sidebar", True)
    moved = repo.set_conversation_project("conv-sidebar", str(tmp_path / "B"))
    assert not moved.pinned
    assert moved.pinned_at == ""
    assert moved.has_unread_turn
    assert moved.project_id == str(tmp_path / "B")
    assert moved.updated_at == original.updated_at
    assert moved.content_revision == original.content_revision
    assert moved.execution_cwd == original.execution_cwd
    assert moved.workspace_root == original.workspace_root
    assert moved.transcript == original.transcript
    assert moved.context_snapshot == original.context_snapshot
    assert checkpoint_files == {path.name: path.read_bytes() for path in repo.storage_root.glob("*.g*") if ".meta." not in path.name}
    cold = ConversationRepository(repo.storage_root).get_conversation("conv-sidebar")
    assert cold.project_id == moved.project_id
    assert cold.has_unread_turn
    assert not cold.pinned
    assert project_public_conversation(cold)["project_id"] == moved.project_id
    assert project_public_conversation(cold)["has_unread_turn"] is True
    summary = repo.list_conversations()[0].to_dict()
    assert summary["project_id"] == moved.project_id
    assert summary["has_unread_turn"] is True
    assert pinned.pinned_at
    assert unread.has_unread_turn


def test_repeated_read_state_is_idempotent_and_empty_project_is_explicit_removal(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    conversation(repo, tmp_path / "A")
    repo.set_conversation_read_state("conv-sidebar", True)
    before = repo.list_conversations_with_revision()[1]
    original = repo.get_conversation("conv-sidebar")
    repeated = repo.set_conversation_read_state("conv-sidebar", True)
    assert repeated.revision == original.revision
    assert repo.list_conversations_with_revision()[1] == before
    cleared = repo.set_conversation_project("conv-sidebar", "")
    assert cleared.project_id == ""
    assert cleared.execution_cwd == str(tmp_path / "A")
    assert ConversationRepository(repo.storage_root).get_conversation("conv-sidebar").project_id == ""


def test_metadata_mutations_do_not_lose_a_frozen_turn_or_other_sidebar_writer(tmp_path):
    path = tmp_path / "conversations"
    writer = ConversationRepository(path)
    original = conversation(writer, tmp_path / "A")
    other = ConversationRepository(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda action: action(), [lambda: writer.set_conversation_pinned("conv-sidebar", True), lambda: other.set_conversation_read_state("conv-sidebar", True)]))
    final = writer.commit_turn_projection("conv-sidebar", assistant_message={"id": "final", "role": "assistant", "content": "Actual final answer"},
        context_snapshot=original.context_snapshot, expected_revision=original.revision)
    assert final.pinned
    assert final.has_unread_turn
    assert final.transcript[-1]["content"] == "Actual final answer"
    assert ConversationRepository(path).get_conversation("conv-sidebar").pinned


class Session:
    def __init__(self, repo, session_id, active):
        self.conversation_repo = repo
        self.session_id = session_id
        self.active_conversation_id = active
        self.is_connected = True
        self.ws_manager = None
        self.events = []
        self.results = []

    async def send_conversation_list(self, **kwargs):
        self.events.append([record.to_dict() for record in self.conversation_repo.list_conversations()])
        return True

    async def emit_command_result(self, command, message, **kwargs):
        self.results.append({"command": command, "message": message, **kwargs})

    async def send_event(self, event):
        self.events.append(event)


@pytest.mark.asyncio
async def test_handlers_publish_canonical_metadata_to_peer_windows_without_activating_or_rebinding(tmp_path, monkeypatch):
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"
    root_a.mkdir()
    root_b.mkdir()
    projects = RecentProjectStore(tmp_path / "recent_projects.json")
    projects.add(str(root_a), "A")
    projects.add(str(root_b), "B")
    monkeypatch.setattr("backend.ws.handlers.sidebar.RecentProjectStore", lambda: projects)
    repo = ConversationRepository(tmp_path / "conversations")
    conversation(repo, root_a)
    session = Session(repo, "first", "other-reader")
    peer = Session(ConversationRepository(repo.storage_root), "peer", "another-reader")
    manager = SimpleNamespace(iter_sessions=lambda: [session, peer])
    session.ws_manager = peer.ws_manager = manager
    await handle_pin(session, {"conversation_id": "conv-sidebar", "pinned": True})
    await handle_read_state(session, {"conversation_id": "conv-sidebar", "has_unread_turn": True})
    await handle_project(session, {"conversation_id": "conv-sidebar", "project_id": str(root_b)})
    assert len(session.events) == len(peer.events) == 3
    assert peer.events[-1][0]["project_id"] == str(root_b.resolve())
    assert peer.events[-1][0]["has_unread_turn"] is True
    assert peer.events[-1][0]["pinned"] is False
    assert all(result["level"] == "success" for result in session.results)
    assert session.active_conversation_id == "other-reader"
    assert peer.active_conversation_id == "another-reader"
    assert repo.get_conversation("conv-sidebar").execution_cwd == str(root_a)


@pytest.mark.asyncio
async def test_unknown_project_and_missing_owner_do_not_mutate_an_existing_record(tmp_path, monkeypatch):
    projects = RecentProjectStore(tmp_path / "recent_projects.json")
    monkeypatch.setattr("backend.ws.handlers.sidebar.RecentProjectStore", lambda: projects)
    repo = ConversationRepository(tmp_path / "conversations")
    original = conversation(repo, tmp_path / "A")
    session = Session(repo, "first", "conv-sidebar")
    await handle_project(session, {"conversation_id": "conv-sidebar", "project_id": str(tmp_path / "unknown")})
    await handle_pin(session, {"conversation_id": "conv-missing", "pinned": True})
    assert repo.get_conversation("conv-sidebar").revision == original.revision
    assert repo.get_conversation("conv-sidebar").project_id is None
    assert not repo.get_conversation("conv-sidebar").pinned
    assert [event.data["level"] for event in session.events] == ["error", "error"]
