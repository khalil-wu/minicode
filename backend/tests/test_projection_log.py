from __future__ import annotations

import asyncio
import json

import pytest

from backend.agent.execution_journal import ExecutionJournal, ExecutionJournalCorruptionError
from backend.conversations.context_delta import context_snapshot_delta
from backend.conversations.projection_log import apply_value_change, value_change
from backend.conversations.repository import ConversationRepository, ConversationStorageCorruptError
from backend.ws.agent_runner import _replay_pending_conversation_projections


def message(text, **extra):
    return {"id": "answer", "role": "assistant", "content": text,
            "blocks": [{"type": "text", "content": text, "source": "model_final"}], **extra}


def advance(repo, cid, text, *, terminal=False):
    base = repo.get_projection_context(cid)
    after = {"history": [{"role": "assistant", "content": text}], "note": text[-8:]}
    return repo.commit_turn_projection(cid, assistant_message=message(text, terminal_status="completed" if terminal else "partial"),
                                      context_delta=context_snapshot_delta(base[1], after),
                                      expected_revision=base[0], partial=not terminal, return_record=False)


@pytest.mark.parametrize("before,after", [
    ({"x": 1}, {"x": True, "nil": None}),
    ({"x": ["中文", 2, 3]}, {"x": ["中文😀", 7]}),
    ({"a": {"b": "prefix"}, "gone": 1}, {"a": {"b": "replacement"}}),
    ({"a": []}, {"a": [None, {"nested": [False]}]}),
    ({"blocks": [{"content": "a"}, {"content": "b"}]}, {"blocks": [{"content": "abc"}, {"content": "bcd"}]}),
])
def test_change_preserves_json_types_replacements_and_multiple_list_positions(before, after):
    original = json.dumps(before)
    restored = apply_value_change(before, value_change(before, after))
    assert json.dumps(restored, sort_keys=True) == json.dumps(after, sort_keys=True)
    assert json.dumps(before) == original


