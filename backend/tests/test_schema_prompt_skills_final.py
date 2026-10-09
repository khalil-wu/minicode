"""End-to-end contracts found by the final Codex source audit."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.agent.tool_schema_derivation import derive_turn_tool_schema_state
from backend.agent.tool_execution import toolset_policy_guard_reason
from backend.config import PermissionSettings
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.skills.loader import SkillLoader
from backend.skills.manager import SkillManager
from backend.tools.registry import ToolRegistry
from backend.tools.base import PermissionLevel
from backend.tools.schema import code_mode_parameters
from backend.tools.tool_search import ToolSearchTool, build_deferred_tools_prompt_block
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolAvailabilityFilter, ToolsetPolicy
from backend.llm.base import ToolCallEvent


def test_code_mode_directory_handles_valid_boolean_composite_and_nullable_schemas():
    signature = code_mode_parameters({
        "type": ["object", "null"],
        "properties": {
            "anything": True,
            "forbidden": False,
            "mode": {"const": "read", "description": "Read mode preserves files."},
            "rows": {"type": ["array", "null"], "items": False},
            "combined": {"allOf": [{"anyOf": [{"type": "string"}, {"type": "number"}]}, {"type": "string"}]},
        },
        "required": ["mode"],
    })
    assert "anything?: unknown" in signature and "forbidden?: never" in signature
    assert 'mode: "read"' in signature and "Read mode preserves files." in signature
    assert "rows?: Array<never> | null" in signature
    assert "combined?: (string | number) & string" in signature
    assert signature.endswith("} | null")


def _skill(path, name):
    path.parent.mkdir(parents=True)
    path.write_text(f"---\nname: {name}\ndescription: Read code\n---\nDo the work.", encoding="utf-8")


def test_linked_skill_selection_resolves_duplicate_names_without_connector_misfires(tmp_path, monkeypatch):
    first, second = tmp_path / "first" / "SKILL.md", tmp_path / "second" / "SKILL.md"
    _skill(first, "review")
    _skill(second, "review")
    loader = SkillLoader(tmp_path)
    monkeypatch.setattr(loader, "_search_dirs", lambda: [("workspace", tmp_path)])
    manager = SkillManager(loader)
    assert manager.detect("$review") == []
    selected = manager.detect(f"[$review](skill://{second}) $review")
    assert [(item.name, item.source_path) for item in selected] == [("review", str(second))]
    both = manager.detect(f"[$review]({first}) [$review]({second})")
    assert {item.source_path for item in both} == {str(first), str(second)}
    assert manager.detect("[$review](app://review) [$review](mcp://review) [$review](plugin://review)") == []
    second.unlink()
    loader.discover()
    assert manager.detect("[$review](app://review)") == []
    assert manager.detect(f"[$review]({second})") == []
    assert [item.name for item in manager.detect("/review")] == ["review"]


def test_empty_skill_catalog_is_a_completed_discovery(tmp_path, monkeypatch):
    loader = SkillLoader(tmp_path)
    roots = Mock(return_value=[])
    monkeypatch.setattr(loader, "_search_dirs", roots)
    loader.discover()
    assert loader.list_metas() == loader.list_skill_names() == []
    assert loader.get_all_layer1() == ""
    assert loader.get_metas("missing") == []
    assert loader.get_meta_by_path(tmp_path / "SKILL.md") is None
    assert loader.load_full("missing") is None
    assert roots.call_count == 1
    loader.set_project_root(tmp_path / "other")
    assert loader.list_metas() == []
    assert roots.call_count == 2


def test_deferred_directory_reuses_one_projection_without_building_a_search_index(monkeypatch):
    import backend.tools.tool_search as search

    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    for index in range(200):
        registry.register(MCPToolProxy("fixture", MCPToolDef(f"act_{index}", "Detailed rules " * 300), None))
    views = Mock(wraps=registry.build_schema_views)
    monkeypatch.setattr(registry, "build_schema_views", views)
    tokenize = Mock(side_effect=AssertionError("Names-only discovery should not tokenize descriptions"))
    monkeypatch.setattr(search, "_tokenize", tokenize)
    state = derive_turn_tool_schema_state(base_tool_schemas=registry.get_schemas(), mcp_instructions={}, tool_registry=registry)
    assert 'total="200"' in state.deferred_tools_prompt_block
    assert views.call_count == 1
    assert tokenize.call_count == 0


def test_direct_schema_build_does_not_materialize_deferred_metadata(monkeypatch):
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    deferred = MCPToolProxy("fixture", MCPToolDef("act", "Detailed rules"), None)
    monkeypatch.setattr(deferred, "model_description", Mock(side_effect=AssertionError("Deferred description touched")))
    monkeypatch.setattr(deferred, "to_runtime_metadata", Mock(side_effect=AssertionError("Deferred metadata touched")))
    registry.register(deferred)
    assert [item["function"]["name"] for item in registry.get_schemas()] == ["tool_search"]


def test_approval_policy_and_mutated_checker_policy_invalidate_deferred_visibility(tmp_path):
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    registry.register(MCPToolProxy("fixture", MCPToolDef("act", "Write data"), None))
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    permission = PermissionContext(mode="confirm", workspace_root=tmp_path)
    schemas = registry.get_schemas(permission_checker=checker, permission_context=permission)
    first = derive_turn_tool_schema_state(
        base_tool_schemas=schemas, mcp_instructions={}, tool_registry=registry,
        permission_checker=checker, permission_context=permission,
    )
    assert "mcp__fixture__act" in first.deferred_tools_prompt_block
    never = derive_turn_tool_schema_state(
        base_tool_schemas=schemas, mcp_instructions={}, tool_registry=registry,
        permission_checker=checker, permission_context=replace(permission, approval_policy="never"), previous=first,
    )
    assert never.deferred_tools_prompt_block == ""
    checker._settings.always_deny.append("mcp__fixture__act")
    denied = derive_turn_tool_schema_state(
        base_tool_schemas=schemas, mcp_instructions={}, tool_registry=registry,
        permission_checker=checker, permission_context=permission, previous=first,
    )
    assert denied.deferred_tools_prompt_block == ""


def test_long_mcp_description_instructions_and_empty_argument_schema_survive_discovery():
    description = "Detailed tool rules. " * 300 + "REQUIRED TOOL CONDITION"
    instructions = "Server routing rules. " * 300 + "REQUIRED SERVER CONDITION"
    client = MCPClient("fixture")
    client._set_server_instructions(instructions)
    assert client.instructions == instructions
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    bridge = MCPToolRegistry(registry)
    bridge.register_server_tools("fixture", [MCPToolDef("act", description, {"type": "object"})], client)
    schema = registry.get_tool_schema("mcp__fixture__act", require_deferred=True)
    assert schema["function"]["description"] == description
    assert schema["function"]["parameters"] == {"type": "object", "properties": {}}
    state = derive_turn_tool_schema_state(base_tool_schemas=registry.get_schemas(), mcp_instructions={"fixture": client.instructions}, tool_registry=registry)
    assert "REQUIRED SERVER CONDITION" in state.runtime_guidance
    assert "REQUIRED TOOL CONDITION" not in state.deferred_tools_prompt_block


@pytest.mark.asyncio
async def test_mcp_name_collisions_and_long_names_remain_discoverable_and_route_raw_names():
    calls = []

    async def call_tool(name, args, **kwargs):
        calls.append(name)
        return MCPCallResult(content=[{"type": "text", "text": name}])

    client = SimpleNamespace(connected=True, call_tool=call_tool)
    registry = ToolRegistry()
    bridge = MCPToolRegistry(registry)
    names = ["read.name", "read/name", "read" + "_long" * 40]
    assert bridge.register_server_tools("fixture.one", [MCPToolDef(name, name) for name in names], client) == 3
    fork = registry.fork()
    fork_names = set(fork.list_tools())
    assert bridge.register_server_tools("fixture/one", [MCPToolDef("read.name", "other")], client) == 1
    model_names = registry.list_tools()
    assert len(model_names) == len(set(model_names)) == 4
    assert all(len(name) <= 128 for name in model_names)
    assert all(fork.get_tool(name).name == name for name in fork_names)
    block = build_deferred_tools_prompt_block(registry)
    assert all(name in block for name in model_names)
    for name in bridge.get_all_mcp_tools()["fixture.one"]:
        assert not (await registry.get_tool(name).execute({})).is_error
    assert set(calls) == set(names)
    state = derive_turn_tool_schema_state(
        base_tool_schemas=[], mcp_instructions={"fixture.one": "FIRST RULES", "fixture/one": "SECOND RULES"}, tool_registry=registry,
    )
    assert "FIRST RULES" in state.runtime_guidance and "SECOND RULES" in state.runtime_guidance
    reverse_registry = ToolRegistry()
    reverse = MCPToolRegistry(reverse_registry)
    reverse.register_server_tools("fixture/one", [MCPToolDef("read.name", "other")], client)
    reverse.register_server_tools("fixture.one", [MCPToolDef(name, name) for name in names], client)
    assert set(reverse_registry.list_tools()) == set(model_names)


def _renamed_mcp_tool(raw_name="read.name"):
    registry = ToolRegistry()
    registry.register(ToolSearchTool(registry))
    bridge = MCPToolRegistry(registry)
    bridge.register_server_tools("fixture.one", [MCPToolDef(raw_name, "Read", annotations={"readOnlyHint": True})], None)
    bridge.register_server_tools("fixture/one", [MCPToolDef("read/name", "Read", annotations={"readOnlyHint": True})], None)
    name = bridge.get_all_mcp_tools()["fixture.one"][0]
    return registry, registry.get_tool(name)


@pytest.mark.parametrize("rule", ["mcp__fixture.one__read.name", "mcp__fixture_one__read_name", "mcp__fixture.one", "mcp__fixture_one"])
def test_mcp_hash_names_preserve_raw_and_normalized_permission_denials(tmp_path, rule):
    registry, tool = _renamed_mcp_tool()
    assert tool.name != "mcp__fixture_one__read_name"
    permission = PermissionContext(mode="bypass", workspace_root=tmp_path)
    static = PermissionChecker(PermissionSettings(always_deny=[rule]), tmp_path)
    assert not static.capability_available(tool.name, context=permission, tool=tool)[0]
    assert static.evaluate(tool.name, {}, context=permission, tool=tool).decision == "deny"
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    denied = replace(permission, tool_deny_rules=[rule])
    assert checker.evaluate(tool.name, {}, context=denied, tool=tool).decision == "deny"
    state = derive_turn_tool_schema_state(
        base_tool_schemas=registry.get_schemas(), mcp_instructions={}, tool_registry=registry,
        permission_checker=checker, permission_context=denied,
    )
    assert tool.name not in state.deferred_tools_prompt_block
    override = replace(permission, session_overrides={rule: PermissionLevel.ALWAYS_DENY})
    assert checker.evaluate(tool.name, {}, context=override, tool=tool).decision == "deny"
    wire_approval = replace(permission, session_overrides={tool.name: PermissionLevel.AUTO, rule: PermissionLevel.ALWAYS_DENY})
    assert checker.evaluate(tool.name, {}, context=wire_approval, tool=tool).decision == "deny"


@pytest.mark.parametrize("alias", ["mcp__fixture.one__read.name", "mcp__fixture_one__read_name"])
def test_mcp_policy_aliases_share_toolset_schema_and_execution_gates(tmp_path, alias):
    registry, tool = _renamed_mcp_tool()
    spec = registry.get_tool_spec(tool.name)
    only = ToolsetPolicy(enabled_toolsets=frozenset(), enabled_tools=frozenset({alias}))
    assert only.is_directly_visible(spec)
    only.validate_against([spec])
    assert ToolAvailabilityFilter(tools=frozenset({alias})).allows(spec)
    visible = {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=only)}
    assert tool.name in visible
    disabled = replace(only, disabled_tools=frozenset({alias}))
    assert not disabled.is_available(spec)
    assert tool.name not in {schema["function"]["name"] for schema in registry.get_schemas(toolset_policy=disabled)}
    context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"), workspace_root=tmp_path,
        metadata={ACTIVE_TOOLSET_POLICY_METADATA_KEY: disabled}, tool_registry=registry,
    )
    assert toolset_policy_guard_reason(ToolCallEvent(id="test", name=tool.name, arguments={}), registry, context)


@pytest.mark.parametrize("event_name", ["PRE_TOOL_USE", "POST_TOOL_USE", "POST_TOOL_USE_FAILURE", "PERMISSION_REQUEST", "PERMISSION_DENIED"])
def test_mcp_content_rules_and_hook_conditions_keep_original_policy_identity(tmp_path, event_name):
    import json
    from backend.hooks.dispatcher import compile_hook_matcher, select_handlers
    from backend.hooks.manager import HookEvent, _prepare_hook_condition_matcher
    from backend.permissions.content_rules import parse_content_rule, rule_matches_call

    registry, tool = _renamed_mcp_tool()
    event = getattr(HookEvent, event_name)
    raw_alias, normalized_alias = tool.policy_aliases[:2]
    permission = PermissionContext(mode="bypass", workspace_root=tmp_path)
    for alias in (raw_alias, normalized_alias):
        rule = parse_content_rule(alias)
        assert rule_matches_call(rule, tool.name, {}, policy_aliases=tool.policy_aliases)
        checker = PermissionChecker(PermissionSettings(content_deny_rules=[alias]), tmp_path)
        assert checker.evaluate(tool.name, {}, context=permission, tool=tool).decision == "deny"
        entry = SimpleNamespace(condition="" if event == HookEvent.PERMISSION_DENIED else alias,
                                raw_matcher=alias, matcher=compile_hook_matcher(alias))
        matcher, policy_aliases = _prepare_hook_condition_matcher(
            event=event, match_target=tool.name,
            env_extras={"TOOL_NAME": tool.name, "TOOL_ARGS_JSON": json.dumps({})}, tool_registry=registry,
        )
        assert matcher(entry)
        assert select_handlers([entry], event=event, match_target=tool.name,
                               condition_matches=matcher, match_aliases=policy_aliases) == [entry]
    wire_checker = PermissionChecker(PermissionSettings(always_deny=[tool.name]), tmp_path)
    assert wire_checker.evaluate(tool.name, {}, context=permission, tool=tool).decision == "deny"


def test_mcp_refresh_preserves_historical_hashed_policy_name_after_collision_removal(tmp_path):
    from backend.mcp.manager import MCPServerManager, MCPServerState, MCPServerConfig, ServerStatus

    first = MCPToolDef("read.name", "Read", annotations={"readOnlyHint": True})
    second = MCPToolDef("read/name", "Read", annotations={"readOnlyHint": True})
    client = SimpleNamespace(connected=True, instructions="")
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=None)
    for server, definition in (("fixture.one", first), ("fixture/one", second)):
        manager._servers[server] = MCPServerState(
            config=MCPServerConfig(name=server, transport="http", url="https://fixture.invalid/mcp"),
            client=client, tools=[definition], status=ServerStatus.CONNECTED,
        )
    registry = ToolRegistry()
    bridge = MCPToolRegistry(registry, mcp_manager=manager)
    bridge.register_server_tools("fixture.one", [first], client)
    bridge.register_server_tools("fixture/one", [second], client)
    historical = bridge.get_all_mcp_tools()["fixture.one"][0]
    manager._servers.pop("fixture/one")
    manager._registry_version += 1
    bridge.sync()
    current = registry.get_tool("mcp__fixture_one__read_name")
    assert current is not None and current.name != historical
    assert historical in current.policy_aliases
    assert registry.get_tool(historical) is None
    checker = PermissionChecker(PermissionSettings(always_deny=[historical]), tmp_path)
    assert checker.evaluate(current.name, {}, context=PermissionContext(mode="bypass"), tool=current).decision == "deny"
    disabled = ToolsetPolicy(disabled_tools=frozenset({historical}))
    assert not disabled.is_available(registry.get_tool_spec(current.name))


def test_permission_rule_persistence_uses_raw_identity_and_restores_without_old_catalog(tmp_path):
    from backend.conversations.repository import ConversationRepository
    from backend.services.permission_rules_service import prepare_permission_rule_add, prepare_permission_rule_remove

    registry, tool = _renamed_mcp_tool()
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(title="Permission identity")
    mutation = prepare_permission_rule_add(
        conversation, {"pattern": tool.name, "rule_kind": "deny"},
        conversation_id=conversation.id, tool_registry=registry,
    )
    assert mutation.deny_rules == [tool.policy_identity]
    repo.update_permission_rules(conversation.id, deny_rules=mutation.deny_rules, overrides=mutation.serialized_overrides)
    fresh_repo = ConversationRepository(tmp_path / "conversations")
    restored = fresh_repo.get_conversation(conversation.id)
    fresh_registry = ToolRegistry()
    fresh_bridge = MCPToolRegistry(fresh_registry)
    fresh_bridge.register_server_tools("fixture.one", [MCPToolDef("read.name", "Read", annotations={"readOnlyHint": True})], None)
    current = fresh_registry.get_tool("mcp__fixture_one__read_name")
    permission = PermissionContext(mode="bypass", tool_deny_rules=restored.permission_deny_rules)
    checker = PermissionChecker(PermissionSettings(), tmp_path)
    assert checker.evaluate(current.name, {}, context=permission, tool=current).decision == "deny"
    removal = prepare_permission_rule_remove(
        restored, {"pattern": current.name, "rule_kind": "deny"},
        conversation_id=restored.id, tool_registry=fresh_registry,
    )
    assert removal.deny_rules == []
    glob = prepare_permission_rule_add(
        conversation, {"pattern": "mcp__fixture*", "rule_kind": "deny"},
        conversation_id=conversation.id, tool_registry=registry,
    )
    assert glob.deny_rules == ["mcp__fixture*"]


@pytest.mark.parametrize("raw_name", ["read.name", "do*thing", "build[x]"])
def test_global_content_rule_persistence_uses_stable_known_mcp_identity(tmp_path, monkeypatch, raw_name):
    import json
    import backend.config as config
    from backend.services.permission_content_service import add_permission_content_rule

    registry, tool = _renamed_mcp_tool(raw_name)
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "_load_settings_json", lambda: json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {})
    monkeypatch.setattr(config, "_write_settings_json", lambda data: settings_path.write_text(json.dumps(data), encoding="utf-8"))
    result = add_permission_content_rule(tool.name, deny=True, tool_registry=registry)
    assert result.should_emit_config_change
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["permissions"]["content_deny_rules"] == [tool.policy_identity]
    fresh = MCPToolProxy("fixture.one", MCPToolDef(raw_name, "Read", annotations={"readOnlyHint": True}), None)
    checker = PermissionChecker(config.permission_settings_from_config(saved), tmp_path)
    assert checker.evaluate(fresh.name, {}, context=PermissionContext(mode="bypass"), tool=fresh).decision == "deny"
