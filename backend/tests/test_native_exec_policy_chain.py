from __future__ import annotations

from dataclasses import replace
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.model_execution import ModelExecutionSnapshot
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_serial
from backend.agent.tool_execution import _authorize_final_tool_request, _bind_final_tool_request, run_tool_with_timeout
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings, PermissionSettings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.config_requirements import ConfigRequirements, ConfigRequirementsError, RequirementSource, RequirementsLayerEntry, compose_requirements
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType, ToolCallEvent
from backend.llm.provider_contracts import ModelDefinition
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.exec_policy import ExecPolicyOwner, config_folders, load_trusted_policy_sources
from backend.permissions.native_execpolicy import NativeExecPolicyRuntime, SOURCE_COMMIT
from backend.permissions.prompt import permission_instructions
from backend.permissions.grants import PermissionGrantResponse, normalize_permission_profile
from backend.sandbox.policy import PermissionProfile, SandboxPolicy
from backend.tools.base import PermissionLevel
from backend.tools.command_tool import RunCommandTool
from backend.tools.registry import ToolRegistry


def setup(tmp_path, *, approval_policy="untrusted", rules="", ignored=False, model_specialty=None):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir(exist_ok=True)
    workspace.mkdir(exist_ok=True)
    if rules:
        (home / "rules").mkdir(exist_ok=True)
        (home / "rules/default.rules").write_text(rules, encoding="utf-8")
    requirements = ConfigRequirements(raw={"auto_review": {"ignore_rules": ["source-exec-model"]}} if ignored else {})
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(home / "settings.json")), {}),), requirements)
    config = AppConfig(llm=LLMSettings(api_key="", provider="custom", model="source-exec-model"), config_layer_stack=stack)
    info = ModelDefinition(provider="custom", id="source-exec-model", name="Source execution test",
        api="openai-completions", base_url="http://127.0.0.1/unused",
        extra={"model_specialty": model_specialty} if model_specialty is not None else {})
    snapshot = ModelExecutionSnapshot(config=config, llm=SimpleNamespace(), provider="custom", model=info.id, model_info=info)
    owner = ExecPolicyOwner(home=home, stack=stack)
    registry = ToolRegistry()
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    tool = RunCommandTool(store)
    registry.register(tool)
    events = []
    async def emit(kind, payload):
        events.append((kind, payload))
    permission = PermissionContext(mode="confirm", approval_policy=approval_policy,
        workspace_root=workspace, conversation_id="source-owner")
    run = RunContext(model_execution=snapshot, exec_policy_owner=owner, active_tool_registry=registry)
    checker = PermissionChecker(PermissionSettings(), workspace)
    context = ToolExecutionContext(permission=permission, run_context=run, model_execution=snapshot,
        workspace_root=workspace, sandbox_policy=SandboxPolicy(permission_profile=PermissionProfile.disabled(), workspace_root=workspace),
        permission_checker=checker, tool_registry=registry, artifact_store=store, emit_event=emit,
        conversation_id="source-owner", session_id="source-session", turn_id="source-turn", metadata={"run_id": "source-turn"})
    return SimpleNamespace(home=home, workspace=workspace, stack=stack, snapshot=snapshot, owner=owner,
        registry=registry, tool=tool, context=context, checker=checker, events=events)


def authorize(host, *, ident="exec-call", command='Write-Output "SOURCE_NATIVE_COMMAND"', prefix=None, **args):
    arguments = {"command": command, "login": False, **args}
    if prefix is not None:
        arguments["prefix_rule"] = prefix
    call = ToolCallEvent(id=ident, name="run_command", arguments=arguments)
    result = _authorize_final_tool_request(call, tool_registry=host.registry, permission_checker=host.checker,
        permission_context=host.context.permission, tool_ctx=host.context)
    assert not result.error, result.error
    _bind_final_tool_request(call, result.request)
    return call, result


