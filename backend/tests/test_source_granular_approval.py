from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.bootstrap.app import AppBootstrap
from backend.config import PermissionSettings, permission_settings_from_config
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.config_requirements import ConfigRequirements, RequirementViolation, RequirementSource, RequirementsLayerEntry, compose_requirements
from backend.permissions.approval_policy import ApprovalPolicy, policy_for_mode
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import FileSystemSandboxPolicy, PermissionProfile, SandboxPolicy
from backend.tools.base import BaseTool, PermissionLevel, ToolSchema
from backend.tools.contracts import ToolSpec


def granular(*, sandbox=True, rules=True, elicitation=True, **optional):
    return {"granular": {"sandbox_approval": sandbox, "rules": rules, "mcp_elicitations": elicitation, **optional}}


@pytest.mark.parametrize("raw", ["on-request", "never", "untrusted", "on-failure"])
def test_policy_strings_follow_source_alias(raw):
    assert ApprovalPolicy.parse(raw).to_wire() == ("on-request" if raw == "on-failure" else raw)


def test_granular_required_fields_and_false_optional_defaults():
    policy = ApprovalPolicy.parse(granular(sandbox=False, rules=True, elicitation=False))
    assert policy.to_wire() == granular(sandbox=False, rules=True, elicitation=False, skill_approval=False, request_permissions=False)
    assert policy.permits("rules")
    assert not policy.permits("sandbox_approval")
    assert policy.permits("mcp_tool_call") and policy.permits("network_access")
    assert not policy.permits("request_permissions")
    assert not policy.permits("skill_approval")
    assert policy_for_mode("bypass", policy).to_wire() == "never"
    assert policy_for_mode("confirm", policy) is policy


@pytest.mark.parametrize("raw", ["granular", "maybe", {"granular": {"rules": True}}, granular(rules="false"), granular(skill_approval="true"), {"granular": True}])
def test_invalid_policy_values_are_not_coerced_or_guessed(raw):
    with pytest.raises(ValueError):
        ApprovalPolicy.parse(raw)


def test_layered_partial_granular_override_merges_before_source_validation():
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"approval_policy": granular(request_permissions=True)}),
        ConfigLayer(ConfigLayerSource("session_flags"), {"approval_policy": {"granular": {"rules": False}}})))
    effective = stack.effective_config()
    settings = permission_settings_from_config(effective)
    context = PermissionChecker(settings).build_context(mode="confirm")
    assert context.approval_policy == "granular"
    assert context.granular_approval == {"sandbox_approval": True, "rules": False, "mcp_elicitations": True, "skill_approval": False, "request_permissions": True}
    assert context.approval_policy_value() == effective["approval_policy"]
    full = PermissionChecker(settings).build_context(mode="bypass")
    assert full.approval_policy_value() == "never" and full.granular_approval is None


def test_managed_policy_matches_the_complete_source_object_instead_of_just_kind():
    source = RequirementSource("system_requirements_toml", "requirements.toml")
    requirements = compose_requirements([RequirementsLayerEntry(source, {"allowed_approval_policies": [granular(rules=False)]})])
    requirements.ensure_approval_policy(granular(rules=False))
    requirements.ensure_permission_mode("confirm", approval_policy=granular(rules=False))
    with pytest.raises(RequirementViolation):
        requirements.ensure_approval_policy(granular(rules=True))
    with pytest.raises(RequirementViolation):
        requirements.ensure_permission_mode("bypass")
    with pytest.raises(RequirementViolation):
        requirements.approval_policy_for_mode("confirm")


class ShellAction(BaseTool):
    name = "run_command"
    permission = PermissionLevel.CONFIRM
    read_only = False

    def get_spec(self):
        return ToolSpec(self.name, capability="shell.execute")

    def get_schema(self):
        return ToolSchema(self.name, "Run", {"type": "object", "properties": {}})

    async def execute(self, args, context=None):
        raise AssertionError("A rejected policy must not execute the action")


class McpAction(ShellAction):
    name = "remote_action"

    def get_spec(self):
        return ToolSpec(self.name, capability="mcp", toolset="mcp")


