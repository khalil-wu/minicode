"""Captured executor permissions, serialized in the Source file-URI domain.

The receiving thread constructs this scope from its accepted configuration.
No connection, environment lookup or host filesystem resolution occurs here.
"""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Mapping
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from functools import cache
import re
from typing import Any

from backend.environments.filesystem import file_uri
from backend.sandbox.policy import (
    DEFAULT_PROTECTED_METADATA_NAMES,
    FileSystemAccessMode, FileSystemSandboxKind, FileSystemSpecialPath,
    FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    NetworkSandboxPolicy, PermissionProfile, SandboxEnforcement,
    merge_glob_scan_max_depth,
    _has_structured_network_constraints, _mapping_bool, _permission_rule_paths,
)
from backend.workspace.executor_paths import ExecutorPath


def permission_scope_for_context(context, *, environment_id: str | None = None):
    scope = context.permission.environment_scope
    if scope is not None and (environment_id is None or scope.environment_id == environment_id):
        return scope
    run = context.run_context
    snapshot = run.turn_environments if run is not None else None
    if snapshot is None:
        return None
    environment = (snapshot.primary() if environment_id is None else next((item for item in snapshot.turn_environments()
        if item.selection.environment_id == environment_id), None))
    return environment.permission_scope if environment is not None else None


def _grant_native(path: ExecutorPath) -> str:
    return (path.native.replace("\\", "/") if path.convention == "windows"
        else path.native_bytes().decode("utf-8", errors="surrogateescape"))


def _grant_glob_matches(candidate: ExecutorPath, pattern: ExecutorPath) -> bool:
    """Match MiniCode's configured grant globs in the executor's namespace.

    Source allows globs only for denies. Existing MiniCode grant globs must
    therefore become concrete target paths, with glob.iglob's segment and
    dotfile semantics rather than the controller's pathlib.match semantics.
    """
    if candidate.convention != pattern.convention:
        return False
    name, glob = _grant_native(candidate), _grant_native(pattern)
    if pattern.convention == "windows":
        name, glob = name.lower(), glob.lower()
    names = tuple(part for part in name.split("/") if part)
    patterns = tuple(part for part in glob.split("/") if part)

    @cache
    def matches(index: int, position: int) -> bool:
        if index == len(patterns):
            return position == len(names)
        component = patterns[index]
        if component == "**":
            return matches(index + 1, position) or (position < len(names)
                and not names[position].startswith(".") and matches(index, position + 1))
        return (position < len(names) and (not names[position].startswith(".") or component.startswith("."))
            and fnmatchcase(names[position], component) and matches(index + 1, position + 1))

    return matches(0, 0)


def permission_profile_from_executor_wire(data: Mapping[str, Any]) -> PermissionProfile:
    kind = data["type"]
    if kind == "disabled":
        return PermissionProfile.disabled()
    network = NetworkSandboxPolicy(data["network"])
    if kind == "external":
        return PermissionProfile.external(network=network)
    if kind != "managed":
        raise ValueError(f"Unknown executor permission profile: {kind}")
    filesystem = data["file_system"]
    if filesystem["type"] == "unrestricted":
        return PermissionProfile.managed(FileSystemSandboxPolicy.unrestricted(), network=network)
    if filesystem["type"] != "restricted":
        raise ValueError(f"Unknown executor filesystem permissions: {filesystem['type']}")
    entries = []
    for item in filesystem["entries"]:
        path = item["path"]
        if path["type"] == "path":
            selector = FileSystemPath("path", ExecutorPath.parse(path["path"]).uri)
        elif path["type"] == "glob_pattern":
            selector = FileSystemPath.glob(path["pattern"])
        elif path["type"] == "special":
            special = path["value"]
            value = special["path"] if special["kind"] == "unknown" else FileSystemSpecialPath(special["kind"])
            selector = FileSystemPath("special", value, special.get("subpath"))
        else:
            raise ValueError(f"Unknown executor filesystem selector: {path['type']}")
        entries.append(FileSystemSandboxEntry(selector, FileSystemAccessMode(item["access"]), item.get("missing_path_behavior")))
    return PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries,
        glob_scan_max_depth=filesystem.get("glob_scan_max_depth")), network=network)


