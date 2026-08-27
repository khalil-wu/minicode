"""Canonical subprocess permission profiles and sandbox policy resolution.

The data model follows Codex's current split between a serializable permission
profile, a cwd/workspace-resolved policy, and per-launch process settings.  The
``SandboxPolicy`` wrapper remains as a compatibility adapter for older callers;
the runner resolves it before selecting an OS backend.
"""
from __future__ import annotations

import os
import sys
import tempfile
import glob as glob_module
from fnmatch import fnmatchcase
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from backend.runtime_env import ShellEnvironmentPolicy, sanitize_env_overrides


class FileSystemAccessMode(str, Enum):
    READ = "read"
    WRITE = "write"
    DENY = "deny"

    @property
    def can_read(self) -> bool:
        return self is not FileSystemAccessMode.DENY

    @property
    def can_write(self) -> bool:
        return self is FileSystemAccessMode.WRITE


class FileSystemSandboxKind(str, Enum):
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"
    EXTERNAL = "external-sandbox"


class FileSystemSpecialPath(str, Enum):
    ROOT = "root"
    MINIMAL = "minimal"
    PROJECT_ROOTS = "project_roots"
    TMPDIR = "tmpdir"
    SLASH_TMP = "slash_tmp"


@dataclass(frozen=True, slots=True)
class FileSystemPath:
    kind: Literal["path", "glob_pattern", "special"]
    value: Path | str | FileSystemSpecialPath
    subpath: str | None = None

    @classmethod
    def path(cls, path: str | Path) -> "FileSystemPath":
        return cls("path", Path(path).expanduser())

    @classmethod
    def glob(cls, pattern: str) -> "FileSystemPath":
        return cls("glob_pattern", str(pattern))

    @classmethod
    def special(
        cls,
        value: FileSystemSpecialPath,
        *,
        subpath: str | None = None,
    ) -> "FileSystemPath":
        return cls("special", value, subpath)


@dataclass(frozen=True, slots=True)
class FileSystemSandboxEntry:
    path: FileSystemPath
    access: FileSystemAccessMode
    missing_path_behavior: Literal["skip"] | None = None

    def __post_init__(self) -> None:
        if self.path.kind == "glob_pattern" and self.access is not FileSystemAccessMode.DENY:
            raise ValueError("Filesystem glob entries may only deny access")


@dataclass(frozen=True, slots=True)
class FileSystemSandboxPolicy:
    kind: FileSystemSandboxKind = FileSystemSandboxKind.RESTRICTED
    entries: tuple[FileSystemSandboxEntry, ...] = ()
    glob_scan_max_depth: int | None = None

    @classmethod
    def restricted(
        cls,
        entries: tuple[FileSystemSandboxEntry, ...] | list[FileSystemSandboxEntry],
        *,
        glob_scan_max_depth: int | None = None,
    ) -> "FileSystemSandboxPolicy":
        return cls(
            kind=FileSystemSandboxKind.RESTRICTED,
            entries=tuple(entries),
            glob_scan_max_depth=glob_scan_max_depth,
        )

    @classmethod
    def unrestricted(cls) -> "FileSystemSandboxPolicy":
        return cls(kind=FileSystemSandboxKind.UNRESTRICTED)

    def has_denied_read_restrictions(self) -> bool:
        return any(entry.access is FileSystemAccessMode.DENY for entry in self.entries)


class NetworkSandboxPolicy(str, Enum):
    RESTRICTED = "restricted"
    ENABLED = "enabled"


class SandboxEnforcement(str, Enum):
    MANAGED = "managed"
    DISABLED = "disabled"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class PermissionProfile:
    enforcement: SandboxEnforcement
    file_system: FileSystemSandboxPolicy
    network: NetworkSandboxPolicy

    @classmethod
    def managed(
        cls,
        file_system: FileSystemSandboxPolicy,
        *,
        network: NetworkSandboxPolicy = NetworkSandboxPolicy.RESTRICTED,
    ) -> "PermissionProfile":
        return cls(SandboxEnforcement.MANAGED, file_system, network)

    @classmethod
    def disabled(cls) -> "PermissionProfile":
        return cls(
            SandboxEnforcement.DISABLED,
            FileSystemSandboxPolicy.unrestricted(),
            NetworkSandboxPolicy.ENABLED,
        )

    @classmethod
    def external(
        cls,
        *,
        network: NetworkSandboxPolicy = NetworkSandboxPolicy.RESTRICTED,
    ) -> "PermissionProfile":
        return cls(
            SandboxEnforcement.EXTERNAL,
            FileSystemSandboxPolicy(kind=FileSystemSandboxKind.EXTERNAL),
            network,
        )


@dataclass(frozen=True, slots=True)
class FileSystemPermissions:
    entries: tuple[FileSystemSandboxEntry, ...] = ()
    glob_scan_max_depth: int | None = None


@dataclass(frozen=True, slots=True)
class NetworkPermissions:
    enabled: bool | None = None


@dataclass(frozen=True, slots=True)
class AdditionalPermissionProfile:
    file_system: FileSystemPermissions | None = None
    network: NetworkPermissions | None = None

    @property
    def is_empty(self) -> bool:
        return (
            self.file_system is None or not self.file_system.entries
        ) and (
            self.network is None or self.network.enabled is None
        )


DEFAULT_PROTECTED_METADATA_NAMES: tuple[str, ...] = (
    ".git",
    # MiniCode's own instructions, rules, agents, todos and worktree bookkeeping.
    ".minicode",
    ".mcp.json",
    ".gitconfig",
    ".gitmodules",
    "settings.json",
    "settings.local.json",
)


