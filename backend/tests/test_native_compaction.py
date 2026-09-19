from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest

from backend.agent.checkpoint import load_latest_checkpoint, save_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.state import AgentState
from backend.config import AgentSettings, LLMSettings, load_llm_settings
from backend.conversations.repository import ConversationRepository
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import LLMTurnContext, LLMMessage, StreamEventType, estimate_llm_message_tokens
from backend.llm.model_runtime import ModelRuntime
from backend.llm.native_compaction import native_compaction_windows
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.capabilities import capabilities_from_openai_settings
from backend.services.llm_adapter_factory import build_provider_adapter


def settings(**kwargs):
    return LLMSettings(api_key="fixture-key", provider="openai", base_url="https://fixture.invalid/v1", model="alpha", wire_api="responses", native_compaction=True, max_tokens=1024, **kwargs)


def output_window():
    return [
        {"id": "original-input", "type": "message", "role": "user", "status": "completed", "content": [{"type": "input_text", "text": "Preserve contract A17."}]},
        {"type": "function_call", "id": "old-function", "call_id": "old-call", "name": "run_command", "arguments": '{"command":"echo old"}', "status": "completed"},
        {"type": "function_call_output", "call_id": "old-call", "output": "already executed"},
        *[{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"retained {index}"}]} for index in range(70)],
        {"id": "cmp-one", "type": "compaction", "encrypted_content": "opaque-provider-state-" * 8000},
    ]


class Endpoint:
    def __init__(self):
        self.requests = []
        self.output = output_window()
        self.answer = "Ready"

    def __call__(self, request):
        self.requests.append((str(request.url), json.loads(request.content), dict(request.headers)))
        if request.url.path.endswith("/compact"):
            return httpx.Response(200, json={"id": "compact-response", "object": "response.compaction", "output": self.output,
                "usage": {"input_tokens": 200, "output_tokens": 20, "input_tokens_details": {"cached_tokens": 100}}})
        events = [
            {"type": "response.output_text.delta", "item_id": "answer", "delta": self.answer},
            {"type": "response.completed", "response": {"id": "next-response", "status": "completed", "output": [
                {"id": "answer", "type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": self.answer}]}
            ], "usage": {"input_tokens": 30, "output_tokens": 1}}},
        ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(f"data: {json.dumps(event)}\n\n" for event in events).encode())


def test_native_window_survives_repository_journal_checkpoint_and_next_wire_request(tmp_path):
    async def scenario():
        endpoint = Endpoint()
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            turn = LLMTurnContext()
            builder = ContextBuilder(llm=adapter)
            builder.bind_llm_turn_context(turn)
            await builder.start_turn("Preserve contract A17.", AgentState(user_message="Preserve contract A17."))
            builder.append_assistant("Earlier work")
            await builder.compact()
            assert endpoint.requests[0][0].endswith("/responses/compact")
            assert set(endpoint.requests[0][1]) == {"model", "input", "instructions"}
            assert turn.usage.input_tokens == 200 and turn.usage.output_tokens == 20
            assert turn.side_call_records[0]["status"] == "completed"
            assert "opaque-provider-state" not in json.dumps(turn.side_call_records)
            snapshot = builder.export_snapshot()
            assert snapshot["context_schema_version"] == 4
            repo = ConversationRepository(tmp_path / "conversations")
            saved = repo.create_conversation(context_snapshot=snapshot)
            cold = ConversationRepository(tmp_path / "conversations").get_conversation(saved.id)
            journal = ExecutionJournal("native", base_dir=tmp_path / "journals")
            journal.append_lifecycle("conversation_projection_pending", {"conversation_id": saved.id, "context_snapshot": snapshot, "assistant_message": {"id": "answer", "role": "assistant", "content": "working"}})
            assert journal.pending_conversation_projections()[0].payload["context_snapshot"]["history"][0]["provider_items"][0]["output"] == endpoint.output
            path = save_checkpoint(session_id="native", user_message="continue", iterations=1, reply="", messages=snapshot["history"], context_snapshot=snapshot,
                tool_calls=[], active_skills=[], disabled_tools=set(), stopped_reason="timeout", last_mutation_index=0, base_dir=tmp_path)
            assert json.loads(path.read_text())["schema_version"] == 10
            checkpoint = load_latest_checkpoint("native", base_dir=tmp_path)
            restored = ContextBuilder(llm=adapter)
            restored.load_snapshot(checkpoint.context_snapshot)
            assert restored.export_snapshot()["history"] == cold.context_snapshot["history"]
            await restored.start_turn("Continue", AgentState(user_message="Continue"))
            events = [event async for event in adapter.stream_chat(await restored.build(AgentState(user_message="Continue")))]
            sent = endpoint.requests[-1][1]["input"]
            assert sent[:len(endpoint.output)] == endpoint.output
            final_content = sent[-1]["content"]
            if isinstance(final_content, list):
                assert final_content[-1]["text"] == "Continue"
            else:
                assert final_content.endswith("Continue")
            assert any(event.type == StreamEventType.DONE for event in events)
            assert not any(event.tool_calls for event in events)
    asyncio.run(scenario())


