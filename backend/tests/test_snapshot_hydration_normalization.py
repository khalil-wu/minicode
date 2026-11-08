from copy import deepcopy
import asyncio
import threading
from unittest.mock import Mock

import pytest

import backend.agent.context as module
from backend.agent.context import ContextBuilder
from backend.llm.base import estimate_llm_message_tokens
from backend.conversations.repository import ConversationRepository
from backend.ws.conversation_runtime import ConversationRuntime


def history_snapshot():
    return {"history": [
        {"role": "system", "content": "outdated system instructions"},
        {"role": "developer", "content": "outdated prefix instructions"},
        {"role": "user", "content": "Original request 中文", "images": [{"media_type": "image/png", "data": "aGVsbG8="}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "call", "name": "inspect", "arguments": {"nested": ["source"]}}],
            "provider_items": [{"type": "reasoning", "encrypted_content": "opaque+/==", "summary": [{"type": "summary_text", "text": "kept"}]}]},
        {"role": "tool", "tool_call_id": "call", "name": "inspect", "content": "Actual result"},
        {"role": "developer", "content": "Retain this in-turn instruction\r\n", "input_origin": "agent"},
        {"role": "user", "content": "Continue", "documents": [{"media_type": "application/pdf", "file_name": "a.pdf", "data": "cGRm"}],
            "attachment_refs": [{"artifact_id": "attachment-1", "file_name": "a.pdf", "kind": "document"}]},
        {"role": "assistant", "content": "Done"},
    ], "turn_admissions": {"accepted": {"history_start": 2, "history_end": 3}},
        "guardian_evidence": ContextBuilder().guardian_evidence.checkpoint()}


@pytest.mark.parametrize("recent_count", [0, 1, 2, 3, 4, 8])
def test_partial_snapshot_keeps_developer_items_and_absolute_timestamps(recent_count):
    source = history_snapshot()
    original = deepcopy(source)
    full = ContextBuilder()
    full.load_snapshot(source)
    partial = ContextBuilder()
    pending = partial.load_snapshot_partial(source, recent_history_count=recent_count)
    partial.prepend_history_messages(ContextBuilder._deserialize_normalized_history(pending))
    partial_snapshot, full_snapshot = partial.export_snapshot(), full.export_snapshot()
    # Each restored owner deliberately starts a fresh Guardian review revision.
    assert partial_snapshot["guardian_evidence"].pop("review_context_revision") > source["guardian_evidence"]["review_context_revision"]
    assert full_snapshot["guardian_evidence"].pop("review_context_revision") > source["guardian_evidence"]["review_context_revision"]
    assert partial_snapshot == full_snapshot
    assert [message.timestamp_ms for message in partial._history] == list(range(1, 7))
    assert [(message.role, message.content) for message in partial._history][3] == ("developer", "Retain this in-turn instruction\r\n")
    assert partial._history[0].is_user_input
    assert source == original
    assert [message.to_openai_message() for message in partial._history] == [message.to_openai_message() for message in full._history]


@pytest.mark.parametrize("partial", [False, True])
def test_snapshot_hydration_normalizes_provider_items_once_and_owns_input(partial, monkeypatch):
    source = history_snapshot()
    sanitize = Mock(wraps=module._sanitize_provider_items)
    monkeypatch.setattr(module, "_sanitize_provider_items", sanitize)
    builder = ContextBuilder()
    if partial:
        pending = builder.load_snapshot_partial(source, recent_history_count=3)
        builder.prepend_history_messages(ContextBuilder._deserialize_normalized_history(pending))
    else:
        builder.load_snapshot(source)
    assert sanitize.call_count == 6
    source["history"][2]["images"][0]["data"] = "changed"
    source["history"][3]["tool_calls"][0]["arguments"]["nested"].append("caller changed")
    source["history"][3]["provider_items"][0]["summary"][0]["text"] = "changed"
    source["history"][6]["documents"][0]["data"] = "changed"
    assert builder._history[0].images[0]["data"] == "aGVsbG8="
    assert builder._history[1].tool_calls[0].arguments["nested"] == ["source"]
    assert builder._history[1].provider_items[0]["summary"][0]["text"] == "kept"
    assert builder._history[4].documents[0]["data"] == "cGRm"


