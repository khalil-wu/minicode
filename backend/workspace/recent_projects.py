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
        path = data.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ValueError("A recent project requires a non-empty path")
        return RecentProject(
            path=path,
            name=str(data.get("name", "")),
            project_type=str(data.get("project_type", "unknown")),
            last_opened=float(data.get("last_opened", 0)),
        )


class RecentProjectStore:
    """
    基于 JSON 文件的最近项目存储。

    已打开的项目独立于会话保留；再次打开时保持列表位置。
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._store_path = store_path or DEFAULT_STORE_PATH
        self._projects: list[RecentProject] = []
        self._load()

    def _load(self) -> None:
        """从文件加载。"""
        try:
            raw = self._store_path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
                raise ValueError("Recent workspace metadata must contain a list of project records")
            self._projects = [RecentProject.from_dict(item) for item in data]
        except FileNotFoundError:
            self._projects = []
        except (OSError, ValueError, TypeError) as exc:
            raise RecentProjectPersistenceError(
                "Recent workspace metadata could not be read"
            ) from exc

    def _save(self, projects: list[RecentProject]) -> None:
        """Publish the in-memory list only after its atomic save succeeds."""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            data = [p.to_dict() for p in projects]
            atomic_write_text(
                self._store_path,
                json.dumps(data, ensure_ascii=False, indent=2),
            )
        except OSError as exc:
            raise RecentProjectPersistenceError(
                "Recent workspace metadata could not be saved"
            ) from exc
        self._projects = projects

    def add(self, path: str, name: str, project_type: str = "unknown") -> None:
        """记录打开的项目；更新元数据不会改变现有项目的位置。"""
        normalized = str(Path(path).resolve())
        identity = canonical_file_path_key(normalized)
        with file_mutation_locks([self._store_path]):
            self._load()
            existing_index = next((index for index, project in enumerate(self._projects)
                                   if canonical_file_path_key(project.path) == identity), len(self._projects))
            # Canonical aliases share one saved project and its stable position.
            candidate = [
                project
                for project in self._projects
                if canonical_file_path_key(project.path) != identity
            ]
            candidate.insert(existing_index, RecentProject(
                path=normalized,
                name=name,
                project_type=project_type,
                last_opened=time.time(),
            ))
            self._save(candidate)

    def list(self, limit: int | None = None, clean: bool = False) -> list[RecentProject]:
        """读取已保存项目；只有显式清理才移除不可用目录。"""
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
                if len(existing) != before:
                    self._save(existing)

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
                self._save(candidate)
                return True
            return False

    def clear(self) -> int:
        """清空所有记录并返回被移除的记录数。"""
        with file_mutation_locks([self._store_path]):
            self._load()
            removed = len(self._projects)
            self._save([])
            return removed
