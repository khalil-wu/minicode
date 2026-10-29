from __future__ import annotations

import itertools
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from backend.agent.guardian.contract import GuardianReceipt
from backend.agent.guardian.tool_approval import review_tool_request
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.tool_execution import _authorize_final_tool_request
from backend.config import AppConfig, AgentSettings, LLMSettings, PermissionSettings, TokenBudget
from backend.agent.query_engine import AgentSession
from backend.artifact.store import ArtifactStore
from backend.mcp.approval import MCPApprovalStore
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.mcp.client import MCPCallResult, MCPClient, MCPToolDef
from backend.mcp.config_file import write_mcp_config
from backend.mcp.manager import MCPServerConfig, MCPServerManager, MCPServerState, ServerStatus
from backend.mcp.registry import MCPToolProxy, MCPToolRegistry
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.grants import PermissionGrantResponse
from backend.sandbox.policy import (
    NetworkPermissions, AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath,
    FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemSpecialPath, NetworkSandboxPolicy,
    PermissionProfile, SandboxPolicy,
)
from backend.sdk import query
from backend.tools.base import PermissionLevel
from backend.tools.registry import ToolRegistry
from backend.tests.test_code_execution import run_model


class Transport:
    connected = True
    instructions = ""
    server_capabilities = {}
    server_info = {}

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments, *, request_owner):
        self.calls.append((name, dict(arguments), request_owner))
        return MCPCallResult(content=[{"type": "text", "text": "Actual MCP observation"}])


def proxy(annotations, mode="auto", *, parallel=False):
    client = Transport()
    definition = MCPToolDef("operation", "Exact operation", {"type": "object", "properties": {"value": {"type": "string"}}},
        annotations=annotations, meta={"anthropic/alwaysLoad": True})
    config = MCPServerConfig(name="source-server", command="python", default_tools_approval_mode=mode,
        supports_parallel_tool_calls=parallel)
    manager = SimpleNamespace(registry_version=0, get_server_contract=lambda name: (name, mode),
        get_server_config=lambda name: config, get_client=lambda name: client,
        catalog_is_cached=lambda name: False, get_all_tools=lambda: {"source-server": [definition]})
    return MCPToolProxy("source-server", definition, client, manager=manager), client, manager


def settings(**overrides):
    return PermissionSettings(require_confirm=[], require_diff_review=[], auto_allow=[], **overrides)


def policy(workspace, kind):
    if kind == "disabled":
        profile = PermissionProfile.disabled()
    elif kind == "external":
        profile = PermissionProfile.external(network=NetworkSandboxPolicy.RESTRICTED)
    elif kind == "managed-full":
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.unrestricted(), network=NetworkSandboxPolicy.RESTRICTED)
    else:
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
            FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.PROJECT_ROOTS), FileSystemAccessMode.WRITE),
        ]), network=NetworkSandboxPolicy.ENABLED)
    return SandboxPolicy(workspace_root=workspace, permission_profile=profile)


async def authorize(tool, workspace, sandbox, permission, *, permission_settings=None, context=None, arguments=None):
    registry = ToolRegistry()
    registry.register(tool)
    checker = PermissionChecker(permission_settings or settings(), workspace)
    context = context or ToolExecutionContext(permission, workspace_root=workspace, sandbox_policy=sandbox, tool_registry=registry,
        session_id="owned-session", conversation_id="owned-thread", turn_id="owned-turn")
    call = ToolCallEvent(id="frozen-mcp-call", name=tool.name,
        arguments={"value": "original"} if arguments is None else arguments)
    return (await _authorize_final_tool_request(call, tool_registry=registry, permission_checker=checker,
        permission_context=permission, tool_ctx=context)), context, registry


