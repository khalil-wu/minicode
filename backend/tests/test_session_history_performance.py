from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.conversations.repository import ConversationRepository
from backend.artifact.store import ArtifactStore
from backend.ws.handlers.session import handle_session_sync
from backend.ws.session_restore import SessionRestoreManager


def _history(count: int) -> list[dict]:
    return [{"id": f"message-{index}", "role": "user" if index % 2 == 0 else "assistant",
             "content": f"message {index}"} for index in range(count)]


def test_restore_reads_a_history_page_without_private_checkpoint(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(transcript=_history(220), context_snapshot={
        "history": [{"role": "user", "content": "private provider context"}],
    })
    cold = ConversationRepository(tmp_path)

    def full_history_is_not_a_page(*args, **kwargs):
        raise AssertionError("initial restore must not read provider history")

    monkeypatch.setattr(cold, "get_conversation", full_history_is_not_a_page)
    monkeypatch.setattr(cold, "_read_snapshot_path", full_history_is_not_a_page)
    restored = asyncio.run(SessionRestoreManager(cold).restore_session("session", record.id))
    assert restored["error"] is None
    assert restored["restored"]
    assert len(restored["messages"]) == 80
    assert restored["messages"][0]["id"] == "message-140"
    assert restored["conversation"]["transcript_page"]["total_messages"] == 220
    assert restored["conversation"]["transcript_page"]["has_more"]
    assert "private provider context" not in json.dumps(restored)


@pytest.mark.parametrize("switch_during_page,switch_during_metadata", [(False, False), (True, False), (False, True)])
def test_sync_keeps_the_page_cursor_and_does_not_read_full_history(tmp_path, monkeypatch, switch_during_page, switch_during_metadata):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(transcript=_history(220))
    next_record = repo.create_conversation(transcript=_history(220))
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    for owner in (record, next_record):
        artifact_id = store.save("AA==", source="tool_exec.image", type="image", media_type="image/png", conversation_id=owner.id)
        repo.upsert_transcript_message(owner.id, {**owner.transcript[-1], "artifacts": [{"artifactId": artifact_id, "kind": "image", "summary": "Old code image"}]})
    sent = []
    projection_lock = asyncio.Lock()

    class Session:
        session_id = "session"
        active_conversation_id = record.id
        conversation_repo = repo
        artifact_store = store
        selected_model = "model"
        provider = "custom"
        available_models = ["model"]
        models_source = "test"
        session_lifecycle = SimpleNamespace(current_workspace_root=lambda: None)
        event_outbox = SimpleNamespace(current_replay_seq=0, replay_log_degraded=False)

        def _conversation_projection_lock(self, _owner):
            return projection_lock

        @property
        def active_conversation(self):
            raise AssertionError("sync must not load a private record")

        def runtime_snapshot(self):
            return {"session_id": self.session_id, "active_conversation_id": self.active_conversation_id}

        async def send_payload(self, payload, **kwargs):
            sent.append(payload)

        async def reemit_pending_state(self):
            sent.append({"type": "pending-state"})

        def schedule_next_queued_user_message(self, conversation_id):
            assert conversation_id == self.active_conversation_id

    reads = []
    original_view = repo.get_conversation_view

    def read_page(owner):
        reads.append(owner)
        page = original_view(owner)
        if switch_during_page and len(reads) == 1:
            Session.active_conversation_id = next_record.id
        return page

    monkeypatch.setattr(repo, "get_conversation_view", read_page)
    get_meta = store.get_meta
    metadata_reads = []
    def read_metadata(artifact_id, **scope):
        metadata_reads.append(scope["conversation_id"])
        if switch_during_metadata and scope["conversation_id"] == record.id:
            Session.active_conversation_id = next_record.id
        return get_meta(artifact_id, **scope)
    monkeypatch.setattr(store, "get_meta", read_metadata)
    asyncio.run(handle_session_sync(Session(), {}))
    assert [payload["type"] for payload in sent] == ["session.synced", "pending-state"]
    page = sent[0]["active_conversation"]
    assert len(page["transcript"]) == 80
    assert page["transcript_page"] == {"before_message_id": "message-140", "has_more": True, "total_messages": 220}
    assert page["id"] == sent[0]["active_conversation_id"] == sent[0]["session"]["active_conversation_id"]
    assert page["transcript"][-1]["artifacts"][0]["source"] == "tool"
    assert reads == ([record.id, next_record.id] if switch_during_page or switch_during_metadata else [record.id])
    assert metadata_reads == reads
    assert "source" not in repo.get_conversation(page["id"]).transcript[-1]["artifacts"][0]
    store.shutdown()


def test_delete_uses_metadata_revision_even_if_history_is_unreadable(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(transcript=_history(4))
    record = repo.rename_conversation(record.id, "renamed")
    record = repo.set_archived(record.id, True)
    manifest_path = tmp_path / f"{record.id}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for suffix in ("snapshot.json", "transcript.jsonl"):
        (tmp_path / f"{record.id}.g{manifest['current_generation']}.{suffix}").write_text("{broken", encoding="utf-8")
    assert ConversationRepository(tmp_path).delete_conversation(record.id)
    tombstone = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert tombstone["deleted"]
    assert tombstone["deletion_generation"] > record.revision
    assert ConversationRepository(tmp_path).get_conversation(record.id) is None


def test_warm_inventory_uses_directory_stamps_and_observes_another_writer(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(title="before")
    repo.list_conversations()
    original_stat = Path.stat
    manifest_stats = []

    def observe(path, *args, **kwargs):
        if path.name.endswith(".manifest.json"):
            manifest_stats.append(path.name)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", observe)
    assert repo.list_conversations()[0].title == "before"
    assert manifest_stats == []
    ConversationRepository(tmp_path).rename_conversation(record.id, "another process")
    assert repo.list_conversations()[0].title == "another process"
