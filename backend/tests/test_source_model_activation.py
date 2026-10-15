from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import UserCommand
from backend.agent.model_activation import ModelActivationRejected, admit_model_activation, validate_model_activation
from backend.agent.run_context import RunContext
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.config_requirements import ConfigRequirements
from backend.feature_flags import FeatureFlags
from backend.llm.model_metadata import source_model_metadata_source
from backend.llm.model_runtime import ModelRuntime
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox.policy import FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemSandboxKind, FileSystemSpecialPath, FileSystemAccessMode, PermissionProfile, SandboxPolicy
from backend.tests.test_model_control_ownership import controls
from backend.tests.test_model_execution_ownership import setup, execute
from backend.llm.base import ToolCallEvent
from backend.ws.handlers.misc import handle_model_command, handle_turn_settings_update


def with_config(snapshot, *, requirements=None, settings=None, flags=None):
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file="fixture.json"), settings or {}),),
        requirements=ConfigRequirements(raw=requirements or {}))
    return replace(snapshot, config=replace(snapshot.config, config_layer_stack=stack,
        feature_flags=FeatureFlags({**snapshot.config.feature_flags.flags, **(flags or {})})))


def model_extra(snapshot, **fields):
    return replace(snapshot, model_info=replace(snapshot.model_info, extra={**snapshot.model_info.extra, **fields}))


@pytest.mark.parametrize("change,reason", [
    ("disabled", "step_model_switching"), ("active_fallback", "active model has only fallback"),
    ("destination_fallback", "destination model has only fallback"), ("policy", "admitted approval policy"),
    ("required", "Automatic approval review is required"), ("ignore", "admitted prefix-rule policy"),
    ("cyber", "admitted prefix-rule policy"), ("node", "node REPL review requirement"),
    ("guardian", "Guardian coverage"), ("node_disabled", "node REPL availability"),
    ("reviewer", "explicit Guardian reviewer model"), ("v2", "Guardian V2 classification"),
    ("node_policy", "parent-fallback node REPL policy"), ("guardian_policy", "parent-fallback policy"),
    ("template", "parent-fallback policy template"), ("managed_policy", "approval_policy"),
    ("managed_reviewer", "reviewer is no longer allowed"),
])
def test_source_live_admission_retains_actual_authority_and_model_owned_restrictions(tmp_path, monkeypatch, change, reason):
    fixture = setup(tmp_path, monkeypatch, None)
    admitted = fixture.owner.model_execution
    current = admitted
    destination = replace(admitted, model="model-b", model_info=admitted.model_runtime.get_model("custom", "model-b"))
    permission = PermissionContext()
    current_permission = permission
    live_config = admitted.config
    tool_context = SimpleNamespace(sandbox_policy=SandboxPolicy(permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted([]))))
    if change == "disabled": admitted = with_config(admitted, flags={"step_model_switching": False})
    elif change == "active_fallback": current = replace(current, model_info=replace(current.model_info, source_model_metadata=None))
    elif change == "destination_fallback": destination = replace(destination, model_info=replace(destination.model_info, source_model_metadata=None))
    elif change == "policy": current_permission = replace(permission, approval_policy="never")
    elif change == "required": live_config = with_config(admitted, requirements={"auto_review": {"required_on_models": ["model-b"]}}).config
    elif change == "ignore": live_config = with_config(admitted, requirements={"auto_review": {"ignore_rules": ["model-b"]}}).config
    elif change == "cyber": destination = model_extra(destination, model_specialty="cyber")
    elif change == "node": destination = model_extra(destination, node_repl_auto_review_required=True)
    elif change == "guardian": destination = model_extra(destination, guardian={"computer_use": "disabled", "shell": "synchronous"})
    elif change == "node_disabled": destination = model_extra(destination, node_repl_disabled=True)
    elif change == "reviewer": destination = model_extra(destination, auto_review_model_override="review-other")
    elif change == "v2":
        admitted = with_config(admitted, flags={"guardianv2": True})
        destination = model_extra(destination, model_messages={"guardian_v2": {"max_tool_call_lag": 7}})
    elif change == "node_policy": destination = model_extra(destination, model_messages={"auto_review": {"node_repl_policy": "Changed"}})
    elif change == "guardian_policy": destination = model_extra(destination, model_messages={"auto_review": {"policy": "Changed"}})
    elif change == "template": destination = model_extra(destination, model_messages={"auto_review": {"policy_template": "Changed"}})
    elif change == "managed_policy": live_config = with_config(admitted, requirements={"allowed_approval_policies": ["never"]}).config
    elif change == "managed_reviewer": live_config = with_config(admitted, requirements={"allowed_approvals_reviewers": ["auto_review"]}).config
    try:
        with pytest.raises(ValueError, match=reason):
            validate_model_activation(admitted=admitted, current=current, destination=destination, admitted_permission=permission,
                current_permission=current_permission, live_config=live_config, tool_context=tool_context)
    finally: fixture.runtime.close(release_lease=True)