@pytest.mark.asyncio
@pytest.mark.parametrize("read,destructive,world", itertools.product((None, False, True), repeat=3))
async def test_source_auto_preserves_three_state_annotations(tmp_path, read, destructive, world):
    annotations = {key: value for key, value in (("readOnlyHint", read), ("destructiveHint", destructive), ("openWorldHint", world)) if value is not None}
    tool, _, _ = proxy(annotations)
    expected = destructive is True or read is not True and not (destructive is False and world is False)
    result, _, _ = (await authorize(tool, tmp_path, policy(tmp_path, "managed-workspace"), PermissionContext(workspace_root=tmp_path)))
    assert result.permission_decision.permission_level is (PermissionLevel.CONFIRM if expected else PermissionLevel.AUTO)
    assert tool.is_concurrency_safe() is (read is True)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,read,destructive,world,expected", [
    ("writes", True, True, True, PermissionLevel.AUTO),
    ("writes", False, False, False, PermissionLevel.CONFIRM),
    ("prompt", True, False, False, PermissionLevel.CONFIRM),
    ("approve", False, True, True, PermissionLevel.AUTO),
])
async def test_source_mode_and_parallel_overrides_are_independent(tmp_path, mode, read, destructive, world, expected):
    tool, _, _ = proxy({"readOnlyHint": read, "destructiveHint": destructive, "openWorldHint": world}, mode, parallel=True)
    result, _, _ = (await authorize(tool, tmp_path, policy(tmp_path, "managed-workspace"), PermissionContext(workspace_root=tmp_path)))
    assert result.permission_decision.permission_level is expected
    assert tool.is_concurrency_safe() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,allowed", [("disabled", True), ("external", True), ("managed-full", True), ("managed-workspace", False), ("missing", False)])
async def test_never_uses_captured_real_profile_and_not_bypass_or_network_labels(tmp_path, kind, allowed):
    tool, _, _ = proxy({"destructiveHint": True, "openWorldHint": True}, "prompt")
    permission = PermissionContext(mode="bypass", approval_policy="never", sandbox_mode="danger-full-access", workspace_root=tmp_path)
    result, _, _ = (await authorize(tool, tmp_path, None if kind == "missing" else policy(tmp_path, kind), permission))
    assert (result.permission_decision.decision == "allow") is allowed
    assert result.request.arguments == {"value": "original"}
    assert len(result.request.digest) == 64


@pytest.mark.asyncio
async def test_dynamic_final_authorization_rechecks_the_actual_issuing_profile(tmp_path):
    tool, _, _ = proxy({"destructiveHint": True})
    permission = PermissionContext(mode="bypass", approval_policy="never", workspace_root=tmp_path)
    first, context, _ = (await authorize(tool, tmp_path, policy(tmp_path, "disabled"), permission))
    assert first.permission_decision.decision == "allow"
    context.sandbox_policy = policy(tmp_path, "managed-workspace")
    second, _, _ = (await authorize(tool, tmp_path, context.sandbox_policy, permission, context=context))
    assert second.request.digest == first.request.digest
    assert second.permission_decision.decision == "deny"


@pytest.mark.asyncio
async def test_explicit_asks_denies_and_plan_are_not_removed_by_source_preapproval(tmp_path):
    tool, _, _ = proxy({"readOnlyHint": True}, "approve")
    permission = PermissionContext(approval_policy="never", workspace_root=tmp_path)
    actual = policy(tmp_path, "disabled")
    for rules in (settings(always_deny=[tool.policy_identity]),
        replace(settings(), require_confirm=["mcp__*"]),
        replace(settings(), require_diff_review=[tool.policy_identity])):
        result, _, _ = (await authorize(tool, tmp_path, actual, permission, permission_settings=rules))
        assert result.permission_decision.decision == "deny"
    denied, _, _ = (await authorize(tool, tmp_path, actual, replace(permission, tool_deny_rules=[tool.policy_identity])))
    assert denied.permission_decision.decision == "deny"
    planned, _, _ = (await authorize(tool, tmp_path, actual, replace(permission, mode="plan")))
    assert planned.permission_decision.decision == "deny"


@pytest.mark.asyncio
async def test_real_strict_turn_grant_requires_guardian_even_for_approve_and_never_full_access(tmp_path):
    tool, _, _ = proxy({"readOnlyHint": True}, "approve")
    permission = PermissionContext(approval_policy="never", workspace_root=tmp_path)
    context = ToolExecutionContext(permission, workspace_root=tmp_path, sandbox_policy=policy(tmp_path, "disabled"),
        session_id="owned-session", conversation_id="owned-thread", turn_id="owned-turn", run_context=RunContext())
    context.permission_grants.record(PermissionGrantResponse(AdditionalPermissionProfile(network=NetworkPermissions(enabled=True)), "turn", True), "local", context.turn_id)
    result, _, registry = (await authorize(tool, tmp_path, context.sandbox_policy, permission, context=context))
    assert result.permission_decision.permission_level is PermissionLevel.CONFIRM
    assert (result.permission_decision.matched_rule_source, result.permission_decision.matched_rule) == ("tool", "strict_auto_review")
    model = SourceModel(LLMSettings(provider="openai", model="fixture", api_key=""), tool.name)
    config = AppConfig(llm=model._settings, permissions=settings())
    context.model_execution = ModelExecutionSnapshot.capture(config, model)
    observed = []
    class Reviewer:
        async def decide(self, action, issuing, *, strict_auto_review):
            observed.append((action, issuing, strict_auto_review))
            return GuardianReceipt("real-review", action.request_digest, action.policy_identity, action.turn_id, action.environment_id, "approved")
    context.run_context.guardian_review_service = Reviewer()
    response = await review_tool_request(result.request, context, registry)
    assert observed[0][2] is True
    assert observed[0][0].payload["arguments"] == {"value": "original"}
    assert response["request_digest"] == result.request.digest