def test_actual_packaged_engine_identity_and_source_parser():
    native = NativeExecPolicyRuntime()
    assert "backend" in native.directory.parts and ".tmp" not in native.directory.parts
    assert native.identity()["source_commit"] == SOURCE_COMMIT
    result = native.invoke({"operation": "inspect", "sources": [{"path": "actual.rules", "contents":
        'def add(verb):\n    prefix_rule(pattern=["git", verb])\nfor verb in ["status", "diff"]:\n    add(verb)\n'}], "ignore_allow_prefix_rules": False})
    assert result["policy"]["allowed_prefixes"] == [["git", "diff"], ["git", "status"]]


def test_trusted_layers_keep_source_order_extension_case_and_disabled_project(tmp_path):
    layers = []
    for kind, folder in (("system", "system"), ("user", "user"), ("project", "project"), ("project", "project/child")):
        directory = tmp_path / folder
        (directory / "rules").mkdir(parents=True, exist_ok=True)
        (directory / "rules/z.rules").write_text('prefix_rule(pattern=["z"])', encoding="utf-8")
        (directory / "rules/a.rules").write_text('prefix_rule(pattern=["a"])', encoding="utf-8")
        (directory / "rules/no.RULES").write_text("invalid", encoding="utf-8")
        source = ConfigLayerSource(kind, project_config_folder=str(directory)) if kind == "project" else ConfigLayerSource(kind, file=str(directory / "config.toml"))
        layers.append(ConfigLayer(source, {}, disabled_reason="untrusted" if folder.endswith("child") else None))
    stack = ConfigLayerStack(tuple(layers))
    assert len(config_folders(stack)) == 3
    sources = load_trusted_policy_sources(stack)
    assert [Path(item.path).name for item in sources] == ["a.rules", "z.rules"] * 3
    assert len(load_trusted_policy_sources(stack, ignore_user_and_project_rules=True)) == 2


def test_invalid_user_rules_produce_real_warning_and_keep_managed_policy(tmp_path):
    host = setup(tmp_path, rules='prefix_rule(pattern=["git"], match=[["hg"]])')
    assert host.owner.load_warning is not None
    assert host.owner.load_warning.error.location["path"].endswith("default.rules")
    assert host.owner.inspect_for_model(host.snapshot)["allowed_prefixes"] == []
    requirements = ConfigRequirements(raw={"rules": {"prefix_rules": [{"pattern": [{"token": "git"}], "decision": "forbidden"}]}})
    managed = ExecPolicyOwner(home=host.home, stack=replace(host.stack, requirements=requirements))
    assert managed.load_warning is not None
    assert managed.inspect_for_model(host.snapshot)["rule_count"] == 1


def test_requirements_use_original_native_producer_and_higher_layer_replaces_complete_rule_table():
    source = RequirementSource("system_requirements_toml", location="actual-requirements.toml")
    with pytest.raises(ConfigRequirementsError, match="not permitted in requirements.toml"):
        compose_requirements([RequirementsLayerEntry(source, {"rules": {"prefix_rules": [
            {"pattern": [{"token": "git"}], "decision": "allow"}]}})])
    with pytest.raises(ConfigRequirementsError, match="prefix_rules"):
        compose_requirements([RequirementsLayerEntry(source, {"rules": {"prefix_rules": [
            {"pattern": [{"token": "git"}], "decision": "forbidden"}]}}), RequirementsLayerEntry(source, {"rules": {}})])


def test_final_digest_binds_both_source_policy_and_actual_execution_argv(tmp_path):
    host = setup(tmp_path)
    _, auth = authorize(host, prefix=["Write-Output"])
    request = auth.request
    native = request.exec_policy
    assert request.digest == native.command.execution_digest
    assert request.digest != request.arguments_digest
    assert native.kind == "needs_approval"
    assert native.amendment_array() == ["Write-Output"]
    assert native.command.argv[0] == native.command.policy_argv[0]
    assert "SOURCE_NATIVE_COMMAND" in native.command.argv[-1]
    assert native.parsed_commands == (("Write-Output", "SOURCE_NATIVE_COMMAND"),)
    _, other = authorize(host, ident="second", command='Write-Output "DIFFERENT"', prefix=["Write-Output"])
    assert other.request.digest != request.digest