def test_default_shell_request_and_explicit_rule_have_source_independent_gates():
    tool = ShellAction()
    defaults = permission_settings_from_config({"approval_policy": granular(sandbox=False, rules=True)})
    checker = PermissionChecker(defaults)
    context = checker.build_context(mode="confirm")
    assert checker.check(tool.name, {}, context=context, tool=tool) == PermissionLevel.ALWAYS_DENY
    explicit_rule = replace(defaults, content_ask_rules=["run_command(*)"])
    checker = PermissionChecker(explicit_rule)
    assert checker.check(tool.name, {"command": "Write-Output source"}, context=context, tool=tool) == PermissionLevel.CONFIRM
    no_rules = checker.build_context(mode="confirm", approval_policy=granular(sandbox=True, rules=False))
    assert checker.check(tool.name, {"command": "Write-Output source"}, context=no_rules, tool=tool) == PermissionLevel.ALWAYS_DENY


def test_mcp_tool_approval_uses_real_producer_kind_not_a_name_prefix():
    settings = PermissionSettings(require_confirm=["remote_action"], approval_policy=ApprovalPolicy.parse(granular(sandbox=False, rules=False, elicitation=False)))
    checker = PermissionChecker(settings)
    context = checker.build_context(mode="confirm")
    tool = McpAction()
    assert checker.check(tool.name, {}, context=context, tool=tool) == PermissionLevel.CONFIRM
    assert checker.check(tool.name, {}, context=replace(context, approval_policy="never", granular_approval=None), tool=tool) == PermissionLevel.ALWAYS_DENY


@pytest.mark.asyncio
@pytest.mark.parametrize("policy, sandbox, expected", [
    ("never", "danger-full-access", {"action": "accept", "content": {}}),
    ("never", "external-sandbox", {"action": "accept", "content": {}}),
    ("never", "workspace-write", {"action": "decline"}),
    ("never", "read-only", {"action": "decline"}),
    ("never", None, {"action": "decline"}),
    (granular(elicitation=False), "danger-full-access", {"action": "decline"}),
])
async def test_empty_mcp_form_uses_source_never_exception_and_child_live_policy(tmp_path, policy, sandbox, expected):
    parsed = ApprovalPolicy.parse(policy)
    context = PermissionContext(approval_policy=parsed.kind, granular_approval=parsed.granular.to_dict() if parsed.granular else None)
    holder = {"context": PermissionContext(approval_policy="on-request")}
    owner = {"conversation_id": "parent-conversation", "permission_context_provider": lambda: holder["context"]}
    if sandbox is not None:
        profile = PermissionProfile.disabled() if sandbox == "danger-full-access" else PermissionProfile.external() if sandbox == "external-sandbox" else PermissionProfile.managed(FileSystemSandboxPolicy.restricted([]))
        owner["mcp_permission_profile"] = SandboxPolicy(workspace_root=tmp_path, permission_profile=profile).resolve()
    session = SimpleNamespace(send_event=AsyncMock(), permission_context=PermissionContext(mode="bypass", approval_policy="never"))
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda params: (session, owner)
    holder["context"] = context
    result = await bootstrap._handle_mcp_elicitation({"mode": "form", "schema": {"type": "object", "properties": {}}})
    assert result == expected
    session.send_event.assert_not_called()


@pytest.mark.asyncio
async def test_granular_mcp_url_is_declined_without_registering_ui_or_running_hook():
    policy = ApprovalPolicy.parse(granular(elicitation=False))
    context = PermissionContext(approval_policy=policy.kind, granular_approval=policy.granular.to_dict())
    hooks = SimpleNamespace(has_hooks=lambda event: pytest.fail("a blocked Source category cannot run elicitation hooks"))
    owner = {"conversation_id": "conversation", "permission_context_provider": lambda: context, "hook_manager": hooks}
    bootstrap = object.__new__(AppBootstrap)
    bootstrap._resolve_mcp_request_session = lambda params: (SimpleNamespace(), owner)
    assert await bootstrap._handle_mcp_elicitation({"mode": "url", "url": "https://example.test"}) == {"action": "decline"}


def test_permission_mode_admission_uses_owning_config_and_full_managed_object():
    from backend.config import AppConfig, LLMSettings
    from backend.services.conversation_permission_service import plan_permission_mode_update

    source = RequirementSource("system_requirements_toml", "owned-requirements.toml")
    policy = granular(rules=False)
    requirements = compose_requirements([RequirementsLayerEntry(source, {"allowed_approval_policies": [policy]})])
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user"), {"approval_policy": policy}),), requirements=requirements)
    config = AppConfig(llm=LLMSettings(api_key=""), permissions=permission_settings_from_config(stack.effective_config()), config_layer_stack=stack)
    assert plan_permission_mode_update({"mode": "confirm"}, config=config).error_event is None
    assert plan_permission_mode_update({"mode": "bypass"}, config=config).error_event is not None
    from backend.ws.handler import WebSocketSession

    context = PermissionChecker(config.permissions).build_context(mode="confirm")
    session = SimpleNamespace(config=config, permission_context=context,
        session_lifecycle=SimpleNamespace(sandbox_capability_payload={"probe_status": "ready"}))
    payload = WebSocketSession._runtime_permission_payload(session)
    assert payload["approval_policy"] == ApprovalPolicy.parse(policy).to_wire()
    assert payload["available_modes"] == ["confirm", "auto", "plan"]


