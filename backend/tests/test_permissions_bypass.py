"""Bypass mode is the explicit unattended permission profile."""

import tempfile
from pathlib import Path

from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.base import PermissionLevel
from backend.tools.command_tool import RunCommandTool
from backend.tools.edit_file import EditFileTool
from backend.tools.git_support import _is_denied_path
from backend.tools.agent_user_tools import BriefTool
from backend.tools.read_file import ReadFileTool
from backend.tools.registry import CapabilityRegistry
from backend.tools.write_file import WriteFileTool
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin


def _checker(td: str) -> PermissionChecker:
    return PermissionChecker(settings=PermissionSettings(), workspace_root=Path(td))


def test_bypass_keeps_cc_safety_boundary_for_git_metadata_paths():
    td = tempfile.mkdtemp()
    checker = _checker(td)
    ctx = PermissionContext(mode="bypass")
    decision = checker.evaluate(
        "edit_file",
        {"file_path": ".git/HEAD", "old_string": "a", "new_string": "b"},
        context=ctx,
        tool=EditFileTool(),
    )
    assert decision.decision == "deny"
    assert not decision.capability_allowed


def test_bypass_keeps_configured_sensitive_path_boundary():
    td = tempfile.mkdtemp()
    checker = _checker(td)
    ctx = PermissionContext(mode="bypass")
    for path in (".env", "secrets/token.key"):
        decision = checker.evaluate(
            "write_file",
            {"file_path": path, "content": "value"},
            context=ctx,
            tool=WriteFileTool(),
        )
        assert decision.decision == "deny"
        assert not decision.capability_allowed


def test_bypass_allows_normal_workspace_write():
    td = tempfile.mkdtemp()
    checker = _checker(td)
    ctx = PermissionContext(mode="bypass")
    # A normal workspace file is fine in bypass (full workspace access).
    decision = checker.evaluate(
        "edit_file",
        {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
        context=ctx,
        tool=EditFileTool(),
    )
    assert decision.decision == "allow"
    assert decision.capability_allowed


def test_bypass_keeps_shell_substitution_at_confirmation_boundary(tmp_path: Path) -> None:
    checker = PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path)
    tool = RunCommandTool(ArtifactStore(storage_dir=str(tmp_path)))
    context = PermissionContext(mode="bypass")

    for command in ("$(rm -rf /)", "`rm -rf /`", "echo $(date)"):
        decision = checker.evaluate(
            "run_command",
            {"command": command},
            context=context,
            tool=tool,
        )
        assert decision.permission_level.value == "confirm"
        assert decision.matched_rule_source == "injection_risk"


def test_bypass_keeps_destructive_git_and_external_commands_at_confirmation_boundary(
    tmp_path: Path,
) -> None:
    checker = PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path)
    tool = RunCommandTool(ArtifactStore(storage_dir=str(tmp_path)))
    context = PermissionContext(mode="bypass")

    for command in (
        "git checkout .",
        "git restore -- .",
        "git stash clear",
        "git commit --amend",
        "kubectl delete pod api",
        "terraform destroy",
        "DROP TABLE users",
    ):
        decision = checker.evaluate(
            "run_command",
            {"command": command},
            context=context,
            tool=tool,
        )
        assert decision.permission_level is PermissionLevel.CONFIRM, command


def test_auto_approval_rechecks_capability_boundary_for_outside_path(tmp_path: Path) -> None:
    class _Runtime(SessionApprovalRuntimeMixin):
        pass

    checker = PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path)
    context = PermissionContext(mode="auto", workspace_root=tmp_path)
    registry = CapabilityRegistry()
    registry.register(ReadFileTool(ArtifactStore(storage_dir=str(tmp_path / "artifacts"))))
    runtime = _Runtime()
    runtime.permission_checker = checker
    runtime.permission_context = context
    runtime.tool_registry = registry

    outside_payload = {
        "type": "control_request",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "read_file",
            "input": {"file_path": str(tmp_path.parent / "outside.txt")},
        },
        "conversation_id": "conversation-1",
    }
    inside_payload = {
        "type": "control_request",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": "read_file",
            "input": {"file_path": str(tmp_path / "inside.txt")},
        },
        "conversation_id": "conversation-1",
    }

    assert runtime._pending_tool_payload_is_auto_allowed(outside_payload) is False
    assert runtime._pending_tool_payload_is_auto_allowed(inside_payload) is True


def test_reply_attachment_paths_use_the_active_filesystem_policy(tmp_path: Path) -> None:
    allowed_root = tmp_path / "allowed"
    denied_root = tmp_path / "denied"
    allowed_root.mkdir()
    denied_root.mkdir()
    allowed_file = allowed_root / "report.txt"
    denied_file = denied_root / "private.txt"
    allowed_file.write_text("ok", encoding="utf-8")
    denied_file.write_text("private", encoding="utf-8")

    checker = PermissionChecker(settings=PermissionSettings(), workspace_root=tmp_path)
    context = PermissionContext(
        mode="auto",
        workspace_root=tmp_path,
        filesystem_constraints={"allowlist": ["allowed"]},
    )
    decision = checker.evaluate(
        "reply",
        {"message": "See this", "attachments": [str(denied_file)]},
        context=context,
        tool=BriefTool(),
    )

    assert decision.decision == "deny"
    assert decision.capability_allowed is False
    allowed_decision = checker.evaluate(
        "reply",
        {"message": "See this", "attachments": [str(allowed_file)]},
        context=context,
        tool=BriefTool(),
    )
    assert allowed_decision.decision == "ask"
    assert allowed_decision.capability_allowed is True


def test_bypass_preserves_host_supplied_filesystem_denylist():
    """Bypass waives the user's own settings policy, not a managed constraint.

    ``permissions.filesystem.deny_read`` reaches the checker as a live context
    constraint and is folded into the sandbox layer's hard denies unconditionally.
    Dropping it in bypass made the two layers disagree about the same
    administrator policy.
    """
    td = tempfile.mkdtemp()
    checker = _checker(td)
    Path(td, "backend").mkdir()
    Path(td, "backend", "config.py").write_text("x", encoding="utf-8")
    constraints = {"denylist": ["backend/config.py"]}

    for mode in ("default", "bypass"):
        ctx = PermissionContext(mode=mode, filesystem_constraints=dict(constraints))
        assert not checker.is_path_allowed("backend/config.py", context=ctx), mode
        # The built-in secret/repo floor survives alongside the host constraint
        # instead of being replaced by it.
        assert not checker.is_path_allowed(".env", context=ctx), mode
        assert checker.is_path_allowed("ok.py", context=ctx), mode


def test_bypass_still_waives_the_users_own_settings_denylist():
    import dataclasses

    td = tempfile.mkdtemp()
    settings = PermissionSettings()
    checker = PermissionChecker(
        settings=dataclasses.replace(
            settings, path_denylist=[*settings.path_denylist, "notes.txt"]
        ),
        workspace_root=Path(td),
    )

    assert not checker.is_path_allowed(
        "notes.txt", context=PermissionContext(mode="confirm")
    )
    assert checker.is_path_allowed(
        "notes.txt", context=PermissionContext(mode="bypass")
    )
    assert not checker.is_path_allowed(
        ".env", context=PermissionContext(mode="bypass")
    )


def test_git_diff_permission_failure_fails_closed() -> None:
    class _BrokenChecker:
        def is_path_allowed(self, *_args, **_kwargs):
            raise RuntimeError("policy unavailable")

    context = type("Context", (), {
        "permission_checker": _BrokenChecker(),
        "permission": PermissionContext(mode="auto"),
    })()

    assert _is_denied_path(context, ".env") is True
