from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import threading

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.history_store import ConversationHistory
from backend.agent.run_context import RunContext
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.tests.test_user_input_presentation_chain import session_for
from backend.ws.manager import WebSocketManager
from backend.ws.model_prewarm import prepare_thread_model_context, release_prepared_model_context


def test_history_clone_preserves_nested_payload_aliases_subclasses_and_accounting():
    @dataclass
    class SpecializedMessage(LLMMessage):
        extra: list | None = None

    shared = {"nested": ["original"]}
    tool = ToolCallEvent("call", "read_file", {"options": shared}, encrypted_function_args=["opaque"])
    message = LLMMessage("assistant", "result", tool_calls=[tool],
        provider_items=[{"type": "reasoning", "payload": shared}],
        images=[{"data": "exact-image", "media_type": "image/png"}],
        documents=[{"data": "exact-pdf", "media_type": "application/pdf"}],
        accepted_input={"parts": ["actual input"]})
    builder = ContextBuilder()
    builder._history_store.append(message)
    builder._history_store.append(SpecializedMessage("user", "special", extra=[shared]))
    cloned = builder._history_store.clone()
    expected = deepcopy(builder._history)
    assert cloned.messages == expected
    assert cloned.tokens_total == builder._history_store.tokens_total
    assert cloned.ledger_categories() == builder._history_store.ledger_categories()
    copied = cloned.messages[0]
    assert copied.provider_items[0]["payload"] is copied.tool_calls[0].arguments["options"]
    assert cloned.messages[1].extra[0] is copied.provider_items[0]["payload"]
    copied.tool_calls[0].arguments["options"]["nested"].append("changed")
    copied.images[0]["data"] = "changed image"
    copied.documents[0]["data"] = "changed document"
    copied.accepted_input["parts"].append("changed input")
    assert message.tool_calls[0].arguments["options"] == {"nested": ["original"]}
    assert message.images[0]["data"] == "exact-image"
    assert message.documents[0]["data"] == "exact-pdf"
    assert message.accepted_input == {"parts": ["actual input"]}


def test_history_clone_keeps_graph_identity_for_repeated_messages_and_cycles():
    history = ConversationHistory()
    message = LLMMessage("user", "input")
    message.extra = {"message": message, "fields": vars(message), "history": history.messages}
    history.messages.extend([message, message])
    cloned = history.clone()
    first = cloned.messages[0]
    assert first is cloned.messages[1] and first is not message
    assert first.extra["message"] is first
    assert first.extra["fields"] is vars(first)
    assert first.extra["history"] is cloned.messages


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [4, 64])
async def test_first_real_turn_reuses_completed_reader_parsing_and_token_accounting(tmp_path, monkeypatch, count):
    session, record, adapter = session_for(tmp_path, monkeypatch)
    manager = WebSocketManager()
    session.ws_manager = manager
    snapshot = {"history": [{"role": "user" if index % 2 == 0 else "assistant",
        "content": f"reader history {index}"} for index in range(count)], "history_frozen_count": count}
    saved = session.conversation_repo.save_context_snapshot(record.id, snapshot)
    session.load_active_conversation_snapshot(record.id, saved.context_snapshot, snapshot_revision=saved.revision)
    await session.conversation_runtime.wait_for_hydration(record.id)
    original = session.context_builder.export_snapshot()
    reader = session.context_builder._history_store

    def no_second_decode(*args, **kwargs):
        raise AssertionError("Execution decoded the already prepared reader history again")

    monkeypatch.setattr(ContextBuilder, "_deserialize_normalized_history", no_second_decode)
    try:
        await prepare_thread_model_context(session, record.id, Path(record.execution_cwd), session.config)
        thread = manager.execution_contexts.thread_runtime_for(session.conversation_repo.store_instance_id(), record.id)
        prepared = thread.model_context[0]
        assert prepared._history_store is not reader
        assert prepared.export_snapshot()["history"] == original["history"]
        assert prepared._history_store.token_estimates == reader.token_estimates
        assert prepared._history_frozen_count == count
        # A reader edit after handoff cannot enter the actual provider turn.
        session.context_builder.append_user("reader edit must stay private")
        await asyncio.create_task(session._run_agent("first real input", conversation_id=record.id,
            run_context=RunContext(environment_thread_runtime=thread),
            metadata={"user_message_id": "first-user", "assistant_message_id": "first-answer"}))
        await asyncio.gather(*tuple(session.run_manager.lifecycle_cleanup_tasks_for(record.id)))
        assert len(adapter.requests) == 1
        text = "\n".join(str(message.content) for message in adapter.requests[0])
        assert "reader history 0" in text and f"reader history {count - 1}" in text
        assert "reader edit must stay private" not in text
    finally:
        release_prepared_model_context(session)
        session.ws_manager = None
        await session.session_lifecycle.shutdown(reason="shared-history-preparation")
        for thread in manager.execution_contexts.thread_runtimes():
            await thread.close_environments()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["history", "reader", "owner", "ui"])
async def test_prepared_history_reuse_keeps_owner_and_durable_snapshot_boundaries(tmp_path, monkeypatch, change):
    session, record, _ = session_for(tmp_path, monkeypatch)
    saved = session.conversation_repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "original"}]})
    session.load_active_conversation_snapshot(record.id, saved.context_snapshot, snapshot_revision=saved.revision)
    snapshot = deepcopy(saved.context_snapshot)
    owner = record.id
    if change == "history":
        snapshot["history"][0]["content"] = "external update"
    elif change == "reader":
        session.context_builder.append_user("reader update")
    elif change == "owner":
        owner = "different-owner"
    else:
        snapshot["ui_agent_state"] = {"subagents": []}
        snapshot["_ui_agent_state_revision"] = 9
    try:
        copied = session.conversation_runtime.copy_prepared_history(owner, snapshot)
        assert (copied is not None) == (change == "ui")
        if copied is not None:
            assert [message.content for message in copied.messages] == ["original"]
    finally:
        await session.session_lifecycle.shutdown(reason="prepared-history-match")


@pytest.mark.asyncio
async def test_navigation_during_prepared_copy_cannot_publish_mixed_history(tmp_path, monkeypatch):
    session, record, _ = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    saved = repo.save_context_snapshot(record.id, {"history": [{"role": "user", "content": "original"}]})
    session.load_active_conversation_snapshot(record.id, saved.context_snapshot, snapshot_revision=saved.revision)
    entered, release = threading.Event(), threading.Event()
    original = ConversationHistory.clone

    def held(history):
        entered.set()
        assert release.wait(5)
        return original(history)

    monkeypatch.setattr(ConversationHistory, "clone", held)
    copying = asyncio.create_task(asyncio.to_thread(session.conversation_runtime.copy_prepared_history, record.id, saved.context_snapshot))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        other = repo.create_conversation(context_snapshot={"history": [{"role": "user", "content": "other"}]})
        session.active_conversation_id = other.id
        session.load_active_conversation_snapshot(other.id, other.context_snapshot, snapshot_revision=other.revision)
        release.set()
        assert await copying is None
        assert session.context_builder.export_snapshot()["history"][0]["content"] == "other"
    finally:
        release.set()
        await copying
        await session.session_lifecycle.shutdown(reason="prepared-history-navigation")
