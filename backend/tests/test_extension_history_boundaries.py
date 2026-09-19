from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.extension_actions import ExtensionExecutionActions
from backend.agent.extension_history import REWIND_KEY, extension_values, rewind_extension_state
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.query_engine import QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.conversations.repository import ConversationRepository
from backend.extensions.lifecycle_observer import lifecycle_observer_factory
from backend.llm.base import LLMMessage
from backend.tests.test_extension_model_ownership import bind
from backend.tests.test_model_execution_ownership import setup
from backend.tools.subagent_support import _fork_snapshot_for_child
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.utils import build_summary_from_transcript


def actions(builder, run_id="run"):
    return ExtensionExecutionActions(SimpleNamespace(
        context_builder=builder, metadata={"run_id": run_id},
        run_context=SimpleNamespace(extension_name_setter=None, execution_journal=None),
    ))


async def two_turns(builder):
    first = actions(builder, "first")
    entry = first.append_entry("before-first", {"value": 1})
    first.set_label(entry, "original")
    builder.append_user("first")
    builder.record_turn_admission("user-1", {"history_start": 0, "history_end": 1})
    builder.append_assistant("first answer")
    first.set_session_name("first complete")
    expected = deepcopy(builder.extension_state)
    second = actions(builder, "second")
    second.append_entry("future", {"value": 2})
    second.set_label(entry, "changed later")
    builder.append_user("second")
    builder.record_turn_admission("user-2", {"history_start": 2, "history_end": 3})
    builder.append_assistant("second answer")
    second.set_session_name("second complete")
    second.send_message("source-owned pending delivery")
    builder.extension_state["followups"] = [{"content": "source-owned followup"}]
    return expected, entry


@pytest.mark.asyncio
async def test_historical_fork_and_persisted_retry_restore_private_state_and_admissions(tmp_path):
    builder = ContextBuilder()
    expected, entry = await two_turns(builder)
    original = builder.export_snapshot()
    fork = builder.fork_from(1)
    assert fork.extension_state == expected
    assert fork.extension_cursor == {}
    assert list(fork.export_snapshot()["turn_admissions"]) == ["user-1"]
    fork.extension_state["labels"][entry] = "branch edit"
    fork._history[0].content = "branch history edit"
    fork._history[0].attachment_refs.append({"artifact_id": "branch-only"})
    assert builder.export_snapshot() == original

    repo = ConversationRepository(tmp_path / "conversations")
    transcript = [{"id": f"{'user' if index % 2 == 0 else 'assistant'}-{index // 2 + 1}",
                   "role": message.role, "content": message.content} for index, message in enumerate(builder._history)]
    source = repo.create_conversation(transcript=transcript, context_snapshot=original)
    restored_builder = ContextBuilder()
    runtime = ConversationRuntime(conversation_repo=repo, context_builder=restored_builder,
                                  build_summary_from_transcript=build_summary_from_transcript)
    updated = runtime.rewind_to_user_turn(conversation=source, retry_from_message_id="user-2")
    cold = ConversationRepository(tmp_path / "conversations").get_conversation(source.id)
    assert updated.context_snapshot["extension_state"] == cold.context_snapshot["extension_state"] == expected
    assert restored_builder.extension_state == expected
    assert [message["id"] for message in cold.transcript] == ["user-1", "assistant-1"]
    assert original["extension_state"]["labels"][entry] == "changed later"


@pytest.mark.asyncio
async def test_clone_keeps_current_values_without_source_queues_or_cursor(tmp_path):
    builder = ContextBuilder()
    await two_turns(builder)
    repo = ConversationRepository(tmp_path)
    source = repo.create_conversation(context_snapshot=builder.export_snapshot())
    clone = repo.clone_conversation(source.id)
    cold = ConversationRepository(tmp_path).get_conversation(clone.id)
    state = cold.context_snapshot["extension_state"]
    assert extension_values(state) == extension_values(builder.extension_state)
    assert "pending_messages" not in state and "followups" not in state
    assert "extension_cursor" not in cold.context_snapshot
    assert "pending_messages" in repo.get_conversation(source.id).context_snapshot["extension_state"]


@pytest.mark.asyncio
async def test_appended_private_payload_is_not_copied_into_undo_records():
    builder = ContextBuilder()
    extension = actions(builder)
    for index in range(64):
        extension.append_entry("large", {"index": index, "payload": "x" * 1024})
    encoded = json.dumps(builder.extension_state[REWIND_KEY])
    assert "xxxx" not in encoded
    assert len(encoded) < 10000
    restored = rewind_extension_state(builder.extension_state, history_end=0, current_history_end=0)
    assert extension_values(restored) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["retained_tail", "whole_prefix", "before_first_mutation"])
