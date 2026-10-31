import asyncio
from datetime import UTC, datetime
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.agent.swarm_store import FileSwarmStore
from backend.conversations.repository import ConversationRepository
from backend.services import storage_settings_service as storage
from backend.services.file_disk_usage import file_disk_bytes
from backend.services.general_maintenance import compress_cold_history
from backend.ws.handlers import conversation as conversation_handlers

NOW = datetime(2026, 10, 19, tzinfo=UTC)
OLD = datetime(2025, 1, 1, tzinfo=UTC)
OLD_MS = int(OLD.timestamp() * 1000)


def old_chat(repo, name, *, archived=True, source="user"):
    chat = repo.create_conversation(title=name, thread_source=source)
    def age(record):
        record.updated_at = OLD.isoformat()
        record.archived = archived
    repo._mutate_meta(chat.id, age, touch_updated_at=False)
    return chat


def add_tree(store, conversation_id):
    store.upsert_agent_run({"run_id": "root", "conversation_id": conversation_id, "role": "main",
        "status": "completed", "started_at": OLD_MS, "completed_at": OLD_MS})
    for child, parent in [("child", "root"), ("grandchild", "child")]:
        store.upsert_subagent({"subagent_id": child, "parent_run_id": parent, "status": "completed",
            "started_at": OLD_MS, "completed_at": OLD_MS, "input_prompt": "Private delegation instructions"})


