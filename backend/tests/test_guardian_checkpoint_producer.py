from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from backend.agent.context import ContextBuilder
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.state import AgentState
from backend.config import AppConfig, LLMSettings, load_llm_settings
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import load_feature_flags
from backend.llm.base import LLMMessage
from backend.llm.model_runtime import ModelRuntime
from backend.llm.native_compaction import native_compaction_windows
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.services.llm_adapter_factory import build_provider_adapter
from backend.tests.test_native_compaction import Endpoint


@pytest.mark.parametrize("source", ["settings", "models_json", "extension"])
def test_admitted_model_hash_reaches_the_actual_selected_compactor(tmp_path, monkeypatch, source):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    metadata = {"native_compaction": True, "comp_hash": "issuing-catalog-hash"}
    if source == "settings":
        config = {"llm": {"provider": "openai", "openai": {"api_key": "fixture",
            "base_url": "https://fixture.invalid/v1", "model": "alpha", "wire_api": "responses",
            "available_models": ["alpha"], "model_metadata": {"alpha": metadata}}}}
        assert load_llm_settings(config).comp_hash == metadata["comp_hash"]
        runtime = ModelRuntime(settings_snapshot=config, provider_configs={}, models_path=tmp_path / "models.json")
        provider = "openai"
    else:
        provider = "fixture"
        profile = {"api_key": "fixture", "models": [{"id": "alpha", "api": "openai-responses",
            "base_url": "https://fixture.invalid/v1", "context_window": 128000, "max_tokens": 8000, **metadata}]}
        if source == "models_json":
            path = tmp_path / "models.json"
            path.write_text(json.dumps({"providers": {provider: profile}}), encoding="utf-8")
            runtime = ModelRuntime(models_path=path)
        else:
            runtime = ModelRuntime(provider_configs={}, models_path=tmp_path / "models.json")
            runtime.register_provider(provider, profile)
    adapter = build_provider_adapter(provider, model_override="alpha", model_runtime=runtime)
    try:
        assert adapter._settings.comp_hash == "issuing-catalog-hash"
        assert runtime.resolve_adapter_spec(provider, model_id="alpha").comp_hash == "issuing-catalog-hash"
        assert runtime.get_model(provider, "alpha").to_extension_dict()["comp_hash"] == "issuing-catalog-hash"
    finally:
        asyncio.run(adapter.aclose())


def test_in_flight_compaction_and_cold_restore_keep_the_actual_issuing_hash(tmp_path):
    async def scenario():
        entered = asyncio.Event()
        finish = asyncio.Event()
        requests = []

        async def compact(request):
            requests.append(json.loads(request.content))
            entered.set()
            await finish.wait()
            return httpx.Response(200, json={"id": "cmp-response", "output": [
                {"type": "compaction", "id": "cmp-first", "encrypted_content": "actual-opaque-fixture"}],
                "usage": {"input_tokens": 30, "output_tokens": 4}})

        settings = LLMSettings(api_key="fixture", provider="openai", model="alpha",
            base_url="https://fixture.invalid/v1", wire_api="responses", native_compaction=True,
            comp_hash="producer-alpha", max_tokens=1000)
        async with httpx.AsyncClient(transport=httpx.MockTransport(compact)) as client:
            adapter = OpenAIAdapter(settings, http_client=client)
            builder = ContextBuilder(llm=adapter)
            await builder.start_turn("Keep the actual request", AgentState(user_message="Keep the actual request"))
            task = asyncio.create_task(builder.compact())
            await entered.wait()
            adapter._settings = replace(settings, model="beta", comp_hash="later-beta")
            finish.set()
            await task
            assert requests[0]["model"] == "alpha"
            assert "comp_hash" not in requests[0]
            snapshot = builder.export_snapshot()
            repo = ConversationRepository(tmp_path / "conversations")
            saved = repo.create_conversation(context_snapshot=snapshot)
            cold = ConversationRepository(tmp_path / "conversations").get_conversation(saved.id)
            restored = ContextBuilder(llm=adapter)
            restored.load_snapshot(cold.context_snapshot)
            window = native_compaction_windows(restored._history[0])[0]
            assert window["compaction_model_hash"] == "producer-alpha"
            assert restored.guardian_evidence.parent_compaction.model_hash == "producer-alpha"
            info = ModelDefinition(provider="openai", id="beta", name="beta", api="openai-responses",
                base_url=settings.base_url, comp_hash="later-beta")
            model = ModelExecutionSnapshot(config=AppConfig(llm=adapter._settings), llm=adapter,
                provider="openai", model="beta", model_info=info)
            restored.bind_guardian_model(model, restored=True)
            assert restored.guardian_evidence.context_mode == "legacy"
            assert restored.guardian_evidence.parent_compaction.model_hash == "producer-alpha"
            unknown = builder.export_snapshot()
            unknown["history"][0]["provider_items"][0].pop("compaction_model_hash")
            unknown["guardian_evidence"]["parent_compaction"]["model_hash"] = None
            restored.load_snapshot(unknown)
            restored.bind_guardian_model(model, restored=True)
            assert "compaction_model_hash" not in native_compaction_windows(restored._history[0])[0]
            assert restored.guardian_evidence.parent_compaction.model_hash is None

    asyncio.run(scenario())


def test_typed_encrypted_agent_message_reaches_responses_input_without_json_text():
    async def scenario():
        endpoint = Endpoint()
        item = {"type": "agent_message", "id": "amsg-actual", "author": "/root/worker",
            "recipient": "/root", "content": [{"type": "encrypted_content", "encrypted_content": "opaque-agent-fixture"}],
            "internal_chat_message_metadata_passthrough": {"turn_id": "actual-turn"}}
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            adapter = OpenAIAdapter(LLMSettings(api_key="fixture", provider="openai", model="alpha",
                base_url="https://fixture.invalid/v1", wire_api="responses"), http_client=client)
            messages = [LLMMessage(role="assistant", content="", provider_items=[item]), LLMMessage(role="user", content="Review this action")]
            async for _ in adapter.stream_chat(messages):
                pass
            sent = endpoint.requests[-1][1]["input"]
            assert sent[0] == item
            assert sent[1]["role"] == "user"
            assert sent[1]["content"] == "Review this action"
            assert messages[0].provider_items == [item]

    asyncio.run(scenario())


def test_source_stable_checkpoint_policy_has_a_real_default_without_a_desktop_switch(monkeypatch):
    import backend.services.feature_flag_settings_service as service

    monkeypatch.setattr(service, "_load_settings_json", lambda: {})
    monkeypatch.setattr(service, "get_config_requirements", lambda: SimpleNamespace(feature_requirements={}))
    payload = service.get_feature_flag_settings()
    assert payload["effective"]["guardian_reuse_parent_compaction"]["enabled"] is True
    assert all(flag["name"] != "guardian_reuse_parent_compaction" for flag in payload["flags"])
    disabled = load_feature_flags({"features": {"guardian_reuse_parent_compaction": False}}, managed_requirements={})
    assert not disabled.enabled("guardian_reuse_parent_compaction")
