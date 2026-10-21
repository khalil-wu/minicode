from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest

from backend.agent.code_execution_vm import CodeVM
from backend.agent.codex_prompts import codex_multi_agent_mode, codex_multi_agent_role
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.tool_execution import run_tool, toolset_policy_guard_reason
from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.agents.source_roles import source_agent_options
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.provider_contracts import ModelDefinition
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.checker import PermissionChecker
from backend.tools.agent_tools import TaskTool
from backend.tools.code_execution import ToolExecTool
from backend.tools.registry import ToolRegistry
from backend.tools.schema import code_mode_tool_name
from backend.tools.swarm_tools import SendMessageTool
from backend.tools.swarm_tools import TaskCreateTool, TeamCreateTool
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, SOURCE_V2_COORDINATION_TOOLS, ToolsetPolicy


def v2_case(tmp_path, **options):
    config = AppConfig(llm=LLMSettings(api_key="fixture", model="configured-v2", provider="custom"),
        feature_flags=FeatureFlags({"multi_agent_v2": True}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")),
            {"features": {"multi_agent_v2": {"enabled": True, **options}}}),)))
    registry = ToolRegistry()
    registry.register(TaskTool(config_snapshot=config, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts")))
    registry.register(SendMessageTool())
    registry.register(ToolExecTool())
    snapshot = ModelExecutionSnapshot(config=config, llm=None, provider="custom", model=config.llm.model)
    policy = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="confirm",
        tool_mode="code_mode_only", model_execution=snapshot)
    return config, registry, snapshot, policy


def test_default_v2_namespace_exposure_and_old_executor_identity_stay_separate(tmp_path):
    _config, registry, _snapshot, policy = v2_case(tmp_path)
    direct = {schema["function"]["name"]: schema for schema in registry.get_schemas(toolset_policy=policy)}
    assert SOURCE_V2_COORDINATION_TOOLS <= direct.keys()
    assert "task" not in direct
    assert direct["spawn_agent"]["_minicode_namespace"]["name"] == "collaboration"
    assert "agent_type" not in direct["spawn_agent"]["function"]["parameters"]["properties"]
    assert direct["spawn_agent"]["_minicode_output_schema"]["required"] == ["task_name"]
    nested = replace(policy, nested_surface=True, code_mode_only=False)
    assert not SOURCE_V2_COORDINATION_TOOLS.intersection(
        schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=nested))
    context = ToolExecutionContext(permission=PermissionContext(mode="confirm"),
        metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: nested})
    reason = toolset_policy_guard_reason(ToolCallEvent("nested", "list_agents", {}), registry, context)
    assert "only as a direct model tool" in reason
    assert registry.get_tool_spec("list_agents").exposure == "core"
    assert all("_minicode_namespace" not in schema for schema in registry.get_schemas(toolset_policy=ToolsetPolicy.default()))


@pytest.mark.parametrize("name,args", [("wait_agent", {}), ("send_message", {"target": "/root/worker", "message": "x"}),
    ("followup_task", {"target": "/root/worker", "message": "x"})])
def test_configured_disabled_tools_cannot_enter_through_direct_or_code_mode(tmp_path, name, args):
    _config, registry, _snapshot, policy = v2_case(tmp_path, disable_direct_message=True, wait_agent_enabled=False)
    for nested in (False, True):
        active = replace(policy, nested_surface=nested, code_mode_only=False)
        context = ToolExecutionContext(permission=PermissionContext(mode="confirm"),
            metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: active})
        call = ToolCallEvent("unoffered", name, args, native_namespace="collaboration", native_name=name)
        assert "unavailable" in toolset_policy_guard_reason(call, registry, context)
        assert registry.get_tool_schema(name, toolset_policy=active) is None
        assert not registry.get_schema_view(name, toolset_policy=active).code_mode_available


@pytest.mark.parametrize("version", [None, "v1", "v2"])
def test_v2_child_tool_gate_uses_the_actual_receiving_model(tmp_path, version):
    _config, registry, snapshot, root = v2_case(tmp_path)
    model = ModelDefinition(id=snapshot.model, provider="custom", name="Child model", api="openai-responses",
        base_url="https://test.invalid/v1", extra={"multi_agent_version": version} if version is not None else {})
    child = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="confirm", tool_mode="code_mode_only",
        model_execution=replace(snapshot, model_info=model), multi_agent_version="v2", is_subagent=True)
    visible = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=child)}
    assert bool(visible & SOURCE_V2_COORDINATION_TOOLS) is (version == "v2")
    assert SOURCE_V2_COORDINATION_TOOLS <= {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=root)}
    assert child.coordination_options is not None


