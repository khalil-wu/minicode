"""Bypass mode is the explicit unattended permission profile."""

import tempfile
from pathlib import Path

from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.tools.edit_file import EditFileTool
from backend.tools.write_file import WriteFileTool


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
