from copy import deepcopy

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.state import AgentState
from backend.llm.base import LLMMessage


def recorder(tmp_path):
    builder = ContextBuilder()
    for index in range(1000):
        builder._history_store.append(LLMMessage(role="user", content=f"retained {index}"))
    return QueryJournalRecorder(ExecutionJournal("snapshot-reuse", base_dir=tmp_path),
        {"run_id": "run", "turn_id": "turn"}, AgentState(user_message="continue"),
        builder, None, "conversation")


def test_query_start_persists_ordered_facts_with_one_sync_per_attempt(tmp_path, monkeypatch):
    from backend.agent import execution_journal

    owner = recorder(tmp_path)
    sync = execution_journal.os.fsync
    synced = []

    def observe(fd):
        synced.append(fd)
        sync(fd)

    monkeypatch.setattr(execution_journal.os, "fsync", observe)
    owner.record_turn_started()
    events = owner.journal.read_events()
    assert len(synced) == 1
    assert [event.payload["lifecycle"] for event in events] == ["turn_started", "provider_claimed"]
    assert events[0].payload == {
        "lifecycle": "turn_started", "conversation_id": "conversation",
        "run_id": "run", "turn_id": "turn", "context_snapshot": owner.context_builder.export_snapshot(),
    }
    assert owner.projection_base_snapshot is events[0].payload["context_snapshot"]
    # A resumed execution of the same turn is a new provider attempt.
    owner.context_builder.append_assistant("retained before resuming")
    owner.record_turn_started()
    recovered = ExecutionJournal("snapshot-reuse", base_dir=tmp_path).read_events()
    assert len(synced) == 2
    assert [event.seq for event in recovered] == [1, 2, 3, 4]
    assert len({event.event_id for event in recovered}) == 4
    assert recovered[2].payload["context_snapshot"] == owner.context_builder.export_snapshot()


@pytest.mark.parametrize("failure", ["second_fact", "sync"])
def test_failed_query_start_preserves_previous_recovery_and_retries(tmp_path, monkeypatch, failure):
    from backend.agent import execution_journal

    owner = recorder(tmp_path)
    owner.record_turn_started()
    previous = owner.journal.path.read_bytes()
    original = deepcopy(owner.projection_base_snapshot)
    owner.context_builder.append_assistant("new attempt")
    append = owner.journal._append_unlocked
    sync = execution_journal.os.fsync
    syncs = 0

    def append_fact(kind, payload, **kwargs):
        if payload.get("lifecycle") == "provider_claimed":
            raise OSError("startup write failed")
        return append(kind, payload, **kwargs)

    def sync_fact(fd):
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            raise OSError("startup sync failed")
        sync(fd)

    with monkeypatch.context() as scoped:
        if failure == "second_fact":
            scoped.setattr(owner.journal, "_append_unlocked", append_fact)
        else:
            scoped.setattr(execution_journal.os, "fsync", sync_fact)
        with pytest.raises(OSError, match="startup"):
            owner.record_turn_started()
    assert owner.journal.path.read_bytes() == previous
    recovered = ExecutionJournal("snapshot-reuse", base_dir=tmp_path).read_events()
    assert recovered[0].payload["context_snapshot"] == original
    assert len(recovered) == 2
    owner.record_turn_started()
    recovered = ExecutionJournal("snapshot-reuse", base_dir=tmp_path).read_events()
    assert [event.seq for event in recovered] == [1, 2, 3, 4]
    assert recovered[2].payload["context_snapshot"] == owner.context_builder.export_snapshot()
    assert recovered[3].payload["lifecycle"] == "provider_claimed"


