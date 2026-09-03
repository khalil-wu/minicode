from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.permissions.context import PermissionContext
from backend.sandbox.policy import (
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemSandboxEntry,
    FileSystemSandboxPolicy,
    FileSystemSpecialPath,
    PermissionProfile,
    SandboxPolicy,
    sandbox_policy_from_config_snapshot,
)
from backend.sandbox.runner import (
    SandboxRunner,
    SandboxUnavailableError,
    _bubblewrap_command,
    _container_policy_masks,
    _container_targets_for_path,
    _expand_unreadable_glob,
    _policy_path_is_writable,
    _seatbelt_profile,
)
from backend.tools.base import PermissionLevel
from backend.tools.command_tool import RunCommandTool


def _managed_policy(
    workspace: Path,
    *entries: FileSystemSandboxEntry,
    protect_workspace_metadata: bool = False,
) -> SandboxPolicy:
    return SandboxPolicy(
        permission_profile=PermissionProfile.managed(
            FileSystemSandboxPolicy.restricted(list(entries))
        ),
        workspace_root=workspace,
        protect_workspace_metadata=protect_workspace_metadata,
    )


def _path_entry(path: Path, access: FileSystemAccessMode) -> FileSystemSandboxEntry:
    return FileSystemSandboxEntry(FileSystemPath.path(path), access)


def test_nested_access_uses_most_specific_path_then_access_tiebreaker(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    public = workspace / "docs" / "public"
    private_public = workspace / "private" / "public"
    public.mkdir(parents=True)
    private_public.mkdir(parents=True)
    policy = _managed_policy(
        workspace,
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        ),
        _path_entry(workspace, FileSystemAccessMode.WRITE),
        _path_entry(workspace / "docs", FileSystemAccessMode.READ),
        _path_entry(public, FileSystemAccessMode.WRITE),
        _path_entry(workspace / "private", FileSystemAccessMode.DENY),
        _path_entry(private_public, FileSystemAccessMode.READ),
        _path_entry(private_public, FileSystemAccessMode.WRITE),
    )
    resolved = policy.resolve(cwd=workspace)

    assert resolved.resolve_access(workspace / "src") is FileSystemAccessMode.WRITE
    assert resolved.resolve_access(workspace / "docs" / "guide.md") is FileSystemAccessMode.READ
    assert resolved.resolve_access(public / "index.md") is FileSystemAccessMode.WRITE
    assert resolved.resolve_access(workspace / "private" / "token") is FileSystemAccessMode.DENY
    assert resolved.resolve_access(private_public / "index.md") is FileSystemAccessMode.WRITE


def test_lexical_parent_traversal_cannot_match_workspace_write_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    resolved = _managed_policy(
        workspace,
        _path_entry(workspace, FileSystemAccessMode.WRITE),
    ).resolve(cwd=workspace)

    assert resolved.resolve_access(workspace / ".." / "outside.txt") is FileSystemAccessMode.DENY
    assert resolved.resolve_access(workspace / "nested" / ".." / "file.txt") is FileSystemAccessMode.WRITE


def test_root_read_with_deny_is_a_baseline_not_full_disk_read(tmp_path: Path) -> None:
    denied = tmp_path / "secret"
    policy = _managed_policy(
        tmp_path,
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        ),
        _path_entry(denied, FileSystemAccessMode.DENY),
    )
    resolved = policy.resolve(cwd=tmp_path)

    assert resolved.root_read_baseline is True
    assert resolved.full_disk_read is False
    assert resolved.resolve_access(tmp_path / "public") is FileSystemAccessMode.READ
    assert resolved.resolve_access(denied) is FileSystemAccessMode.DENY


def test_escalation_preserves_deny_read_entries(tmp_path: Path) -> None:
    denied = tmp_path / "secret"
    policy = _managed_policy(
        tmp_path,
        _path_entry(tmp_path, FileSystemAccessMode.WRITE),
        _path_entry(denied, FileSystemAccessMode.DENY),
    )

    escalated = policy.escalated_preserving_denied_reads().resolve(cwd=tmp_path)

    assert escalated.root_write_baseline is True
    assert escalated.resolve_access(tmp_path / "other") is FileSystemAccessMode.WRITE
    assert escalated.resolve_access(denied) is FileSystemAccessMode.DENY


def test_skip_missing_entry_is_not_resolved_or_materialized(tmp_path: Path) -> None:
    missing = tmp_path / "other-platform"
    policy = _managed_policy(
        tmp_path,
        FileSystemSandboxEntry(
            FileSystemPath.path(missing),
            FileSystemAccessMode.READ,
            missing_path_behavior="skip",
        ),
    )

    resolved = policy.resolve(cwd=tmp_path)

    assert all(path != missing for path, _access in resolved.resolved_entries)
    assert not missing.exists()


