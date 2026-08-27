"""
项目工作区上下文管理（MiniCode 实现）。

核心功能：
  - 发现项目结构（.git, package.json, pyproject.toml 等）
  - 加载 MINICODE.md 项目指令
  - 构建文件索引（支持 .gitignore 过滤）
  - 提供项目元信息给 Agent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pathspec.gitignore import GitIgnoreSpec

logger = logging.getLogger(__name__)

DEFAULT_INDEX_MAX_FILES = 50_000

DEFAULT_IGNORED_DIRS = {
    ".git",
    ".svn",
    ".hg",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".nox",
    ".cache",
    ".parcel-cache",
    ".next",
    ".nuxt",
    ".turbo",
    ".vite",
    ".idea",
    ".vscode",
    ".minicode",
    ".minicode",
    ".venv",
    "venv",
    "env",
    ".env",
    "conda",
    ".conda",
    "miniconda",
    "miniconda3",
    "anaconda",
    "anaconda3",
    "site-packages",
    "dist",
    "build",
    "target",
    "out",
    "coverage",
    ".coverage",
    "htmlcov",
    ".ipynb_checkpoints",
    # ML experiment output dirs that can contain thousands of binary artifacts.
    "data",
    "datasets",
    "dataset",
    "models",
    "checkpoints",
    "runs",
    "wandb",
    "mlruns",
    "logs",
    "tmp",
    "temp",
}

DEFAULT_IGNORED_FILE_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pyd",
    ".so",
    ".dll",
    ".dylib",
    ".exe",
    ".bin",
    ".pt",
    ".pth",
    ".onnx",
    ".ckpt",
    ".safetensors",
    ".h5",
    ".npz",
    ".npy",
    ".parquet",
    ".feather",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
    ".rar",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".mp4",
    ".mov",
    ".avi",
}


@dataclass
class ProjectMetadata:
    """项目元数据"""
    root_path: Path
    project_type: str  # "python" | "node" | "rust" | "unknown"
    name: str
    description: str = ""
    has_project_instructions: bool = False
    gitignore_patterns: list[str] = field(default_factory=list)
    file_count: int = 0
    total_size: int = 0  # bytes


@dataclass
class FileIndexEntry:
    """文件索引条目"""
    path: Path
    relative_path: str
    size: int
    mtime: float
    is_text: bool


class WorkspaceContext:
    """
    工作区上下文管理器。

    负责：
      1. 项目发现与分析
      2. 文件索引构建
      3. .gitignore 规则解析
      4. MiniCode 项目指令发现
    """

    def __init__(self, root_path: str | Path, *, max_index_files: int = DEFAULT_INDEX_MAX_FILES):
        self.root_path = Path(root_path).resolve()
        self.metadata: ProjectMetadata | None = None
        self.file_index: dict[str, FileIndexEntry] = {}
        self._gitignore_patterns: list[str] = []
        self._gitignore_spec = GitIgnoreSpec.from_lines([])
        self.max_index_files = max(1, int(max_index_files or DEFAULT_INDEX_MAX_FILES))
        self.index_truncated = False

    async def initialize(self) -> ProjectMetadata:
        """初始化工作区上下文"""
        if not self.root_path.exists():
            raise ValueError(f"路径不存在: {self.root_path}")

        if not self.root_path.is_dir():
            raise ValueError(f"不是目录: {self.root_path}")

        logger.info(f"初始化工作区: {self.root_path}")

        # 1. 发现项目类型
        project_type = self._detect_project_type()
        project_name = self.root_path.name

        # 2. 发现 MiniCode 项目指令。指令内容由 instruction_discovery 统一加载，
        # workspace metadata 只报告是否存在，避免产生第二套 prompt 真相源。
        has_project_instructions = self._has_project_instructions()

        # 3. 加载 .gitignore
        self._gitignore_patterns = self._load_gitignore()
        self._gitignore_spec = GitIgnoreSpec.from_lines(self._gitignore_patterns)

        # 先就位 metadata（指令状态 / 项目类型立即可用），文件统计在索引完成后回填
        self.metadata = ProjectMetadata(
            root_path=self.root_path,
            project_type=project_type,
            name=project_name,
            has_project_instructions=has_project_instructions,
            gitignore_patterns=self._gitignore_patterns,
        )

        # 4. 构建文件索引
        await self._build_file_index()

        # 5. 计算统计信息
        self.metadata.total_size = sum(entry.size for entry in self.file_index.values())
        self.metadata.file_count = len(self.file_index)

        logger.info(
            f"工作区初始化完成: {project_name} ({project_type}), "
            f"{self.metadata.file_count} 文件, {self.metadata.total_size / 1024 / 1024:.2f} MB"
        )

        return self.metadata

    def _detect_project_type(self) -> str:
        """检测项目类型"""
        if (self.root_path / "pyproject.toml").exists() or (self.root_path / "setup.py").exists():
            return "python"
        if (self.root_path / "package.json").exists():
            return "node"
        if (self.root_path / "Cargo.toml").exists():
            return "rust"
        if (self.root_path / "go.mod").exists():
            return "go"
        if (self.root_path / "pom.xml").exists() or (self.root_path / "build.gradle").exists():
            return "java"
        return "unknown"

    def _has_project_instructions(self) -> bool:
        """Return whether the project declares any MiniCode instruction source."""

        config_dir = self.root_path / ".minicode"
        if any(
            (config_dir / name).is_file()
            for name in ("INSTRUCTIONS.md", "INSTRUCTIONS.local.md")
        ):
            return True
        rules_dir = config_dir / "rules"
        try:
            return rules_dir.is_dir() and any(path.is_file() for path in rules_dir.rglob("*.md"))
        except OSError:
            return False

    def _load_gitignore(self) -> list[str]:
        """加载 .gitignore 规则"""
        gitignore_path = self.root_path / ".gitignore"
        if not gitignore_path.exists():
            return []

        try:
            content = gitignore_path.read_text(encoding="utf-8")
            patterns = [
                line.strip()
                for line in content.splitlines()
                if line.strip() and not line.startswith("#")
            ]
            logger.info(f"加载 .gitignore: {len(patterns)} 条规则")
            return patterns
        except Exception as e:
            logger.warning(f"读取 .gitignore 失败: {e}")
            return []

    async def _build_file_index(self) -> None:
        """构建文件索引（尊重 .gitignore，遍历时剪枝忽略目录）"""
        import asyncio
        import os
        self.file_index.clear()

        def scan_filesystem() -> tuple[dict[str, FileIndexEntry], bool]:
            results: dict[str, FileIndexEntry] = {}
            ignore_spec = self._gitignore_spec
            truncated = False

            def file_ignored(rel_path: str, name: str) -> bool:
                if Path(name).suffix.lower() in DEFAULT_IGNORED_FILE_SUFFIXES:
                    return True
                return ignore_spec.match_file(rel_path.replace(os.sep, "/"))

            # 在线程池中执行同步的递归扫描；剪枝避免走入 node_modules/.git 等大目录
            for dirpath, dirnames, filenames in os.walk(self.root_path):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in DEFAULT_IGNORED_DIRS
                ]
                rel_dir = os.path.relpath(dirpath, self.root_path)
                for name in filenames:
                    if len(results) >= self.max_index_files:
                        truncated = True
                        dirnames[:] = []
                        break
                    rel_path = name if rel_dir == "." else f"{rel_dir}{os.sep}{name}"
                    if file_ignored(rel_path, name):
                        continue
                    path = Path(dirpath) / name
                    try:
                        stat = path.stat()
                        results[rel_path] = FileIndexEntry(
                            path=path,
                            relative_path=rel_path,
                            size=stat.st_size,
                            mtime=stat.st_mtime,
                            is_text=self._is_text_file(path),
                        )
                    except Exception as e:
                        logger.debug(f"跳过文件 {path}: {e}")
                if truncated:
                    break
            return results, truncated

        results, truncated = await asyncio.to_thread(scan_filesystem)
        self.index_truncated = truncated
        self.file_index.update(results)
        if truncated:
            logger.warning(
                "工作区文件索引达到上限 %s，已截断: %s",
                self.max_index_files,
                self.root_path,
            )

    def _is_text_file(self, path: Path) -> bool:
        """简单判断是否为文本文件"""
        text_extensions = {
            ".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml",
            ".md", ".txt", ".html", ".css", ".scss", ".sass",
            ".sh", ".bash", ".zsh", ".fish",
            ".c", ".cpp", ".h", ".hpp", ".rs", ".go", ".java",
            ".toml", ".ini", ".cfg", ".conf",
        }
        return path.suffix.lower() in text_extensions

    def get_project_summary(self) -> str:
        """生成项目摘要（用于 system prompt）"""
        if not self.metadata:
            return ""

        lines = [
            f"# 项目上下文",
            f"",
            f"**项目名称**: {self.metadata.name}",
            f"**项目类型**: {self.metadata.project_type}",
            f"**根目录**: {self.metadata.root_path}",
            f"**文件数量**: {self.metadata.file_count}",
            f"**总大小**: {self.metadata.total_size / 1024 / 1024:.2f} MB",
        ]
        if self.index_truncated:
            lines.append(f"**索引状态**: 已截断到前 {self.max_index_files} 个文件")

        # Project instructions are injected by the dedicated context-builder
        # path. File discovery is owned by list_files, so this summary does not
        # carry a second prompt source or a capped file tree.

        return "\n".join(lines)

    def resolve_path(self, path_str: str) -> Path:
        """解析路径（支持相对路径）"""
        path = Path(path_str)
        if path.is_absolute():
            return path
        return (self.root_path / path).resolve()

    def get_file_list(self, pattern: str | None = None, limit: int = 100) -> list[str]:
        """获取文件列表（支持简单模式匹配）"""
        files = list(self.file_index.keys())

        if pattern:
            files = [f for f in files if pattern in f]

        return sorted(files)[:limit]

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典"""
        if not self.metadata:
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