@dataclass(frozen=True, slots=True)
class WritableRoot:
    root: Path
    read_only_subpaths: tuple[Path, ...] = ()
    protected_metadata_names: tuple[str, ...] = ()

    def is_path_writable(self, path: str | Path) -> bool:
        candidate = _absolute_path(path)
        if not _is_relative_to(candidate, self.root):
            return False
        if any(_is_relative_to(candidate, readonly) for readonly in self.read_only_subpaths):
            return False
        try:
            first = candidate.relative_to(self.root).parts[0]
        except (IndexError, ValueError):
            return True
        return first.casefold() not in {
            name.casefold() for name in self.protected_metadata_names
        }


@dataclass(frozen=True, slots=True)
class ResolvedSandboxPolicy:
    enforcement: SandboxEnforcement
    network: NetworkSandboxPolicy
    root_access: FileSystemAccessMode | None = None
    full_disk_read: bool = False
    full_disk_write: bool = False
    include_platform_defaults: bool = True
    readable_roots: tuple[Path, ...] = ()
    writable_roots: tuple[WritableRoot, ...] = ()
    unreadable_roots: tuple[Path, ...] = ()
    unreadable_globs: tuple[str, ...] = ()
    resolved_entries: tuple[tuple[Path, FileSystemAccessMode], ...] = ()
    entries: tuple[FileSystemSandboxEntry, ...] = ()
    glob_scan_max_depth: int | None = None

    @property
    def allow_network(self) -> bool:
        return self.network is NetworkSandboxPolicy.ENABLED

    @property
    def has_denied_read_restrictions(self) -> bool:
        return bool(self.unreadable_roots or self.unreadable_globs)

    @property
    def root_read_baseline(self) -> bool:
        return self.root_access is not None and self.root_access.can_read

    @property
    def root_write_baseline(self) -> bool:
        return self.root_access is FileSystemAccessMode.WRITE

    def resolve_access(self, path: str | Path) -> FileSystemAccessMode:
        access = _resolve_concrete_access(
            path,
            self.resolved_entries,
            baseline=self.root_access or FileSystemAccessMode.DENY,
        )
        candidate = _absolute_path(path)
        if any(_glob_matches_path(pattern, candidate) for pattern in self.unreadable_globs):
            return FileSystemAccessMode.DENY
        return access