def test_review_loads_the_complete_durable_tree_and_filters_age_archive_source(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    archived = old_chat(repo, "Archived tree")
    active = old_chat(repo, "Unarchived", archived=False)
    old_chat(repo, "Unknown source", source="unknown")
    repo.create_conversation(title="Recently used")
    add_tree(store, archived.id)
    cold_store = FileSwarmStore(store.root)
    reviewed = storage.review_storage(repo, days=90, archived_only=True, store=cold_store, now=NOW)
    assert [chat["id"] for chat in reviewed["chats"]] == [archived.id]
    assert reviewed["chats"][0]["subagent_count"] == 2
    assert "input_prompt" not in str(reviewed)
    assert {chat["id"] for chat in storage.review_storage(repo, days=90, archived_only=False, store=cold_store, now=NOW)["chats"]} == {archived.id, active.id}
    recent_child = cold_store.get_subagent("grandchild")
    recent_child["last_progress_at"] = int(NOW.timestamp() * 1000)
    cold_store.upsert_subagent(recent_child)
    assert storage.review_storage(repo, days=90, archived_only=True, store=cold_store, now=NOW)["chats"] == []
    assert repo.get_conversation(archived.id) is not None


def test_review_snapshot_detects_a_new_descendant_and_root_edits(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    chat = old_chat(repo, "Review before delete")
    add_tree(store, chat.id)
    snapshot = storage.review_storage(repo, days=30, archived_only=True, store=store, now=NOW)["chats"][0]["snapshot"]
    assert not storage.storage_review_changed(repo, chat.id, snapshot, store=store)
    store.upsert_subagent({"subagent_id": "new-child", "parent_run_id": "child", "status": "completed",
        "started_at": OLD_MS, "completed_at": OLD_MS})
    assert storage.storage_review_changed(repo, chat.id, snapshot, store=store)
    reviewed = storage.review_storage(repo, days=30, archived_only=True, store=store, now=NOW)["chats"][0]
    repo.rename_conversation(chat.id, "Changed after review")
    assert storage.storage_review_changed(repo, chat.id, reviewed["snapshot"], store=store)


def test_changed_review_is_skipped_before_stopping_or_deleting_user_work(monkeypatch, tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    chat = old_chat(repo, "Keep the changed chat")
    snapshot = storage.review_storage(repo, days=90, archived_only=True, store=store, now=NOW)["chats"][0]["snapshot"]
    repo.rename_conversation(chat.id, "Keep this latest title")
    monkeypatch.setattr(storage, "storage_records", lambda: store)
    stop = AsyncMock()
    monkeypatch.setattr(conversation_handlers, "_stop_conversation_run", stop)
    session = SimpleNamespace(conversation_repo=repo, emit_command_result=AsyncMock())
    from backend.services.conversation_payload_service import parse_conversation_delete_request
    request = parse_conversation_delete_request({"conversation_id": chat.id, "storage_review": snapshot})
    asyncio.run(conversation_handlers._handle_conversation_delete_fenced(session, request))
    stop.assert_not_awaited()
    assert session.emit_command_result.await_args.kwargs["data"]["reason"] == "storage_review_changed"
    assert repo.get_conversation(chat.id).title == "Keep this latest title"


def test_usage_counts_actual_bytes_without_removing_files(tmp_path):
    (tmp_path / "conversations").mkdir()
    (tmp_path / "conversations" / "chat.json").write_bytes(b"12345")
    (tmp_path / "settings.json").write_bytes(b"12")
    usage = storage.disk_usage(tmp_path)
    assert usage["total_bytes"] == 7
    assert {item["name"]: item["bytes"] for item in usage["categories"]} == {
        "chats": 0, "archived": 0, "subagents": 0, "unclassified": 5, "worktrees": 0, "other": 2}
    assert (tmp_path / "conversations" / "chat.json").read_bytes() == b"12345"


def test_review_counts_owned_files_and_every_descendant_without_charging_shared_database(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    chat = old_chat(repo, "Physical tree bytes")
    unrelated = old_chat(repo, "Other tree")
    store.upsert_agent_run({"run_id": "primary", "conversation_id": chat.id, "role": "primary",
        "status": "completed", "started_at": OLD_MS, "completed_at": OLD_MS})
    task = store.create_task({"conversation_id": chat.id, "subject": "Task-owned child", "description": "A real owned task"})
    for identity, parent, task_id in [("child", "primary", ""), ("grandchild", "child", ""), ("task-child", "", task["task_id"])]:
        store.upsert_subagent({"subagent_id": identity, "parent_run_id": parent, "task_id": task_id,
            "status": "completed", "started_at": OLD_MS, "completed_at": OLD_MS,
            "input_prompt": "Private payload stays in SQLite, never projected as bytes"})
    journal_bytes = 0
    child_bytes = 0
    for identity, repeats in [("primary", 2), ("child", 3), ("grandchild", 5), ("task-child", 7), ("unrelated", 100)]:
        directory = tmp_path / "sidechains" / identity
        directory.mkdir(parents=True)
        data = b'{"event_type":"assistant","payload":{"text":"real retained evidence"}}\n' * repeats
        (directory / "events.jsonl").write_bytes(data)
        if identity != "unrelated": journal_bytes += len(data)
        if identity in {"child", "grandchild", "task-child"}: child_bytes += len(data)
    parent_bytes = sum(file_disk_bytes(path) for path in repo.storage_root.glob(f"{chat.id}.*") if path.is_file())
    result = storage.review_storage(repo, days=90, archived_only=True, store=store, now=NOW)
    parent = next(item for item in result["chats"] if item["id"] == chat.id)
    assert parent["bytes"] == parent_bytes + journal_bytes
    assert parent["subagent_count"] == 3
    assert "input_prompt" not in str(result)
    assert {item["id"] for item in result["inventory"]} == {chat.id, unrelated.id}
    usage = storage.disk_usage(tmp_path, repository=repo, records=store)
    categories = {item["name"]: item["bytes"] for item in usage["categories"]}
    assert categories["subagents"] == child_bytes
    assert categories["other"] >= file_disk_bytes(store.path)
    assert (tmp_path / "sidechains" / "grandchild" / "events.jsonl").exists()


def test_primary_runs_and_incomplete_descendant_activity_are_not_cleanup_candidates(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    active = old_chat(repo, "Still executing primary")
    store.upsert_agent_run({"run_id": "primary-running", "conversation_id": active.id, "role": "primary",
        "status": "running", "started_at": OLD_MS})
    incomplete = old_chat(repo, "Unknown descendant age")
    store.upsert_agent_run({"run_id": "primary-old", "conversation_id": incomplete.id, "role": "primary",
        "status": "completed", "started_at": OLD_MS, "completed_at": OLD_MS})
    store.upsert_subagent({"subagent_id": "age-unknown", "parent_run_id": "primary-old", "status": "completed",
        "started_at": 0, "completed_at": 0})
    assert storage.review_storage(repo, days=90, archived_only=True, store=store, now=NOW)["inventory"] == []


def test_review_size_tracks_actual_compressed_history_without_losing_restored_content(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    chat = old_chat(repo, "Compressible retained history")
    content = "Retained original user evidence " * 20000
    repo.append_transcript_message(chat.id, {"id": "original-user", "role": "user", "content": content})
    repo._mutate_meta(chat.id, lambda record: setattr(record, "updated_at", OLD.isoformat()), touch_updated_at=False)
    before = storage.review_storage(repo, days=90, archived_only=True, store=store, now=NOW)["chats"][0]["bytes"]
    paths = list(repo.history_paths(chat.id))
    for path in paths:
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    assert compress_cold_history(repo, paths, now=NOW) > 0
    after = storage.review_storage(repo, days=90, archived_only=True, store=store, now=NOW)["chats"][0]["bytes"]
    assert after < before
    assert ConversationRepository(repo.storage_root).get_conversation(chat.id).transcript[0]["content"] == content


def test_review_snapshot_detects_a_durable_child_edit_even_when_activity_timestamps_stay_equal(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    store = FileSwarmStore(tmp_path / "swarm")
    chat = old_chat(repo, "Do not remove changed child evidence")
    add_tree(store, chat.id)
    snapshot = storage.review_storage(repo, days=90, archived_only=True, store=store, now=NOW)["chats"][0]["snapshot"]
    child = store.get_subagent("grandchild")
    child["objective"] = "Updated durable child objective"
    store.upsert_subagent(child)
    assert storage.storage_review_changed(repo, chat.id, snapshot, store=store)