def test_source_explicit_reviewer_override_and_default_empty_v2_transcript_compare_as_source(tmp_path, monkeypatch):
    fixture = setup(tmp_path, monkeypatch, None)
    base = with_config(fixture.owner.model_execution, flags={"guardianv2": True})
    admitted = model_extra(base, auto_review_model_override="pinned-reviewer")
    destination = model_extra(replace(admitted, model="model-b", model_info=admitted.model_runtime.get_model("custom", "model-b")),
        auto_review_model_override="pinned-reviewer", model_messages={"auto_review": {"node_repl_policy": "Different ignored fallback"}, "guardian_v2": {"transcript": {}}})
    try:
        validate_model_activation(admitted=admitted, current=admitted, destination=destination, admitted_permission=PermissionContext(),
            current_permission=PermissionContext(), live_config=base.config, tool_context=None)
    finally: fixture.runtime.close(release_lease=True)


@pytest.mark.parametrize("scope,full_disk", [("unrestricted", True), ("external", True), ("minimal", True), ("root_read", True),
    ("root_deny", False), ("shadowed_read", True), ("denied_path", False), ("read_only_path", False),
    ("workspace", False), ("glob", False), ("slash_tmp", os.name == "nt")])
def test_model_required_authority_uses_source_effective_full_disk_write_semantics(tmp_path, monkeypatch, scope, full_disk):
    fixture = setup(tmp_path, monkeypatch, None)
    admitted = with_config(fixture.owner.model_execution,
        requirements={"auto_review": {"required_on_models": ["model-a", "model-b"]}}, settings={"approvals_reviewer": "auto_review"})
    destination = replace(admitted, model="model-b", model_info=admitted.model_runtime.get_model("custom", "model-b"))
    root = FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.WRITE)
    literal = FileSystemPath.path(tmp_path / "same-target")
    entries = [root]
    if scope == "minimal": entries += [FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.MINIMAL), FileSystemAccessMode.READ)]
    if scope in {"root_read", "root_deny"}: entries += [FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ if scope == "root_read" else FileSystemAccessMode.DENY)]
    if scope == "glob": entries += [FileSystemSandboxEntry(FileSystemPath.glob(str(tmp_path / "secret-*")), FileSystemAccessMode.DENY)]
    if scope == "slash_tmp": entries += [FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.SLASH_TMP), FileSystemAccessMode.READ)]
    if scope == "shadowed_read": entries += [FileSystemSandboxEntry(literal, FileSystemAccessMode.READ), FileSystemSandboxEntry(literal, FileSystemAccessMode.WRITE)]
    if scope in {"denied_path", "read_only_path"}: entries += [FileSystemSandboxEntry(literal, FileSystemAccessMode.DENY if scope == "denied_path" else FileSystemAccessMode.READ)]
    if scope == "workspace": entries = [FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ)]
    filesystem = FileSystemSandboxPolicy.restricted(entries)
    if scope == "unrestricted": filesystem = FileSystemSandboxPolicy.unrestricted()
    if scope == "external": filesystem = FileSystemSandboxPolicy(kind=FileSystemSandboxKind.EXTERNAL)
    profile = PermissionProfile.external() if scope == "external" else PermissionProfile.managed(filesystem)
    tool_context = SimpleNamespace(sandbox_policy=SandboxPolicy(permission_profile=profile))
    try:
        if full_disk:
            with pytest.raises(ModelActivationRejected, match="Automatic approval review is required"):
                validate_model_activation(admitted=admitted, current=admitted, destination=destination,
                    admitted_permission=PermissionContext(), current_permission=PermissionContext(), live_config=admitted.config, tool_context=tool_context)
        else:
            validate_model_activation(admitted=admitted, current=admitted, destination=destination,
                admitted_permission=PermissionContext(), current_permission=PermissionContext(), live_config=admitted.config, tool_context=tool_context)
    finally: fixture.runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_explicit_turn_update_changes_next_step_without_saving_thread_defaults(tmp_path, monkeypatch):
    fixture = host = None
    requests = 0
    async def behavior(model, messages):
        nonlocal requests
        requests += 1
        if requests == 1:
            await handle_turn_settings_update(host, {"conversation_id": "conv_model_control", "turn_id": fixture.owner.model_activation.turn_id,
                "model": "model-b", "effort": "high"})
            assert host.emit_command_result.call_args.kwargs["data"]["status"] == "applied"
            return ToolCallEvent(id="issued-a", name="inspect_model", arguments={})
        if requests == 2: return ToolCallEvent(id="issued-b", name="inspect_model", arguments={})
    fixture = setup(tmp_path, monkeypatch, behavior)
    host, repository = controls(fixture, tmp_path, monkeypatch)
    try:
        await execute(fixture, tmp_path, conversation_id="conv_model_control")
        assert fixture.inspector.observations == [("model-a", "model-a", "low", 96000), ("model-b", "model-b", "high", 32768)]
        assert repository.get_conversation_summary("conv_model_control").model_selection["model"] == "model-a"
    finally: await host.run_manager.shutdown_notification_wakes()


