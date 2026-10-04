from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.agent.execution_journal import ExecutionJournal
from backend.conversations.context_delta import context_snapshot_delta
from backend.conversations.repository import ConversationRepository
from backend.services.conversation_projection_service import replay_pending_conversation_projections
from backend.ws.conversation_runtime import ConversationRuntime


def _case(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    journal = ExecutionJournal("projection-owner", base_dir=tmp_path / "journal")
    conversation = repository.create_conversation()
    return repository, journal, conversation


def _admit(repository, conversation_id, user_id, run_id, before=None):
    snapshot = dict(before or {})
    history = list(snapshot.get("history", []))
    start = len(history)
    history.append({"role": "user", "content": "identical prompt"})
    snapshot["history"] = history
    snapshot["turn_admissions"] = {
        **snapshot.get("turn_admissions", {}),
        user_id: {"history_start": start, "history_end": len(history), "run_id": run_id, "client_command_id": user_id},
    }
    return repository.commit_turn_admission(conversation_id,
        user_message={"id": user_id, "role": "user", "content": "identical prompt"}, context_snapshot=snapshot)


def _projection(record, user_ids, run_id, answer="accepted answer", message_id="assistant-owner"):
    before = record.context_snapshot
    after = {**before, "history": [*before.get("history", []), {"role": "assistant", "content": answer}]}
    return {
        "conversation_id": record.id,
        "assistant_message": {"id": message_id, "role": "assistant", "content": answer, "terminal_status": "completed"},
        "context_snapshot": after,
        "context_delta": context_snapshot_delta(before, after),
        "expected_revision": record.revision,
        "run_id": run_id,
        "source_user_message_ids": user_ids,
    }


def _terminal(journal, payload):
    intent = journal.append_lifecycle("terminal_intent", payload)
    journal.append_lifecycle("runtime_terminal_committed", {
        "run_id": payload["run_id"], "terminal_intent_event_id": intent.event_id,
    })
    return intent


def _receipts(journal):
    return [event.payload for event in journal.read_events() if event.payload.get("lifecycle") == "conversation_projection_committed"]


@pytest.mark.asyncio
async def test_full_snapshot_commit_before_receipt_preserves_later_plan_patch(tmp_path):
    repository, journal, record = _case(tmp_path)
    message = {"id": "accepted-message", "role": "assistant", "content": "accepted reply"}
    snapshot = {"history": [{"role": "assistant", "content": "accepted reply"}]}
    journal.append_lifecycle("conversation_projection_pending", {
        "conversation_id": record.id, "assistant_message": message,
        "context_snapshot": snapshot, "expected_revision": record.revision,
    })
    repository.commit_turn_projection(record.id, assistant_message=message, context_snapshot=snapshot, expected_revision=record.revision)
    patched = repository.patch_context_snapshot(record.id, {"plan_file_reference": {"content": "new plan owner"}})
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    loaded = repository.get_conversation(record.id)
    assert loaded.revision == patched.revision
    assert loaded.context_snapshot["plan_file_reference"] == {"content": "new plan owner"}
    assert len(loaded.transcript) == 1 and journal.pending_conversation_projections() == []
    assert _receipts(journal)[-1]["applied"] is True


@pytest.mark.asyncio
async def test_owned_delta_recovers_answer_and_preserves_new_non_input_state(tmp_path):
    repository, journal, record = _case(tmp_path)
    admitted = _admit(repository, record.id, "u1", "r1")
    journal.append_lifecycle("conversation_projection_pending", _projection(admitted, ["u1"], "r1"))
    repository.patch_context_snapshot(record.id, {"plan_file_reference": {"content": "new plan"}})
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    loaded = repository.get_conversation(record.id)
    assert loaded.context_snapshot["plan_file_reference"] == {"content": "new plan"}
    assert [message["role"] for message in loaded.context_snapshot["history"]] == ["user", "assistant"]
    assert [message["id"] for message in loaded.transcript] == ["u1", "assistant-owner"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "terminal"])