def executor_windows_sandbox_selection(windows_sandbox_type: str, windows_sandbox_level: str, cwd_uri: str) -> str:
    # core/tools/sandboxing.rs::executor_windows_sandbox_selection. The
    # executor's path convention, rather than this Python process, selects it.
    if ExecutorPath.parse(cwd_uri).convention != "windows":
        return "disabled"
    if windows_sandbox_type == "windowsMxc":
        return "mxc"
    return "restricted-token" if windows_sandbox_level == "disabled" else windows_sandbox_level


async def capture_environment_permission_profile(permission, config_stack, cwd_uri: str,
        workspace_root_uris: tuple[str, ...], platform_os: str | None, filesystem, *,
        user_home_uri: str | None = None, temporary_directory_uris: tuple[str, ...] | None = None,
        windows_sandbox_type: str = "none", windows_sandbox_level: str = "disabled",
        use_legacy_landlock: bool = False) -> PermissionProfile:
    """Compile accepted MiniCode config in the selected executor's path domain.

    The config/requirements owner is already captured. Only optional grant-glob
    expansion performs IO, through this exact executor under the read profile.
    No controller Path/glob/home lookup participates in an executor grant.
    """
    requirements = config_stack.requirements
    mode, violation = requirements.resolve_permission_mode(permission.mode,
        sandbox_mode=permission.sandbox_mode, approval_policy=permission.approval_policy_value())
    if violation is not None:
        raise violation
    sandbox_mode = requirements.sandbox_mode_for_permission_mode(mode, sandbox_mode=permission.sandbox_mode)
    effective = config_stack.effective_config()
    policy = config_stack.policy_config() or {}
    settings = dict(effective.get("sandbox") or {})
    managed = dict(policy.get("sandbox") or {})
    for key, value in requirements.sandbox_settings.items():
        managed[key] = ({**managed[key], **value}
            if isinstance(value, Mapping) and isinstance(managed.get(key), Mapping) else value)
    permissions = dict(effective.get("permissions") or {})
    managed_permissions = dict(policy.get("permissions") or {})
    workspace_write = dict(effective.get("sandbox_workspace_write") or {})
    constraints = permission.filesystem_constraints
    enabled = _mapping_bool(managed, "enabled", default=_mapping_bool(settings, "enabled", default=False))
    platforms = managed.get("enabledPlatforms", settings.get("enabledPlatforms"))
    if platforms:
        enabled = enabled and platform_os in platforms
    allow_unsandboxed = _mapping_bool(managed, "allowUnsandboxedCommands",
        default=_mapping_bool(settings, "allowUnsandboxedCommands", default=True))
    filesystem_config = dict(settings.get("filesystem") or {})
    managed_filesystem = dict(managed.get("filesystem") or {})
    filesystem_disabled = enabled and _mapping_bool(managed_filesystem, "disabled",
        default=_mapping_bool(filesystem_config, "disabled", default=False))
    allow_write, deny_write, deny_read, allow_read = [], [], [], []
    if enabled and not filesystem_disabled:
        allow_write = [*filesystem_config.get("allowWrite", ()), *_permission_rule_paths(permissions, "allow", "edit_file")]
        deny_write = [*filesystem_config.get("denyWrite", ()), *_permission_rule_paths(permissions, "deny", "edit_file")]
        deny_read = [*filesystem_config.get("denyRead", ()), *_permission_rule_paths(permissions, "deny", "read_file")]
        allow_read = [*filesystem_config.get("allowRead", ()), *_permission_rule_paths(permissions, "allow", "read_file")]
        if managed_filesystem.get("allowManagedReadPathsOnly") is True:
            allow_read = [*managed_filesystem.get("allowRead", ()), *_permission_rule_paths(managed_permissions, "allow", "read_file")]
    hard_denies = tuple(dict.fromkeys((*requirements.filesystem_deny_read, *constraints.get("denylist", ()))))
    structured_network = ((enabled and _has_structured_network_constraints(settings.get("network")))
        or _has_structured_network_constraints(permissions.get("network"))
        or _has_structured_network_constraints(requirements.network_constraints))
    network = (NetworkSandboxPolicy.ENABLED if sandbox_mode == "danger-full-access" and allow_unsandboxed
        or workspace_write.get("network_access", False) else NetworkSandboxPolicy.RESTRICTED)
    if structured_network:
        network = NetworkSandboxPolicy.RESTRICTED
    restrictions = bool(allow_write or deny_write or deny_read or allow_read or hard_denies)
    if sandbox_mode == "external-sandbox" and not restrictions:
        return PermissionProfile.external(network=network)
    if sandbox_mode == "danger-full-access" and allow_unsandboxed and not restrictions and not structured_network:
        return PermissionProfile.disabled()

    cwd = ExecutorPath.parse(cwd_uri, platform_os=platform_os)
    def resolve(value: str) -> ExecutorPath:
        return ExecutorPath.resolve(value, cwd_uri=cwd.uri, platform_os=platform_os, user_home_uri=user_home_uri)

    def deny_entry(value: str) -> FileSystemSandboxEntry:
        candidate = resolve(value)
        selector = (FileSystemPath.glob(candidate.native) if any(char in value for char in "*?[")
            else FileSystemPath("path", candidate.uri))
        return FileSystemSandboxEntry(selector, FileSystemAccessMode.DENY)

    read_denies = [deny_entry(value) for value in (*deny_read, *hard_denies)]
    discovery_profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
        FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
        *read_denies,
    ]))
    discovery = EnvironmentPermissionScope("policy-materialization", discovery_profile, cwd.uri,
        tuple(workspace_root_uris), platform_os, user_home_uri, temporary_directory_uris,
        executor_windows_sandbox_selection(windows_sandbox_type, windows_sandbox_level, cwd.uri), use_legacy_landlock)

    async def expand(values) -> tuple[ExecutorPath, ...]:
        from backend.environments.errors import ExecutorRpcError
        found: dict[ExecutorPath, None] = {}
        for value in values:
            raw = str(value).strip()
            if not raw:
                continue
            if not any(char in raw for char in "*?["):
                found[resolve(raw)] = None
                continue
            pattern = resolve(raw)
            prefix = pattern.parent
            while any(char in _grant_native(prefix) for char in "*?["):
                prefix = prefix.parent
            try:
                # A concrete directory grant already covers its descendants,
                # including unreadable children. Do not scan that subtree or
                # promote a static prefix of a more complex wildcard grant.
                if pattern.name == "**" and pattern.parent == prefix:
                    metadata = await filesystem.get_metadata(prefix.uri, sandbox=discovery.sandbox_context())
                    if metadata["isDirectory"]:
                        found[prefix] = None
                    continue
                outcome = await filesystem.walk(prefix.uri, {"maxDepth": 64, "maxDirectories": 10000,
                    "maxEntries": 50000, "followDirectorySymlinks": False, "pruneHiddenDirectories": False},
                    sandbox=discovery.sandbox_context())
            except ExecutorRpcError as error:
                if error.code != -32004:
                    raise
                continue
            except FileNotFoundError:
                continue
            if outcome["errors"] or outcome["truncated"]:
                raise ValueError("Executor permission grant expansion was not complete")
            for item in outcome["entries"]:
                candidate = ExecutorPath.parse(item["path"], platform_os=platform_os)
                if _grant_glob_matches(candidate, pattern):
                    found[candidate] = None
        return tuple(found)

    entries = [FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ)]
    if sandbox_mode == "danger-full-access" and allow_unsandboxed:
        entries.append(FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.WRITE))
    elif sandbox_mode != "read-only":
        write_rules = list(constraints.get("write_allowlist", constraints.get("allowlist", ()))) or ["."]
        write_rules.extend(workspace_write.get("writable_roots", ()))
        write_rules.extend(allow_write)
        if "." in write_rules:
            entries.append(FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.PROJECT_ROOTS), FileSystemAccessMode.WRITE))
        for path in await expand(value for value in write_rules if value != "."):
            entries.append(FileSystemSandboxEntry(FileSystemPath("path", path.uri), FileSystemAccessMode.WRITE))
        if not workspace_write.get("exclude_tmpdir_env_var", False):
            entries.append(FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.TMPDIR), FileSystemAccessMode.WRITE))
        if not workspace_write.get("exclude_slash_tmp", False):
            entries.append(FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.SLASH_TMP), FileSystemAccessMode.WRITE))
    for path in await expand(deny_write):
        entries.append(FileSystemSandboxEntry(FileSystemPath("path", path.uri), FileSystemAccessMode.READ))
    entries.extend(deny_entry(value) for value in deny_read)
    for path in await expand((*allow_read, *constraints.get("readable_roots", ()))):
        entries.append(FileSystemSandboxEntry(FileSystemPath("path", path.uri), FileSystemAccessMode.READ))
    entries.extend(deny_entry(value) for value in hard_denies)
    if sandbox_mode != "read-only":
        entries.extend(FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.PROJECT_ROOTS, subpath=name),
            FileSystemAccessMode.READ) for name in DEFAULT_PROTECTED_METADATA_NAMES)
    return PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries), network=network)