def test_native_retry_pins_model_endpoint_headers_and_accounts_once(monkeypatch):
    monkeypatch.setattr("backend.llm.base._SIDE_QUERY_BASE_DELAY_SECONDS", 0)
    async def scenario():
        endpoint = Endpoint()
        async def handle(request):
            response = endpoint(request)
            if len(endpoint.requests) == 1:
                adapter._settings = replace(adapter._settings, model="changed", api_key="changed", base_url="https://other.invalid/v1")
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
            return response
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            turn = LLMTurnContext()
            result = await adapter.compact_context([LLMMessage(role="user", content="work")], turn_context=turn)
            assert len(endpoint.requests) == 2
            assert endpoint.requests[0] == endpoint.requests[1]
            assert turn.usage.input_tokens == 200
            assert turn.side_call_records[0]["attempts"] == 2
            assert native_compaction_windows(result)[0]["model"] == "alpha"
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["cancel", "timeout", "invalid"])
def test_failed_native_compaction_does_not_install_partial_context(monkeypatch, failure):
    monkeypatch.setattr("backend.llm.base._SIDE_QUERY_BASE_DELAY_SECONDS", 0)
    if failure == "timeout":
        monkeypatch.setattr("backend.llm.base._SIDE_QUERY_ATTEMPT_TIMEOUT_SECONDS", .02)
    async def scenario():
        entered = asyncio.Event()
        requests = 0
        async def handle(request):
            nonlocal requests
            requests += 1
            entered.set()
            if failure == "invalid":
                return httpx.Response(200, json={"output": [{"type": "compaction", "encrypted_content": ""}]})
            await asyncio.Event().wait()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            builder = ContextBuilder(llm=adapter)
            turn = LLMTurnContext()
            builder.bind_llm_turn_context(turn)
            await builder.start_turn("Keep this exact request", AgentState(user_message="Keep this exact request"))
            before = builder.export_snapshot()
            task = asyncio.create_task(builder.compact())
            await entered.wait()
            if failure == "cancel": task.cancel()
            expected = asyncio.CancelledError if failure == "cancel" else asyncio.TimeoutError if failure == "timeout" else ValueError
            with pytest.raises(expected): await task
            assert builder.export_snapshot() == before
            assert requests == (3 if failure == "timeout" else 1)
            assert turn.side_call_records[0]["status"] == ("cancelled" if failure == "cancel" else "failed")
    asyncio.run(scenario())


def test_local_compaction_preserves_an_existing_native_window_and_provider_scope():
    async def scenario():
        endpoint = Endpoint()
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            builder = ContextBuilder(llm=adapter, agent_settings=AgentSettings(compaction_keep_recent_tokens=5))
            builder.append_user("original")
            await builder.compact()
            original = deepcopy(adapter._build_responses_input(builder._history))
            adapter._settings = replace(adapter._settings, native_compaction=False)
            await builder.start_turn("new request", AgentState(user_message="new request"))
            builder.append_assistant("new work " * 40)
            builder.append_user("latest")
            await builder.compact()
            assert adapter._build_responses_input(builder._history)[:len(original)] == original
            assert sum(url.endswith("/compact") for url, _, _ in endpoint.requests) == 1
            with pytest.raises(ValueError, match="original provider"):
                AnthropicAdapter._convert_messages(builder._history)
            with pytest.raises(ValueError, match="original provider"):
                builder._history[0].to_openai_message()
            adapter._settings = replace(adapter._settings, base_url="https://other.invalid/v1")
            with pytest.raises(ValueError, match="original provider"):
                await builder.build(AgentState(user_message="latest"))
    asyncio.run(scenario())