@dataclass(frozen=True)
class SandboxPolicy:
    """One subprocess launch request with a canonical permission profile.

    ``writable_roots``/``readable_roots`` remain accepted while callers migrate
    to ``permission_profile``. They are compiled once in ``__post_init__`` and
    are not the policy consumed by the OS backends.
    """

    permission_profile: PermissionProfile | None = None
    workspace_root: Path | None = None
    workspace_roots: tuple[Path, ...] = ()
    policy_cwd: Path | None = None
    writable_roots: tuple[Path, ...] = ()
    readable_roots: tuple[Path, ...] = ()
    denied_roots: tuple[Path, ...] = ()
    denied_globs: tuple[str, ...] = ()
    allow_network: bool = False
    disable_os_sandbox: bool = False
    protect_workspace_metadata: bool = True
    fail_if_unavailable: bool = True
    preflight_required: bool = False
    allow_unsandboxed_commands: bool = True
    auto_allow_commands_if_sandboxed: bool = False
    excluded_commands: tuple[str, ...] = ()
    policy_limitations: tuple[str, ...] = ()
    shell_environment_policy: ShellEnvironmentPolicy = field(
        default_factory=ShellEnvironmentPolicy
    )
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None

    def __post_init__(self) -> None:
        workspace_root = (
            _absolute_path(self.workspace_root)
            if self.workspace_root is not None
            else None
        )
        workspace_roots = tuple(
            _dedupe_paths(
                [
                    *(self.workspace_roots or ()),
                    *((workspace_root,) if workspace_root is not None else ()),
                ]
            )
        )
        writable_roots = tuple(_dedupe_paths(self.writable_roots))
        readable_roots = tuple(_dedupe_paths(self.readable_roots))
        denied_roots = tuple(_dedupe_paths(self.denied_roots))
        policy_cwd = (
            _absolute_path(self.policy_cwd)
            if self.policy_cwd is not None
            else workspace_root
        )

        object.__setattr__(self, "workspace_root", workspace_root)
        object.__setattr__(self, "workspace_roots", workspace_roots)
        object.__setattr__(self, "writable_roots", writable_roots)
        object.__setattr__(self, "readable_roots", readable_roots)
        object.__setattr__(self, "denied_roots", denied_roots)
        object.__setattr__(self, "policy_cwd", policy_cwd)
        object.__setattr__(
            self,
            "excluded_commands",
            tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in self.excluded_commands
                    if str(value).strip()
                )
            ),
        )
        object.__setattr__(
            self,
            "policy_limitations",
            tuple(
                dict.fromkeys(
                    str(value).strip()
                    for value in self.policy_limitations
                    if str(value).strip()
                )
            ),
        )
        object.__setattr__(
            self,
            "env_overrides",
            sanitize_env_overrides(self.env_overrides),
        )
        object.__setattr__(
            self,
            "shell_environment_policy",
            ShellEnvironmentPolicy.from_mapping(self.shell_environment_policy),
        )

        profile = self.permission_profile
        if profile is None:
            profile = _legacy_permission_profile(
                workspace_roots=workspace_roots,
                writable_roots=writable_roots,
                readable_roots=readable_roots,
                denied_roots=denied_roots,
                denied_globs=self.denied_globs,
                allow_network=self.allow_network,
                disabled=self.disable_os_sandbox,
                protect_workspace_metadata=self.protect_workspace_metadata,
            )
            object.__setattr__(self, "permission_profile", profile)
        object.__setattr__(
            self,
            "allow_network",
            profile.network is NetworkSandboxPolicy.ENABLED,
        )
        object.__setattr__(
            self,
            "disable_os_sandbox",
            profile.enforcement is SandboxEnforcement.DISABLED,
        )

    @classmethod
    def workspace_default(
        cls,
        workspace: Path,
        *,
        timeout: float | None = None,
    ) -> "SandboxPolicy":
        return cls(
            workspace_root=workspace,
            writable_roots=(workspace,),
            allow_network=False,
            timeout=timeout,
        )

    @classmethod
    def permissive(
        cls,
        workspace: Path,
        *,
        timeout: float | None = None,
    ) -> "SandboxPolicy":
        return cls(
            workspace_root=workspace,
            writable_roots=(workspace,),
            allow_network=True,
            timeout=timeout,
        )

    @classmethod
    def bypass(
        cls,
        *,
        timeout: float | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> "SandboxPolicy":
        return cls(
            permission_profile=PermissionProfile.disabled(),
            env_overrides=dict(env_overrides or {}),
            timeout=timeout,
        )

    def resolve(self, *, cwd: str | Path | None = None) -> ResolvedSandboxPolicy:
        assert self.permission_profile is not None
        return resolve_permission_profile(
            self.permission_profile,
            workspace_roots=self.workspace_roots,
            cwd=_absolute_path(cwd or self.policy_cwd or Path.cwd()),
            protect_workspace_metadata=self.protect_workspace_metadata,
        )

    def with_additional_permissions(
        self,
        additional: AdditionalPermissionProfile,
    ) -> "SandboxPolicy":
        if additional.is_empty:
            return self
        assert self.permission_profile is not None
        profile = self.permission_profile
        if profile.enforcement is SandboxEnforcement.DISABLED:
            return self
        # codex returns Unrestricted/ExternalSandbox file policies unchanged
        # when merging additional permissions (policy_transforms.rs); folding
        # EXTERNAL into a restricted managed profile would deny the preview
        # subprocess everything.
        if profile.enforcement is SandboxEnforcement.EXTERNAL:
            return self

        entries = list(profile.file_system.entries)
        glob_depth = profile.file_system.glob_scan_max_depth
        if additional.file_system is not None:
            entries.extend(additional.file_system.entries)
            if additional.file_system.glob_scan_max_depth is not None:
                glob_depth = additional.file_system.glob_scan_max_depth
        network = profile.network
        # codex's network merge is grant-only (policy_transforms
        # effective_network_sandbox_policy): additional permissions may enable
        # networking but must never downgrade a base policy that already has
        # it enabled.
        if (
            additional.network is not None
            and additional.network.enabled
            and network is not NetworkSandboxPolicy.ENABLED
        ):
            network = NetworkSandboxPolicy.ENABLED
        effective = PermissionProfile.managed(
            FileSystemSandboxPolicy.restricted(entries, glob_scan_max_depth=glob_depth),
            network=network,
        )
        return replace(self, permission_profile=effective)

    def escalated_preserving_denied_reads(self) -> "SandboxPolicy":
        """Grant broad access only when doing so cannot drop deny-read rules."""
        resolved = self.resolve()
        if not resolved.has_denied_read_restrictions:
            return replace(
                self,
                permission_profile=PermissionProfile.disabled(),
                workspace_root=None,
                workspace_roots=(),
                policy_cwd=None,
                writable_roots=(),
                readable_roots=(),
                denied_roots=(),
                denied_globs=(),
            )

        assert self.permission_profile is not None
        entries = list(self.permission_profile.file_system.entries)
        entries.append(
            FileSystemSandboxEntry(
                FileSystemPath.special(FileSystemSpecialPath.ROOT),
                FileSystemAccessMode.WRITE,
            )
        )
        profile = PermissionProfile.managed(
            FileSystemSandboxPolicy.restricted(
                entries,
                glob_scan_max_depth=self.permission_profile.file_system.glob_scan_max_depth,
            ),
            network=NetworkSandboxPolicy.ENABLED,
        )
        return replace(self, permission_profile=profile)


def sandbox_policy_for_permission_context(
    workspace_root: str | Path,
    permission_context: Any,
    *,
    config_stack: Any | None = None,
) -> SandboxPolicy:
    """Compile the canonical launch policy for any MiniCode control surface.

    Agent turns and explicit terminal commands must use identical managed
    requirements and filesystem/network constraints. Callers supply the
    target conversation's immutable permission context; this helper owns the
    config-layer projection instead of allowing a second shell policy.
    """
    from backend.config import load_config_layer_stack

    workspace = _absolute_path(workspace_root)
    stack = config_stack or load_config_layer_stack(cwd=workspace)
    effective_config = stack.effective_config()
    policy_config = stack.policy_config() or {}
    requirements = stack.requirements
    effective_permission = permission_context
    resolved_mode, _violation = requirements.resolve_permission_mode(
        getattr(effective_permission, "mode", "confirm")
    )
    if _violation is not None:
        raise _violation
    constraints = {
        key: list(value)
        for key, value in (
            getattr(effective_permission, "filesystem_constraints", {}) or {}
        ).items()
    }
    if requirements.filesystem_deny_read:
        constraints["denylist"] = list(
            dict.fromkeys(
                [
                    *requirements.filesystem_deny_read,
                    *constraints.get("denylist", []),
                ]
            )
        )
    effective_permission = replace(
        effective_permission,
        mode=resolved_mode,
        approval_policy=requirements.approval_policy_for_mode(resolved_mode),
        sandbox_mode=requirements.sandbox_mode_for_permission_mode(resolved_mode),
        filesystem_constraints=constraints,
    )
    effective_sandbox = effective_config.get("sandbox")
    effective_permissions = effective_config.get("permissions")
    effective_workspace_write = effective_config.get("sandbox_workspace_write")
    managed_sandbox = policy_config.get("sandbox")
    managed_permissions = policy_config.get("permissions")
    effective_sandbox = effective_sandbox if isinstance(effective_sandbox, Mapping) else {}
    effective_permissions = effective_permissions if isinstance(effective_permissions, Mapping) else {}
    managed_sandbox = managed_sandbox if isinstance(managed_sandbox, Mapping) else {}
    managed_permissions = managed_permissions if isinstance(managed_permissions, Mapping) else {}
    managed_sandbox_snapshot = dict(managed_sandbox)
    for key, value in requirements.sandbox_settings.items():
        if isinstance(value, Mapping) and isinstance(managed_sandbox_snapshot.get(key), Mapping):
            managed_sandbox_snapshot[key] = {
                **dict(managed_sandbox_snapshot[key]),
                **dict(value),
            }
        else:
            managed_sandbox_snapshot[key] = value
    allowed_modes = requirements.value_for("allowed_sandbox_modes", ())
    if not isinstance(allowed_modes, list):
        allowed_modes = ()
    return sandbox_policy_from_config_snapshot(
        workspace,
        sandbox_mode=(
            getattr(effective_permission, "sandbox_mode", "")
            or requirements.sandbox_mode_for_permission_mode(resolved_mode)
        ),
        sandbox_settings=effective_sandbox,
        managed_sandbox_settings=managed_sandbox_snapshot,
        permission_settings=effective_permissions,
        managed_permission_settings=managed_permissions,
        workspace_write_settings=(
            effective_workspace_write
            if isinstance(effective_workspace_write, Mapping)
            else {}
        ),
        filesystem_constraints=(
            getattr(effective_permission, "filesystem_constraints", {}) or {}
        ),
        requirements_deny_read=requirements.filesystem_deny_read,
        requirements_network=requirements.network_constraints,
        allowed_sandbox_modes=allowed_modes,
        shell_environment_policy=effective_config.get("shell_environment_policy"),
    )


def sandbox_policy_from_config_snapshot(
    workspace_root: str | Path,
    *,
    sandbox_mode: str,
    sandbox_settings: Mapping[str, Any] | None = None,
    managed_sandbox_settings: Mapping[str, Any] | None = None,
    permission_settings: Mapping[str, Any] | None = None,
    managed_permission_settings: Mapping[str, Any] | None = None,
    workspace_write_settings: Mapping[str, Any] | None = None,
    filesystem_constraints: Mapping[str, Iterable[str]] | None = None,
    requirements_deny_read: Iterable[str] = (),
    requirements_network: Mapping[str, Any] | None = None,
    allowed_sandbox_modes: Iterable[str] = (),
    shell_environment_policy: Mapping[str, Any] | ShellEnvironmentPolicy | None = None,
) -> SandboxPolicy:
    """Compile one turn's config stack into a canonical launch policy.

    This is the adapter boundary shared by Claude's sandbox settings and
    Codex's permission profiles/requirements. Callers pass an already composed
    config snapshot; this module never reads settings files itself.
    """

    workspace = _absolute_path(workspace_root)
    settings = dict(sandbox_settings or {})
    managed = dict(managed_sandbox_settings or {})
    permissions = dict(permission_settings or {})
    managed_permissions = dict(managed_permission_settings or {})
    workspace_write = dict(workspace_write_settings or {})
    constraints = dict(filesystem_constraints or {})

    sandbox_enabled = _mapping_bool(
        managed,
        "enabled",
        default=_mapping_bool(settings, "enabled", default=False),
    )
    enabled_platforms = managed.get("enabledPlatforms", settings.get("enabledPlatforms"))
    if isinstance(enabled_platforms, (list, tuple)) and enabled_platforms:
        current_platform = (
            "windows"
            if os.name == "nt"
            else "macos"
            if sys.platform == "darwin"
            else "linux"
        )
        sandbox_enabled = sandbox_enabled and current_platform in {
            str(value).strip().lower()
            for value in enabled_platforms
            if isinstance(value, str)
        }
    allow_unsandboxed = _mapping_bool(
        managed,
        "allowUnsandboxedCommands",
        default=_mapping_bool(settings, "allowUnsandboxedCommands", default=True),
    )
    fail_if_unavailable = _mapping_bool(
        managed,
        "failIfUnavailable",
        default=_mapping_bool(
            settings,
            "failIfUnavailable",
            default=False if sandbox_enabled else True,
        ),
    )
    auto_allow_commands = sandbox_enabled and _mapping_bool(
        managed,
        "autoAllowCommandsIfSandboxed",
        default=_mapping_bool(settings, "autoAllowCommandsIfSandboxed", default=True),
    )
    allowed_modes = tuple(
        dict.fromkeys(
            str(value).strip().lower()
            for value in allowed_sandbox_modes
            if str(value).strip()
        )
    )
    if allowed_modes and "danger-full-access" not in allowed_modes:
        allow_unsandboxed = False
        fail_if_unavailable = True

    hard_denies = list(
        dict.fromkeys(
            [
                *(str(value) for value in requirements_deny_read if str(value)),
                *(
                    str(value)
                    for value in constraints.get("denylist", ())
                    if str(value)
                ),
            ]
        )
    )
    mode = str(sandbox_mode or "workspace-write").strip().lower()
    limitations: list[str] = []
    if allowed_modes and mode not in allowed_modes:
        # Fail closed, but never silently: the requested sandbox mode was
        # narrowed by the allowed-modes policy and the user must be able to
        # see that their configuration was not honored as-is.
        limitations.append(
            f"Requested sandbox mode '{mode}' is not in the allowed modes "
            f"({', '.join(allowed_modes)}); downgraded to 'read-only'."
        )
        mode = "read-only"

    network = NetworkSandboxPolicy.RESTRICTED
    if mode == "danger-full-access" and allow_unsandboxed:
        network = NetworkSandboxPolicy.ENABLED
    elif _mapping_bool(workspace_write, "network_access", default=False):
        network = NetworkSandboxPolicy.ENABLED
    structured_network = (
        sandbox_enabled
        and _has_structured_network_constraints(settings.get("network"))
    ) or _has_structured_network_constraints(requirements_network)
    if structured_network:
        network = NetworkSandboxPolicy.RESTRICTED
        allow_unsandboxed = False
        fail_if_unavailable = True
        limitations.append(
            "Domain, socket, local-binding, and proxy sandbox constraints require "
            "an authenticated network proxy backend that is unavailable on this host; "
            "subprocess network access remains fully disabled."
        )

    file_system = settings.get("filesystem")
    managed_file_system = managed.get("filesystem")
    file_system = file_system if isinstance(file_system, Mapping) else {}
    managed_file_system = (
        managed_file_system if isinstance(managed_file_system, Mapping) else {}
    )
    filesystem_disabled = sandbox_enabled and _mapping_bool(
        managed_file_system,
        "disabled",
        default=_mapping_bool(file_system, "disabled", default=False),
    )

    allow_write_rules: list[str] = []
    deny_write_rules: list[str] = []
    deny_read_rules: list[str] = []
    allow_read_rules: list[str] = []
    if sandbox_enabled and not filesystem_disabled:
        allow_write_rules.extend(_string_tuple(file_system.get("allowWrite")))
        deny_write_rules.extend(_string_tuple(file_system.get("denyWrite")))
        deny_read_rules.extend(_string_tuple(file_system.get("denyRead")))
        allow_read_rules.extend(_string_tuple(file_system.get("allowRead")))
        allow_write_rules.extend(_permission_rule_paths(permissions, "allow", "edit_file"))
        deny_write_rules.extend(_permission_rule_paths(permissions, "deny", "edit_file"))
        deny_read_rules.extend(_permission_rule_paths(permissions, "deny", "read_file"))
        allow_read_rules.extend(_permission_rule_paths(permissions, "allow", "read_file"))
        if managed_file_system.get("allowManagedReadPathsOnly") is True:
            allow_read_rules = [
                *_string_tuple(managed_file_system.get("allowRead")),
                *_permission_rule_paths(managed_permissions, "allow", "read_file"),
            ]

    has_filesystem_restrictions = bool(
        allow_write_rules
        or deny_write_rules
        or deny_read_rules
        or allow_read_rules
        or hard_denies
    )
    common = {
        "workspace_root": workspace,
        "fail_if_unavailable": fail_if_unavailable,
        "preflight_required": sandbox_enabled and fail_if_unavailable,
        "allow_unsandboxed_commands": allow_unsandboxed,
        "auto_allow_commands_if_sandboxed": auto_allow_commands,
        "excluded_commands": _string_tuple(
            managed.get("excludedCommands", settings.get("excludedCommands"))
        ),
        "policy_limitations": tuple(limitations),
        "shell_environment_policy": ShellEnvironmentPolicy.from_mapping(
            shell_environment_policy
        ),
    }
    if mode == "external-sandbox" and not has_filesystem_restrictions:
        return SandboxPolicy(
            permission_profile=PermissionProfile.external(network=network),
            **common,
        )
    if (
        mode == "danger-full-access"
        and allow_unsandboxed
        and not has_filesystem_restrictions
        and not structured_network
    ):
        common["preflight_required"] = False
        common["auto_allow_commands_if_sandboxed"] = False
        return SandboxPolicy(
            permission_profile=PermissionProfile.disabled(),
            **common,
        )

    entries: list[FileSystemSandboxEntry] = [
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.ROOT),
            FileSystemAccessMode.READ,
        )
    ]
    if mode == "danger-full-access" and allow_unsandboxed:
        entries.append(
            FileSystemSandboxEntry(
                FileSystemPath.special(FileSystemSpecialPath.ROOT),
                FileSystemAccessMode.WRITE,
            )
        )
    elif mode != "read-only":
        raw_write_rules = constraints.get("write_allowlist")
        if raw_write_rules is None:
            raw_write_rules = constraints.get("allowlist", ())
        write_rules = list(raw_write_rules)
        if not write_rules:
            write_rules = ["."]
        write_rules.extend(_string_tuple(workspace_write.get("writable_roots")))
        write_rules.extend(allow_write_rules)
        for path in _expand_policy_paths(write_rules, workspace, grant=True):
            entries.append(
                FileSystemSandboxEntry(
                    FileSystemPath.path(path),
                    FileSystemAccessMode.WRITE,
                )
            )
        if not _mapping_bool(workspace_write, "exclude_tmpdir_env_var", default=False):
            entries.append(
                FileSystemSandboxEntry(
                    FileSystemPath.special(FileSystemSpecialPath.TMPDIR),
                    FileSystemAccessMode.WRITE,
                )
            )
        if not _mapping_bool(workspace_write, "exclude_slash_tmp", default=False):
            entries.append(
                FileSystemSandboxEntry(
                    FileSystemPath.special(FileSystemSpecialPath.SLASH_TMP),
                    FileSystemAccessMode.WRITE,
                )
            )

    for path in _expand_policy_paths(deny_write_rules, workspace, grant=False):
        entries.append(
            FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.READ)
        )
    for value in deny_read_rules:
        entries.append(_deny_entry(value, workspace))

    hard_path_denies = tuple(
        _deny_static_prefix(value, workspace)
        for value in hard_denies
        if not _contains_glob(value)
    )
    for path in _expand_policy_paths(allow_read_rules, workspace, grant=False):
        if any(_is_relative_to(path, denied) for denied in hard_path_denies):
            continue
        entries.append(
            FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.READ)
        )
    for path in _expand_policy_paths(
        constraints.get("readable_roots", ()), workspace, grant=True
    ):
        entries.append(
            FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.READ)
        )
    for value in hard_denies:
        entries.append(_deny_entry(value, workspace))

    profile = PermissionProfile.managed(
        FileSystemSandboxPolicy.restricted(entries),
        network=network,
    )
    return SandboxPolicy(permission_profile=profile, **common)