def test_query_captures_share_only_owned_unchanged_journal_history(tmp_path, monkeypatch):
    from backend.conversations import projection_log

    owner = recorder(tmp_path)
    owner.record_turn_started()
    started = owner.journal.read_events()[0]
    assert owner.projection_base_snapshot is started.payload["context_snapshot"]
    owner.context_builder.append_assistant("new reply")
    captured = owner.capture_context_snapshot()
    base = owner.projection_base_snapshot["history"]
    assert all(original is updated for original, updated in zip(base, captured["history"]))
    traversed = []
    change = projection_log.value_change
    ids = {id(item) for item in base}

    def observe(before, after):
        if id(before) in ids:
            traversed.append(before is after)
        return change(before, after)

    monkeypatch.setattr(projection_log, "value_change", observe)
    owner.record_terminal_intent(AgentEvent.done(status="completed"))
    assert traversed and all(traversed)
    assert ExecutionJournal("snapshot-reuse", base_dir=tmp_path).read_events()[-1].payload["context_snapshot"] == captured


@pytest.mark.parametrize("mutation", ["append", "rewrite", "expired", "clear"])
def test_journal_snapshot_reuses_history_and_keeps_exact_recovery_after_changes(tmp_path, monkeypatch, mutation):
    from backend.agent import context as context_module

    owner = recorder(tmp_path)
    owner.record_turn_started()
    baseline = deepcopy(owner.projection_base_snapshot)
    builder = owner.context_builder
    builder.extension_state["entries"] = [{"id": "new", "type": "custom", "data": {"kept": True}}]
    if mutation == "append":
        builder.append_assistant("new reply")
        expected_serialized = 1
    elif mutation == "rewrite":
        builder._history[0].content = "changed retained prefix"
        builder._history_store.refresh_message_estimate(builder._history[0])
        expected_serialized = 1000
    elif mutation == "expired":
        for index in range(260):
            builder.append_assistant(f"later {index}")
        expected_serialized = 1260
    else:
        builder.clear()
        builder.append_user("replacement after clear")
        expected_serialized = 1
    expected = builder.export_snapshot()
    original = context_module._sanitize_provider_items
    serialized = []

    def observe(items):
        serialized.append(items)
        return original(items)

    with monkeypatch.context() as scoped:
        scoped.setattr(context_module, "_sanitize_provider_items", observe)
        captured = owner.capture_context_snapshot()
    assert len(serialized) == expected_serialized
    assert captured == expected
    assert owner.projection_base_snapshot == baseline
    owner.record_terminal_intent(AgentEvent.done(status="completed"))
    recovered = ExecutionJournal("snapshot-reuse", base_dir=tmp_path).read_events()
    assert recovered[-1].payload["context_snapshot"] == expected


@pytest.mark.asyncio
async def test_real_query_uses_journal_base_for_admission_and_terminal_snapshots(tmp_path, monkeypatch):
    from backend.tests.test_user_input_presentation_chain import session_for
    import asyncio

    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    calls = []
    capture = QueryJournalRecorder.capture_context_snapshot

    def observe(owner):
        captured = capture(owner)
        assert captured == owner.context_builder.export_snapshot()
        calls.append((bool(adapter.requests), len(captured["history"])))
        return captured

    monkeypatch.setattr(QueryJournalRecorder, "capture_context_snapshot", observe)
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
            "conversation_id": conversation.id, "content": "current input",
            "user_message_id": "snapshot-user", "assistant_message_id": "snapshot-answer",
        }))
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        assert len(adapter.requests) == 1
        assert any(not requested for requested, _ in calls), "Admission must use the existing journal snapshot"
        assert any(requested for requested, _ in calls), "Terminal persistence must use the same owner"
    finally:
        await session.session_lifecycle.shutdown(reason="query-snapshot-reuse")