@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("restore_skill", [False, True])
def test_hydration_accounts_each_installed_history_once_and_recounts_inserted_skills(partial, restore_skill):
    source = history_snapshot()
    if restore_skill:
        source["invoked_skills"] = [{"name": "retained", "path": "/skills/retained/SKILL.md", "content": "Required skill instructions"}]
    builder = ContextBuilder()
    estimator = Mock(wraps=estimate_llm_message_tokens)
    builder._history_store._estimator = estimator
    if partial:
        pending = builder.load_snapshot_partial(source, recent_history_count=3)
        builder.prepend_history_messages(ContextBuilder._deserialize_normalized_history(pending))
    else:
        builder.load_snapshot(source)
    # Prefix preparation reuses the installed suffix's accounting. Only a
    # real inserted skill requires rebuilding the affected history.
    assert estimator.call_count == 6 + (7 if restore_skill else 0)
    expected = ContextBuilder()
    expected.load_snapshot(builder.export_snapshot())
    assert builder.context_ledger() == expected.context_ledger()
    assert builder._history_tokens_total == expected._history_tokens_total
    if restore_skill:
        assert "Required skill instructions" in builder._history[0].content


@pytest.mark.asyncio
async def test_real_background_hydration_keeps_the_developer_at_the_recent_suffix_start(tmp_path):
    history = [{"role": "user" if index % 2 == 0 else "assistant", "content": f"history {index}"} for index in range(24)]
    history[4] = {"role": "developer", "content": "Keep this authoritative in-turn instruction"}
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot={"history": history})
    builder = ContextBuilder()
    runtime = ConversationRuntime(conversation_repo=repo, context_builder=builder, build_summary_from_transcript=lambda *_: "")
    runtime.active_conversation_id = record.id
    try:
        assert runtime.load_active_conversation_snapshot(record.id, record.context_snapshot, snapshot_revision=record.revision)
        assert builder._history[0].role == "developer"
        await runtime.wait_for_hydration(record.id)
        assert [(message.role, message.content) for message in builder._history] == [(message["role"], message["content"]) for message in history]
        assert [message.timestamp_ms for message in builder._history] == list(range(1, 25))
        assert builder._history_frozen_count == 24
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["unchanged", "replacement", "switch"])
async def test_repository_normalization_yields_foreground_and_only_publishes_its_current_snapshot(tmp_path, monkeypatch, change):
    repo = ConversationRepository(tmp_path)
    source = history_snapshot()
    source["history"].extend({"role": "assistant", "content": f"retained {index}"} for index in range(40))
    record = repo.create_conversation(context_snapshot=source)
    other = repo.create_conversation(context_snapshot={"history": [{"role": "user", "content": "Different conversation"}]})
    builder = ContextBuilder()
    runtime = ConversationRuntime(conversation_repo=repo, context_builder=builder, build_summary_from_transcript=lambda *_: "")
    runtime.active_conversation_id = record.id
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    normalizer = ContextBuilder._snapshot_history_with_admissions
    normalizations = []
    def held(snapshot):
        assert threading.get_ident() != foreground_thread
        normalizations.append(snapshot)
        if len(normalizations) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5)
        return normalizer(snapshot)
    foreground_thread = threading.get_ident()
    monkeypatch.setattr(ContextBuilder, "_snapshot_history_with_admissions", staticmethod(held))
    try:
        runtime.defer_repository_hydration(record.id, on_hydration_complete=None)
        runtime.start_hydration(record.id)
        await asyncio.wait_for(entered.wait(), 2)
        assert not runtime._projection_lock_for(record.id).locked()
        tick = asyncio.Event()
        loop.call_soon(tick.set)
        await asyncio.wait_for(tick.wait(), .5)
        if change == "replacement":
            repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "Replaced during restore"}]})
        elif change == "switch":
            runtime.active_conversation_id = other.id
            runtime.defer_repository_hydration(other.id, on_hydration_complete=None)
            runtime.start_hydration(other.id)
        release.set()
        await runtime.wait_for_hydration(runtime.active_conversation_id)
        if change == "unchanged":
            assert len(normalizations) == 1
            monkeypatch.setattr(ContextBuilder, "_snapshot_history_with_admissions", staticmethod(normalizer))
            expected = ContextBuilder()
            expected.load_snapshot(source)
            assert [message.to_openai_message() for message in builder._history] == [message.to_openai_message() for message in expected._history]
            assert builder._history[0].is_user_input
        else:
            assert [message.content for message in builder._history] == ["Replaced during restore" if change == "replacement" else "Different conversation"]
    finally:
        release.set()
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["none", "append", "replacement", "switch", "shutdown"])
async def test_prefix_accounting_runs_off_loop_and_keeps_live_suffix_ownership(tmp_path, monkeypatch, change):
    from backend.llm.base import LLMMessage

    source = history_snapshot()
    source["history"].extend({"role": "assistant", "content": f"retained-{index}"} for index in range(1000))
    repo = ConversationRepository(tmp_path)
    record = repo.create_conversation(context_snapshot=source)
    other = repo.create_conversation(context_snapshot={"history": [{"role": "user", "content": "Other owner"}]})
    builder = ContextBuilder()
    runtime = ConversationRuntime(conversation_repo=repo, context_builder=builder, build_summary_from_transcript=lambda *_: "")
    runtime.active_conversation_id = record.id
    foreground = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    estimates = []
    held_once = False
    def estimate(message, raw):
        nonlocal held_once
        worker = threading.get_ident() != foreground
        if worker and not held_once:
            held_once = True
            entered.set()
            assert release.wait(5)
        estimates.append((message.content, worker))
        return estimate_llm_message_tokens(message, raw)
    builder._history_store._estimator = estimate
    try:
        runtime.load_active_conversation_snapshot(record.id, record.context_snapshot, snapshot_revision=record.revision)
        assert await asyncio.to_thread(entered.wait, 3)
        assert len(builder._history) == 20
        tail = tuple(builder._history)
        before = builder._history_store.revision
        if change == "append":
            builder._history_store.append(LLMMessage("user", "Arrived during hydration", timestamp_ms=2000))
        elif change == "replacement":
            repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "Replaced owner"}]})
        elif change == "switch":
            runtime.active_conversation_id = other.id
            runtime.defer_repository_hydration(other.id, on_hydration_complete=None)
            runtime.start_hydration(other.id)
        elif change == "shutdown":
            await runtime.shutdown()
        release.set()
        await runtime.wait_for_hydration(runtime.active_conversation_id)
        if change in {"none", "append"}:
            assert sum(not worker for _, worker in estimates) == 20 + int(change == "append")
            assert len(estimates) == 1006 + int(change == "append")
            assert builder._history_store.changed_since(before) == 0
            start = -21 if change == "append" else -20
            assert all(builder._history[start + index] is message for index, message in enumerate(tail))
            expected = ContextBuilder()
            expected.load_snapshot(builder.export_snapshot())
            assert builder.context_ledger() == expected.context_ledger()
            assert builder._history_tokens_total == expected._history_tokens_total
            assert builder._history_store.token_estimates == expected._history_store.token_estimates
        elif change == "shutdown":
            assert tuple(builder._history) == tail
        else:
            assert [message.content for message in builder._history] == ["Replaced owner" if change == "replacement" else "Other owner"]
    finally:
        release.set()
        await runtime.shutdown()