@pytest.mark.parametrize("ignored,specialty", [(True, None), (False, "cyber")])
def test_admitted_model_filter_controls_actual_requirement_and_same_permission_prompt(tmp_path, ignored, specialty):
    host = setup(tmp_path, rules='prefix_rule(pattern=["Write-Output"], decision="allow")', ignored=ignored, model_specialty=specialty)
    _, auth = authorize(host, prefix=["Write-Output"])
    assert auth.request.exec_policy.kind == "needs_approval"
    assert auth.request.exec_policy.proposed_amendment is None
    assert "Approved command prefixes" not in permission_instructions(host.context)


def test_safe_skip_is_not_promoted_by_required_on_models_but_strict_review_is(tmp_path):
    host = setup(tmp_path, approval_policy="on-request")
    host.snapshot.config.config_layer_stack.requirements.raw["auto_review"] = {"required_on_models": [host.snapshot.model]}
    _, auth = authorize(host)
    assert auth.permission_decision.permission_level == PermissionLevel.AUTO
    granted = normalize_permission_profile({"network": {"enabled": True}}, cwd=host.workspace)
    host.context.permission_grants.record(PermissionGrantResponse(granted, "turn", True),
        host.context.environment_id, host.context.turn_id)
    _, strict = authorize(host, ident="strict")
    assert strict.permission_decision.permission_level == PermissionLevel.CONFIRM
    assert strict.permission_decision.matched_rule == "strict_auto_review"


