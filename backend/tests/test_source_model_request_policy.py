from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMMessage
from backend.llm.model_request_policy import ModelRequestPolicy, resolve_model_request_policy
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.services import llm_adapter_factory
from backend.tools.subagent_support import _resolve_subagent_llm


def config_for_role(tmp_path, *, root_tier=None, wire="responses"):
    path = tmp_path / "worker.toml"
    path.write_text('developer_instructions="Role instructions"\nmodel_reasoning_summary="detailed"\nmodel_verbosity="high"\nservice_tier="priority"\n', encoding="utf-8")
    effective = {"agents": {"worker": {"description": "Wire worker", "config_file": "worker.toml"}}}
    if root_tier is not None:
        effective["service_tier"] = root_tier
    return AppConfig(llm=LLMSettings(api_key="test", provider="custom", model="gpt-6.1-sol", base_url="https://example.test/v1", wire_api=wire,
        reasoning_effort="low", reasoning_effort_levels=("low", "medium", "high", "xhigh", "max", "ultra")),
        feature_flags=FeatureFlags({}), config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")), effective),)))


def completed_wire(wire: str):
    if wire == "responses":
        return [{"type": "response.completed", "response": {"id": "response", "status": "completed", "model": "gpt-6.1-sol",
            "output": [{"type": "message", "id": "answer", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Done", "annotations": []}]}]}}]
    return [{"id": "chat", "model": "gpt-6.1-sol", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Done"}, "finish_reason": None}]},
        {"id": "chat", "model": "gpt-6.1-sol", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["responses", "chat"])
@pytest.mark.parametrize("operation", ["stream", "simple"])
@pytest.mark.parametrize("root_tier", [None, "priority"])
async def test_role_settings_reach_real_transport_and_do_not_mutate_parent(tmp_path, monkeypatch, wire, operation, root_tier):
    requests = []
    async def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in completed_wire(wire)) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(llm_adapter_factory, "build_wire_adapter", lambda settings, **kwargs: OpenAIAdapter(settings, http_client=client))
        config = config_for_role(tmp_path, root_tier=root_tier, wire=wire)
        parent = llm_adapter_factory.create_session_llm(config)
        owner = RunContext(model_execution=ModelExecutionSnapshot(config=config, llm=parent, provider="custom", model="gpt-6.1-sol", thinking_level="low"))
        child = await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="worker", workspace_root=tmp_path)
        try:
            assert child.owns_llm and child.llm is not parent
            assert parent.model_request_policy().verbosity == "low"
            assert child.llm.model_request_policy().verbosity == "high"
            messages = [LLMMessage(role="user", content="Answer")]
            if operation == "stream":
                _ = [event async for event in child.llm.stream_chat(messages)]
            else:
                assert await child.llm.simple_chat(messages) == "Done"
            payload = requests[-1]
            if wire == "responses":
                assert payload["reasoning"]["summary"] == "detailed"
                assert payload["text"]["verbosity"] == "high"
            else:
                assert payload["verbosity"] == "high"
                assert "reasoning" not in payload
            assert payload.get("service_tier") == root_tier
            assert child.config.model_request_policy.service_tier == root_tier
            # Source propagates the root user's later selection to existing children.
            changed = dict(config.config_layer_stack.layers[0].config)
            changed["service_tier"] = "default"
            next_config = replace(config, config_layer_stack=ConfigLayerStack((replace(config.config_layer_stack.layers[0], config=changed),)))
            owner.active_model_execution = replace(owner.model_execution, config=next_config)
            _ = [event async for event in child.llm.stream_chat(messages)]
            assert "service_tier" not in requests[-1]
            assert parent.model_request_policy().verbosity == "low"
        finally:
            await child.llm.aclose()
            await parent.aclose()


def test_unsupported_model_options_are_omitted_and_output_schema_is_retained():
    model = ModelDefinition(provider="custom", id="custom-model", name="Custom", api="openai-responses", base_url="https://example.test/v1",
        extra={"support_verbosity": False, "supports_reasoning_summary_parameter": False, "service_tiers": []})
    config = AppConfig(llm=LLMSettings(api_key="test"), config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"), {"model_verbosity": "high", "model_reasoning_summary": "detailed"}),)))
    policy = resolve_model_request_policy(config, model=model.id, definition=model, root_service_tier="priority")
    adapter = OpenAIAdapter(LLMSettings(api_key="test", model=model.id, wire_api="responses"))
    adapter.apply_model_request_policy(policy)
    payload = {"model": model.id, "reasoning": {"effort": "high", "summary": "auto"}, "text": {"format": {"type": "json_schema"}}}
    adapter._apply_model_request_options(payload, model=model.id, responses=True)
    assert payload == {"model": model.id, "reasoning": {"effort": "high"}, "text": {"format": {"type": "json_schema"}}}
    assert policy.service_tier_for_request("flex") == "flex"
    assert policy.service_tier_for_request("default") is None


def test_responses_verbosity_coexists_with_schema_and_summary_none_removes_only_summary():
    adapter = OpenAIAdapter(LLMSettings(api_key="test", model="gpt-6.1-sol", wire_api="responses"))
    adapter.apply_model_request_policy(ModelRequestPolicy(model="gpt-6.1-sol", reasoning_summary="none", verbosity="low"))
    payload = {"reasoning": {"effort": "high", "summary": "auto"}, "text": {"format": {"type": "json_schema"}}}
    adapter._apply_model_request_options(payload, model="gpt-6.1-sol", responses=True)
    assert payload["reasoning"] == {"effort": "high"}
    assert payload["text"] == {"format": {"type": "json_schema"}, "verbosity": "low"}
    separate = {"reasoning": {"summary": "auto"}}
    adapter._apply_model_request_options(separate, model="different-side-model", responses=True)
    assert separate == {"reasoning": {"summary": "auto"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["responses", "chat"])
@pytest.mark.parametrize("strict", [True, False])
async def test_typed_final_schema_reaches_real_stream_transport_alongside_verbosity(wire, strict):
    requests = []
    async def respond(request):
        requests.append(json.loads(request.content))
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in completed_wire(wire)) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode(), request=request)

    schema = {"type": "object", "properties": {"decision": {"type": "string"}}, "required": ["decision"], "additionalProperties": False}
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="test", base_url="https://example.test/v1", model="gpt-6.1-sol", wire_api=wire), http_client=client)
        adapter.apply_model_request_policy(ModelRequestPolicy(model="gpt-6.1-sol", verbosity="low", final_output_json_schema=schema, final_output_json_schema_strict=strict))
        _ = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="Review")])]
        payload = requests[0]
        expected = {"name": "codex_output_schema", "strict": strict, "schema": schema}
        if wire == "responses":
            assert payload["text"] == {"verbosity": "low", "format": {"type": "json_schema", **expected}}
        else:
            assert payload["verbosity"] == "low"
            assert payload["response_format"] == {"type": "json_schema", "json_schema": expected}
        await adapter.aclose()


