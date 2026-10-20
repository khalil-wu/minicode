"""
最近项目记录管理。

提供基于 JSON 文件的最近打开项目持久化存储。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, replace
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
    source_folders: list[str] | None = None
    pinned: bool | None = None
    appearance: dict[str, Any] | None = None
    named: bool = False
    managed_workspace_root: str | None = None

    @property
    def execution_root(self) -> str:
        return self.workspace_roots[0]

    @property
    def workspace_roots(self) -> tuple[str, ...]:
        return tuple(self.source_folders) if self.source_folders else (self.managed_workspace_root or self.path,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "name": self.name,
            "project_type": self.project_type,
            "last_opened": self.last_opened,
            **({"source_folders": self.source_folders} if self.source_folders is not None else {}),
            **({"pinned": self.pinned} if self.pinned is not None else {}),
            **({"appearance": self.appearance} if self.appearance is not None else {}),
            **({"named": True} if self.named else {}),
            **({"managed_workspace_root": self.managed_workspace_root} if self.managed_workspace_root is not None else {}),
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
            source_folders=list(data["source_folders"]) if "source_folders" in data else None,
            pinned=data.get("pinned"), appearance=data.get("appearance"), named=data.get("named", False),
            managed_workspace_root=data.get("managed_workspace_root"),
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
                                   if identity in {canonical_file_path_key(folder) for folder in project.workspace_roots}), len(self._projects))
            existing = self._projects[existing_index] if existing_index < len(self._projects) else None
            # Canonical aliases share one saved project and its stable position.
            candidate = [
                project
                for project in self._projects
                if project is not existing
            ]
            candidate.insert(existing_index, replace(existing, last_opened=time.time(), name=existing.name if existing.named else name,
                project_type=existing.project_type if existing.named else project_type) if existing is not None else RecentProject(
                path=normalized,
                name=name,
                project_type=project_type,
                last_opened=time.time(),
            ))
            self._save(candidate)

    def create(self, name: str, source_folders: list[str], appearance: dict[str, Any] | None = None, *, managed_workspace_root: str | None = None) -> RecentProject:
        """Register a named project without opening a chat or changing any files."""
        folders = list(dict.fromkeys(str(Path(folder).resolve()) for folder in source_folders))
        identities = {canonical_file_path_key(folder) for folder in folders}
        with file_mutation_locks([self._store_path]):
            self._load()
            if any(identities.intersection(canonical_file_path_key(folder) for folder in project.workspace_roots) for project in self._projects if project.named):
                raise ValueError("A source folder already belongs to a named project.")
            replaced = [project for project in self._projects if canonical_file_path_key(project.path) not in identities]
            identity = folders[0] if folders else str(Path(managed_workspace_root).resolve())
            project = RecentProject(identity, name, "project", time.time(), folders, True, appearance, True, managed_workspace_root)
            self._save([*replaced, project])
            return project

    def update(self, path: str, updates: dict[str, Any]) -> RecentProject:
        identity = canonical_file_path_key(path)
        with file_mutation_locks([self._store_path]):
            self._load()
            project = next((item for item in self._projects if canonical_file_path_key(item.path) == identity), None)
            if project is None: raise ValueError("Project not found.")
            if "source_folders" in updates:
                updates["source_folders"] = list(dict.fromkeys(str(Path(folder).resolve()) for folder in updates["source_folders"]))
                identities = {canonical_file_path_key(folder) for folder in updates["source_folders"]}
                if any(identities.intersection(canonical_file_path_key(folder) for folder in other.workspace_roots) for other in self._projects if other is not project and other.named):
                    raise ValueError("A source folder already belongs to another named project.")
            if "name" in updates or "source_folders" in updates: updates["named"] = True
            updated = replace(project, **updates)
            absorbed = {canonical_file_path_key(folder) for folder in updated.workspace_roots}
            self._save([updated if item is project else item for item in self._projects
                if item is project or item.named or canonical_file_path_key(item.path) not in absorbed])
            return updated

    def project_for_root(self, path: str) -> RecentProject | None:
        identity = canonical_file_path_key(path)
        return next((project for project in self.list() if identity in {
            canonical_file_path_key(folder) for folder in project.workspace_roots}), None)

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
