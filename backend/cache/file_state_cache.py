"""
FileStateCache - LRU 文件状态缓存

防止重复读取文件，使用 LRU 策略管理缓存。
- 最多 100 个条目
- 总大小限制 25MB
- mtime 检测文件修改
- 单个文件 > 5MB 不缓存
"""

from __future__ import annotations

import hashlib
import logging
from collections import OrderedDict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class FileState:
    """文件状态快照"""

    __slots__ = ('path', 'content', 'mtime', 'hash', 'size')

    def __init__(self, path: Path, content: str, mtime: float):
        self.path = path
        self.content = content
        self.mtime = mtime
        self.hash = hashlib.sha256(content.encode()).hexdigest()[:16]
        self.size = len(content.encode('utf-8'))


class FileStateCache:
    """文件状态 LRU 缓存

    使用 OrderedDict 实现 LRU，自动驱逐最久未使用的条目。
    """

    def __init__(
        self,
        max_entries: int = 100,
        max_size_mb: int = 25,
        max_file_size_mb: int = 5,
    ):
        self.cache: OrderedDict[str, FileState] = OrderedDict()
        self.max_entries = max_entries
        self.max_size_bytes = max_size_mb * 1024 * 1024
        self.max_file_size_bytes = max_file_size_mb * 1024 * 1024
        self.current_size = 0

        # 统计
        self._hits = 0
        self._misses = 0

    def get(self, path: Path) -> Optional[str]:
        """获取缓存的文件内容

        Args:
            path: 文件路径

        Returns:
            文件内容，或 None（未缓存/已修改/文件不存在）
        """
        key = str(path)

        # 检查缓存
        if key in self.cache:
            cached = self.cache[key]

            # 检查文件是否被修改
            if path.exists():
                current_mtime = path.stat().st_mtime
                if current_mtime == cached.mtime:
                    # 缓存命中！移到末尾（LRU）
                    self.cache.move_to_end(key)
                    self._hits += 1
                    return cached.content

            # 文件已修改，移除缓存
            self._evict(key)

        self._misses += 1
        return None

    def put(self, path: Path, content: str):
        """缓存文件内容

        Args:
            path: 文件路径
            content: 文件内容
        """
        key = str(path)

        # 检查文件大小
        content_size = len(content.encode('utf-8'))
        if content_size > self.max_file_size_bytes:
            logger.debug(f"File too large to cache: {path} ({content_size / 1024 / 1024:.1f}MB)")
            return

        # 获取 mtime
        mtime = path.stat().st_mtime if path.exists() else 0
        state = FileState(path, content, mtime)

        # 驱逐直到有足够空间
        while (
            self.current_size + state.size > self.max_size_bytes
            or len(self.cache) >= self.max_entries
        ):
            if not self.cache:
                break
            self._evict_lru()

        # 添加到缓存
        self.cache[key] = state
        self.current_size += state.size

        logger.debug(
            f"Cached file: {path.name} ({state.size / 1024:.1f}KB), "
            f"total: {len(self.cache)} files, {self.current_size / 1024 / 1024:.1f}MB"
        )

    def invalidate(self, path: Path):
        """使缓存失效

        Args:
            path: 文件路径
        """
        key = str(path)
        if key in self.cache:
            self._evict(key)
            logger.debug(f"Invalidated cache: {path}")

    def invalidate_pattern(self, pattern: str):
        """使匹配模式的所有缓存失效

        Args:
            pattern: Glob 模式（如 "*.py"）
        """
        from fnmatch import fnmatch

        to_evict = [
            key for key in self.cache
            if fnmatch(Path(key).name, pattern)
        ]

        for key in to_evict:
            self._evict(key)

        if to_evict:
            logger.debug(f"Invalidated {len(to_evict)} cached files matching '{pattern}'")

    def clear(self):
        """清空缓存"""
        self.cache.clear()
        self.current_size = 0
        logger.debug("Cache cleared")

    def _evict(self, key: str):
        """驱逐指定条目"""
        if key in self.cache:
            state = self.cache.pop(key)
            self.current_size -= state.size

    def _evict_lru(self):
        """驱逐最久未使用的条目"""
        if self.cache:
            key, state = self.cache.popitem(last=False)  # FIFO = LRU
            self.current_size -= state.size
            logger.debug(f"Evicted LRU: {state.path.name}")

    def get_stats(self) -> dict:
        """获取缓存统计信息"""
        hit_rate = self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0

        return {
            "entries": len(self.cache),
            "size_mb": self.current_size / 1024 / 1024,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": hit_rate,
            "max_entries": self.max_entries,
            "max_size_mb": self.max_size_bytes / 1024 / 1024,
        }


# 全局单例
_global_cache: Optional[FileStateCache] = None


def get_file_cache() -> FileStateCache:
    """获取全局文件缓存实例"""
    global _global_cache
    if _global_cache is None:
        _global_cache = FileStateCache()
    return _global_cache
