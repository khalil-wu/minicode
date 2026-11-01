"""Frozen-request review and permission boundaries.

Each test drives the production entry the read-only audit reproduced: the
frozen executable command Guardian reviews, the checker's captured-policy
branch, PowerShell-hosted content rules and the executor-scoped path denylist.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.environment_selection import EnvironmentConfig, EnvironmentConfigState, TurnEnvironment, TurnEnvironmentSelection
from backend.agent.final_tool_request import FinalExecutableToolRequest
from backend.agent.guardian.action import GuardianAction
from backend.agent.run_context import RunContext
from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings, permission_settings_from_config
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.environments.config import EnvironmentSpec
from backend.environments.manager import Environment
from backend.feature_flags import FeatureFlags
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.content_rules import parse_content_rule, rule_matches_call
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.permissions.environment_scope import EnvironmentPermissionScope
from backend.permissions.exec_policy import ExecPolicyDecision, FrozenExecCommand
from backend.sandbox.policy import (
    FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    FileSystemSpecialPath, PermissionProfile, SandboxPolicy,
)
from backend.services.permission_rules_service import prepare_permission_rule_add
from backend.tools.base import PermissionLevel
from backend.tools.command_tool import RunCommandTool
from backend.tools.read_file import ReadFileTool


class _NeedsApproval:
    """Replace only the native rule classification after the real capture."""

    def command_requirement(self, capture, **kwargs):
        return ExecPolicyDecision(capture, "needs_approval", None, False, None, (), "{}")


def _captured(request, workspace, permission, kind, *, shell="sh", sandbox_permissions="use_default"):
    policy = SandboxPolicy.workspace_default(workspace)
    argv = [shell, "-c", str(request.arguments["command"])]
    command = FrozenExecCommand.capture(request, argv=argv, policy_argv=argv, cwd=workspace, configured_shell=Path(shell),
        permission=permission, profile=policy.permission_profile, resolved_profile=policy.resolve(cwd=workspace),
        windows_sandbox_level="disabled", sandbox_permissions=sandbox_permissions, prefix_rule=None,
        environment_id="local", policy=policy, additional_permissions_present=False)
    return ExecPolicyDecision(command, kind, None, False, None, (tuple(argv),), "{}")


@pytest.mark.asyncio
async def test_guardian_reviews_the_frozen_executor_command(tmp_path):
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(()))
    config = EnvironmentConfig(profile, ("file:///repo",), allow_login_shell=False)
    selection = TurnEnvironmentSelection("posix", "file:///repo", ("file:///repo",), EnvironmentConfigState.ready(config))
    environment = Environment(EnvironmentSpec("posix", url="ws://127.0.0.1:9"), client_name="no-connect")
    lease = environment.borrow()
    try:
        attachment = TurnEnvironment(selection, "Thread", lease,
            {"platformOs": "linux", "shell": {"name": "bash", "path": "/bin/bash"}, "temporaryDirectories": []})
        run = RunContext(exec_policy_owner=_NeedsApproval())
        model = SimpleNamespace(config=SimpleNamespace(feature_flags=FeatureFlags({"exec_permission_approvals": True})))
        permission = PermissionContext(workspace_root=tmp_path, environment_scope=attachment.permission_scope)
        context = ToolExecutionContext(permission=permission, workspace_root=tmp_path, run_context=run, model_execution=model,
            turn_environment=attachment, environment_id=selection.environment_id, turn_id="turn")
        tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
        reviewed = {}
        for mode in ("use_default", "require_escalated", "with_additional_permissions"):
            args = {"command": "printf reviewed", "cwd": "file:///repo", "sandbox_permissions": mode}
            if mode == "with_additional_permissions":
                args["additional_permissions"] = {"network": {"enabled": True}}
            request = FinalExecutableToolRequest.freeze(ToolCallEvent(id="call", name="run_command", arguments=args))
            decision = tool.capture_exec_policy(request, context, permission)
            payload = GuardianAction.from_tool_request(request.with_exec_policy(decision), context).payload
            assert payload["command"] == list(decision.command.policy_argv)
            assert payload["command"][0] == "/bin/bash"
            assert payload["cwd"] == "/repo"
            assert payload["sandbox_permissions"] == decision.command.sandbox_permissions == mode
            reviewed[mode] = payload
        assert "additional_permissions" not in reviewed["use_default"]
        assert "additional_permissions" not in reviewed["require_escalated"]
        assert reviewed["with_additional_permissions"]["additional_permissions"] == {"network": {"enabled": True}, "file_system": None}
        assert environment.client.connect_task is None
    finally:
        await lease.aclose()
        await environment.close()


def test_guardian_action_keeps_canonical_escalation_for_local_commands(tmp_path):
    args = {"command": "echo escalate", "sandbox_permissions": "require_escalated"}
    request = FinalExecutableToolRequest.freeze(ToolCallEvent(id="call", name="run_command", arguments=args))
    permission = PermissionContext(workspace_root=tmp_path)
    context = ToolExecutionContext(permission=permission, workspace_root=tmp_path, turn_id="turn")
    decision = _captured(request, tmp_path, permission, "needs_approval", sandbox_permissions="require_escalated")
    payload = GuardianAction.from_tool_request(request.with_exec_policy(decision), context).payload
    assert payload["sandbox_permissions"] == "require_escalated"
    assert payload["cwd"] == str(tmp_path)
    assert payload["command"] == list(decision.command.policy_argv)
    with pytest.raises(ValueError):
        GuardianAction.from_tool_request(request, context)


def test_captured_exec_policy_keeps_session_overrides_authoritative(tmp_path):
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    args = {"command": "echo audit"}
    request = FinalExecutableToolRequest.freeze(ToolCallEvent(id="call", name="run_command", arguments=args))
    for level, expected in (("deny", PermissionLevel.ALWAYS_DENY), ("confirm", PermissionLevel.CONFIRM)):
        target = SimpleNamespace(permission_deny_rules=[], permission_overrides={})
        mutation = prepare_permission_rule_add(target, {"rule_kind": "override", "pattern": "run_command", "level": level},
            conversation_id="thread")
        permission = replace(checker.build_context(mode="confirm", session_overrides=mutation.overrides), workspace_root=tmp_path)
        for kind in ("skip", "needs_approval"):
            context = replace(permission, captured_exec_policy=_captured(request, tmp_path, permission, kind))
            observed = checker.evaluate("run_command", args, context=context, tool=tool)
            assert (observed.permission_level, observed.matched_rule_source) == (expected, "session_override")
    permission = replace(checker.build_context(mode="confirm"), workspace_root=tmp_path)
    skip = replace(permission, captured_exec_policy=_captured(request, tmp_path, permission, "skip"))
    assert checker.evaluate("run_command", args, context=skip, tool=tool).permission_level == PermissionLevel.AUTO


def test_content_deny_rules_follow_the_powershell_host(tmp_path):
    rule = parse_content_rule("run_command(echo:*)")
    for command in ("& 'echo' audit", '& "echo" audit', "echo audit"):
        assert rule_matches_call(rule, "run_command", {"command": command}, effect="deny", shell="pwsh.exe")
    assert not rule_matches_call(rule, "run_command", {"command": "& 'echo' audit"}, effect="deny", shell="/bin/bash")
    checker = PermissionChecker(PermissionSettings(content_deny_rules=["run_command(echo:*)"]), workspace_root=tmp_path)
    tool = RunCommandTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    permission = replace(checker.build_context(mode="confirm"), workspace_root=tmp_path)
    args = {"command": "& 'echo' audit"}
    request = FinalExecutableToolRequest.freeze(ToolCallEvent(id="call", name="run_command", arguments=args))
    context = replace(permission, captured_exec_policy=_captured(request, tmp_path, permission, "skip", shell="pwsh.exe"))
    observed = checker.evaluate("run_command", args, context=context, tool=tool)
    assert (observed.permission_level, observed.matched_rule_source) == (PermissionLevel.ALWAYS_DENY, "content_rule")


def _scope(cwd_uri, platform_os):
    profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ)]))
    return EnvironmentPermissionScope("remote", profile, cwd_uri, (cwd_uri,), platform_os)


def test_executor_scope_keeps_the_path_denylist(tmp_path):
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")),
        {"permissions": {"path_denylist": ["blocked.txt"]}}),))
    checker = PermissionChecker(permission_settings_from_config(stack.effective_config()), workspace_root=tmp_path)
    posix = replace(checker.build_context(mode="confirm"), workspace_root=tmp_path, environment_scope=_scope("file:///repo", "linux"))
    allowed = checker.prepare_path_check(context=posix)
    assert allowed("src/app.py") and allowed("file:///repo/.env.example") and allowed("file:///repo")
    assert not allowed(".env") and not allowed("file:///repo/nested/.env.staging") and not allowed("blocked.txt")
    assert not allowed("file:///repo/.git/config") and not allowed("file:///repo/keys/server.pem")
    assert allowed(".ENV")
    windows = replace(checker.build_context(mode="confirm"), workspace_root=tmp_path, environment_scope=_scope("file:///C:/repo", "windows"))
    assert not checker.prepare_path_check(context=windows)("C:\\repo\\sub\\.ENV")
    tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
    denied = checker.evaluate("read_file", {"file_path": ".env"}, context=posix, tool=tool)
    assert not denied.capability_allowed and "禁止的路径模式" in denied.capability_reason
    assert checker.evaluate("read_file", {"file_path": "README.md"}, context=posix, tool=tool).capability_allowed