@pytest.mark.asyncio
async def test_standalone_plan_transitions_preserve_configured_granular_policy(tmp_path):
    from backend.tools.plan_tool import EnterPlanModeTool, ExitPlanModeTool

    policy = ApprovalPolicy.parse(granular(sandbox=False, rules=True, request_permissions=True))
    plan_path = tmp_path / "owned-plan.md"
    permission = PermissionChecker(PermissionSettings(approval_policy=policy)).build_context(
        mode="confirm", filesystem_constraints={"plan_files": [str(plan_path)]})
    context = ToolExecutionContext(permission=permission, workspace_root=tmp_path)
    entered = await EnterPlanModeTool().execute({}, context)
    assert not entered.is_error and context.permission.mode == "plan"
    assert context.permission.approval_policy_value() == policy.to_wire()
    exited = await ExitPlanModeTool().execute({"plan": "# Source plan\nImplement the approved change."}, context)
    assert not exited.is_error and context.permission.mode == "confirm"
    assert context.permission.approval_policy_value() == policy.to_wire()


@pytest.mark.asyncio
async def test_mcp_prepared_call_publishes_actual_profile_and_child_permission_owner(tmp_path):
    from backend.agent.run_context import RunContext
    from backend.mcp.client import MCPCallResult, MCPToolDef
    from backend.mcp.registry import MCPToolProxy

    holder = {"permission": PermissionChecker(PermissionSettings(approval_policy=ApprovalPolicy.parse(granular()))).build_context(mode="confirm")}
    profile = SandboxPolicy(workspace_root=tmp_path, permission_profile=PermissionProfile.external())
    context = ToolExecutionContext(permission=holder["permission"], workspace_root=tmp_path, sandbox_policy=profile,
        run_context=RunContext(permission_context_provider=lambda: holder["permission"], mcp_owner_session_id="child-session"))
    client = SimpleNamespace(connected=True, call_tool=AsyncMock(return_value=MCPCallResult(content=[{"type": "text", "text": "done"}])))
    proxy = MCPToolProxy("source", MCPToolDef(name="call", description="Source action"), client)
    result = await proxy.execute({}, context=context)
    assert not result.is_error
    owner = client.call_tool.call_args.kwargs["request_owner"]
    assert owner["mcp_permission_profile"] == profile.resolve(cwd=tmp_path)
    holder["permission"] = replace(holder["permission"], approval_policy="never", granular_approval=None)
    assert owner["permission_context_provider"]().approval_policy == "never"


@pytest.mark.asyncio
@pytest.mark.parametrize("policy, strict, intent, hook_allowed", [
    (granular(sandbox=False), False, "require_escalated", False),
    (granular(sandbox=False), True, "use_default", True),
    ("never", False, "require_escalated", False),
    ("never", True, "require_escalated", True),
])
async def test_stdin_sandbox_gate_precedes_hooks_except_source_strict_never(tmp_path, policy, strict, intent, hook_allowed):
    from backend.agent.run_context import RunContext
    from backend.terminal.stdin_authority import StdinAuthorityError, TerminalInputApproval
    from backend.tools.monitor_tool import MonitorTool

    parsed = ApprovalPolicy.parse(policy)
    permission = PermissionContext(approval_policy=parsed.kind,
        granular_approval=parsed.granular.to_dict() if parsed.granular is not None else None,
        strict_auto_review=strict)
    hooks = SimpleNamespace(run_permission_request=AsyncMock(return_value=SimpleNamespace(
        updated_input=None, blocked=False, permission_decision="allow")))
    context = ToolExecutionContext(permission=permission, workspace_root=tmp_path, run_context=RunContext(hook_manager=hooks))
    action = TerminalInputApproval("parent-call", "approval", "local", "process", 17, "input", str(tmp_path), False, intent, None, "Source approval")
    if hook_allowed:
        await MonitorTool()._approve_terminal_input(action, context, {})
        hooks.run_permission_request.assert_awaited_once()
    else:
        with pytest.raises(StdinAuthorityError, match="approval_policy"):
            await MonitorTool()._approve_terminal_input(action, context, {})
        hooks.run_permission_request.assert_not_called()


