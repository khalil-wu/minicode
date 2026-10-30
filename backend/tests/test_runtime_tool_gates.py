from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.tool_execution import toolset_policy_guard_reason
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.agents.source_roles import (
    inherit_role_restrictions, project_role_config, read_role_file, role_tool_denials,
    source_agent_options, source_spawn_version,
)
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.conversations.repository import ConversationRepository
from backend.feature_flags import load_feature_flags
from backend.llm.base import ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.goal_runtime import new_goal
from backend.services.llm_config_service import llm_model_updated_payload
from backend.services.tool_registry_factory import build_tool_registry
from backend.tools.agent_tools import TaskTool
from backend.tools.agent_user_tools import RequestUserInputAsyncTool, SendMessageToUserAsyncTool
from backend.tools.code_execution import ToolExecTool
from backend.tools.goal_tools import GoalTool
from backend.tools.plan_tool import UpdatePlanTool
from backend.tools.registry import ToolRegistry
from backend.tools.sleep_tool import CurrentTimeTool, SleepTool
from backend.tools.subagent_context import build_agent_execution_profile, subagent_toolset_policy
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy
from backend.ws.handler import WebSocketSession
from backend.ws.handlers.conversation import handle_conversation_goal_set
from backend.ws.handlers.session import handle_runtime_capabilities_inspect


def configured(data=None):
    data = data or {}
    return AppConfig(llm=LLMSettings(provider="custom", model="runtime-gates", api_key="offline", tool_mode="code_mode"),
        feature_flags=load_feature_flags(data, managed_requirements={}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), data),)))


def model_info(supported=(), version=None):
    return ModelDefinition(id="runtime-gates", provider="custom", name="Gate fixture", api="openai-responses",
        base_url="https://fixture.invalid/v1", extra={"experimental_supported_tools": list(supported),
            **({"multi_agent_version": version} if version is not None else {})})


@pytest.fixture
def artifacts(tmp_path):
    return ArtifactStore(storage_dir=tmp_path / "artifacts")


def installed(config, artifacts):
    registry = ToolRegistry()
    for tool in (ToolExecTool(), RequestUserInputAsyncTool(), SendMessageToUserAsyncTool(),
                 CurrentTimeTool(), SleepTool(), UpdatePlanTool(), TaskTool(config_snapshot=config, artifact_store=artifacts)):
        registry.register(tool)
    for name in ("create_goal", "get_goal", "update_goal"):
        registry.register(GoalTool(name, config))
    return registry


def policy_for(registry, config, *, info=None, child=False, background=False, mode="confirm", base=None):
    snapshot = ModelExecutionSnapshot(config=config, llm=None, provider="custom", model="runtime-gates", model_info=info)
    if child:
        base = subagent_toolset_policy(execution_profile=build_agent_execution_profile(background=background), permission_mode=mode)
    return effective_toolset_policy(base_policy=base, tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=None, permission_mode=mode,
        model_execution=snapshot, is_subagent=child)


@pytest.mark.parametrize("supported,features,expected", [
    ((), {}, set()),
    (("request_user_input_async",), {}, {"request_user_input_async"}),
    (("send_user_message_async",), {}, {"request_user_input_async"}),
    (("send_message_to_user_async",), {}, {"send_message_to_user_async"}),
    ((), {"send_message_to_user_async": True}, {"send_message_to_user_async"}),
    (("request_user_input_async",), {"send_message_to_user_async": True}, {"request_user_input_async", "send_message_to_user_async"}),
])
@pytest.mark.parametrize("child", [False, True])
def test_async_tools_have_independent_source_gates_and_direct_only_execution(supported, features, expected, child, artifacts):
    config = configured({"features": features})
    registry = installed(config, artifacts)
    policy = policy_for(registry, config, info=model_info(supported), child=child)
    if child:
        expected = set()
    names = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)}
    assert names & {"request_user_input_async", "send_message_to_user_async"} == expected
    for name in ("request_user_input_async", "send_message_to_user_async"):
        context = ToolExecutionContext(permission=PermissionContext(), metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
        assert (not toolset_policy_guard_reason(ToolCallEvent("async", name, {}), registry, context)) == (name in expected)
        nested = replace(policy, nested_surface=True, code_mode_only=False)
        context.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY] = nested
        assert toolset_policy_guard_reason(ToolCallEvent("nested", name, {}), registry, context)
        assert not registry.get_schema_view(name, toolset_policy=nested).code_mode_available