class SourceModel(LLMAdapter):
    def __init__(self, settings, tool_name):
        self._settings = settings
        self._provider_id = "openai"
        self.tool_name = tool_name
        self.requests = []

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.requests.append(repr(messages))
        if len(self.requests) == 1:
            yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="source-call", name=self.tool_name, arguments={"value": "original"})])
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
        else:
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Completed", phase="final_answer")
            yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

    async def simple_chat(self, messages, *, max_tokens=None):
        return "Unused"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,mode,annotations,allowed", [
    ("managed-workspace", "auto", {"readOnlyHint": True, "openWorldHint": True}, True),
    ("managed-workspace", "auto", {"destructiveHint": False, "openWorldHint": False}, True),
    ("managed-workspace", "auto", {}, False),
    ("disabled", "auto", {"destructiveHint": True, "openWorldHint": True}, True),
    ("external", "prompt", {}, True),
    ("managed-full", "prompt", {}, True),
    ("managed-workspace", "prompt", {"readOnlyHint": True}, False),
    ("managed-workspace", "approve", {"destructiveHint": True}, True),
])
async def test_frozen_mcp_proxy_reaches_real_query_execution_or_blocked_projection(tmp_path, monkeypatch, kind, mode, annotations, allowed):
    from backend.agent.environment_selection import EnvironmentConfig, EnvironmentConfigState, TurnEnvironmentRequest

    tool, transport, manager = proxy(annotations, mode)
    peer = tmp_path / "owned_mcp_server.py"
    definition = {"name": tool._tool_def.name, "description": tool._tool_def.description,
        "inputSchema": tool._tool_def.input_schema, "annotations": annotations, "_meta": tool._tool_def.meta}
    peer.write_text('import json,sys\nTOOL=' + repr(definition) + '\n' + '''for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request["method"]
    if method=="initialize": result={"protocolVersion":request["params"]["protocolVersion"],"capabilities":{"tools":{}},"serverInfo":{"name":"owned-local-mcp","version":"1"}}
    elif method=="tools/list": result={"tools":[TOOL]}
    else: result={"content":[{"type":"text","text":"Actual MCP observation"}]}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
''', encoding="utf-8")
    config_path = tmp_path / "source-mcp.json"
    write_mcp_config(json.dumps({"servers": {"source-server": {
        "transport": "stdio", "command": sys.executable, "args": [str(peer)], "startup_timeout_sec": 3,
        "default_tools_approval_mode": mode,
    }}}), config_path)
    manager = MCPServerManager(config_path=config_path, workspace_root=tmp_path)
    [server] = manager.load_config()
    manager._servers[server.name] = MCPServerState(server, client=transport, status=ServerStatus.CONNECTED, tools=[tool._tool_def])
    tool = MCPToolProxy(server.name, tool._tool_def, transport, manager=manager)
    real_policy = policy(tmp_path, kind)
    original_call = MCPClient.call_tool
    async def observe_call(client, name, arguments, *, request_owner=None, request_meta=None):
        transport.calls.append((name, arguments, request_owner))
        return await original_call(client, name, arguments, request_owner=request_owner, request_meta=request_meta)
    monkeypatch.setattr(MCPClient, "call_tool", observe_call)
    config = AppConfig(llm=LLMSettings(provider="openai", model="fixture", api_key="", auth_header=False), permissions=settings())
    model = SourceModel(config.llm, tool.name)
    registry = ToolRegistry()
    catalog = MCPToolRegistry(registry, mcp_manager=manager)
    catalog._publish_catalog([tool])
    run = RunContext(mcp_manager=manager, mcp_owner_session_id="source-owner")
    events = [event async for event in query("Use the exact remote operation", llm=model, config=config, tool_registry=registry,
        permission_context=PermissionContext(approval_policy="never", workspace_root=tmp_path), workspace_root=tmp_path,
        environment_requests=(TurnEnvironmentRequest("local", tmp_path.as_uri(), (tmp_path.as_uri(),),
            EnvironmentConfigState.ready(EnvironmentConfig(real_policy.permission_profile, (tmp_path.as_uri(),)))),),
        metadata={"conversation_id": f"mcp-source-{kind}-{mode}"}, run_context=run, max_iterations=3)]
    results = [event for event in events if event.type == "tool_result"]
    assert len(results) == 1
    assert len(transport.calls) == int(allowed)
    assert results[0].data["activity_kind"] == "mcpToolCall"
    assert len(results[0].data["request_digest"]) == 64
    assert results[0].data["is_error"] is (not allowed)
    if allowed:
        assert transport.calls[0][0:2] == ("operation", {"value": "original"})
        assert transport.calls[0][2]["mcp_permission_scope"].permission_profile == real_policy.permission_profile
        assert transport.calls[0][2]["mcp_permission_profile"] is None
        assert "Actual MCP observation" in model.requests[-1]
    else:
        assert results[0].data["status"] == "blocked"
        assert "Actual MCP observation" not in model.requests[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,expected_prompts", [("auto", 1), ("prompt", 2), ("writes", 2)])
async def test_real_query_remembers_only_source_auto_tool_across_changed_arguments(tmp_path, mode, expected_prompts):
    tool, transport, _ = proxy({}, mode)

    class TwoOperations(LLMAdapter):
        def __init__(self):
            self._settings = LLMSettings(api_key="", model="fixture", tool_mode="direct")
            self.steps = 0

        async def simple_chat(self, messages, *, max_tokens=None):
            return "Unused auxiliary request"

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.steps += 1
            if self.steps <= 2:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                    id=f"owned-mcp-{self.steps}", name=tool.name, arguments={"value": "first" if self.steps == 1 else "different"})])
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Both requested operations completed.", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")

    prompts = []
    async def approval(request_id):
        prompts.append(request_id)
        return {"action": "approve", "remember_for_session": True}

    model = TwoOperations()
    registry = ToolRegistry()
    registry.register(tool)
    session = AgentSession(llm=model, tool_registry=registry, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(settings(), tmp_path), agent_settings=AgentSettings(max_iterations=5, stream_max_attempts=0, code_mode_only=False),
        token_budget=TokenBudget(total=96000), approval_handler=approval)
    state, _, _, events, owner = await run_model(tmp_path, model, [tool], session=session, mode="confirm")
    assert state.terminal_status == "completed"
    assert prompts == [f"owned-mcp-{index}" for index in range(1, expected_prompts + 1)]
    assert [call[1] for call in transport.calls] == [{"value": "first"}, {"value": "different"}]
    approval_events = [event for event in events if event.type == "approval_request"]
    assert len(approval_events) == expected_prompts
    assert approval_events[0].data["mcp_approval"]["allow_session_remember"] is (mode == "auto")
    assert len(owner.mcp_tool_approvals.session_approved) == int(mode == "auto")


