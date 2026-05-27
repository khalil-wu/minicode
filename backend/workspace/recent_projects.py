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

logger = logging.getLogger(__name__)

DEFAULT_STORE_PATH = Path("data/recent_projects.json")
MAX_RECENT_PROJECTS = 20


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

    def _save(self) -> None:
        """保存到文件。"""
        try:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            data = [p.to_dict() for p in self._projects]
            self._store_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("Failed to save recent projects: %s", exc)

    def add(self, path: str, name: str, project_type: str = "unknown") -> None:
        """记录打开的项目（若已存在则更新时间戳并移到最前）。"""
        normalized = str(Path(path).resolve())

        # 移除已存在的记录
        self._projects = [p for p in self._projects if p.path != normalized]

        # 插入到最前面
        self._projects.insert(0, RecentProject(
            path=normalized,
            name=name,
            project_type=project_type,
            last_opened=time.time(),
        ))

        # 保持上限
        self._projects = self._projects[:MAX_RECENT_PROJECTS]
        self._save()

    def list(self, limit: int = 10, clean: bool = True) -> list[RecentProject]:
        """获取最近项目列表。clean=True 时自动移除不存在的路径。"""
        if clean:
            before = len(self._projects)
            self._projects = [p for p in self._projects if Path(p.path).exists()]
            if len(self._projects) != before:
                self._save()

        return self._projects[:limit]

    def remove(self, path: str) -> bool:
        """移除指定路径的记录。"""
        normalized = str(Path(path).resolve())
        before = len(self._projects)
        self._projects = [p for p in self._projects if p.path != normalized]

        if len(self._projects) != before:
            self._save()
            return True
        return False

    def clear(self) -> None:
        """清空所有记录。"""
        self._projects.clear()
        self._save()