@pytest.mark.parametrize("supported,features,time_visible,sleep_visible", [
    ((), {}, False, False),
    (("clock",), {}, True, True),
    ((), {"current_time_reminder": True}, True, False),
    (("clock",), {"current_time_reminder": True}, True, False),
    ((), {"current_time_reminder": {"enabled": True, "sleep_tool": True}}, True, True),
    (("clock",), {"sleep_tool": False}, True, False),
    ((), {"sleep_tool": {"mode": "always_on"}}, False, True),
    (("clock",), {"sleep_tool": {"enabled": False, "mode": "always_on"}}, True, False),
    ((), {"current_time_reminder": {"enabled": False, "sleep_tool": True}}, False, False),
])
def test_clock_and_sleep_share_source_model_and_feature_selection_with_execution(supported, features, time_visible, sleep_visible, artifacts):
    config = configured({"features": features})
    registry = installed(config, artifacts)
    policy = policy_for(registry, config, info=model_info(supported))
    names = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)}
    assert ("curr_time" in names, "sleep" in names) == (time_visible, sleep_visible)
    for name, visible in (("curr_time", time_visible), ("sleep", sleep_visible)):
        # An explicit active selection cannot reopen a tool disabled by its source gate.
        forced = policy.with_active_tool_selection([name])
        context = ToolExecutionContext(permission=PermissionContext(), metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: forced})
        assert (not toolset_policy_guard_reason(ToolCallEvent("clock", name, {}), registry, context)) == visible


def test_factory_and_next_step_register_only_the_tools_enabled_for_that_snapshot(tmp_path):
    off = configured({"features": {"goals": False, "multi_agent": False}})
    registry = build_tool_registry(ArtifactStore(storage_dir=tmp_path / "artifacts"), config=off, workspace_root=None)
    assert all(registry.get_tool(name) is None for name in ("sleep", "curr_time", "update_plan", "create_goal",
        "request_user_input_async", "send_message_to_user_async"))
    on = configured({"features": {"goals": True, "send_message_to_user_async": True,
        "current_time_reminder": {"enabled": True, "sleep_tool": True}}, "tools": {"update_plan": {"enabled": True}}})
    policy = policy_for(registry, on, info=model_info(("request_user_input_async",)))
    names = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)}
    assert {"sleep", "curr_time", "update_plan", "create_goal", "request_user_input_async", "send_message_to_user_async"} <= names
    assert "task" in names
    disabled = policy_for(registry, off, info=model_info())
    names = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=disabled)}
    assert not {"sleep", "curr_time", "update_plan", "create_goal", "request_user_input_async", "send_message_to_user_async"} & names
    assert "task" not in names


