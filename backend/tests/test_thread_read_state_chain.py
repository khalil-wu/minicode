from __future__ import annotations

from copy import deepcopy
import json

import pytest

from backend.conversations.repository import ConversationRepository
from backend.conversations.read_state import ReadStateConflict
from backend.services.turn_notifications import completion_notification


def terminal(repo, owner, turn, *, text="Actual result", status="completed", publish=True):
    return repo.commit_turn_projection(owner, assistant_message={
        "id": f"message-{turn}", "turn_id": turn, "role": "assistant", "content": text,
        "terminal_status": status, "metadata": {"completion_notification": completion_notification(
            status=status, last_agent_message=text)},
    }, context_snapshot={"history": []}, publish_attention=publish)


def mark(repo, owner, unread, state=None):
    state = state or repo.get_conversation_summary(owner).read_state
    return repo.set_conversation_read_state(owner, unread, expected_revision=state["revision"])


def test_live_attention_keeps_earliest_position_and_rejects_unseen_marks(tmp_path):
    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation()
    other = repo.create_conversation()
    baseline = repo.get_conversation_summary(first.id).read_state
    assert baseline == {"firstUnread": None, "revision": f"initial:{first.id}"}
    assert "read_receipt" not in json.loads(repo._manifest_path_for(first.id).read_text(encoding="utf8"))
    with pytest.raises(ReadStateConflict):
        mark(repo, other.id, True, baseline)
    marked = mark(repo, first.id, True, baseline)
    assert marked.read_state["firstUnread"] == {"type": "threadStart"}
    again = mark(repo, first.id, True, marked.read_state)
    assert again.read_state != marked.read_state
    with pytest.raises(ReadStateConflict):
        mark(repo, first.id, False, marked.read_state)
    mark(repo, first.id, False, again.read_state)
    one = terminal(repo, first.id, "turn-1")
    two = terminal(repo, first.id, "turn-2")
    assert one.read_state["firstUnread"] == two.read_state["firstUnread"] == {"type": "turn", "turnId": "turn-1"}
    assert one.read_state["revision"] != two.read_state["revision"]
    for unread in (False, True):
        with pytest.raises(ReadStateConflict) as conflict:
            mark(repo, first.id, unread, one.read_state)
        assert conflict.value.read_state == two.read_state
    read = mark(repo, first.id, False)
    assert terminal(repo, first.id, "turn-2").read_state == read.read_state
    assert terminal(repo, first.id, "historical-turn", publish=False).read_state == read.read_state
    assert ConversationRepository(tmp_path).get_conversation_view(first.id)["read_state"] == read.read_state


