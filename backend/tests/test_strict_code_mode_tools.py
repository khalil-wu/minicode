from __future__ import annotations

from dataclasses import replace
from functools import partial
import json

import pytest

from backend.agent.tool_schema_derivation import effective_toolset_policy
from backend.config import AppConfig, LLMSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition
from backend.mcp import config_file
from backend.mcp.client import MCPToolDef
from backend.mcp.manager import MCPServerConfig, MCPServerManager
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.sdk import FunctionTool
from backend.services import mcp_service
from backend.tests.test_code_execution import FixtureTool, ScriptModel, run_model
from backend.tests.test_harness_mcp_services_regressions import MCPPeer, manager_fixture
from backend.tests.test_mcp_settings_management import _Manager, _install_memory_config, _noop_hook
from backend.tools.code_execution import ToolExecTool, ToolWaitTool
from backend.tools.contracts import ToolSpec
from backend.tools.registry import ToolRegistry
from backend.tools.tool_search import DeferredToolCatalog, ToolSearchTool
from backend.tools.toolsets import ToolsetPolicy


class ClientTool(FunctionTool):
    def __init__(self, name="client_echo", *, exposure="core", namespace="client", always_load=False):
        super().__init__(name=name, description="Aurora dynamic client echo", func=self.echo,
            parameters={"type": "object", "properties": {}})
        self.exposure = exposure
        self.namespace = namespace
        self.always_load = always_load
        self.calls = 0
        self.schemas = 0

    def echo(self):
        self.calls += 1
        return "Actual client echo"

    def get_spec(self):
        return ToolSpec(self.name, exposure=self.exposure, namespace=self.namespace)

    def get_schema(self):
        self.schemas += 1
        return replace(super().get_schema(), namespace=self.namespace, namespace_tool_name="echo")


def config(*, enabled=True, code_mode=None):
    return AppConfig(llm=LLMSettings(api_key="fixture"), feature_flags=FeatureFlags({"code_mode_only_strict_3p_tools": enabled}),
        config_layer_stack=ConfigLayerStack((ConfigLayer(ConfigLayerSource("session_flags"),
            {"code_mode": code_mode or {}}),)))


def registry_with(*tools):
    registry = ToolRegistry()
    for tool in (ToolExecTool(), ToolWaitTool(), *tools):
        registry.register(tool)
    registry.register(ToolSearchTool(registry))
    return registry


def policy_for(registry, *, mode="code_mode_only", enabled=True, search=True, code_mode=None, base=None, denied=()):
    return effective_toolset_policy(base_policy=base, tool_registry=registry, disabled_tools=set(denied),
        requires_explicit_workspace=False, workspace_root=None, permission_mode="bypass", tool_mode=mode,
        supports_search_tool=search, config_snapshot=config(enabled=enabled, code_mode=code_mode))


@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("exposure", ["core", "deferred", "direct_model_only", "deferred_model_only", "code_mode_only"])
def test_strict_schema_search_and_cache_keep_client_tools_nested_despite_caller_overrides(search, exposure):
    client = ClientTool(exposure=exposure, always_load=True)
    builtin = FixtureTool("builtin")
    registry = registry_with(client, builtin)
    settings = {"direct_only_tool_namespaces": ["client"], "excluded_tool_namespaces": ["client"]}
    base = ToolsetPolicy(enabled_tools=frozenset({client.name}), include_deferred_directly=True)
    ordinary = policy_for(registry, enabled=False, search=search, code_mode=settings, base=base)
    assert client.name in {item["function"]["name"] for item in registry.get_schemas(toolset_policy=ordinary)}
    client.schemas = 0
    strict = policy_for(registry, search=search, code_mode=settings, base=base)
    outer = registry.get_schemas(toolset_policy=strict)
    names = {item["function"]["name"] for item in outer}
    assert {client.name, "tool_search"}.isdisjoint(names)
    prompt = next(item["function"]["description"] for item in outer if item["function"]["name"] == "tool_exec")
    assert "Aurora dynamic client echo" not in prompt
    assert client.schemas == 0
    assert "tools.builtin(" in prompt
    assert ("tools.tool_search(" in prompt) is search
    nested = replace(strict, nested_surface=True)
    assert client.name in {item["function"]["name"] for item in registry.get_schemas(toolset_policy=nested)}
    assert [entry.name for entry in DeferredToolCatalog(registry, toolset_policy=strict).search("Aurora", 8, nested=True)] == [client.name]
    assert registry.get_schema_view(client.name, toolset_policy=strict, materialize_schema=False).exposure == "deferred"
    assert registry.get_tool_spec(builtin.name).third_party is False
    assert client.name in {item["function"]["name"] for item in registry.get_schemas(toolset_policy=ordinary)}


