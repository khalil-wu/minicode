from __future__ import annotations

import json

import pytest

from backend import config_helpers, managed_settings
from backend.agent.run_context import RunContext
from backend.artifact.store import ArtifactStore
from backend.config import load_config
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.config_file import write_mcp_config
from backend.mcp.manager import MCPServerManager, MCPServerState, ServerStatus
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.sdk import query
from backend.tools.registry import ToolRegistry


class HostApprovalModel(LLMAdapter):
    def __init__(self, settings, tool_name):
        self._settings = settings
        self._provider_id = "openai"
        self.tool_name = tool_name
        self.requests = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.requests.append((repr(messages), tools))
        if len(self.requests) == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                id="host-approval-call", name=self.tool_name, arguments={},
            )])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Finished.", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, messages, *, max_tokens=None):
        return "Owned fixture transport."


class OwnedMCPTransport:
    connected = True
    instructions = ""
    server_capabilities = {}
    server_info = {}

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments, *, request_owner):
        self.calls.append((name, arguments, request_owner))
        return MCPCallResult(content=[{"type": "text", "text": "Host approved output"}])


@pytest.mark.asyncio
@pytest.mark.parametrize("case,allowed", [
    ("default", True), ("raw_tool", True), ("hard_deny", False),
    ("managed_deny", False), ("destructive", True), ("plan", False),
    ("read_only_hint", True),
])
async def test_host_mcp_approval_config_reaches_sdk_result_without_waiving_boundaries(tmp_path, monkeypatch, case, allowed):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("MINICODE_MANAGED_SETTINGS_DIR", str(managed))
    monkeypatch.setenv("MINICODE_SYSTEM_CONFIG_FILE", str(tmp_path / "no-system.toml"))
    monkeypatch.setenv("MINICODE_REQUIREMENTS_FILE", str(tmp_path / "no-requirements.toml"))
    monkeypatch.setattr(managed_settings, "_load_admin_platform_settings", lambda: managed_settings.ManagedSettingsResult({}))
    monkeypatch.setattr(managed_settings, "_load_windows_registry_settings", lambda _hive: managed_settings.ManagedSettingsResult({}))
    identity = "mcp::host.server/run.op"
    config_helpers.SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_helpers.SETTINGS_FILE.write_text(json.dumps({
        "agent": {"code_mode_only": False},
        "llm": {"provider": "openai", "openai": {"base_url": "https://fixture.invalid/v1", "model": "fixture", "auth_header": False}},
        "permissions": {"always_deny": [identity] if case == "hard_deny" else []},
    }), encoding="utf-8")
    if case == "managed_deny":
        (managed / "managed-settings.json").write_text(json.dumps({"permissions": {"always_deny": [identity]}}), encoding="utf-8")
    server = {"transport": "stdio", "command": "python", "args": [], "default_tools_approval_mode": "approve"}
    if case == "raw_tool":
        server.update(default_tools_approval_mode="prompt", tools={"run.op": {"approval_mode": "approve"}})
    elif case == "read_only_hint":
        server["default_tools_approval_mode"] = "auto"
    path = tmp_path / ".mcp.json"
    write_mcp_config(json.dumps({"servers": {"host.server": server}}), path)
    manager = MCPServerManager(config_path=path, workspace_root=workspace, managed_settings_dir=managed)
    [server_config] = manager.load_config()
    assert server_config.name == "host.server"
    client = OwnedMCPTransport()
    definition = MCPToolDef("run.op", "Host operation", {"type": "object", "properties": {}, "additionalProperties": False},
        annotations={"readOnlyHint": case == "read_only_hint", "openWorldHint": case != "read_only_hint", "destructiveHint": case == "destructive"},
        meta={"anthropic/alwaysLoad": True})
    sibling = MCPToolDef("run_op", "Another raw tool", {"type": "object", "properties": {}})
    manager._servers[server_config.name] = MCPServerState(server_config, client=client, status=ServerStatus.CONNECTED, tools=[definition, sibling])
    registry = ToolRegistry()
    catalog = MCPToolRegistry(registry, mcp_manager=manager)
    catalog._publish_catalog([MCPToolProxy(server_config.name, definition, client, manager=manager), MCPToolProxy(server_config.name, sibling, client, manager=manager)])
    tool = next(tool for tool in registry.get_tools() if tool.policy_identity == identity)
    if case == "raw_tool":
        neighbor = next(tool for tool in registry.get_tools() if tool.policy_identity != identity)
        assert PermissionChecker(load_config(cwd=workspace, resolve_credentials=False).permissions, workspace).evaluate(
            neighbor.name, {}, context=PermissionContext(mode="confirm", approval_policy="never"), tool=neighbor,
        ).decision == "deny"
    config = load_config(cwd=workspace, resolve_credentials=False)
    context = PermissionContext(mode="plan" if case == "plan" else "confirm", approval_policy="never", workspace_root=workspace)
    decision = PermissionChecker(config.permissions, workspace).evaluate(tool.name, {}, context=context, tool=tool)
    assert (decision.decision == "allow") is allowed
    if allowed:
        assert decision.matched_rule_source == "tool"
        assert decision.request_digest
    model = HostApprovalModel(config.llm, tool.name)
    events = [event async for event in query("Run the host-approved operation", llm=model, config=config,
        tool_registry=registry, permission_context=context, workspace_root=workspace, max_iterations=3,
        run_context=RunContext(mcp_manager=manager, mcp_owner_session_id="host-approval-sdk"),
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"), metadata={"conversation_id": f"host-approval-{case}"},
    )]
    assert len(client.calls) == int(allowed)
    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert results[0].data.get("is_error", False) is not allowed
    assert len(model.requests) == 2
    if allowed:
        assert client.calls[0][0] == "run.op"
        assert "Host approved output" in model.requests[1][0]
    else:
        assert "Host approved output" not in model.requests[1][0]