def test_next_step_automatic_tools_do_not_widen_an_explicit_session_scope(artifacts):
    config = configured({"features": {"goals": True, "multi_agent_v2": True,
        "send_message_to_user_async": True, "current_time_reminder": {"enabled": True, "sleep_tool": True}},
        "tools": {"update_plan": {"enabled": True}}})
    registry = ToolRegistry()
    registry.register(ToolExecTool())
    scope = ToolsetPolicy.from_iterables(enabled_toolsets=(), enabled_tools=["tool_exec"])
    policy = policy_for(registry, config, info=model_info(("request_user_input_async",), "v2"), base=scope)
    assert registry.list_tools() == ["tool_exec"]
    assert {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=policy)} == {"tool_exec"}


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_child_update_plan_is_selected_by_config_and_mode_without_a_child_hard_deny(background, artifacts):
    config = configured({"tools": {"update_plan": {"enabled": True}}})
    registry = installed(config, artifacts)
    checker = PermissionChecker(config.permissions)
    permission = PermissionContext(mode="confirm", source="subagent:worker")
    policy = policy_for(registry, config, child=True, background=background)
    assert registry.get_tool_schema("update_plan", toolset_policy=policy, permission_checker=checker, permission_context=permission)
    emitted = []
    async def emit(kind, payload):
        emitted.append((kind, payload))
    context = ToolExecutionContext(permission=permission, conversation_id="child-owner", emit_event=emit,
        metadata={"agent_mode": "build", "turn_id": "child-turn", ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
    assert not toolset_policy_guard_reason(ToolCallEvent("plan", "update_plan", {}), registry, context)
    result = await registry.get_tool("update_plan").execute({"plan": [{"step": "Owned child work", "status": "in_progress"}]}, context)
    assert not result.is_error and emitted[0][0] == "turn.plan.updated"
    assert emitted[0][1]["thread_id"] == "child-owner"
    plan_permission = replace(permission, mode="plan")
    assert registry.get_tool_schema("update_plan", toolset_policy=policy, permission_checker=checker, permission_context=plan_permission) is None
    off = policy_for(registry, configured(), child=True, background=background)
    assert registry.get_tool_schema("update_plan", toolset_policy=off) is None


@pytest.mark.parametrize("features,agents,version,expected", [
    ({"multi_agent": False}, {}, None, "disabled"),
    ({"multi_agent": True}, {}, None, "v1"),
    ({"multi_agent": False}, {}, "v1", "v1"),
    ({"multi_agent": True}, {}, "disabled", "disabled"),
    ({"multi_agent": True}, {"enabled": False}, "v1", "disabled"),
    ({"multi_agent": False, "multi_agent_v2": True}, {"enabled": False}, None, "v2"),
])
def test_canonical_multi_agent_disable_controls_the_source_fallback(features, agents, version, expected, artifacts):
    config = configured({"features": features, "agents": agents})
    info = model_info(version=version)
    assert source_spawn_version(config, definition=info) == expected
    assert source_agent_options(config, definition=info)["enabled"] is (expected != "disabled")
    registry = installed(config, artifacts)
    policy = policy_for(registry, config, info=info)
    assert policy.is_available(registry.get_tool_spec("task")) is (expected == "v1")
    if expected == "disabled":
        for nested in (False, True):
            context = ToolExecutionContext(permission=PermissionContext(), metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: replace(policy, nested_surface=nested)})
            assert toolset_policy_guard_reason(ToolCallEvent("disabled-agent", "task", {}), registry, context)


@pytest.mark.asyncio
async def test_canonical_multi_agent_disable_rejects_before_starting_a_child(artifacts):
    config = configured({"features": {"multi_agent": False}})
    snapshot = ModelExecutionSnapshot(config=config, llm=None, provider="custom", model=config.llm.model)
    owner = RunContext(model_execution=snapshot, multi_agent_version="disabled")
    context = ToolExecutionContext(permission=PermissionContext(), model_execution=snapshot, run_context=owner)
    result = await TaskTool(config_snapshot=config, artifact_store=artifacts).execute({"description": "Must not start", "prompt": "Inspect source"}, context)
    assert result.is_error and "disabled" in result.content
    assert owner.agent_runtime is None and owner.model_execution is snapshot


@pytest.mark.parametrize("key", ["memories", "memory_tool"])
def test_role_memory_disable_is_canonical_and_survives_inheritance(tmp_path, key):
    path = tmp_path / "worker.toml"
    path.write_text(f'name="worker"\ndeveloper_instructions="Own this task"\n[features]\n{key}=false\n', encoding="utf-8")
    role = read_role_file(path)
    assert role.overrides["features"] == {"memories": False}
    parent = configured({"features": {"memories": True}})
    child = project_role_config(parent, role.overrides)
    resumed = project_role_config(parent, inherit_role_restrictions(child, {}))
    assert parent.feature_flags.enabled("memories") and not resumed.feature_flags.enabled("memories")
    assert resumed.config_layer_stack.effective_config()["features"]["memories"] is False
    from backend.memory.file_memory import FileMemory
    from backend.tools.memory_tools import MemoryReadTool
    registry = ToolRegistry()
    registry.register(MemoryReadTool(FileMemory(tmp_path / "memory")))
    assert role_tool_denials({"features": resumed.feature_flags.flags}, registry) == ["memory_read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["set", "get", "pause", "resume", "clear"])
async def test_goal_ws_gate_reads_the_captured_target_scope_before_mutating_anything(tmp_path, monkeypatch, action):
    repository = ConversationRepository(tmp_path / "conversations")
    target = repository.create_conversation(workspace_root=str(tmp_path / "disabled"))
    other = repository.create_conversation(workspace_root=str(tmp_path / "enabled"))
    repository.update_goal(target.id, new_goal("Keep saved target", token_budget=500))
    before = repository.get_conversation(target.id)
    scopes = []
    def load(*, cwd, resolve_credentials):
        scopes.append((cwd, resolve_credentials))
        return configured({"features": {"goals": cwd != Path(target.workspace_root)}})
    monkeypatch.setattr("backend.config.load_config", load)
    session = SimpleNamespace(conversation_repo=repository, active_conversation_id=other.id,
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda record: Path(record.workspace_root)),
        emit_command_result=AsyncMock())
    assert await handle_conversation_goal_set(session, {"conversation_id": target.id, "action": action,
        "text": "Must not overwrite", "source": "slash:/goal"})
    assert scopes == [(Path(target.workspace_root), False)]
    assert session.emit_command_result.call_args.args == ("goal", "goals feature is disabled")
    after = repository.get_conversation(target.id)
    assert after.goal == before.goal and after.revision == before.revision and after.transcript == before.transcript


@pytest.mark.asyncio
async def test_disabled_goal_tools_reject_without_accounting_or_overwriting_saved_state(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    conversation = repository.create_conversation()
    repository.update_goal(conversation.id, new_goal("Retain this goal"))
    before = repository.get_conversation(conversation.id)
    off = configured({"features": {"goals": False}})
    snapshot = ModelExecutionSnapshot(config=off, llm=None, provider="custom", model=off.llm.model)
    owner = RunContext(conversation_repository=repository, model_execution=snapshot)
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id=conversation.id,
        model_execution=snapshot, run_context=owner, emit_event=AsyncMock())
    for name, args in (("create_goal", {"objective": "Replacement"}), ("get_goal", {}), ("update_goal", {"status": "complete"})):
        result = await GoalTool(name, configured()).execute(args, context)
        assert result.is_error and "goals feature is disabled" in result.content
    assert owner.goal_accounting is None and context.emit_event.await_count == 0
    after = repository.get_conversation(conversation.id)
    assert after.goal == before.goal and after.revision == before.revision


@pytest.mark.asyncio
async def test_runtime_inspect_and_workspace_switch_publish_the_same_fresh_scoped_gate_and_tool_plan(tmp_path, monkeypatch, artifacts):
    on, off = configured(), configured({"features": {"goals": False, "multi_agent": False}})
    scopes = {tmp_path / "on": on, tmp_path / "off": off}
    captured = ModelExecutionSnapshot(config=on, llm=None, provider="custom", model=on.llm.model)
    registry = installed(on, artifacts)
    admitted = policy_for(registry, on)
    active = RunContext(model_execution=captured, toolset_policy=admitted)
    reads, published = [], []
    def load(*, cwd, resolve_credentials):
        reads.append((cwd, resolve_credentials))
        return scopes[cwd]
    monkeypatch.setattr("backend.config.load_config", load)
    monkeypatch.setattr("backend.commands.catalog.get_enabled_composer_command_catalog", lambda *_args, **_kwargs: [{"name": "goal"}, {"name": "help"}])
    capture_realtime_start = Mock(side_effect=AssertionError("Capability projection must not capture realtime authentication"))
    monkeypatch.setattr("backend.realtime.session.capture_config", capture_realtime_start)
    session = SimpleNamespace(active_conversation_id="visible", session_id="session", root=tmp_path / "on", config=on,
        run_manager=SimpleNamespace(context_for=lambda _: active), tool_registry=registry,
        refresh_tool_registry_if_mcp_changed=Mock(), permission_checker=None, permission_context=PermissionContext(),
        _mcp_registry_version_snapshot=0, _extension_runtime_states={}, provider="custom", selected_model=on.llm.model,
        _model_runtime_for_conversation=lambda _: None, skill_manager=None,
        _provider_capabilities_payload=lambda _: {}, _runtime_permission_payload=lambda: {},
        command_registry=SimpleNamespace(list_extension_slash_commands=lambda **_: []))
    session._llm_selection_payload = lambda: llm_model_updated_payload(
        provider=session.provider, selected_model=session.selected_model,
        available_models=[session.selected_model], workspace_root=session.root,
        settings_data=scopes[session.root].config_layer_stack.effective_config(),
    )
    session.session_lifecycle = SimpleNamespace(workspace_root_for_conversation=lambda *_: session.root, current_workspace_root=lambda: session.root)
    session.conversation_repo = SimpleNamespace(get_conversation_summary=lambda _: SimpleNamespace(workspace_root=str(session.root)))
    session.runtime_toolset_policy = lambda registry, **kwargs: WebSocketSession.runtime_toolset_policy(session, registry, **kwargs)
    session.runtime_capability_snapshot = lambda **kwargs: WebSocketSession.runtime_capability_snapshot(session, **kwargs)
    async def send(*, source):
        published.append(WebSocketSession.runtime_capabilities_payload(session, source=source))
    session.session_lifecycle.send_runtime_capabilities = send
    await handle_runtime_capabilities_inspect(session, {"source": "settings.feature_flags"})
    session.root = tmp_path / "off"
    await handle_runtime_capabilities_inspect(session, {"source": "workspace.activate"})
    first, second = [event["capabilities"] for event in published]
    assert first["feature_flags"]["goals"]["enabled"] is True and second["feature_flags"]["goals"]["enabled"] is False
    assert "create_goal" in {tool["function"]["name"] for tool in first["tools"]}
    assert "create_goal" not in {tool["function"]["name"] for tool in second["tools"]}
    assert [command["name"] for command in second["composer_commands"]] == ["help"]
    assert published[-1]["workspace_root"] == str(session.root)
    assert reads == [(tmp_path / "on", False), (tmp_path / "off", False)]
    capture_realtime_start.assert_not_called()
    assert session.config is on and active.model_execution is captured and active.toolset_policy is admitted
    assert session.runtime_toolset_policy(registry) is admitted
