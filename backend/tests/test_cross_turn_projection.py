from __future__ import annotations

import json
import re
from copy import deepcopy

import pytest

from backend.conversations.repository import ConversationRepository, ConversationStorageCorruptError, ConversationWriteConflict
from backend.conversations.context_delta import context_snapshot_delta


def seed(repo, count=40):
    history = [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": f"kept {i}"} for i in range(count)]
    return repo.create_conversation(transcript=history, context_snapshot={"history": [{"role": m["role"], "content": m["content"]} for m in history]})


def test_multiple_turns_use_one_checkpoint_and_cold_readers_see_every_message(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    checkpoint = repo.transcript_path(original.id)
    original_bytes = checkpoint.read_bytes()
    writes = []
    write = repo._write_generation
    monkeypatch.setattr(repo, "_write_generation", lambda *args: (writes.append(args[1]), write(*args))[1])
    snapshot = deepcopy(original.context_snapshot)
    expected = deepcopy(original.transcript)
    for turn in range(3):
        user = {"id": f"u{turn}", "role": "user", "content": f"input {turn}"}
        snapshot["history"].append({"role": "user", "content": user["content"]})
        admitted = repo.commit_turn_admission(original.id, user_message=user, context_snapshot=snapshot)
        expected = admitted.transcript
        for partial in (True, False):
            answer = {"id": f"a{turn}", "role": "assistant", "content": f"{'partial' if partial else 'final'} {turn}"}
            next_snapshot = {"history": [*snapshot["history"], {"role": "assistant", "content": answer["content"]}]}
            base = repo.get_projection_context(original.id)
            result = repo.commit_turn_projection(original.id, assistant_message=answer,
                context_delta=context_snapshot_delta(base[1], next_snapshot), expected_revision=base[0], partial=partial)
            expected = result.transcript
        snapshot = next_snapshot
    assert writes == [], "Normal turns must not rewrite complete history generations"
    assert checkpoint.read_bytes() == original_bytes
    cold = ConversationRepository(tmp_path)
    actual = cold.get_conversation(original.id)
    assert actual.transcript == expected and actual.context_snapshot == snapshot
    assert actual.message_count == 46
    page = ConversationRepository(tmp_path).get_transcript_page(original.id, limit=4)
    assert [m["id"] for m in page["transcript"]] == ["u1", "a1", "u2", "a2"]
    prior = ConversationRepository(tmp_path).get_transcript_page(original.id, limit=2, before_message_id="u1")
    assert [m["id"] for m in prior["transcript"]] == ["u0", "a0"]
    for message_id in ("m1", "u0", "a0", "a2"):
        assert ConversationRepository(tmp_path)._get_transcript_message(original.id, message_id) == next(m for m in expected if m["id"] == message_id)
    match = ConversationRepository(tmp_path).find_transcript_match(original.id, re.compile("final 1"))
    assert match is not None


def test_duplicate_ids_keep_position_and_earlier_replacements(tmp_path):
    repo = ConversationRepository(tmp_path)
    original = seed(repo, 2)
    repo.append_transcript_message(original.id, {"id": "same", "role": "assistant", "content": "first"})
    repo.append_transcript_message(original.id, {"id": "same", "role": "assistant", "content": "second"})
    result = repo.upsert_transcript_message(original.id, {"id": "same", "role": "assistant", "content": "updated last"})
    result = repo.upsert_transcript_message(original.id, {"id": "m0", "role": "user", "content": "changed first"})
    cold = ConversationRepository(tmp_path)
    assert cold.get_conversation(original.id).transcript == result.transcript
    assert [m["content"] for m in cold.get_conversation_view(original.id)["transcript"]] == ["changed first", "kept 1", "first", "updated last"]
    assert cold._get_transcript_message(original.id, "same")["content"] == "first"


def test_context_saves_and_metadata_do_not_require_a_new_checkpoint(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    def forbidden(*_args):
        raise AssertionError("Unchanged history was rewritten")
    monkeypatch.setattr(repo, "_write_generation", forbidden)
    changed = {**original.context_snapshot, "note": "new"}
    saved = repo.save_context_snapshot(original.id, changed)
    renamed = repo.rename_conversation(original.id, "renamed")
    repo.set_archived(original.id, True)
    restored = ConversationRepository(tmp_path).get_conversation(original.id)
    assert restored.context_snapshot == changed and restored.transcript == original.transcript
    assert restored.title == "renamed" and restored.archived
    assert restored.revision > renamed.revision > saved.revision


def test_failed_delta_publication_and_retry_preserve_committed_prefix(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    repo.append_transcript_message(original.id, {"id": "first", "role": "user", "content": "committed"})
    before = repo.get_conversation(original.id)
    write = repo._safe_write_text
    def fail(path, text, encoding="utf-8"):
        if path.name.endswith(".manifest.json"):
            raise OSError("injected manifest failure")
        return write(path, text, encoding)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "_safe_write_text", fail)
        with pytest.raises(OSError, match="injected"):
            repo.append_transcript_message(original.id, {"id": "lost", "role": "user", "content": "uncommitted"})
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == before.transcript
    saved = repo.append_transcript_message(original.id, {"id": "retry", "role": "user", "content": "committed retry"})
    assert [m["id"] for m in saved.transcript][-2:] == ["first", "retry"]
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == saved.transcript


def test_periodic_checkpoint_retains_fallback_and_does_not_lose_cross_turn_deltas(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    monkeypatch.setattr(repo, "_MAX_PROJECTION_ADMISSIONS", 2)
    first = repo.commit_turn_admission(original.id, user_message={"id": "u1", "role": "user", "content": "first"}, context_snapshot={"history": []})
    before = repo._read_manifest(original.id)
    assert before["current_generation"] == original.revision
    second = repo.commit_turn_admission(original.id, user_message={"id": "u2", "role": "user", "content": "second"}, context_snapshot={"history": []})
    manifest = repo._read_manifest(original.id)
    assert manifest["current_generation"] == second.revision
    path = repo._generation_paths(original.id, manifest["current_generation"])[2]
    path.write_text("broken", encoding="utf-8")
    fallback = ConversationRepository(tmp_path).get_conversation(original.id)
    assert fallback.revision == first.revision and fallback.transcript == first.transcript


def test_cold_indexed_page_never_loads_private_checkpoint(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    repo.append_transcript_message(original.id, {"id": "user", "role": "user", "content": "next"})
    repo.upsert_transcript_message(original.id, {"id": "answer", "role": "assistant", "content": "visible"})
    cold = ConversationRepository(tmp_path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("UI page loaded the private checkpoint")
    monkeypatch.setattr(cold, "_read_snapshot_path", forbidden)
    assert [m["id"] for m in cold.get_conversation_view(original.id, limit=2)["transcript"]] == ["user", "answer"]


def test_stale_detached_save_conflicts_after_incremental_admission(tmp_path):
    first, second = ConversationRepository(tmp_path), ConversationRepository(tmp_path)
    original = seed(first)
    stale = second.get_conversation(original.id)
    first.append_transcript_message(original.id, {"id": "new", "role": "user", "content": "keep"})
    with pytest.raises(ConversationWriteConflict):
        second.save_conversation(stale)
    assert second.get_conversation(original.id).transcript[-1]["id"] == "new"


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_admission_writes_only_the_new_input_not_the_old_checkpoint(tmp_path, monkeypatch, count):
    repo = ConversationRepository(tmp_path)
    original = seed(repo, count)
    writes = []
    write = repo._safe_write_text
    def observe(path, text, encoding="utf-8"):
        writes.append((path.name, len(text.encode(encoding))))
        return write(path, text, encoding)
    monkeypatch.setattr(repo, "_safe_write_text", observe)
    next_snapshot = {"history": [*original.context_snapshot["history"], {"role": "user", "content": "same new input"}]}
    repo.commit_turn_admission(original.id, user_message={"id": "new", "role": "user", "content": "same new input"}, context_snapshot=next_snapshot)
    pointer = repo._read_manifest(original.id)["projection_log"]
    assert sum(size for _, size in writes) + pointer["bytes"] < 10000
    assert all(".transcript." not in name and ".snapshot." not in name for name, _ in writes)


def test_failed_periodic_checkpoint_preserves_the_entire_previous_delta_chain(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    repo.commit_turn_admission(original.id, user_message={"id": "u1", "role": "user", "content": "first"}, context_snapshot={})
    before = repo.get_conversation(original.id)
    monkeypatch.setattr(repo, "_MAX_PROJECTION_ADMISSIONS", 2)
    write = repo._safe_write_text
    def fail(path, text, encoding="utf-8"):
        if ".snapshot." in path.name:
            raise OSError("checkpoint write failed")
        return write(path, text, encoding)
    with monkeypatch.context() as scoped:
        scoped.setattr(repo, "_safe_write_text", fail)
        with pytest.raises(OSError, match="checkpoint"):
            repo.commit_turn_admission(original.id, user_message={"id": "u2", "role": "user", "content": "second"}, context_snapshot={})
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == before.transcript
    after = repo.commit_turn_admission(original.id, user_message={"id": "u2", "role": "user", "content": "second"}, context_snapshot={})
    assert [m["id"] for m in after.transcript][-2:] == ["u1", "u2"]


def test_projection_byte_limit_compacts_and_retires_only_unneeded_generations(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    repo.append_transcript_message(original.id, {"id": "u", "role": "user", "content": "first"})
    monkeypatch.setattr(repo, "_MAX_PROJECTION_BYTES", 1)
    result = repo.upsert_transcript_message(original.id, {"id": "a", "role": "assistant", "content": "answer"})
    manifest = repo._read_manifest(original.id)
    assert manifest["current_generation"] == result.revision
    assert manifest["previous_projection_log"]["generation"] == original.revision
    assert ConversationRepository(tmp_path).get_conversation(original.id).transcript == result.transcript


def test_metadata_cache_rechecks_changed_files_and_remains_bounded(tmp_path):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    metadata = repo._read_generation_metadata(original.id, original.revision)
    assert repo._read_generation_metadata(original.id, original.revision) is metadata
    path = repo._generation_paths(original.id, original.revision)[0]
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ConversationStorageCorruptError):
        repo._read_generation_metadata(original.id, original.revision)
    for index in range(70):
        created = seed(repo, 2)
        repo._read_generation_metadata(created.id, created.revision)
    assert len(repo._generation_metadata_cache) <= repo._MAX_RECORD_CACHE


def test_returned_records_cannot_mutate_the_committed_projection_base(tmp_path):
    repo = ConversationRepository(tmp_path)
    original = seed(repo, 2)
    result = repo.commit_turn_admission(original.id, user_message={"id": "u", "role": "user", "content": "saved"},
                                       context_snapshot={"history": [{"role": "user", "content": "saved"}]})
    result.transcript[-1]["content"] = "caller changed this result"
    result.context_snapshot["history"][-1]["content"] = "caller changed the checkpoint"
    result = repo.upsert_transcript_message(original.id, {"id": "a", "role": "assistant", "content": "answer"})
    cold = ConversationRepository(tmp_path).get_conversation(original.id)
    assert cold.transcript[-2]["content"] == "saved"
    assert cold.context_snapshot["history"][-1]["content"] == "saved"
    assert cold.transcript == result.transcript


def test_internal_admission_returns_metadata_without_copying_the_old_history(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo, 1000)
    snapshot = {"history": [*original.context_snapshot["history"], {"role": "user", "content": "new input"}]}
    cached = repo._record_cache[original.id]
    result = repo.commit_turn_admission(original.id, user_message={"id": "u", "role": "user", "content": "new input"},
        context_snapshot=snapshot, expected_revision=original.revision, return_record=False)
    assert not hasattr(result, "transcript")
    assert repo._record_cache[original.id].transcript[0] is cached.transcript[0]
    snapshot["history"][-1]["content"] = "caller mutation"
    snapshot["history"][0]["content"] = "caller changed the old input too"
    restored = ConversationRepository(tmp_path).get_conversation(original.id)
    assert restored.context_snapshot["history"][-1]["content"] == "new input"
    assert restored.context_snapshot["history"][0]["content"] == "kept 0"
    assert restored.revision == result.revision


def test_late_tool_facts_do_not_force_a_full_history_checkpoint(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    original = seed(repo)
    repo.append_transcript_message(original.id, {"id": "parent", "role": "assistant", "content": "accepted",
        "tool_calls": [{"id": "spawn", "name": "task", "args": {}, "status": "success"}]})
    def forbidden(*_args):
        raise AssertionError("A late tool fact rewrote the checkpoint")
    monkeypatch.setattr(repo, "_write_generation", forbidden)
    tool = {"id": "spawn", "name": "task", "args": {}, "status": "success", "summary": "Accepted"}
    assert repo.update_tool_call(original.id, message_id="parent", tool_call=tool)
    delegation = {"agent_id": "child", "name": "worker", "status": "running", "started_at": 80, "accepted_at": 20}
    assert repo.update_tool_delegation(original.id, message_id="parent", tool_call_id="spawn", delegation=delegation)
    cold = ConversationRepository(tmp_path).get_conversation(original.id)
    assert cold.transcript[-1]["tool_calls"][0]["delegations"][0]["status"] == "running"