def resolve_permission_profile(
    profile: PermissionProfile,
    *,
    workspace_roots: tuple[Path, ...],
    cwd: Path,
    protect_workspace_metadata: bool = True,
) -> ResolvedSandboxPolicy:
    file_system = profile.file_system
    if profile.enforcement is SandboxEnforcement.DISABLED:
        return ResolvedSandboxPolicy(
            enforcement=SandboxEnforcement.DISABLED,
            network=NetworkSandboxPolicy.ENABLED,
            root_access=FileSystemAccessMode.WRITE,
            full_disk_read=True,
            full_disk_write=True,
            include_platform_defaults=False,
            entries=file_system.entries,
        )
    if profile.enforcement is SandboxEnforcement.EXTERNAL:
        return ResolvedSandboxPolicy(
            enforcement=SandboxEnforcement.EXTERNAL,
            network=profile.network,
            root_access=FileSystemAccessMode.WRITE,
            full_disk_read=True,
            full_disk_write=True,
            include_platform_defaults=False,
            entries=file_system.entries,
        )
    if file_system.kind is FileSystemSandboxKind.UNRESTRICTED:
        return ResolvedSandboxPolicy(
            enforcement=SandboxEnforcement.MANAGED,
            network=profile.network,
            root_access=FileSystemAccessMode.WRITE,
            full_disk_read=True,
            full_disk_write=True,
            include_platform_defaults=False,
            entries=file_system.entries,
        )

    concrete: list[tuple[Path, FileSystemAccessMode]] = []
    unreadable_globs: list[str] = []
    full_disk_access: list[FileSystemAccessMode] = []
    minimal_access: list[FileSystemAccessMode] = []
    for entry in file_system.entries:
        if entry.path.kind == "glob_pattern":
            unreadable_globs.append(_resolve_glob(str(entry.path.value), cwd))
            continue
        if entry.path.kind == "path":
            raw_path = Path(entry.path.value)
            path = raw_path if raw_path.is_absolute() else cwd / raw_path
            if entry.missing_path_behavior == "skip" and not path.exists():
                continue
            concrete.extend((candidate, entry.access) for candidate in _path_candidates(path))
            continue

        special = entry.path.value
        if special is FileSystemSpecialPath.ROOT:
            full_disk_access.append(entry.access)
        elif special is FileSystemSpecialPath.MINIMAL:
            minimal_access.append(entry.access)
        elif special is FileSystemSpecialPath.PROJECT_ROOTS:
            for root in workspace_roots:
                path = root / entry.path.subpath if entry.path.subpath else root
                concrete.extend((candidate, entry.access) for candidate in _path_candidates(path))
        elif special is FileSystemSpecialPath.TMPDIR:
            concrete.append((_absolute_path(tempfile.gettempdir()), entry.access))
        elif special is FileSystemSpecialPath.SLASH_TMP and os.name != "nt":
            concrete.append((Path("/tmp"), entry.access))

    concrete = _dedupe_resolved_entries(concrete)
    include_platform_defaults = (
        max(
            minimal_access,
            default=FileSystemAccessMode.DENY,
            key=lambda access: {
                FileSystemAccessMode.READ: 0,
                FileSystemAccessMode.WRITE: 1,
                FileSystemAccessMode.DENY: 2,
            }[access],
        ).can_read
    )
    read_paths = _ordered_paths(
        path for path, access in concrete if access is FileSystemAccessMode.READ
    )
    write_paths = _ordered_paths(
        path for path, access in concrete if access is FileSystemAccessMode.WRITE
    )
    deny_paths = _ordered_paths(
        path for path, access in concrete if access is FileSystemAccessMode.DENY
    )
    protected_roots = set(workspace_roots) if protect_workspace_metadata else set()

    root_access = max(
        full_disk_access,
        default=None,
        key=_access_precedence,
    )
    full_disk_read = (
        root_access in {FileSystemAccessMode.READ, FileSystemAccessMode.WRITE}
        and not (deny_paths or unreadable_globs)
    )
    full_disk_write = (
        root_access is FileSystemAccessMode.WRITE
        and not (read_paths or deny_paths or unreadable_globs)
    )

    writable: list[WritableRoot] = []
    # Keep nested write roots. A narrower write entry can deliberately reopen a
    # path under a broader read-only or deny entry; Codex resolves the most
    # specific matching filesystem entry rather than collapsing all children
    # under the first parent root.
    for root in write_paths:
        read_only = [path for path in read_paths if path != root and _is_relative_to(path, root)]
        protected_names = tuple(
            name
            for name in DEFAULT_PROTECTED_METADATA_NAMES
            if _matches_workspace_root(root, protected_roots)
            and not _has_explicit_metadata_write(concrete, root / name)
        )
        if protected_names:
            read_only.extend(root / name for name in protected_names if (root / name).exists())
            read_only.extend(_resolved_gitdir_paths(root))
        writable.append(
            WritableRoot(
                root=root,
                read_only_subpaths=tuple(_dedupe_paths(read_only)),
                protected_metadata_names=tuple(protected_names),
            )
        )

    if root_access is FileSystemAccessMode.WRITE:
        root = _filesystem_root(cwd)
        narrower_read = [path for path in read_paths if path != root]
        protected_names = tuple(
            name
            for name in DEFAULT_PROTECTED_METADATA_NAMES
            if _matches_workspace_root(root, protected_roots)
            and not _has_explicit_metadata_write(concrete, root / name)
        )
        writable.insert(
            0,
            WritableRoot(
                root=root,
                read_only_subpaths=tuple(_dedupe_paths(narrower_read)),
                protected_metadata_names=tuple(protected_names),
            ),
        )

    return ResolvedSandboxPolicy(
        enforcement=SandboxEnforcement.MANAGED,
        network=profile.network,
        root_access=root_access,
        full_disk_read=full_disk_read,
        full_disk_write=full_disk_write,
        include_platform_defaults=include_platform_defaults and not full_disk_read,
        # Nested read and deny roots are semantically significant. For example,
        # deny /private, read /private/public, deny /private/public/token must
        # survive resolution in this exact order for an OS backend to layer the
        # corresponding mounts correctly.
        readable_roots=tuple(read_paths),
        writable_roots=tuple(writable),
        unreadable_roots=tuple(deny_paths),
        unreadable_globs=tuple(dict.fromkeys(unreadable_globs)),
        resolved_entries=tuple(concrete),
        entries=file_system.entries,
        glob_scan_max_depth=file_system.glob_scan_max_depth,
    )


