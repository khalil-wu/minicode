from __future__ import annotations

import asyncio
import json

import pytest

from backend.agent.execution_journal import ExecutionJournal
from backend.conversations.context_delta import apply_context_snapshot_delta, compose_context_snapshot_deltas, context_snapshot_delta
from backend.conversations.repository import ConversationRepository, ConversationWriteConflict
from backend.ws.agent_runner import _replay_pending_conversation_projections


@pytest.mark.parametrize("middle", [[], [1], [1, 2, 3], [7, 2], [1, 2, 8, 9]])
@pytest.mark.parametrize("last", [[], [1], [1, 2, 3], [7, 2], [1, 2, 8, 9]])
def test_delta_composition_preserves_rewrites_truncation_and_metadata(middle, last):
    before = {"history": [1, 2, 3], "keep": 4, "remove": 5}
    intermediate = {"history": middle, "keep": 6, "added": "x"}
    after = {"history": last, "keep": 4, "final": True}
    first = context_snapshot_delta(before, intermediate)
    second = context_snapshot_delta(intermediate, after)
    assert apply_context_snapshot_delta(before, first) == intermediate
    assert apply_context_snapshot_delta(intermediate, second) == after
    assert apply_context_snapshot_delta(before, compose_context_snapshot_deltas(first, second)) == after


def test_partial_context_and_transcript_survive_reload_metadata_change_and_journal_replay(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    history = [{"role": "user", "content": f"{i}:" + "x" * 4096} for i in range(200)]
    snapshot = {"history": history, "note": "original"}
    record = repo.create_conversation(context_snapshot=snapshot, transcript=[{"id": f"m-{i}", **m} for i, m in enumerate(history)])
    checkpoint_path = repo.transcript_path(record.id)
    journal = ExecutionJournal("audit-delta", base_dir=tmp_path / "journals")
    written = []
    real_write = repo._safe_write_text
    def write(path, text, encoding="utf-8"):
        written.append((path.name, len(text.encode(encoding))))
        real_write(path, text, encoding)
    monkeypatch.setattr(repo, "_safe_write_text", write)

    next_snapshot = {"history": history + [{"role": "assistant", "content": "first step"}], "note": "working"}
    first = repo.commit_turn_projection(record.id,
        assistant_message={"id": "answer", "role": "assistant", "content": "first step", "terminal_status": "partial"},
        context_delta=context_snapshot_delta(snapshot, next_snapshot), partial=True, expected_revision=record.revision)
    assert sum(size for _, size in written) < 20000
    assert repo.transcript_path(record.id) == checkpoint_path
    assert not any(".transcript." in name or ".snapshot." in name for name, _ in written)
    renamed = repo.rename_conversation(record.id, "during execution")
    restored = ConversationRepository(tmp_path / "conversations").get_conversation(record.id)
    assert restored.context_snapshot == next_snapshot
    assert restored.transcript[-1]["content"] == "first step"
    assert restored.title == "during execution"
    assert restored.revision == renamed.revision > first.revision

    final_snapshot = {"history": next_snapshot["history"] + [{"role": "user", "content": "follow up"}]}
    pending = journal.append_lifecycle("conversation_projection_pending", {
        "conversation_id": record.id,
        "assistant_message": {"id": "answer", "role": "assistant", "content": "more work", "terminal_status": "partial"},
        "context_delta": context_snapshot_delta(next_snapshot, final_snapshot),
        "partial": True, "expected_revision": renamed.revision,
    })
    asyncio.run(_replay_pending_conversation_projections(repo, journal, conversation_id=record.id))
    recovered = ConversationRepository(tmp_path / "conversations").get_conversation(record.id)
    assert recovered.context_snapshot == final_snapshot
    assert recovered.transcript[-1]["content"] == "more work"
    assert len(recovered.transcript) == len(history) + 1
    assert not journal.pending_conversation_projections()
    asyncio.run(_replay_pending_conversation_projections(repo, journal, conversation_id=record.id))
    assert repo.get_conversation(record.id).revision == recovered.revision
    assert pending.event_id

    terminal = repo.commit_turn_projection(record.id,
        assistant_message={"id": "answer", "role": "assistant", "content": "done"},
        context_snapshot=final_snapshot, expected_revision=recovered.revision)
    manifest = json.loads((tmp_path / "conversations" / f"{record.id}.manifest.json").read_text(encoding="utf-8"))
    assert "partial_projection" not in manifest
    # A failed newer checkpoint recovers the complete previous partial state.
    (tmp_path / "conversations" / f"{record.id}.g{manifest['current_generation']}.snapshot.json").write_text("invalid", encoding="utf-8")
    fallback = ConversationRepository(tmp_path / "conversations").get_conversation(record.id)
    assert fallback.context_snapshot == final_snapshot
    assert fallback.transcript[-1]["content"] == "more work"
    assert fallback.revision == recovered.revision < terminal.revision


def test_delta_rejects_invalid_checkpoint_offset():
    with pytest.raises(ValueError, match="checkpoint history"):
        apply_context_snapshot_delta({"history": []}, {"history_from": 10, "history": []})


def test_title_change_cannot_hide_a_new_context_delta_with_unchanged_assistant_text(tmp_path):
    repo = ConversationRepository(tmp_path)
    initial = {"history": [], "note": 0}
    record = repo.create_conversation(context_snapshot=initial)
    message = {"id": "answer", "role": "assistant", "content": "working"}
    first = repo.commit_turn_projection(record.id, assistant_message=message,
        context_delta=context_snapshot_delta(initial, {**initial, "note": 1}), partial=True,
        expected_revision=record.revision)
    delta = context_snapshot_delta(first.context_snapshot, {**initial, "note": 2})
    repo.rename_conversation(record.id, "keep this newer title")
    second = repo.commit_turn_projection(record.id, assistant_message=message, context_delta=delta,
                                         partial=True, expected_revision=first.revision)
    restored = ConversationRepository(tmp_path).get_conversation(record.id)
    assert restored.title == "keep this newer title"
    assert restored.context_snapshot["note"] == 2
    duplicate = repo.commit_turn_projection(record.id, assistant_message=message, context_delta=delta,
                                             partial=True, expected_revision=first.revision)
    assert duplicate.revision == second.revision
    with pytest.raises(ConversationWriteConflict):
        repo.commit_turn_projection(record.id, assistant_message=message,
            context_delta={"set": {"note": 3}, "removed": []}, partial=True, expected_revision=first.revision)