async def test_compaction_rebases_private_boundaries(mode):
    builder = ContextBuilder()
    if mode == "before_first_mutation":
        builder.append_user("old")
        builder.append_assistant("answer")
        builder.extension_state = {"entries": [{"id": "legacy"}]}
        extension = actions(builder)
        builder._install_compacted_history([LLMMessage(role="user", content="summary")], removed_prefix=2, recent_count=0, restore_state=None)
        extension.append_entry("new", 1)
        builder.append_user("new question")
        assert builder.fork_from(0).extension_state["entries"] == [{"id": "legacy"}]
        return
    expected, _ = await two_turns(builder)
    if mode == "retained_tail":
        builder._install_compacted_history([LLMMessage(role="user", content="summary"), *builder._history[2:]],
                                          removed_prefix=2, recent_count=2, restore_state=None)
        assert extension_values(builder.fork_from(0).extension_state) == extension_values(expected)
        assert all(item["history_end"] <= 3 for item in builder.extension_state[REWIND_KEY]["changes"])
    else:
        builder._install_compacted_history([LLMMessage(role="user", content="summary")], removed_prefix=4, recent_count=0, restore_state=None)
        assert builder.extension_state[REWIND_KEY] == {"floor": 1, "changes": []}
        assert extension_values(builder.fork_from(0).extension_state) == extension_values(builder.extension_state)


@pytest.mark.asyncio
async def test_bounded_snapshot_and_child_inheritance_keep_rebased_state():
    builder = ContextBuilder()
    expected, _ = await two_turns(builder)
    tail = builder.export_snapshot(max_messages=2)
    restored = ContextBuilder()
    restored.load_snapshot(tail)
    assert extension_values(rewind_extension_state(restored.extension_state, history_end=0, current_history_end=2)) == extension_values(expected)
    child_snapshot = _fork_snapshot_for_child({"_context_builder": builder}, "1")
    child = ContextBuilder()
    child.load_snapshot(child_snapshot)
    assert child.history_length == 1
    assert extension_values(child.fork_from(-1).extension_state) == extension_values(builder.extension_state)
    assert child.extension_cursor == {}


def test_legacy_private_state_never_silently_follows_a_historical_fork():
    builder = ContextBuilder()
    builder.append_user("first")
    builder.append_assistant("first answer")
    builder.append_user("later")
    builder.extension_state = {"labels": {"entry": "unknown old value"}}
    with pytest.raises(ValueError, match="was not recorded"):
        builder.fork_from(1)
    assert builder.fork_from(-1).extension_state == builder.extension_state


@pytest.mark.asyncio
async def test_two_real_queries_cold_journal_and_next_branch_request_use_selected_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    captured = {}
    async def behavior(model, messages):
        return None
    fixture = setup(tmp_path, monkeypatch, behavior)
    def factory(api):
        def before(event, ctx):
            if "entry" not in captured:
                captured["entry"] = api.append_entry("first", {"payload": "private first"})
                api.set_label(captured["entry"], "original")
            elif captured.get("branch"):
                assert ctx.session_manager.get_label(captured["entry"]) == "original"
                assert len(ctx.session_manager.get_entries()) == 1
                captured["branch_observed"] = True
            else:
                api.append_entry("future", {"payload": "private future"})
                api.set_label(captured["entry"], "future label")
        api.on("before_agent_start", before)
    runner = await bind(fixture, tmp_path, factory)
    fixture.session.lifecycle_observer_factory = lifecycle_observer_factory
    async def query(builder, journal, text):
        fixture.session.context_builder = builder
        owner = replace(fixture.owner, execution_journal=journal, lifecycle_runtime=runner)
        state = AgentState(user_message=text, conversation_id="model-conv", workspace_root=tmp_path)
        events = [event async for event in QueryEngine().submit(QuerySubmission(
            session=fixture.session, state=state, user_message=text,
            runtime=AgentLoopSessionContext(session_id="boundary", workspace_root=tmp_path, run_context=owner)))]
        assert state.terminal_status == "completed", events
    try:
        journal = fixture.owner.execution_journal
        await query(fixture.builder, journal, "first")
        first_end = fixture.builder.history_length
        expected = deepcopy(fixture.builder.extension_state)
        await query(fixture.builder, journal, "second")
        cold = ExecutionJournal(journal.agent_id, base_dir=journal.base_dir)
        restored = ContextBuilder(llm=fixture.model, token_budget=fixture.config.token_budget)
        restored.load_snapshot(cold.reconstruct_context_snapshot())
        branch = restored.fork_from(first_end - 1)
        assert branch.extension_state == expected
        captured["branch"] = True
        await query(branch, ExecutionJournal("branch", base_dir=tmp_path / "journal"), "branch continuation")
        assert captured["branch_observed"]
        assert len(fixture.builder.extension_state["entries"]) == 2
    finally:
        await fixture.session.aclose()
        fixture.runtime.close(release_lease=True)
        runner.invalidate()