def test_corrupt_native_snapshot_fails_instead_of_dropping_the_unknown_provider_item():
    snapshot = {"context_schema_version": 2, "history": [{"role": "assistant", "provider_items": [
        {"type": "responses_compaction", "origin": "fixture", "output": []}
    ]}]}
    with pytest.raises(ValueError, match="context window"):
        ContextBuilder().load_snapshot(snapshot)
    with pytest.raises(ValueError, match="schema version"):
        ContextBuilder().load_snapshot({"context_schema_version": 5})


@pytest.mark.parametrize("source", ["settings", "models_json", "extension"])
def test_native_compaction_configuration_reaches_the_selected_adapter(tmp_path, monkeypatch, source):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    if source == "settings":
        config = {"llm": {"provider": "openai", "openai": {"api_key": "fixture", "base_url": "https://fixture.invalid/v1", "model": "alpha", "wire_api": "responses", "available_models": ["alpha"], "model_metadata": {"alpha": {"native_compaction": True}}}}}
        assert load_llm_settings(config).native_compaction is True
        runtime = ModelRuntime(settings_snapshot=config, provider_configs={}, models_path=tmp_path / "models.json")
        provider = "openai"
    else:
        provider = "fixture"
        profile = {"api_key": "fixture", "models": [{"id": "alpha", "api": "openai-responses", "base_url": "https://fixture.invalid/v1", "native_compaction": True, "context_window": 128000, "max_tokens": 8000}]}
        if source == "models_json":
            path = tmp_path / "models.json"
            path.write_text(json.dumps({"providers": {provider: profile}}))
            runtime = ModelRuntime(models_path=path)
        else:
            runtime = ModelRuntime(provider_configs={})
            runtime.register_provider(provider, profile)
    adapter = build_provider_adapter(provider, model_override="alpha", model_runtime=runtime)
    assert adapter.capabilities.native_compaction is True
    assert runtime.get_model(provider, "alpha").to_extension_dict()["native_compaction"] is True
    asyncio.run(adapter.aclose())


@pytest.mark.parametrize("base_url, wire, selection, expected", [
    ("https://api.openai.com/v1", "responses", None, True),
    ("https://api.openai.com/v1", "responses", False, False),
    ("https://gateway.invalid/v1", "responses", None, False),
    ("https://gateway.invalid/v1", "responses", True, True),
    ("https://api.openai.com/v1", "chat", True, False),
])
def test_native_policy_is_explicit_for_unknown_gateways(base_url, wire, selection, expected):
    config = replace(settings(), base_url=base_url, wire_api=wire, native_compaction=selection)
    assert capabilities_from_openai_settings(config).native_compaction is expected


def test_request_hooks_cannot_mutate_the_saved_native_window_or_next_retry(monkeypatch):
    monkeypatch.setattr("backend.llm.base._SIDE_QUERY_BASE_DELAY_SECONDS", 0)
    async def scenario():
        endpoint = Endpoint()
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            window = await adapter.compact_context([LLMMessage(role="user", content="work")])
            original = deepcopy(window.provider_items)
            class Hook:
                async def emit_before_provider_request(self, payload):
                    payload["input"][0]["content"][0]["text"] += " hook suffix"
                    return payload
            turn = LLMTurnContext(lifecycle_runtime=Hook())
            await adapter.compact_context([window], turn_context=turn)
            await adapter.compact_context([window], turn_context=turn)
            assert window.provider_items == original
            assert endpoint.requests[1][1]["input"][0]["content"][0]["text"] == "Preserve contract A17. hook suffix"
            assert endpoint.requests[1][1] == endpoint.requests[2][1]
    asyncio.run(scenario())