@pytest.mark.asyncio
async def test_sdk_without_explicit_context_retains_native_checker_config_policy(tmp_path):
    from backend.artifact.store import ArtifactStore
    from backend.config import AgentSettings, AppConfig, LLMSettings
    from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
    from backend.sdk import query
    from backend.tools.registry import ToolRegistry

    settings = permission_settings_from_config({"approval_policy": granular(sandbox=False, rules=False)})
    config = AppConfig(llm=LLMSettings(api_key="", provider="openai", model="source-fixture", base_url="https://fixture.invalid/v1"),
        agent=AgentSettings(code_mode_only=False, max_iterations=3), permissions=settings)

    class Model(LLMAdapter):
        def __init__(self):
            self._settings = config.llm
            self._provider_id = "openai"
            self.requests = 0

        async def stream_chat(self, messages, tools=None, metadata=None):
            self.requests += 1
            if self.requests == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(id="source-policy-call", name="run_command", arguments={})])
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="The configured policy rejected the command.", phase="final_answer")
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop", end_turn=True)

        async def simple_chat(self, messages, *, max_tokens=None):
            return "Owned transport."

    registry = ToolRegistry()
    registry.register(ShellAction())
    events = [event async for event in query("Apply the configured policy", config=config, llm=Model(),
        tool_registry=registry, workspace_root=tmp_path, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker=PermissionChecker(settings, tmp_path))]
    decisions = [event.data for event in events if event.type == "permission.decision"]
    assert decisions and decisions[0]["decision"] == "deny"
    assert decisions[0]["matched_rule"]["rule"] == "approval_policy:granular.sandbox_approval=false"
    assert not any(event.type == "approval_request" for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("policy, explicit_rule, escalated", [
    ("never", False, False), (granular(sandbox=False, rules=False), False, False),
    ("never", True, False), (granular(sandbox=False, rules=False), True, False),
    ("never", False, True), (granular(sandbox=False, rules=False), False, True),
])
async def test_real_strict_turn_grant_reaches_frozen_command_review_without_waiving_rule_or_sandbox_gate(tmp_path, policy, explicit_rule, escalated):
    import json
    import shlex
    import sys
    from backend.agent.context import ContextBuilder
    from backend.agent.guardian.host import GuardianHostState
    from backend.agent.state import AgentState
    from backend.agent.tool_batch_runner import ToolBatchRunner
    from backend.llm.base import ToolCallEvent
    from backend.permissions.grants import PermissionGrantResponse
    from backend.permissions.exec_policy import ExecPolicyOwner
    from backend.sandbox.policy import AdditionalPermissionProfile, FileSystemPermissions, FileSystemSandboxEntry, FileSystemPath, FileSystemAccessMode
    from backend.tests.test_codex_guardian import ControlledRuntime, guardian_context
    from backend.tools.command_tool import RunCommandTool
    from backend.tools.command_support import command_shell_argv
    from backend.tools.registry import ToolRegistry

    ControlledRuntime.outputs = ['{"outcome":"allow"}']
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    ctx, _ = guardian_context(tmp_path)
    settings = permission_settings_from_config({"approval_policy": policy,
        **({"permissions": {"require_confirm": ["run_command"]}} if explicit_rule else {})})
    mode = "confirm" if escalated else "bypass"
    ctx.permission = replace(PermissionChecker(settings, tmp_path).build_context(mode=mode, approval_policy=policy),
        workspace_root=tmp_path, sandbox_mode="workspace-write" if escalated else "danger-full-access")
    # Disabled parent execution can be narrowed to Source's managed readonly
    # reviewer. An External profile cannot supply that local reviewer boundary.
    ctx.sandbox_policy = SandboxPolicy.workspace_default(tmp_path) if escalated else SandboxPolicy(
        workspace_root=tmp_path, permission_profile=PermissionProfile.disabled())
    target = tmp_path / "reviewed.txt"
    ctx.permission_grants.record(PermissionGrantResponse(AdditionalPermissionProfile(
        file_system=FileSystemPermissions(entries=(FileSystemSandboxEntry(FileSystemPath.path(target), FileSystemAccessMode.WRITE),))),
        scope="turn", strict_auto_review=True), "local", ctx.turn_id)
    assert ctx.permission_grants.strict_auto_review_enabled(ctx.turn_id)
    builder = ContextBuilder()
    builder.bind_tool_context(ctx)
    host = GuardianHostState(ctx.conversation_id)
    host.bind(builder)
    host.evidence.record_accepted_message("user", "Write only the approved reviewed.txt file", turn_id=ctx.turn_id,
        message_id="real-user", acceptance_order=host.evidence.reserve_order())
    host.service.pool.runtime_factory = ControlledRuntime
    ctx.run_context.context_builder = builder
    ctx.run_context.guardian_review_service = host.service
    emissions = []
    async def emit(kind, data):
        emissions.append((kind, data))
    ctx.emit_event = emit
    script = "from pathlib import Path; Path('reviewed.txt').write_text('approved bytes')"
    command = ("& '" + sys.executable.replace("'", "''") + "' -c '" + script.replace("'", "''") + "'"
        if sys.platform == "win32" else shlex.quote(sys.executable) + " -c " + shlex.quote(script))
    args = {"command": command, **({"sandbox_permissions": "require_escalated"} if escalated else {})}
    native_home = tmp_path / "strict-exec-policy"
    if explicit_rule:
        rules = native_home / "rules"
        rules.mkdir(parents=True)
        # Source retains this PowerShell invocation as a generic command;
        # its executable, rather than the embedded Python string, owns the rule.
        executable = command_shell_argv(command, wrap_powershell=False)[0] if sys.platform == "win32" else sys.executable
        (rules / "default.rules").write_text(
            f'prefix_rule(pattern={json.dumps([executable])}, decision="prompt")', encoding="utf-8")
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(native_home / "settings.json")), {}),))
    ctx.run_context.exec_policy_owner = ExecPolicyOwner(home=native_home, stack=stack)
    registry = ToolRegistry()
    registry.register(RunCommandTool(ctx.artifact_store))
    state = AgentState(user_message="Write the approved file")
    batch = ToolBatchRunner(ctx=builder, state=state, tool_registry=registry,
        permission_checker=PermissionChecker(settings, tmp_path), approval_handler=None, skill_manager=None,
        permission_context=ctx.permission, tool_ctx=ctx)
    try:
        events = [event async for event in batch.run([ToolCallEvent(id="strict-call", name="run_command", arguments=args)])]
        decisions = [event.data for event in events if event.type == "permission.decision"]
        if escalated:
            assert not decisions and not target.exists()
            assert state.tool_calls[-1].error_kind == "exec_policy"
            assert not ControlledRuntime.instances
            assert not any(kind == "guardian_assessment" for kind, _ in emissions)
            return
        assert decisions and decisions[0]["request_digest"] == state.tool_calls[-1].request_digest
        if explicit_rule or escalated:
            assert not target.exists()
            assert decisions[0]["decision"] == "deny"
            assert decisions[0]["matched_rule"]["rule"] != "strict_auto_review"
            assert not ControlledRuntime.instances
            assert not any(kind == "guardian_assessment" for kind, _ in emissions)
        else:
            assert decisions[0]["matched_rule"] == {"source": "tool", "rule": "strict_auto_review"}
            assert state.tool_calls[-1].status == "success", json.dumps([
                {key: event.data.get(key) for key in ("summary", "content", "message", "error_kind")}
                for event in events if event.type in {"tool_result", "error"}], ensure_ascii=False, indent=2)
            assert target.read_text() == "approved bytes"
            if policy == "never":
                # Source ReviewRequest::decide validates Full access first,
                # even for strict requests, and emits no reviewer assessment.
                assert not ControlledRuntime.instances
                assert not any(kind == "guardian_assessment" for kind, _ in emissions)
            else:
                assert len(ControlledRuntime.instances) == 1
                assert "reviewed.txt" in ControlledRuntime.instances[0].prompts[0].text
                assert [data["status"] for kind, data in emissions if kind == "guardian_assessment"] == ["in_progress", "approved"]
            assert not any(event.type == "approval_request" for event in events)
    finally:
        await host.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_rule", [False, True])