@pytest.mark.parametrize("mode", ["direct", "code_mode", "code_mode_only"])
def test_flag_is_scoped_to_effective_mode_and_does_not_resurrect_hidden_or_denied_tools(mode):
    allowed, hidden, denied = ClientTool(), ClientTool("hidden", exposure="hidden"), ClientTool("denied")
    registry = registry_with(allowed, hidden, denied)
    off = policy_for(registry, mode=mode, enabled=False, denied={denied.name})
    on = policy_for(registry, mode=mode, denied={denied.name})
    assert on.strict_third_party is (mode == "code_mode_only")
    if mode != "code_mode_only":
        assert registry.get_schemas(toolset_policy=off) == registry.get_schemas(toolset_policy=on)
    for policy in (on, replace(on, nested_surface=True)):
        assert {hidden.name, denied.name}.isdisjoint(item["function"]["name"] for item in registry.get_schemas(toolset_policy=policy))
        for tool in (hidden, denied):
            view = registry.get_schema_view(tool.name, toolset_policy=policy, materialize_schema=False)
            assert not view.schema_available and not view.code_mode_available
    disabled_exec = policy_for(registry, denied={"tool_exec"})
    assert not registry.get_schema_view(allowed.name, toolset_policy=disabled_exec).code_mode_available


# Source spec_plan.rs exposes different surfaces when a model cannot search or
# when code-only mode has no remaining exec route. These are public outcomes.
@pytest.mark.parametrize("omitted, searchable, code_only, no_search", [
    ([], (False, True), (False, True), (True, True)),
    (["direct"], (False, True), (False, True), (False, True)),
    (["deferred"], (True, True), (False, True), (True, True)),
    (["code_mode"], (False, False), (True, False), (True, False)),
    (["direct", "deferred"], (False, True), (False, True), (False, True)),
    (["direct", "code_mode"], (False, False), (False, False), (False, False)),
    (["deferred", "code_mode"], (True, False), (True, False), (True, False)),
    (["direct", "deferred", "code_mode"], (False, False), (False, False), (False, False)),
])
def test_mcp_omissions_and_strict_override_use_the_same_registry_surfaces(omitted, searchable, code_only, no_search):
    proxy = MCPToolProxy("fixture", MCPToolDef("echo", "Aurora MCP echo", input_schema={"type": "object"}), None)
    proxy._omit_tools_from = frozenset(omitted)
    registry = registry_with(proxy)
    for mode, search, expected in (("code_mode", True, searchable), ("code_mode_only", True, code_only), ("code_mode", False, no_search)):
        policy = policy_for(registry, mode=mode, search=search, enabled=False)
        view = registry.get_schema_view(proxy.name, toolset_policy=policy)
        assert (view.direct, view.code_mode_available) == expected
        nested_names = {item["function"]["name"] for item in registry.get_schemas(toolset_policy=replace(policy, nested_surface=True))}
        assert (proxy.name in nested_names) is expected[1]
    strict = policy_for(registry, code_mode={"direct_only_tool_namespaces": [proxy.model_namespace],
        "excluded_tool_namespaces": [proxy.model_namespace]})
    view = registry.get_schema_view(proxy.name, toolset_policy=strict)
    assert (view.direct, view.code_mode_available, view.exposure) == (False, True, "deferred")


def test_schema_only_client_namespace_and_builtin_collision_keep_their_identity():
    class NamespacedClient(FunctionTool):
        def get_schema(self):
            return replace(super().get_schema(), namespace="client")
    client = NamespacedClient(name="echo", description="echo", func=lambda: "echo")
    builtin = FixtureTool("update_plan")
    registry = registry_with(client, builtin)
    with pytest.raises(ValueError, match="name conflict"):
        registry.register(ClientTool("update_plan"))
    assert registry.get_tool_spec("update_plan").third_party is False
    policy = policy_for(registry, enabled=False, code_mode={"excluded_tool_namespaces": ["client"]})
    assert not registry.get_schema_view("echo", toolset_policy=policy).code_mode_available