def executor_permission_profile(profile: PermissionProfile) -> dict[str, Any]:
    if profile.enforcement is SandboxEnforcement.DISABLED:
        return {"type": "disabled"}
    if profile.enforcement is SandboxEnforcement.EXTERNAL:
        return {"type": "external", "network": profile.network.value}
    filesystem = profile.file_system
    if filesystem.kind is FileSystemSandboxKind.UNRESTRICTED:
        filesystem_wire = {"type": "unrestricted"}
    else:
        entries = []
        for entry in filesystem.entries:
            selector = entry.path
            if selector.kind == "path":
                path = {"type": "path", "path": file_uri(str(selector.value))}
            elif selector.kind == "glob_pattern":
                path = {"type": "glob_pattern", "pattern": str(selector.value)}
            else:
                special = ({"kind": selector.value.value} if isinstance(selector.value, FileSystemSpecialPath)
                    else {"kind": "unknown", "path": selector.value})
                if selector.subpath is not None:
                    special["subpath"] = selector.subpath
                path = {"type": "special", "value": special}
            item = {"path": path, "access": entry.access.value}
            if entry.missing_path_behavior is not None:
                item["missing_path_behavior"] = entry.missing_path_behavior
            entries.append(item)
        filesystem_wire = {"type": "restricted", "entries": entries}
        if filesystem.glob_scan_max_depth is not None:
            filesystem_wire["glob_scan_max_depth"] = filesystem.glob_scan_max_depth
    return {"type": "managed", "file_system": filesystem_wire, "network": profile.network.value}


