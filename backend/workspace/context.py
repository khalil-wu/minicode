"""
项目工作区上下文管理（参考 Claude Code 的 FileStateCache + Project Discovery）。

核心功能：
  - 发现项目结构（.git, package.json, pyproject.toml 等）
  - 加载 CLAUDE.md 项目指令
  - 构建文件索引（支持 .gitignore 过滤）
  - 提供项目元信息给 Agent
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ProjectMetadata:
    """项目元数据"""
    root_path: Path
    project_type: str  # "python" | "node" | "rust" | "unknown"
    name: str
    description: str = ""
    claude_md_content: str = ""  # CLAUDE.md 内容
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
      4. CLAUDE.md 加载
    """

    def __init__(self, root_path: str | Path):
        self.root_path = Path(root_path).resolve()
        self.metadata: ProjectMetadata | None = None
        self.file_index: dict[str, FileIndexEntry] = {}
        self._gitignore_patterns: list[str] = []

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

        # 2. 加载 CLAUDE.md
        claude_md_content = self._load_claude_md()

        # 3. 加载 .gitignore
        self._gitignore_patterns = self._load_gitignore()

        # 先就位 metadata（CLAUDE.md / 项目类型立即可用），文件统计在索引完成后回填
        self.metadata = ProjectMetadata(
            root_path=self.root_path,
            project_type=project_type,
            name=project_name,
            claude_md_content=claude_md_content,
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

    def _load_claude_md(self) -> str:
        """加载 CLAUDE.md 项目指令"""
        claude_md_path = self.root_path / "CLAUDE.md"
        if claude_md_path.exists():
            try:
                content = claude_md_path.read_text(encoding="utf-8")
                logger.info(f"加载 CLAUDE.md: {len(content)} 字符")
                return content
            except Exception as e:
                logger.warning(f"读取 CLAUDE.md 失败: {e}")
        return ""

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

        def scan_filesystem() -> dict[str, FileIndexEntry]:
            results: dict[str, FileIndexEntry] = {}
            default_ignore = {
                ".git", ".svn", ".hg",
                "node_modules", "__pycache__", ".pytest_cache",
                "venv", ".venv", "env",
                "dist", "build", "target",
                ".idea", ".vscode", ".claude",
            }
            patterns = self._gitignore_patterns
            # 无通配符的 gitignore 目录规则可直接用于剪枝
            prunable = {p.rstrip("/").lstrip("/") for p in patterns if "*" not in p and "/" not in p.rstrip("/")}

            def file_ignored(rel_path: str, name: str) -> bool:
                for pattern in patterns:
                    if pattern in rel_path or name == pattern:
                        return True
                return False

            # 在线程池中执行同步的递归扫描；剪枝避免走入 node_modules/.git 等大目录
            for dirpath, dirnames, filenames in os.walk(self.root_path):
                dirnames[:] = [
                    d for d in dirnames
                    if d not in default_ignore and d not in prunable
                ]
                rel_dir = os.path.relpath(dirpath, self.root_path)
                for name in filenames:
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
            return results

        results = await asyncio.to_thread(scan_filesystem)
        self.file_index.update(results)

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

        if self.metadata.claude_md_content:
            lines.extend([
                f"",
                f"## 项目指令 (CLAUDE.md)",
                f"",
                self.metadata.claude_md_content[:2000],
            ])

        # File tree (capped at 50 entries)
        file_list = sorted(self.file_index.keys())[:50]
        if file_list:
            lines.extend(["", "## 文件结构 (前50项)", ""])
            lines.extend(f"  {f}" for f in file_list)
            if len(self.file_index) > 50:
                lines.append(f"  ... 共 {len(self.file_index)} 个文件")

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
            "has_claude_md": bool(self.metadata.claude_md_content),
        }
