from __future__ import annotations

import asyncio
from copy import deepcopy
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import zstandard

from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.conversations import compression
from backend.conversations.repository import ConversationRepository, ConversationStorageCorruptError
from backend.services.conversation_projection_service import replay_pending_conversation_projections
from backend.services.general_maintenance import cold_history_files, compress_cold_history, maintain_local_chats
from backend.services.storage_settings_service import disk_usage
from backend.services.file_disk_usage import file_disk_bytes

NOW = datetime(2026, 10, 20, tzinfo=UTC)
OLD = NOW - timedelta(days=9)
CUTOFF_NS = int((NOW - timedelta(days=7)).timestamp() * 1_000_000_000)


def make_history(repository, *, count=82):
    return repository.create_conversation(title="原始历史", transcript=[{
        "id": f"message-{index}", "role": "user" if index % 2 == 0 else "assistant",
        "content": f"Original {index} — 原文\r\n" * 300,
    } for index in range(count)], context_snapshot={
        "history": [{"role": "user", "content": "Private provider messages\r\n原文" * 2000}],
        "provider_items": [{"type": "message", "content": "Original provider body"}],
        "pending_draft": {"text": "ORIGINAL UNSENT DRAFT", "attachments": ["local-file.png"]},
    })


def compress_record(repository, conversation_id):
    paths = repository.history_paths(conversation_id)
    for path in paths:
        if path.exists():
            os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    files = cold_history_files(repository, protected_ids=set(), now=NOW)
    assert compress_cold_history(repository, files, now=NOW) == len(files)
    return files


def test_single_zstd_frame_preserves_all_original_bytes_metadata_and_private_context(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository)
    metadata = (repository.storage_root / f"{record.id}.manifest.json").read_bytes()
    originals = {path: path.read_bytes() for path in repository.history_paths(record.id)}
    for path in originals:
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    assert compress_cold_history(repository, list(originals), now=NOW) == len(originals)
    for logical, original in originals.items():
        compressed = compression.compressed_history_path(logical)
        assert not logical.exists() and compressed.is_file()
        frame = zstandard.get_frame_parameters(compressed.read_bytes())
        assert frame.content_size == len(original) and frame.has_checksum
        assert compression.read_history_bytes(logical) == original
        assert compressed.stat().st_mtime_ns == int(OLD.timestamp() * 1_000_000_000)
    assert (repository.storage_root / f"{record.id}.manifest.json").read_bytes() == metadata
    cold = ConversationRepository(repository.storage_root)
    assert cold.get_conversation(record.id).context_snapshot == record.context_snapshot
    assert cold.get_conversation(record.id).transcript == record.transcript
    assert cold.transcript_path(record.id).suffix == ".zst"
    assert not list(repository.storage_root.glob("*.tmp"))