@dataclass(frozen=True, slots=True)
class EnvironmentPermissionScope:
    """Pure authority snapshot; the actual attachment retains the live lease."""

    environment_id: str
    permission_profile: PermissionProfile
    cwd_uri: str
    workspace_root_uris: tuple[str, ...]
    platform_os: str | None
    user_home_uri: str | None = None
    temporary_directory_uris: tuple[str, ...] | None = None
    windows_sandbox_level: str = "disabled"
    use_legacy_landlock: bool = False

    def resolve_path(self, value: str) -> ExecutorPath:
        return ExecutorPath.resolve(value, cwd_uri=self.cwd_uri, platform_os=self.platform_os,
            user_home_uri=self.user_home_uri)

    def process_sandbox_context(self, *, enforce_managed_network: bool = False) -> dict[str, Any] | None:
        # Source SandboxManager::select_initial chooses no OS sandbox for
        # Disabled/External or a fully unrestricted managed profile. FS RPCs
        # still receive sandbox_context(); process None is an explicit launch
        # choice here, never a fallback after a rejected sandbox attempt.
        profile = self.permission_profile
        if not enforce_managed_network and (profile.enforcement is not SandboxEnforcement.MANAGED or (
                self.has_full_disk_read_access() and self.has_full_disk_write_access()
                and profile.network is NetworkSandboxPolicy.ENABLED)):
            return None
        return self.sandbox_context()

    def with_additional_permissions(self, additional) -> EnvironmentPermissionScope:
        profile = self.permission_profile
        if additional.is_empty or profile.enforcement is not SandboxEnforcement.MANAGED:
            return self
        filesystem = profile.file_system
        if additional.file_system is not None and filesystem.kind is FileSystemSandboxKind.RESTRICTED:
            extra = additional.file_system
            filesystem = FileSystemSandboxPolicy.restricted(tuple(dict.fromkeys((*filesystem.entries, *extra.entries))),
                glob_scan_max_depth=merge_glob_scan_max_depth(filesystem.entries, filesystem.glob_scan_max_depth,
                    extra.entries, extra.glob_scan_max_depth))
        network = (NetworkSandboxPolicy.ENABLED if additional.network is not None and additional.network.enabled
            else profile.network)
        return replace(self, permission_profile=PermissionProfile.managed(filesystem, network=network))

    def has_denied_read_restrictions(self) -> bool:
        filesystem = self.permission_profile.file_system
        windows = ExecutorPath.parse(self.cwd_uri).convention == "windows"
        return filesystem.kind is FileSystemSandboxKind.RESTRICTED and any(
            entry.access is FileSystemAccessMode.DENY and not (windows and entry.path.kind == "special"
                and entry.path.value is FileSystemSpecialPath.SLASH_TMP) for entry in filesystem.entries)

    def has_full_disk_write_access(self) -> bool:
        profile = self.permission_profile
        if profile.enforcement is not SandboxEnforcement.MANAGED or profile.file_system.kind is FileSystemSandboxKind.UNRESTRICTED:
            return True
        entries = profile.file_system.entries
        cwd = ExecutorPath.parse(self.cwd_uri)
        if cwd.convention is None:
            return False

        def same_target(left: FileSystemPath, right: FileSystemPath) -> bool:
            if left.kind == right.kind == "path":
                return ExecutorPath.parse(str(left.value)) == ExecutorPath.parse(str(right.value))
            if left.kind == right.kind:
                return left == right
            if "glob_pattern" in {left.kind, right.kind}:
                return False
            path, special = (left, right) if left.kind == "path" else (right, left)
            target = ExecutorPath.parse(str(path.value))
            if target.convention != cwd.convention:
                return False
            return ((special.value is FileSystemSpecialPath.ROOT and target.lexical_depth == 0)
                or (special.value is FileSystemSpecialPath.SLASH_TMP and target.convention == "posix"
                    and target.lexical_depth == 1 and target.name == "tmp"))

        def narrows(entry: FileSystemSandboxEntry) -> bool:
            if entry.access is FileSystemAccessMode.WRITE:
                return False
            path = entry.path
            if path.kind == "special":
                if path.value is FileSystemSpecialPath.ROOT:
                    return entry.access is FileSystemAccessMode.DENY
                if (path.value is FileSystemSpecialPath.MINIMAL or not isinstance(path.value, FileSystemSpecialPath)
                        or cwd.convention == "windows" and path.value is FileSystemSpecialPath.SLASH_TMP):
                    return False
            if path.kind == "glob_pattern" or entry.access is FileSystemAccessMode.DENY:
                return True
            return not any(candidate.access is FileSystemAccessMode.WRITE and same_target(path, candidate.path)
                for candidate in entries)

        return any(entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.ROOT
            and entry.access is FileSystemAccessMode.WRITE for entry in entries) and not any(map(narrows, entries))

    def _resolved_entries(self) -> tuple[tuple[ExecutorPath, FileSystemAccessMode], ...]:
        cwd = ExecutorPath.parse(self.cwd_uri, platform_os=self.platform_os)
        root = cwd
        while root.parent != root:
            root = root.parent
        resolved = []
        for entry in self.permission_profile.file_system.entries:
            selector = entry.path
            if selector.kind == "path":
                paths = (ExecutorPath.parse(str(selector.value), platform_os=self.platform_os),)
            elif selector.kind == "glob_pattern":
                continue
            elif selector.value is FileSystemSpecialPath.ROOT:
                paths = (root,)
            elif selector.value is FileSystemSpecialPath.PROJECT_ROOTS:
                paths = tuple(ExecutorPath.parse(path, platform_os=self.platform_os) for path in self.workspace_root_uris)
                if selector.subpath is not None:
                    paths = tuple(path.joinpath(selector.subpath) for path in paths)
            elif selector.value is FileSystemSpecialPath.TMPDIR:
                paths = tuple(ExecutorPath.parse(path, platform_os=self.platform_os)
                    for path in self.temporary_directory_uris or ())
            elif selector.value is FileSystemSpecialPath.SLASH_TMP and cwd.convention == "posix":
                paths = (cwd.joinpath("/tmp"),)
            else:
                continue
            resolved.extend((path, entry.access) for path in paths if path.convention == cwd.convention)
        return tuple(resolved)

    def instruction_paths(self):
        entries = self._resolved_entries()
        writable = tuple(dict.fromkeys(path.native for path, access in entries if access is FileSystemAccessMode.WRITE))
        denied = tuple(dict.fromkeys(path.native for path, access in entries if access is FileSystemAccessMode.DENY))
        globs = tuple(sorted({str(entry.path.value) for entry in self.permission_profile.file_system.entries
            if entry.path.kind == "glob_pattern"}))
        return writable, denied, globs

    def resolve_access(self, value: str) -> FileSystemAccessMode:
        profile = self.permission_profile
        if profile.enforcement is not SandboxEnforcement.MANAGED or profile.file_system.kind is FileSystemSandboxKind.UNRESTRICTED:
            return FileSystemAccessMode.WRITE
        path, cwd = self.resolve_path(value), ExecutorPath.parse(self.cwd_uri)
        if path.convention != cwd.convention or path.lexical_depth is None or cwd.lexical_depth is None:
            return FileSystemAccessMode.DENY
        from backend.sandbox.permission_glob import permission_glob_matches

        resolved = self._resolved_entries()
        if any(access is FileSystemAccessMode.DENY and root.lexical_depth is not None and root.lexical_depth > 0
                and path.is_relative_to(root) for root, access in resolved):
            return FileSystemAccessMode.DENY
        for entry in profile.file_system.entries:
            if entry.path.kind == "glob_pattern":
                pattern = _grant_native(self.resolve_path(str(entry.path.value)))
                try:
                    denied = permission_glob_matches(pattern, _grant_native(path), windows=cwd.convention == "windows")
                except ValueError:
                    denied = True  # Source InvalidDenyReadGlobBehavior::FailClosed.
                if denied:
                    return FileSystemAccessMode.DENY
        ranks = {FileSystemAccessMode.READ: 0, FileSystemAccessMode.WRITE: 1, FileSystemAccessMode.DENY: 2}
        matches = [(root.lexical_depth, ranks[access], access) for root, access in resolved
            if root.lexical_depth is not None and path.is_relative_to(root)]
        return max(matches, default=(-1, -1, FileSystemAccessMode.DENY))[-1]

    def has_full_disk_read_access(self) -> bool:
        profile = self.permission_profile
        if profile.enforcement is not SandboxEnforcement.MANAGED or profile.file_system.kind is FileSystemSandboxKind.UNRESTRICTED:
            return True
        entries = profile.file_system.entries
        root_read = any(entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.ROOT
            and entry.access.can_read for entry in entries)
        return root_read and not self.has_denied_read_restrictions()

    def sandbox_context(self, *, permission_profile: PermissionProfile | None = None) -> dict[str, Any]:
        # Disabled remains an explicit Source permission profile. An omitted
        # Python sandbox argument must never accidentally grant a bypass.
        selected_profile = permission_profile or self.permission_profile
        convention = ExecutorPath.parse(self.cwd_uri).convention
        legacy = any(entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.PROJECT_ROOTS
            or entry.path.kind == "glob_pattern" and not (
                bool(re.match(r"[A-Za-z]:[\\/]|[\\/]{2}", str(entry.path.value))) if convention == "windows"
                else str(entry.path.value).startswith("/")) for entry in selected_profile.file_system.entries)
        context = {
            "permissions": executor_permission_profile(selected_profile),
            "policyContext": {"cwd": file_uri(self.cwd_uri),
                "workspaceRoots": [file_uri(root) for root in self.workspace_root_uris]},
            "windowsSandboxLevel": self.windows_sandbox_level,
            "useLegacyLandlock": self.use_legacy_landlock,
        }
        if legacy:
            context["cwd"] = file_uri(self.cwd_uri)
            context["workspaceRoots"] = [file_uri(root) for root in self.workspace_root_uris]
        if self.user_home_uri is not None:
            context["userHomeDir"] = file_uri(self.user_home_uri)
        if self.temporary_directory_uris is not None:
            context["temporaryDirectories"] = [file_uri(path) for path in self.temporary_directory_uris]
        return context

    def identity(self) -> dict[str, Any]:
        return {"environment_id": self.environment_id, "platform_os": self.platform_os,
            "sandbox": deepcopy(self.sandbox_context())}
