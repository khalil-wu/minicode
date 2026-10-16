"""Captured shell launch policy in the selected executor's path domain."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import ntpath
import shlex
from typing import Any

from backend.permissions.environment_scope import EnvironmentPermissionScope
from backend.runtime_env import ShellEnvironmentPolicy
from backend.sandbox.policy import (
    FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    FileSystemSpecialPath, NetworkSandboxPolicy, PermissionProfile, SandboxEnforcement,
)
from backend.workspace.executor_paths import ExecutorPath


@dataclass(frozen=True, slots=True)
class EnvironmentCommandPolicy:
    scope: EnvironmentPermissionScope
    attachment: object
    shell_environment_policy: ShellEnvironmentPolicy
    env_overrides: dict[str, str]
    timeout: float | None
    allow_unsandboxed_commands: bool
    network_policy: Any | None = None
    network_approval_factory: Any | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "shell_environment_policy", deepcopy(self.shell_environment_policy))
        object.__setattr__(self, "env_overrides", dict(self.env_overrides))

    @property
    def permission_profile(self):
        return self.scope.permission_profile

    @property
    def disable_os_sandbox(self):
        return self.permission_profile.enforcement is not SandboxEnforcement.MANAGED

    @property
    def allow_network(self):
        return self.permission_profile.network is NetworkSandboxPolicy.ENABLED

    def with_additional_permissions(self, additional):
        scope = self.scope.with_additional_permissions(additional)
        return replace(self, scope=scope,
            network_policy=self.network_policy.for_scope(scope) if self.network_policy is not None else None)

    def escalated_preserving_denied_reads(self):
        profile = self.permission_profile
        if not self.scope.has_denied_read_restrictions():
            profile = PermissionProfile.disabled()
        else:
            entries = (*profile.file_system.entries,
                FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.WRITE))
            profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries,
                glob_scan_max_depth=profile.file_system.glob_scan_max_depth), network=NetworkSandboxPolicy.ENABLED)
        scope = replace(self.scope, permission_profile=profile)
        return replace(self, scope=scope,
            network_policy=self.network_policy.for_scope(scope) if self.network_policy is not None else None)

    def env_policy_wire(self):
        policy = self.shell_environment_policy
        return {"inherit": policy.inherit, "ignoreDefaultExcludes": policy.ignore_default_excludes,
            "exclude": list(policy.exclude), "set": dict(policy.set_values), "includeOnly": list(policy.include_only)}


def executor_shell_argv(environment, command, *, shell=None, login=None, fallback_shell=None):
    allow_login = environment.config.allow_login_shell
    if login is True and not allow_login:
        raise ValueError("login shell is disabled by config; omit `login` or set it to false.")
    use_login = allow_login if login is None else login
    info = environment.shell
    if info is None and shell is not None:
        raise ValueError(f"environment `{environment.selection.environment_id}` does not report a shell")
    info = fallback_shell if info is None else info
    name, program = info["name"], info["path"]
    kinds = {"pwsh": "powershell", "powershell": "powershell", "bash": "bash", "zsh": "zsh", "sh": "sh", "cmd": "cmd"}
    if name not in set(kinds.values()):
        raise ValueError(f"unknown environment shell `{name}`")
    if shell is not None:
        requested = ntpath.splitext(ntpath.basename(shell))[0].lower()
        if kinds.get(requested) != name:
            raise ValueError(f"environment `{environment.selection.environment_id}` only supports `{name}`")
    if name == "powershell":
        argv = [program, *([] if use_login else ["-NoProfile"]), "-Command", command]
    elif name == "cmd":
        argv = [program, "/c", command]
    else:
        argv = [program, "-lc" if use_login else "-c", command]
        paths = environment.peer_info.get("prependPathDirs", ())
        parsed = [ExecutorPath.parse(path) for path in paths]
        if use_login and parsed and all(path.convention == "posix" and ":" not in path.native for path in parsed):
            setup = []
            for path in reversed(parsed):
                quoted = shlex.quote(path.native)
                setup.append(f'case ":${{PATH-}}:" in *:{quoted}:*) export PATH ;; '
                    f'*) if (export PATH=) 2>/dev/null; then export PATH={quoted}${{PATH:+":$PATH"}}; fi ;; esac')
            prefix = "; ".join(setup)
            if "\n" in prefix:
                escaped = shlex.quote(prefix.replace("\\", "\\\\").replace("\n", "\\n"))
                prefix = f'eval "$(command -p printf \'%b\' {escaped})"'
            argv[-1] = prefix + "; " + command
    return argv
