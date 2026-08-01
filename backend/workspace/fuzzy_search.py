"""
模糊文件搜索引擎（参考 Claude Code 的 fuzzySearch.ts）。

特性：
- 评分算法：边界匹配、CamelCase、连续字符
- 字符位图预过滤
- Top-k 结果
- 测试文件惩罚
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from pathspec.gitignore import GitIgnoreSpec

from backend.security.sensitive_files import is_sensitive_file
from backend.workspace.path_filters import is_windows_reserved_path

logger = logging.getLogger(__name__)


@dataclass
class FuzzyMatch:
    """模糊匹配结果"""
    path: Path
    score: float
    matched_indices: list[int]  # 匹配字符的索引位置


class FuzzySearchEngine:
    """
    模糊文件搜索引擎。

    参考 Claude Code 的评分算法：
    - 边界匹配（路径分隔符、单词边界）：+10 分
    - CamelCase 匹配：+8 分
    - 连续字符匹配：每个连续字符 +5 分
    - 测试文件惩罚：-20 分
    - 基础匹配：+1 分
    """

    # 评分权重
    SCORE_BOUNDARY = 10
    SCORE_CAMEL_CASE = 8
    SCORE_CONSECUTIVE = 5
    SCORE_BASE = 1
    PENALTY_TEST_FILE = -20

    def __init__(self, workspace_root: Path):
        """
        初始化搜索引擎。

        Args:
            workspace_root: 工作区根目录
        """
        self.workspace_root = workspace_root.resolve()
        self._file_cache: list[Path] = []
        self._cache_valid = False

        logger.info(f"Initialized fuzzy search engine for {workspace_root}")

    def search(
        self,
        query: str,
        max_results: int = 20,
        include_tests: bool = True,
    ) -> list[FuzzyMatch]:
        """
        执行模糊搜索。

        Args:
            query: 搜索查询
            max_results: 最大结果数
            include_tests: 是否包含测试文件

        Returns:
            匹配结果列表（按分数降序）
        """
        if not query:
            return []

        query_lower = query.lower()

        # 刷新文件缓存
        if not self._cache_valid:
            self._refresh_file_cache()

        # 字符位图预过滤
        query_chars = set(query_lower)
        candidates: list[Path] = []

        for path in self._file_cache:
            path_str = str(path.relative_to(self.workspace_root)).lower()

            # 快速检查：查询中的所有字符是否都在路径中
            if query_chars.issubset(set(path_str)):
                candidates.append(path)

        # 评分和排序
        matches: list[FuzzyMatch] = []

        for path in candidates:
            match = self._score_match(path, query_lower)
            if match is not None:
                # 测试文件惩罚
                if not include_tests and self._is_test_file(path):
                    continue

                matches.append(match)

        # 按分数降序排序，取 top-k
        matches.sort(key=lambda m: m.score, reverse=True)
        return matches[:max_results]

    def invalidate_cache(self) -> None:
        """使文件缓存失效"""
        self._cache_valid = False
        logger.debug("File cache invalidated")

    def _refresh_file_cache(self) -> None:
        """刷新文件缓存"""
        self._file_cache.clear()

        # 忽略的目录
        ignore_dirs = {
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
        }
        gitignore_path = self.workspace_root / ".gitignore"
        try:
            gitignore = GitIgnoreSpec.from_lines(
                gitignore_path.read_text(encoding="utf-8").splitlines()
                if gitignore_path.is_file()
                else []
            )
        except OSError:
            logger.warning("Failed to read .gitignore for fuzzy search", exc_info=True)
            gitignore = GitIgnoreSpec.from_lines([])

        try:
            for path in self.workspace_root.rglob("*"):
                # 跳过目录
                if path.is_dir():
                    continue

                # 跳过忽略的目录
                rel_parts = path.relative_to(self.workspace_root).parts
                rel_path = Path(*rel_parts)
                if any(part in ignore_dirs for part in rel_parts):
                    continue

                # 跳过隐藏文件
                if any(part.startswith(".") for part in rel_parts):
                    continue

                if gitignore.match_file(rel_path.as_posix()) or is_sensitive_file(rel_path):
                    continue

                if is_windows_reserved_path(rel_path):
                    continue

                self._file_cache.append(path)

            self._cache_valid = True
            logger.info(f"Refreshed file cache: {len(self._file_cache)} files")

        except Exception as e:
            logger.error(f"Failed to refresh file cache: {e}", exc_info=True)

    def _score_match(self, path: Path, query: str) -> Optional[FuzzyMatch]:
        """
        计算匹配分数。

        Args:
            path: 文件路径
            query: 查询字符串（小写）

        Returns:
            匹配结果，如果不匹配则返回 None
        """
        path_str = str(path.relative_to(self.workspace_root))
        path_lower = path_str.lower()

        # 查找匹配位置
        matched_indices: list[int] = []
        query_idx = 0
        path_idx = 0

        while query_idx < len(query) and path_idx < len(path_lower):
            if query[query_idx] == path_lower[path_idx]:
                matched_indices.append(path_idx)
                query_idx += 1
            path_idx += 1

        # 如果没有匹配所有查询字符，返回 None
        if query_idx < len(query):
            return None

        # 计算分数
        score = 0.0

        for i, idx in enumerate(matched_indices):
            # 基础分数
            score += self.SCORE_BASE

            # 边界匹配（路径分隔符后、单词开头）
            if idx == 0 or path_str[idx - 1] in ("/", "\\", "_", "-", "."):
                score += self.SCORE_BOUNDARY

            # CamelCase 匹配
            elif idx > 0 and path_str[idx].isupper() and path_str[idx - 1].islower():
                score += self.SCORE_CAMEL_CASE

            # 连续字符匹配
            if i > 0 and matched_indices[i] == matched_indices[i - 1] + 1:
                score += self.SCORE_CONSECUTIVE

        # 完全匹配奖励
        if path_lower == query:
            score += 100

        # 文件名匹配奖励（比路径匹配更重要）
        filename_lower = path.name.lower()
        if query in filename_lower:
            score += 50

        return FuzzyMatch(
            path=path,
            score=score,
            matched_indices=matched_indices,
        )

    def _is_test_file(self, path: Path) -> bool:
        """
        检查是否为测试文件。

        Args:
            path: 文件路径

        Returns:
            True 如果是测试文件
        """
        try:
            path_str = path.relative_to(self.workspace_root).as_posix().lower()
        except ValueError:
            path_str = path.as_posix().lower()
        test_patterns = [
            "test_",
            "_test.",
            ".test.",
            "spec.",
            ".spec.",
            "/tests/",
            "/test/",
            "__tests__",
        ]
        return any(pattern in path_str for pattern in test_patterns)


# 全局搜索引擎实例
_global_engine: Optional[FuzzySearchEngine] = None


def get_global_fuzzy_search(workspace_root: Optional[Path] = None) -> FuzzySearchEngine:
    """
    获取全局模糊搜索引擎实例。

    Args:
        workspace_root: 工作区根目录（首次调用时必须提供）

    Returns:
        搜索引擎实例
    """
    global _global_engine

    resolved_root = workspace_root.resolve() if workspace_root is not None else Path.cwd().resolve()

    if (
        _global_engine is None
        or _global_engine.workspace_root != resolved_root
    ):
        _global_engine = FuzzySearchEngine(resolved_root)

    return _global_engine


def invalidate_global_fuzzy_search() -> None:
    """使全局搜索引擎缓存失效"""
    global _global_engine

    if _global_engine is not None:
        _global_engine.invalidate_cache()
