"""
模糊文件搜索引擎（MiniCode 实现）。

特性：
- 评分算法：边界匹配、CamelCase、连续字符
- 字符位图预过滤
- Top-k 结果
- 测试文件惩罚
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from pathspec.gitignore import GitIgnoreSpec

from backend.security.sensitive_files import is_protected_write_path
from backend.workspace.path_filters import is_windows_reserved_path

logger = logging.getLogger(__name__)

_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
_IndexedFile = tuple[Path, str, frozenset[str]]


def iter_search_paths(
    root: Path,
    *,
    include_hidden: bool = False,
    ignore_dirs: set[str] = _IGNORE_DIRS,
    ignore_rules: Literal["all", "directories", "none"] = "all",
) -> Iterator[tuple[Path, bool]]:
    """Walk searchable files and folders, pruning ignored and linked directories."""
    pending: list[tuple[Path, tuple[tuple[Path, GitIgnoreSpec], ...]]] = [(root, ())]
    while pending:
        directory, ignore_specs = pending.pop()
        ignore_lines = []
        if ignore_rules != "none":
            try:
                ignore_lines = (directory / ".gitignore").read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                pass
        if ignore_lines:
            ignore_specs = (*ignore_specs, (directory, GitIgnoreSpec.from_lines(ignore_lines)))

        with os.scandir(directory) as entries:
            for entry in entries:
                if (not include_hidden and entry.name.startswith(".")) or entry.is_symlink():
                    continue
                path = Path(entry.path)
                if is_windows_reserved_path(path.name):
                    continue
                is_dir = entry.is_dir(follow_symlinks=False)
                if is_dir and (
                    entry.name.casefold() in ignore_dirs
                    or (os.name == "nt" and entry.stat(follow_symlinks=False).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT)
                ):
                    continue

                ignored = False
                for base, spec in ignore_specs:
                    relative = path.relative_to(base).as_posix() + ("/" if is_dir else "")
                    decision = spec.check_file(relative).include
                    if decision is not None:
                        ignored = decision
                if ignored and (is_dir or ignore_rules == "all"):
                    continue
                if is_dir:
                    pending.append((path, ignore_specs))
                    yield path, True
                elif entry.is_file(follow_symlinks=False):
                    yield path, False


@dataclass
class FuzzyMatch:
    """模糊匹配结果"""
    path: Path
    score: float
    matched_indices: list[int]  # 匹配字符的索引位置


class FuzzySearchEngine:
    """
    模糊文件搜索引擎。

    评分算法（自有设计）：
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
        self._generation = 0
        self._file_cache: tuple[int, tuple[_IndexedFile, ...]] = (-1, ())

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
        generation, files = self._file_cache
        if generation != self._generation:
            files = self._refresh_file_cache()

        # The cache stores the normalized path and its character index once.
        # Rebuilding those sets for every query made repeated searches scale
        # with both the tree size and the query count.
        query_chars = set(query_lower)

        # 评分和排序
        matches: list[FuzzyMatch] = []

        for path, path_str, path_chars in files:
            # 快速检查：查询中的所有字符是否都在路径中
            if not query_chars.issubset(path_chars):
                continue
            match = self._score_match(path, query_lower, path_str=path_str)
            if match is not None:
                if self._is_test_file(path):
                    if not include_tests:
                        continue
                    match.score += self.PENALTY_TEST_FILE

                matches.append(match)

        # 按分数降序排序，取 top-k
        matches.sort(key=lambda m: (-m.score, m.path.as_posix()))
        return matches[:max_results]

    def invalidate_cache(self) -> None:
        """使文件缓存失效"""
        self._generation += 1
        logger.debug("File cache invalidated")

    def _refresh_file_cache(self) -> tuple[_IndexedFile, ...]:
        """Publish a complete index; invalidation during a scan remains effective."""
        generation = self._generation
        files: list[_IndexedFile] = []
        for path, is_dir in iter_search_paths(self.workspace_root):
            if not is_dir and not is_protected_write_path(path.relative_to(self.workspace_root)):
                normalized = path.relative_to(self.workspace_root).as_posix()
                files.append((path, normalized, frozenset(normalized.lower())))
        snapshot = tuple(files)
        self._file_cache = (generation, snapshot)
        logger.info("Refreshed file cache: %d files", len(snapshot))
        return snapshot


    def _score_match(
        self,
        path: Path,
        query: str,
        *,
        path_str: str | None = None,
    ) -> Optional[FuzzyMatch]:
        """
        计算匹配分数。

        Args:
            path: 文件路径
            query: 查询字符串（小写）

        Returns:
            匹配结果，如果不匹配则返回 None
        """
        path_str = path_str or path.relative_to(self.workspace_root).as_posix()
        path_lower = path_str.lower()
        original_indices = [index for index, char in enumerate(path_str) for _ in char.lower()]

        # 查找匹配位置
        matched_indices: list[int] = []
        query_idx = 0
        for lower_idx, char in enumerate(path_lower):
            if query[query_idx] == char:
                path_idx = original_indices[lower_idx]
                if not matched_indices or matched_indices[-1] != path_idx:
                    matched_indices.append(path_idx)
                query_idx += 1
            if query_idx == len(query):
                break

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
        relative = path.relative_to(self.workspace_root)
        if any(part.lower() in {"tests", "test", "__tests__"} for part in relative.parts[:-1]):
            return True
        path_str = relative.name.lower()
        test_patterns = [
            "test_",
            "_test.",
            ".test.",
            "spec.",
            ".spec.",
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

    engine = _global_engine
    if engine is None or engine.workspace_root != resolved_root:
        engine = FuzzySearchEngine(resolved_root)
        _global_engine = engine

    return engine


def invalidate_global_fuzzy_search() -> None:
    """使全局搜索引擎缓存失效"""
    global _global_engine

    if _global_engine is not None:
        _global_engine.invalidate_cache()