@pytest.mark.parametrize("field", ["direct_only_tool_namespaces", "excluded_tool_namespaces"])
@pytest.mark.parametrize("value", ["client", [1], {"client": True}])
def test_namespace_configuration_requires_source_array_shape(field, value):
    with pytest.raises(ValueError, match="array of strings"):
        policy_for(registry_with(), code_mode={field: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_actual_code_host_discovers_and_calls_strict_mcp_despite_omit_and_namespace_overrides(tmp_path, monkeypatch, search):
    peer = MCPPeer("ACTUAL_MCP_ECHO", [MCPToolDef("echo", "Aurora MCP echo", input_schema={"type": "object"},
        annotations={"readOnlyHint": True}, meta={"anthropic/alwaysLoad": True})])
    manager = manager_fixture(tmp_path)
    manager._create_client = lambda *_args, **_kwargs: peer
    await manager.start_server(MCPServerConfig("fixture", command="python", omit_tools_from=["direct", "deferred", "code_mode"]))
    try:
        proxy = MCPToolProxy("fixture", peer.tools[0], peer, manager=manager)
        captured = config(code_mode={"direct_only_tool_namespaces": [proxy.model_namespace], "excluded_tool_namespaces": [proxy.model_namespace]})
        captured.config_layer_stack = ConfigLayerStack((
            ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), {}),
            *captured.config_layer_stack.layers,
        ))
        monkeypatch.setattr("backend.tests.test_code_execution.AppConfig", partial(AppConfig,
            feature_flags=captured.feature_flags, config_layer_stack=captured.config_layer_stack))
        code = "const target=ALL_TOOLS.find(t=>t.name.endsWith('__echo')); text(target.name);"
        if search:
            code += "const found=await tools.tool_search({query:'Aurora MCP echo'}); text(found.tools.some(t=>t.name===target.name));"
        else:
            code += "text(ALL_TOOLS.some(t=>t.name==='tool_search'));"
        code += "text(await tools[target.name]({}));"
        model = ScriptModel(code)
        model._settings = LLMSettings(api_key="fixture", supports_search_tool=search)
        info = ModelDefinition(provider="custom", id="fixture", name="Strict fixture", api="openai-responses",
            base_url="", supports_search_tool=search, tool_mode="code_mode_only")
        state, _, journal, events, _ = await run_model(tmp_path, model, [proxy], code_mode_only=False, model_info=info)
        assert state.terminal_status == "completed", model.reports
        assert peer.calls == [("echo", {})], model.reports
        assert model.reports[-1]["status"] == "completed", model.reports
        output = [part for report in model.reports for part in report["output"]]
        assert output[1] == str(search).lower()
        assert "ACTUAL_MCP_ECHO" in output[-1]
        for names, descriptions in zip(model.visible_tool_names, model.visible_tool_descriptions):
            assert {proxy.name, "tool_search"}.isdisjoint(names)
            assert "Aurora MCP echo" not in descriptions["tool_exec"]
        calls = {record.tool_call_id for record in state.tool_calls if record.tool_name == proxy.name}
        assert any(event.type == "tool_result" and event.data.get("id") in calls
            and event.data.get("call_source", {}).get("kind") == "code_mode" for event in events)
        assert not journal.unresolved_tool_uses()
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, expected_calls", [("direct", 1), ("code_mode", 1), ("code_mode_only", 0)])
async def test_model_mode_override_reaches_direct_dispatch_guard(tmp_path, monkeypatch, mode, expected_calls):
    monkeypatch.setattr("backend.tests.test_code_execution.AppConfig", partial(AppConfig,
        feature_flags=config().feature_flags))
    client = ClientTool(always_load=True)
    model = ScriptModel("", first_call=ToolCallEvent(id="direct-client", name=client.name, arguments={}))
    model._settings = LLMSettings(api_key="fixture")
    info = ModelDefinition(provider="custom", id="fixture", name="Override fixture", api="openai-responses", base_url="", tool_mode=mode)
    state, _, _, _, _ = await run_model(tmp_path, model, [client], code_mode_only=True, model_info=info)
    assert client.calls == expected_calls
    assert (client.name in model.visible_tool_names[0]) is bool(expected_calls)
    if not expected_calls:
        assert all(record.status != "success" for record in state.tool_calls if record.tool_name == client.name)
        assert "not a direct model tool" in str(model.reports)


