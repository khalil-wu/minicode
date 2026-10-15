"""Codex request-permission normalization and session/turn grant authority.

The request and the approved subset are separate immutable values. A grant
never substitutes for tool approval, and never travels to another environment.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.sandbox.policy import (
    AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath,
    FileSystemPermissions, FileSystemSandboxEntry, FileSystemSandboxPolicy,
    FileSystemSpecialPath, NetworkPermissions, PermissionProfile,
    SandboxPolicy, resolve_permission_profile, _absolute_path,
    merge_glob_scan_max_depth,
    _glob_matches_path,
)

if TYPE_CHECKING:
    from backend.permissions.context import PermissionContext, ToolExecutionContext


def normalize_permission_profile(
    payload: dict[str, Any], *, cwd: Path, workspace_roots: tuple[Path, ...] = (),
) -> AdditionalPermissionProfile:
    """Resolve the Source legacy path seam, then normalize canonical entries."""
    if set(payload) - {"network", "file_system"}:
        raise ValueError("permission profile has unknown fields")
    raw_network = payload.get("network")
    network = None
    if raw_network is not None:
        if set(raw_network) - {"enabled"}:
            raise ValueError("network permissions have unknown fields")
        enabled = raw_network.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError("network.enabled must be boolean")
        if enabled is not None:
            network = NetworkPermissions(enabled)
    raw_fs = payload.get("file_system")
    file_system = None
    if raw_fs is not None:
        if set(raw_fs) - {"entries", "glob_scan_max_depth", "read", "write"}:
            raise ValueError("filesystem permissions have unknown fields")
        entries: list[FileSystemSandboxEntry] = []
        for access in ("read", "write"):
            for path in raw_fs.get(access) or ():
                entries.append(FileSystemSandboxEntry(
                    FileSystemPath.path(_resolve_legacy_path(path, cwd)),
                    FileSystemAccessMode(access),
                ))
        for raw_entry in raw_fs.get("entries", ()):
            if set(raw_entry) - {"path", "access", "missing_path_behavior"}:
                raise ValueError("filesystem entry has unknown fields")
            raw_path = raw_entry["path"]
            kind = raw_path["type"]
            if kind == "path":
                path = FileSystemPath.path(_resolve_legacy_path(raw_path["path"], cwd))
            elif kind == "glob_pattern":
                path = FileSystemPath.glob(raw_path["pattern"])
            elif kind == "special":
                value = raw_path["value"]
                special_kind = value["kind"]
                if special_kind in {"project_roots", "current_working_directory"}:
                    path = FileSystemPath.special(
                        FileSystemSpecialPath.PROJECT_ROOTS,
                        subpath=value.get("subpath"),
                    )
                elif special_kind == "unknown":
                    path = FileSystemPath("special", value["path"], value.get("subpath"))
                else:
                    path = FileSystemPath.special(FileSystemSpecialPath(special_kind))
            else:
                raise ValueError(f"unknown filesystem path type: {kind}")
            entries.append(FileSystemSandboxEntry(
                path, FileSystemAccessMode("deny" if raw_entry["access"] == "none" else raw_entry["access"]),
                raw_entry.get("missing_path_behavior"),
            ))
        depth = raw_fs.get("glob_scan_max_depth")
        if depth is not None and (not isinstance(depth, int) or isinstance(depth, bool) or depth <= 0):
            raise ValueError("glob_scan_max_depth must be a positive integer")
        if entries:
            file_system = FileSystemPermissions(tuple(dict.fromkeys(entries)), depth)
    return AdditionalPermissionProfile(file_system=file_system, network=network)


def _resolve_legacy_path(path: str, cwd: Path) -> Path:
    candidate = Path(path).expanduser()
    return _absolute_path(candidate if candidate.is_absolute() else cwd / candidate)


def permission_profile_payload(profile: AdditionalPermissionProfile) -> dict[str, Any]:
    result: dict[str, Any] = {"network": None, "file_system": None}
    if profile.network is not None:
        result["network"] = {"enabled": profile.network.enabled}
    fs = profile.file_system
    if fs is not None:
        if fs.glob_scan_max_depth is None and all(
            e.path.kind == "path" and e.access is not FileSystemAccessMode.DENY for e in fs.entries
        ):
            result["file_system"] = {
                access: [str(e.path.value) for e in fs.entries if e.access.value == access]
                for access in ("read", "write") if any(e.access.value == access for e in fs.entries)
            }
        else:
            entries = []
            for entry in fs.entries:
                path: dict[str, Any] = {"type": entry.path.kind}
                if entry.path.kind == "path":
                    path["path"] = str(entry.path.value)
                elif entry.path.kind == "glob_pattern":
                    path["pattern"] = str(entry.path.value)
                else:
                    value = entry.path.value
                    path["value"] = {"kind": value.value} if isinstance(value, FileSystemSpecialPath) else {"kind": "unknown", "path": value}
                    if entry.path.subpath is not None:
                        path["value"]["subpath"] = entry.path.subpath
                item = {"path": path, "access": entry.access.value}
                if entry.missing_path_behavior is not None:
                    item["missing_path_behavior"] = entry.missing_path_behavior
                entries.append(item)
            result["file_system"] = {"entries": entries}
            if fs.glob_scan_max_depth is not None:
                result["file_system"]["glob_scan_max_depth"] = fs.glob_scan_max_depth
    return result


def _materialize_entries(
    fs: FileSystemPermissions, cwd: Path, workspace_roots: tuple[Path, ...],
    temporary_directories: tuple[Path, ...],
) -> tuple[FileSystemSandboxEntry, ...]:
    entries: list[FileSystemSandboxEntry] = []
    for entry in fs.entries:
        path = entry.path
        if path.kind == "glob_pattern":
            path = FileSystemPath.glob(str(_resolve_legacy_path(str(path.value), cwd)))
        elif path.kind == "special" and path.value is FileSystemSpecialPath.PROJECT_ROOTS:
            if not workspace_roots and entry.access is FileSystemAccessMode.DENY:
                raise ValueError("unable to resolve project root deny permission")
            entries.extend(replace(entry, path=FileSystemPath.path(_absolute_path(root / path.subpath if path.subpath else root))) for root in workspace_roots)
            continue
        elif path.kind == "special" and path.value is FileSystemSpecialPath.TMPDIR:
            entries.extend(replace(entry, path=FileSystemPath.path(root)) for root in temporary_directories)
            continue
        entries.append(replace(entry, path=path))
    return tuple(dict.fromkeys(entries))


def local_permission_temporary_directories() -> tuple[Path, ...]:
    """Source EnvironmentInfo: TEMP/TMP on Windows, TMPDIR on POSIX."""
    directories: list[Path] = []
    for name in (("TEMP", "TMP") if os.name == "nt" else ("TMPDIR",)):
        raw = os.environ.get(name)
        if not raw:
            continue
        path = Path(raw)
        if os.name == "nt" and not path.is_absolute():
            continue
        path = _absolute_path(path)
        if path not in directories:
            directories.append(path)
    return tuple(directories)


def _resolved_fs(fs: FileSystemPermissions, cwd: Path, roots: tuple[Path, ...]):
    return resolve_permission_profile(
        PermissionProfile.managed(FileSystemSandboxPolicy.restricted(fs.entries, glob_scan_max_depth=fs.glob_scan_max_depth)),
        cwd=cwd, workspace_roots=roots, protect_workspace_metadata=False,
    )


def _path_for_entry(entry: FileSystemSandboxEntry, cwd: Path) -> Path | None:
    if entry.path.kind == "path":
        return Path(entry.path.value)
    if entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.ROOT:
        return Path(cwd.anchor or os.sep)
    if entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.SLASH_TMP and os.name != "nt":
        return Path("/tmp")
    return None


def _requested_lexical_access(fs: FileSystemPermissions, path: Path, cwd: Path) -> FileSystemAccessMode:
    """Source URI intersection never resolves symlink targets on disk."""
    root = Path(cwd.anchor or os.sep)
    matches: list[tuple[Path, FileSystemAccessMode]] = []
    for entry in fs.entries:
        entry_path = _path_for_entry(entry, cwd)
        if entry.access is FileSystemAccessMode.DENY:
            # Source's ReadDenyMatcher retains every explicit non-root deny,
            # independently of a more specific write/read entry below it.
            if entry.path.kind == "glob_pattern" and _glob_matches_path(str(entry.path.value), path):
                return FileSystemAccessMode.DENY
            if entry_path is not None and entry_path != root and path.is_relative_to(entry_path):
                return FileSystemAccessMode.DENY
        if entry_path is not None and path.is_relative_to(entry_path):
            matches.append((entry_path, entry.access))
    precedence = {FileSystemAccessMode.READ: 0, FileSystemAccessMode.WRITE: 1, FileSystemAccessMode.DENY: 2}
    return max(matches, default=(root, FileSystemAccessMode.DENY), key=lambda item: (len(item[0].parts), precedence[item[1]]))[1]


def _deny_constrains(deny: FileSystemSandboxEntry, grants: list[FileSystemSandboxEntry], cwd: Path) -> bool:
    deny_path = _path_for_entry(deny, cwd)
    if deny.path.kind == "glob_pattern":
        pattern = str(deny.path.value)
        first = min((pattern.find(token) for token in ("*", "?", "[", "]") if token in pattern), default=len(pattern))
        if first == 0:
            return bool(grants)
        prefix = pattern[:first]
        deny_path = _resolve_legacy_path(prefix, cwd)
        if first < len(pattern) and not prefix.endswith(("/", "\\")):
            deny_path = deny_path.parent
    for grant in grants:
        grant_path = _path_for_entry(grant, cwd)
        if grant_path is None or deny_path is None or grant_path.is_relative_to(deny_path) or deny_path.is_relative_to(grant_path):
            return True
    return False


def _merged_glob_depth(left: FileSystemPermissions | None, right: FileSystemPermissions | None) -> int | None:
    left = left or FileSystemPermissions()
    right = right or FileSystemPermissions()
    return merge_glob_scan_max_depth(left.entries, left.glob_scan_max_depth, right.entries, right.glob_scan_max_depth)


def intersect_permission_profiles(
    requested: AdditionalPermissionProfile, granted: AdditionalPermissionProfile,
    *, cwd: Path, workspace_roots: tuple[Path, ...] = (),
    temporary_directories: tuple[Path, ...] | None = None,
) -> AdditionalPermissionProfile:
    network = NetworkPermissions(True) if (
        requested.network is not None and requested.network.enabled is True
        and granted.network is not None and granted.network.enabled is True
    ) else None
    accepted: list[FileSystemSandboxEntry] = []
    fs = None
    if requested.file_system is not None and granted.file_system is not None:
        temporary_directories = local_permission_temporary_directories() if temporary_directories is None else temporary_directories
        req = replace(requested.file_system, entries=_materialize_entries(requested.file_system, cwd, workspace_roots, temporary_directories))
        grant = replace(granted.file_system, entries=_materialize_entries(granted.file_system, cwd, workspace_roots, temporary_directories))
        for entry in grant.entries:
            if not entry.access.can_read:
                continue
            if entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.SLASH_TMP and os.name == "nt":
                continue
            if os.name == "nt" and entry.path.kind == "special" and entry.path.value is FileSystemSpecialPath.ROOT and not any(e.path == entry.path and _access_covers(e.access, entry.access) for e in req.entries):
                continue
            path = _path_for_entry(entry, cwd)
            within = _access_covers(_requested_lexical_access(req, path, cwd), entry.access) if path is not None else any(e.path == entry.path and _access_covers(e.access, entry.access) for e in req.entries)
            if within and entry not in accepted:
                accepted.append(entry)
        denies: list[FileSystemPermissions] = []
        entries = list(accepted)
        for source in (req, grant):
            retained = tuple(e for e in source.entries if e.access is FileSystemAccessMode.DENY and _deny_constrains(e, accepted, cwd))
            entries.extend(e for e in retained if e not in entries)
            denies.append(FileSystemPermissions(retained, source.glob_scan_max_depth))
        if entries:
            fs = FileSystemPermissions(tuple(entries), _merged_glob_depth(*denies))
    return AdditionalPermissionProfile(file_system=fs, network=network)


def _access_covers(requested: FileSystemAccessMode, granted: FileSystemAccessMode) -> bool:
    return requested.can_write if granted is FileSystemAccessMode.WRITE else requested.can_read if granted is FileSystemAccessMode.READ else False


def merge_permission_profiles(left: AdditionalPermissionProfile | None, right: AdditionalPermissionProfile | None) -> AdditionalPermissionProfile:
    if left is None:
        return right or AdditionalPermissionProfile()
    if right is None:
        return left
    network = NetworkPermissions(True) if any(p.network is not None and p.network.enabled is True for p in (left, right)) else None
    fs = None
    if left.file_system is not None and right.file_system is not None:
        fs = FileSystemPermissions(tuple(dict.fromkeys((*left.file_system.entries, *right.file_system.entries))), _merged_glob_depth(left.file_system, right.file_system))
    else:
        fs = left.file_system or right.file_system
    return AdditionalPermissionProfile(file_system=fs, network=network)


@dataclass(frozen=True)
class PermissionGrantResponse:
    permissions: AdditionalPermissionProfile = field(default_factory=AdditionalPermissionProfile)
    scope: str = "turn"
    strict_auto_review: bool = False

    def payload(self) -> dict[str, Any]:
        result = {"permissions": permission_profile_payload(self.permissions), "scope": self.scope}
        if self.strict_auto_review:
            result["strict_auto_review"] = True
        return result


def normalize_permission_response(requested: AdditionalPermissionProfile, payload: dict[str, Any], *, cwd: Path, workspace_roots: tuple[Path, ...] = (), temporary_directories: tuple[Path, ...] | None = None) -> PermissionGrantResponse:
    scope = payload.get("scope", "turn")
    strict = payload.get("strict_auto_review", False)
    if scope not in {"turn", "session"}:
        raise ValueError("permission grant scope must be turn or session")
    if not isinstance(strict, bool):
        raise ValueError("strict_auto_review must be boolean")
    if strict and scope == "session":
        return PermissionGrantResponse()
    granted = normalize_permission_profile(payload["permissions"], cwd=cwd, workspace_roots=workspace_roots)
    if granted.is_empty:
        return PermissionGrantResponse(granted, scope, strict)
    return PermissionGrantResponse(intersect_permission_profiles(requested, granted, cwd=cwd, workspace_roots=workspace_roots, temporary_directories=temporary_directories), scope, strict)


@dataclass
class PermissionGrantStore:
    _session: dict[str, AdditionalPermissionProfile] = field(default_factory=dict)
    _turns: dict[str, dict[str, AdditionalPermissionProfile]] = field(default_factory=dict)
    _strict_turns: set[str] = field(default_factory=set)

    def begin_turn(self, turn_id: str) -> None:
        self._turns.setdefault(turn_id, {})

    def end_turn(self, turn_id: str) -> None:
        self._turns.pop(turn_id, None)
        self._strict_turns.discard(turn_id)

    def record(self, response: PermissionGrantResponse, environment_id: str, turn_id: str) -> None:
        if response.permissions.is_empty:
            return
        target = self._session if response.scope == "session" else self._turns.setdefault(turn_id, {})
        target[environment_id] = merge_permission_profiles(target.get(environment_id), response.permissions)
        if response.scope == "turn" and response.strict_auto_review:
            self._strict_turns.add(turn_id)

    def granted(self, environment_id: str, turn_id: str) -> AdditionalPermissionProfile:
        return merge_permission_profiles(self._session.get(environment_id), self._turns.get(turn_id, {}).get(environment_id))

    def strict_auto_review_enabled(self, turn_id: str) -> bool:
        return turn_id in self._strict_turns


def current_granted_permissions(context: ToolExecutionContext) -> AdditionalPermissionProfile:
    return context.permission_grants.granted(context.environment_id, context.turn_id)


def sandbox_policy_with_grants(policy: SandboxPolicy, context: ToolExecutionContext | None) -> SandboxPolicy:
    return policy if context is None else policy.with_additional_permissions(current_granted_permissions(context))


def permission_context_with_grants(permission: PermissionContext, context: ToolExecutionContext) -> PermissionContext:
    return replace(permission, additional_permissions=current_granted_permissions(context), strict_auto_review=context.permission_grants.strict_auto_review_enabled(context.turn_id))


def permission_profile_allows_path(profile: AdditionalPermissionProfile | None, path: str | Path, *, cwd: Path, workspace_root: Path | None, write: bool = False) -> bool:
    if profile is None or profile.file_system is None:
        return False
    roots = (workspace_root,) if workspace_root is not None else ()
    access = _resolved_fs(profile.file_system, cwd, roots).resolve_access(_resolve_legacy_path(os.fspath(path), cwd))
    return access.can_write if write else access.can_read
