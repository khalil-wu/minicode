from __future__ import annotations

from contextlib import aclosing
from dataclasses import replace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.context_window import ContextWindowConfig
from backend.agent.loop_session import AgentLoopSessionContext
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.token_budget_activation import (
    config_for_model_switch,
    experimental_context_is_eligible,
    prepare_token_budget_for_startup,
    register_context_window_tools,
    resolve_context_window_config,
)
from backend.artifact.store import ArtifactStore
from backend.config import (
    AgentSettings,
    AppConfig,
    LLMSettings,
    PermissionSettings,
    TokenBudget,
)
from backend.feature_flags import FeatureFlags
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.config_requirements import ConfigRequirements
from backend.llm.base import ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition, TokenBudgetAuthContext
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.tool_registry_factory import build_tool_registry
from backend.tests.test_codex_context_window import WindowModel
from backend.tools.registry import ToolRegistry


def defaults(prefix="A", *, enabled=True, notes=False):
    return {"enabled": enabled, "use_history_notes_extension": notes,
        "reminder_threshold_tokens": 6144, "reminder_message_template": prefix + " {n_remaining} tokens",
        "guidance_message": prefix + " guidance", "auto_compact_fallback_prompt": prefix + " fallback",
        "auto_compact_fallback_buffer_tokens": 16384}


def model(prefix="A", *, enabled=True, notes=False, experimental=False):
    return ModelDefinition(provider="fixture", id=prefix, name=prefix, api="openai-responses", base_url="http://127.0.0.1",
        context_window=100000, context_window_source="declared", context_window_verified=True,
        extra={"model_messages": {"token_budget": defaults(prefix, enabled=enabled, notes=notes)}},
        supports_experimental_context=experimental)


def config(*, raw=None, pinned=None, enabled=False, seed=None):
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), raw or {}),),
        requirements=ConfigRequirements(raw={"features": pinned or {}}))
    return AppConfig(llm=LLMSettings(api_key="fixture-key"), feature_flags=FeatureFlags({"token_budget": enabled, "context_management": True}),
        config_layer_stack=stack, context_window_config=seed)


def auth(**changes):
    return replace(TokenBudgetAuthContext(auth_mode="chatgpt", account_plan_type="pro",
        supports_codex_backend_routes=True, requires_openai_auth=True), **changes)


def test_model_owned_activation_preserves_configured_startup_and_unresolved_seed():
    original = config()
    started = prepare_token_budget_for_startup(original, model(notes=True))
    assert started.feature_flags.enabled("token_budget") and not original.feature_flags.enabled("token_budget")
    assert started.token_budget_startup_config.enabled is False
    assert started.token_budget_startup_config.token_budget is None
    assert started.context_window_config.use_history_notes_extension
    assert started.context_window_config.reminder_threshold_tokens is None
    resolved = resolve_context_window_config(started, model(notes=True))
    assert resolved.reminder_threshold_tokens == 6144 and resolved.guidance_message == "A guidance"
    assert resolved.use_history_notes_extension


@pytest.mark.parametrize("raw,pinned,expected", [
    ({"features": {"token_budget": False}}, None, False),
    ({"features": {"token_budget": {"enabled": False}}}, None, False),
    ({}, {"token_budget": False}, False), ({}, None, True),
])
def test_explicit_configuration_and_managed_off_prevent_model_activation(raw, pinned, expected):
    started = prepare_token_budget_for_startup(config(raw=raw, pinned=pinned), model())
    assert started.feature_flags.enabled("token_budget") is expected
    assert (resolve_context_window_config(started, model()) is not None) is expected


def test_enable_only_uses_model_defaults_but_explicit_prompt_preferences_win():
    base = config(raw={"features": {"token_budget": {"enabled": True}}}, enabled=True)
    started = prepare_token_budget_for_startup(base, model())
    assert resolve_context_window_config(started, model()).guidance_message == "A guidance"
    explicit = config(raw={"features": {"token_budget": {"enabled": True, "guidance_message": "USER GUIDANCE"}}}, enabled=True)
    resolved = resolve_context_window_config(prepare_token_budget_for_startup(explicit, model()), model())
    assert resolved.guidance_message == "USER GUIDANCE" and resolved.reminder_threshold_tokens is None


