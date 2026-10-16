"""Source permission-grant intersection over the selected executor's URIs."""
from __future__ import annotations

from dataclasses import replace

from backend.permissions.environment_scope import EnvironmentPermissionScope
from backend.sandbox.policy import (
    AdditionalPermissionProfile, FileSystemAccessMode as Access, FileSystemPath,
    FileSystemPermissions, FileSystemSandboxPolicy, FileSystemSpecialPath as Special,
    NetworkPermissions, PermissionProfile, merge_glob_scan_max_depth,
)
from backend.workspace.executor_paths import ExecutorPath


def _root(cwd):
    while cwd.parent != cwd:
        cwd = cwd.parent
    return cwd


def _path(entry, cwd):
    path = entry.path
    if path.kind == "path":
        return ExecutorPath.parse(str(path.value), platform_os=cwd.platform_os)
    if path.kind == "special" and path.value is Special.ROOT:
        return _root(cwd)
    if path.kind == "special" and path.value is Special.SLASH_TMP and cwd.convention == "posix":
        return cwd.joinpath("/tmp")
    return None


def _materialize(filesystem, cwd, roots, temporary):
    entries = []
    for entry in filesystem.entries:
        selector = entry.path
        if selector.kind == "glob_pattern":
            selector = FileSystemPath.glob(cwd.joinpath(str(selector.value)).native)
        elif selector.kind == "special" and selector.value in {Special.PROJECT_ROOTS, Special.TMPDIR}:
            paths = roots if selector.value is Special.PROJECT_ROOTS else temporary
            if not paths and selector.value is Special.PROJECT_ROOTS and entry.access is Access.DENY:
                raise ValueError("unable to resolve project root deny permission")
            entries.extend(replace(entry, path=FileSystemPath("path",
                (path.joinpath(selector.subpath) if selector.subpath else path).uri)) for path in paths)
            continue
        entries.append(replace(entry, path=selector))
    return replace(filesystem, entries=tuple(dict.fromkeys(entries)))


def _covers(requested, granted):
    return requested.can_write if granted is Access.WRITE else requested.can_read if granted is Access.READ else False


def _constrains(deny, accepted, cwd):
    denied = _path(deny, cwd)
    if deny.path.kind == "glob_pattern":
        # Preserve the exact deny matcher whenever an accepted grant may
        # overlap its static prefix; uncertainty cannot remove a restriction.
        pattern = cwd.joinpath(str(deny.path.value))
        denied = pattern
        while any(char in denied.native for char in "*?["):
            denied = denied.parent
    return any((target := _path(grant, cwd)) is None or denied is None
        or target.is_relative_to(denied) or denied.is_relative_to(target) for grant in accepted)


def intersect_environment_permissions(requested, granted, *, cwd, workspace_roots=(), temporary_directories=None):
    network = NetworkPermissions(True) if (requested.network is not None and requested.network.enabled is True
        and granted.network is not None and granted.network.enabled is True) else None
    if requested.file_system is None or granted.file_system is None:
        return AdditionalPermissionProfile(network=network)
    roots = tuple(path if isinstance(path, ExecutorPath) else ExecutorPath.parse(path) for path in workspace_roots)
    temporary = tuple(path if isinstance(path, ExecutorPath) else ExecutorPath.parse(path) for path in temporary_directories or ())
    req = _materialize(requested.file_system, cwd, roots, temporary)
    grant = _materialize(granted.file_system, cwd, roots, temporary)
    scope = EnvironmentPermissionScope("grant-intersection", PermissionProfile.managed(
        FileSystemSandboxPolicy.restricted(req.entries, glob_scan_max_depth=req.glob_scan_max_depth)),
        cwd.uri, tuple(path.uri for path in roots), cwd.platform_os,
        temporary_directory_uris=tuple(path.uri for path in temporary))
    accepted = []
    for entry in grant.entries:
        if not entry.access.can_read:
            continue
        if cwd.convention == "windows" and entry.path.kind == "special":
            if entry.path.value is Special.SLASH_TMP:
                continue
            if entry.path.value is Special.ROOT and not any(candidate.path == entry.path
                    and _covers(candidate.access, entry.access) for candidate in req.entries):
                continue
        target = _path(entry, cwd)
        within = (_covers(scope.resolve_access(target.uri), entry.access) if target is not None
            else any(candidate.path == entry.path and _covers(candidate.access, entry.access) for candidate in req.entries))
        if within and entry not in accepted:
            accepted.append(entry)
    entries = list(accepted)
    retained = []
    for source in (req, grant):
        denies = tuple(entry for entry in source.entries if entry.access is Access.DENY and _constrains(entry, accepted, cwd))
        entries.extend(entry for entry in denies if entry not in entries)
        retained.append(FileSystemPermissions(denies, source.glob_scan_max_depth))
    depth = merge_glob_scan_max_depth(retained[0].entries, retained[0].glob_scan_max_depth,
        retained[1].entries, retained[1].glob_scan_max_depth)
    filesystem = FileSystemPermissions(tuple(entries), depth) if entries else None
    return AdditionalPermissionProfile(file_system=filesystem, network=network)
