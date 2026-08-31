"""Tests for Codex-style escalate-on-failure in RunCommandTool."""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.artifact.store import ArtifactStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import PermissionLevel
from backend.sandbox import SandboxResult, SandboxRunner
from backend.tools.command_support import _looks_like_sandbox_denial
from backend.tools.command_tool import RunCommandTool


@pytest.mark.parametrize(
    "stderr,exit_code,expected",
    [
        ("curl: (6) Could not resolve host: pypi.org", 6, False),
        ("Network is unreachable", 1, True),
        ("connection refused", 1, False),
        ("Read-only file system", 1, True),
        ("EACCES: permission denied", 1, False),
        ("operation not permitted", 1, False),
        ("failed to connect to db", 1, False),
        ("sandbox-exec: deny file-write", 1, True),
        ("SyntaxError: invalid syntax", 1, False),
        ("file not found", 1, False),
        ("Network is unreachable", 0, False),  # exit 0 = not a failure
        ("", 1, False),
    ],
)
def test_sandbox_denial_detection(stderr, exit_code, expected):
    assert _looks_like_sandbox_denial(stderr, exit_code) is expected


def test_check_permission_forces_confirm_on_escalation():
    tool = RunCommandTool(ArtifactStore())
    default_ctx = PermissionContext(mode="confirm")

    # Escalation request in a normal mode must require confirmation.
    assert (
        tool.check_permission(
            {"command": "pip install x", "with_escalated_permissions": True},
            default_ctx,
        )
        == PermissionLevel.CONFIRM
    )
    # No escalation -> defer to centralized policy.
    assert tool.check_permission({"command": "ls"}, default_ctx) is None
    # Bypass mode already runs unsandboxed -> escalation is moot, defer.
    assert (
        tool.check_permission(
            {"command": "pip install x", "with_escalated_permissions": True},
            PermissionContext(mode="bypass"),
        )
        is None
    )


def _sandboxed_ctx(workspace: Path) -> ToolExecutionContext:
    return ToolExecutionContext(
        permission=PermissionContext(mode="confirm"),
        workspace_root=workspace,
        allow_network=False,
    )


def test_sandbox_failure_surfaces_escalation_hint():
    tool = RunCommandTool(ArtifactStore())
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cmd = (
            "python -c \"import sys; sys.stderr.write('Network is unreachable'); "
            "sys.exit(1)\""
        )
        with patch.object(
            SandboxRunner,
            "run",
            return_value=SandboxResult(stdout="", stderr="Network is unreachable", exit_code=1),
        ):
            res = asyncio.run(tool._execute_foreground(cmd, str(d), 30, _sandboxed_ctx(d), escalated=False))
        assert res.is_error
        assert res.status == "failed"
        assert "[sandbox]" in res.content
        assert "with_escalated_permissions=true" in res.content


def test_escalated_retry_does_not_readvertise_hint():
    tool = RunCommandTool(ArtifactStore())
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cmd = (
            "python -c \"import sys; sys.stderr.write('Network is unreachable'); "
            "sys.exit(1)\""
        )
        res = asyncio.run(tool._execute_foreground(cmd, str(d), 30, _sandboxed_ctx(d), escalated=True))
        # Already escalated: never advertise escalation again.
        assert "[sandbox]" not in res.content


def test_ordinary_failure_has_no_escalation_hint():
    tool = RunCommandTool(ArtifactStore())
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        cmd = (
            "python -c \"import sys; sys.stderr.write('SyntaxError: bad'); "
            "sys.exit(1)\""
        )
        with patch.object(
            SandboxRunner,
            "run",
            return_value=SandboxResult(stdout="", stderr="SyntaxError: bad", exit_code=1),
        ):
            res = asyncio.run(tool._execute_foreground(cmd, str(d), 30, _sandboxed_ctx(d), escalated=False))
        assert res.is_error
        assert "[sandbox]" not in res.content


def test_unavailable_sandbox_requires_explicit_escalation(tmp_path: Path):
    tool = RunCommandTool(ArtifactStore())

    res = asyncio.run(
        tool._execute_foreground(
            "echo blocked",
            str(tmp_path),
            30,
            _sandboxed_ctx(tmp_path),
            escalated=False,
        )
    )

    if not res.is_error:
        pytest.skip("This host has an enforceable restricted sandbox")
    assert "sandbox is unavailable" in res.content.lower()
    assert "with_escalated_permissions=true" in res.content
    assert "with_escalated_permissions=true" in res.content


def test_schema_exposes_escalation_fields():
    tool = RunCommandTool(ArtifactStore())
    props = tool.get_schema().parameters["properties"]
    assert "with_escalated_permissions" in props
    assert "justification" in props


def test_escalated_command_can_resolve_an_external_working_directory(tmp_path: Path) -> None:
    tool = RunCommandTool(ArtifactStore())
    workspace = tmp_path / "workspace"
    external = tmp_path / "external"
    workspace.mkdir()
    external.mkdir()
    context = _sandboxed_ctx(workspace)

    with pytest.raises(ValueError, match="cwd must stay inside workspace"):
        tool._resolve_cwd(str(external), context)

    resolved = tool._resolve_cwd(
        str(external),
        context,
        allow_workspace_escape=True,
    )
    assert resolved == str(external.resolve())
