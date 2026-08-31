"""Conversation-scoped workspace metadata and on-demand file discovery."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pathspec.gitignore import GitIgnoreSpec

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
    ".conda", "data", "datasets", "dataset", "models", "checkpoints",
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
_TEXT_FILE_SUFFIXES = {
    ".bash", ".c", ".cfg", ".conf", ".cpp", ".css", ".fish", ".go",
    ".h", ".hpp", ".html", ".ini", ".java", ".js", ".json", ".jsx",
    ".md", ".py", ".rs", ".sass", ".scss", ".sh", ".toml", ".ts",
    ".tsx", ".txt", ".yaml", ".yml", ".zsh",
}


@dataclass
class ProjectMetadata:
    root_path: Path
    project_type: str
    name: str
    description: str = ""
    has_project_instructions: bool = False
    gitignore_patterns: list[str] = field(default_factory=list)
    # Kept in the public protocol for compatibility. Workspace activation no
    # longer performs a full-tree count merely to populate these decorations.
    file_count: int = 0
    total_size: int = 0


@dataclass
class FileIndexEntry:
    path: Path
    relative_path: str
    size: int
    mtime: float
    is_text: bool


class WorkspaceContext:
    """Own project metadata without maintaining a second filesystem index."""

    def __init__(self, root_path: str | Path, *, max_index_files: int = 50_000, **_: Any) -> None:
        self.root_path = Path(root_path).resolve()
        self.metadata: ProjectMetadata | None = None
        self.file_index: dict[str, FileIndexEntry] = {}
        self.max_index_files = max(1, int(max_index_files or 50_000))
        self.index_truncated = False
        self._gitignore_patterns: list[str] = []
        self._gitignore_spec = GitIgnoreSpec.from_lines([])

    async def initialize(self) -> ProjectMetadata:
        if not self.root_path.exists():
            raise ValueError(f"路径不存在: {self.root_path}")
        if not self.root_path.is_dir():
            raise ValueError(f"不是目录: {self.root_path}")
        self._gitignore_patterns = self._load_gitignore()
        self._gitignore_spec = GitIgnoreSpec.from_lines(self._gitignore_patterns)
        self.metadata = ProjectMetadata(
            root_path=self.root_path,
            project_type=self._detect_project_type(),
            name=self.root_path.name,
            has_project_instructions=self._has_project_instructions(),
            gitignore_patterns=list(self._gitignore_patterns),
        )
        await self._build_file_index()
        self.metadata.file_count = len(self.file_index)
        self.metadata.total_size = sum(entry.size for entry in self.file_index.values())
        logger.info(
            "工作区初始化完成: %s (%s)",
            self.metadata.name,
            self.metadata.project_type,
        )
        return self.metadata

    async def _build_file_index(self) -> None:
        def scan() -> tuple[dict[str, FileIndexEntry], bool]:
            result: dict[str, FileIndexEntry] = {}
            truncated = False
            for relative, path in self._iter_visible_files():
                if len(result) >= self.max_index_files:
                    truncated = True
                    break
                stat = path.stat()
                result[relative] = FileIndexEntry(
                    path=path,
                    relative_path=relative,
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                    is_text=path.suffix.lower() in _TEXT_FILE_SUFFIXES,
                )
            return result, truncated
        self.file_index, self.index_truncated = await asyncio.to_thread(scan)

    def _iter_visible_files(self) -> Iterator[tuple[str, Path]]:
        for dirpath, dirnames, filenames in os.walk(self.root_path):
            relative_dir = Path(dirpath).relative_to(self.root_path)
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not self._directory_is_ignored(relative_dir, dirname)
            ]
            for filename in filenames:
                relative = (relative_dir / filename).as_posix()
                if self._file_is_ignored(relative, filename):
                    continue
                yield relative, Path(dirpath) / filename

    def _directory_is_ignored(self, relative_dir: Path, dirname: str) -> bool:
        if dirname in _STRUCTURAL_IGNORED_DIRS:
            return True
        relative = (relative_dir / dirname).as_posix() + "/"
        return self._gitignore_spec.match_file(relative)

    def _file_is_ignored(self, relative: str, filename: str) -> bool:
        return (
            Path(filename).suffix.lower() in _IGNORED_FILE_SUFFIXES
            or self._gitignore_spec.match_file(relative)
        )

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
