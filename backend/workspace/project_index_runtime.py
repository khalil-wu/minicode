from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .models import WorkspaceProjectIndexResponse
from .project_index import CachedProjectIndexFile, ProjectIndexScanCancelled, build_project_index
from .service import WorkspaceService


@dataclass
class _Scan:
    task: asyncio.Task[WorkspaceProjectIndexResponse]
    stop: threading.Event
    include_dependencies: bool
    revision: int
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


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root))


def _is_dependency(path: str) -> bool:
    return "node_modules" in path.split("/")


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

    def invalidate_path(self, root: Path, path: Path) -> None:
        with self._lock:
            entry = self._workspaces.get(_root_key(root))
            if entry is not None:
                entry.revision += 1
                entry.dirty.add(path.relative_to(root).as_posix())

    def invalidate_all(self) -> None:
        with self._lock:
            for entry in self._workspaces.values():
                entry.revision += 1
                entry.files.clear()

    async def _scan(self, entry: _WorkspaceIndex, service: WorkspaceService,
                    include_dependencies: bool, stop: threading.Event,
                    revision: int, cached: dict[str, CachedProjectIndexFile], dirty: frozenset[str]) -> WorkspaceProjectIndexResponse:
        updated: dict[str, CachedProjectIndexFile] = {}
        try:
            result = await asyncio.to_thread(build_project_index, service,
                include_dependencies=include_dependencies, stop=stop, cached_files=cached,
                updated_files=updated, dirty_paths=dirty)
            with self._lock:
                if entry.revision == revision:
                    if include_dependencies:
                        entry.files = updated
                    else:
                        entry.files = {**{path: file for path, file in entry.files.items() if _is_dependency(path)}, **updated}
                    entry.dirty.difference_update(dirty)
            return result
        except ProjectIndexScanCancelled:
            raise asyncio.CancelledError() from None
        finally:
            with self._lock:
                entry.scan = None
                self._forget_idle(entry)

    async def read(self, service: WorkspaceService, *, include_dependencies: bool = True) -> WorkspaceProjectIndexResponse:
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
                        task = asyncio.create_task(self._scan(entry, service, include_dependencies, stop,
                            entry.revision, entry.files.copy(), frozenset(entry.dirty)))
                        scan = _Scan(task, stop, include_dependencies, entry.revision)
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
                if scan.revision < required_revision or (include_dependencies and not scan.include_dependencies):
                    continue
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