@pytest.mark.asyncio
async def test_remembered_tool_does_not_bypass_strict_review_explicit_ask_or_other_thread(tmp_path):
    tool, _, _ = proxy({})
    store = MCPApprovalStore({tool.session_approval_key()})
    permission = PermissionContext(workspace_root=tmp_path)
    context = ToolExecutionContext(permission, workspace_root=tmp_path, sandbox_policy=policy(tmp_path, "managed-workspace"),
        run_context=RunContext(mcp_tool_approvals=store))
    allowed, _, _ = (await authorize(tool, tmp_path, context.sandbox_policy, permission, context=context))
    assert allowed.permission_decision.permission_level is PermissionLevel.AUTO
    explicit, _, _ = (await authorize(tool, tmp_path, context.sandbox_policy, permission, context=context,
        permission_settings=replace(settings(), require_confirm=[tool.policy_identity])))
    assert explicit.permission_decision.permission_level is PermissionLevel.CONFIRM
    context.turn_id = "cache-strict-turn"
    context.permission_grants.record(PermissionGrantResponse(AdditionalPermissionProfile(network=NetworkPermissions(enabled=True)), "turn", True), "local", context.turn_id)
    reviewed, _, _ = (await authorize(tool, tmp_path, context.sandbox_policy, permission, context=context))
    assert reviewed.permission_decision.permission_level is PermissionLevel.CONFIRM
    fresh, _, _ = (await authorize(tool, tmp_path, context.sandbox_policy, permission))
    assert fresh.permission_decision.permission_level is PermissionLevel.CONFIRM