def test_native_budget_counts_model_visible_state_and_not_wrapper_or_base64_expansion():
    def carrier(data_size, model):
        return LLMMessage(role="assistant", provider_items=[{
            "type": "responses_compaction", "origin": "owner", "model": model, "output": [
                {"type": "compaction", "encrypted_content": "A" * 4000},
                {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * data_size}]},
            ],
        }])
    short = carrier(4000, "alpha")
    large = carrier(400000, "model-name-that-is-not-part-of-the-request-input")
    assert estimate_llm_message_tokens(short) == estimate_llm_message_tokens(large)
    assert 2500 < estimate_llm_message_tokens(short) < 2700


def test_runtime_mode_updates_append_after_native_context_without_rewriting_it():
    async def scenario():
        endpoint = Endpoint()
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            builder = ContextBuilder(llm=adapter)
            state = AgentState(user_message="original", prompt_context={"agent_mode": "plan"})
            await builder.start_turn("original", state)
            await builder.compact(restore_state=state)
            await builder.build(state)
            before = deepcopy(adapter._build_responses_input(builder._history))
            assert "mode: plan" in before[-1]["content"]
            await builder.build(state)
            assert adapter._build_responses_input(builder._history) == before
            state.prompt_context["agent_mode"] = "build"
            await builder.build(state)
            after = adapter._build_responses_input(builder._history)
            assert after[:len(before)] == before
            assert len(after) == len(before) + 1
            assert "mode: build" in after[-1]["content"]
            assert after[:len(endpoint.output)] == endpoint.output
    asyncio.run(scenario())


def test_query_engine_resumes_native_checkpoint_without_readmitting_the_user_or_old_tools(tmp_path, monkeypatch):
    from backend.agent.loop_session import AgentLoopSessionContext
    from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
    from backend.agent.run_context import RunContext
    from backend.agent.runtime import AgentRuntime
    from backend.artifact.store import ArtifactStore
    from backend.config import PermissionSettings, TokenBudget
    from backend.permissions.checker import PermissionChecker
    from backend.permissions.context import PermissionContext
    from backend.tools.registry import ToolRegistry

    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    async def scenario():
        endpoint = Endpoint()
        endpoint.answer = "Contract A17 is preserved. The previous command was already completed."
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(settings(), http_client=client)
            budget = TokenBudget(total=96000, response_reserve=4096)
            builder = ContextBuilder(llm=adapter, token_budget=budget)
            original_request = "Preserve contract A17."
            await builder.start_turn(original_request, AgentState(user_message=original_request))
            await builder.compact()
            snapshot = builder.export_snapshot()
            save_checkpoint(session_id="native-resume", conversation_id="native-conv", run_id="previous-run", user_message=original_request,
                iterations=1, reply="", messages=snapshot["history"], context_snapshot=snapshot, tool_calls=[], active_skills=[], disabled_tools=set(), stopped_reason="timeout", last_mutation_index=0)
            runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
            journal = ExecutionJournal("native-resume", base_dir=tmp_path / "journals")
            session = AgentSession(llm=adapter, tool_registry=ToolRegistry(), artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
                permission_checker=PermissionChecker(PermissionSettings(), tmp_path), agent_settings=AgentSettings(max_iterations=2, max_turn_seconds=10),
                token_budget=budget, context_builder=ContextBuilder(llm=adapter, token_budget=budget))
            state = AgentState(user_message=original_request, conversation_id="native-conv", workspace_root=tmp_path)
            try:
                events = [event async for event in QueryEngine().submit(QuerySubmission(user_message=original_request, state=state, session=session,
                    runtime=AgentLoopSessionContext(session_id="native-resume", workspace_root=tmp_path, permission_context=PermissionContext(mode="bypass"),
                        metadata={"resume_from_checkpoint": True, "conversation_id": "native-conv"}, run_context=RunContext(agent_runtime=runtime, execution_journal=journal))))]
                assert state.terminal_status == "completed", state.stopped_reason
                assert not state.tool_calls
                assert not any(event.type == "tool_call" for event in events)
                sent = next(body["input"] for url, body, _ in endpoint.requests if not url.endswith("/compact"))
                assert sent[:len(endpoint.output)] == endpoint.output
                assert not any(item.get("role") == "user" and isinstance(item.get("content"), str) and item["content"].rstrip().endswith(original_request) for item in sent)
            finally:
                await session.aclose()
                runtime.close(release_lease=True)
    asyncio.run(scenario())