def test_partial_growth_is_linear_and_cold_page_preserves_block_zero(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": []})
    original_write = repo._safe_write_text
    writes = []

    def count_write(path, text, encoding="utf-8"):
        writes.append(len(text.encode(encoding)))
        original_write(path, text, encoding)

    monkeypatch.setattr(repo, "_safe_write_text", count_write)
    measured = {}
    text = ""
    for index in range(64):
        text += "中文 text " * 128
        advance(repo, record.id, text)
        if index in {31, 63}:
            manifest = repo._read_manifest(record.id)
            path = repo._partial_projection_path(record.id, manifest["current_generation"])
            measured[index + 1] = sum(writes) + path.stat().st_size
    assert measured[64] < measured[32] * 2.3
    assert manifest["version"] == 8
    assert "partial_projection" not in manifest
    page = ConversationRepository(tmp_path).get_conversation_view(record.id)
    assert page["transcript"][-1]["content"] == text
    assert page["transcript"][-1]["blocks"][0]["content"] == text
    restored = ConversationRepository(tmp_path).get_conversation(record.id)
    assert restored.context_snapshot["history"][0]["content"] == text


def test_failed_manifest_write_and_incomplete_tail_are_not_replayed(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": []})
    first = advance(repo, record.id, "committed")
    manifest = repo._read_manifest(record.id)
    path = repo._partial_projection_path(record.id, manifest["current_generation"])
    old_position = manifest["projection_log"]["bytes"]
    write = repo._safe_write_text

    def fail_manifest(target, text, encoding="utf-8"):
        if target == repo._manifest_path_for(record.id):
            raise PermissionError("injected manifest failure")
        write(target, text, encoding)

    with monkeypatch.context() as change:
        change.setattr(repo, "_safe_write_text", fail_manifest)
        with pytest.raises(PermissionError):
            advance(repo, record.id, "uncommitted text")
    with path.open("ab") as handle:
        handle.write(b'{"interrupted":')
    cold = ConversationRepository(tmp_path)
    assert cold.get_conversation(record.id).transcript[-1]["content"] == "committed"
    assert cold.get_conversation(record.id).revision == first.revision
    assert path.stat().st_size > old_position
    advance(repo, record.id, "retried", terminal=True)
    assert path.stat().st_size == repo._read_manifest(record.id)["projection_log"]["bytes"]
    assert ConversationRepository(tmp_path).get_conversation(record.id).transcript[-1]["content"] == "retried"


def test_checkpoint_fallback_retains_the_previous_projection_log_and_reclaims_it(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": []})
    before = advance(repo, record.id, "retained partial")
    old_log = repo._partial_projection_path(record.id, repo._read_manifest(record.id)["current_generation"])
    committed = repo.commit_turn_projection(record.id, assistant_message=message("done"),
                                           context_snapshot={"history": []}, expected_revision=before.revision)
    manifest = repo._read_manifest(record.id)
    assert manifest["previous_projection_log"]["bytes"] == old_log.stat().st_size
    snapshot_path = repo._generation_paths(record.id, manifest["current_generation"])[2]
    valid = snapshot_path.read_bytes()
    snapshot_path.write_text("broken", encoding="utf-8")
    restored = ConversationRepository(tmp_path).get_conversation(record.id)
    assert restored.transcript[-1]["content"] == "retained partial"
    assert restored.revision == before.revision
    snapshot_path.write_bytes(valid)
    repo.append_transcript_message(record.id, {"id": "next", "role": "user", "content": "next"})
    assert not old_log.exists()
    advance(repo, record.id, "new partial")
    assert list(tmp_path.glob("*.projection.jsonl"))
    assert repo.delete_conversation(record.id)
    assert not list(tmp_path.glob("*.projection.jsonl"))


def test_corrupt_committed_projection_never_becomes_an_empty_success(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": []})
    advance(repo, record.id, "required evidence")
    manifest = repo._read_manifest(record.id)
    log = repo._partial_projection_path(record.id, manifest["current_generation"])
    log.write_bytes(log.read_bytes()[:-3])
    with pytest.raises(ConversationStorageCorruptError):
        ConversationRepository(tmp_path).get_conversation(record.id)


def test_legacy_inline_projection_migrates_without_losing_its_base(tmp_path):
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": []})
    advance(repo, record.id, "legacy")
    path = repo._manifest_path_for(record.id)
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["partial_projection"] = repo._load_partial_projection(record.id, manifest)
    manifest.pop("projection_log")
    manifest["version"] = 3
    path.write_text(json.dumps(manifest), encoding="utf-8")
    repo._partial_projection_path(record.id, manifest["current_generation"]).unlink()
    migrated = ConversationRepository(tmp_path)
    assert migrated.get_conversation(record.id).transcript[-1]["content"] == "legacy"
    advance(migrated, record.id, "legacy continued")
    cold = ConversationRepository(tmp_path).get_conversation(record.id)
    assert cold.transcript[-1]["content"] == "legacy continued"
    assert cold.context_snapshot["history"][0]["content"] == "legacy continued"


def test_two_repositories_extend_the_latest_committed_projection(tmp_path):
    first, second = ConversationRepository(tmp_path), ConversationRepository(tmp_path)
    record = first.create_conversation(context_snapshot={"history": []})
    advance(first, record.id, "first")
    advance(second, record.id, "second")
    first.rename_conversation(record.id, "kept title")
    advance(first, record.id, "second continued")
    assert second.get_conversation(record.id).transcript[-1]["content"] == "second continued"
    assert second.get_conversation(record.id).title == "kept title"


def test_journal_delta_recovers_exact_pending_payload_after_another_writer(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(context_snapshot={"history": []})
    journal = ExecutionJournal("projection", base_dir=tmp_path / "journal")
    payload = {"conversation_id": record.id, "assistant_message": message("x" * 12000),
               "context_snapshot": {"history": []}, "expected_revision": record.revision}
    first = journal.append_lifecycle("conversation_projection_pending", payload)
    next_payload = {**payload, "assistant_message": message("x" * 12000 + " new")}
    other = ExecutionJournal("projection", base_dir=tmp_path / "journal")
    second = other.append_lifecycle("conversation_projection_pending", next_payload)
    assert second.payload["lifecycle"] == "conversation_projection_delta"
    assert len(json.dumps(second.payload)) < 1000
    third_payload = {**next_payload, "assistant_message": message("replaced final")}
    third = journal.append_lifecycle("conversation_projection_pending", third_payload)
    recovered = ExecutionJournal("projection", base_dir=tmp_path / "journal")
    assert recovered.pending_conversation_projections()[-1].payload["assistant_message"] == third_payload["assistant_message"]
    # A later committed replacement supersedes both earlier unpublished views.
    repo.commit_turn_projection(record.id, **{k: v for k, v in third_payload.items() if k != "conversation_id"})
    journal.append_lifecycle("conversation_projection_committed", {"conversation_id": record.id,
                             "pending_event_id": third.event_id, "message_id": "answer"})
    assert ExecutionJournal("projection", base_dir=tmp_path / "journal").pending_conversation_projections() == []


def test_delta_only_pending_projection_replays_through_the_real_recovery_entry(tmp_path):
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(context_snapshot={"history": []})
    journal = ExecutionJournal("recovery", base_dir=tmp_path / "journal")
    payload = {"conversation_id": record.id, "assistant_message": message("first"),
               "context_delta": {"set": {}, "removed": []}, "partial": True, "expected_revision": record.revision}
    first = journal.append_lifecycle("conversation_projection_pending", payload)
    committed = repo.commit_turn_projection(record.id, **{k: v for k, v in payload.items() if k != "conversation_id"})
    journal.append_lifecycle("conversation_projection_committed", {"conversation_id": record.id,
                             "pending_event_id": first.event_id, "message_id": "answer"})
    second = journal.append_lifecycle("conversation_projection_pending", {**payload,
                                      "assistant_message": message("first continued"), "expected_revision": committed.revision})
    assert second.payload["lifecycle"] == "conversation_projection_delta"
    asyncio.run(_replay_pending_conversation_projections(repo, ExecutionJournal("recovery", base_dir=tmp_path / "journal"), conversation_id=record.id))
    assert ConversationRepository(tmp_path / "conversations").get_conversation(record.id).transcript[-1]["content"] == "first continued"


def test_journal_rejects_a_delta_that_references_another_base(tmp_path):
    journal = ExecutionJournal("corrupt-base", base_dir=tmp_path)
    payload = {"conversation_id": "conv", "assistant_message": message("before")}
    journal.append_lifecycle("conversation_projection_pending", payload)
    journal.append_lifecycle("conversation_projection_pending", {**payload, "assistant_message": message("after")})
    rows = [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]
    rows[1]["payload"]["base_event_id"] = "wrong base"
    journal.path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    with pytest.raises(ExecutionJournalCorruptionError, match="matching base"):
        ExecutionJournal("corrupt-base", base_dir=tmp_path).read_events()
