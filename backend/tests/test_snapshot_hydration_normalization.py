from copy import deepcopy
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
    # Initial recent/full installation, optional complete prefix installation,
    # and only a real inserted skill require token accounting.
    assert estimator.call_count == (9 if partial else 6) + (7 if restore_skill else 0)
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
