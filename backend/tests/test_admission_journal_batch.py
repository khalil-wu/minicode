from __future__ import annotations

import pytest

from backend.agent.execution_journal import ExecutionJournal, ExecutionJournalError


def entries():
    return [("user_prompt", {"conversation_id": "conv", "content": "input", "context_snapshot": {"history": []}}, "user"),
            ("system", {"lifecycle": "turn_input_consumed", "conversation_id": "conv", "user_message_id": "user"}, "consumed")]


def test_journal_reuses_captured_lock_identity_and_sees_other_writer(tmp_path, monkeypatch):
    from unittest.mock import Mock

    first = ExecutionJournal("shared", base_dir=tmp_path)
    second = ExecutionJournal("shared", base_dir=tmp_path)
    assert first._thread_lock is second._thread_lock
    with monkeypatch.context() as scoped:
        resolve = Mock(side_effect=AssertionError("Journal lock identity was already captured"))
        scoped.setattr("backend.agent.execution_journal.canonical_file_path_key", resolve)
        first.append("system", {"source": "first"})
        second.append_once_many(entries())
        assert [event.seq for event in first.read_events()] == [1, 2, 3]
        resolve.assert_not_called()
    assert [event.seq for event in ExecutionJournal("shared", base_dir=tmp_path).read_events()] == [1, 2, 3]


def test_admission_batch_preserves_two_facts_with_one_fsync(tmp_path, monkeypatch):
    journal = ExecutionJournal("batch", base_dir=tmp_path)
    calls = []
    sync = __import__("os").fsync
    monkeypatch.setattr("backend.agent.execution_journal.os.fsync", lambda fd: (calls.append(fd), sync(fd))[1])
    saved = journal.append_once_many(entries())
    assert len(calls) == 1
    assert [e.event_id for e in saved] == ["user", "consumed"]
    assert [e.seq for e in saved] == [1, 2]
    replay = journal.append_once_many(entries())
    assert [e.to_dict() for e in replay] == [e.to_dict() for e in saved]
    assert len(ExecutionJournal("batch", base_dir=tmp_path).read_events()) == 2


def test_failed_batch_discards_only_its_unpublished_tail(tmp_path, monkeypatch):
    journal = ExecutionJournal("batch", base_dir=tmp_path)
    journal.append_once("system", {"before": True}, event_id="before")
    old_bytes = journal.path.read_bytes()
    append = journal._append_unlocked
    def fail_second(*args, **kwargs):
        if kwargs.get("event_id") == "consumed":
            raise OSError("injected second entry failure")
        return append(*args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(journal, "_append_unlocked", fail_second)
        with pytest.raises(OSError, match="injected"):
            journal.append_once_many(entries())
    assert journal.path.read_bytes() == old_bytes
    result = journal.append_once_many(entries())
    assert [e.seq for e in result] == [2, 3]
    assert len(ExecutionJournal("batch", base_dir=tmp_path).read_events()) == 3


def test_batch_replays_a_complete_first_entry_and_writes_only_the_missing_fact(tmp_path):
    journal = ExecutionJournal("batch", base_dir=tmp_path)
    kind, payload, identity = entries()[0]
    original = journal.append_once(kind, payload, event_id=identity)
    result = ExecutionJournal("batch", base_dir=tmp_path).append_once_many(entries())
    assert result[0].to_dict() == original.to_dict()
    assert result[1].seq == 2


def test_conflicting_batch_is_rejected_before_any_new_entry_is_written(tmp_path):
    journal = ExecutionJournal("batch", base_dir=tmp_path)
    journal.append_once("system", {"different": True}, event_id="consumed")
    before = journal.path.read_bytes()
    with pytest.raises(ExecutionJournalError):
        journal.append_once_many(entries())
    assert journal.path.read_bytes() == before


def test_fsync_failure_keeps_the_previous_chain_retryable(tmp_path, monkeypatch):
    journal = ExecutionJournal("batch", base_dir=tmp_path)
    journal.append_once("system", {"before": True}, event_id="before")
    before = journal.path.read_bytes()
    sync = __import__("os").fsync
    count = 0
    def fail_once(fd):
        nonlocal count
        count += 1
        if count == 1:
            raise OSError("injected sync failure")
        return sync(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr("backend.agent.execution_journal.os.fsync", fail_once)
        with pytest.raises(OSError, match="injected"):
            journal.append_once_many(entries())
    assert journal.path.read_bytes() == before
    journal.append_once_many(entries())
    assert [e.event_id for e in ExecutionJournal("batch", base_dir=tmp_path).read_events()] == ["before", "user", "consumed"]
