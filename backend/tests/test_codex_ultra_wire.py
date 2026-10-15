from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.codex_prompts import codex_model_instructions, codex_multi_agent_mode
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.model_runtime_definitions import _base_model
from backend.llm.model_selection import apply_model_thinking_level
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.reasoning_effort import reasoning_effort_wire_value
from backend.services.llm_adapter_factory import _openai_compatible_settings
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.ws.handler import WebSocketSession


@pytest.mark.parametrize(("model", "wire"), [("gpt-6.1-sol", "xhigh"), ("openai/gpt-6.1-sol", "xhigh"),
    ("gpt-6-astra", "xhigh"), ("gpt-6-sol", "max"), ("gpt-5.6-sol", "max"), ("gpt-5.6-terra", "max")])
def test_ultra_is_the_official_model_owned_collaboration_alias(model, wire):
    assert reasoning_effort_wire_value(model, "ultra") == wire
    assert reasoning_effort_wire_value(model, "high") == "high"
    assert reasoning_effort_wire_value("custom-native-ultra", "ultra") == "ultra"


@pytest.mark.asyncio
async def test_real_error_contract_is_avoided_on_the_first_request_without_disabling_tools():
    requests = []
    source = {"model": "gpt-6.1-sol", "api_key": "fixture-key", "base_url": "https://fixture.test/v1", "wire_api": "responses",
        "reasoning_effort": "ultra", "proxy_mode": "direct", "model_metadata": {}}
    settings = _openai_compatible_settings(source, provider="custom", model_override="gpt-6.1-sol")
    definition = _base_model("custom", "gpt-6.1-sol", api="openai-responses", base_url=source["base_url"], max_tokens=0, settings=source)
    tools = [{"type": "function", "function": {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}}
        for name in ["tool_exec", "tool_search", "tool_wait", "update_plan"]]

    async def endpoint(request):
        body = json.loads(request.content)
        requests.append(body)
        assert body["reasoning"]["effort"] == "xhigh"
        assert len(body["tools"]) == 4
        assert {item["name"] for item in body["tools"] if item["type"] == "function"} == {"tool_exec", "tool_wait", "update_plan"}
        assert [item for item in body["tools"] if item["type"] == "tool_search"] == [{
            "type": "tool_search", "execution": "client", "description": "tool_search",
            "parameters": {"type": "object", "properties": {}},
        }]
        assert codex_multi_agent_mode("gpt-6.1-sol", True) in body["instructions"]
        event = {"type": "response.completed", "response": {"id": "ultra-wire-fixture", "status": "completed", "output": [
            {"type": "message", "id": "answer", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "OK", "annotations": []}]}]}}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())

    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        adapter = OpenAIAdapter(settings, http_client=client)
        try:
            assert apply_model_thinking_level(adapter, definition, "ultra") == "ultra"
            capabilities = adapter.capabilities.to_dict()
            assert adapter.current_reasoning_effort() == "ultra"
            assert capabilities["effective_reasoning_effort"] == "ultra"
            assert capabilities["wire_reasoning_effort"] == "xhigh"
            assert capabilities["reasoning_effort_levels"] == ["low", "medium", "high", "xhigh", "max", "ultra"]
            assert capabilities["wire_reasoning_effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
            builder = ContextBuilder(llm=adapter)
            state = AgentState(user_message="Only reply OK")
            await builder.start_turn(state.user_message, state)
            messages = await builder.build(state)
            assert next(message.content for message in messages if message.role == "system").startswith(codex_model_instructions("gpt-6.1-sol"))
            assert any(message.role == "developer" and message.content == codex_multi_agent_mode("gpt-6.1-sol", True) for message in messages)
            events = [event async for event in adapter.stream_chat(messages, tools=tools)]
            assert len(requests) == 1
            assert any(event.type == StreamEventType.DONE for event in events)
            assert not any(event.type == StreamEventType.ERROR for event in events)
            apply_model_thinking_level(adapter, definition, "medium")
            later = await builder.build(AgentState(user_message="Only reply OK"))
            assert any(message.role == "developer" and message.content == codex_multi_agent_mode("gpt-6.1-sol", False) for message in later)
            assert not any(codex_multi_agent_mode("gpt-6.1-sol", True) in message.content for message in later)
        finally:
            await adapter.aclose()


@pytest.mark.parametrize(("configured", "canonical", "wire"), [("ultra", "ultra", "xhigh"), ("medium", "medium", "medium"), ("", "low", "low")])
def test_restored_session_projects_the_saved_policy_without_rebinding(configured, canonical, wire):
    source = {"model": "gpt-6.1-sol", "api_key": "fixture-key", "base_url": "https://fixture.test/v1", "wire_api": "responses",
        "reasoning_effort": "", "proxy_mode": "direct", "model_metadata": {}}
    config = AppConfig(llm=LLMSettings(api_key="fixture-key", provider="custom", model=source["model"], reasoning_effort=configured),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"llm": {"custom": source}}),)))
    selected_client = object()
    session = SimpleNamespace(config=config, provider="custom", llm=selected_client, selected_model=source["model"],
        available_models=[source["model"]], models_source="settings", active_conversation_id="restored-ultra",
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: None),
        _model_runtime_for_conversation=lambda _: None, default_model_selection=lambda _: (config, None),
        conversation_repo=SimpleNamespace(get_conversation_summary=lambda _: SimpleNamespace(model_selection={"selection_mode": "model"})),
        _model_override_active=True)
    session._llm_selection_payload = lambda: WebSocketSession._llm_selection_payload(session)
    caps = WebSocketSession._provider_capabilities_payload(session)
    assert caps["effective_reasoning_effort"] == canonical
    assert caps["wire_reasoning_effort"] == wire
    assert caps["configured_reasoning_effort"] == session.config.llm.reasoning_effort == configured
    assert caps["model_selection_mode"] == "model"
    assert caps["default_model_selection"] is None and caps["available_model_options"] == []
    assert session.llm is selected_client