def test_explicit_nested_metadata_write_reopens_only_nested_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    hooks = workspace / ".git" / "hooks"
    hooks.mkdir(parents=True)
    policy = _managed_policy(
        workspace,
        _path_entry(workspace, FileSystemAccessMode.WRITE),
        _path_entry(hooks, FileSystemAccessMode.WRITE),
        protect_workspace_metadata=True,
    )

    resolved = policy.resolve(cwd=workspace)
    workspace_root = next(root for root in resolved.writable_roots if root.root == workspace)

    assert ".git" in workspace_root.protected_metadata_names
    assert resolved.resolve_access(hooks / "pre-commit") is FileSystemAccessMode.WRITE


def test_read_only_profile_does_not_gain_tmp_write(tmp_path: Path) -> None:
    policy = _managed_policy(
        tmp_path,
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        ),
    )
    resolved = policy.resolve(cwd=tmp_path)
    command = _bubblewrap_command("true", resolved)

    assert not _policy_path_is_writable(resolved, Path("/tmp"))
    assert "'--tmpfs' '/tmp'" not in command


def test_container_masks_every_alias_for_denied_path(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    workspace = parent / "workspace"
    denied = workspace / "secret"
    denied.mkdir(parents=True)
    policy = _managed_policy(
        workspace,
        _path_entry(parent, FileSystemAccessMode.READ),
        _path_entry(workspace, FileSystemAccessMode.WRITE),
        _path_entry(denied, FileSystemAccessMode.DENY),
    )
    resolved = policy.resolve(cwd=workspace)

    targets = _container_targets_for_path(denied, resolved, workspace)
    masks = _container_policy_masks(resolved, workspace)

    assert "/workspace/secret" in targets
    assert "/readable/0/workspace/secret" in targets
    assert len(targets) == len(set(targets))
    assert all(any(f"={target}:" in arg for arg in masks) for target in targets)


def test_container_reopens_nested_write_after_read_only_parent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    docs = workspace / "docs"
    public = docs / "public"
    public.mkdir(parents=True)
    policy = _managed_policy(
        workspace,
        _path_entry(workspace, FileSystemAccessMode.WRITE),
        _path_entry(docs, FileSystemAccessMode.READ),
        _path_entry(public, FileSystemAccessMode.WRITE),
    )
    masks = _container_policy_masks(policy.resolve(cwd=workspace), workspace)

    docs_mount = next(index for index, arg in enumerate(masks) if ":/workspace/docs:ro" in arg)
    public_mount = next(
        index for index, arg in enumerate(masks) if ":/workspace/docs/public:rw" in arg
    )
    assert docs_mount < public_mount


def test_synthetic_mount_targets_are_removed_only_by_owned_lifecycle(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    policy = _managed_policy(
        workspace,
        _path_entry(workspace, FileSystemAccessMode.WRITE),
        protect_workspace_metadata=True,
    )
    runner = SandboxRunner(policy)

    ready_file = runner._prepare_synthetic_mount_targets(policy.resolve(cwd=workspace))
    # The protected metadata set is MiniCode's own
    # (DEFAULT_PROTECTED_METADATA_NAMES in backend/sandbox/policy.py); the
    # legacy ``.agents`` name is no longer part of it. Only ``.git`` and
    # ``.minicode`` are directory-shaped placeholders, the rest are files
    # (_protected_metadata_target_is_directory).
    created_dirs = [workspace / ".git", workspace / ".minicode"]
    created_files = [
        workspace / ".mcp.json",
        workspace / ".gitconfig",
        workspace / ".gitmodules",
        workspace / "settings.json",
        workspace / "settings.local.json",
    ]
    created = created_dirs + created_files
    assert ready_file is not None
    assert all(path.is_dir() for path in created_dirs)
    assert all(path.is_file() for path in created_files)

    runner._cleanup_sandbox_setup_state()

    assert all(not path.exists() for path in created)
    assert not ready_file.exists()


def test_seatbelt_directory_filters_cover_inode_and_descendants(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    denied = workspace / "secret"
    denied.mkdir(parents=True)
    policy = _managed_policy(
        workspace,
        _path_entry(workspace, FileSystemAccessMode.WRITE),
        _path_entry(denied, FileSystemAccessMode.DENY),
    )

    profile = _seatbelt_profile(policy.resolve(cwd=workspace))
    escaped = str(denied).replace("\\", "\\\\")

    assert f'(literal "{escaped}")' in profile
    assert f'(subpath "{escaped}")' in profile
    assert "deny file-read* file-read-metadata file-write*" in profile


def test_unreadable_glob_includes_hidden_ignored_and_symlink_target(tmp_path: Path) -> None:
    hidden = tmp_path / ".hidden.env"
    ignored = tmp_path / "ignored.env"
    target = tmp_path / "target.env"
    hidden.write_text("hidden", encoding="utf-8")
    ignored.write_text("ignored", encoding="utf-8")
    target.write_text("target", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored.env\n", encoding="utf-8")

    matches = _expand_unreadable_glob(str(tmp_path / "*.env"), None)

    assert hidden.absolute() in matches
    assert ignored.absolute() in matches
    try:
        link = tmp_path / "linked.env"
        link.symlink_to(target)
    except OSError:
        return
    matches = _expand_unreadable_glob(str(tmp_path / "*.env"), None)
    assert link.absolute() in matches
    assert target.resolve() in matches


def test_unreadable_glob_scan_failure_and_match_cap_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import backend.sandbox.runner as runner_module

    monkeypatch.setattr(runner_module.shutil, "which", lambda _name: "rg")
    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["rg"],
            returncode=2,
            stdout=b"",
            stderr=b"scan failed",
        ),
    )
    with pytest.raises(SandboxUnavailableError, match="scan failed"):
        _expand_unreadable_glob(str(tmp_path / "*.env"), None)

    output = b"\0".join(f"item-{index}.env".encode() for index in range(8193)) + b"\0"
    monkeypatch.setattr(
        runner_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=["rg"],
            returncode=0,
            stdout=output,
            stderr=b"",
        ),
    )
    with pytest.raises(SandboxUnavailableError, match="8192"):
        _expand_unreadable_glob(str(tmp_path / "*.env"), None)


def test_managed_read_deny_cannot_be_reopened_by_user_allow_read(tmp_path: Path) -> None:
    denied = tmp_path / "secret"
    policy = sandbox_policy_from_config_snapshot(
        tmp_path,
        sandbox_mode="workspace-write",
        sandbox_settings={
            "enabled": True,
            "filesystem": {"allowRead": [str(denied)]},
        },
        requirements_deny_read=[str(denied)],
    )

    assert policy.resolve(cwd=tmp_path).resolve_access(denied) is FileSystemAccessMode.DENY


def test_auto_allow_commands_never_auto_allows_excluded_or_escalated_commands(
    tmp_path: Path,
) -> None:
    policy = sandbox_policy_from_config_snapshot(
        tmp_path,
        sandbox_mode="workspace-write",
        sandbox_settings={
            "enabled": True,
            "excludedCommands": ["docker:*"],
        },
    )
    context = PermissionContext(
        sandbox_auto_allow_commands=policy.auto_allow_commands_if_sandboxed,
        sandbox_excluded_commands=policy.excluded_commands,
        allow_unsandboxed_commands=policy.allow_unsandboxed_commands,
    )

    assert policy.auto_allow_commands_if_sandboxed is True
    assert RunCommandTool.check_permission(
        RunCommandTool,
        {"command": "python -V"},
        context,
    ) is PermissionLevel.AUTO
    assert RunCommandTool.check_permission(
        RunCommandTool,
        {"command": "docker ps"},
        context,
    ) is PermissionLevel.CONFIRM
    assert RunCommandTool.check_permission(
        RunCommandTool,
        {"command": "python -V", "with_escalated_permissions": True},
        context,
    ) is PermissionLevel.CONFIRM


def test_managed_unsandboxed_false_blocks_escalation_before_execution(tmp_path: Path) -> None:
    policy = sandbox_policy_from_config_snapshot(
        tmp_path,
        sandbox_mode="workspace-write",
        sandbox_settings={"enabled": True},
        managed_sandbox_settings={"allowUnsandboxedCommands": False},
    )
    context = PermissionContext(
        allow_unsandboxed_commands=policy.allow_unsandboxed_commands,
        sandbox_auto_allow_commands=policy.auto_allow_commands_if_sandboxed,
    )

    assert policy.allow_unsandboxed_commands is False
    assert RunCommandTool.check_permission(
        RunCommandTool,
        {"command": "python -V", "with_escalated_permissions": True},
        context,
    ) is PermissionLevel.ALWAYS_DENY


def test_dialog_authored_content_rules_reach_the_sandbox_filesystem_policy(tmp_path: Path) -> None:
    """The approval dialog's "always allow/deny" persists content_*_rules.

    The permission checker merges ``content_allow_rules`` with ``allow`` (and the
    deny pair), but the sandbox read only ``allow``/``deny``, so every rule the
    dialog wrote was invisible to the filesystem policy.
    """

    def build(permissions: dict[str, object]):
        return sandbox_policy_from_config_snapshot(
            tmp_path,
            sandbox_mode="workspace-write",
            sandbox_settings={"enabled": True, "fileSystem": {}},
            permission_settings=permissions,
        )

    def entries(policy):
        file_system = policy.permission_profile.file_system
        return [
            (str(entry.path.value), entry.access.value)
            for entry in (file_system.entries or ())
        ]

    settings_spelling = entries(build({"deny": ["read_file(secrets/**)"]}))
    dialog_spelling = entries(build({"content_deny_rules": ["read_file(secrets/**)"]}))

    assert settings_spelling == dialog_spelling
    assert settings_spelling != entries(build({}))
    assert any(access == "deny" for _path, access in dialog_spelling)

    both = entries(
        build({"deny": ["read_file(a/**)"], "content_deny_rules": ["read_file(b/**)"]})
    )
    denied = {path for path, access in both if access == "deny"}
    assert any(path.replace("\\", "/").endswith("a/**") for path in denied)
    assert any(path.replace("\\", "/").endswith("b/**") for path in denied)
