from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.message import AgentEvent
from backend.agent.query_terminal import QueryTerminalTransaction
from backend.conversations.repository import ConversationRepository
from backend.llm.base import LLMMessage, ToolCallEvent, UsageInfo, estimate_llm_context_tokens
from backend.ws.event_log import WebSocketReplayEventStore
from backend.ws.event_outbox import EventOutbox


@pytest.mark.parametrize("text", ["x" * 80_000, "中文约束" * 10_000], ids=["ascii", "cjk"])
def test_history_admission_and_request_account_for_full_tool_arguments(text):
    builder = ContextBuilder()
    message = LLMMessage(role="assistant", content="", tool_calls=[
        ToolCallEvent(id="patch", name="apply_patch", arguments={"patch": text})])
    builder._history_store.append(message)
    expected = estimate_llm_context_tokens([message])
    assert expected >= len(text.encode("utf-8")) // 4
    assert builder._history_tokens_total == expected
    assert builder.get_budget_snapshot(AgentState(user_message=""), messages=[message])["used"] == expected


def test_actual_input_usage_adds_subsequent_items_and_resets_on_model_change():
    builder = ContextBuilder()
    original = LLMMessage(role="user", content="request")
    builder._history_store.append(original)
    builder.begin_provider_request([original], [])
    # Tools can commit while the response is still streaming, before usage arrives.
    result = LLMMessage(role="tool", content="new output " * 100, tool_call_id="read")
    builder._history_store.append(result)
    builder.record_actual_usage(UsageInfo(input_tokens=10_000))
    estimated_delta = estimate_llm_context_tokens([result])
    snapshot = builder.get_budget_snapshot(AgentState(user_message=""), messages=[original, result])
    assert snapshot["used"] == 10_000 + estimated_delta
    assert builder.token_usage >= 10_000 + estimated_delta
    builder.bind_llm(object())
    assert builder.get_budget_snapshot(AgentState(user_message=""), messages=[original, result])["used"] == estimate_llm_context_tokens([original, result])


def test_native_items_use_the_same_history_and_request_estimate():
    builder = ContextBuilder()
    message = LLMMessage(role="assistant", content="", provider_items=[
        {"type": "reasoning", "encrypted_content": "x" * 80_000}])
    builder._history_store.append(message)
    assert builder._history_tokens_total == estimate_llm_context_tokens([message])
    assert builder._history_tokens_total > 20_000


def test_other_conversation_reads_do_not_wait_for_generation_write(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path)
    first, second = repository.create_conversation(), repository.create_conversation()
    entered, release = threading.Event(), threading.Event()
    original = repository._write_generation

    def held_write(record, generation):
        entered.set()
        assert release.wait(5)
        original(record, generation)

    monkeypatch.setattr(repository, "_write_generation", held_write)
    with ThreadPoolExecutor(3) as executor:
        writer = executor.submit(repository.commit_turn_projection, first.id,
                                 assistant_message={"id": "a", "role": "assistant", "content": "done"})
        assert entered.wait(5)
        try:
            unrelated = executor.submit(repository.get_conversation_summary, second.id)
            assert unrelated.result(timeout=1).id == second.id
            same = executor.submit(repository.get_conversation_summary, first.id)
            assert not same.done()
        finally:
            release.set()
        writer.result()
        assert same.result().message_count == 1


def test_fast_projection_retains_owned_history_and_recovers_terminal_overlay(tmp_path):
    repository = ConversationRepository(tmp_path)
    record = repository.create_conversation(transcript=[{"id": "u", "role": "user", "content": "task"}],
                                            context_snapshot={"history": [{"role": "user", "content": "task"}]})
    base = repository.get_projection_context(record.id)
    original_message = repository._record_cache[record.id].transcript[0]
    original_generation = repository._read_manifest(record.id)["current_generation"]
    summary = repository.commit_turn_projection(record.id,
        assistant_message={"id": "a", "role": "assistant", "content": "finished", "terminal_status": "completed"},
        context_delta={"set": {}, "removed": [], "history_from": 1,
                       "history": [{"role": "assistant", "content": "finished"}]},
        expected_revision=base[0], return_record=False)
    assert summary.message_count == 2
    assert repository._record_cache[record.id].transcript[0] is original_message
    assert len(base[1]["history"]) == 1
    assert repository._read_manifest(record.id)["current_generation"] == original_generation
    restored = ConversationRepository(tmp_path).get_conversation(record.id)
    assert restored.transcript[-1]["content"] == "finished"
    assert restored.context_snapshot["history"][-1]["content"] == "finished"


@pytest.mark.asyncio
async def test_replay_window_appends_between_batched_trims(tmp_path, monkeypatch):
    store = WebSocketReplayEventStore(session_id="audit", root_dir=tmp_path)
    outbox = EventOutbox(session_id="audit", websocket=None, replay_root=tmp_path, replay_limit=5,
                         cleanup_tasks=set(), has_active_run=lambda: False,
                         requires_conversation_owner=lambda *_: False, workspace_scoped_event_types=(),
                         replay_state=(store, []))
    rewrites = []
    original = store.rewrite

    def record_rewrite(events):
        rewrites.append(len(events))
        original(events)

    monkeypatch.setattr(store, "rewrite", record_rewrite)
    for seq in range(1, 21):
        item, window = outbox._stage({"type": "agent_message_delta", "conversation_id": "conv_audit00",
                                     "seq": seq, "content": "delta"})
        await outbox._persist_batch([item], window)
    assert rewrites == [5, 5, 5, 5]
    assert [event["seq"] for event in store.load(limit=5)] == list(range(16, 21))
    replay, gap = outbox.replay_window_after(17)
    assert not gap
    assert [event["seq"] for event in replay] == [18, 19, 20]


@pytest.mark.asyncio
async def test_cancelling_terminal_publication_finishes_the_same_transaction():
    entered, release = threading.Event(), threading.Event()
    recorded = []

    def intent(event):
        recorded.append(("intent", event.data["status"]))
        entered.set()
        assert release.wait(5)

    transaction = QueryTerminalTransaction(
        turn_ctx=SimpleNamespace(state=AgentState(user_message="task", reply="done"), turn_kernel=None, metadata={}),
        journal=SimpleNamespace(record_terminal_intent=intent,
                                record_terminal=lambda event: recorded.append(("terminal", event.data["status"]))),
    )
    task = asyncio.create_task(transaction.commit(AgentEvent.done(status="completed"), validate=False))
    assert await asyncio.to_thread(entered.wait, 2)
    try:
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    result = await task
    assert result.status == "completed"
    assert transaction.finalized
    assert recorded == [("intent", "completed"), ("terminal", "completed")]
