"""Persistent attached forks own deletion and stay outside ordinary discovery."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.conversations.import_export import import_conversation_tree
from backend.conversations.repository import (
    AttachedThreadsExistError, ConversationCreationUnavailableError,
    ConversationLifecycleBusyError, ConversationRepository, ConversationStorageCorruptError,
)
from backend.services.conversation_payload_service import parse_conversation_delete_request
from backend.tests.test_conversation_activation_contract import _session
from backend.ws.handlers import conversation as handlers


def ids(page):
    return [item["id"] for item in page["data"]]


def test_attached_forks_survive_restart_and_missing_index_with_pagination(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation(title="BLUE HERON")
    other = repo.create_conversation(title="Other")
    children = [repo.clone_conversation(parent.id, attach_to_source=True) for _ in range(5)]
    peer_child = repo.clone_conversation(other.id, attach_to_source=True)
    repo.directory.page()
    repo.directory.path.unlink()
    cold = ConversationRepository(tmp_path)
    assert ids(cold.directory.page({"limit": 2})) == [other.id, parent.id]
    assert ids(cold.directory.page({"limit": 2, "sort_direction": "asc"})) == [parent.id, other.id]
    assert ids(cold.directory.page({"limit": 1, "search_term": "blue heron", "use_state_db_only": True})) == [parent.id]
    assert ids(cold.directory.page({"parent_thread_id": parent.id})) == []
    assert ids(cold.directory.page({"ancestor_thread_id": parent.id})) == []
    observed, cursor = [], None
    while True:
        page = cold.directory.page({"attached_to_thread_id": parent.id, "limit": 2, "cursor": cursor})
        observed.extend(ids(page))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert observed == [child.id for child in reversed(children)]
    assert ids(cold.directory.page({"attached_to_thread_id": other.id})) == [peer_child.id]
    assert {row.id for row in cold.list_conversations()} == {parent.id, other.id}
    assert len(cold.list_conversations(include_attached=True)) == 8
    for child in children:
        assert cold.get_conversation(child.id).attached_to_thread_id == parent.id
        assert cold.get_conversation_view(child.id)["attached_to_thread_id"] == parent.id
    with pytest.raises(AttachedThreadsExistError):
        cold.delete_conversation(parent.id)


def test_ordinary_fork_detaches_and_nested_attached_deletion_orders_lifetimes(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation(transcript=[{"role": "user", "content": "Keep original"}])
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    independent = repo.clone_conversation(child.id)
    nested = repo.clone_conversation(child.id, attach_to_source=True)
    assert independent.attached_to_thread_id == ""
    assert ids(repo.directory.page({"attached_to_thread_id": child.id})) == [nested.id]
    repo.set_archived(child.id, True)
    assert ids(repo.directory.page({"attached_to_thread_id": parent.id})) == []
    assert ids(repo.directory.page({"attached_to_thread_id": parent.id, "archived": True})) == [child.id]
    for owner, dependent in ((parent.id, child.id), (child.id, nested.id)):
        with pytest.raises(AttachedThreadsExistError) as error:
            repo.delete_conversation(owner)
        assert error.value.child_id == dependent
    for owner in (nested.id, child.id, parent.id):
        assert repo.delete_conversation(owner)
    assert repo.get_conversation(independent.id).transcript[0]["content"] == "Keep original"
    assert ids(repo.directory.page()) == [independent.id]


def test_attached_inventory_hides_pin_section_known_active_and_project_rows_without_deleting(tmp_path):
    repo = ConversationRepository(tmp_path / "chats")
    parent = repo.create_conversation(workspace_root=str(tmp_path))
    section = repo.thread_sections.create("Owned section")
    repo.thread_sections.move_thread(parent.id, section["id"])
    repo.set_conversation_pinned(parent.id, True)
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    assert "thread:" + child.id not in repo.thread_sections.list()[0]["item_keys"]
    repo.set_conversation_pinned(child.id, True)
    # Exercise stale placement data as well as an explicit pin.
    repo.thread_sections.move("thread:" + child.id, section["id"])
    from backend.conversations.sidebar_order import project_key
    inventory = repo.directory.inventory(known_ids=[child.id], active_id=child.id,
        project_keys=[project_key(str(tmp_path))])
    assert child.id not in [row["id"] for row in inventory["conversations"]]
    assert child.id not in inventory["pinned_conversation_ids"]
    assert child.id not in inventory["removed_conversation_ids"]
    assert "thread:" + child.id not in inventory["sidebar_sections"][0]["item_keys"]
    assert "thread:" + child.id in repo.thread_sections.list()[0]["item_keys"]


@pytest.mark.parametrize("extra", [{"parent_thread_id": "conv_parent"}, {"ancestor_thread_id": "conv_parent"}])
def test_attached_listing_rejects_conflicting_relation_filters(tmp_path, extra):
    with pytest.raises(ValueError, match="cannot be combined"):
        ConversationRepository(tmp_path).directory.page({"attached_to_thread_id": "conv_parent", **extra})


@pytest.mark.parametrize("options", [{"ephemeral": True}, {"defer_goal_continuation": True}, {"attach_to_source": "yes"}])
@pytest.mark.asyncio
async def test_invalid_attach_commands_leave_no_child(tmp_path, options):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    with pytest.raises(ValueError):
        await handlers.handle_conversation_clone(_session(repo, parent.id),
            {"conversation_id": parent.id, "attach_to_source": True, **options})
    assert ids(repo.directory.page({"attached_to_thread_id": parent.id})) == []


def test_attachment_is_part_of_idempotent_creation_identity(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True, idempotency_key="attach-once")
    cold = ConversationRepository(tmp_path)
    assert cold.clone_conversation(parent.id, attach_to_source=True, idempotency_key="attach-once").id == child.id
    with pytest.raises(ConversationCreationUnavailableError):
        cold.clone_conversation(parent.id, idempotency_key="attach-once")
    ordinary = cold.clone_conversation(parent.id, idempotency_key="ordinary")
    assert cold.clone_conversation(parent.id, attach_to_source=False, idempotency_key="ordinary").id == ordinary.id
    with pytest.raises(ConversationCreationUnavailableError):
        cold.clone_conversation(parent.id, attach_to_source=True, idempotency_key="ordinary")


@pytest.mark.parametrize("failed_stage", ["generation", "index"])
def test_failed_creation_has_no_canonical_or_discoverable_attachment(tmp_path, monkeypatch, failed_stage):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    repo.directory.page()
    def fail(*args, **kwargs):
        raise OSError("injected attached creation failure")
    with monkeypatch.context() as fault:
        fault.setattr(repo if failed_stage == "generation" else repo.directory,
            "_write_generation" if failed_stage == "generation" else "committed", fail)
        with pytest.raises(OSError, match="injected attached"):
            repo.clone_conversation(parent.id, attach_to_source=True)
    cold = ConversationRepository(tmp_path)
    assert ids(cold.directory.page({"attached_to_thread_id": parent.id})) == []
    assert cold.delete_conversation(parent.id)


def test_canonical_guard_survives_absent_rows_and_damaged_child_history(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    repo.directory.page()
    with repo.directory.connect() as connection:
        connection.execute("DELETE FROM threads WHERE id=?", (child.id,))
    repo.transcript_path(child.id).write_text("corrupted history", encoding="utf-8")
    cold = ConversationRepository(tmp_path)
    with pytest.raises(AttachedThreadsExistError):
        cold.delete_conversation(parent.id)
    assert cold.delete_conversation(child.id)
    assert cold.delete_conversation(parent.id)


def test_unreadable_relation_metadata_does_not_allow_destructive_cleanup(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    (tmp_path / f"{child.id}.manifest.json").write_text("broken", encoding="utf-8")
    with pytest.raises(ConversationStorageCorruptError):
        ConversationRepository(tmp_path).delete_conversation(parent.id)
    assert repo.get_conversation(parent.id) is not None


def test_active_attached_projection_is_valid_outside_ordinary_inventory(tmp_path):
    from backend.ws.payload_contracts import validate_session_projection_payload
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    payload = {"type": "conversation.list", "conversations": [], "active_conversation_id": child.id,
        "active_conversation": repo.get_conversation_view(child.id)}
    validate_session_projection_payload(payload)
    for active in (None, {"id": child.id}, {"id": parent.id, "attached_to_thread_id": child.id}):
        with pytest.raises(ValueError):
            validate_session_projection_payload({**payload, "active_conversation": active})


def test_corrupt_sqlite_is_preserved_and_attachment_rebuilt_from_canonical_metadata(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    repo.directory.page()
    damaged = b"damaged derived sqlite directory"
    repo.directory.path.write_bytes(damaged)
    cold = ConversationRepository(tmp_path)
    assert ids(cold.directory.page()) == [parent.id]
    assert ids(cold.directory.page({"attached_to_thread_id": parent.id})) == [child.id]
    backups = list((tmp_path / "db-backups").glob("*/.conversation-directory.sqlite3"))
    assert len(backups) == 1 and backups[0].read_bytes() == damaged
    with pytest.raises(AttachedThreadsExistError):
        cold.delete_conversation(parent.id)


def test_noncorruption_sqlite_failure_is_reported_without_replacing_directory(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    repo.directory.page()
    before = repo.directory.path.read_bytes()
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    def blocked():
        raise error
    monkeypatch.setattr(repo.directory, "_open_connection", blocked)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        repo.directory.page()
    assert repo.directory.path.read_bytes() == before
    assert not (tmp_path / "db-backups").exists()
    assert repo.get_conversation(parent.id) is not None


def test_native_lifecycle_lock_excludes_other_repo_and_process_and_spans_worker(tmp_path):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    other = ConversationRepository(tmp_path)
    with repo.conversation_lifecycle(parent.id) as ownership:
        for operation in (lambda: other.clone_conversation(parent.id, attach_to_source=True),
                          lambda: other.delete_conversation(parent.id)):
            with pytest.raises(ConversationLifecycleBusyError):
                operation()
        script = """