@pytest.mark.asyncio
async def test_real_serial_approval_persists_original_array_then_executes_and_next_call_skips(tmp_path):
    from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
    from backend.ws.turn_wait_state import TurnWaitState
    host = setup(tmp_path)
    call, auth = authorize(host, prefix=["Write-Output"], justification="SOURCE_NATIVE_PUBLIC_REASON")
    builder = ContextBuilder()
    builder.bind_tool_context(host.context)
    runtime = SessionApprovalRuntimeMixin()
    runtime.turn_wait_state = TurnWaitState()
    runtime.approval_diff_cache = {}
    runtime.config = SimpleNamespace(agent=SimpleNamespace(approval_timeout_seconds=None))
    runtime.run_manager = SimpleNamespace(run_tasks={})
    replayed = []
    async def send_payload(payload, **kwargs):
        replayed.append(json.loads(json.dumps(payload)))
    runtime.send_payload = send_payload
    observed = []
    async def approve(call_id):
        event = next(event for event in observed if event.type == "approval_request")
        assert event.data["proposed_execpolicy_amendment"] == ["Write-Output"]
        assert event.data["request_digest"] == auth.request.digest
        assert event.data["display_command"] == auth.request.arguments["command"]
        assert event.data["reason"] == "SOURCE_NATIVE_PUBLIC_REASON"
        assert event.data["command"] == list(auth.request.exec_policy.command.policy_argv)
        event.data.update(conversation_id="source-owner", turn_id="source-turn", message_id="source-answer")
        wire = runtime.build_approval_request_payload(event)
        await runtime.reemit_pending_state("source-owner")
        assert replayed == [wire]
        assert wire["request"]["display_command"] == 'Write-Output "SOURCE_NATIVE_COMMAND"'
        assert wire["request"]["reason"] == "SOURCE_NATIVE_PUBLIC_REASON"
        decision = {"action": "approve", "request_digest": auth.request.digest,
            "decision": {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": ["Write-Output"]}}}
        assert runtime._resolve_pending_approval(call_id, decision)
        return await runtime.approval_handler(call_id)
    async for event in execute_serial(call, perm=auth.permission_decision.permission_level,
        permission_decision=auth.permission_decision, permission_checker=host.checker, permission_context=host.context.permission,
        ctx=builder, state=AgentState(user_message="native source approval"), tool_registry=host.registry,
        tool_ctx=host.context, approval_handler=approve, skill_manager=None, iteration_id="iter:1"):
        observed.append(event)
    result = next(event for event in observed if event.type == "tool_result")
    assert not result.data["is_error"], result.data
    assert "SOURCE_NATIVE_COMMAND" in result.data["summary"]
    assert host.owner.default_policy_path.is_file()
    assert host.owner.revision == 1
    assert host.owner.inspect_for_model(host.snapshot)["allowed_prefixes"] == [["Write-Output"]]
    host.context.permission = replace(host.context.permission, approval_policy="on-request")
    assert '["Write-Output"]' in permission_instructions(host.context)
    _, second = authorize(host, ident="next")
    assert second.permission_decision.permission_level == PermissionLevel.AUTO
    assert second.request.exec_policy.bypass_sandbox is True
    cold = ExecPolicyOwner(home=host.home, stack=host.stack)
    assert cold.inspect_for_model(host.snapshot)["allowed_prefixes"] == [["Write-Output"]]


@pytest.mark.asyncio
async def test_failed_real_amendment_write_warns_without_reuse_and_still_allows_approved_call(tmp_path):
    host = setup(tmp_path)
    call, auth = authorize(host, prefix=["Write-Output"])
    (host.home / "rules").write_text("preserved blocking file", encoding="utf-8")
    builder = ContextBuilder()
    builder.bind_tool_context(host.context)
    async def approve(call_id):
        return {"action": "approve", "request_digest": auth.request.digest,
            "decision": {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": ["Write-Output"]}}}
    events = [event async for event in execute_serial(call, perm=auth.permission_decision.permission_level,
        permission_decision=auth.permission_decision, permission_checker=host.checker, permission_context=host.context.permission,
        ctx=builder, state=AgentState(user_message="failed rule persistence"), tool_registry=host.registry,
        tool_ctx=host.context, approval_handler=approve, skill_manager=None, iteration_id="iter:1")]
    warnings = [payload["message"] for kind, payload in host.events if kind == "system_notice"]
    assert len(warnings) == 1 and "Failed to apply execpolicy amendment" in warnings[0]
    assert host.owner.revision == 0
    assert host.owner.inspect_for_model(host.snapshot)["allowed_prefixes"] == []
    result = next(event for event in events if event.type == "tool_result")
    assert not result.data["is_error"], result.data
    assert "SOURCE_NATIVE_COMMAND" in result.data["summary"]


@pytest.mark.asyncio
async def test_an_approval_cannot_replace_the_source_proposed_prefix_or_execute_the_changed_grant(tmp_path):
    host = setup(tmp_path)
    call, auth = authorize(host, command='Set-Content -LiteralPath denied-native-marker.txt -Value "not approved"', prefix=["Set-Content"])
    builder = ContextBuilder()
    builder.bind_tool_context(host.context)
    async def forged(call_id):
        return {"action": "approve", "request_digest": auth.request.digest,
            "decision": {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": ["powershell"]}}}
    events = [event async for event in execute_serial(call, perm=auth.permission_decision.permission_level,
        permission_decision=auth.permission_decision, permission_checker=host.checker, permission_context=host.context.permission,
        ctx=builder, state=AgentState(user_message="forged reusable grant"), tool_registry=host.registry,
        tool_ctx=host.context, approval_handler=forged, skill_manager=None, iteration_id="iter:1")]
    assert next(event for event in events if event.type == "tool_result").data["is_error"]
    assert not (host.workspace / "denied-native-marker.txt").exists()
    assert not host.owner.default_policy_path.exists()


@pytest.mark.asyncio
async def test_changed_discovery_after_approval_cannot_replace_frozen_shell_or_argv(tmp_path, monkeypatch):
    host = setup(tmp_path, approval_policy="on-request")
    call, auth = authorize(host)
    def changed(*args, **kwargs):
        raise AssertionError("Shell must not be discovered again after final request capture")
    monkeypatch.setattr("backend.tools.command_tool.command_shell_argv", changed)
    result = await run_tool_with_timeout(call, host.registry, host.context)
    assert not result.is_error, result.content
    assert "SOURCE_NATIVE_COMMAND" in result.content


def test_allow_prefix_does_not_remove_denied_read_restrictions(tmp_path):
    host = setup(tmp_path, rules='prefix_rule(pattern=["Write-Output"], decision="allow")')
    _, auth = authorize(host)
    native = auth.request.exec_policy
    restricted = replace(native, command=replace(native.command, denied_reads=True))
    assert restricted.first_attempt_override(already_approved=True) == "no_override"
    escalated = replace(restricted, command=replace(restricted.command, sandbox_permissions="require_escalated"))
    assert escalated.first_attempt_override(already_approved=False) == "no_override"
    assert escalated.first_attempt_override(already_approved=True) == "escalated_sandbox_with_restrictions"


def test_catalog_permission_text_preserves_literal_empty_and_does_not_append_bundled_prefixes(tmp_path):
    host = setup(tmp_path, rules='prefix_rule(pattern=["Write-Output"], decision="allow")')
    info = replace(host.snapshot.model_info, extra={"model_messages": {"approvals": {"unless_trusted": ""},
        "permissions": {"danger_full_access": "Catalog network: {{ network_access }}"}}})
    host.context.model_execution = replace(host.snapshot, model_info=info)
    text = permission_instructions(host.context)
    assert "Catalog network: enabled" in text
    assert "Approved command prefixes" not in text
    assert "untrusted" not in text


@pytest.mark.parametrize("scope", ["turn", "session"])
def test_real_sticky_grant_allows_preapproved_additional_under_never_without_new_approval(tmp_path, scope):
    host = setup(tmp_path, approval_policy="never")
    granted = normalize_permission_profile({"network": {"enabled": True}}, cwd=host.workspace)
    host.context.permission_grants.record(PermissionGrantResponse(granted, scope), host.context.environment_id, host.context.turn_id)
    _, auth = authorize(host, sandbox_permissions="with_additional_permissions", additional_permissions={"network": {"enabled": True}})
    native = auth.request.exec_policy
    assert native.kind == "skip"
    assert native.command.sandbox_permissions == "use_default"
    assert native.available_decisions() == ["approved", "abort"]


def test_unapproved_additional_and_explicit_escalation_cannot_bypass_never(tmp_path):
    host = setup(tmp_path, approval_policy="never")
    for args in ({"sandbox_permissions": "require_escalated"},
        {"sandbox_permissions": "with_additional_permissions", "additional_permissions": {"network": {"enabled": True}}}):
        call = ToolCallEvent(id="unapproved", name="run_command", arguments={"command": "Write-Output rejected", **args})
        auth = _authorize_final_tool_request(call, tool_registry=host.registry, permission_checker=host.checker,
            permission_context=host.context.permission, tool_ctx=host.context)
        assert "cannot ask for escalated permissions" in auth.error


def test_implicit_existing_grant_keeps_additional_only_available_decisions(tmp_path):
    host = setup(tmp_path)
    grant = normalize_permission_profile({"network": {"enabled": True}}, cwd=host.workspace)
    host.context.permission_grants.record(PermissionGrantResponse(grant, "turn"), host.context.environment_id, host.context.turn_id)
    _, auth = authorize(host, prefix=["Write-Output"])
    assert auth.request.exec_policy.proposed_amendment == ("Write-Output",)
    assert auth.request.exec_policy.available_decisions() == ["approved", "abort"]


@pytest.mark.asyncio
async def test_original_ws_waiter_accepts_only_frozen_available_native_decisions(tmp_path):
    from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
    from backend.ws.turn_wait_state import TurnWaitState
    host = setup(tmp_path)
    _, auth = authorize(host, prefix=["Write-Output"])
    runtime = SessionApprovalRuntimeMixin()
    runtime.turn_wait_state = TurnWaitState()
    runtime.approval_diff_cache = {}
    native = auth.request.exec_policy
    runtime.turn_wait_state.pending_approval_payloads["native"] = {"conversation_id": "source-owner", "request": {
        "tool_name": "run_command", "command": list(native.command.policy_argv),
        "request_digest": auth.request.digest, "proposed_execpolicy_amendment": native.amendment_array(),
        "available_decisions": native.available_decisions()}}
    future = asyncio.get_running_loop().create_future()
    runtime.turn_wait_state.register_waiter("native", future, kind="approval")
    assert not runtime._resolve_pending_approval("native", {"action": "approve", "remember_for_session": True})
    assert not runtime._resolve_pending_approval("native", {"action": "approve", "auto_approved": True})
    assert not runtime._resolve_pending_approval("native", {"action": "approve", "decision": "approved_for_session"})
    assert not runtime._resolve_pending_approval("native", {"action": "approve", "decision": {
        "approved_execpolicy_amendment": {"proposed_execpolicy_amendment": ["powershell"]}}})
    assert not future.done()
    decision = {"approved_execpolicy_amendment": {"proposed_execpolicy_amendment": native.amendment_array()}}
    assert runtime._resolve_pending_approval("native", {"action": "approve", "decision": decision, "request_digest": auth.request.digest})
    assert future.result()["decision"] == decision
    assert future.result()["request_digest"] == auth.request.digest
    assert not host.owner.default_policy_path.exists()


@pytest.mark.asyncio
async def test_native_abort_interrupts_original_turn_before_command_execution(tmp_path):
    host = setup(tmp_path)
    host.context.cancel_event = asyncio.Event()
    cancelled = []
    host.context.run_context.record_user_cancelled = cancelled.append
    call, auth = authorize(host, command='Set-Content -LiteralPath aborted-native-marker.txt -Value "not approved"')
    builder = ContextBuilder()
    builder.bind_tool_context(host.context)
    async def abort(call_id):
        return {"action": "reject", "decision": "abort", "request_digest": auth.request.digest}
    with pytest.raises(asyncio.CancelledError):
        async for _ in execute_serial(call, perm=auth.permission_decision.permission_level,
            permission_decision=auth.permission_decision, permission_checker=host.checker, permission_context=host.context.permission,
            ctx=builder, state=AgentState(user_message="abort native command"), tool_registry=host.registry,
            tool_ctx=host.context, approval_handler=abort, skill_manager=None, iteration_id="iter:1"):
            pass
    assert cancelled == ["user_cancelled"]
    assert host.context.cancel_event.is_set()
    assert not (host.workspace / "aborted-native-marker.txt").exists()


@pytest.mark.asyncio
async def test_production_loop_uses_captured_trusted_layers_actual_native_argv_and_permission_instructions(tmp_path):
    from backend.agent.loop import run_agent_loop
    class CommandModel(LLMAdapter):
        def __init__(self):
            self.requests = []
            self._model = "source-exec-model"
        async def simple_chat(self, messages, *, max_tokens=None):
            return "Native source audit"
        async def stream_chat(self, messages, tools=None, metadata=None):
            self.requests.append(messages)
            if len(self.requests) == 1:
                yield StreamEvent(type=StreamEventType.TOOL_CALL, tool_calls=[ToolCallEvent(
                    id="production-native", name="run_command", arguments={"command": 'Write-Output "PRODUCTION_SOURCE_NATIVE"', "login": False})])
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="tool_calls")
            else:
                yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Native command verified.", phase="final_answer", item_id="native-final")
                yield StreamEvent(type=StreamEventType.DONE, finish_reason="stop")
    host = setup(tmp_path, approval_policy="never")
    adapter = CommandModel()
    snapshot = replace(host.snapshot, llm=adapter)
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime/metrics.jsonl", swarm_store_dir=tmp_path / "runtime/swarm", enable_lease_heartbeat=False)
    run = RunContext(model_execution=snapshot, agent_runtime=runtime)
    builder = ContextBuilder(llm=adapter)
    permission = replace(host.context.permission, mode="bypass", sandbox_mode="danger-full-access")
    try:
        events = [event async for event in run_agent_loop("Verify the Source native command", adapter,
            host.registry, host.context.artifact_store, host.checker, context_builder=builder,
            permission_context=permission, run_context=run, session_id="native-production-session",
            metadata={"conversation_id": "native-production-owner", "workspace_root": str(host.workspace)})]
        result = next(event for event in events if event.type == "tool_result")
        assert not result.data["is_error"], result.data
        assert "PRODUCTION_SOURCE_NATIVE" in result.data["summary"]
        assert next(event for event in reversed(events) if event.type == "done").data["status"] == "completed"
        assert not any(event.type == "approval_request" for event in events)
        assert run.exec_policy_owner.home == host.home
        assert any("<permissions instructions>" in str(message.content) for message in adapter.requests[0])
    finally:
        runtime.close(release_lease=True)