def _mapping_bool(value: Mapping[str, Any], key: str, *, default: bool) -> bool:
    candidate = value.get(key)
    return candidate if isinstance(candidate, bool) else default


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item).strip() for item in value if isinstance(item, str) and item.strip())


def _permission_rule_paths(
    permissions: Mapping[str, Any],
    decision: str,
    tool_glob: str,
) -> tuple[str, ...]:
    """Collect the paths one Tool(content) decision grants or refuses.

    Both spellings must be read. ``allow``/``deny`` is the settings.json syntax,
    while the approval dialog's "always allow/deny" action persists
    ``content_allow_rules``/``content_deny_rules``; the permission checker merges
    the pair, so a sandbox that read only one key silently ignored every rule the
    dialog wrote.
    """
    from backend.permissions.content_rules import parse_content_rules

    raw_rules: list[str] = []
    for key in (f"content_{decision}_rules", decision):
        raw_rules.extend(_string_tuple(permissions.get(key)))
    paths: list[str] = []
    for rule in parse_content_rules(list(dict.fromkeys(raw_rules))):
        if rule.tool_glob != tool_glob:
            continue
        paths.append(rule.content or ".")
    return tuple(dict.fromkeys(paths))


def _contains_glob(value: str) -> bool:
    return any(token in str(value) for token in ("*", "?", "["))