import sys
from pathlib import Path
from backend.conversations.repository import ConversationRepository, ConversationLifecycleBusyError
repo = ConversationRepository(Path(sys.argv[1]))
for operation in (lambda: repo.clone_conversation(sys.argv[2], attach_to_source=True), lambda: repo.delete_conversation(sys.argv[2])):
    try:
        operation()
    except ConversationLifecycleBusyError:
        print('owned')
    else:
        raise AssertionError('Cross-process source ownership escaped')
"""
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", script, str(tmp_path), parent.id],
            capture_output=True, text=True, timeout=20, cwd=Path(__file__).resolve().parents[2])
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["owned", "owned"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(repo.delete_conversation, parent.id, lifecycle_lock=ownership).result(timeout=5)
    assert other.clone_conversation(parent.id, attach_to_source=True) is None


def test_inflight_creation_blocks_deletion_until_canonical_child_published(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    entered, release = threading.Event(), threading.Event()
    commit = repo._commit_record
    def held(record, **kwargs):
        entered.set()
        assert release.wait(5)
        return commit(record, **kwargs)
    monkeypatch.setattr(repo, "_commit_record", held)
    other = ConversationRepository(tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(repo.clone_conversation, parent.id, attach_to_source=True)
        try:
            assert entered.wait(5)
            with pytest.raises(ConversationLifecycleBusyError):
                other.delete_conversation(parent.id)
        finally:
            release.set()
        child = pending.result(timeout=5)
    with pytest.raises(AttachedThreadsExistError) as error:
        other.delete_conversation(parent.id)
    assert error.value.child_id == child.id


@pytest.mark.asyncio
async def test_source_delete_rejects_before_stopping_run_or_clearing_resources(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    stop, cleanup = AsyncMock(), AsyncMock()
    monkeypatch.setattr(handlers, "_stop_conversation_run", stop)
    monkeypatch.setattr(handlers, "_handle_conversation_delete_after_run_fence", cleanup)
    session = SimpleNamespace(conversation_repo=repo, emit_command_result=AsyncMock())
    request = parse_conversation_delete_request({"conversation_id": parent.id})
    await handlers._handle_conversation_delete_fenced(session, request)
    stop.assert_not_awaited()
    cleanup.assert_not_awaited()
    result = session.emit_command_result.await_args.kwargs["data"]
    assert result == {"conversation_id": parent.id, "deleted": False, "reason": "attached_threads_exist", "attached_thread_id": child.id}
    assert repo.get_conversation(parent.id) is not None


@pytest.mark.asyncio
async def test_async_delete_owns_source_across_stop_await_and_releases_on_cancellation(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    entered = asyncio.Event()
    async def hold(*args, **kwargs):
        entered.set()
        await asyncio.Future()
    monkeypatch.setattr(handlers, "_stop_conversation_run", hold)
    session = SimpleNamespace(conversation_repo=repo, emit_command_result=AsyncMock(), ws_manager=None)
    request = parse_conversation_delete_request({"conversation_id": parent.id})
    task = asyncio.create_task(handlers._handle_conversation_delete_fenced(session, request))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        with pytest.raises(ConversationLifecycleBusyError):
            await asyncio.to_thread(ConversationRepository(tmp_path).clone_conversation, parent.id, attach_to_source=True)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert repo.clone_conversation(parent.id, attach_to_source=True) is not None


@pytest.mark.asyncio
async def test_cancelled_clone_finishes_owned_resource_copy_without_blocking_event_loop(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    parent = repo.create_conversation()
    session = _session(repo, parent.id)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    def share(*args):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
    session.attachment_store.share_for_conversation.side_effect = share
    task = asyncio.create_task(handlers.handle_conversation_clone(session,
        {"conversation_id": parent.id, "attach_to_source": True}))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    child, = repo.directory.page({"attached_to_thread_id": parent.id})["data"]
    assert session.artifact_store.share_for_conversation.call_args.args[1] == child["id"]
    assert session.diagnostic_store.share_for_conversation.call_args.args[1] == child["id"]


def test_shared_resource_owners_and_export_import_rebind_attached_sources(tmp_path):
    repo = ConversationRepository(tmp_path / "original")
    parent = repo.create_conversation()
    repo.thread_resources.add(parent.id, "document", "shared", {"title": "Retained"})
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    assert {row["thread_id"] for row in repo.thread_resources.owners("document", "shared")["data"]} == {parent.id, child.id}
    exported = repo.export_conversation_tree(parent.id, include_descendants=True)
    exported["conversations"].reverse()
    imported_repo = ConversationRepository(tmp_path / "imported")
    result = import_conversation_tree(imported_repo, exported)
    imported_child, = imported_repo.directory.page({"attached_to_thread_id": result["conversation_id"]})["data"]
    assert imported_child["id"] != child.id
    with pytest.raises(AttachedThreadsExistError):
        imported_repo.delete_conversation(result["conversation_id"])


def test_imported_standalone_attached_copy_is_independent(tmp_path):
    repo = ConversationRepository(tmp_path / "source")
    parent = repo.create_conversation()
    child = repo.clone_conversation(parent.id, attach_to_source=True)
    other = ConversationRepository(tmp_path / "target")
    result = import_conversation_tree(other, repo.export_conversation_tree(child.id, include_descendants=False))
    assert other.get_conversation(result["conversation_id"]).attached_to_thread_id == ""


def test_failed_import_rolls_back_attached_descendants_before_their_new_parent(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "source")
    parent = repo.create_conversation()
    repo.clone_conversation(parent.id, attach_to_source=True)
    other = ConversationRepository(tmp_path / "target")
    save = other.save_conversation
    def failed_child(record):
        save(record)
        if record.attached_to_thread_id:
            raise OSError("failed import after child publication")
    monkeypatch.setattr(other, "save_conversation", failed_child)
    with pytest.raises(OSError, match="failed import"):
        import_conversation_tree(other, repo.export_conversation_tree(parent.id))
    assert other.list_conversations(include_attached=True) == []
    assert len(repo.list_conversations(include_attached=True)) == 2