async def test_cleared_owner_is_settled_without_resurrecting_history_and_next_turn_works(tmp_path, kind):
    repository, journal, record = _case(tmp_path)
    admitted = _admit(repository, record.id, "old-u", "old-run")
    payload = _projection(admitted, ["old-u"], "old-run")
    if kind == "pending":
        journal.append_lifecycle("conversation_projection_pending", payload)
    else:
        _terminal(journal, payload)
    cleared = repository.clear_conversation(record.id, context_snapshot={"plan_file_reference": {"content": "kept"}})
    for _ in range(2):
        await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    loaded = repository.get_conversation(record.id)
    assert loaded.transcript == [] and loaded.context_snapshot == cleared.context_snapshot
    assert _receipts(journal)[-1]["applied"] is False and _receipts(journal)[-1]["superseded"] is True
    assert journal.pending_conversation_projections() == [] and journal.unprojected_terminal_projections() == []
    new = _admit(repository, record.id, "new-u", "new-run", loaded.context_snapshot)
    _terminal(journal, _projection(new, ["new-u"], "new-run", "new accepted answer"))
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    assert [message["content"] for message in repository.get_conversation(record.id).transcript] == ["identical prompt", "new accepted answer"]
    assert _receipts(journal)[-1]["applied"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pending", "terminal"])
async def test_rewind_removing_steer_invalidates_result_even_when_original_user_remains(tmp_path, kind):
    repository, journal, record = _case(tmp_path)
    initial = _admit(repository, record.id, "initial", "r1")
    steered = _admit(repository, record.id, "steer", "r1", initial.context_snapshot)
    payload = _projection(steered, ["initial", "steer"], "r1")
    if kind == "pending":
        journal.append_lifecycle("conversation_projection_pending", payload)
    else:
        _terminal(journal, payload)
    runtime = ConversationRuntime(conversation_repo=repository,
        context_builder=SimpleNamespace(load_snapshot=lambda snapshot: None), build_summary_from_transcript=lambda *args, **kwargs: "")
    rewound = runtime.rewind_to_user_turn(conversation=steered, retry_from_message_id="steer")
    assert [message["id"] for message in rewound.transcript] == ["initial"]
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    loaded = repository.get_conversation(record.id)
    assert loaded.transcript == rewound.transcript and loaded.context_snapshot == rewound.context_snapshot
    assert _receipts(journal)[-1]["applied"] is False


@pytest.mark.asyncio
async def test_receipt_for_superseded_run_does_not_cover_new_run_reusing_message_identity(tmp_path):
    repository, journal, record = _case(tmp_path)
    old = _admit(repository, record.id, "u1", "old-run")
    journal.append_lifecycle("conversation_projection_pending", _projection(old, ["u1"], "old-run"))
    repository.clear_conversation(record.id)
    new = _admit(repository, record.id, "u1", "new-run")
    _terminal(journal, _projection(new, ["u1"], "new-run", "new branch result"))
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    assert repository.get_conversation(record.id).transcript[-1]["content"] == "new branch result"
    assert [(receipt["run_id"], receipt["applied"]) for receipt in _receipts(journal)] == [("old-run", False), ("new-run", True)]


def test_superseded_receipt_does_not_acknowledge_another_runs_earlier_partial(tmp_path):
    repository, journal, record = _case(tmp_path)
    admitted = _admit(repository, record.id, "u1", "r1")
    earlier = journal.append_lifecycle("conversation_projection_pending", _projection(admitted, ["u1"], "r1", "partial"))
    stale = journal.append_lifecycle("conversation_projection_pending", _projection(admitted, ["gone"], "r0"))
    journal.append_lifecycle("conversation_projection_committed", {
        "conversation_id": record.id, "pending_event_id": stale.event_id,
        "message_id": "assistant-owner", "run_id": "r0", "applied": False, "superseded": True,
    })
    assert [event.event_id for event in journal.pending_conversation_projections()] == [earlier.event_id]


@pytest.mark.asyncio
async def test_legacy_terminal_without_stable_ownership_keeps_fact_and_releases_replay(tmp_path):
    repository, journal, record = _case(tmp_path)
    payload = {"conversation_id": record.id, "run_id": "legacy-run",
        "assistant_message": {"id": "legacy-message", "role": "assistant", "content": "legacy answer"},
        "context_snapshot": {"history": [{"role": "assistant", "content": "legacy answer"}]}}
    source = _terminal(journal, payload)
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    assert repository.get_conversation(record.id).transcript == []
    assert any(event.event_id == source.event_id for event in journal.read_events())
    assert journal.unprojected_terminal_projections() == [] and _receipts(journal)[-1]["applied"] is False


def test_successful_receipt_does_not_acknowledge_another_runs_partial(tmp_path):
    repository, journal, record = _case(tmp_path)
    admitted = _admit(repository, record.id, "u1", "r1")
    earlier = journal.append_lifecycle("conversation_projection_pending", _projection(admitted, ["u1"], "r1", "partial"))
    later = journal.append_lifecycle("conversation_projection_pending", _projection(admitted, ["u1"], "r2"))
    journal.append_lifecycle("conversation_projection_committed", {
        "conversation_id": record.id, "pending_event_id": later.event_id,
        "message_id": "assistant-owner", "run_id": "r2", "applied": True,
    })
    assert [event.event_id for event in journal.pending_conversation_projections()] == [earlier.event_id]


@pytest.mark.asyncio
async def test_legacy_pending_without_revision_or_owner_cannot_overwrite_current_head(tmp_path):
    repository, journal, record = _case(tmp_path)
    pending = journal.append_lifecycle("conversation_projection_pending", {
        "conversation_id": record.id,
        "assistant_message": {"id": "old", "role": "assistant", "content": "unbound old answer"},
        "context_snapshot": {"history": [{"role": "assistant", "content": "unbound old answer"}]},
    })
    await replay_pending_conversation_projections(repository, journal, conversation_id=record.id)
    assert repository.get_conversation(record.id).transcript == []
    assert any(event.event_id == pending.event_id for event in journal.read_events())
    assert journal.pending_conversation_projections() == []
    assert _receipts(journal)[-1]["applied"] is False
