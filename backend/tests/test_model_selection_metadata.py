from __future__ import annotations

import json

import pytest

from backend.conversations.models import ConversationSummary
from backend.conversations.repository import ConversationRepository, ConversationWriteConflict


@pytest.mark.parametrize("warm", [False, True])
@pytest.mark.parametrize("inline_metadata", [False, True])
def test_model_selection_updates_only_metadata_and_retains_owned_history(tmp_path, monkeypatch, warm, inline_metadata):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(
        transcript=[{"id": "question", "role": "user", "content": "Keep the original task"}],
        context_snapshot={"history": [{"role": "user", "content": "Keep the original task"}]},
    )
    projected = repo.commit_turn_projection(created.id,
        assistant_message={"id": "answer", "role": "assistant", "content": "First result"},
        context_delta={"set": {"note": "retained"}, "removed": []}, partial=True,
        expected_revision=created.revision) if inline_metadata else created
    manifest_path = tmp_path / f"{created.id}.manifest.json"
    before = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not inline_metadata:
        before.pop("metadata")
        manifest_path.write_text(json.dumps(before), encoding="utf-8")
    repo = ConversationRepository(tmp_path)
    if warm:
        detached = repo.get_conversation(created.id)
        cached = repo._record_cache[created.id]
    repo.list_conversations()
    checkpoint_bytes = {path: path.read_bytes() for path in tmp_path.iterdir()
        if ".g" in path.name and path.is_file()}

    def history_forbidden(*args, **kwargs):
        raise AssertionError("Changing model settings must not load history")

    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "_load_record_for_mutation", history_forbidden)
        scoped.setattr(repo, "_read_generation", history_forbidden)
        selected = repo.update_model_selection(created.id, provider="custom", model="next", reasoning_effort="high")
        assert isinstance(selected, ConversationSummary)
        assert selected.revision == projected.revision + 1
        assert selected.content_revision == projected.content_revision
        assert selected.message_count == projected.message_count
        assert repo.get_conversation_summary(created.id).model_selection == selected.model_selection
        unchanged = repo.update_model_selection(created.id, provider="custom", model="next", reasoning_effort="high")
        stale = repo.update_model_selection(created.id, provider="old", model="old", only_if_unset=True)
        assert unchanged.revision == stale.revision == selected.revision
        assert stale.model_selection == selected.model_selection
        if warm:
            current = repo._record_cache[created.id]
            assert current.transcript is cached.transcript
            assert current.context_snapshot is cached.context_snapshot
            assert cached.model_selection == detached.model_selection == {}
        else:
            assert created.id not in repo._record_cache
    selected.model_selection["model"] = "caller-only"
    after = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ["current_generation", "previous_generation", "projection_log", "projection_revision"]:
        assert after.get(field) == before.get(field)
    assert all(path.read_bytes() == data for path, data in checkpoint_bytes.items())
    restored = ConversationRepository(tmp_path).get_conversation(created.id)
    assert restored.model_selection == {"provider": "custom", "model": "next", "reasoning_effort": "high"}
    assert restored.transcript == projected.transcript
    assert restored.context_snapshot == projected.context_snapshot
    assert repo.get_conversation(created.id) == restored
    with pytest.raises(ConversationWriteConflict):
        repo.save_conversation(projected)


def test_failed_model_metadata_commit_does_not_publish_settings_or_mutate_owned_history(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "Kept"}])
    repo.get_conversation(created.id)
    repo.list_conversations()
    cached = repo._record_cache[created.id]
    write = repo._safe_write_text

    def fail_manifest(path, text, encoding="utf-8"):
        if path == repo._manifest_path_for(created.id):
            raise OSError("simulated metadata publication failure")
        return write(path, text, encoding=encoding)

    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "_safe_write_text", fail_manifest)
        with pytest.raises(OSError, match="publication failure"):
            repo.update_model_selection(created.id, provider="custom", model="rejected")
    assert repo._record_cache[created.id] is cached
    assert cached.model_selection == {}
    assert repo.get_conversation_summary(created.id).model_selection == {}
    assert ConversationRepository(tmp_path).get_conversation(created.id).model_selection == {}


def test_model_metadata_observes_other_writer_and_deleted_owner(tmp_path):
    repo = ConversationRepository(tmp_path)
    created = repo.create_conversation(transcript=[{"role": "user", "content": "first"}])
    repo.get_conversation(created.id)
    other = ConversationRepository(tmp_path)
    other.append_transcript_message(created.id, {"role": "user", "content": "other writer"})
    latest = other.update_model_selection(created.id, provider="custom", model="new", reasoning_effort="high")
    unchanged = repo.update_model_selection(created.id, provider="old", model="old", only_if_unset=True)
    assert unchanged.model_selection == latest.model_selection and unchanged.revision == latest.revision
    selected = repo.update_model_selection(created.id, provider="custom", model="newer")
    assert selected.model_selection["reasoning_effort"] == "high"
    assert created.id not in repo._record_cache
    assert repo.get_conversation(created.id).transcript[-1]["content"] == "other writer"
    other.delete_conversation(created.id)
    assert repo.update_model_selection(created.id, provider="custom", model="deleted") is None
