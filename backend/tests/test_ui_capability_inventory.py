from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tests.test_step_capability_snapshot import _DeferredTool, _SnapshotTool
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ToolsetPolicy
from backend.ws.handler import WebSocketSession


@pytest.mark.parametrize("deny", [False, True])
@pytest.mark.parametrize("search", [False, True])
def test_ui_inventory_keeps_tool_exposure_without_materializing_model_schemas(tmp_path, monkeypatch, deny, search):
    registry = ToolRegistry()
    registry.register(_SnapshotTool("public"))
    registry.register(_DeferredTool())
    private = _SnapshotTool("private")
    private.name = "internal_tool"
    private.private_model_only = True
    registry.register(private)
    checker = PermissionChecker(PermissionSettings(auto_allow=["*"], require_confirm=[]), tmp_path)
    context = PermissionContext(mode="auto", workspace_root=tmp_path,
                                tool_deny_rules=["snapshot_tool"] if deny else [])
    policy = ToolsetPolicy(supports_search_tool=search)
    full = registry.build_snapshot(permission_checker=checker, permission_context=context, toolset_policy=policy)
    schemas = registry.get_schemas(permission_checker=checker, permission_context=context, toolset_policy=policy)
    assert all("parameters" in item["function"] for item in schemas)

    def forbidden(**_kwargs):
        raise AssertionError("UI inventory must not materialize provider tool schemas")

    monkeypatch.setattr(registry, "get_schemas", forbidden)
    inventory = registry.build_snapshot(permission_checker=checker, permission_context=context,
                                        toolset_policy=policy, include_tool_schemas=False)
    assert inventory["tools"] == [
        {"type": "function", "function": {"name": item["function"]["name"]}}
        for item in full["tools"]
    ]
    assert {key: value for key, value in inventory.items() if key != "tools"} == {
        key: value for key, value in full.items() if key != "tools"
    }
    changed = registry.build_snapshot(permission_checker=checker,
        permission_context=replace(context, tool_deny_rules=["snapshot_tool"] if not deny else []),
        toolset_policy=policy, include_tool_schemas=False)
    assert ("snapshot_tool" in {tool["function"]["name"] for tool in changed["tools"]}) is deny


@pytest.mark.parametrize("nested", [False, True])
def test_ui_inventory_matches_code_mode_and_connector_order(nested):
    registry = ToolRegistry()
    for name in ("tool_exec", "snapshot_tool", "tool_wait", "mcp__fixture__lookup"):
        tool = _SnapshotTool(name)
        tool.name = name
        registry.register(tool)
    policy = ToolsetPolicy(code_mode_only=True, nested_surface=nested)
    full = registry.build_snapshot(toolset_policy=policy)
    inventory = registry.build_snapshot(toolset_policy=policy, include_tool_schemas=False)
    assert inventory["tools"] == [
        {"type": "function", "function": {"name": item["function"]["name"]}}
        for item in full["tools"]
    ]
    assert inventory["tool_views"] == full["tool_views"]
    assert inventory["summary"] == full["summary"]


def test_session_capabilities_send_names_not_large_argument_schemas(tmp_path):
    registry = ToolRegistry()
    tool = _SnapshotTool("large")
    schema = tool.get_schema()
    schema.parameters["properties"]["code"] = {"type": "string", "description": "nested tool directory " * 10000}
    tool.get_schema = Mock(return_value=schema)
    registry.register(tool)
    owner = SimpleNamespace(
        active_conversation_id="owner", session_id="session",
        session_lifecycle=SimpleNamespace(workspace_root_for_conversation=lambda: tmp_path),
        refresh_tool_registry_if_mcp_changed=Mock(), tool_registry=registry,
        permission_checker=None, permission_context=None, _mcp_registry_version_snapshot=0,
        runtime_toolset_policy=lambda _, **_kwargs: ToolsetPolicy(),
        _llm_selection_payload=lambda **_: {}, _provider_capabilities_payload=lambda _: {},
    )
    registry.get_schemas = Mock(wraps=registry.get_schemas)
    snapshot = WebSocketSession.runtime_capability_snapshot(owner, include_catalogs=False)
    assert snapshot["tools"] == [{"type": "function", "function": {"name": tool.name}}]
    assert len(json.dumps(snapshot)) < 10000
    registry.get_schemas.assert_not_called()
    assert "parameters" in registry.get_schemas()[0]["function"]