@pytest.mark.asyncio
async def test_source_default_child_owns_transport_and_observes_root_tier_change_without_role_override(tmp_path, monkeypatch):
    requests = []
    async def respond(request):
        requests.append(json.loads(request.content))
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in completed_wire("responses")) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(llm_adapter_factory, "build_wire_adapter", lambda settings, **kwargs: OpenAIAdapter(settings, http_client=client))
        config = config_for_role(tmp_path, root_tier="priority")
        parent = llm_adapter_factory.create_session_llm(config)
        owner = RunContext(model_execution=ModelExecutionSnapshot(config=config, llm=parent, provider="custom", model="gpt-6.1-sol", thinking_level="low"))
        child = await _resolve_subagent_llm(parent, parent_metadata={}, run_context=owner, agent_type="default", role_requested=False, workspace_root=tmp_path)
        try:
            assert child.owns_llm and child.llm is not parent
            assert child.llm.model_request_policy().verbosity == parent.model_request_policy().verbosity
            _ = [event async for event in child.llm.stream_chat([LLMMessage(role="user", content="Answer")])]
            assert requests[-1]["service_tier"] == "priority"
            changed = dict(config.config_layer_stack.layers[0].config)
            changed.pop("service_tier")
            next_config = replace(config, config_layer_stack=ConfigLayerStack((replace(config.config_layer_stack.layers[0], config=changed),)))
            owner.active_model_execution = replace(owner.model_execution, config=next_config)
            _ = [event async for event in child.llm.stream_chat([LLMMessage(role="user", content="Continue")])]
            assert "service_tier" not in requests[-1]
        finally:
            await child.llm.aclose()
            await parent.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["stream", "simple"])
async def test_guardian_cache_override_changes_wire_key_and_keeps_private_session_id(operation):
    requests = []
    async def respond(request):
        requests.append(json.loads(request.content))
        stream = "".join("data: " + json.dumps(event) + "\n\n" for event in completed_wire("responses")) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream.encode(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(LLMSettings(api_key="test", base_url="https://example.test/v1", model="gpt-6.1-sol", wire_api="responses"), http_client=client)
        adapter.apply_model_request_policy(ModelRequestPolicy(model="gpt-6.1-sol", prompt_cache_key_override="guardian:parent-thread"))
        messages = [LLMMessage(role="user", content="Review")]
        metadata = {"session_id": "guardian-private-session", "conversation_id": "guardian-private-thread"}
        if operation == "stream":
            _ = [event async for event in adapter.stream_chat(messages, metadata=metadata)]
        else:
            from backend.llm.base import LLMSideCallContext, SideQueryOptions
            assert await adapter._side_query_chat(messages, context=LLMSideCallContext(
                options=SideQueryOptions(operation="review", session_id=metadata["session_id"], thread_id=metadata["conversation_id"]), record={})) == "Done"
        assert requests[0]["prompt_cache_key"] == "guardian:parent-thread"
        assert requests[0]["client_metadata"]["session_id"] == "guardian-private-session"
        assert metadata["session_id"] == "guardian-private-session"
        await adapter.aclose()