def _has_structured_network_constraints(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(
        value.get(key) not in (None, False, (), [], {})
        for key in (
            "allowedDomains",
            "deniedDomains",
            "strictAllowlist",
            "allowManagedDomainsOnly",
            "allowUnixSockets",
            "allowAllUnixSockets",
            "allowLocalBinding",
            "httpProxyPort",
            "socksProxyPort",
            "domains",
            "unix_sockets",
            "managed_allowed_domains_only",
            "allow_local_binding",
            "http_port",
            "socks_port",
        )
    )


def _expand_policy_paths(
    values: Iterable[str],
    workspace: Path,
    *,
    grant: bool,
) -> tuple[Path, ...]:
    paths: list[Path] = []
    for value in values:
        raw = str(value or "").strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = workspace / candidate
        pattern = str(candidate)
        if not any(token in pattern for token in ("*", "?", "[")):
            paths.append(candidate.absolute())
            continue
        normalized = pattern.replace("\\", "/")
        if grant and normalized.endswith("/**"):
            prefix = Path(pattern[:-3]).expanduser().absolute()
            if prefix.exists():
                paths.append(prefix)
        for match in glob_module.iglob(pattern, recursive=True):
            matched = Path(match).expanduser().absolute()
            if matched.exists():
                paths.append(matched)
    return tuple(_dedupe_paths(paths))


def _deny_entry(value: str, workspace: Path) -> FileSystemSandboxEntry:
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    pattern = str(candidate.absolute())
    if any(token in raw for token in ("*", "?", "[")):
        return FileSystemSandboxEntry(
            FileSystemPath.glob(pattern),
            FileSystemAccessMode.DENY,
        )
    return FileSystemSandboxEntry(
        FileSystemPath.path(candidate),
        FileSystemAccessMode.DENY,
    )


def _deny_static_prefix(value: str, workspace: Path) -> Path:
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    pattern = str(candidate.absolute())
    wildcard_at = min(
        (pattern.find(token) for token in ("*", "?", "[") if token in pattern),
        default=-1,
    )
    if wildcard_at < 0:
        return candidate.absolute()
    prefix = pattern[:wildcard_at]
    separator = max(prefix.rfind("/"), prefix.rfind("\\"))
    static = prefix[:separator] if separator >= 0 else str(workspace)
    return Path(static or candidate.anchor or os.sep).absolute()


def _legacy_permission_profile(
    *,
    workspace_roots: tuple[Path, ...],
    writable_roots: tuple[Path, ...],
    readable_roots: tuple[Path, ...],
    denied_roots: tuple[Path, ...],
    denied_globs: tuple[str, ...],
    allow_network: bool,
    disabled: bool,
    protect_workspace_metadata: bool,
) -> PermissionProfile:
    if disabled:
        return PermissionProfile.disabled()
    entries: list[FileSystemSandboxEntry] = [
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.MINIMAL),
            FileSystemAccessMode.READ,
        ),
        FileSystemSandboxEntry(
            FileSystemPath.special(FileSystemSpecialPath.TMPDIR),
            FileSystemAccessMode.WRITE,
        )
    ]
    entries.extend(
        FileSystemSandboxEntry(FileSystemPath.path(root), FileSystemAccessMode.READ)
        for root in workspace_roots
    )
    entries.extend(
        FileSystemSandboxEntry(FileSystemPath.path(root), FileSystemAccessMode.READ)
        for root in readable_roots
    )
    entries.extend(
        FileSystemSandboxEntry(FileSystemPath.path(root), FileSystemAccessMode.WRITE)
        for root in writable_roots
    )
    if protect_workspace_metadata:
        for root in writable_roots:
            if root not in workspace_roots:
                continue
            entries.extend(
                FileSystemSandboxEntry(
                    FileSystemPath.path(root / name),
                    FileSystemAccessMode.READ,
                )
                for name in DEFAULT_PROTECTED_METADATA_NAMES
                if (root / name).exists()
            )
            entries.extend(
                FileSystemSandboxEntry(FileSystemPath.path(path), FileSystemAccessMode.READ)
                for path in _resolved_gitdir_paths(root)
            )
    entries.extend(
        FileSystemSandboxEntry(FileSystemPath.path(root), FileSystemAccessMode.DENY)
        for root in denied_roots
    )
    entries.extend(
        FileSystemSandboxEntry(FileSystemPath.glob(pattern), FileSystemAccessMode.DENY)
        for pattern in denied_globs
    )
    return PermissionProfile.managed(
        FileSystemSandboxPolicy.restricted(entries),
        network=(
            NetworkSandboxPolicy.ENABLED
            if allow_network
            else NetworkSandboxPolicy.RESTRICTED
        ),
    )


