"""A declined write stays declined for the path, not for the tool that asked."""
from pathlib import Path
from types import SimpleNamespace

from backend.artifact.store import ArtifactStore
from backend.config import permission_settings_from_config
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext
from backend.permissions.denial_memory import (
    denied_shell_write_target,
    discard_denied_write_targets,
    normalized_denied_path,
    record_denied_write_targets,
    shell_write_targets,
)
from backend.tools.base import PermissionLevel
from backend.tools.command_tool import RunCommandTool


def _skip_policy(cwd: Path):
    return SimpleNamespace(kind="skip", reason=None, command=SimpleNamespace(cwd=cwd, configured_shell="bash"))


def test_shell_write_targets_cover_redirects_dd_and_copy_destinations():
    targets = shell_write_targets(
        "printf 'hello' > note.txt && cp a.txt b/c.txt; tee x.log y.log < in; dd if=/dev/zero of=out.bin; echo hi 2>/dev/null >> log.txt"
    )
    assert targets == ["note.txt", "b/c.txt", "x.log", "y.log", "out.bin", "log.txt"]
    assert shell_write_targets("cat note.txt | grep x") == []


def test_record_and_discard_normalize_against_the_workspace(tmp_path):
    permission = PermissionContext(mode="confirm")
    recorded = record_denied_write_targets(permission, "write_file", {"file_path": "docs/note.txt"}, workspace_root=tmp_path)
    assert recorded == [normalized_denied_path(tmp_path / "docs" / "note.txt", None)]
    assert permission.denied_write_paths == set(recorded)

    patch = "*** Begin Patch\n*** Update File: docs/note.txt\n@@\n-old\n+new\n*** End Patch\n"
    record_denied_write_targets(permission, "apply_patch", {"patch": patch}, workspace_root=tmp_path)
    assert permission.denied_write_paths == set(recorded)

    discard_denied_write_targets(permission, "run_command", {"command": "echo new > docs/note.txt"}, workspace_root=tmp_path)
    assert permission.denied_write_paths == set()


def test_shell_write_to_a_declined_path_is_asked_again_instead_of_running(tmp_path):
    checker = PermissionChecker(permission_settings_from_config({}), tmp_path)
    tool = RunCommandTool(ArtifactStore())
    permission = PermissionContext(mode="confirm", captured_exec_policy=_skip_policy(tmp_path))
    record_denied_write_targets(permission, "write_file", {"file_path": "note.txt"}, workspace_root=tmp_path)

    level, source, rule = checker._evaluate_level("run_command", {"command": "printf 'hello' > note.txt"}, context=permission, tool=tool)
    assert (level, source) == (PermissionLevel.CONFIRM, "denial_memory")
    assert "note.txt" in rule

    level, source, _ = checker._evaluate_level("run_command", {"command": "printf 'hello' > other.txt"}, context=permission, tool=tool)
    assert (level, source) == (PermissionLevel.AUTO, "exec_policy")

    assert denied_shell_write_target(permission, "cp a.txt note.txt", tmp_path) == "note.txt"
    assert denied_shell_write_target(permission, "cat note.txt", tmp_path) == ""


def test_an_explicit_approval_lifts_the_refusal(tmp_path):
    permission = PermissionContext(mode="confirm", captured_exec_policy=_skip_policy(tmp_path))
    record_denied_write_targets(permission, "edit_file", {"file_path": "note.txt"}, workspace_root=tmp_path)
    assert denied_shell_write_target(permission, "echo x > note.txt", tmp_path) == "note.txt"
    discard_denied_write_targets(permission, "edit_file", {"file_path": "note.txt"}, workspace_root=tmp_path)
    assert denied_shell_write_target(permission, "echo x > note.txt", tmp_path) == ""


def test_derived_contexts_share_the_refusal_within_the_turn(tmp_path):
    from dataclasses import replace

    permission = PermissionContext(mode="confirm")
    derived = replace(permission, captured_exec_policy=_skip_policy(tmp_path))
    record_denied_write_targets(permission, "write_file", {"file_path": "note.txt"}, workspace_root=tmp_path)
    assert derived.denied_write_paths is permission.denied_write_paths
    assert denied_shell_write_target(derived, "echo x > note.txt", tmp_path) == "note.txt"
