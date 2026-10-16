"""The only executor URI to native-path boundary, for the actual local host."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path

from backend.permissions.environment_scope import permission_profile_from_executor_wire
from backend.runtime_env import (ShellEnvironmentPolicy, shell_subprocess_env,
    _matches_any_env_pattern, _scrub_non_inheritable_env, _apply_minicode_actions_scrub)
from backend.sandbox.policy import (
    FileSystemAccessMode, FileSystemPath, FileSystemSandboxKind, FileSystemSpecialPath,
    PermissionProfile, SandboxEnforcement, SandboxPolicy,
)
from backend.workspace.executor_paths import ExecutorPath


def local_path(uri: str) -> Path:
    return ExecutorPath.parse(uri).to_local_path()


def _context_paths(sandbox: dict) -> tuple[str, tuple[str, ...]]:
    policy_context = sandbox.get("policyContext")
    cwd = (policy_context.get("cwd") if policy_context is not None else None) or sandbox.get("cwd")
    roots = tuple(policy_context.get("workspaceRoots", ())) if policy_context is not None else tuple(sandbox.get("workspaceRoots", ()))
    if cwd is None:
        profile = permission_profile_from_executor_wire(sandbox["permissions"])
        needs_cwd = any(entry.path.kind == "glob_pattern" and not Path(str(entry.path.value)).is_absolute()
            or entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.PROJECT_ROOTS
            for entry in profile.file_system.entries)
        if needs_cwd:
            raise ValueError("Filesystem sandbox policy requires an executor cwd")
        cwd = Path.cwd().as_uri()  # Source ingress resolves legacy context-free absolute policies here.
    return cwd, roots


def _native_profile(profile: PermissionProfile) -> PermissionProfile:
    entries = tuple(replace(entry, path=FileSystemPath("path", local_path(str(entry.path.value))))
        if entry.path.kind == "path" else entry for entry in profile.file_system.entries)
    return replace(profile, file_system=replace(profile.file_system, entries=entries))


def _execution_environment(env_policy: dict | None, env: dict | None) -> dict[str, str]:
    if env_policy is None:
        policy = ShellEnvironmentPolicy()
    else:
        policy = ShellEnvironmentPolicy(inherit=env_policy.get("inherit", "all"),
            ignore_default_excludes=env_policy.get("ignoreDefaultExcludes", False),
            exclude=tuple(env_policy.get("exclude", ())))
    captured = shell_subprocess_env(policy)
    if env_policy is not None:
        captured.update(env_policy.get("set", {}))
        include_only = tuple(env_policy.get("includeOnly", ()))
        if include_only:
            captured = {key: value for key, value in captured.items() if _matches_any_env_pattern(key, include_only)}
    if env is not None:
        captured.update(env)
    _scrub_non_inheritable_env(captured)
    _apply_minicode_actions_scrub(captured)
    return captured


def local_sandbox_policy(sandbox: dict | None, *, env: dict | None = None,
        env_policy: dict | None = None) -> SandboxPolicy:
    execution_env = _execution_environment(env_policy, env)
    if sandbox is None:
        return SandboxPolicy(permission_profile=PermissionProfile.disabled(),
            captured_env=execution_env, protect_workspace_metadata=False)
    cwd_uri, roots = _context_paths(sandbox)
    cwd = local_path(cwd_uri)
    profile = _native_profile(permission_profile_from_executor_wire(sandbox["permissions"]))
    home = local_path(sandbox["userHomeDir"]) if sandbox.get("userHomeDir") is not None else None
    temporary = (tuple(local_path(uri) for uri in sandbox["temporaryDirectories"])
        if sandbox.get("temporaryDirectories") is not None else None)
    if os.name == "nt" and env is not None:
        overrides = [Path(value) for key, value in env.items() if key.upper() in {"TEMP", "TMP"}]
        if overrides:
            temporary = tuple(path if path.is_absolute() else cwd / path for path in overrides)
    return SandboxPolicy(permission_profile=profile, workspace_root=None,
        workspace_roots=tuple(local_path(root) for root in roots), policy_cwd=cwd,
        captured_env=execution_env, protect_workspace_metadata=False,
        user_home_dir=home, temporary_directories=temporary,
        windows_sandbox_selection=sandbox["windowsSandboxLevel"],
        use_legacy_landlock=sandbox.get("useLegacyLandlock", False))


def requires_filesystem_sandbox(sandbox: dict | None, *, write: bool) -> bool:
    if sandbox is None:
        return False
    cwd, _roots = _context_paths(sandbox)
    profile = permission_profile_from_executor_wire(sandbox["permissions"])
    if profile.enforcement is not SandboxEnforcement.MANAGED or profile.file_system.kind is FileSystemSandboxKind.UNRESTRICTED:
        return False
    entries = profile.file_system.entries
    windows = ExecutorPath.parse(cwd).convention == "windows"
    root_access = any(entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.ROOT
        and (entry.access.can_write if write else entry.access.can_read) for entry in entries)
    if not root_access:
        return True
    if not write:
        return any(entry.access is FileSystemAccessMode.DENY and not (windows
            and entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.SLASH_TMP)
            for entry in entries)
    for entry in entries:
        if entry.access.can_write:
            continue
        selector = entry.path
        if selector.kind == "special":
            if selector.value is FileSystemSpecialPath.ROOT:
                if entry.access is FileSystemAccessMode.DENY:
                    return True
                continue
            if (selector.value is FileSystemSpecialPath.MINIMAL
                    or not isinstance(selector.value, FileSystemSpecialPath)
                    or windows and selector.value is FileSystemSpecialPath.SLASH_TMP):
                continue
        if selector.kind == "glob_pattern" or entry.access is FileSystemAccessMode.DENY:
            return True
        overridden = any(candidate.access.can_write and (
            candidate.path == selector or candidate.path.kind == selector.kind == "path"
            and ExecutorPath.parse(str(candidate.path.value)) == ExecutorPath.parse(str(selector.value)))
            for candidate in entries)
        if not overridden:
            return True
    return False
