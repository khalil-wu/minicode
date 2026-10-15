"""
文件系统监控服务（基于 watchdog）。

文件变更监控采用 watchdog；防抖和忽略规则按本项目的工作区语义实现：
- 稳定期 500ms 防抖
- 支持忽略模式（.git, node_modules 等）
- 防抖处理避免重复触发
- 异步事件通知
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Set, Optional
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler, FileSystemEvent

from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.fuzzy_search import invalidate_global_fuzzy_search
from backend.config_helpers import DATA_ROOT

if os.name == "nt":
    from watchdog.observers import winapi

    # Windows reports LAST_ACCESS as FileModifiedEvent. Indexing and editor
    # reads must not announce content changes and restart their own scan.
    winapi.WATCHDOG_FILE_NOTIFY_FLAGS &= ~winapi.FILE_NOTIFY_CHANGE_LAST_ACCESS

logger = logging.getLogger(__name__)


class WorkspaceFileWatcher:
    """
    工作区文件监控器。

    特性：
    - 实时监控文件变更（change, create, delete）
    - 500ms 稳定期（防抖）
    - 自动忽略常见目录（.git, node_modules 等）
    - 异步事件回调
    """

    # 默认忽略的目录和文件模式
    DEFAULT_IGNORE_PATTERNS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".DS_Store",
        "*.pyc",
        "*.pyo",
        "*.pyd",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "*.egg-info",
    }

    def __init__(
        self,
        workspace_root: Path,
        on_change: Callable[[Path, str], None],
        ignore_patterns: Optional[Set[str]] = None,
        stability_threshold: float = 0.5,  # 500ms 稳定期
        on_index_invalidated: Callable[[str, int], Coroutine[Any, Any, None]] | None = None,
    ):
        """
        初始化文件监控器。

        Args:
            workspace_root: 工作区根目录
            on_change: 文件变更回调函数 (path, event_type)
            ignore_patterns: 额外的忽略模式
            stability_threshold: 稳定期（秒），等待文件写入完成
        """
        self.workspace_root = workspace_root.resolve()
        self.on_change = on_change
        self.stability_threshold = stability_threshold
        self.on_index_invalidated = on_index_invalidated

        # 合并忽略模式
        self.ignore_patterns = self.DEFAULT_IGNORE_PATTERNS.copy()
        if ignore_patterns:
            self.ignore_patterns.update(ignore_patterns)

        self.observer: Optional[Observer] = None
        self._handler: Optional[FileSystemEventHandler] = None
        self._debounce_tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._closed = False
        self._loop = asyncio.get_running_loop()
        self._unsubscribe_index: Callable[[], None] | None = None
        self._index_tasks: set[asyncio.Task] = set()
        self._dependency_roots: set[Path] = set()
        self._dependency_refresh_task: asyncio.Task | None = None

        logger.info(
            f"Initialized file watcher for {workspace_root} "
            f"(stability: {stability_threshold}s)"
        )

    def _should_ignore(self, path: Path) -> bool:
        """
        检查路径是否应该被忽略。

        Args:
            path: 文件路径

        Returns:
            True 如果应该忽略
        """
        if path.is_relative_to(DATA_ROOT):
            return True
        try:
            rel_path = path.relative_to(self.workspace_root)
        except ValueError:
            # 路径不在工作区内
            return True

        # 检查路径的每个部分
        for part in rel_path.parts:
            if part in self.ignore_patterns:
                return True

            # 检查通配符模式
            for pattern in self.ignore_patterns:
                if "*" in pattern:
                    import fnmatch
                    if fnmatch.fnmatch(part, pattern):
                        return True

        return False

    def _create_handler(self) -> FileSystemEventHandler:
        """创建事件处理器"""

        class Handler(FileSystemEventHandler):
            def __init__(self, watcher: WorkspaceFileWatcher):
                self.watcher = watcher

            def on_any_event(self, event: FileSystemEvent):
                if event.event_type not in ("modified", "created", "deleted", "moved"):
                    return
                if self.watcher._closed:
                    return
                loop = self.watcher._loop
                if loop.is_closed():
                    return

                changes: list[tuple[Path, str]] = []
                source = Path(event.src_path)
                if event.event_type == "moved":
                    changes.append((source, "deleted"))
                    dest_path = getattr(event, "dest_path", "")
                    if dest_path:
                        changes.append((Path(dest_path), "moved"))
                else:
                    changes.append((source, event.event_type))

                for path, event_type in changes:
                    if self.watcher._should_ignore(path):
                        dependency_root = self.watcher._dependency_root(path, event.is_directory)
                        if dependency_root is None:
                            continue
                        coro = self.watcher._debounced_dependency_change(dependency_root)
                    else:
                        coro = self.watcher._debounced_change(path, event_type)
                    try:
                        asyncio.run_coroutine_threadsafe(coro, loop)
                    except RuntimeError:
                        coro.close()

        return Handler(self)

    def _dependency_root(self, path: Path, is_directory: bool) -> Path | None:
        if path.is_relative_to(DATA_ROOT) or not path.is_relative_to(self.workspace_root):
            return None
        parts = path.relative_to(self.workspace_root).parts
        for index, part in enumerate(parts):
            if part.casefold() != "node_modules":
                continue
            from backend.workspace.project_index import _file_kind

            if is_directory or _file_kind(path, dependency=True) is not None:
                return self.workspace_root.joinpath(*parts[:index + 1])
        return None

    async def _debounced_dependency_change(self, root: Path) -> None:
        if self._closed:
            return
        self._dependency_roots.add(root)
        if self._dependency_refresh_task is not None:
            self._dependency_refresh_task.cancel()

        async def notify() -> None:
            await asyncio.sleep(self.stability_threshold)
            from backend.workspace.project_index_runtime import project_index_runtime

            roots, self._dependency_roots = self._dependency_roots, set()
            for dependency_root in roots:
                project_index_runtime.invalidate_all(dependency_root)

        self._dependency_refresh_task = asyncio.create_task(notify())

    def _index_invalidated(self, instance_id: str, revision: int) -> None:
        self._loop.call_soon_threadsafe(self._deliver_index_invalidation, instance_id, revision)

    def _deliver_index_invalidation(self, instance_id: str, revision: int) -> None:
        if self._closed:
            return
        task = asyncio.create_task(self.on_index_invalidated(instance_id, revision))
        self._index_tasks.add(task)
        task.add_done_callback(self._index_tasks.discard)

    async def _debounced_change(self, path: Path, event_type: str):
        """
        防抖处理文件变更。

        等待稳定期后才触发回调，避免文件写入过程中的多次触发。

        Args:
            path: 文件路径
            event_type: 事件类型（modified, created, deleted, moved）
        """
        # A watchdog callback can already be queued when stop() closes this
        # watcher. Check the owner after crossing onto its event loop.
        if self._closed:
            return
        path_str = str(path)

        # 取消之前的任务
        if path_str in self._debounce_tasks:
            self._debounce_tasks[path_str].cancel()

        async def delayed_callback():
            try:
                # 等待稳定期
                await asyncio.sleep(self.stability_threshold)

                # Invalidate derived state, then notify consumers.
                try:
                    from backend.workspace.project_index_runtime import project_index_runtime

                    project_index_runtime.invalidate_path(self.workspace_root, path)
                    # 使文件缓存失效
                    if event_type in ("modified", "deleted"):
                        cache = get_global_file_cache()
                        cache.invalidate(path)

                    # 使模糊搜索缓存失效
                    if event_type in ("created", "deleted", "moved") or (
                        event_type == "modified" and path.name == ".gitignore"
                    ):
                        invalidate_global_fuzzy_search()

                    # Publish the change only after readers can see fresh indexes.
                    if asyncio.iscoroutinefunction(self.on_change):
                        await self.on_change(path, event_type)
                    else:
                        self.on_change(path, event_type)

                    logger.debug(f"File changed: {path} ({event_type})")
                except Exception as e:
                    logger.error(f"Error in file change callback: {e}", exc_info=True)

            finally:
                # 清理任务
                if self._debounce_tasks.get(path_str) is task:
                    self._debounce_tasks.pop(path_str, None)

        # 创建新任务
        task = asyncio.create_task(delayed_callback())
        self._debounce_tasks[path_str] = task

    def start(self):
        """启动文件监控"""
        if self._running:
            logger.warning("File watcher already running")
            return

        if not self.workspace_root.exists():
            logger.error(f"Workspace root does not exist: {self.workspace_root}")
            return

        self._handler = self._create_handler()
        self.observer = Observer()
        self.observer.schedule(
            self._handler,
            str(self.workspace_root),
            recursive=True
        )
        self.observer.start()
        from backend.workspace.project_index_runtime import project_index_runtime

        project_index_runtime.retain_workspace(self.workspace_root)
        if self.on_index_invalidated is not None:
            self._unsubscribe_index = project_index_runtime.subscribe_invalidation(self.workspace_root, self._index_invalidated)
        self._running = True
        self._closed = False

        logger.info(f"File watcher started for {self.workspace_root}")

    def stop(self):
        """停止文件监控"""
        if not self._running:
            return
        self._running = False
        self._closed = True
        from backend.workspace.project_index_runtime import project_index_runtime

        if self._unsubscribe_index is not None:
            self._unsubscribe_index()
            self._unsubscribe_index = None
        project_index_runtime.release_workspace(self.workspace_root)
        if self._dependency_refresh_task is not None:
            self._dependency_refresh_task.cancel()
        self._dependency_roots.clear()
        for task in self._index_tasks:
            task.cancel()
        self._index_tasks.clear()

        # 取消所有防抖任务
        for task in self._debounce_tasks.values():
            task.cancel()
        self._debounce_tasks.clear()

        # 停止观察者
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=2.0)
            self.observer = None

        logger.info("File watcher stopped")

    def is_running(self) -> bool:
        """检查监控器是否正在运行"""
        return self._running

    def add_ignore_pattern(self, pattern: str):
        """添加忽略模式"""
        self.ignore_patterns.add(pattern)

    def remove_ignore_pattern(self, pattern: str):
        """移除忽略模式"""
        self.ignore_patterns.discard(pattern)
