from __future__ import annotations

import asyncio
import os
import threading
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING
from dataclasses import dataclass, field
from pathlib import Path

from .models import WorkspaceProjectIndexResponse
from .project_index import CachedProjectIndexFile, ProjectIndexScanCancelled, build_project_index
from .service import WorkspaceService

if TYPE_CHECKING:
    from backend.sandbox import SandboxPolicy


@dataclass
class _Scan:
    task: asyncio.Task[WorkspaceProjectIndexResponse]
    stop: threading.Event
    include_dependencies: bool
    include_dependency_paths: bool
    revision: int
    base_revision: int
    cached_files: dict[str, CachedProjectIndexFile]
    readers: set[asyncio.Task] = field(default_factory=set)


@dataclass
class _WorkspaceIndex:
    root: Path
    owners: int = 0
    readers: int = 0
    revision: int = 0
    files: dict[str, CachedProjectIndexFile] = field(default_factory=dict)
    dirty: set[str] = field(default_factory=set)
    scan: _Scan | None = None
    dependency_paths: tuple[str, ...] = ()
    complete: bool = False
    generation: int = 0
    instance_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    listeners: set[Callable[[str, int], None]] = field(default_factory=set)


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root))


def _is_dependency(path: str) -> bool:
    return any(part.casefold() == "node_modules" for part in path.split("/"))