def test_v2_does_not_expose_legacy_task_or_team_routes_inside_code_mode(tmp_path):
    _config, registry, _snapshot, policy = v2_case(tmp_path, non_code_mode_only=False, disable_direct_message=True, wait_agent_enabled=False)
    registry.register(TaskCreateTool())
    registry.register(TeamCreateTool())
    nested = replace(policy, nested_surface=True, code_mode_only=False)
    context = ToolExecutionContext(permission=PermissionContext(mode="confirm"),
        metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: nested})
    for name in ("task", "task_create", "team_create"):
        assert registry.get_tool_schema(name, toolset_policy=nested) is None
        assert "unavailable" in toolset_policy_guard_reason(ToolCallEvent("old", name, {}), registry, context)


def test_schema_options_and_catalog_overrides_are_step_local(tmp_path):
    config, registry, snapshot, first = v2_case(tmp_path, expose_spawn_agent_model_overrides=False,
        hide_spawn_agent_metadata=False, tool_namespace="crew", usage_hint_text="Use the assigned directory.")
    first = replace(first, model_messages={"tools": {"multi_agent": {"spawn_agent": {"description": ""},
        "list_agents": {"parameters": json.dumps({"type": "object", "properties": {"path_prefix": {"type": "string"}}, "additionalProperties": False})}}}})
    spawn = registry.get_tool_schema("spawn_agent", toolset_policy=first)
    assert not {"model", "reasoning_effort"}.intersection(spawn["function"]["parameters"]["properties"])
    assert spawn["function"]["description"] == "\n\nUse the assigned directory."
    assert spawn["_minicode_namespace"]["name"] == "crew"
    assert spawn["_minicode_output_schema"]["required"] == ["task_name", "nickname"]
    raw = registry.get_tool("spawn_agent").get_schema()
    assert "model" in raw.parameters["properties"] and raw.namespace == ""
    next_config = replace(config, config_layer_stack=ConfigLayerStack(()))
    second = effective_toolset_policy(base_policy=None, tool_registry=registry, disabled_tools=set(),
        requires_explicit_workspace=False, workspace_root=tmp_path, permission_mode="confirm",
        model_execution=replace(snapshot, config=next_config))
    assert second.cache_key() != first.cache_key()
    assert registry.get_tool_schema("spawn_agent", toolset_policy=second)["_minicode_namespace"]["name"] == "collaboration"
    assert registry.get_tool_schema("spawn_agent", toolset_policy=first) == spawn


