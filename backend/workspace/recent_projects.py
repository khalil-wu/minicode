"""
最近项目记录管理。

提供基于 JSON 文件的最近打开项目持久化存储。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.config import DATA_ROOT

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = DATA_ROOT / "recent_projects.json"
MAX_RECENT_PROJECTS = 20


class RecentProjectPersistenceError(RuntimeError):
    """Raised when an explicit MRU mutation cannot be persisted."""


@dataclass
class RecentProject:
    """最近项目记录。"""
    path: str
    name: str
    project_type: str
    last_opened: float  # Unix timestamp

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "project_type": self.project_type,
            "last_opened": self.last_opened,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RecentProject:
        return RecentProject(
            path=str(data.get("path", "")),
            name=str(data.get("name", "")),
            project_type=str(data.get("project_type", "unknown")),
            last_opened=float(data.get("last_opened", 0)),
        )


class RecentProjectStore:
    """
    基于 JSON 文件的最近项目存储。

    自动清理不存在的路径，按最后打开时间排序。
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._store_path = store_path or DEFAULT_STORE_PATH
        self._projects: list[RecentProject] = []
        self._load()

    def _load(self) -> None:
        """从文件加载。"""
        if not self._store_path.exists():
            self._projects = []
            return

        try:
            raw = self._store_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, list):
                self._projects = [RecentProject.from_dict(item) for item in data if isinstance(item, dict)]
            else:
                self._projects = []
        except Exception as exc:
            logger.warning("Failed to load recent projects: %s", exc)
            self._projects = []

    def _save(self, *, strict: bool = False) -> bool:
        """保存到文件；显式删除操作可要求失败向上传播。"""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            data = [p.to_dict() for p in self._projects]
            atomic_write_text(
                self._store_path,
                json.dumps(data, ensure_ascii=False, indent=2),
            )
        except Exception as exc:
            logger.warning("Failed to save recent projects: %s", exc)
            if strict:
                raise RecentProjectPersistenceError(
                    "Recent workspace metadata could not be saved"
                ) from exc
            return False
        return True

    def add(self, path: str, name: str, project_type: str = "unknown") -> None:
        """记录打开的项目（若已存在则更新时间戳并移到最前）。"""
        normalized = str(Path(path).resolve())
        identity = canonical_file_path_key(normalized)
        with file_mutation_locks([self._store_path]):
            self._load()
            # Remove spelling aliases of the same project (not distinct POSIX
            # paths that differ by case), then insert the latest display path.
            self._projects = [
                project
                for project in self._projects
                if canonical_file_path_key(project.path) != identity
            ]
            self._projects.insert(0, RecentProject(
                path=normalized,
                name=name,
                project_type=project_type,
                last_opened=time.time(),
            ))
            self._projects = self._projects[:MAX_RECENT_PROJECTS]
            self._save()

    def list(self, limit: int = 10, clean: bool = True) -> list[RecentProject]:
        """获取最近项目列表。clean=True 时自动移除不存在的路径。"""
        with file_mutation_locks([self._store_path]):
            self._load()
            if clean:
                before = len(self._projects)
                existing: list[RecentProject] = []
                for project in self._projects:
                    try:
                        if Path(project.path).exists():
                            existing.append(project)
                    except OSError:
                        # Stale recent entries can point at removed worktrees,
                        # disconnected drives, or sandboxes no longer accessible
                        # to this process. Treat them as unavailable instead of
                        # aborting the websocket command without a response.
                        logger.debug("Recent project path is unavailable: %s", project.path)
                self._projects = existing
                if len(self._projects) != before:
                    self._save()

        return self._projects[:limit]

    def remove(self, path: str) -> bool:
        """移除指定路径的记录。"""
        normalized = str(Path(path).resolve())
        identity = canonical_file_path_key(normalized)
        with file_mutation_locks([self._store_path]):
            self._load()
            original = list(self._projects)
            candidate = [
                project
                for project in self._projects
                if canonical_file_path_key(project.path) != identity
            ]
            if len(candidate) != len(original):
                self._projects = candidate
                try:
                    self._save(strict=True)
                except RecentProjectPersistenceError:
                    self._projects = original
                    raise
                return True
            return False

    def clear(self) -> int:
        """清空所有记录并返回被移除的记录数。"""
        with file_mutation_locks([self._store_path]):
            self._load()
            removed = len(self._projects)
            original = list(self._projects)
            self._projects.clear()
            try:
                self._save(strict=True)
            except RecentProjectPersistenceError:
                self._projects = original
                raise
            return removed
