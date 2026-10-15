"""Actual startup A -> persisted rollout B/C -> detached Source model request."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import time
from uuid import UUID

import pytest

from backend.conversations.repository import ConversationRepository
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.cost_tracker import CostTracker
from backend.llm.openai_adapter import OpenAIAdapter
from backend.config import LLMSettings
from backend.memory.file_memory import FileMemory
from backend.memory.generation import MemoryGenerationCoordinator, schedule_memory_startup
from backend.memory.job_store import MemoryJobStore, MEMORY_DB_NAME
from backend.tests.guardian_http_fixture import http_endpoint, send_json, send_sse
from backend.tests.test_internal_request_provenance_chain import foreground_scene, response_events


def conversation(repository, workspace, label):
    record = repository.create_conversation(title=label, workspace_root=str(workspace))
    return repository.append_transcript_message(record.id, {"id": f"user-{label}", "role": "user", "content": label})


def phase1_result(label):
    return json.dumps({"raw_memory": f"Remembered {label}", "rollout_summary": f"Extracted {label}", "rollout_slug": label})


def response_body(text, response_id):
    return [{"type": "response.completed", "response": {"id": response_id, "status": "completed",
        "output": [{"type": "message", "id": response_id + "-answer", "role": "assistant",
            "content": [{"type": "output_text", "text": text}]}], "usage": {"input_tokens": 7, "output_tokens": 3}}}]


def observe_side_calls(monkeypatch, adapter):
    calls = []
    original = adapter._side_query_chat
    async def observed(messages, *, context):
        calls.append(context)
        return await original(messages, context=context)
    monkeypatch.setattr(adapter, "_side_query_chat", observed)
    return calls


def only_phase1(monkeypatch):
    async def phase2(_coordinator):
        pass
    monkeypatch.setattr(MemoryGenerationCoordinator, "_run_phase2", phase2)
    future = int(time.time()) + 7 * 60 * 60
    monkeypatch.setattr(MemoryGenerationCoordinator, "_now", lambda _coordinator: future)


@pytest.mark.asyncio
async def test_real_scheduled_startup_owns_two_extractions_and_retry_without_borrowing_foreground_accounting(tmp_path, monkeypatch):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    startup = conversation(repository, tmp_path, "FOREGROUND_ACTUAL_INPUT")
    previous = [conversation(repository, tmp_path, label) for label in ("ROLLOUT_B_FACT", "ROLLOUT_C_FACT")]
    tracker = CostTracker()
    monkeypatch.setattr(CostTracker, "_instance", tracker)
    seen_turns = set()
    failed_once = False
    async def respond(request, reader, writer):
        nonlocal failed_once
        client = request.body.get("client_metadata", {})
        source = json.loads(client.get("x-codex-turn-metadata", "{}"))
        if source.get("request_kind") != "memory":
            await send_sse(writer, response_events(request.index))
            return
        turn = client["turn_id"]
        if not failed_once:
            failed_once = True
            await send_json(writer, {"error": {"message": "controlled phase1 retry", "type": "server_error"}}, 503)
            return
        seen_turns.add(turn)
        prompt = str(request.body["input"])
        label = "ROLLOUT_B_FACT" if "ROLLOUT_B_FACT" in prompt else "ROLLOUT_C_FACT"
        await send_sse(writer, response_body(phase1_result(label), f"phase1-response-{request.index}"))
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(api_key="local-phase1-key", provider="custom", model="phase1-fixture",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        calls = observe_side_calls(monkeypatch, adapter)
        try:
            async with foreground_scene(tmp_path, adapter, conversation_id=startup.id) as foreground:
                before = deepcopy(foreground.builder.export_snapshot())
                side_records = list(foreground.owner.llm_turn_context.side_call_records)
                task = schedule_memory_startup(repository=repository, llm=adapter, workspace_root=tmp_path,
                    current_conversation_id=startup.id, startup_sandbox_mode="workspace-write")
                assert schedule_memory_startup(repository=repository, llm=adapter, workspace_root=tmp_path,
                    current_conversation_id=previous[0].id, startup_sandbox_mode="read-only") is task
                await task
                assert foreground.builder.export_snapshot() == before
                assert foreground.owner.llm_turn_context.side_call_records == side_records
                cost = tracker.get_summary("foreground-session")
                assert (cost["input_tokens"], cost["output_tokens"]) == (7, 3)
                total = tracker.get_summary()
                assert (total["input_tokens"], total["output_tokens"]) == (21, 9)
            requests = server.requests[1:]
            assert len(requests) == 3 and len(seen_turns) == 2
            assert len(calls) == 3 and all(call.turn is None for call in calls)
            turn_ids = set()
            for request in requests:
                assert request.path == "/v1/responses"
                client = request.body["client_metadata"]
                assert client["session_id"] == client["thread_id"] == startup.id
                assert client["x-codex-window-id"] == request.headers["x-codex-window-id"] == f"{startup.id}:0"
                source = json.loads(client["x-codex-turn-metadata"])
                assert client["x-codex-turn-metadata"] == request.headers["x-codex-turn-metadata"]
                assert source == {"turn_id": client["turn_id"], "root_turn_id": client["turn_id"], "request_kind": "memory",
                    "thread_source": "memory_consolidation", "turn_trigger": "memory_consolidation", "sandbox_mode": "workspace-write"}
                assert UUID(client["turn_id"]).version == 7
                assert client["root_turn_id"] == client["turn_id"]
                assert not {"x-openai-subagent", "x-openai-memgen-request", "x-codex-guardian"} & request.headers.keys()
                turn_ids.add(client["turn_id"])
            assert len(turn_ids) == 2
            by_turn = {turn: [request for request in requests if request.body["client_metadata"]["turn_id"] == turn] for turn in turn_ids}
            assert sorted(map(len, by_turn.values())) == [1, 2]
            for call in calls:
                actual = call.request_metadata()
                assert actual["session_id"] == actual["thread_id"] == startup.id
                assert actual["turn_id"] == call.options.turn_id and actual["query_source"] == "background"
                assert actual["_source_responses_metadata"]["request_kind"] == "memory"
            store = MemoryJobStore(FileMemory.for_workspace(tmp_path).memory_dir / MEMORY_DB_NAME)
            outputs = store.list_stage1_outputs(limit=None, max_unused_days=30)
            assert {output.thread_id for output in outputs} == {record.id for record in previous}
            assert {output.raw_memory for output in outputs} == {"Remembered ROLLOUT_B_FACT", "Remembered ROLLOUT_C_FACT"}
            assert startup.id not in {output.thread_id for output in outputs}
            cold = ConversationRepository(tmp_path / "conversations")
            assert {cold.get_conversation(record.id).transcript[0]["content"] for record in previous} == {"ROLLOUT_B_FACT", "ROLLOUT_C_FACT"}
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_id,source_header", [("known-startup", "collab_spawn"), ("", None)])
async def test_original_startup_source_label_and_explicit_unknown_never_become_phase2_internal_memory(tmp_path, monkeypatch, startup_id, source_header):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    old = conversation(repository, tmp_path, "OLD_SOURCE_ROLLOUT")
    async def respond(request, reader, writer):
        await send_sse(writer, response_body(phase1_result("OLD_SOURCE_ROLLOUT"), "source-header-response"))
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(api_key="local-phase1-key", provider="custom", model="phase1-fixture",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        try:
            coordinator = MemoryGenerationCoordinator(repository=repository, llm=adapter, workspace_root=tmp_path)
            await coordinator.run_startup(current_conversation_id=startup_id, startup_subagent_header=source_header)
            assert len(server.requests) == 1
            request = server.requests[0]
            client = request.body["client_metadata"]
            assert "x-openai-memgen-request" not in request.headers
            if startup_id:
                assert client["session_id"] == client["thread_id"] == startup_id
                assert request.headers["x-openai-subagent"] == client["x-openai-subagent"] == source_header
                source = json.loads(client["x-codex-turn-metadata"])
                assert source["request_kind"] == "memory" and "sandbox_mode" not in source
            else:
                assert client["session_id"] == client["thread_id"] != old.id
                assert "turn_id" not in client and "x-codex-turn-metadata" not in client
                assert not {"x-openai-subagent", "x-codex-window-id", "x-codex-turn-metadata"} & request.headers.keys()
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", ["chat", "anthropic"])
async def test_detached_source_identity_preserves_actual_non_responses_provider_schema(tmp_path, monkeypatch, wire_api):
    only_phase1(monkeypatch)
    repository = ConversationRepository(tmp_path / "conversations")
    startup = repository.create_conversation(workspace_root=str(tmp_path))
    old = conversation(repository, tmp_path, "NON_RESPONSES_ROLLOUT")
    text = phase1_result("NON_RESPONSES_ROLLOUT")
    async def respond(request, reader, writer):
        if wire_api == "chat":
            events = [{"id": "phase1-chat", "object": "chat.completion.chunk", "choices": [
                {"index": 0, "delta": {"content": text}, "finish_reason": None}]},
                {"id": "phase1-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}}]
        else:
            events = [{"type": "message_start", "message": {"id": "phase1-anthropic", "type": "message", "role": "assistant",
                "content": [], "model": "claude-sonnet-4", "usage": {"input_tokens": 7, "output_tokens": 0}}},
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
                {"type": "message_stop"}]
        await send_sse(writer, events)
    async with http_endpoint(respond) as server:
        adapter = (OpenAIAdapter(LLMSettings(api_key="local-phase1-key", provider="custom", model="phase1-fixture",
            base_url=server.url + "/v1", wire_api="chat", proxy_mode="direct")) if wire_api == "chat"
            else AnthropicAdapter(api_key="local-phase1-key", model="claude-sonnet-4", base_url=server.url + "/v1", proxy_mode="direct"))
        try:
            coordinator = MemoryGenerationCoordinator(repository=repository, llm=adapter, workspace_root=tmp_path)
            await coordinator.run_startup(current_conversation_id=startup.id, startup_sandbox_mode="read-only")
            assert len(server.requests) == 1
            request = server.requests[0]
            assert not {"x-openai-subagent", "x-openai-memgen-request", "x-codex-turn-metadata", "x-codex-window-id"} & request.headers.keys()
            assert "client_metadata" not in request.body
            if wire_api == "chat":
                assert request.body["metadata"]["session_id"] == request.body["metadata"]["thread_id"] == startup.id
                assert "_source_responses_metadata" not in request.body["metadata"]
            else:
                assert list(request.body["metadata"]) == ["user_id"]
            store = MemoryJobStore(FileMemory.for_workspace(tmp_path).memory_dir / MEMORY_DB_NAME)
            assert store.list_stage1_outputs(limit=None, max_unused_days=30)[0].thread_id == old.id
        finally:
            await adapter.aclose()