@pytest.mark.asyncio
@pytest.mark.parametrize("wire_namespace", ["crew", "functions", "other"])
async def test_actual_responses_namespace_reaches_the_captured_executor_only(tmp_path, wire_namespace):
    _config, registry, snapshot, policy = v2_case(tmp_path, tool_namespace="crew")
    item = {"type": "function_call", "id": "item-list", "call_id": "call-list", "name": "list_agents",
        "namespace": wire_namespace, "arguments": "{}", "status": "completed"}
    wire = [{"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": "", "status": "in_progress"}},
        {"type": "response.function_call_arguments.done", "item_id": item["id"], "arguments": "{}"},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "response", "status": "completed", "output": [item]}}]
    requests = []
    async def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\n\n" for event in wire).encode(), request=request)
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", session_id="session", conversation_id="conversation")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        adapter = OpenAIAdapter(LLMSettings(api_key="fixture", wire_api="responses", base_url="https://test.invalid/v1"), http_client=http)
        try:
            events = [event async for event in adapter.stream_chat([LLMMessage(role="user", content="Inspect actual agents")], registry.get_schemas(toolset_policy=policy))]
            assert not any(event.type == StreamEventType.ERROR for event in events), events
            assert any(tool["type"] == "namespace" and tool["name"] == "crew" for tool in requests[0]["tools"])
            calls = [call for event in events for call in event.tool_calls]
            assert calls
            call = calls[-1]
            assert call.native_namespace == wire_namespace and call.native_name == "list_agents"
            context = ToolExecutionContext(permission=PermissionContext(mode="confirm"), session_id="session", conversation_id="conversation",
                model_execution=snapshot, run_context=RunContext(agent_runtime=runtime),
                metadata={"run_id": "parent", ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
            result = await run_tool(call, registry, context)
            if wire_namespace == "crew":
                assert call.name == "list_agents"
                assert all(event.tool_call_start.name == "list_agents" for event in events if event.tool_call_start)
                assert not result.is_error, result.content
                assert json.loads(result.content)["agents"][0]["agent_name"] == "/root"
            else:
                assert result.is_error
        finally:
            await adapter.aclose()
            await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("namespace,public_name", [("crew", "crew__list_agents"), ("crew-a", "crew_a__list_agents"),
    ("7crew", "_crew__list_agents"), ("crew_", "crew_list_agents")])
async def test_v2_code_mode_exports_the_namespaced_script_binding_and_dispatches_original_tool(tmp_path, namespace, public_name):
    _config, registry, _snapshot, policy = v2_case(tmp_path, non_code_mode_only=False, tool_namespace=namespace)
    nested = replace(policy, nested_surface=True, code_mode_only=False)
    schema = registry.get_tool_schema("list_agents", toolset_policy=nested)
    assert schema is not None
    assert code_mode_tool_name(schema) == public_name
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", session_id="session", conversation_id="conversation")
    context = ToolExecutionContext(permission=PermissionContext(mode="confirm"), session_id="session", conversation_id="conversation",
        run_context=RunContext(agent_runtime=runtime), metadata={"run_id": "parent", ACTIVE_TOOLSET_POLICY_METADATA_KEY: nested})
    vm = CodeVM()
    try:
        packet = await vm.step("start", {"code": f"text(typeof tools.list_agents); text(ALL_TOOLS[0].name); text(await tools.{public_name}({{}}));",
            "tools": [{"name": "list_agents", "public_name": code_mode_tool_name(schema), "description": schema["function"]["description"], "parameters": schema["function"]["parameters"]}], "storage": {}})
        request = next(request for request in packet["requests"] if request["kind"] == "tool")
        assert request["name"] == "list_agents"
        result = await run_tool(ToolCallEvent("code-list", request["name"], request["args"]), registry, context)
        assert not result.is_error, result.content
        finished = await vm.step("deliver", [{"id": request["id"], "value": json.loads(result.content), "tool_call_id": "code-list"}])
        output = [item["text"] for item in [*packet["output"], *finished["output"]] if item["kind"] == "text"]
        assert output[0] == "undefined" and output[1] == public_name
        assert "/root" in output[-1]
    finally:
        vm.close()
        await runtime.aclose(release_lease=True)


def test_source_role_and_mode_precedence_preserves_empty_configured_text(tmp_path):
    config, _registry, _snapshot, _policy = v2_case(tmp_path, root_agent_usage_hint_text="Configured root",
        subagent_usage_hint_text="", multi_agent_mode_hint_text="")
    options = source_agent_options(config)
    messages = {"multi_agent": {"role": {"root": "Catalog root", "subagent": "Catalog child"},
        "mode": {"hint_text": "Catalog mode"}}}
    assert codex_multi_agent_role(options, messages, subagent=False) == "Configured root"
    assert codex_multi_agent_role(options, messages, subagent=True) == ""
    assert codex_multi_agent_mode("unknown", True, model_messages=messages, options=options) == ""
    default = source_agent_options(replace(config, config_layer_stack=ConfigLayerStack(())))
    rendered = codex_multi_agent_role(default, messages, subagent=False)
    assert "Catalog root" in rendered and "4 available concurrency slots" in rendered
    assert "<multi_agent_role>" in rendered and "fork_turns" in rendered
    assert codex_multi_agent_role(default, {"multi_agent": {"role": {"root": ""}}}, subagent=False) == ""


def test_source_session_child_cap_does_not_count_the_root_twice(tmp_path):
    config, _registry, _snapshot, _policy = v2_case(tmp_path)
    config.config_layer_stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"agents": {"max_concurrent_threads_per_session": 4}}),))
    assert source_agent_options(config)["max_children"] == 4


@pytest.mark.parametrize("namespace", ["", "bad namespace", " crew", "crew ", "functions", "mcp", "mcp__host", "a" * 65])
def test_namespace_uses_the_source_configuration_boundary(tmp_path, namespace):
    with pytest.raises(ValueError, match="features.multi_agent_v2.tool_namespace"):
        v2_case(tmp_path, tool_namespace=namespace)


@pytest.mark.asyncio
async def test_code_mode_identifier_collision_keeps_only_the_first_source_binding(tmp_path):
    from backend.tests.test_code_execution import FixtureTool, ScriptModel, run_model

    winner, shadowed = FixtureTool("probe-1", count=1), FixtureTool("probe_1", count=1)
    model = ScriptModel('text(ALL_TOOLS.filter(tool => tool.name === "probe_1").length); text(await tools.probe_1({}));')
    _state, _builder, _journal, events, _owner = await run_model(tmp_path, model, [winner, shadowed])
    assert not any(event.type == "error" for event in events), events
    assert winner.executions == 1 and shadowed.executions == 0
    assert model.reports[-1]["output"][0] == "1"