def test_model_switch_reresolves_raw_prompts_and_fresh_child_restores_original_preferences():
    started = prepare_token_budget_for_startup(config(), model())
    selected = config_for_model_switch(started, config())
    assert selected.token_budget_startup_config is started.token_budget_startup_config
    assert resolve_context_window_config(selected, model("B", enabled=False)).guidance_message == "B guidance"
    child = prepare_token_budget_for_startup(selected, model("B", enabled=False))
    assert not child.feature_flags.enabled("token_budget") and child.context_window_config is None
    fork = prepare_token_budget_for_startup(selected, model("B", enabled=False), inherit_activation=True)
    assert fork.feature_flags.enabled("token_budget")
    assert resolve_context_window_config(fork, model("B", enabled=False)).guidance_message == "B guidance"


@pytest.mark.parametrize("changes,expected", [
    ({}, True), ({"auth_mode": "api_key"}, False), ({"auth_mode": None}, False),
    ({"account_plan_type": "plus"}, True), ({"account_plan_type": "prolite"}, True), ({"account_plan_type": "promax"}, True),
    ({"account_plan_type": "free"}, False), ({"account_plan_type": "enterprise"}, False),
    ({"supports_codex_backend_routes": False}, False), ({"requires_openai_auth": False}, False),
    ({"has_env_key": True}, False), ({"has_experimental_bearer_token": True}, False),
    ({"has_auth": True}, False), ({"has_aws": True}, False),
])
def test_experimental_admission_uses_only_real_source_auth_capabilities(changes, expected):
    cfg, selected, credential = config(), model(enabled=False, experimental=True), auth(**changes)
    assert experimental_context_is_eligible(cfg, selected, credential) is expected
    started = prepare_token_budget_for_startup(cfg, selected, credential)
    assert started.feature_flags.enabled("token_budget") is expected
    assert bool(started.context_window_config and started.context_window_config.use_history_notes_extension) is expected


def test_experimental_admission_requires_feature_model_and_managed_permission():
    assert not experimental_context_is_eligible(config(), model(experimental=False), auth())
    disabled = replace(config(), feature_flags=FeatureFlags({"context_management": False}))
    assert not experimental_context_is_eligible(disabled, model(experimental=True), auth())
    started = prepare_token_budget_for_startup(config(pinned={"token_budget": False}), model(enabled=False, experimental=True), auth())
    assert not started.feature_flags.enabled("token_budget")


def test_invalid_model_defaults_are_ignored_without_hiding_user_configuration_errors(caplog):
    selected = model()
    bad = replace(selected, extra={"model_messages": {"token_budget": {**defaults(), "reminder_threshold_tokens": True}}})
    started = prepare_token_budget_for_startup(config(), bad)
    assert resolve_context_window_config(started, bad) == ContextWindowConfig()
    assert "ignoring invalid model-owned" in caplog.text
    with pytest.raises(ValueError, match="must be positive"):
        prepare_token_budget_for_startup(config(raw={"features": {"token_budget": {"enabled": True, "reminder_threshold_tokens": True}}}, enabled=True), selected)


