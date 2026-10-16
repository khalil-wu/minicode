"""Guardian uses its issuing attachment and a real read-only execution ceiling."""
from __future__ import annotations

import base64
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from backend.agent.environment_selection import bind_tool_execution_environment, close_tool_execution_environment
from backend.agent.guardian.config import parent_has_full_access, parent_environment_identity
from backend.agent.guardian.prompt import GuardianPermissionEvidence
from backend.agent.guardian.runtime import GuardianCommandTool, GuardianPermissionChecker, GuardianViewImageTool
from backend.permissions.environment_command import EnvironmentCommandPolicy
from backend.sandbox.policy import (FileSystemAccessMode as Access, FileSystemPath, FileSystemSandboxEntry as Entry,
    FileSystemSandboxPolicy, FileSystemSpecialPath as Special, NetworkSandboxPolicy, PermissionProfile, SandboxEnforcement)
from backend.tests.test_executor_tool_admission import executor_host, batch, print_command


@pytest.mark.asyncio
async def test_guardian_freezes_readonly_target_before_native_policy_and_actual_execution(executor_host):
    host = executor_host
    host.registry.unregister("run_command")
    host.registry.register(GuardianCommandTool(host.context.artifact_store, None))
    host.checker = GuardianPermissionChecker(host.checker._settings, host.workspace)
    host.context = replace(host.context, permission_checker=host.checker,
        permission=replace(host.context.permission, mode="confirm", sandbox_mode="read-only",
            sandbox_auto_allow_commands=True, allow_unsandboxed_commands=False))
    _, result, _ = await batch(host, "run_command", {"command": print_command(host, "GUARDIAN_TARGET_READONLY"), "login": False})
    assert not result["is_error"], result
    command = next(iter(host.background._commands.values()))
    policy = command.sandbox_policy
    assert isinstance(policy, EnvironmentCommandPolicy)
    assert policy.scope.environment_id == host.environments.primary().selection.environment_id
    assert policy.permission_profile.enforcement is SandboxEnforcement.MANAGED
    assert policy.permission_profile.network is NetworkSandboxPolicy.RESTRICTED
    assert policy.scope.resolve_access("guardian-unwritten.txt") is Access.READ
    assert policy.scope.process_sandbox_context() is not None
    assert "GUARDIAN_TARGET_READONLY" in result["summary"]


@pytest.mark.asyncio
async def test_guardian_image_reads_real_executor_bytes_under_readonly_profile(executor_host):
    host = executor_host
    contents = BytesIO()
    Image.new("RGB", (2, 3), "blue").save(contents, format="PNG")
    (host.workspace / "guardian-image.png").write_bytes(contents.getvalue())
    context = bind_tool_execution_environment(host.context, host.environments.primary())
    try:
        result = await GuardianViewImageTool(None).execute({"path": "guardian-image.png"}, context)
        assert not result.is_error
        assert base64.b64decode(result.images[0]["data"]) == contents.getvalue()
        assert result.images[0]["media_type"] == "image/png"
    finally:
        await close_tool_execution_environment(context)


@pytest.mark.asyncio
async def test_guardian_denied_read_evidence_and_image_do_not_use_controller_paths(executor_host, monkeypatch):
    host = executor_host
    context = bind_tool_execution_environment(host.context, host.environments.primary())
    try:
        scope = context.permission.environment_scope
        denied = scope.resolve_path("private/secret.png")
        profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
            Entry(FileSystemPath.special(Special.ROOT), Access.WRITE),
            Entry(FileSystemPath("path", denied.uri), Access.DENY)]))
        context = replace(context, permission=replace(context.permission,
            environment_scope=replace(scope, permission_profile=profile)))
        evidence = GuardianPermissionEvidence.from_context(context, environment_id=context.environment_id)
        assert evidence.denied_paths == (denied.native,)
        async def forbidden_read(*_args, **_kwargs):
            pytest.fail("Guardian attempted to open a policy-denied image")
        monkeypatch.setattr(context.turn_environment.environment.filesystem, "open", forbidden_read)
        result = await GuardianViewImageTool(None).execute({"path": denied.uri}, context)
        assert result.is_error and "denies" in result.content
        assert not result.images
    finally:
        await close_tool_execution_environment(context)


@pytest.mark.asyncio
async def test_guardian_full_access_and_reuse_identity_come_from_real_snapshot(executor_host):
    host = executor_host
    assert host.context.sandbox_policy is None
    assert parent_has_full_access(host.context)
    original = parent_environment_identity(host.context)
    assert original[0][0] == host.environments.primary().selection.environment_id
    assert original[0][1] == host.workspace.as_uri()
    from backend.agent.environment_selection import TurnEnvironmentSnapshot
    host.context.run_context.turn_environments = TurnEnvironmentSnapshot()
    assert not parent_has_full_access(host.context)
    assert parent_environment_identity(host.context) == ()
