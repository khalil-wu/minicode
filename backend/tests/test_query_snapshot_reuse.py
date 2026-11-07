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