def test_receipt_does_not_load_history_or_rewrite_checkpoint_and_survives_rebuild(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation(transcript=[{"id": str(i), "role": "user", "content": "history"} for i in range(1000)])
    before = {p.name: p.read_bytes() for p in repo.history_paths(first.id)}
    cold = ConversationRepository(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Read receipt must not materialize transcript or checkpoint")

    monkeypatch.setattr(cold, "_load_record_for_mutation", forbidden)
    unread = mark(cold, first.id, True)
    assert unread.updated_at == first.updated_at and unread.content_revision == first.content_revision
    assert {p.name: p.read_bytes() for p in repo.history_paths(first.id)} == before
    cold.directory.ensure_current()
    with cold.directory.connect() as db:
        db.execute("DELETE FROM threads")
        db.execute("UPDATE directory_state SET revision=0")
    page = cold.directory.page({"limit": 20})
    assert page["data"][0]["read_state"] == unread.read_state
    reopened = ConversationRepository(tmp_path)
    assert reopened.get_conversation(first.id).read_state == unread.read_state
    assert reopened.get_conversation_view(first.id)["read_state"] == unread.read_state
    frozen = reopened.get_conversation(first.id)
    read = mark(cold, first.id, False)
    frozen.revision = read.revision
    frozen.title = "Metadata writer with an older read receipt"
    reopened.save_conversation(frozen)
    assert reopened.get_conversation(first.id).read_state == read.read_state


def test_receipt_failure_preserves_durable_state_and_caller_can_retry(tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation()
    original = repo.get_conversation_summary(first.id).read_state
    write = repo._safe_write_text

    def fail(path, *args, **kwargs):
        if path == repo._manifest_path_for(first.id):
            raise OSError("publication interrupted")
        return write(path, *args, **kwargs)

    monkeypatch.setattr(repo, "_safe_write_text", fail)
    with pytest.raises(OSError, match="publication interrupted"):
        mark(repo, first.id, True, original)
    assert ConversationRepository(tmp_path).get_conversation_summary(first.id).read_state == original
    monkeypatch.setattr(repo, "_safe_write_text", write)
    assert mark(repo, first.id, True, original).has_unread_turn


@pytest.mark.parametrize("new_mark", [False, True])
def test_rewind_retires_removed_position_but_preserves_a_newer_window_mark(tmp_path, new_mark):
    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation()
    completed = terminal(repo, first.id, "removed-turn")
    captured = deepcopy(completed.read_state)
    if new_mark:
        newer = mark(repo, first.id, True)
    rewound = repo.commit_rewind_projection(first.id, transcript=[], context_snapshot={}, summary="",
        memory_pollution_sources=[], expected_revision=completed.revision,
        expected_read_state_revision=captured["revision"])
    assert rewound.has_unread_turn is new_mark
    if new_mark:
        assert rewound.read_state == newer.read_state
    else:
        assert rewound.read_state["firstUnread"] is None
    assert rewound.last_published_turn == "removed-turn"
    assert repo.publish_thread_attention(first.id, completed.transcript[-1]).read_state == rewound.read_state


def test_rewind_rejects_new_context_even_when_visible_transcript_is_unchanged(tmp_path):
    from backend.conversations.repository import ConversationWriteConflict

    repo = ConversationRepository(tmp_path)
    record = terminal(repo, repo.create_conversation().id, "existing-turn")
    repo.commit_turn_projection(record.id, assistant_message=None, context_delta={"set": {"new_context": True}, "removed": []})
    with pytest.raises(ConversationWriteConflict):
        repo.commit_rewind_projection(record.id, transcript=[], context_snapshot={}, summary="", memory_pollution_sources=[],
            expected_revision=record.revision, expected_read_state_revision=record.read_state["revision"])
    assert repo.get_conversation(record.id).context_snapshot["new_context"] is True


def test_checkpoint_recovery_preserves_acknowledgement_and_dedupe(tmp_path):
    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation()
    completed = terminal(repo, first.id, "turn-1")
    read = mark(repo, first.id, False)
    repo.save_conversation(repo.get_conversation(first.id))
    manifest = json.loads(repo._manifest_path_for(first.id).read_text(encoding="utf8"))
    repo._generation_paths(first.id, manifest["current_generation"])[1].write_text("invalid json", encoding="utf8")
    recovered = ConversationRepository(tmp_path).get_conversation(first.id)
    assert recovered.read_state == read.read_state
    assert recovered.last_published_turn == "turn-1"
    assert repo.publish_thread_attention(first.id, completed.transcript[-1]).read_state == read.read_state


@pytest.mark.parametrize("text,status,goal,unread", [
    ("", "completed", {}, False), ("", "cancelled", {}, False),
    ("Partial evidence", "cancelled", {}, True), ("", "failed", {"status": "active"}, True),
    ("Continue automatically", "completed", {"status": "active"}, False),
    ("<decision>DONT_NOTIFY</decision>", "completed", {}, False),
])
def test_actual_terminal_attention_policy(tmp_path, text, status, goal, unread):
    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation()
    first.goal = goal
    repo.save_conversation(first)
    assert terminal(repo, first.id, "actual-turn", text=text, status=status).has_unread_turn is unread


@pytest.mark.asyncio
async def test_source_command_conflict_returns_current_receipt_without_clearing_it(tmp_path):
    from backend.tests.test_sidebar_metadata_chain import Session
    from backend.ws.handlers.sidebar import handle_thread_read_state

    repo = ConversationRepository(tmp_path)
    first = repo.create_conversation()
    session = Session(repo, "reader", first.id)
    initial = repo.get_conversation_summary(first.id).read_state
    completed = terminal(repo, first.id, "turn-1")
    await handle_thread_read_state(session, {"threadId": first.id, "expectedRevision": initial["revision"], "operation": {"type": "read"}})
    assert session.results[-1]["data"]["reason"] == "readStateConflict"
    assert session.results[-1]["data"]["readState"] == completed.read_state
    assert not session.events
    await handle_thread_read_state(session, {"threadId": first.id, "expectedRevision": completed.read_state["revision"], "operation": {"type": "read"}})
    assert session.events[-1]["type"] == "thread/readState/changed"
    assert session.results[-1]["data"]["readState"]["firstUnread"] is None


@pytest.mark.asyncio
async def test_real_query_publishes_exact_turn_receipt_after_terminal_commit(tmp_path, monkeypatch):
    from backend.tests.test_user_input_presentation_chain import session_for

    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    try:
        await session._run_agent("Complete the real turn", conversation_id=conversation.id,
            metadata={"user_message_id": "read-user", "assistant_message_id": "read-answer"})
        stored = ConversationRepository(session.conversation_repo.storage_root).get_conversation(conversation.id)
        answer = next(row for row in stored.transcript if row["id"] == "read-answer")
        assert stored.read_state["firstUnread"] == {"type": "turn", "turnId": answer["turn_id"]}
        events = [event for event in session.ws.messages if event["type"] == "thread/readState/changed"]
        assert len(events) == 1
        assert events[0]["readState"] == stored.read_state
        assert len(adapter.requests) == 1
        assert session.conversation_repo.get_execution_context(conversation.id).read_state == stored.read_state
    finally:
        await session.session_lifecycle.shutdown(reason="real_read_receipt")


@pytest.mark.asyncio
async def test_pending_journal_projection_restores_result_without_new_attention(tmp_path):
    from backend.agent.execution_journal import ExecutionJournal
    from backend.services.conversation_projection_service import replay_pending_conversation_projections

    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation()
    notice = completion_notification(status="completed", last_agent_message="Recovered answer")
    journal = ExecutionJournal("read-state-replay", base_dir=tmp_path / "journal")
    journal.append_lifecycle("conversation_projection_pending", {
        "conversation_id": record.id, "expected_revision": record.revision,
        "assistant_message": {"id": "recovered", "role": "assistant", "content": "Recovered answer",
            "turn_id": "recovered-turn", "terminal_status": "completed", "metadata": {"completion_notification": notice}},
        "context_snapshot": {},
    })
    await replay_pending_conversation_projections(repo, journal, conversation_id=record.id)
    loaded = repo.get_conversation(record.id)
    assert loaded.transcript[-1]["content"] == "Recovered answer"
    assert loaded.read_state["firstUnread"] is None
