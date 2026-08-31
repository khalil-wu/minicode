from __future__ import annotations

from pathlib import Path

import pytest

import backend.artifact.store as artifact_store_module
from backend.artifact.store import ArtifactStore
from backend.conversations.repository import ConversationRepository


def test_artifact_save_failure_does_not_publish_partial_record(tmp_path, monkeypatch) -> None:
    real_atomic_write = artifact_store_module.atomic_write_text

    def fail_metadata_write(path: Path, content: str, **kwargs) -> None:
        if path.name.endswith(".meta.json"):
            raise OSError("simulated metadata write failure")
        real_atomic_write(path, content, **kwargs)

    monkeypatch.setattr(artifact_store_module, "atomic_write_text", fail_metadata_write)
    store = ArtifactStore(storage_dir=tmp_path)

    with pytest.raises(OSError, match="simulated metadata write failure"):
        store.save("body", source="failure-test")

    assert store.count == 0
    assert list(tmp_path.glob("*.meta.json")) == []
    assert list(tmp_path.glob("*.txt")) == []


def test_conversation_repository_instances_preserve_interleaved_updates(tmp_path) -> None:
    first = ConversationRepository(base_dir=tmp_path)
    second = ConversationRepository(base_dir=tmp_path)
    conversation_id = "conv_concurrent_test"
    first.create_conversation(conversation_id=conversation_id)

    assert second.get_conversation(conversation_id) is not None
    first.rename_conversation(conversation_id, "Renamed elsewhere")
    second.update_summary(conversation_id, "Summary from second repository")
    first.append_transcript_message(conversation_id, {"role": "user", "content": "first"})
    second.append_transcript_message(conversation_id, {"role": "assistant", "content": "second"})

    restored = ConversationRepository(base_dir=tmp_path).get_conversation(conversation_id)
    assert restored is not None
    assert restored.title == "Renamed elsewhere"
    assert restored.summary == "Summary from second repository"
    assert [message["content"] for message in restored.transcript] == ["first", "second"]
