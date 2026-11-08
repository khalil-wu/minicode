from __future__ import annotations

from copy import deepcopy
import json

import pytest

from backend.agent.checkpoint import load_latest_checkpoint, save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.state import AgentState
from backend.config import LLMSettings
from backend.conversations.context_delta import apply_context_snapshot_delta
from backend.conversations.repository import ConversationRepository
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter, _openai_chat_messages


def populated_context():
    builder = ContextBuilder()
    builder._history_store.append(LLMMessage("user", "Same request 中文\r\n", timestamp_ms=1))
    builder._history_store.append(LLMMessage("assistant", "", timestamp_ms=2, phase="commentary",
        tool_calls=[ToolCallEvent("read", "read_file", {"file_path": "a.py", "empty": [], "enabled": False},
            native_namespace="functions", native_name="read_file", encrypted_function_args=[])],
        provider_items=[{"type": "reasoning", "encrypted_content": "opaque+/==", "summary": []}]))
    builder._history_store.append(LLMMessage("tool", "fallback text must not replace native empty output", timestamp_ms=3,
        name="read_file", tool_call_id="read", is_error=True, mcp_output=[], code_mode_output=[], tool_search_output=[],
        history_notes_output="", native_tool_output="", private_model_only=True))
    builder._history_store.append(LLMMessage("developer", "Retained in-turn instruction", timestamp_ms=4, content_kind="agent.role"))
    builder._history_store.append(LLMMessage("user", "Same request 中文\r\n", timestamp_ms=5,
        images=[{"media_type": "image/png", "data": "aW1hZ2U=", "detail": "original"}],
        documents=[{"media_type": "application/pdf", "data": "cGRm", "file_name": "proof.pdf"}],
        is_user_input=True, input_origin="user", runtime_context="Owned runtime instructions", accepted_input={}))
    builder._history_store.append(LLMMessage("assistant", "Actual final answer", timestamp_ms=6,
        phase="final_answer", answer_source="retained"))
    builder._history_frozen_count = len(builder._history)
    return builder


@pytest.mark.asyncio
async def test_sparse_and_legacy_snapshots_restore_identical_provider_inputs():
    original = populated_context()
    sparse = original.export_snapshot()
    legacy = deepcopy(sparse)
    defaults = {"name": None, "tool_call_id": None, "is_error": False, "phase": "", "input_origin": "",
        "answer_source": "", "runtime_context": "", "provider_items": [], "attachment_refs": [],
        "tool_calls": [], "images": [], "documents": []}
    for item in legacy["history"]:
        for key, value in defaults.items():
            item.setdefault(key, deepcopy(value))
    assert len(json.dumps(sparse)) < len(json.dumps(legacy))
    assert set(sparse["history"][0]) == {"role", "content", "timestamp_ms"}
    native = sparse["history"][2]
    assert native["mcp_output"] == native["code_mode_output"] == native["tool_search_output"] == []
    assert native["history_notes_output"] == native["native_tool_output"] == ""
    assert sparse["history"][4]["accepted_input"] == {}
    assert sparse["history"][1]["provider_items"][0]["summary"] == []
    adapter = OpenAIAdapter(LLMSettings(api_key="", provider="openai", wire_api="responses"))
    anthropic = object.__new__(AnthropicAdapter)
    try:
        expected = (adapter._build_responses_input(original._history), _openai_chat_messages(original._history),
            anthropic._convert_messages(original._history))
        for snapshot in (sparse, legacy):
            for partial in (False, True):
                restored = ContextBuilder()
                if partial:
                    prefix = restored.load_snapshot_partial(snapshot, recent_history_count=2)
                    restored.prepend_history_messages(ContextBuilder._deserialize_normalized_history(prefix))
                else:
                    restored.load_snapshot(snapshot)
                assert restored._history == original._history
                assert restored._history_frozen_count == 6
                assert restored.export_snapshot()["history"] == sparse["history"]
                assert (adapter._build_responses_input(restored._history), _openai_chat_messages(restored._history),
                    anthropic._convert_messages(restored._history)) == expected
    finally:
        await adapter.aclose()


def test_sparse_history_survives_checkpoint_repository_journal_and_delta_recovery(tmp_path):
    builder = populated_context()
    snapshot = builder.export_snapshot()
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(context_snapshot=snapshot)
    cold_record = ConversationRepository(tmp_path / "conversations").get_conversation(record.id)
    assert cold_record.context_snapshot["history"] == snapshot["history"]
    save_checkpoint(session_id="sparse", conversation_id=record.id, user_message="continue", iterations=1, reply="",
        messages=snapshot["history"], context_snapshot=snapshot, tool_calls=[], active_skills=[], disabled_tools=set(),
        stopped_reason="interrupted", last_mutation_index=0, base_dir=tmp_path / "checkpoints")
    checkpoint = load_latest_checkpoint("sparse", conversation_id=record.id, base_dir=tmp_path / "checkpoints")
    assert checkpoint.messages == snapshot["history"]
    journal = ExecutionJournal("sparse", base_dir=tmp_path / "journals")
    owner = QueryJournalRecorder(journal, {"run_id": "turn", "turn_id": "turn"}, AgentState(user_message="continue"),
        builder, None, record.id)
    owner.record_turn_started()
    base, revision = owner.projection_base_snapshot, owner.projection_history_revision
    builder.append_user("Next actual input")
    delta, _ = builder.export_snapshot_delta(base, since_revision=revision)
    assert delta["history_from"] == 6 and len(delta["history"]) == 1
    assert apply_context_snapshot_delta(base, delta)["history"] == builder.export_snapshot()["history"]
    owner.record_terminal_intent(AgentEvent.done(status="cancelled", reason="interrupted"))
    cold_journal = ExecutionJournal("sparse", base_dir=tmp_path / "journals")
    assert cold_journal.reconstruct_history() == builder.export_snapshot()["history"]
    recovered = ContextBuilder()
    recovered.load_snapshot(cold_journal.reconstruct_context_snapshot())
    assert recovered._history == builder._history


def test_sparse_media_reference_keeps_identity_ownership_and_atomic_tool_groups():
    builder = ContextBuilder()
    builder._history_store.append(LLMMessage("user", "pixels", timestamp_ms=1,
        images=[{"media_type": "image/png", "data": "owned-by-store", "artifact_id": "stored"},
            {"media_type": "image/png", "data": "inline"}],
        attachment_refs=[{"artifact_id": "stored", "kind": "image", "file_name": "stored.png"}]))
    builder._history_store.append(LLMMessage("assistant", "", timestamp_ms=2,
        tool_calls=[ToolCallEvent("call", "inspect", {"nested": [1, 2]})]))
    builder._history_store.append(LLMMessage("tool", "actual result", timestamp_ms=3, name="inspect", tool_call_id="call"))
    snapshot = builder.export_snapshot()
    assert snapshot["history"][0]["images"] == [{"media_type": "image/png", "artifact_id": "stored"},
        {"media_type": "image/png", "data": "inline"}]
    tail = builder.export_snapshot(max_messages=1)
    assert [item["role"] for item in tail["history"]] == ["assistant", "tool"]
    snapshot["history"][0]["images"][1]["data"] = "mutated"
    snapshot["history"][0]["attachment_refs"][0]["file_name"] = "changed.png"
    snapshot["history"][1]["tool_calls"][0]["arguments"]["nested"].append(3)
    assert builder._history[0].images[1]["data"] == "inline"
    assert builder._history[0].attachment_refs[0]["file_name"] == "stored.png"
    assert builder._history[1].tool_calls[0].arguments == {"nested": [1, 2]}
