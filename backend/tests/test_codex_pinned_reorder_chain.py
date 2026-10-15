from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.sidebar import handle_pin_reorder
from backend.ws.payload_contracts import validate_session_projection_payload


def seed(repository, workspace, names=("alpha", "hidden", "bravo", "charlie")):
    identifiers = []
    for name in names:
        record = repository.create_conversation(conversation_id=f"conv-pin-{name}", title=name, workspace_root=str(workspace),
            execution_cwd=str(workspace), transcript=[{"id": f"user-{name}", "role": "user", "content": f"Original {name}"}],
            context_snapshot={"original": name})
        identifiers.append(record.id)
        repository.set_conversation_pinned(record.id, True)
    return identifiers


def order(repository):
    return repository.get_pinned_order(repository.list_conversations())["pinned_conversation_ids"]


def test_source_reorder_preserves_hidden_slots_cold_metadata_and_all_original_record_bytes(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    alpha, hidden, bravo, charlie = seed(repository, tmp_path)
    repository.set_archived(hidden, True)
    repository.set_conversation_read_state(alpha, True)
    before = {identifier: repository.get_conversation(identifier).to_dict() for identifier in (alpha, hidden, bravo, charlie)}
    files = {path.name: path.read_bytes() for path in repository.storage_root.glob("*.g*")}
    revision = repository.list_conversations_with_revision()[1]
    assert repository.reorder_pinned_conversations(charlie, alpha, [alpha, bravo, charlie]) == [charlie, hidden, alpha, bravo]
    assert repository.list_conversations_with_revision()[1] > revision
    assert {identifier: repository.get_conversation(identifier).to_dict() for identifier in before} == before
    assert {path.name: path.read_bytes() for path in repository.storage_root.glob("*.g*")} == files
    cold = ConversationRepository(repository.storage_root)
    assert order(cold) == [charlie, hidden, alpha, bravo]
    assert cold.get_conversation(alpha).has_unread_turn
    assert cold.get_pinned_order(cold.list_conversations())["pinned_sort_mode"] == "manual"
    cold.set_archived(hidden, False)
    assert order(cold) == [charlie, hidden, alpha, bravo]


def test_source_repin_appends_and_project_move_clears_membership_without_retiming_other_pins(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    alpha, bravo, charlie = seed(repository, tmp_path, ("alpha", "bravo", "charlie"))
    repository.reorder_pinned_conversations(charlie, alpha, [alpha, bravo, charlie])
    charlie_time = repository.get_conversation(charlie).pinned_at
    repository.set_conversation_pinned(alpha, False)
    repository.set_conversation_pinned(alpha, True)
    assert order(repository) == [charlie, bravo, alpha]
    repository.set_conversation_project(charlie, "")
    assert order(repository) == [bravo, alpha]
    assert not repository.get_conversation(charlie).pinned
    assert repository.get_conversation(bravo).pinned_at
    assert charlie_time
    assert ConversationRepository(repository.storage_root).get_conversation(alpha).workspace_root == str(tmp_path)


def test_source_visible_order_becomes_manual_and_preserves_a_new_pin_not_in_the_drag_snapshot(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    alpha, bravo, charlie = seed(repository, tmp_path, ("alpha", "bravo", "charlie"))
    extra = seed(repository, tmp_path, ("extra",))[0]
    assert repository.reorder_pinned_conversations(alpha, None, [charlie, bravo, alpha]) == [charlie, bravo, alpha, extra]
    assert set(order(repository)) == {alpha, bravo, charlie, extra}


@pytest.mark.parametrize("moved,before,visible", [
    ("conv-pin-missing", None, ["conv-pin-alpha"]),
    ("conv-pin-alpha", "conv-pin-missing", ["conv-pin-alpha"]),
    ("conv-pin-alpha", None, ["conv-pin-alpha", "conv-pin-alpha"]),
])
def test_changed_or_invalid_pinned_membership_reports_failure_and_keeps_saved_order(tmp_path, moved, before, visible):
    repository = ConversationRepository(tmp_path / "conversations")
    seed(repository, tmp_path, ("alpha", "bravo"))
    original, revision = order(repository), repository.list_conversations_with_revision()[1]
    with pytest.raises(ValueError, match="pinned threads changed"):
        repository.reorder_pinned_conversations(moved, before, visible)
    assert order(repository) == original
    assert repository.list_conversations_with_revision()[1] == revision


def test_reorder_and_read_metadata_writer_share_existing_store_without_losing_unread(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    alpha, bravo = seed(repository, tmp_path, ("alpha", "bravo"))
    writer = ConversationRepository(repository.storage_root)
    with ThreadPoolExecutor(max_workers=2) as pool:
        operations = [pool.submit(repository.reorder_pinned_conversations, bravo, alpha, [alpha, bravo]),
                      pool.submit(writer.set_conversation_read_state, alpha, True)]
        for operation in operations:
            operation.result(timeout=5)
    assert order(repository) == [bravo, alpha]
    assert repository.get_conversation(alpha).has_unread_turn


class Session:
    def __init__(self, repository, name):
        self.conversation_repo = repository
        self.session_id = name
        self.active_conversation_id = "unchanged-reader"
        self.is_connected = True
        self.ws_manager = None
        self.events, self.results = [], []

    async def send_conversation_list(self):
        self.events.append(self.conversation_repo.get_pinned_order(self.conversation_repo.list_conversations()))
        return True

    async def emit_command_result(self, command, message, **fields):
        self.results.append({"command": command, **fields})

    async def send_event(self, event):
        self.events.append(event.to_ws_message())


@pytest.mark.asyncio
async def test_real_reorder_handler_broadcasts_canonical_order_to_peer_without_switching_reader(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    alpha, bravo = seed(repository, tmp_path, ("alpha", "bravo"))
    owner, peer = Session(repository, "owner"), Session(ConversationRepository(repository.storage_root), "peer")
    manager = SimpleNamespace(iter_sessions=lambda: [owner, peer])
    owner.ws_manager = peer.ws_manager = manager
    await handle_pin_reorder(owner, {"moved_conversation_id": bravo, "before_conversation_id": alpha, "visible_conversation_ids": [alpha, bravo]})
    assert owner.results[0]["level"] == "success"
    assert owner.events == peer.events == [{"pinned_conversation_ids": [bravo, alpha], "pinned_sort_mode": "manual"}]
    assert owner.active_conversation_id == peer.active_conversation_id == "unchanged-reader"


def test_real_inventory_producer_projects_pinned_order_and_revision_after_cold_read(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    alpha, bravo = seed(repository, tmp_path, ("alpha", "bravo"))
    repository.reorder_pinned_conversations(bravo, alpha, [alpha, bravo])
    session = object.__new__(WebSocketSession)
    session.conversation_runtime = SimpleNamespace(active_conversation_id=None, active_conversation=None)
    session.runtime_snapshot = lambda: {"session_id": "real-inventory"}
    session.send_payload = AsyncMock(return_value=True)
    session.conversation_repo = ConversationRepository(repository.storage_root)
    assert asyncio.run(session.send_conversation_list()) is True
    payload = session.send_payload.await_args.args[0]
    validate_session_projection_payload(payload)
    assert payload["pinned_conversation_ids"] == [bravo, alpha]
    assert payload["pinned_sort_mode"] == "manual"
    assert payload["inventory_revision"] == repository.list_conversations_with_revision()[1]