def test_registry_factory_consumes_direct_sdk_declared_metadata_without_name_inference(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    try:
        cfg = AppConfig(llm=LLMSettings(api_key="fixture-key", model="unrecognized-by-name", model_messages={"token_budget": defaults()}))
        registry = build_tool_registry(store, config=cfg, workspace_root=tmp_path)
        assert registry.get_tool("new_context").config.guidance_message == "A guidance"
        assert registry.get_tool("get_context_remaining") is not None
        declared_off = replace(cfg, llm=replace(cfg.llm, model_messages={"token_budget": defaults(enabled=False)}))
        disabled = build_tool_registry(store, config=declared_off, workspace_root=tmp_path)
        assert disabled.get_tool("new_context") is None
        named_only = replace(cfg, llm=replace(cfg.llm, model="gpt-6.1-sol", model_messages=None))
        assert build_tool_registry(store, config=named_only, workspace_root=tmp_path).get_tool("new_context") is None
    finally:
        store.shutdown()


def test_live_context_binding_reresolves_model_prompts_without_resetting_window_or_transcript(tmp_path):
    first, second = WindowModel([]), WindowModel([])
    selected_a, selected_b = model(), model("B", enabled=False)
    first_snapshot = ModelExecutionSnapshot(config=config(), llm=first, provider="fixture", model="A", model_info=selected_a)
    builder = ContextBuilder(llm=first, workspace_root=tmp_path)
    prepared = builder.bind_context_window_model(first_snapshot, startup=True)
    builder.append_user("ACTUAL EXISTING USER WORK")
    window = builder._context_window_state.window_id
    history = builder.export_snapshot()["history"]
    second_snapshot = ModelExecutionSnapshot(config=config(), llm=second, provider="fixture", model="B", model_info=selected_b)
    context = ToolExecutionContext(PermissionContext(), run_context=RunContext(model_execution=second_snapshot), model_execution=second_snapshot)
    builder.bind_tool_context(context)
    builder.bind_llm(second)
    assert builder._context_window_config.guidance_message == "B guidance"
    assert builder._context_window_state.window_id == window and builder.export_snapshot()["history"] == history
    assert builder._context_window_source_config.token_budget_startup_config == prepared.config.token_budget_startup_config
    registry = ToolRegistry()
    register_context_window_tools(registry, builder._context_window_config)
    version = registry.version
    register_context_window_tools(registry, builder._context_window_config)
    assert registry.version == version
    assert registry.get_tool("new_context").config.guidance_message == "B guidance"


@pytest.mark.asyncio
async def test_real_query_startup_exposes_declared_model_tools_and_uses_raw_model_guidance(tmp_path):
    fixture = WindowModel([[ToolCallEvent(id="switch", name="new_context", arguments={})], "MODEL_OWNED_FOLLOWUP"])
    selected = model()
    cfg = config()
    snapshot = ModelExecutionSnapshot(config=cfg, llm=fixture, provider="fixture", model="A", model_info=selected)
    registry = ToolRegistry()
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    builder = ContextBuilder(llm=fixture, workspace_root=tmp_path)
    session = AgentSession(llm=fixture, tool_registry=registry, artifact_store=store,
        permission_checker=PermissionChecker(PermissionSettings(), tmp_path),
        agent_settings=AgentSettings(max_iterations=5, max_turn_seconds=20, stream_max_attempts=0, code_mode_only=False),
        token_budget=TokenBudget(total=200000), context_builder=builder)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    owner = RunContext(agent_runtime=runtime, model_execution=snapshot)
    state = AgentState(user_message="ACTUAL_MODEL_OWNED_TASK", conversation_id="model-owned", workspace_root=tmp_path)
    try:
        async with aclosing(QueryEngine().submit(QuerySubmission(user_message=state.user_message, session=session, state=state,
            runtime=AgentLoopSessionContext(session_id="model-owned-session", workspace_root=tmp_path,
                permission_context=PermissionContext(mode="bypass"), run_context=owner)))) as stream:
            events = [event async for event in stream]
        assert state.terminal_status == "completed" and fixture.calls == 2
        assert {"new_context", "get_context_remaining"} <= {tool["function"]["name"] for tool in fixture.schemas[0]}
        assert "A guidance" in "\n".join(str(message.content) for message in fixture.inputs[0])
        assert builder._context_window_state.window_number == 1
        assert sum(event.type == "context_compacted" for event in events) == 1
        assert builder._context_window_source_config.token_budget_startup_config.enabled is False
    finally:
        await session.aclose(); store.shutdown(); runtime.close(release_lease=True)