def test_indexed_pages_read_decoded_offsets_without_materializing_or_loading_private_checkpoint(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository)
    files = compress_record(repository, record.id)
    cold = ConversationRepository(repository.storage_root)
    monkeypatch.setattr(cold, "_read_generation", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Unexpected full checkpoint read")))
    observed = []
    before = ""
    while True:
        page = cold.get_transcript_page(record.id, limit=17, before_message_id=before)
        observed = [message["id"] for message in page["transcript"]] + observed
        if not page["transcript_page"]["has_more"]:
            break
        before = page["transcript_page"]["before_message_id"]
    assert observed == [message["id"] for message in record.transcript]
    assert all(not path.exists() and compression.compressed_history_path(path).exists() for path in files)


def test_tool_pagination_and_deferred_diff_read_the_complete_compressed_message(tmp_path, monkeypatch):
    from backend.tests.test_tool_history_pagination import long_message

    repository = ConversationRepository(tmp_path / "conversations")
    conversation_id = "conv_zstd_tool_history"
    patch = "diff --git a/output.py b/output.py\n--- a/output.py\n+++ b/output.py\n@@ -0,0 +1000 @@\n" + ("+" + "Original diff line 原文 " * 60 + "\n") * 1000
    message = long_message(240)
    message["metadata"] = {"turn_diff": {"thread_id": conversation_id, "conversation_id": conversation_id,
        "message_id": "answer", "turn_id": "fixture-turn", "revision": 1, "diff": patch, "source": "tool", "truncated": False}}
    record = repository.create_conversation(conversation_id=conversation_id, transcript=[message])
    compress_record(repository, record.id)
    cold = ConversationRepository(repository.storage_root)
    monkeypatch.setattr(cold, "_read_generation", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Unexpected private checkpoint read")))
    page = cold.get_conversation_view(record.id)
    message = page["transcript"][0]
    assert message["tool_page"]["total"] == 240
    assert message["metadata"]["turn_diff"]["deferred"] and message["metadata"]["turn_diff"]["diff"] is None
    seen = {block["transcriptIndex"] for block in message["blocks"] if block["type"] == "tool_call"}
    cursor = message["tool_page"]
    while cursor["remaining"]:
        earlier = cold.get_message_tool_items(record.id, "answer", before=cursor["before"], limit=37, revision=cursor["revision"])
        seen.update(block["transcriptIndex"] for block in earlier["blocks"])
        cursor = earlier["tool_page"]
    assert seen == set(range(240))
    assert cold.get_message_turn_diff(record.id, "answer", turn_id="fixture-turn", revision=1)["diff"] == patch


def test_compressed_projection_is_materialized_only_for_append_and_keeps_its_committed_prefix(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    first = repository.commit_turn_projection(record.id,
        assistant_message={"id": "streaming-answer", "role": "assistant", "content": "First partial"},
        context_delta={"set": {"progress": "first"}, "removed": []}, partial=True)
    projection = next(path for path in repository.history_paths(record.id) if path.name.endswith(".projection.jsonl"))
    committed = projection.read_bytes()
    with projection.open("ab") as handle:
        handle.write(b"uncommitted crash fragment")
    compress_record(repository, record.id)
    assert compression.read_history_bytes(projection) == committed + b"uncommitted crash fragment"
    cold = ConversationRepository(repository.storage_root)
    second = cold.commit_turn_projection(record.id,
        assistant_message={"id": "streaming-answer", "role": "assistant", "content": "Complete original answer"},
        context_delta={"set": {"progress": "second"}, "removed": []}, expected_revision=first.revision)
    assert projection.exists() and not compression.compressed_history_path(projection).exists()
    assert projection.read_bytes().startswith(committed)
    assert b"uncommitted crash fragment" not in projection.read_bytes()
    reloaded = ConversationRepository(repository.storage_root).get_conversation(record.id)
    assert reloaded.context_snapshot["progress"] == "second"
    assert reloaded.transcript[-1]["content"] == "Complete original answer"
    assert reloaded.revision == second.revision
    assert reloaded.context_snapshot["pending_draft"] == record.context_snapshot["pending_draft"]


def test_new_checkpoint_reclaims_all_stale_zstd_generations_and_delete_removes_both_representations(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    first = make_history(repository, count=2)
    first_files = compress_record(repository, first.id)
    repository.append_transcript_message(first.id, {"id": "new-user", "role": "user", "content": "Continue without changing old history"})
    second_files = compress_record(repository, first.id)
    assert first_files and second_files
    latest = repository.append_transcript_message(first.id, {"id": "new-answer", "role": "assistant", "content": "Continued"})
    assert all(not compression.compressed_history_path(path).exists() for path in first_files)
    assert ConversationRepository(repository.storage_root).get_conversation(first.id).transcript == latest.transcript
    assert repository.delete_conversation(first.id)
    remaining = list(repository.storage_root.glob(f"{first.id}.*"))
    assert [path.name for path in remaining] == [f"{first.id}.manifest.json"]
    assert ConversationRepository(repository.storage_root).get_conversation(first.id) is None


def test_truncated_current_frame_recovers_previous_generation_and_retains_damaged_evidence(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    first = make_history(repository, count=2)
    repository.append_transcript_message(first.id, {"id": "next-user", "role": "user", "content": "A newer checkpoint"})
    compress_record(repository, first.id)
    manifest = json.loads((repository.storage_root / f"{first.id}.manifest.json").read_text(encoding="utf8"))
    damaged = repository.storage_root / f"{first.id}.g{manifest['current_generation']}.snapshot.json.zst"
    damaged.write_bytes(damaged.read_bytes()[:-2])
    with pytest.raises(compression.HistoryCompressionError, match="truncated"):
        compression.read_history_bytes(damaged)
    cold = ConversationRepository(repository.storage_root)
    recovered = cold.get_conversation(first.id)
    assert recovered.transcript == first.transcript
    assert recovered.context_snapshot == first.context_snapshot
    cold.append_transcript_message(first.id, {"id": "recovered-user", "role": "user", "content": "Resume from the real previous checkpoint"})
    assert damaged.exists()
    assert ConversationRepository(repository.storage_root).get_conversation(first.id).transcript[-1]["id"] == "recovered-user"


def test_unreadable_only_generation_is_an_explicit_storage_error_not_an_empty_chat(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    files = compress_record(repository, record.id)
    snapshot = next(compression.compressed_history_path(path) for path in files if path.name.endswith(".snapshot.json"))
    snapshot.write_bytes(b"invalid zstd frame")
    with pytest.raises(ConversationStorageCorruptError):
        ConversationRepository(repository.storage_root).get_conversation(record.id)
    assert snapshot.read_bytes() == b"invalid zstd frame"


def test_failed_materialization_never_replaces_the_original_compressed_history(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    files = compress_record(repository, record.id)
    logical = files[0]
    compressed = compression.compressed_history_path(logical)
    before = compressed.read_bytes()
    replace = compression.os.replace
    def fail_publish(source, destination):
        if Path(destination) == logical:
            raise OSError("fixture materialization publish failure")
        return replace(source, destination)
    monkeypatch.setattr(compression.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failure"):
        compression.materialize_history_for_append(logical)
    assert not logical.exists()
    assert compressed.read_bytes() == before
    assert not list(repository.storage_root.glob("*.tmp"))


def test_failed_compression_publish_keeps_plain_bytes_and_cleans_its_temporary_file(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    logical = repository.history_paths(record.id)[0]
    original = logical.read_bytes()
    os.utime(logical, (OLD.timestamp(), OLD.timestamp()))
    replace = compression.os.replace
    def fail_publish(source, destination):
        if Path(destination) == compression.compressed_history_path(logical):
            raise OSError("fixture compression publish failure")
        return replace(source, destination)
    monkeypatch.setattr(compression.os, "replace", fail_publish)
    with pytest.raises(OSError, match="publish failure"):
        repository.compress_history_file(logical, cutoff_ns=CUTOFF_NS)
    assert logical.read_bytes() == original
    assert not compression.compressed_history_path(logical).exists()
    assert not list(repository.storage_root.glob("*.tmp"))


def test_plain_representation_wins_after_interrupted_compression_publication(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    logical = repository.history_paths(record.id)[0]
    original = logical.read_bytes()
    os.utime(logical, (OLD.timestamp(), OLD.timestamp()))
    unlink = Path.unlink
    def keep_original(path, *args, **kwargs):
        if path == logical:
            raise OSError("fixture interrupted after zstd publish")
        return unlink(path, *args, **kwargs)
    with monkeypatch.context() as context:
        context.setattr(Path, "unlink", keep_original)
        with pytest.raises(OSError, match="interrupted"):
            repository.compress_history_file(logical, cutoff_ns=CUTOFF_NS)
    compressed = compression.compressed_history_path(logical)
    assert logical.exists() and compressed.exists()
    usage = disk_usage(tmp_path, repository=repository)
    assert usage["total_bytes"] == sum(file_disk_bytes(path) for path in repository.storage_root.iterdir() if path.is_file())
    # The ordinary read and next writer must choose the current plain bytes.
    assert compression.read_history_bytes(logical) == original
    assert compression.materialize_history_for_append(logical) == logical
    assert logical.read_bytes() == original and not compressed.exists()


def test_changed_projection_and_deleted_owner_are_skipped_during_publication(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    logical = repository.history_paths(record.id)[0]
    os.utime(logical, (OLD.timestamp(), OLD.timestamp()))
    original = logical.read_bytes()
    decode = compression._decode_frame
    def changed_after_verification(path, output):
        result = decode(path, output)
        logical.write_bytes(original + b"\n")
        return result
    with monkeypatch.context() as context:
        context.setattr(compression, "_decode_frame", changed_after_verification)
        assert not repository.compress_history_file(logical, cutoff_ns=CUTOFF_NS)
    assert logical.read_bytes() == original + b"\n"
    assert not compression.compressed_history_path(logical).exists()
    os.utime(logical, (OLD.timestamp(), OLD.timestamp()))
    def deleted_after_verification(path, output):
        result = decode(path, output)
        repository.delete_conversation(record.id)
        return result
    with monkeypatch.context() as context:
        context.setattr(compression, "_decode_frame", deleted_after_verification)
        assert not repository.compress_history_file(logical, cutoff_ns=CUTOFF_NS)
    assert repository.get_conversation(record.id) is None
    assert not compression.compressed_history_path(logical).exists()
    assert not list(repository.storage_root.glob("*.tmp"))


def test_query_journal_recovers_a_committed_terminal_into_compressed_history_without_rewriting_its_raw_evidence(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    before = {"history": [{"role": "user", "content": "Original input"}], "pending_draft": "KEEP UNSENT CONTENT"}
    record = repository.create_conversation(transcript=[{"id": "original-user", "role": "user", "content": "Original input"}], context_snapshot=before)
    after = {**deepcopy(before), "history": [*before["history"], {"role": "assistant", "content": "Recovered original answer"}]}
    journal = ExecutionJournal("fixture-query-recovery", base_dir=tmp_path / "sidechains")
    recorder = QueryJournalRecorder(journal=journal,
        metadata={"run_id": "fixture-run", "user_message_id": "original-user", "assistant_message_id": "recovered-answer"},
        state=SimpleNamespace(reply="Recovered original answer"), context_builder=SimpleNamespace(export_snapshot=lambda: deepcopy(after)),
        turn_kernel=None, conversation_id=record.id, projection_base_snapshot=deepcopy(before))
    recorder.record_terminal_intent(AgentEvent.done(status="completed"))
    recorder.record_event(AgentEvent(type="agent.run.completed", data={"run_id": "fixture-run", "status": "completed"}))
    raw_evidence = journal.path.read_bytes()
    compress_record(repository, record.id)
    cold = ConversationRepository(repository.storage_root)
    asyncio.run(replay_pending_conversation_projections(cold, journal, conversation_id=record.id))
    loaded = ConversationRepository(repository.storage_root).get_conversation(record.id)
    assert [message["id"] for message in loaded.transcript] == ["original-user", "recovered-answer"]
    assert loaded.context_snapshot == after
    assert journal.path.read_bytes().startswith(raw_evidence)
    assert not journal.path.with_suffix(".jsonl.zst").exists()
    assert not journal.unprojected_terminal_projections()
    revision = loaded.revision
    asyncio.run(replay_pending_conversation_projections(cold, journal, conversation_id=record.id))
    assert ConversationRepository(repository.storage_root).get_conversation(record.id).revision == revision


def test_legacy_plain_split_files_and_compressed_split_files_migrate_without_losing_original_history(tmp_path):
    source = ConversationRepository(tmp_path / "source")
    record = make_history(source, count=2)
    legacy_root = tmp_path / "conversations"
    legacy_root.mkdir()
    metadata = record.to_meta_dict()
    # Split storage predates the manifest revision protocol. The source's
    # committed generation is not a revision of this independent legacy store.
    for field in ("revision", "content_revision", "content_updated_at"):
        metadata.pop(field)
    (legacy_root / f"{record.id}.meta.json").write_text(json.dumps(metadata), encoding="utf8")
    transcript = legacy_root / f"{record.id}.transcript.jsonl"
    transcript.write_text("".join(json.dumps(message, ensure_ascii=False) + "\n" for message in record.transcript), encoding="utf8", newline="")
    (legacy_root / f"{record.id}.snapshot.json").write_text(json.dumps(record.context_snapshot), encoding="utf8")
    legacy = ConversationRepository(legacy_root)
    loaded = legacy.get_conversation(record.id)
    assert loaded.revision == 0
    assert loaded.transcript == record.transcript
    files = compress_record(legacy, record.id)
    assert len(files) == 2
    migrated = legacy.append_transcript_message(record.id, {"id": "after-migration", "role": "user", "content": "Continue the exact old conversation"})
    assert migrated.transcript[:-1] == record.transcript
    assert migrated.context_snapshot == record.context_snapshot
    assert all(compression.compressed_history_path(path).exists() for path in files)
    assert legacy.delete_conversation(record.id)
    assert all(not compression.compressed_history_path(path).exists() for path in files)


def test_compression_is_opt_in_and_does_not_touch_active_or_recent_history(tmp_path, monkeypatch):
    from backend.services import general_maintenance as maintenance

    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    paths = repository.history_paths(record.id)
    assert not cold_history_files(repository, protected_ids=set(), now=datetime.now(UTC))
    for path in paths:
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    assert not cold_history_files(repository, protected_ids={record.id}, now=NOW)
    manager = SimpleNamespace(resource_sessions=lambda: [], execution_contexts=SimpleNamespace(context_for=lambda _id: None))
    monkeypatch.setattr(maintenance, "_current_scheduler", lambda: None)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Disabled preference started a compression job")
    result = asyncio.run(maintain_local_chats(manager, repository=repository, auto_archive_after_days=0,
        compress_local_history=False, now=NOW, compress=forbidden))
    assert result["compressed_files"] == 0
    assert all(path.exists() for path in paths)


def test_exact_seven_day_cutoff_is_eligible_and_newer_history_is_retained(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    exact, recent = repository.history_paths(record.id)
    os.utime(exact, ns=(CUTOFF_NS, CUTOFF_NS))
    os.utime(recent, ns=(CUTOFF_NS + 1_000_000_000, CUTOFF_NS + 1_000_000_000))
    assert repository.compress_history_file(exact, cutoff_ns=CUTOFF_NS)
    assert not repository.compress_history_file(recent, cutoff_ns=CUTOFF_NS)
    assert recent.exists() and not compression.compressed_history_path(recent).exists()


def test_projection_append_refuses_an_incomplete_frame_without_publishing_a_new_revision(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    current = repository.commit_turn_projection(record.id,
        assistant_message={"id": "streaming-answer", "role": "assistant", "content": "Original partial"},
        context_delta={"set": {"progress": "first"}, "removed": []}, partial=True)
    projection = next(path for path in repository.history_paths(record.id) if path.name.endswith(".projection.jsonl"))
    compress_record(repository, record.id)
    compressed = compression.compressed_history_path(projection)
    damaged = compressed.read_bytes()[:-2]
    compressed.write_bytes(damaged)
    manifest = repository.storage_root / f"{record.id}.manifest.json"
    committed = manifest.read_bytes()
    with pytest.raises((compression.HistoryCompressionError, ConversationStorageCorruptError)):
        repository.commit_turn_projection(record.id,
            assistant_message={"id": "streaming-answer", "role": "assistant", "content": "Uncommitted later text"},
            context_delta={"set": {"progress": "second"}, "removed": []}, expected_revision=current.revision)
    assert manifest.read_bytes() == committed
    assert compressed.read_bytes() == damaged and not projection.exists()
    assert not list(repository.storage_root.glob("*.tmp"))


def test_storage_counts_the_frame_published_after_its_directory_snapshot(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    files = repository.history_paths(record.id)
    for path in files:
        os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    directory = repository.storage_root
    iterdir = Path.iterdir
    switched = False
    def publish_after_scandir(path):
        nonlocal switched
        if path == directory and not switched:
            captured = list(iterdir(path))
            switched = True
            assert compress_cold_history(repository, list(files), now=NOW) == len(files)
            return iter(captured)
        return iterdir(path)
    with monkeypatch.context() as context:
        context.setattr(Path, "iterdir", publish_after_scandir)
        usage = disk_usage(tmp_path, repository=repository)
    assert switched
    assert usage["total_bytes"] == sum(file_disk_bytes(path) for path in directory.iterdir() if path.is_file())
    assert next(category["bytes"] for category in usage["categories"] if category["name"] == "chats") > 0
    repository.set_archived(record.id, True)
    archived = disk_usage(tmp_path, repository=repository)
    assert next(category["bytes"] for category in archived["categories"] if category["name"] == "chats") == 0
    assert next(category["bytes"] for category in archived["categories"] if category["name"] == "archived") > 0


def test_compression_cannot_publish_a_frame_outside_its_owned_store(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    outside = tmp_path / f"{record.id}.snapshot.json"
    outside.write_bytes(b"Outside original data")
    with pytest.raises(ValueError, match="inside its conversation store"):
        repository.compress_history_file(outside, cutoff_ns=CUTOFF_NS)
    assert outside.read_bytes() == b"Outside original data"
    assert not compression.compressed_history_path(outside).exists()


def test_only_stale_owned_transition_temporaries_are_reclaimed(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    stale = repository.storage_root / f".{record.id}.g1.snapshot.json.compress-abandoned.tmp"
    recent = repository.storage_root / f".{record.id}.g1.transcript.jsonl.decompress-active.tmp"
    foreign = repository.storage_root / ".original-user-document.snapshot.json.compress-private.tmp"
    for path in (stale, recent, foreign):
        path.write_bytes(b"Unpublished transition bytes")
    os.utime(recent, (NOW.timestamp(), NOW.timestamp()))
    os.utime(stale, (OLD.timestamp(), OLD.timestamp()))
    os.utime(foreign, (OLD.timestamp(), OLD.timestamp()))
    assert repository.cleanup_history_temporaries(older_than_ns=int((NOW - timedelta(hours=6)).timestamp() * 1_000_000_000)) == 1
    assert not stale.exists() and recent.exists() and foreign.exists()
    assert repository.get_conversation(record.id).context_snapshot == record.context_snapshot


def test_temporary_published_while_cleaner_waits_for_its_owner_lock_is_not_statted_or_deleted(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    record = make_history(repository, count=2)
    logical = next(path for path in repository.history_paths(record.id) if path.name.endswith(".snapshot.json"))
    original = logical.read_bytes()
    staged = repository.storage_root / f".{logical.name}.compress-publication.tmp"
    staged.write_bytes(zstandard.ZstdCompressor(write_content_size=True, write_checksum=True).compress(original))
    os.utime(staged, (OLD.timestamp(), OLD.timestamp()))
    owner_lock = repository._store_lock
    published = False

    @contextmanager
    def publication_finishes_before_cleaner_acquires(*owners):
        nonlocal published
        with owner_lock(*owners):
            if owners == (record.id,) and not published:
                # The cleaner saw this staged path in its directory snapshot;
                # the publisher completes before the cleaner owns the lock.
                os.replace(staged, compression.compressed_history_path(logical))
                logical.unlink()
                published = True
            yield

    monkeypatch.setattr(repository, "_store_lock", publication_finishes_before_cleaner_acquires)
    assert repository.cleanup_history_temporaries(older_than_ns=int((NOW - timedelta(hours=6)).timestamp() * 1_000_000_000)) == 0
    assert published and not staged.exists()
    assert compression.read_history_bytes(logical) == original
    assert repository.get_conversation(record.id).context_snapshot == record.context_snapshot
