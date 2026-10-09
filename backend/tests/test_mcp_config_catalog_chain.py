from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.mcp import config_file, project_settings
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.manager import MCPServerConfig, MCPServerManager, MCPServerState, ServerStatus
from backend.mcp.policy import mcp_policy_from_requirements
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.tools.base import PermissionLevel
from backend.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    "transport,url",
    [("http", "file:///invalid"), ("sse", "wss://invalid/mcp"), ("ws", "https://invalid/mcp")],
)
def test_save_and_runtime_reject_the_same_transport_url_without_publication(tmp_path, transport, url):
    path = tmp_path / "mcp.json"
    original = '{"servers": {}}\n'
    path.write_text(original, encoding="utf-8")
    invalid = json.dumps({"servers": {"fixture": {"transport": transport, "url": url}}})
    with pytest.raises(ValueError, match="URL scheme"):
        config_file.write_mcp_config(invalid, path)
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("*.bak")) == []
    path.write_text(invalid, encoding="utf-8")
    with pytest.raises(ValueError, match="URL scheme"):
        MCPServerManager(config_path=path, workspace_root=None)._load_local_configs()


def test_save_preserves_url_templates_and_read_does_not_resolve_environment(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    monkeypatch.setenv("MINICODE_MCP_AUDIT_URL", "https://fixture.invalid/mcp")
    raw = {"servers": {"fixture": {"transport": "http", "url": "${MINICODE_MCP_AUDIT_URL}"}}}
    receipt = config_file.write_mcp_config(json.dumps(raw), path)
    assert json.loads(receipt["config"]["content"]) == raw
    configs = MCPServerManager(config_path=path, workspace_root=None)._load_local_configs()
    assert configs[0].url == "https://fixture.invalid/mcp"
    monkeypatch.delenv("MINICODE_MCP_AUDIT_URL")
    assert json.loads(config_file.read_mcp_config(path)["content"]) == raw
    with pytest.raises(ValueError, match="missing environment variable"):
        config_file.write_mcp_config(json.dumps(raw), path)
    monkeypatch.setenv("MINICODE_MCP_AUDIT_URL", "file:///invalid")
    with pytest.raises(ValueError, match="URL scheme"):
        config_file.write_mcp_config(json.dumps(raw), path)
    assert json.loads(path.read_text(encoding="utf-8")) == raw


def test_write_receipt_keeps_its_owner_when_another_writer_publishes_after_unlock(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    first = {"servers": {"first": {"transport": "http", "url": "https://first.invalid/mcp"}}}
    second = {"servers": {"second": {"transport": "http", "url": "https://second.invalid/mcp"}}}
    lock = config_file._MCP_CONFIG_WRITE_LOCK

    @contextmanager
    def publish_after_unlock():
        with lock:
            yield
        config_file.atomic_write_text(path, json.dumps(second))

    monkeypatch.setattr(config_file, "_MCP_CONFIG_WRITE_LOCK", publish_after_unlock())
    receipt = config_file.write_mcp_config(json.dumps(first), path)
    assert json.loads(path.read_text(encoding="utf-8")) == second
    assert json.loads(receipt["config"]["content"]) == first
    assert receipt["config"]["servers"] == receipt["servers"]
    assert receipt["servers"][0]["name"] == "first"


@pytest.mark.asyncio
async def test_registered_schema_and_permission_use_one_catalog_until_changed_definition_is_rejected(tmp_path):
    calls = []

    async def call_tool(name, args, **kwargs):
        calls.append((name, args))
        return MCPCallResult(content=[{"type": "text", "text": "done"}])

    definition = MCPToolDef(
        name="read", description="x" * 7000,
        input_schema={"type": "object", "properties": {}},
        annotations={"readOnlyHint": True}, meta={"ui": {"visibility": ["model"]}},
    )
    client = SimpleNamespace(connected=True, instructions="", call_tool=call_tool)
    manager = MCPServerManager(config_path=tmp_path / "mcp.json", workspace_root=None)
    manager._servers["fixture"] = MCPServerState(
        config=MCPServerConfig(name="fixture", transport="http", url="https://fixture.invalid/mcp"),
        client=client, tools=[definition], status=ServerStatus.CONNECTED,
    )
    tools = ToolRegistry()
    bridge = MCPToolRegistry(tools, mcp_manager=manager)
    bridge.register_server_tools("fixture", manager.get_all_tools()["fixture"], client)
    proxy = tools.get_tool("mcp__fixture__read")
    assert isinstance(proxy, MCPToolProxy)
    assert definition.description in proxy.get_schema().description
    assert proxy.permission is PermissionLevel.AUTO
    await manager._notify_status("fixture", ServerStatus.CONNECTED)
    assert not (await proxy.execute({})).is_error
    definition.annotations["readOnlyHint"] = False
    definition.input_schema["properties"]["required_new"] = {"type": "string"}
    definition.input_schema["required"] = ["required_new"]
    definition.meta["ui"]["visibility"][:] = ["app"]
    await manager._notify_status("fixture", ServerStatus.CONNECTED)
    assert proxy.permission is PermissionLevel.AUTO
    assert "required" not in proxy.get_schema().parameters
    result = await proxy.execute({})
    assert result.is_error and "catalog changed" in result.content
    assert calls == [("read", {})]
    bridge.sync()
    assert not tools.has_tool("mcp__fixture__read")


def test_requirement_regex_accepts_the_same_inline_flags_as_its_runtime_matcher():
    policy = mcp_policy_from_requirements(
        {"mcp_servers": {"fixture": {"identity": {"url": {
            "match": "regex", "expression": r"(?i)https://fixture\.invalid/mcp",
        }}}}}, source="audit",
    )
    config = MCPServerConfig(name="fixture", transport="http", url="https://FIXTURE.invalid/mcp")
    assert policy.disabled_reason(config) is None
    config.url += "/extra"
    assert policy.disabled_reason(config) is not None


def test_project_approval_normalizes_at_read_boundary_and_keeps_rejection_precedence(tmp_path, monkeypatch):
    monkeypatch.setattr(project_settings, "is_workspace_trusted", lambda root: True)
    path = project_settings.project_local_settings_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({"enabled_servers": [" fixture "], "disabled_servers": ["fixture"], "approve_all": True}), encoding="utf-8")
    assert project_settings.project_mcp_server_status("fixture", tmp_path) == "rejected"
    project_settings.approve_project_mcp_server("fixture", tmp_path)
    assert project_settings.project_mcp_server_status("fixture", tmp_path) == "approved"
    project_settings.reject_project_mcp_server("fixture", tmp_path)
    saved = project_settings.read_project_local_settings(tmp_path)
    assert saved == {"disabled_servers": ["fixture"], "approve_all": True}
    assert project_settings.project_mcp_server_status("fixture", tmp_path) == "rejected"
    path.write_text('{"enabled_servers": "fixture"}', encoding="utf-8")
    with pytest.raises(ValueError):
        project_settings.approve_project_mcp_server("fixture", tmp_path)
