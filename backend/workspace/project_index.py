from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from collections.abc import Callable, Iterator
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import HTTPException

from backend.security import sensitive_files
from backend.security.sensitive_files import is_protected_write_path
from .fuzzy_search import _IGNORE_DIRS, iter_search_paths
from .models import (
    ProjectIndexFileKind,
    WorkspaceProjectIndexFile,
    WorkspaceProjectIndexIssue,
    WorkspaceProjectIndexResponse,
)

if TYPE_CHECKING:
    from .service import WorkspaceService


_SOURCE_SUFFIXES = {".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"}
_DECLARATION_SUFFIXES = (".d.ts", ".d.mts", ".d.cts")
# Published declarations commonly live in dist/build, and pnpm stores their
# real files below .pnpm. The dependency pass reads only types and metadata.
_DEPENDENCY_IGNORED_DIRS = _IGNORE_DIRS - {"node_modules", "dist", "build"}


class ProjectIndexScanCancelled(Exception):
    """The last reader relinquished this workspace scan."""


@dataclass(frozen=True)
class CachedProjectIndexFile:
    version: tuple[int, int, int]
    file: WorkspaceProjectIndexFile


def _file_version(metadata: os.stat_result) -> tuple[int, int, int]:
    return metadata.st_mtime_ns, metadata.st_size, metadata.st_ino


def _file_kind(path: Path, *, dependency: bool) -> ProjectIndexFileKind | None:
    name = path.name.lower()
    if name.endswith(_DECLARATION_SUFFIXES):
        return "declaration"
    if name == "package.json":
        return "package"
    if name in {"tsconfig.json", "jsconfig.json"} or (
        name.startswith(("tsconfig.", "jsconfig.")) and name.endswith(".json")
    ):
        return "config"
    if not dependency and path.suffix.lower() in _SOURCE_SUFFIXES:
        return "source"
    return None


def _dependency_directory(directory: Path, application_roots: tuple[Path, ...]) -> Path | None:
    candidate = directory / "node_modules"
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(metadata.st_mode) or (
        os.name == "nt" and metadata.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    ) or is_protected_write_path(candidate, application_roots=application_roots, resolved_path=candidate):
        return None
    return candidate


def _project_files(
    root: Path,
    *,
    include_dependencies: bool,
    application_roots: tuple[Path, ...],
    check_cancelled: Callable[[], None] | None = None,
) -> Iterator[tuple[Path, ProjectIndexFileKind]]:
    dependency_roots: list[Path] = []
    if include_dependencies:
        dependency_root = _dependency_directory(root, application_roots)
        if dependency_root is not None:
            dependency_roots.append(dependency_root)
    # root is canonical and the shared walker does not traverse links or
    # junctions. These lexical checks prune protected candidates without I/O;
    # read_indexed_file resolves every real target again before opening it.
    for path, is_dir in iter_search_paths(root, include_hidden=True, check_cancelled=check_cancelled):
        if is_dir:
            if include_dependencies and not is_protected_write_path(path, application_roots=application_roots, resolved_path=path):
                dependency_root = _dependency_directory(path, application_roots)
                if dependency_root is not None:
                    dependency_roots.append(dependency_root)
            continue
        kind = _file_kind(path, dependency=False)
        if kind is not None and not is_protected_write_path(path, application_roots=application_roots, resolved_path=path):
            yield path, kind

    for directory in dependency_roots:
        for path, is_dir in iter_search_paths(
            directory, include_hidden=True, ignore_dirs=_DEPENDENCY_IGNORED_DIRS, ignore_rules="none",
            check_cancelled=check_cancelled,
        ):
            if is_dir:
                continue
            kind = _file_kind(path, dependency=True)
            if kind is not None and not is_protected_write_path(path, application_roots=application_roots, resolved_path=path):
                yield path, kind


def build_project_index(
    service: WorkspaceService, *, include_dependencies: bool = True,
    stop: threading.Event | None = None,
    cached_files: dict[str, CachedProjectIndexFile] | None = None,
    updated_files: dict[str, CachedProjectIndexFile] | None = None,
    dirty_paths: frozenset[str] = frozenset(),
) -> WorkspaceProjectIndexResponse:
    root = service.workspace_root_path()
    application_roots = sensitive_files.application_state_roots()
    files: list[WorkspaceProjectIndexFile] = []
    issues: list[WorkspaceProjectIndexIssue] = []
    cached_files = cached_files or {}

    def check_cancelled() -> None:
        if stop is not None and stop.is_set():
            raise ProjectIndexScanCancelled()

    def read_file(entry: tuple[Path, ProjectIndexFileKind]) -> CachedProjectIndexFile | WorkspaceProjectIndexIssue:
        path, kind = entry
        relative = path.relative_to(root).as_posix()
        try:
            check_cancelled()
            cached = cached_files.get(relative)
            if (cached is not None and relative not in dirty_paths
                    and cached.file.size_bytes <= service._max_file_bytes
                    and cached.version == _file_version(path.lstat())):
                return cached
            snapshot, metadata = service._read_indexed_file_snapshot(path, root=root, application_roots=application_roots)
            check_cancelled()
        except HTTPException as exc:
            return WorkspaceProjectIndexIssue(path=relative, status_code=exc.status_code, message=str(exc.detail))
        except OSError as exc:
            status_code = 404 if isinstance(exc, FileNotFoundError) else 403 if isinstance(exc, PermissionError) else 500
            return WorkspaceProjectIndexIssue(path=relative, status_code=status_code, message=str(exc))
        else:
            return CachedProjectIndexFile(_file_version(metadata), WorkspaceProjectIndexFile(**snapshot.model_dump(), kind=kind))

    # Bound submitted work as well as the I/O pool. Executor.map eagerly
    # queued the complete tree, leaving thousands of reads after HTTP abort.
    with ThreadPoolExecutor(max_workers=8, thread_name_prefix="workspace-index") as readers:
        entries = iter(_project_files(root, include_dependencies=include_dependencies,
            application_roots=application_roots, check_cancelled=check_cancelled))
        pending = set()
        exhausted = False
        try:
            while pending or not exhausted:
                check_cancelled()
                while len(pending) < 8 and not exhausted:
                    entry = next(entries, None)
                    if entry is None:
                        exhausted = True
                    else:
                        pending.add(readers.submit(read_file, entry))
                if not pending:
                    break
                completed, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in completed:
                    result = future.result()
                    if isinstance(result, WorkspaceProjectIndexIssue):
                        issues.append(result)
                    else:
                        files.append(result.file)
                        if updated_files is not None:
                            updated_files[result.file.path] = result
        finally:
            for future in pending:
                future.cancel()
            entries.close()
    check_cancelled()
    files.sort(key=lambda file: file.path)
    issues.sort(key=lambda issue: issue.path)
    return WorkspaceProjectIndexResponse(workspace_root=str(root), files=files, complete=not issues, issues=issues)
