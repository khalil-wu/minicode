from __future__ import annotations

import json

import pytest

from backend.conversations.repository import ConversationRepository, ConversationWriteConflict
from backend.conversations.models import ConversationRecord, ConversationSummary


def test_metadata_updates_reuse_checkpoint_and_keep_revision_conflicts(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"id": "user", "role": "user", "content": "x" * 100000}],
                                       context_snapshot={"history": [{"role": "user", "content": "x" * 100000}]})
    original = repo.get_conversation(created.id)
    transcript_path = repo.transcript_path(created.id)
    transcript_mtime = transcript_path.stat().st_mtime_ns
    writes = []
    write = repo._safe_write_text
    def observe(path, text, encoding="utf-8"):
        writes.append((path.name, len(text.encode(encoding))))
        write(path, text, encoding)
    monkeypatch.setattr(repo, "_safe_write_text", observe)
    renamed = repo.rename_conversation(created.id, "new title")
    archived = repo.set_archived(created.id, True)
    assert renamed.revision == created.revision + 1
    assert archived.revision == renamed.revision + 1
    assert sum(size for _, size in writes) < 20000
    assert all(".transcript." not in name and ".snapshot." not in name for name, _ in writes)
    assert transcript_path.stat().st_mtime_ns == transcript_mtime
    restored = ConversationRepository(tmp_path).get_conversation(created.id)
    assert restored.title == "new title"
    assert restored.archived
    assert restored.transcript == original.transcript
    assert restored.context_snapshot == original.context_snapshot
    with pytest.raises(ConversationWriteConflict):
        repo.save_conversation(original)
    committed = repo.commit_turn_projection(created.id, assistant_message={"id": "answer", "role": "assistant", "content": "done"},
                                             context_snapshot={"history": []}, expected_revision=restored.revision)
    assert committed.revision == archived.revision + 1
    assert committed.title == "new title"


def test_metadata_revision_survives_checkpoint_recovery_and_delete(tmp_path):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "first"}])
    repo.rename_conversation(created.id, "retained title")
    repo.set_archived(created.id, True)
    before = repo.get_conversation(created.id)
    repo.save_context_snapshot(created.id, {"history": [{"role": "user", "content": "new"}]})
    manifest = json.loads((tmp_path / f"{created.id}.manifest.json").read_text(encoding="utf-8"))
    (tmp_path / f"{created.id}.g{manifest['current_generation']}.snapshot.json").write_text("{broken", encoding="utf-8")
    recovered = ConversationRepository(tmp_path).get_conversation(created.id)
    assert recovered.revision == before.revision
    assert recovered.title == "retained title"
    updated = repo.rename_conversation(created.id, "recovered title")
    assert updated.revision > before.revision
    assert ConversationRepository(tmp_path).get_conversation(created.id).title == "recovered title"
    repo.delete_conversation(created.id)
    tombstone = json.loads((tmp_path / f"{created.id}.manifest.json").read_text(encoding="utf-8"))
    assert tombstone["deletion_generation"] > updated.revision


def test_summary_inventory_uses_manifest_and_observes_other_repository_edits(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(title="Before", transcript=[{"role": "user", "content": "history"}])
    repo.list_conversations()

    def history_stamp_forbidden(_identity):
        raise AssertionError("Summary reads must not inspect provider history checkpoints")

    monkeypatch.setattr(repo, "_record_disk_stamp", history_stamp_forbidden)
    assert repo.get_conversation_summary(created.id).title == "Before"
    assert repo.list_conversations()[0].title == "Before"
    other = ConversationRepository(tmp_path)
    other.rename_conversation(created.id, "After")
    other.set_archived(created.id, True)
    summary = repo.get_conversation_summary(created.id)
    assert summary.title == "After" and summary.archived
    assert repo.list_conversations()[0].archived
    other.delete_conversation(created.id)
    assert repo.get_conversation_summary(created.id) is None
    assert repo.list_conversations() == []


@pytest.mark.parametrize("version", [1, 2, 3, 4, 5])
def test_legacy_checkpoint_is_readable_and_metadata_write_upgrades_manifest(tmp_path, version):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "old release"}])
    path = tmp_path / f"{created.id}.manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version"] = version
    path.write_text(json.dumps(manifest), encoding="utf-8")
    upgraded = ConversationRepository(tmp_path)
    assert upgraded.get_conversation(created.id).transcript == created.transcript
    renamed = upgraded.rename_conversation(created.id, "new release")
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 8
    assert ConversationRepository(tmp_path).get_conversation(created.id).revision == renamed.revision
