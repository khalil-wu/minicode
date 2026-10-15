from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from backend.agent.context import ContextBuilder
from backend.agent.context_window import ContextWindowConfig, is_context_window_rollover
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import UserCommand
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.state import AgentState
from backend.agent.turn_state import AgentTurnState
from backend.conversations.public_projection import project_public_transcript_message
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.services.subagent_service import build_subagent_transcript_messages, project_subagent_transcript_events
from backend.tests.test_codex_context_window import WindowModel, run_window, window_builder
from backend.tests.test_context_fork_activation_chain import _connected_fork_session
from backend.tests.test_ws_cold_connection import _release_session
from backend.tools.context_window_tools import GetContextRemainingTool, NewContextTool
from backend.tools.file_tools import ReadFileTool
from backend.tools.registry import ToolRegistry
from backend.ws.agent_runner import _commit_automatic_compaction, _project_agent_message_event
from backend.ws.compaction_coordinator import compact_conversation
from backend.ws.stream_state import apply_stream_event, create_stream_state, get_stream_content_blocks


async def release_window_session(session, conversation_id):
    await _release_session(session)
    # Memory startup is a real leased background operation. Closing the
    # fixture's event loop cannot discard a borrower still pruning its store.
    from backend.memory.generation import _BACKGROUND_TASKS
    owned = [task for task in _BACKGROUND_TASKS if task.get_name() == f"memory-startup:{conversation_id}"]
    if owned:
        await asyncio.gather(*owned)


def test_item_lifecycle_has_one_matching_live_public_and_journal_process(tmp_path):
    async def scenario():
        model = WindowModel([[ToolCallEvent("window-lifecycle", "new_context", {})], "done"])
        _, _, journal, events = await run_window(tmp_path, model)
        lifecycle = [event for event in events if event.type in {"item.started", "item.completed"}
                     and event.data["item"]["type"] == "context_compaction"]
        assert [event.type for event in lifecycle] == ["item.started", "item.completed"]
        started, completed = (event.data["item"] for event in lifecycle)
        assert started["id"] == completed["id"]
        assert started["source"] == completed["source"] == "auto"
        assert started["started_at"] == completed["started_at"] <= completed["finished_at"]
        assert next(event for event in events if event.type == "context_compacted").data["compaction_item_id"] == started["id"]
        stream = create_stream_state(conversation_id="window-conv", message_id="window-answer")
        turn = AgentTurnState(now_ms=lambda: completed["finished_at"])
        for event in lifecycle:
            apply_stream_event({"window-conv": stream}, "window-conv", event.type, event.data)
            _project_agent_message_event(turn, event.type, event.data)
        process = get_stream_content_blocks(stream)[0]
        assert process == turn.finalize(terminal_status="completed").blocks[0]
        public = project_public_transcript_message({"id": "window-answer", "role": "assistant", "content": "", "blocks": [process]})
        assert public["blocks"][0] == process
        reopened = ExecutionJournal("window-test", base_dir=tmp_path / "journals")
        wire_events = [event.to_dict() for event in reopened.read_events()]
        public_events = project_subagent_transcript_events(wire_events)
        messages = build_subagent_transcript_messages({"events": public_events})
        journal_processes = [block for message in messages for block in message["blocks"] if block.get("itemKind") == "context_compaction"]
        assert journal_processes == [process]
        assert not journal.unresolved_tool_uses()
    asyncio.run(scenario())


def test_missing_completion_never_becomes_success_when_turn_ends():
    turn = AgentTurnState(now_ms=lambda: 5_000)
    item = {"id": "interrupted-window", "type": "context_compaction", "source": "auto", "status": "in_progress", "started_at": 1_000}
    _project_agent_message_event(turn, "item.started", {"item": item})
    process = turn.finalize(terminal_status="failed").blocks[0]
    assert process["status"] == "interrupted"
    assert process["startedAt"] == process["timestamp"] == 1_000
    assert process["finishedAt"] == 5_000


def test_repo_automatic_empty_summary_requires_real_window_advance_and_retains_visible_history(tmp_path):
    async def scenario():
        repo = ConversationRepository(tmp_path / "conversations")
        model = WindowModel([])
        builder = window_builder(model, tmp_path)
        builder.append_user("Actual old user")
        builder.append_assistant("Actual old reply")
        transcript = [{"id": "user", "role": "user", "content": "Actual old user"},
                      {"id": "answer", "role": "assistant", "content": "Actual old reply", "tool_calls": [{"id": "read", "name": "read_file", "args": {"file_path": "proof.txt"}, "status": "success", "summary": "Exact read evidence"}]}]
        record = repo.create_conversation(transcript=transcript, context_snapshot=builder.export_snapshot())
        before = deepcopy(record.context_snapshot)
        with pytest.raises(RuntimeError, match="no durable summary or new context window"):
            await _commit_automatic_compaction(repo, conversation_id=record.id, context_builder=builder, summary="")
        assert repo.get_conversation(record.id).context_snapshot == before
        builder.start_new_context_window(AgentState(user_message="Actual old user", workspace_root=tmp_path))
        saved = await _commit_automatic_compaction(repo, conversation_id=record.id, context_builder=builder, summary="")
        assert is_context_window_rollover(before, saved)
        reopened = ConversationRepository(tmp_path / "conversations").get_conversation(record.id)
        assert reopened.transcript == transcript
        assert reopened.compaction_summary == "" and reopened.compaction_state == "compacted"
        assert "Actual old user" not in "\n".join(message["content"] for message in reopened.context_snapshot["history"])
    asyncio.run(scenario())


