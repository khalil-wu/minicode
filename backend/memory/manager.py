"""
记忆统一管理层（DESIGN.md §2 / §3 / §4）。

对上提供：
  - load_index()
  - read_file() / save_file()
  - remember() / recall() / get_memory()
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.memory.file_memory import FileMemory
from backend.memory.vector_memory import VectorMemory


class MemoryManager:
    """统一封装文件记忆与向量记忆。"""

    def __init__(
        self,
        file_memory: FileMemory | None = None,
        vector_memory: VectorMemory | None = None,
    ) -> None:
        self._file_memory = file_memory or FileMemory()
        self._vector_memory = vector_memory or VectorMemory()

    def load_index(self) -> str:
        """加载 MEMORY.md 轻量索引。"""
        return self._file_memory.get_index()

    def list_memory_files(self) -> list[str]:
        return self._file_memory.list_files()

    def read_file(self, filename: str) -> str | None:
        return self._file_memory.read_file(filename)

    def save_file(self, filename: str, content: str, description: str | None = None) -> bool:
        ok = self._file_memory.save_file(filename, content)
        if ok and description:
            self._file_memory.update_index_entry(filename, description)
        return ok

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        importance: int = 3,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        return self._vector_memory.remember(
            content=content,
            tags=tags,
            importance=importance,
            metadata=metadata,
        )

    def recall(
        self,
        query: str,
        top_k: int = 5,
        min_score: float = 0.7,
    ) -> list[dict[str, Any]]:
        return self._vector_memory.recall(query=query, top_k=top_k, min_score=min_score)

    def get_memory(self, memory_id: str) -> str | None:
        return self._vector_memory.get_memory(memory_id)

    def forget(self, memory_id: str) -> bool:
        return self._vector_memory.forget(memory_id)

    def list_vector_memories(self, tag: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        return self._vector_memory.list_memories(tag=tag, limit=limit)
