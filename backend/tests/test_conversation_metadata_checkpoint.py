from __future__ import annotations

import json

import pytest

from backend.conversations.repository import ConversationRepository, ConversationWriteConflict


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
