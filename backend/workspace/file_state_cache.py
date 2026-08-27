"""
文件状态 LRU 缓存（MiniCode 实现）。

特性：
- LRU 驱逐策略（最近最少使用）
- 容量限制：100 条目，25MB 总大小
- 修改时间跟踪（自动失效）
- 线程安全
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Optional

from backend.atomic_io import canonical_file_path_key

logger = logging.getLogger(__name__)


@dataclass
class FileStateEntry:
    """文件状态缓存条目"""
    path: Path
    content: str
    size_bytes: int
    mtime_ns: int  # 修改时间（纳秒）
    language_hint: str


class FileStateCache:
    """
    文件状态 LRU 缓存。

    自有设计：
    - 最大 100 个条目
    - 最大 25MB 总大小
    - 基于修改时间的自动失效
    - LRU 驱逐策略
    """

    DEFAULT_MAX_ENTRIES = 100
    DEFAULT_MAX_SIZE_BYTES = 25 * 1024 * 1024  # 25MB

    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_size_bytes: int = DEFAULT_MAX_SIZE_BYTES,
    ):
        """
        初始化文件状态缓存。

        Args:
            max_entries: 最大条目数
            max_size_bytes: 最大总大小（字节）
        """
        self.max_entries = max_entries
        self.max_size_bytes = max_size_bytes

        self._cache: OrderedDict[str, FileStateEntry] = OrderedDict()
        self._total_size = 0
        self._lock = Lock()

        logger.info(
            f"Initialized file state cache "
            f"(max_entries={max_entries}, max_size={max_size_bytes / 1024 / 1024:.1f}MB)"
        )

    def get(self, path: Path) -> Optional[FileStateEntry]:
        """
        获取文件状态（如果存在且未过期）。

        Args:
            path: 文件路径

        Returns:
            文件状态条目，如果不存在或已过期则返回 None
        """
        path = path.resolve()
        key = canonical_file_path_key(path)

        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None

            # 检查文件是否被修改
            try:
                stat = path.stat()
                if stat.st_mtime_ns != entry.mtime_ns:
                    # 文件已修改，移除缓存
                    self._remove_entry(key)
                    logger.debug(f"Cache miss (modified): {path}")
                    return None
            except OSError:
                # 文件不存在，移除缓存
                self._remove_entry(key)
                logger.debug(f"Cache miss (deleted): {path}")
                return None

            # 移到末尾（最近使用）
            self._cache.move_to_end(key)
            logger.debug(f"Cache hit: {path}")
            return entry

    def put(self, path: Path, content: str, language_hint: str = "") -> None:
        """
        添加或更新文件状态。

        Args:
            path: 文件路径
            content: 文件内容
            language_hint: 语言提示
        """
        path = path.resolve()
        key = canonical_file_path_key(path)

        try:
            stat = path.stat()
        except OSError as e:
            logger.warning(f"Cannot cache file {path}: {e}")
            return

        size_bytes = len(content.encode("utf-8"))
        entry = FileStateEntry(
            path=path,
            content=content,
            size_bytes=size_bytes,
            mtime_ns=stat.st_mtime_ns,
            language_hint=language_hint,
        )

        with self._lock:
            # 如果已存在，先移除旧条目
            if key in self._cache:
                self._remove_entry(key)

            # 检查是否超过单个文件大小限制（不缓存超大文件）
            if size_bytes > self.max_size_bytes // 4:
                logger.debug(f"File too large to cache: {path} ({size_bytes / 1024 / 1024:.1f}MB)")
                return

            # 驱逐条目直到有足够空间
            while (
                len(self._cache) >= self.max_entries
                or self._total_size + size_bytes > self.max_size_bytes
            ):
                if not self._cache:
                    break
                self._evict_lru()

            # 添加新条目
            self._cache[key] = entry
            self._total_size += size_bytes

            logger.debug(
                f"Cached file: {path} "
                f"({size_bytes / 1024:.1f}KB, total={self._total_size / 1024 / 1024:.1f}MB)"
            )

    def invalidate(self, path: Path) -> None:
        """
        使文件缓存失效。

        Args:
            path: 文件路径
        """
        path = path.resolve()
        key = canonical_file_path_key(path)

        with self._lock:
            if key in self._cache:
                self._remove_entry(key)
                logger.debug(f"Invalidated cache: {path}")

    def clear(self) -> None:
        """清空缓存"""
        with self._lock:
            self._cache.clear()
            self._total_size = 0
            logger.info("File state cache cleared")

    def stats(self) -> dict[str, int | float]:
        """
        获取缓存统计信息。

        Returns:
            统计信息字典
        """
        with self._lock:
            return {
                "entries": len(self._cache),
                "total_size_bytes": self._total_size,
                "total_size_mb": self._total_size / 1024 / 1024,
                "max_entries": self.max_entries,
                "max_size_mb": self.max_size_bytes / 1024 / 1024,
            }

    def _remove_entry(self, key: str) -> None:
        """移除条目（内部方法，需要持有锁）"""
        entry = self._cache.pop(key, None)
        if entry:
            self._total_size -= entry.size_bytes

    def _evict_lru(self) -> None:
        """驱逐最近最少使用的条目（内部方法，需要持有锁）"""
        if not self._cache:
            return

        # OrderedDict 的第一个条目是最旧的
        key, entry = self._cache.popitem(last=False)
        self._total_size -= entry.size_bytes

        logger.debug(
            f"Evicted LRU entry: {entry.path} "
            f"({entry.size_bytes / 1024:.1f}KB)"
        )


# 全局缓存实例
_global_cache: Optional[FileStateCache] = None
_global_cache_lock = Lock()


def get_global_file_cache() -> FileStateCache:
    """获取全局文件状态缓存实例"""
    global _global_cache

    with _global_cache_lock:
        if _global_cache is None:
            _global_cache = FileStateCache()
        return _global_cache


def clear_global_file_cache() -> None:
    """清空全局文件状态缓存"""
    global _global_cache

    with _global_cache_lock:
        if _global_cache is not None:
            _global_cache.clear()