def _absolute_path(path: str | Path) -> Path:
    return Path(path).expanduser().absolute()


def _filesystem_root(path: Path) -> Path:
    return Path(path.anchor or os.sep)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _matches_workspace_root(path: Path, roots: set[Path]) -> bool:
    candidates = _path_candidates(path)
    return any(
        candidate == root_candidate
        for root in roots
        for candidate in candidates
        for root_candidate in _path_candidates(root)
    )


def _path_candidates(path: Path) -> tuple[Path, ...]:
    literal = _absolute_path(path)
    candidates = [literal]
    try:
        canonical = literal.resolve(strict=False)
    except OSError:
        canonical = literal
    if canonical != literal:
        candidates.append(canonical)
    return tuple(candidates)


def _dedupe_paths(paths: object) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for raw_path in paths:  # type: ignore[union-attr]
        path = _absolute_path(raw_path)
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _dedupe_resolved_entries(
    entries: list[tuple[Path, FileSystemAccessMode]],
) -> list[tuple[Path, FileSystemAccessMode]]:
    precedence = {
        FileSystemAccessMode.READ: 0,
        FileSystemAccessMode.WRITE: 1,
        FileSystemAccessMode.DENY: 2,
    }
    by_path: dict[str, tuple[Path, FileSystemAccessMode]] = {}
    for path, access in entries:
        key = os.path.normcase(str(path))
        previous = by_path.get(key)
        if previous is None or precedence[access] > precedence[previous[1]]:
            by_path[key] = (path, access)
    return list(by_path.values())


