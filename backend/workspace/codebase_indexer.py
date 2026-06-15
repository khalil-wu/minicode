"""
代码库索引服务（类似 Cursor 的 Codebase 功能）。

特性：
- 索引整个代码库的文件和符号
- 支持 @codebase 引用
- 语义搜索和代码理解
- 自动更新索引
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import hashlib

logger = logging.getLogger(__name__)


@dataclass
class CodeFile:
    """代码文件信息"""
    path: Path
    language: str
    size_bytes: int
    hash: str
    symbols: list[str]  # 函数、类、变量名
    imports: list[str]  # 导入的模块
    content_preview: str  # 前几行预览


@dataclass
class CodebaseIndex:
    """代码库索引"""
    root: Path
    files: dict[str, CodeFile]  # path -> CodeFile
    symbol_map: dict[str, list[str]]  # symbol -> [file_paths]
    total_files: int
    total_size: int
    indexed_at: float


class CodebaseIndexer:
    """
    代码库索引器（类似 Cursor 的 Codebase 功能）。

    功能：
    1. 扫描整个代码库
    2. 提取文件、符号、导入关系
    3. 构建索引用于快速查询
    4. 支持增量更新
    """

    # 支持的语言和扩展名
    LANGUAGE_EXTENSIONS = {
        "python": [".py", ".pyi"],
        "javascript": [".js", ".jsx", ".mjs"],
        "typescript": [".ts", ".tsx"],
        "go": [".go"],
        "rust": [".rs"],
        "java": [".java"],
        "c": [".c", ".h"],
        "cpp": [".cpp", ".hpp", ".cc", ".cxx"],
        "csharp": [".cs"],
        "ruby": [".rb"],
        "php": [".php"],
        "swift": [".swift"],
        "kotlin": [".kt"],
    }

    # 忽略的目录
    IGNORE_DIRS = {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        ".idea",
        ".vscode",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "target",
        "out",
        "bin",
        "obj",
    }

    def __init__(self, workspace_root: Path):
        """
        初始化索引器。

        Args:
            workspace_root: 工作区根目录
        """
        self.workspace_root = workspace_root.resolve()
        self._index: Optional[CodebaseIndex] = None

        logger.info(f"Initialized codebase indexer for {workspace_root}")

    def build_index(self, force: bool = False) -> CodebaseIndex:
        """
        构建代码库索引。

        Args:
            force: 是否强制重建索引

        Returns:
            代码库索引
        """
        if self._index is not None and not force:
            return self._index

        logger.info("Building codebase index...")

        files: dict[str, CodeFile] = {}
        symbol_map: dict[str, list[str]] = {}
        total_size = 0

        # 扫描所有代码文件
        for path in self._scan_files():
            try:
                code_file = self._index_file(path)
                if code_file:
                    rel_path = str(path.relative_to(self.workspace_root))
                    files[rel_path] = code_file
                    total_size += code_file.size_bytes

                    # 构建符号映射
                    for symbol in code_file.symbols:
                        if symbol not in symbol_map:
                            symbol_map[symbol] = []
                        symbol_map[symbol].append(rel_path)

            except Exception as e:
                logger.debug(f"Failed to index {path}: {e}")

        import time
        self._index = CodebaseIndex(
            root=self.workspace_root,
            files=files,
            symbol_map=symbol_map,
            total_files=len(files),
            total_size=total_size,
            indexed_at=time.time(),
        )

        logger.info(
            f"Indexed {len(files)} files "
            f"({total_size / 1024 / 1024:.1f}MB, "
            f"{len(symbol_map)} symbols)"
        )

        return self._index

    def search_symbol(self, symbol: str) -> list[str]:
        """
        搜索符号（函数、类、变量）。

        Args:
            symbol: 符号名

        Returns:
            包含该符号的文件路径列表
        """
        if self._index is None:
            self.build_index()

        return self._index.symbol_map.get(symbol, [])

    def get_file_info(self, path: str) -> Optional[CodeFile]:
        """
        获取文件信息。

        Args:
            path: 相对路径

        Returns:
            文件信息
        """
        if self._index is None:
            self.build_index()

        return self._index.files.get(path)

    def get_related_files(self, path: str, max_results: int = 10) -> list[str]:
        """
        获取相关文件（基于导入关系）。

        Args:
            path: 文件路径
            max_results: 最大结果数

        Returns:
            相关文件路径列表
        """
        if self._index is None:
            self.build_index()

        file_info = self._index.files.get(path)
        if not file_info:
            return []

        related = set()

        # 查找导入的模块对应的文件
        for imp in file_info.imports:
            # 简单的启发式匹配
            for file_path, info in self._index.files.items():
                if imp in file_path or any(imp in sym for sym in info.symbols):
                    related.add(file_path)
                    if len(related) >= max_results:
                        break

        return list(related)[:max_results]

    def invalidate(self) -> None:
        """使索引失效"""
        self._index = None
        logger.debug("Codebase index invalidated")

    def _scan_files(self) -> list[Path]:
        """扫描所有代码文件"""
        files: list[Path] = []

        for path in self.workspace_root.rglob("*"):
            # 跳过目录
            if path.is_dir():
                continue

            # 跳过忽略的目录
            rel_parts = path.relative_to(self.workspace_root).parts
            if any(part in self.IGNORE_DIRS for part in rel_parts):
                continue

            # 跳过隐藏文件
            if any(part.startswith(".") for part in rel_parts):
                continue

            # 检查是否为支持的语言
            if self._get_language(path):
                files.append(path)

        return files

    def _index_file(self, path: Path) -> Optional[CodeFile]:
        """索引单个文件"""
        language = self._get_language(path)
        if not language:
            return None

        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return None

        # 计算文件哈希
        file_hash = hashlib.md5(content.encode()).hexdigest()

        # 提取符号和导入
        symbols = self._extract_symbols(content, language)
        imports = self._extract_imports(content, language)

        # 内容预览（前 5 行）
        lines = content.split("\n")
        preview = "\n".join(lines[:5])

        return CodeFile(
            path=path,
            language=language,
            size_bytes=len(content.encode()),
            hash=file_hash,
            symbols=symbols,
            imports=imports,
            content_preview=preview,
        )

    def _get_language(self, path: Path) -> Optional[str]:
        """获取文件语言"""
        suffix = path.suffix.lower()
        for lang, exts in self.LANGUAGE_EXTENSIONS.items():
            if suffix in exts:
                return lang
        return None

    def _extract_symbols(self, content: str, language: str) -> list[str]:
        """提取符号（简单的正则匹配）"""
        symbols = []

        if language == "python":
            import re
            # 提取函数和类名
            for match in re.finditer(r"^(?:def|class)\s+(\w+)", content, re.MULTILINE):
                symbols.append(match.group(1))

        elif language in ("javascript", "typescript"):
            import re
            # 提取函数、类、const
            for match in re.finditer(
                r"(?:function|class|const|let|var)\s+(\w+)", content
            ):
                symbols.append(match.group(1))

        elif language == "go":
            import re
            # 提取函数和类型
            for match in re.finditer(r"^(?:func|type)\s+(\w+)", content, re.MULTILINE):
                symbols.append(match.group(1))

        return symbols[:100]  # 限制数量

    def _extract_imports(self, content: str, language: str) -> list[str]:
        """提取导入语句"""
        imports = []

        if language == "python":
            import re
            # 提取 import 和 from ... import
            for match in re.finditer(
                r"^(?:import|from)\s+([\w.]+)", content, re.MULTILINE
            ):
                imports.append(match.group(1))

        elif language in ("javascript", "typescript"):
            import re
            # 提取 import ... from
            for match in re.finditer(r"from\s+['\"]([^'\"]+)['\"]", content):
                imports.append(match.group(1))

        elif language == "go":
            import re
            # 提取 import
            for match in re.finditer(r'import\s+"([^"]+)"', content):
                imports.append(match.group(1))

        return imports[:50]  # 限制数量


# 全局索引器实例
_global_indexer: Optional[CodebaseIndexer] = None


def get_global_codebase_indexer(
    workspace_root: Optional[Path] = None,
) -> CodebaseIndexer:
    """
    获取全局代码库索引器实例。

    Args:
        workspace_root: 工作区根目录（首次调用时必须提供）

    Returns:
        索引器实例
    """
    global _global_indexer

    if _global_indexer is None:
        if workspace_root is None:
            workspace_root = Path.cwd()
        _global_indexer = CodebaseIndexer(workspace_root)

    return _global_indexer


def invalidate_global_codebase_index() -> None:
    """使全局代码库索引失效"""
    global _global_indexer

    if _global_indexer is not None:
        _global_indexer.invalidate()