@pytest.mark.parametrize("omitted", [None, [], ["direct", "code_mode"]])
def test_mcp_omit_config_roundtrip_preserves_inherit_and_explicit_empty(tmp_path, omitted):
    path = tmp_path / "mcp.json"
    entry = {"transport": "stdio", "command": "python", "args": [], "env": {}}
    if omitted is not None:
        entry["omit_tools_from"] = omitted
    receipt = config_file.write_mcp_config(json.dumps({"servers": {"fixture": entry}}), path)
    assert json.loads(receipt["config"]["content"])["servers"]["fixture"] == entry
    [saved] = MCPServerManager(config_path=path, workspace_root=None)._load_local_configs()
    assert saved.omit_tools_from == omitted
    assert mcp_service._server_entry_from_config(saved).get("omit_tools_from") == omitted


@pytest.mark.parametrize("omitted", ["direct", ["exec"], [1]])
def test_invalid_mcp_omit_configuration_cannot_be_saved_or_loaded(tmp_path, omitted):
    path = tmp_path / "mcp.json"
    original = '{"servers": {}}'
    path.write_text(original)
    value = json.dumps({"servers": {"fixture": {"transport": "stdio", "command": "python", "omit_tools_from": omitted}}})
    with pytest.raises(ValueError, match="omit_tools_from"):
        config_file.write_mcp_config(value, path)
    assert path.read_text() == original
    path.write_text(value)
    with pytest.raises(ValueError, match="omit_tools_from"):
        MCPServerManager(config_path=path, workspace_root=None)._load_local_configs()


@pytest.mark.asyncio
async def test_mcp_surface_update_invalidates_old_catalog_without_restarting_transport(tmp_path):
    manager = manager_fixture(tmp_path)
    peer = MCPPeer("Actual result")
    manager._create_client = lambda *_args, **_kwargs: peer
    initial = MCPServerConfig("server", command="python")
    desired = initial
    manager.load_config = lambda: [desired]
    await manager.start_server(initial)
    try:
        registry = registry_with()
        bridge = MCPToolRegistry(registry, mcp_manager=manager)
        bridge.register_server_tools("server", manager.get_all_tools()["server"], peer)
        old = registry.get_tool("mcp__server__act")
        assert not (await old.execute({})).is_error
        desired = replace(initial, omit_tools_from=["direct", "deferred", "code_mode"])
        await manager.reload_config()
        assert peer.closed == 0 and manager.get_client("server") is peer
        assert (await old.execute({})).is_error
        bridge.sync()
        ordinary = policy_for(registry, enabled=False)
        strict = policy_for(registry)
        assert not registry.get_schema_view(old.name, toolset_policy=ordinary).schema_available
        assert registry.get_schema_view(old.name, toolset_policy=strict).code_mode_available
        assert not (await registry.get_tool(old.name).execute({})).is_error
        desired = replace(desired, disabled_tools=["act"])
        await manager.reload_config()
        bridge.sync()
        assert registry.get_tool(old.name) is None
        assert not DeferredToolCatalog(registry, toolset_policy=strict).search("act", 8, nested=True)
        assert peer.calls == [("act", {}), ("act", {})]
        assert peer.closed == 0
    finally:
        await manager.stop_all()


@pytest.mark.asyncio
async def test_editing_mcp_settings_preserves_unedited_omissions_and_can_clear_or_reset(monkeypatch):
    entry = {"transport": "stdio", "command": "python", "omit_tools_from": ["code_mode"]}
    saved = _install_memory_config(monkeypatch, {"servers": {"fixture": entry}})
    manager = _Manager()
    base = {"name": "fixture", "transport": "stdio", "command": "python"}
    for patch, expected in (({}, ["code_mode"]), ({"omit_tools_from": []}, []), ({"omit_tools_from": None}, None)):
        await mcp_service.update_mcp_server(manager, {**base, **patch}, config_change_hook=_noop_hook)
        assert saved["data"]["servers"]["fixture"].get("omit_tools_from") == expected
    assert manager.reloaded == 3
