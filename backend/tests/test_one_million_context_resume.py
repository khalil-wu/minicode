from __future__ import annotations

import json

import pytest

from backend.agent.checkpoint import load_latest_checkpoint
from backend.agent.context import ContextBuilder
from backend.agent.query_recovery import prepare_query_recovery
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.turn_kernel import TurnKernel
from backend.config import AppConfig, LLMSettings, TokenBudget, get_provider_model_metadata
from backend.llm.base import LLMMessage, ToolCallEvent
from backend.llm.model_runtime import ModelRuntime
from backend.llm.model_selection import config_with_model_budget
from backend.services.llm_adapter_factory import build_wire_adapter


@pytest.mark.parametrize("model, capacity", [
    ("gpt-6-luna", 1_050_000), ("openai/gpt-5.5", 272_000),
    ("gpt-4o", 128_000), ("claude-sonnet-4-6", 200_000),
    ("llama-3-8b", 8_000), ("llama-3.1-70b", 128_000),
    ("claude-sonnet-4-6[1m]", 1_000_000), ("gateway-model", 1_000_000),
])
def test_application_default_does_not_relabel_published_capacity(monkeypatch, model, capacity):
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)
    metadata = get_provider_model_metadata({}, model)
    assert metadata["context_window"] == 1_000_000
    assert metadata["context_window_verified"] is False
    assert metadata["max_context_window"] == capacity
    assert metadata["max_context_window_verified"] is (model != "gateway-model")


@pytest.mark.parametrize("wire_api", ["chat", "responses", "anthropic"])
def test_wire_adapters_preserve_default_window_provenance(wire_api, monkeypatch):
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)
    metadata = get_provider_model_metadata({}, "claude-sonnet-4-6")
    settings = LLMSettings(api_key="test-only", model="claude-sonnet-4-6", wire_api=wire_api,
        **{key: metadata[key] for key in (
            "context_window", "context_window_source", "context_window_verified",
            "max_context_window", "max_context_window_source", "max_context_window_verified",
        )})
    adapter = build_wire_adapter(settings)
    assert adapter.capabilities.context_window == 1_000_000
    assert adapter.capabilities.context_window_verified is False
    assert adapter.capabilities.max_context_window == 200_000


def test_models_json_and_extension_defaults_reach_selected_turn_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)
    path = tmp_path / "models.json"
    definition = {"id": "test-model", "api": "openai-completions", "base_url": "https://example.invalid/v1"}
    path.write_text(json.dumps({"providers": {"file-provider": {"api_key": "test-only", "models": [definition]}}}))
    runtime = ModelRuntime(models_path=path)
    runtime.register_provider("extension-provider", {"api_key": "test-only", "models": [definition]})
    config = AppConfig(llm=LLMSettings(api_key="test-only"), token_budget=TokenBudget(total=32_000))
    for provider in ("file-provider", "extension-provider"):
        model = runtime.get_model(provider, "test-model")
        assert model.context_window == 1_000_000
        assert model.context_window_verified is False
        selected = config_with_model_budget(config, model_runtime=runtime, provider=provider, model="test-model")
        assert selected.token_budget.total == 1_000_000
        assert selected.token_budget.reserved_response_tokens == 16_384
        assert config.token_budget.total == 32_000


def test_explicit_provider_window_and_maximum_remain_independent(monkeypatch):
    monkeypatch.delenv("MINICODE_MAX_CONTEXT_TOKENS", raising=False)
    section = {"model_metadata": {"gateway-model": {"context_window": 32_000, "max_context_window": 64_000}}}
    metadata = get_provider_model_metadata(section, "gateway-model")
    assert (metadata["context_window"], metadata["max_context_window"]) == (32_000, 64_000)
    assert metadata["context_window_verified"] is True
    monkeypatch.setenv("MINICODE_MAX_CONTEXT_TOKENS", "1000000")
    assert get_provider_model_metadata(section, "gateway-model")["context_window"] == 64_000


def test_extension_capacity_survives_model_override_and_ui_projection():
    from backend.services.llm_config_service import llm_model_updated_payload

    runtime = ModelRuntime(provider_configs={"capacity-provider": {
        "model_overrides": {"limited-model": {"context_window": 1_000_000}},
    }})
    runtime.register_provider("capacity-provider", {"api_key": "test-only", "models": [{
        "id": "limited-model", "api": "openai-completions", "base_url": "https://example.invalid/v1",
        "context_window": 32_000, "max_context_window": 64_000,
    }]})
    selected = runtime.get_model("capacity-provider", "limited-model")
    assert selected.context_window == 1_000_000
    assert selected.max_context_window == 64_000
    payload = llm_model_updated_payload(provider="capacity-provider", selected_model="limited-model",
        available_models=["limited-model"], workspace_root="", provider_metadata={
            "models_source": "extension", "context_window": selected.context_window,
            "max_context_window": selected.max_context_window,
            "max_context_window_verified": selected.max_context_window_verified,
        })
    assert payload["context_window"] == 1_000_000
    assert payload["max_context_window"] == 64_000
    assert payload["max_context_window_verified"] is True


def test_stopped_long_turn_restores_full_context_via_production_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path))
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm",
                           enable_lease_heartbeat=False)
    context = ContextBuilder(token_budget=TokenBudget())
    context.append_user("Original requirement: preserve all invoices and never change rounding.")
    for index in range(200):
        context._history_store.append(LLMMessage(role="assistant", content=f"step {index}: " + "source code\n" * 1800))
    context._history_store.append(LLMMessage(role="assistant", tool_calls=[
        ToolCallEvent(id="already-executed", name="run_command", arguments={"command": "verify"}),
    ]))
    context._history_store.append(LLMMessage(role="tool", tool_call_id="already-executed", content="verified: exit 0"))
    context.append_user("Continue checking rollback and concurrency.")
    before = context.export_snapshot()
    assert len(json.dumps(before)) > 2 * 1024 * 1024
    state = AgentState(user_message="continue", conversation_id="long-context", stopped_reason="interrupted")
    kernel = TurnKernel.create(metadata={}, state=state, budget=TokenBudget(), task_id="long-context",
        session_id="long-session", emit_event=None, initial_user_message="continue",
        run_context=RunContext(agent_runtime=runtime))
    try:
        assert kernel.finalize_checkpoint(session_id="long-session", user_message="continue",
            state=state, context_builder=context) == "saved"
        checkpoint = load_latest_checkpoint("long-session", conversation_id="long-context")
        assert checkpoint.context_snapshot["history"] == before["history"]
        restored = ContextBuilder(token_budget=TokenBudget())
        resumed_state = AgentState(user_message="continue")
        metadata = {"resume_from_checkpoint": True}
        result = prepare_query_recovery(session_id="long-session", conversation_id="long-context",
            metadata=metadata, state=resumed_state, context_builder=restored, max_iterations_budget=10,
            current_run_id="new-run")
        assert result.restored
        assert restored.export_snapshot()["history"] == before["history"]
        assert metadata["run_id"] == "new-run"
        assert metadata["checkpoint_origin"]["run_id"] == kernel.run_record.run_id
    finally:
        runtime.close(release_lease=True)