@pytest.mark.asyncio
async def test_busy_ui_default_does_not_change_steered_same_turn(tmp_path, monkeypatch):
    fixture = host = None
    calls = 0
    async def behavior(model, messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            await handle_model_command(host, {"conversation_id": "conv_model_control", "model": "model-b"})
            fixture.owner.turn_input_queue.enqueue_command(UserCommand("user_message", {"content": "STEER_ORIGINAL_MODEL",
                "user_message_id": "steer-u", "assistant_message_id": "steer-a"}))
            return ToolCallEvent(id="old-turn-tool", name="inspect_model", arguments={})
        assert model.model_id() == "model-a"
        assert any("STEER_ORIGINAL_MODEL" in str(message.content) for message in messages)
        if calls == 2:
            return ToolCallEvent(id="steered-original-model-tool", name="inspect_model", arguments={})
    fixture = setup(tmp_path, monkeypatch, behavior)
    fixture.owner.model_execution = with_config(fixture.owner.model_execution, flags={"step_model_switching": False})
    host, repository = controls(fixture, tmp_path, monkeypatch)
    original = fixture.owner.model_execution
    try:
        await execute(fixture, tmp_path, conversation_id="conv_model_control")
        assert calls >= 2 and fixture.owner.model_execution is original
        assert fixture.inspector.observations and all(observation == ("model-a", "model-a", "low", 96000)
            for observation in fixture.inspector.observations)
        selected = repository.get_conversation_summary("conv_model_control").model_selection
        assert selected["model"] == "model-b"
    finally: await host.run_manager.shutdown_notification_wakes()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrance", ["ws", "extension"])
@pytest.mark.parametrize("change", ["replacement", "cancel", "requirements"])
async def test_real_async_resolution_rechecks_original_execution_and_latest_requirements(tmp_path, monkeypatch, entrance, change):
    fixture = setup(tmp_path, monkeypatch, None)
    host, repository = controls(fixture, tmp_path, monkeypatch)
    owner = fixture.owner
    ready, finish, auth_entered, auth_release = (asyncio.Event() for _ in range(4))
    cancel = asyncio.Event()
    async def live_task():
        owner.model_owner_task = asyncio.current_task()
        owner.context_builder = fixture.builder
        tool_context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path, run_context=owner,
            sandbox_policy=SandboxPolicy(permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted([]))))
        fixture.builder.bind_tool_context(tool_context)
        admit_model_activation(owner, task=asyncio.current_task(), cancel_event=cancel, permission=tool_context.permission, turn_id="target-turn")
        ready.set()
        await finish.wait()
    task = asyncio.create_task(live_task())
    host.run_manager.cleanup(conversation_id="conv_model_control", task=asyncio.current_task(), task_id="model-run",
        cancel_event=host.run_manager.cancel_events["conv_model_control"])
    host.run_manager.register(conversation_id="conv_model_control", task=task, task_id="actual-old",
        cancel_event=cancel, active_conversation_id="conv_model_control", run_context=owner)
    await asyncio.wait_for(ready.wait(), 5)
    async def refresh(_):
        auth_entered.set()
        await auth_release.wait()
    monkeypatch.setattr(host.catalog, "refresh_provider_auth", refresh)
    original = owner.model_execution
    live_config = [fixture.config]
    monkeypatch.setattr("backend.config.load_config", lambda **_: live_config[0])
    operation = asyncio.create_task(handle_turn_settings_update(host, {"conversation_id": "conv_model_control", "turn_id": "target-turn", "model": "model-b"})
        if entrance == "ws" else fixture.runner.actions["set_model"]({"id": "model-b"}))
    await asyncio.wait_for(auth_entered.wait(), 5)
    replacement = None
    if change == "replacement":
        # The originally targeted execution really completes during auth;
        # register an actually running replacement through the real manager.
        finish.set()
        await task
        replacement_owner = RunContext(model_execution=original)
        replacement = asyncio.create_task(asyncio.Event().wait())
        host.run_manager.register(conversation_id="conv_model_control", task=replacement, task_id="actual-new",
            cancel_event=asyncio.Event(), active_conversation_id="conv_model_control", run_context=replacement_owner)
    elif change == "cancel": cancel.set()
    else:
        live_config[0] = with_config(original, requirements={"auto_review": {"ignore_rules": ["model-b"]}}).config
    auth_release.set()
    try:
        if entrance == "ws":
            await operation
            if change == "requirements":
                result = host.send_event.call_args.args[0].data
                assert result["level"] == "error" and "prefix-rule" in result["message"]
            else:
                assert host.emit_command_result.call_args.kwargs["data"]["status"] == "targetUnavailable"
        else:
            with pytest.raises(ValueError): await operation
        assert owner.model_execution is original
        if replacement is not None:
            assert not replacement.done() and host.run_manager.context_for("conv_model_control") is replacement_owner
            assert replacement_owner.model_execution is original
        assert repository.get_conversation_summary("conv_model_control").model_selection["model"] == "model-a"
    finally:
        finish.set()
        await task
        if replacement is not None:
            replacement.cancel()
            await asyncio.gather(replacement, return_exceptions=True)
        await host.run_manager.shutdown_notification_wakes()
        fixture.runtime.close(release_lease=True)


def test_actual_catalog_produces_host_only_metadata_and_unknown_wire_cannot_forge_it(tmp_path):
    runtime = ModelRuntime(settings_snapshot={}, provider_configs={})
    declared = {"api": "openai-completions", "base_url": "https://example.invalid/v1",
        "models": [{"id": "gpt-6-sol", "context_window": 96000, "max_tokens": 4096},
            {"id": "unknown-explicit", "context_window": 96000, "max_tokens": 4096,
                "used_fallback_model_metadata": False, "source_model_metadata": {"reference_commit": "forged"}}]}
    models = runtime._apply_model_config("private", (), declared)
    known, unknown = models
    assert known.source_model_metadata == source_model_metadata_source("gpt-6-sol")
    assert known.source_model_metadata.reference_commit == "822e58cc3d666166c7446c5b1ea2e52f5d09594c"
    assert known.extra["node_repl_auto_review_required"] is True
    assert unknown.source_model_metadata is None
    assert "source_model_metadata" not in known.to_dict()
    assert "source_model_metadata" not in unknown.to_dict() and "used_fallback_model_metadata" not in unknown.to_dict()
    runtime.register_provider("private", declared)
    assert runtime.get_model("private", "gpt-6-sol").source_model_metadata == known.source_model_metadata