def test_manual_transaction_commits_standalone_system_process_with_old_transcript(tmp_path):
    async def scenario():
        repo = ConversationRepository(tmp_path / "manual-conversations")
        builder = window_builder(WindowModel([]), tmp_path)
        builder.append_user("old manual user")
        builder.append_assistant("old manual reply")
        history = [{"id": "user", "role": "user", "content": "old manual user"}, {"id": "assistant", "role": "assistant", "content": "old manual reply"}]
        record = repo.create_conversation(transcript=history, context_snapshot=builder.export_snapshot())
        session = SimpleNamespace(conversation_repo=repo, _conversation_projection_lock=lambda _id: asyncio.Lock())
        item = {"id": "real-manual-item", "type": "context_compaction", "source": "manual", "status": "in_progress", "started_at": 1_000}
        committed = await compact_conversation(session, conversation_id=record.id, context_builder=builder,
            restore_state=AgentState(user_message="old manual user", workspace_root=tmp_path), compaction_item=item, operation_turn_id="real-manual-turn")
        assert committed.summary == ""
        restored = ConversationRepository(tmp_path / "manual-conversations").get_conversation(record.id)
        assert restored.transcript[:2] == history
        process_message = project_public_transcript_message(restored.transcript[2])
        assert process_message["role"] == "system" and process_message["turn_id"] == "real-manual-turn"
        assert process_message["id"] == "real-manual-item"
        block = process_message["blocks"][0]
        assert block["itemKind"] == "context_compaction" and block["status"] == "completed"
        assert block["startedAt"] == block["timestamp"] == 1_000
        assert block["finishedAt"] >= block["startedAt"]
    asyncio.run(scenario())