@pytest.mark.asyncio
async def test_role_and_mode_reach_actual_root_and_child_prompt_composition(tmp_path):
    from backend.agent.context import ContextBuilder
    from backend.agent.state import AgentState
    from backend.tests.test_source_agent_roles import RecordingModel

    _config, _registry, snapshot, policy = v2_case(tmp_path, root_agent_usage_hint_text="Actual root instructions",
        subagent_usage_hint_text="Actual child instructions", multi_agent_mode_hint_text="Actual mode")
    model = RecordingModel()
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", session_id="session", conversation_id="conversation")
    runtime.start_subagent(subagent_id="child", parent_run_id="parent", agent_type="default")
    try:
        for actor, own, other in (("parent", "Actual root instructions", "Actual child instructions"),
                                  ("child", "Actual child instructions", "Actual root instructions")):
            builder = ContextBuilder(llm=model, conversation_id="conversation", workspace_root=tmp_path)
            builder.bind_tool_context(ToolExecutionContext(permission=PermissionContext(), session_id=actor,
                conversation_id="conversation", model_execution=snapshot, run_context=RunContext(agent_runtime=runtime),
                metadata={"run_id": actor, ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy}))
            rendered = builder._render_prompt_messages(AgentState(user_message="Inspect", conversation_id="conversation"), tmp_path)
            developer = [message.content for message in rendered if message.role == "developer"]
            assert own in developer and other not in developer
            assert "<multi_agent_mode>Actual mode</multi_agent_mode>" in developer
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("hide", [True, False])
async def test_real_spawn_keeps_actual_agent_id_in_runtime_metadata_and_source_model_result(tmp_path, hide):
    config, registry, snapshot, policy = v2_case(tmp_path, hide_spawn_agent_metadata=hide)
    class Model(LLMAdapter):
        _settings = config.llm
        async def stream_chat(self, messages, tools=None, metadata=None):
            yield StreamEvent(StreamEventType.TEXT_CHUNK, content="Actual child completed.", phase="final_answer")
            yield StreamEvent(StreamEventType.DONE, finish_reason="stop")
        async def simple_chat(self, messages, *, max_tokens=None):
            return ""
        def model_id(self):
            return self._settings.model

    model = Model()
    snapshot = replace(snapshot, llm=model)
    runtime = AgentRuntime(swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="parent", session_id="session", conversation_id="conversation")
    checker = PermissionChecker(config.permissions, workspace_root=tmp_path)
    context = ToolExecutionContext(permission=checker.build_context(mode="bypass"), permission_checker=checker, workspace_root=tmp_path,
        session_id="session", conversation_id="conversation", llm=model, model_execution=snapshot,
        tool_registry=registry, run_context=RunContext(agent_runtime=runtime, model_execution=snapshot,
            multi_agent_version="v2"), metadata={"run_id": "parent", ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
    try:
        result = await run_tool(ToolCallEvent("spawn-real", "spawn_agent", {"task_name": "worker", "message": "Complete the isolated child task.", "fork_turns": "none"},
            native_namespace="collaboration", native_name="spawn_agent"), registry, context)
        assert not result.is_error, result.content
        output = json.loads(result.content)
        assert output == ({"task_name": "/root/worker"} if hide else {"task_name": "/root/worker", "nickname": None})
        agent_id = result.runtime_metadata["subagent_id"]
        assert runtime.get_subagent(agent_id) is not None
        await runtime.wait_for_subagent_execution(agent_id)
        assert runtime.get_subagent(agent_id).status == "completed"
    finally:
        await runtime.aclose(release_lease=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("child_instructions", ["Child-specific instructions", "", None])
async def test_child_developer_layer_inherits_or_replaces_parent_from_actual_config(tmp_path, child_instructions):
    from backend.tests.test_task_model_resolution import _parent_fixture, _parent_run_context
    from backend.tools.subagent_support import _resolve_subagent_llm

    parent, _runtime, config, metadata = _parent_fixture()
    config.feature_flags = FeatureFlags({"multi_agent_v2": True})
    config.config_layer_stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "config.toml")), {
        "developer_instructions": "Parent instructions", "features": {"multi_agent_v2": {
            "enabled": True, "subagent_developer_instructions": child_instructions}}}),))
    metadata["_subagent_parent_runtime"]["config"] = config
    child = await _resolve_subagent_llm(parent, parent_metadata={}, run_context=_parent_run_context(metadata),
        agent_type="default", fork_turns="none", workspace_root=tmp_path, build_adapter=False)
    expected = "Parent instructions" if child_instructions is None else child_instructions
    assert child.role_overrides["developer_instructions"] == expected
    assert child.config.config_layer_stack.effective_config()["developer_instructions"] == expected
    assert config.config_layer_stack.effective_config()["developer_instructions"] == "Parent instructions"