def _access_precedence(access: FileSystemAccessMode) -> int:
    return {
        FileSystemAccessMode.READ: 0,
        FileSystemAccessMode.WRITE: 1,
        FileSystemAccessMode.DENY: 2,
    }[access]


def _resolve_concrete_access(
    path: str | Path,
    entries: tuple[tuple[Path, FileSystemAccessMode], ...],
    *,
    baseline: FileSystemAccessMode,
) -> FileSystemAccessMode:
    candidate = _absolute_path(path)
    matches = (
        (entry_path, access)
        for entry_path, access in entries
        if _is_relative_to(candidate, entry_path)
    )
    return max(
        matches,
        default=(Path(candidate.anchor or os.sep), baseline),
        key=lambda item: (len(item[0].parts), _access_precedence(item[1])),
    )[1]


def _has_explicit_metadata_write(
    entries: list[tuple[Path, FileSystemAccessMode]],
    metadata_path: Path,
) -> bool:
    return any(
        access is FileSystemAccessMode.WRITE and path == metadata_path
        for path, access in entries
    )


def _glob_matches_path(pattern: str, path: Path) -> bool:
    normalized_pattern = os.path.normcase(str(Path(pattern).expanduser().absolute()))
    normalized_path = os.path.normcase(str(path))
    return fnmatchcase(normalized_path, normalized_pattern)


def _minimal_roots(paths: list[Path]) -> list[Path]:
    ordered = sorted(_dedupe_paths(paths), key=lambda item: (len(item.parts), str(item)))
    result: list[Path] = []
    for path in ordered:
        if any(_is_relative_to(path, root) for root in result):
            continue
        result.append(path)
    return result


def _ordered_paths(paths: object) -> list[Path]:
    """Return unique paths from broadest to most specific."""

    return sorted(
        _dedupe_paths(paths),
        key=lambda item: (len(item.parts), os.path.normcase(str(item))),
    )


def _resolve_glob(pattern: str, cwd: Path) -> str:
    candidate = Path(pattern).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    return str(cwd / candidate)


def _resolved_gitdir_paths(root: Path) -> tuple[Path, ...]:
    dot_git = root / ".git"
    if not dot_git.is_file():
        return ()
    try:
        first_line = dot_git.read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return ()
    prefix = "gitdir:"
    if not first_line.casefold().startswith(prefix):
        return ()
    raw_target = first_line[len(prefix):].strip()
    if not raw_target:
        return ()
    target = Path(raw_target).expanduser()
    if not target.is_absolute():
        target = dot_git.parent / target
    return _path_candidates(target)