def test_auto_partial_projection_requires_exact_core_receipt_to_finish_same_window(tmp_path):
    async def scenario():
        repo = ConversationRepository(tmp_path / "partial-conversations")
        builder = window_builder(WindowModel([]), tmp_path)
        builder.append_user("real initial user")
        record = repo.create_conversation(context_snapshot=builder.export_snapshot())
        builder.start_new_context_window(AgentState(user_message="old", workspace_root=tmp_path))
        snapshot = builder.export_snapshot()
        repo.save_context_snapshot(record.id, snapshot)
        assert repo.get_conversation(record.id).compaction_state == "clean"
        for receipt in (None, {**snapshot["context_window_state"], "window_id": "wrong-window"}):
            with pytest.raises(RuntimeError, match="no durable summary or new context window"):
                await _commit_automatic_compaction(repo, conversation_id=record.id, context_builder=builder, summary="", window_receipt=receipt)
        await _commit_automatic_compaction(repo, conversation_id=record.id, context_builder=builder, summary="",
            window_receipt=snapshot["context_window_state"])
        stored = ConversationRepository(tmp_path / "partial-conversations").get_conversation(record.id)
        assert stored.compaction_state == "compacted" and stored.compaction_summary == ""
        assert stored.context_snapshot["context_window_state"]["window_number"] == 1
    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_real_ws_rollover_keeps_session_rest_and_reopened_user_tool_reply_history(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    session.conversation_repo.update_memory_mode(parent.id, "disabled")
    model = WindowModel([
        [ToolCallEvent("ws-read", "read_file", {"file_path": str(tmp_path / "source-workspace/source.txt")})],
        [ToolCallEvent("ws-new", "new_context", {})],
        "Actual reply after a durable new window.",
    ])
    model.capabilities = replace(model.capabilities, provider="openai", provider_id="openai", model="fixture-model",
        wire_api="responses", reasoning_effort=True, reasoning_effort_levels=("low", "medium", "high"))
    registry = ToolRegistry()
    for tool in (NewContextTool(), GetContextRemainingTool(), ReadFileTool(session.artifact_store)):
        registry.register(tool)
    session.config = replace(session.config, feature_flags=FeatureFlags({"token_budget": True}),
        llm=replace(session.config.llm, api_key="test-key", provider="openai", model="fixture-model"))
    monkeypatch.setenv("OPENAI_AVAILABLE_MODELS", "fixture-model")
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **_kwargs: session.config)
    monkeypatch.setattr("backend.ws.agent_runner._get_or_create_session_llm", lambda *_args, **_kwargs: model)
    monkeypatch.setattr(session, "_build_conversation_tool_registry", lambda *_args, **_kwargs: registry)
    try:
        assert await session.command_dispatcher._dispatch_client_command(UserCommand("user_message", {
            "conversation_id": parent.id, "workspace_root": parent.workspace_root,
            "permission_mode": "bypass", "client_command_id": "window-ws-command",
            "user_message_id": "window-ws-user", "assistant_message_id": "window-ws-answer",
            "content": "Read the actual source file then start a fresh context window.",
        }), session.connection_generation) is True
        running = session.run_manager.running_task_for(parent.id)
        if running is not None:
            await running
        await session.event_outbox.drain_delivery()
        sent = connection["websocket"].sent
        assert not any(event.get("type") == "error" for event in sent), json.dumps([event for event in sent if event.get("type") == "error"], ensure_ascii=False)
        completed = [event for event in sent if event.get("type") == "item.completed" and event["item"]["type"] == "context_compaction"]
        assert len(completed) == 1
        assert completed[0]["message_id"] == "window-ws-answer"
        record = session.conversation_repo.get_conversation(parent.id)
        assert record.context_snapshot["context_window_state"]["window_number"] == 1
        assert "Recorded user 0" not in "\n".join(message.content for message in model.inputs[2])
        assert "Read the actual source file" not in "\n".join(message.content for message in model.inputs[2])
        assert [message["id"] for message in record.transcript] == ["message-0", "message-1", "window-ws-user", "window-ws-answer"]
        answer = record.transcript[-1]
        assert answer["content"] == "Actual reply after a durable new window."
        assert any(block.get("type") == "tool_call" and block["record"]["name"] == "read_file" for block in answer["blocks"])
        assert any(block.get("itemKind") == "context_compaction" for block in answer["blocks"])
        reloaded = ConversationRepository(session.conversation_repo.storage_root).get_conversation(parent.id)
        assert reloaded.transcript == record.transcript
        from backend.api import _state, routes_chat
        monkeypatch.setattr(_state, "ws_manager", session.ws_manager)
        app = FastAPI()
        app.include_router(routes_chat.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local-test") as client:
            response = await client.get(f"/api/conversations/{parent.id}/messages", params={"session_id": session.session_id})
        assert response.status_code == 200, response.text
        rest = response.json()["transcript"]
        assert [message["id"] for message in rest] == [message["id"] for message in record.transcript]
        assert rest[0]["content"] == "Recorded user 0" and rest[1]["content"] == "Recorded assistant 1"
        rest_block = next(block for block in rest[-1]["blocks"] if block.get("itemKind") == "context_compaction")
        assert rest_block["id"] == completed[0]["item"]["id"]
        assert rest_block["startedAt"] == completed[0]["item"]["started_at"]
        assert rest_block["finishedAt"] == completed[0]["item"]["finished_at"]
    finally:
        await release_window_session(session, parent.id)


@pytest.mark.asyncio
async def test_real_manual_ws_new_window_has_independent_operation_and_durable_system_item(tmp_path, monkeypatch):
    session, connection, parent = await _connected_fork_session(tmp_path, monkeypatch)
    session.conversation_repo.update_memory_mode(parent.id, "disabled")
    model = WindowModel([])
    captured_config = replace(session.config, feature_flags=FeatureFlags({"token_budget": True}))
    execution = ModelExecutionSnapshot(config=captured_config, llm=model, provider="openai", model="fixture-model")
    monkeypatch.setattr("backend.ws.handlers.conversation._capture_manual_compaction_model",
        lambda *_args: (execution, tmp_path / "source-workspace", []))
    original = deepcopy(parent.transcript)
    try:
        assert await session.command_dispatcher._dispatch_client_command(UserCommand("context.compact", {
            "conversation_id": parent.id, "client_command_id": "manual-window-command",
        }), session.connection_generation) is True
        await session.event_outbox.drain_delivery()
        sent = connection["websocket"].sent
        assert not any(event.get("type") == "error" for event in sent), json.dumps([event for event in sent if event.get("type") == "error"])
        started = next(event for event in sent if event.get("type") == "item.started")
        completed = next(event for event in sent if event.get("type") == "item.completed")
        assert started["item"]["source"] == completed["item"]["source"] == "manual"
        assert started["turn_id"] == completed["turn_id"]
        assert not started.get("message_id") and not completed.get("message_id")
        assert completed["item"]["finished_at"] >= started["item"]["started_at"]
        legacy = [event for event in sent if event.get("type") in {"context_compacted", "conversation.compaction.updated"}]
        assert legacy and all(event["compaction_item_id"] == completed["item"]["id"] for event in legacy)
        assert model.calls == 0
        restored = ConversationRepository(session.conversation_repo.storage_root).get_conversation(parent.id)
        assert restored.transcript[:len(original)] == original
        assert len(restored.transcript) == len(original) + 1
        process = project_public_transcript_message(restored.transcript[-1])
        assert process["role"] == "system" and process["id"] == completed["item"]["id"]
        assert process["blocks"][0]["startedAt"] == started["item"]["started_at"]
        assert process["blocks"][0]["finishedAt"] == completed["item"]["finished_at"]
        assert restored.context_snapshot["context_window_state"]["window_number"] == 1
        assert restored.compaction_summary == ""
    finally:
        await release_window_session(session, parent.id)