@pytest.mark.parametrize("inline_metadata", [True, False])
def test_archive_and_restore_use_metadata_when_cold_history_checkpoint_is_corrupt(tmp_path, monkeypatch, inline_metadata):
    repo = ConversationRepository(tmp_path)
    history = [{"role": "user", "content": "x" * 4096} for _ in range(64)]
    created = repo.create_conversation(transcript=[{"id": f"message-{index}", **item} for index, item in enumerate(history)], context_snapshot={"history": history})
    manifest_path = tmp_path / f"{created.id}.manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata_path, transcript_path, snapshot_path = repo._generation_paths(created.id, before["current_generation"])
    original_transcript, original_snapshot = transcript_path.read_bytes(), snapshot_path.read_bytes()
    transcript_path.write_text("{broken transcript", encoding="utf-8")
    snapshot_path.write_text("{broken checkpoint", encoding="utf-8")
    if not inline_metadata:
        before.pop("metadata")
        manifest_path.write_text(json.dumps(before), encoding="utf-8")
    cold = ConversationRepository(tmp_path)
    def history_forbidden(*args, **kwargs):
        raise AssertionError("archive metadata must not read or cache full history")
    with monkeypatch.context() as scoped:
        scoped.setattr(cold, "_read_generation", history_forbidden)
        scoped.setattr(cold, "_read_transcript_path", history_forbidden)
        scoped.setattr(cold, "_read_snapshot_path", history_forbidden)
        scoped.setattr(cold, "_cache_record", history_forbidden)
        archived = cold.set_archived(created.id, True)
        assert isinstance(archived, ConversationSummary)
        assert archived.archived and archived.archived_at and archived.message_count == 64
        restored = cold.set_archived(created.id, False)
        assert not restored.archived and not restored.archived_at
        assert restored.message_count == 64 and restored.revision == archived.revision + 1
        assert cold.get_conversation_summary(created.id).revision == restored.revision
    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert after["current_generation"] == before["current_generation"]
    assert after.get("previous_generation") == before.get("previous_generation")
    assert transcript_path.read_text(encoding="utf-8") == "{broken transcript"
    assert snapshot_path.read_text(encoding="utf-8") == "{broken checkpoint"
    assert metadata_path.exists() and created.id not in cold._record_cache
    transcript_path.write_bytes(original_transcript)
    snapshot_path.write_bytes(original_snapshot)
    recovered = cold.get_conversation(created.id)
    assert recovered.transcript == created.transcript and recovered.context_snapshot == created.context_snapshot
    assert recovered.revision == restored.revision and not recovered.archived


def test_warm_archive_invalidates_full_cache_and_keeps_detached_history_and_inventory(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "kept"}], context_snapshot={"history": [{"role": "user", "content": "kept"}]})
    detached = repo.get_conversation(created.id)
    repo.list_conversations()
    def history_forbidden(*args, **kwargs):
        raise AssertionError("warm archive must not load or deepcopy full history")
    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "_load_record_for_mutation", history_forbidden)
        scoped.setattr(repo, "_cache_record", history_forbidden)
        archived = repo.set_archived(created.id, True)
        assert created.id not in repo._record_cache and created.id not in repo._record_cache_stamps
        assert repo.list_conversations()[0].archived
    archived.title = "caller-owned change"
    assert repo.get_conversation_summary(created.id).title == created.title
    loaded = repo.get_conversation(created.id)
    assert loaded.archived and loaded.transcript == detached.transcript and loaded.context_snapshot == detached.context_snapshot
    assert not detached.archived


def test_archive_keeps_existing_partial_projection_and_later_journal_replay(tmp_path, monkeypatch):
    import asyncio
    from backend.agent.execution_journal import ExecutionJournal
    from backend.services.conversation_projection_service import replay_pending_conversation_projections
    repo = ConversationRepository(tmp_path / "conversations")
    created = repo.create_conversation(transcript=[{"id": "question", "role": "user", "content": "Work"}], context_snapshot={"history": [{"role": "user", "content": "Work"}]})
    partial = repo.commit_turn_projection(created.id, assistant_message={"id": "answer", "role": "assistant", "content": "First step", "terminal_status": "partial"},
        context_delta={"set": {"note": "first"}, "removed": []}, partial=True, expected_revision=created.revision)
    manifest_path = tmp_path / "conversations" / f"{created.id}.manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    projection_path = repo._partial_projection_path(created.id, before["projection_log"]["generation"])
    projection_bytes = projection_path.read_bytes()
    cold = ConversationRepository(tmp_path / "conversations")
    with monkeypatch.context() as scoped:
        scoped.setattr(cold, "_read_generation", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("archive read history")))
        archived = cold.set_archived(created.id, True)
        restored = cold.set_archived(created.id, False)
    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    for key in ("current_generation", "previous_generation", "projection_log", "projection_revision", "previous_projection_log"):
        assert after.get(key) == before.get(key)
    assert projection_path.read_bytes() == projection_bytes
    assert restored.message_count == partial.message_count == 2
    assert restored.content_revision == partial.content_revision and restored.revision == archived.revision + 1
    journal = ExecutionJournal("archive-replay", base_dir=tmp_path / "journals")
    journal.append_lifecycle("conversation_projection_pending", {"conversation_id": created.id, "expected_revision": restored.revision,
        "assistant_message": {"id": "answer", "role": "assistant", "content": "Next step", "terminal_status": "partial"},
        "context_delta": {"set": {"note": "next"}, "removed": []}, "partial": True})
    asyncio.run(replay_pending_conversation_projections(cold, journal, conversation_id=created.id))
    loaded = ConversationRepository(tmp_path / "conversations").get_conversation(created.id)
    assert not loaded.archived and loaded.context_snapshot["note"] == "next"
    assert loaded.transcript[-1]["content"] == "Next step" and len(loaded.transcript) == 2
    assert not journal.pending_conversation_projections()


def test_archive_legacy_without_manifest_keeps_full_checkpoint_upgrade(tmp_path):
    legacy = ConversationRecord(id="conv_legacyarchive", title="Legacy", transcript=[{"id": "old", "role": "user", "content": "Kept"}], context_snapshot={"history": [{"role": "user", "content": "Kept"}]})
    (tmp_path / f"{legacy.id}.json").write_text(json.dumps(legacy.to_dict()), encoding="utf-8")
    repo = ConversationRepository(tmp_path)
    summary = repo.set_archived(legacy.id, True)
    assert isinstance(summary, ConversationSummary) and summary.archived
    assert (tmp_path / f"{legacy.id}.manifest.json").exists()
    restored = ConversationRepository(tmp_path).get_conversation(legacy.id)
    assert restored.transcript == legacy.transcript and restored.context_snapshot == legacy.context_snapshot