class ProjectIndexRuntime:
    """One cancellable scan and content snapshot per mounted workspace."""

    def __init__(self) -> None:
        self._workspaces: dict[str, _WorkspaceIndex] = {}
        # Watcher callbacks and synchronous editor/tool writes share this
        # metadata with HTTP consumers. Disk reads never hold the lock.
        self._lock = threading.RLock()

    def retain_workspace(self, root: Path) -> None:
        with self._lock:
            entry = self._workspaces.setdefault(_root_key(root), _WorkspaceIndex(root))
            entry.owners += 1

    def release_workspace(self, root: Path) -> None:
        with self._lock:
            entry = self._workspaces[_root_key(root)]
            entry.owners -= 1
            self._forget_idle(entry)

    def _forget_idle(self, entry: _WorkspaceIndex) -> None:
        if entry.owners == 0 and entry.readers == 0 and entry.scan is None:
            self._workspaces.pop(_root_key(entry.root), None)

    def subscribe_invalidation(self, root: Path, listener: Callable[[str, int], None]) -> Callable[[], None]:
        with self._lock:
            entry = self._workspaces[_root_key(root)]
            entry.listeners.add(listener)

        def unsubscribe() -> None:
            with self._lock:
                entry.listeners.remove(listener)

        return unsubscribe

    @staticmethod
    def _notify_invalidated(entry: _WorkspaceIndex) -> None:
        # Subscribers enqueue onto their mounted owner loop while this lock
        # prevents an unmount from closing the loop before enqueue completes.
        for listener in entry.listeners:
            listener(entry.instance_id, entry.revision)

    def invalidate_path(self, root: Path, path: Path) -> None:
        with self._lock:
            entry = self._workspaces.get(_root_key(root))
            if entry is not None:
                entry.revision += 1
                relative = path.relative_to(root).as_posix()
                entry.dirty.add(relative)
                if _is_dependency(relative):
                    self._notify_invalidated(entry)

    def invalidate_paths(self, paths: tuple[Path, ...]) -> None:
        with self._lock:
            for entry in self._workspaces.values():
                changed = [path.relative_to(entry.root).as_posix() for path in paths if path.is_relative_to(entry.root)]
                if changed:
                    entry.revision += 1
                    entry.dirty.update(changed)
                    if any(_is_dependency(path) for path in changed):
                        self._notify_invalidated(entry)

    def invalidate_all(self, root: Path | None = None, *, mutation_policy: SandboxPolicy | None = None) -> None:
        authority = mutation_policy.resolve() if mutation_policy is not None else None
        with self._lock:
            for entry in self._workspaces.values():
                if authority is not None and not authority.full_disk_write:
                    writable = authority.writable_roots
                    if not any(grant.is_path_writable(entry.root) or
                            (grant.root.is_relative_to(entry.root) and grant.is_path_writable(grant.root))
                            for grant in writable):
                        continue
                    dirty = {path for path in entry.files if authority.resolve_access(entry.root / path).can_write}
                elif authority is not None or root is None:
                    dirty = set(entry.files)
                else:
                    if not (entry.root.is_relative_to(root) or root.is_relative_to(entry.root)):
                        continue
                    dirty = {path for path in entry.files if (entry.root / path).is_relative_to(root)}
                entry.revision += 1
                entry.complete = False
                entry.dirty.update(dirty)
                self._notify_invalidated(entry)

    async def _scan(self, entry: _WorkspaceIndex, service: WorkspaceService,
                    include_dependencies: bool, include_dependency_paths: bool, stop: threading.Event,
                    revision: int, cached: dict[str, CachedProjectIndexFile], dirty: frozenset[str],
                    incremental: bool, dependency_paths: tuple[str, ...], base_revision: int) -> WorkspaceProjectIndexResponse:
        updated: dict[str, CachedProjectIndexFile] = {}
        try:
            result = await asyncio.to_thread(build_project_index, service,
                include_dependencies=include_dependencies, include_dependency_paths=include_dependency_paths, stop=stop, cached_files=cached,
                updated_files=updated, dirty_paths=dirty, incremental=incremental, dependency_paths=dependency_paths)
            with self._lock:
                if entry.revision == revision:
                    entry.generation = base_revision + 1
                    result = result.model_copy(update={"revision": entry.generation, "index_instance_id": entry.instance_id})
                    if include_dependencies:
                        entry.files = updated
                    else:
                        entry.files = {**{path: file for path, file in entry.files.items() if _is_dependency(path)}, **updated}
                    if result.dependency_paths is not None:
                        entry.dependency_paths = tuple(result.dependency_paths)
                    entry.complete = result.complete
                    entry.dirty.difference_update(dirty)
            return result
        except ProjectIndexScanCancelled:
            raise asyncio.CancelledError() from None
        finally:
            with self._lock:
                entry.scan = None
                self._forget_idle(entry)

    async def read(self, service: WorkspaceService, *, include_dependencies: bool = False, include_dependency_paths: bool = False, previous_revision: int | None = None, previous_instance_id: str | None = None) -> WorkspaceProjectIndexResponse:
        root = service.workspace_root_path()
        reader = asyncio.current_task()
        with self._lock:
            entry = self._workspaces.setdefault(_root_key(root), _WorkspaceIndex(root))
            entry.readers += 1
            required_revision = entry.revision
        try:
            while True:
                with self._lock:
                    scan = entry.scan
                    if scan is None:
                        stop = threading.Event()
                        cached = entry.files.copy()
                        task = asyncio.create_task(self._scan(entry, service, include_dependencies, include_dependency_paths, stop,
                            entry.revision, cached, frozenset(entry.dirty),
                            entry.complete and bool(entry.dirty) and not include_dependency_paths and all(path in entry.files and not path.endswith(".gitignore")
                                for path in entry.dirty) and (not include_dependencies or all(path in entry.files for path in entry.dependency_paths)),
                            entry.dependency_paths, entry.generation))
                        scan = _Scan(task, stop, include_dependencies, include_dependency_paths, entry.revision, entry.generation, cached)
                        entry.scan = scan
                    scan.readers.add(reader)
                try:
                    result = await asyncio.shield(scan.task)
                except asyncio.CancelledError:
                    # A new reader can arrive while the previous last reader
                    # is draining cancellation. It starts after that scan exits.
                    if scan.stop.is_set() and scan.task.cancelled() and not reader.cancelling():
                        continue
                    raise
                finally:
                    with self._lock:
                        scan.readers.remove(reader)
                        stop_scan = not scan.readers and not scan.task.done()
                        if stop_scan:
                            scan.stop.set()
                    if stop_scan:
                        # Cancellation is not complete until the bounded file
                        # workers observe it and their thread actually exits.
                        while not scan.task.done():
                            try:
                                await asyncio.shield(scan.task)
                            except asyncio.CancelledError:
                                pass
                if (scan.revision < required_revision or (include_dependencies and not scan.include_dependencies)
                    or (include_dependency_paths and not (scan.include_dependencies or scan.include_dependency_paths))):
                    continue
                if (previous_instance_id == entry.instance_id and previous_revision == scan.base_revision
                        and result.revision is not None and result.complete
                        and not include_dependencies and not include_dependency_paths):
                    source_files = [file for file in result.files if not _is_dependency(file.path)]
                    present = {file.path for file in source_files}
                    changed = [file for file in source_files if scan.cached_files.get(file.path) is None
                        or scan.cached_files[file.path].file != file]
                    removed = sorted(path for path in scan.cached_files if not _is_dependency(path) and path not in present)
                    return result.model_copy(update={"files": changed, "removed_paths": removed, "base_revision": scan.base_revision})
                if not include_dependencies and scan.include_dependencies:
                    files = [file for file in result.files if not _is_dependency(file.path)]
                    issues = [issue for issue in result.issues if not _is_dependency(issue.path)]
                    return result.model_copy(update={"files": files, "issues": issues, "complete": not issues})
                return result
        finally:
            with self._lock:
                entry.readers -= 1
                self._forget_idle(entry)

    async def shutdown(self) -> None:
        with self._lock:
            scans = [entry.scan for entry in self._workspaces.values() if entry.scan is not None]
            for scan in scans:
                for reader in scan.readers:
                    reader.cancel()
                scan.stop.set()
        results = await asyncio.gather(*(scan.task for scan in scans), return_exceptions=True)
        with self._lock:
            for entry in list(self._workspaces.values()):
                entry.files.clear()
                entry.dirty.clear()
                self._forget_idle(entry)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("Workspace project scan shutdown failed", errors)


project_index_runtime = ProjectIndexRuntime()