def test_next_turn_starts_from_durable_canonical_history_and_only_serializes_new_rows(tmp_path, monkeypatch):
    from backend.agent import context as context_module

    first = recorder(tmp_path)
    first.record_turn_started()
    first.context_builder.append_assistant("first final answer")
    first.record_terminal_intent(AgentEvent.done(status="completed"))
    retained = deepcopy(first.terminal_intent_event.payload["context_snapshot"])
    first.context_builder.append_user("next user input")
    first.context_builder._history_frozen_count = 1001
    first.context_builder.extension_state["latest"] = "current metadata"
    second = QueryJournalRecorder(first.journal, {"run_id": "next", "turn_id": "next"},
        AgentState(user_message="next user input"), first.context_builder, None, "conversation")
    calls = []
    sanitize = context_module._sanitize_provider_items

    def observed(items):
        calls.append(items)
        return sanitize(items)

    with monkeypatch.context() as patch:
        patch.setattr(context_module, "_sanitize_provider_items", observed)
        second.record_turn_started()
    assert len(calls) == 1
    expected = second.context_builder.export_snapshot()
    assert second.projection_base_snapshot == expected
    assert first.terminal_intent_event.payload["context_snapshot"] == retained
    cold = ExecutionJournal("snapshot-reuse", base_dir=tmp_path).read_events()
    assert cold[-2].payload["context_snapshot"] == expected
    assert cold[-1].payload["lifecycle"] == "provider_claimed"


def test_idle_snapshot_is_normalized_detached_and_invalidated_by_restore_or_branch(tmp_path, monkeypatch):
    from backend.agent import context as context_module
    from backend.agent.context import clone_context_builder
    raw = {"history": [{"role": "system", "content": "import prefix"},
        {"role": "user", "content": "Same literal prompt"}, {"role": "user", "content": "Same literal prompt"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "read", "name": "read_file", "arguments": {"file_path": "owned"}}]}]}
    builder = ContextBuilder()
    builder.load_snapshot(raw)
    builder.prepare_recovery_snapshot()
    before = deepcopy(builder._recovery_snapshot[0])
    raw["history"][1]["content"] = "external caller mutation"
    builder._history[-1].tool_calls[0].arguments["file_path"] = "updated"
    builder._history_store.refresh_message_estimate(builder._history[-1])
    actual, _ = builder.capture_recovery_snapshot()
    assert actual == builder.export_snapshot()
    assert builder._recovery_snapshot[0] == before
    child = clone_context_builder(builder)
    assert child._recovery_snapshot is None
    child._history[0].content = "child edit"
    assert child.capture_recovery_snapshot()[0] == child.export_snapshot()
    assert builder._history[0].content == "Same literal prompt"
    builder.load_snapshot({"history": [{"role": "user", "content": "different conversation"}]})
    assert builder._recovery_snapshot is None
    assert builder.capture_recovery_snapshot()[0] == builder.export_snapshot()


def test_failed_start_does_not_promote_uncommitted_recovery_base(tmp_path, monkeypatch):
    owner = recorder(tmp_path)
    owner.record_turn_started()
    retained = owner.context_builder._recovery_snapshot
    owner.context_builder.append_user("pending new input")
    write = owner.journal.append_once_many
    monkeypatch.setattr(owner.journal, "append_once_many", lambda entries: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(OSError):
        owner.record_turn_started()
    assert owner.context_builder._recovery_snapshot is retained
    monkeypatch.setattr(owner.journal, "append_once_many", write)
    owner.record_turn_started()
    assert owner.context_builder._recovery_snapshot[0] == owner.context_builder.export_snapshot()


def test_idle_preparation_serializes_once_and_subsequent_journal_start_skips_history(tmp_path, monkeypatch):
    from backend.agent import context as context_module

    owner = recorder(tmp_path)
    owner.context_builder.prepare_recovery_snapshot()
    calls = []
    sanitize = context_module._sanitize_provider_items

    def observed(items):
        calls.append(items)
        return sanitize(items)

    with monkeypatch.context() as patch:
        patch.setattr(context_module, "_sanitize_provider_items", observed)
        owner.record_turn_started()
    assert calls == []
    assert owner.projection_base_snapshot == owner.context_builder.export_snapshot()


def test_new_acceptance_provenance_updates_prepared_history():
    builder = ContextBuilder()
    builder.append_user("Accepted source input")
    message = builder._history[-1]
    builder.prepare_recovery_snapshot()
    builder.record_admitted_user_input(message, text="Accepted source input", turn_id="new-turn",
        message_id="input", acceptance_order=3)
    captured, _ = builder.capture_recovery_snapshot()
    assert captured == builder.export_snapshot()
    assert captured["history"][-1]["accepted_input"]["turn_id"] == "new-turn"
