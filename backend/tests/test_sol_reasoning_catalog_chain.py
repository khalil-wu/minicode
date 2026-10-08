from __future__ import annotations

import json

import httpx
import pytest

from backend.config import get_provider_model_metadata
from backend.llm.base import LLMMessage, StreamEventType
from backend.llm.capabilities import capabilities_from_openai_settings
from backend.llm.model_runtime_definitions import _base_model
from backend.llm.openai_adapter import OpenAIAdapter
from backend.services.llm_adapter_factory import _openai_compatible_settings
from backend.services.llm_config_service import llm_model_updated_payload


SOL_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")


@pytest.mark.parametrize(("model", "default"), [("gpt-6.1-sol", "low"), ("openai/gpt-6.1-sol", "low"), ("gpt-6-sol", "medium")])
def test_sol_catalog_projects_the_actual_reasoning_ladder(model, default, monkeypatch):
    section = {"model": model, "wire_api": "responses", "model_metadata": {}}
    metadata = get_provider_model_metadata(section, model)
    assert tuple(metadata["reasoning_effort_levels"]) == SOL_EFFORTS
    assert metadata["default_reasoning_effort"] == default
    assert metadata["max_context_window"] == 872_000
    definition = _base_model("custom", model, api="openai-responses", base_url="https://example.test/v1", max_tokens=0, settings=section)
    assert definition.reasoning is True
    assert definition.reasoning_effort_levels == SOL_EFFORTS
    monkeypatch.setattr("backend.services.llm_config_service.get_custom_settings", lambda **_kwargs: section)
    event = llm_model_updated_payload(provider="custom", selected_model=model, available_models=[model], workspace_root="C:/repo")
    assert event["reasoning_effort_supported"] is True
    assert tuple(event["reasoning_effort_levels"]) == SOL_EFFORTS
    assert event["effective_reasoning_effort"] == default


def test_sol_catalog_does_not_assign_a_reasoning_ladder_to_custom_name_lookalikes():
    assert get_provider_model_metadata({}, "my-gpt-6.1-sol")["reasoning_effort_levels"] == []
    assert get_provider_model_metadata({}, "gpt-6.1-sol-custom")["reasoning_effort_levels"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_api", ["responses", "chat"])
@pytest.mark.parametrize("effort", SOL_EFFORTS)
async def test_each_sol_reasoning_level_reaches_the_actual_provider_request(wire_api, effort):
    requests = []

    async def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if wire_api == "responses":
            event = {"type": "response.completed", "response": {"id": "reasoning-fixture", "status": "completed", "model": "gpt-6.1-sol",
                "output": [{"type": "message", "id": "answer", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "Done", "annotations": []}]}]}}
            body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
        else:
            event = {"id": "reasoning-fixture", "object": "chat.completion.chunk", "model": "gpt-6.1-sol",
                "choices": [{"index": 0, "delta": {"content": "Done"}, "finish_reason": "stop"}]}
            body = "data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode(), request=request)

    settings = _openai_compatible_settings({"api_key": "fixture-key", "base_url": "https://example.test/v1", "model": "gpt-6.1-sol",
        "wire_api": wire_api, "reasoning_effort": effort, "model_metadata": {}, "proxy_mode": "direct"}, provider="custom", model_override="gpt-6.1-sol")
    capabilities = capabilities_from_openai_settings(settings, provider="custom")
    assert capabilities.reasoning_effort_levels == SOL_EFFORTS
    assert capabilities.effective_reasoning_effort == effort
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        adapter = OpenAIAdapter(settings, http_client=client)
        try:
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="Say Done")])]
        finally:
            await adapter.aclose()
    assert len(requests) == 1
    assert requests[0]["model"] == "gpt-6.1-sol"
    assert (requests[0]["reasoning"]["effort"] if wire_api == "responses" else requests[0]["reasoning_effort"]) == ("xhigh" if effort == "ultra" else effort)
    assert capabilities.wire_reasoning_effort == ("xhigh" if effort == "ultra" else effort)
    assert any(event.type == StreamEventType.DONE for event in events)
    assert not any(event.type == StreamEventType.ERROR for event in events)
