"""Actual internal producers, accepted input and provider transport attribution."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.context_window import ContextWindowConfig
from backend.agent.guardian.config import build_reviewer_config, reviewer_sandbox_policy, select_review_model
from backend.agent.guardian.prompt import GuardianPermissionEvidence, build_guardian_policy, build_guardian_prompt
from backend.agent.guardian.retained import GuardianEvidenceOwner
from backend.agent.guardian.runtime import GuardianRuntime
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, AppConfig, LLMSettings, PermissionSettings, TokenBudget
from backend.feature_flags import FeatureFlags
from backend.llm.anthropic_protocol import _anthropic_request_metadata
from backend.llm.base import LLMMessage, StreamEventType, sanitize_llm_request_metadata
from backend.llm.cost_tracker import CostTracker
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.memory import consolidation_agent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tests.guardian_http_fixture import http_endpoint, send_json, send_sse
from backend.tests.test_codex_guardian import guardian_context
from backend.tests.test_responses_websocket import endpoint
from backend.tools.context_window_tools import NewContextTool
from backend.tools.registry import ToolRegistry


def response_events(index, *, read=False, text="INTERNAL_PROVENANCE_COMPLETE", tokens=(7, 3)):
    item = ({"type": "function_call", "id": "memory-read-item", "call_id": "memory-read-call",
        "name": "read_file", "arguments": json.dumps({"file_path": "memory-input.txt"}), "status": "completed"}
        if read else {"type": "message", "id": f"actual-answer-{index}", "role": "assistant", "phase": "final_answer",
            "content": [{"type": "output_text", "text": text}]})
    return [{"type": "response.completed", "response": {"id": f"actual-response-{index}", "status": "completed",
        "output": [item], "usage": {"input_tokens": tokens[0], "output_tokens": tokens[1]}}}]


def chat_events(index, *, read=False, tokens=(7, 3)):
    delta = ({"tool_calls": [{"index": 0, "id": "memory-read-call", "type": "function",
        "function": {"name": "read_file", "arguments": json.dumps({"file_path": "memory-input.txt"})}}]}
        if read else {"content": "INTERNAL_PROVENANCE_COMPLETE"})
    return [{"id": f"actual-chat-{index}", "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
        {"id": f"actual-chat-{index}", "object": "chat.completion.chunk", "choices": [
            {"index": 0, "delta": {}, "finish_reason": "tool_calls" if read else "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": tokens[0], "completion_tokens": tokens[1]}}]


@asynccontextmanager
async def foreground_scene(tmp_path, adapter, *, metadata=None, window=False, conversation_id="foreground-thread"):
    artifacts = ArtifactStore(storage_dir=tmp_path / "foreground-artifacts")
    registry = ToolRegistry()
    if window:
        registry.register(NewContextTool(ContextWindowConfig(model_context_window=100_000)))
    budget = TokenBudget(total=100_000)
    builder = ContextBuilder(llm=adapter, token_budget=budget, workspace_root=tmp_path)
    settings = AgentSettings(code_mode_only=False, max_iterations=4, stream_max_attempts=0)
    session = AgentSession(llm=adapter, tool_registry=registry, artifact_store=artifacts,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=settings,
        token_budget=budget, context_builder=builder)
    runtime = AgentRuntime(metrics_file=tmp_path / "foreground-runtime/metrics.jsonl",
        swarm_store_dir=tmp_path / "foreground-runtime/swarm", enable_lease_heartbeat=False)
    state = AgentState(user_message="FOREGROUND_ACTUAL_INPUT", conversation_id=conversation_id, workspace_root=tmp_path)
    owner = RunContext(agent_runtime=runtime)
    try:
        events = [event async for event in QueryEngine().submit(QuerySubmission(user_message=state.user_message,
            session=session, state=state, runtime=AgentLoopSessionContext(session_id="foreground-session",
                workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"), run_context=owner,
                metadata=metadata or {})))]
        assert state.terminal_status == "completed", (state.stopped_reason, [event.data for event in events])
        yield SimpleNamespace(builder=builder, state=state, events=events, owner=owner, session=session)
    finally:
        await session.aclose()
        artifacts.shutdown()
        runtime.close(release_lease=True)


def observe_memory_engine(monkeypatch):
    submissions = []
    class RecordingEngine(QueryEngine):
        async def submit(self, submission):
            submissions.append(submission)
            async for event in super().submit(submission):
                yield event
    monkeypatch.setattr(consolidation_agent, "QueryEngine", RecordingEngine)
    return submissions


def assert_memory_input_and_usage(submission, tracker):
    builder = submission.session.context_builder
    admitted = [message for message in builder._history if message.is_user_input]
    assert len(admitted) == 1 and admitted[0].input_origin == "user"
    retained = builder.guardian_evidence.retained.user_messages
    assert len(retained) == 1 and retained[0].value.origin == "user"
    snapshot = json.loads(json.dumps(builder.export_snapshot()))
    restored = ContextBuilder(llm=submission.session.llm)
    restored.load_snapshot(snapshot)
    restored_input = [message for message in restored._history if message.is_user_input]
    assert len(restored_input) == 1 and restored_input[0].input_origin == "user"
    assert restored.guardian_evidence.retained.user_messages[0].value.origin == "user"
    assert len(submission.state.tool_calls) == 1
    assert submission.state.tool_calls[0].status == "success"
    assert "MEMORY_PROVENANCE_TOOL_INPUT" in str(submission.state.tool_calls[0].tool_output)
    summary = tracker.get_summary(submission.runtime.session_id)
    assert (summary["input_tokens"], summary["output_tokens"]) == (14, 6)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", ["chat", "responses"])
async def test_actual_memory_producer_preserves_origin_cost_owner_and_received_http_attribution(tmp_path, monkeypatch, wire_api):
    submissions = observe_memory_engine(monkeypatch)
    tracker = CostTracker()
    monkeypatch.setattr(CostTracker, "_instance", tracker)
    (tmp_path / "memory-input.txt").write_text("MEMORY_PROVENANCE_TOOL_INPUT", encoding="utf-8")
    memory_attempts = 0
    async def respond(request, reader, writer):
        nonlocal memory_attempts
        is_memory = (request.body.get("client_metadata", {}).get("x-openai-subagent") == "memory_consolidation"
            if wire_api == "responses" else request.body.get("metadata", {}).get("query_source") == "background")
        if is_memory:
            memory_attempts += 1
            if memory_attempts == 1:
                await send_json(writer, {"error": {"message": "controlled transient overload", "type": "server_error"}}, 503)
                return
        events = (response_events(request.index, read=is_memory and memory_attempts == 2)
            if wire_api == "responses" else chat_events(request.index, read=is_memory and memory_attempts == 2))
        await send_sse(writer, events)
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="local-provenance-fixture", model="provenance-fixture",
            base_url=server.url + "/v1", wire_api=wire_api, proxy_mode="direct", max_tokens=1024))
        try:
            async with foreground_scene(tmp_path, adapter) as foreground:
                foreground_snapshot = deepcopy(foreground.builder.export_snapshot())
                foreground_cost = tracker.get_summary("foreground-session")
                await consolidation_agent.run_memory_consolidation_agent(llm=adapter, memory_root=tmp_path,
                    prompt="Read memory-input.txt and consolidate the supplied memory.")
                assert len(submissions) == 1 and memory_attempts == 3
                submission = submissions[0]
                assert submission.runtime.metadata["query_source"] == "background"
                assert submission.runtime.metadata["internal_session_source"] == "memory_consolidation"
                assert not submission.session.context_builder.token_budget_enabled
                assert_memory_input_and_usage(submission, tracker)
                assert foreground.builder.export_snapshot() == foreground_snapshot
                current_cost = tracker.get_summary("foreground-session")
                assert (current_cost["input_tokens"], current_cost["output_tokens"]) == (foreground_cost["input_tokens"], foreground_cost["output_tokens"])
                assert (current_cost["input_tokens"], current_cost["output_tokens"]) == (7, 3)
                requests = server.requests[1:]
                if wire_api == "responses":
                    clients = [request.body["client_metadata"] for request in requests]
                    assert len({(client["session_id"], client["thread_id"], client["turn_id"]) for client in clients}) == 1
                    for request, client in zip(requests, clients, strict=True):
                        assert request.headers["x-openai-subagent"] == client["x-openai-subagent"] == "memory_consolidation"
                        assert request.headers["x-openai-memgen-request"] == "true"
                        assert "x-codex-guardian" not in request.headers
                        source = json.loads(client["x-codex-turn-metadata"])
                        assert client["x-codex-turn-metadata"] == request.headers["x-codex-turn-metadata"]
                        assert source["thread_source"] == source["turn_trigger"] == "memory_consolidation"
                        assert source["request_kind"] == "turn"
                        assert not {"context_window_id", "window_number", "parent_thread_id", "subagent_kind"} & source.keys()
                        assert "x-codex-parent-thread-id" not in request.headers
                else:
                    assert len({(request.body["metadata"]["session_id"], request.body["metadata"]["thread_id"], request.body["metadata"]["turn_id"]) for request in requests}) == 1
                    for request in requests:
                        assert request.body["metadata"]["query_source"] == "background"
                        assert not {"internal_session_source", "_source_responses_metadata", "x-openai-subagent"} & request.body["metadata"].keys()
                        assert not {"x-openai-memgen-request", "x-openai-subagent", "x-codex-turn-metadata"} & request.headers.keys()
        finally:
            for submission in submissions:
                await submission.session.aclose()
                submission.session.artifact_store.shutdown()
            await adapter.aclose()


@pytest.mark.asyncio
async def test_actual_memory_websocket_reuses_source_header_and_blob_after_real_file_tool(tmp_path, monkeypatch):
    submissions = observe_memory_engine(monkeypatch)
    (tmp_path / "memory-input.txt").write_text("MEMORY_PROVENANCE_TOOL_INPUT", encoding="utf-8")
    handshakes = []
    def handshake(socket, request):
        handshakes.append(dict(request.headers))
    async def respond(socket, request, index):
        await socket.send(json.dumps(response_events(index, read=index == 1)[0]))
    async with endpoint(respond, process_request=handshake) as (adapter, requests, connections):
        try:
            await consolidation_agent.run_memory_consolidation_agent(llm=adapter, memory_root=tmp_path,
                prompt="Read memory-input.txt before consolidating.")
            assert len(submissions) == 1 and len(requests) == 2 and len(connections) == 1
            assert handshakes[0]["x-openai-subagent"] == "memory_consolidation"
            assert handshakes[0]["x-openai-memgen-request"] == "true"
            assert requests[1]["previous_response_id"] == "actual-response-1"
            assert requests[1]["input"][0]["type"] == "function_call_output"
            assert "MEMORY_PROVENANCE_TOOL_INPUT" in str(requests[1]["input"])
            for request in requests:
                client = request["client_metadata"]
                source = json.loads(client["x-codex-turn-metadata"])
                assert client["x-openai-subagent"] == "memory_consolidation"
                assert source["thread_source"] == source["turn_trigger"] == "memory_consolidation"
                assert source["request_kind"] == "turn" and "context_window_id" not in source
        finally:
            for submission in submissions:
                await submission.session.aclose()
                submission.session.artifact_store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_response", [None, "actual-issued-parent-response"])
@pytest.mark.parametrize("provider,reviewer_model", [
    ("custom", "provenance-reviewer"), ("openai", "gpt-5.6-luna"), ("openai", "overridden-reviewer"),
])
async def test_actual_reused_guardian_runtime_projects_its_source_and_real_parent_only(tmp_path, parent_response, provider, reviewer_model):
    async def respond(request, reader, writer):
        await send_sse(writer, response_events(request.index, text='{"outcome":"allow"}'))
    async with http_endpoint(respond) as server:
        ctx, action = guardian_context(tmp_path)
        settings = LLMSettings(provider=provider, api_key="local-provenance-fixture", model="provenance-parent",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct", max_tokens=1024)
        parent_adapter = OpenAIAdapter(settings)
        info = ModelDefinition(provider=provider, id=settings.model, name=settings.model, api="openai-responses",
            base_url=settings.base_url, context_window=128000, extra={"auto_review_model_override": reviewer_model})
        config = AppConfig(llm=settings, feature_flags=FeatureFlags({"guardian_approval": True}))
        ctx.model_execution = replace(ModelExecutionSnapshot.capture(config, parent_adapter), model_info=info)
        ctx.metadata.update(root_turn_id="actual-root-turn", parent_response_id=parent_response)
        review_model = select_review_model(ctx.model_execution, ())
        reviewer_config = build_reviewer_config(config, review_model)
        reviewer = GuardianRuntime(parent_snapshot=ctx.model_execution, config=reviewer_config,
            policy=build_guardian_policy({}), sandbox_policy=reviewer_sandbox_policy(ctx.sandbox_policy),
            artifacts=ctx.artifact_store, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_selection: OpenAIAdapter(cfg.llm))
        prompt = build_guardian_prompt(GuardianEvidenceOwner().snapshot(), action, GuardianPermissionEvidence("local"), session_id=ctx.session_id)
        try:
            for index in range(2):
                ctx.turn_id = f"actual-parent-turn-{index}"
                answer = await reviewer.run(prompt, cancel_event=asyncio.Event(), parent_context=ctx,
                    deadline=asyncio.get_running_loop().time() + 20)
                assert json.loads(answer)["outcome"] == "allow"
            assert len(server.requests) == 2
            clients = [request.body["client_metadata"] for request in server.requests]
            assert clients[0]["thread_id"] == clients[1]["thread_id"] == reviewer.id
            assert clients[0]["turn_id"] != clients[1]["turn_id"]
            for index, (request, client) in enumerate(zip(server.requests, clients, strict=True)):
                assert request.headers["x-openai-subagent"] == client["x-openai-subagent"] == "guardian"
                assert "x-codex-guardian" not in request.headers
                assert "x-openai-memgen-request" not in request.headers
                assert request.headers["x-codex-parent-thread-id"] == ctx.conversation_id
                source = json.loads(client["x-codex-turn-metadata"])
                assert source["thread_source"] == source["turn_trigger"] == "guardian_review"
                assert source["request_kind"] == "turn"
                assert source["parent_turn_id"] == client["parent_turn_id"] == f"actual-parent-turn-{index}"
                assert source["root_turn_id"] == "actual-root-turn"
                assert not {"context_window_id", "window_number", "subagent_kind"} & source.keys()
                assert "metadata" not in request.body
                assert "parent_response_id" not in client and "parent_response_id" not in source
        finally:
            await reviewer.aclose()
            await parent_adapter.aclose()
            ctx.artifact_store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,reviewer_model", [
    ("custom", "provenance-reviewer"), ("openai", "gpt-5.6-luna"), ("openai", "overridden-reviewer"),
])
async def test_real_guardian_websocket_keeps_custom_api_key_and_override_requests_outside_backend_reviewer_routing(tmp_path, provider, reviewer_model):
    handshakes = []
    def handshake(socket, request):
        handshakes.append(dict(request.headers))
    async def respond(socket, request, index):
        await socket.send(json.dumps(response_events(index, text='{"outcome":"allow"}')[0]))
    async with endpoint(respond, process_request=handshake) as (parent_adapter, requests, connections):
        ctx, action = guardian_context(tmp_path)
        settings = replace(parent_adapter._settings, provider=provider, model="provenance-parent")
        info = ModelDefinition(provider=provider, id=settings.model, name=settings.model, api="openai-responses",
            base_url=settings.base_url, context_window=128000, extra={"auto_review_model_override": reviewer_model})
        config = AppConfig(llm=settings, feature_flags=FeatureFlags({"guardian_approval": True}))
        ctx.model_execution = replace(ModelExecutionSnapshot.capture(config, parent_adapter), provider=provider, model_info=info)
        ctx.metadata.update(root_turn_id="actual-root-turn")
        review_model = select_review_model(ctx.model_execution, ())
        reviewer = GuardianRuntime(parent_snapshot=ctx.model_execution, config=build_reviewer_config(config, review_model),
            policy=build_guardian_policy({}), sandbox_policy=reviewer_sandbox_policy(ctx.sandbox_policy),
            artifacts=ctx.artifact_store, conversation_id=ctx.conversation_id,
            llm_factory=lambda cfg, **_selection: OpenAIAdapter(cfg.llm))
        prompt = build_guardian_prompt(GuardianEvidenceOwner().snapshot(), action, GuardianPermissionEvidence("local"), session_id=ctx.session_id)
        try:
            for index, parent_response in enumerate(("issued-parent-one", "issued-parent-two", None)):
                ctx.turn_id = f"actual-parent-turn-{index}"
                ctx.metadata["parent_response_id"] = parent_response
                answer = await reviewer.run(prompt, cancel_event=asyncio.Event(), parent_context=ctx,
                    deadline=asyncio.get_running_loop().time() + 20)
                assert json.loads(answer)["outcome"] == "allow"
            assert len(requests) == 3 and handshakes
            assert len({request["client_metadata"]["thread_id"] for request in requests}) == 1
            assert len({request["client_metadata"]["turn_id"] for request in requests}) == 3
            for headers in handshakes:
                assert headers["x-openai-subagent"] == "guardian"
                assert not {"x-codex-guardian", "x-openai-memgen-request", "parent_response_id"} & headers.keys()
            for index, request in enumerate(requests):
                assert request["model"] == reviewer_model and "metadata" not in request
                client = request["client_metadata"]
                assert client["x-openai-subagent"] == "guardian"
                assert "parent_response_id" not in client
                source = json.loads(client["x-codex-turn-metadata"])
                assert source["thread_source"] == source["turn_trigger"] == "guardian_review"
                assert source["parent_turn_id"] == f"actual-parent-turn-{index}"
                assert "parent_response_id" not in source
        finally:
            await reviewer.aclose()
            ctx.artifact_store.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [False, True])
async def test_host_source_snapshot_merge_preserves_authored_request_kind_and_existing_window_facts(tmp_path, window):
    source = {"session_id": "actual-source-session", "thread_id": "actual-source-thread", "turn_id": "actual-source-turn",
        "request_kind": "memory", "thread_source": "memory_consolidation", "turn_trigger": "memory_consolidation",
        "context_window_id": "11fd5f29-a7d6-478b-a753-5e9da1b88501", "window_number": 8, "turn_started_at_unix_ms": 12345}
    original = deepcopy(source)
    async def respond(request, reader, writer):
        await send_sse(writer, response_events(request.index))
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="local-provenance-fixture", model="provenance-fixture",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        try:
            metadata = {"_source_responses_metadata": source, "llm_request_metadata": {
                "_source_responses_metadata": {"request_kind": "untrusted-override"}}}
            async with foreground_scene(tmp_path, adapter, metadata=metadata, window=window) as foreground:
                assert foreground.builder.token_budget_enabled is window
                emitted = json.loads(server.requests[0].body["client_metadata"]["x-codex-turn-metadata"])
                assert {key: emitted[key] for key in original} == original
                assert source == original
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
async def test_actual_window_request_uses_the_issuing_runs_epoch_milliseconds(tmp_path):
    async def respond(request, reader, writer):
        await send_sse(writer, response_events(request.index))
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="local-provenance-fixture", model="provenance-fixture",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        try:
            async with foreground_scene(tmp_path, adapter, window=True) as foreground:
                started = next(event for event in foreground.events if event.type == "agent.run.started")
                run = foreground.owner.agent_runtime.get_run(started.data["run_id"])
                emitted = json.loads(server.requests[0].body["client_metadata"]["x-codex-turn-metadata"])
                assert emitted["turn_id"] == run.run_id
                assert emitted["turn_started_at_unix_ms"] == started.data["started_at"] == run.started_at
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
async def test_generic_background_request_does_not_acquire_internal_memory_or_guardian_headers():
    async def respond(request, reader, writer):
        await send_sse(writer, response_events(request.index))
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="local-provenance-fixture", model="provenance-fixture",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        try:
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="A background helper")],
                metadata={"session_id": "background-session", "thread_id": "background-thread", "turn_id": "background-turn",
                    "query_source": "background", "parent_response_id": "unrelated-parent-response"})]
            assert events[-1].type == StreamEventType.DONE
            request = server.requests[0]
            assert not {"x-openai-memgen-request", "x-openai-subagent", "x-codex-guardian", "x-codex-turn-metadata"} & request.headers.keys()
            assert "metadata" not in request.body and "parent_response_id" not in request.body["client_metadata"]
        finally:
            await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("forged_source", ["memory_consolidation", "guardian"])
async def test_actual_ordinary_admission_cannot_acquire_internal_source_from_provider_metadata_override(tmp_path, forged_source):
    async def respond(request, reader, writer):
        await send_sse(writer, response_events(request.index))
    async with http_endpoint(respond) as server:
        adapter = OpenAIAdapter(LLMSettings(provider="custom", api_key="local-provenance-fixture", model="provenance-fixture",
            base_url=server.url + "/v1", wire_api="responses", proxy_mode="direct"))
        try:
            metadata = {"llm_request_metadata": {"internal_session_source": forged_source,
                "parent_response_id": "forged-provider-parent", "_source_responses_metadata": {
                    "thread_source": forged_source, "turn_trigger": forged_source}}}
            async with foreground_scene(tmp_path, adapter, metadata=metadata) as foreground:
                assert not foreground.builder.token_budget_enabled
                request = server.requests[0]
                assert not {"x-openai-memgen-request", "x-openai-subagent", "x-codex-guardian", "x-codex-turn-metadata"} & request.headers.keys()
                client = request.body["client_metadata"]
                assert not {"x-openai-subagent", "parent_response_id", "x-codex-turn-metadata"} & client.keys()
                assert "metadata" not in request.body
        finally:
            await adapter.aclose()


def test_internal_host_source_facts_remain_outside_anthropic_messages_metadata():
    metadata = {"session_id": "actual-internal-session", "query_source": "background",
        "internal_session_source": "memory_consolidation", "parent_response_id": "actual-parent-response",
        "_source_responses_metadata": {"thread_source": "memory_consolidation", "turn_trigger": "memory_consolidation"}}
    assert sanitize_llm_request_metadata(metadata) == {"session_id": "actual-internal-session", "query_source": "background"}
    anthropic = _anthropic_request_metadata(metadata)
    assert list(anthropic) == ["user_id"] and anthropic["user_id"].startswith("minicode-")