async def test_never_managed_strict_grant_reviews_real_patch_and_keeps_explicit_diff_rule(tmp_path, explicit_rule):
    from backend.agent.context import ContextBuilder
    from backend.agent.final_tool_request import canonical_tool_request_digest
    from backend.agent.guardian.host import GuardianHostState
    from backend.agent.state import AgentState
    from backend.agent.tool_batch_runner import ToolBatchRunner
    from backend.llm.base import ToolCallEvent
    from backend.permissions.grants import PermissionGrantResponse
    from backend.sandbox.policy import AdditionalPermissionProfile, FileSystemPermissions, FileSystemSandboxEntry, FileSystemPath, FileSystemAccessMode
    from backend.tests.test_codex_guardian import ControlledRuntime, guardian_context
    from backend.tools.apply_patch import ApplyPatchTool
    from backend.tools.registry import ToolRegistry

    ControlledRuntime.outputs = ['{"outcome":"allow"}']
    ControlledRuntime.instances = []
    ControlledRuntime.on_run = None
    ctx, _ = guardian_context(tmp_path)
    settings = permission_settings_from_config({"approval_policy": "never",
        **({"permissions": {"require_diff_review": ["apply_patch"]}} if explicit_rule else {})})
    ctx.permission = replace(PermissionChecker(settings, tmp_path).build_context(mode="confirm"),
        workspace_root=tmp_path, sandbox_mode="workspace-write")
    target = tmp_path / "reviewed.txt"
    ctx.permission_grants.record(PermissionGrantResponse(AdditionalPermissionProfile(file_system=FileSystemPermissions(entries=(
        FileSystemSandboxEntry(FileSystemPath.path(target), FileSystemAccessMode.WRITE),))),
        scope="turn", strict_auto_review=True), "local", ctx.turn_id)
    builder = ContextBuilder()
    builder.bind_tool_context(ctx)
    host = GuardianHostState(ctx.conversation_id)
    host.bind(builder)
    host.evidence.record_accepted_message("user", "Apply only the reviewed.txt patch", turn_id=ctx.turn_id,
        message_id="real-user", acceptance_order=host.evidence.reserve_order())
    host.service.pool.runtime_factory = ControlledRuntime
    ctx.run_context.context_builder = builder
    ctx.run_context.guardian_review_service = host.service
    emissions = []
    async def emit(kind, data):
        emissions.append((kind, data))
    ctx.emit_event = emit
    registry = ToolRegistry()
    registry.register(ApplyPatchTool())
    state = AgentState(user_message="Apply the approved patch")
    batch = ToolBatchRunner(ctx=builder, state=state, tool_registry=registry,
        permission_checker=PermissionChecker(settings, tmp_path), approval_handler=None, skill_manager=None,
        permission_context=ctx.permission, tool_ctx=ctx)
    args = {"patch": "*** Begin Patch\n*** Add File: reviewed.txt\n+approved bytes\n*** End Patch"}
    try:
        events = [event async for event in batch.run([ToolCallEvent(id="strict-patch", name="apply_patch", arguments=args)])]
        decision = next(event.data for event in events if event.type == "permission.decision")
        assert decision["request_digest"] == canonical_tool_request_digest("apply_patch", args)
        if explicit_rule:
            assert decision["decision"] == "deny" and decision["matched_rule"]["rule"] != "strict_auto_review"
            assert not target.exists() and not ControlledRuntime.instances
        else:
            assert decision["matched_rule"] == {"source": "tool", "rule": "strict_auto_review"}
            assert state.tool_calls[-1].status == "success"
            assert target.read_text() == "approved bytes\n"
            assert len(ControlledRuntime.instances) == 1
            assert "approved bytes" in ControlledRuntime.instances[0].prompts[0].text
            assert [data["status"] for kind, data in emissions if kind == "guardian_assessment"] == ["in_progress", "approved"]
        assert not any(event.type == "approval_request" for event in events)
    finally:
        await host.aclose()


@pytest.mark.parametrize("mode", ["confirm", "bypass"])
@pytest.mark.parametrize("explicit_rule", [False, True])
def test_never_ordinary_command_skips_builtin_routing_but_keeps_configured_rule(tmp_path, mode, explicit_rule):
    from backend.tools.command_tool import RunCommandTool

    settings = permission_settings_from_config({"approval_policy": "never",
        **({"permissions": {"require_confirm": ["run_command"]}} if explicit_rule else {})})
    checker = PermissionChecker(settings, tmp_path)
    context = replace(checker.build_context(mode=mode, approval_policy="never"),
        workspace_root=tmp_path, sandbox_mode="external-sandbox")
    result = checker.evaluate("run_command", {"command": 'Write-Output "$(Get-Location)"'},
        context=context, tool=RunCommandTool(None))
    assert result.decision == ("deny" if explicit_rule else "allow")
    if explicit_rule:
        assert result.matched_rule == "approval_policy:never"
