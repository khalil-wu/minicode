"""Conversation-scoped workspace metadata and on-demand file discovery."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend.security.sensitive_files import is_protected_write_path
from backend.workspace.fuzzy_search import iter_search_paths

logger = logging.getLogger(__name__)

_STRUCTURAL_IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".minicode",
    ".mypy_cache",
    ".next",
    ".nox",
    ".nuxt",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    ".conda",
    "runs", "wandb", "mlruns", "logs", "tmp", "temp", ".ipynb_checkpoints",
    "venv", "env", "dist", "build", "target", "out", "coverage", "htmlcov",
}
_IGNORED_FILE_SUFFIXES = {
    ".pyc", ".pyo", ".pyd", ".so", ".dll", ".dylib", ".exe", ".bin",
    ".pt", ".pth", ".onnx", ".ckpt", ".safetensors", ".h5", ".npz", ".npy",
    ".parquet", ".feather", ".sqlite", ".sqlite3", ".db", ".zip", ".tar",
    ".gz", ".7z", ".rar", ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp4", ".mov", ".avi",
}


@dataclass
class ProjectMetadata:
    root_path: Path
    project_type: str
    name: str
    description: str = ""
    has_project_instructions: bool = False
    gitignore_patterns: list[str] = field(default_factory=list)
    # Bounded metadata statistics; fuzzy search owns the reusable file index.
    file_count: int = 0
    total_size: int = 0


class WorkspaceContext:
    """Own project metadata without maintaining a second filesystem index."""

    def __init__(self, root_path: str | Path, *, max_index_files: int = 50_000) -> None:
        self.root_path = Path(root_path).resolve()
        self.metadata: ProjectMetadata | None = None
        self.max_index_files = max(1, int(max_index_files or 50_000))
        self.index_truncated = False
        self._gitignore_patterns: list[str] = []

    async def initialize(self) -> ProjectMetadata:
        if not self.root_path.exists():
            raise ValueError(f"路径不存在: {self.root_path}")
        if not self.root_path.is_dir():
            raise ValueError(f"不是目录: {self.root_path}")
        self._gitignore_patterns = self._load_gitignore()
        self.metadata = ProjectMetadata(
            root_path=self.root_path,
            project_type=self._detect_project_type(),
            name=self.root_path.name,
            has_project_instructions=self._has_project_instructions(),
            gitignore_patterns=list(self._gitignore_patterns),
        )
        await self._collect_file_stats()
        logger.info(
            "工作区初始化完成: %s (%s)",
            self.metadata.name,
            self.metadata.project_type,
        )
        return self.metadata

    async def _collect_file_stats(self) -> None:
        def scan() -> tuple[int, int, bool]:
            count = 0
            total_size = 0
            truncated = False
            for _relative, path in self._iter_visible_files():
                if count >= self.max_index_files:
                    truncated = True
                    break
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                count += 1
                total_size += stat.st_size
            return count, total_size, truncated
        count, size, self.index_truncated = await asyncio.to_thread(scan)
        self.metadata.file_count = count
        self.metadata.total_size = size

    def _iter_visible_files(self) -> Iterator[tuple[str, Path]]:
        for path, is_dir in iter_search_paths(
            self.root_path, include_hidden=True, ignore_dirs=_STRUCTURAL_IGNORED_DIRS,
        ):
            if is_dir or path.suffix.lower() in _IGNORED_FILE_SUFFIXES or is_protected_write_path(path):
                continue
            yield path.relative_to(self.root_path).as_posix(), path

    def _detect_project_type(self) -> str:
        if (self.root_path / "pyproject.toml").exists() or (
            self.root_path / "setup.py"
        ).exists():
            return "python"
        if (self.root_path / "package.json").exists():
            return "node"
        if (self.root_path / "Cargo.toml").exists():
            return "rust"
        if (self.root_path / "go.mod").exists():
            return "go"
        if (self.root_path / "pom.xml").exists() or (
            self.root_path / "build.gradle"
        ).exists():
            return "java"
        return "unknown"

    def _has_project_instructions(self) -> bool:
        if any((self.root_path / name).is_file() for name in ("AGENTS.md", "AGENTS.override.md")):
            return True
        config_dir = self.root_path / ".minicode"
        if any(
            (config_dir / name).is_file()
            for name in ("INSTRUCTIONS.md", "INSTRUCTIONS.local.md")
        ):
            return True
        rules_dir = config_dir / "rules"
        return rules_dir.is_dir() and any(path.is_file() for path in rules_dir.rglob("*.md"))

    def _load_gitignore(self) -> list[str]:
        gitignore_path = self.root_path / ".gitignore"
        if not gitignore_path.is_file():
            return []
        return gitignore_path.read_text(encoding="utf-8").splitlines()

    def get_project_summary(self) -> str:
        if self.metadata is None:
            return ""
        lines = [
            "# 项目上下文",
            "",
            f"**项目名称**: {self.metadata.name}",
            f"**项目类型**: {self.metadata.project_type}",
            f"**根目录**: {self.metadata.root_path}",
        ]
        if self.index_truncated:
            lines.append(f"**索引状态**: 已截断到前 {self.max_index_files} 个文件")
        return "\n".join(lines)

    def resolve_path(self, path_str: str) -> Path:
        path = Path(path_str)
        return path.resolve() if path.is_absolute() else (self.root_path / path).resolve()

    def get_file_list(self, pattern: str | None = None, limit: int = 100) -> list[str]:
        """Discover a bounded list only when a consumer actually asks for it."""

        maximum = max(0, int(limit))
        if maximum == 0:
            return []
        needle = str(pattern or "")
        matches: list[str] = []
        for relative, _path in self._iter_visible_files():
            if needle and needle not in relative:
                continue
            matches.append(relative)
            if len(matches) >= maximum:
                return sorted(matches)
        return sorted(matches)

    def to_dict(self) -> dict[str, Any]:
        if self.metadata is None:
            return {}
        return {
            "root_path": str(self.metadata.root_path),
            "project_type": self.metadata.project_type,
            "name": self.metadata.name,
            "description": self.metadata.description,
            "file_count": self.metadata.file_count,
            "total_size": self.metadata.total_size,
            "has_project_instructions": self.metadata.has_project_instructions,
            "index_truncated": self.index_truncated,
        }
