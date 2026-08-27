"""Facade for the MiniCode project memory workspace."""

from __future__ import annotations

from backend.memory.file_memory import FileMemory, MemoryResetResult


class MemoryManager:
    def __init__(self, file_memory: FileMemory | None = None) -> None:
        self._file_memory = file_memory or FileMemory()

    def load_context(self) -> str:
        return self._file_memory.get_context()

    def read_file(self, filename: str) -> str | None:
        return self._file_memory.read_file(filename)

    def reset(self) -> MemoryResetResult:
        return self._file_memory.reset()

    def record_citation_usage(self, rollout_ids: list[str]) -> int:
        from backend.memory.job_store import MEMORY_DB_NAME, MemoryJobStore

        store = MemoryJobStore(
            self._file_memory.memory_dir / MEMORY_DB_NAME,
            reset_lock=self._file_memory.reset_lock,
        )
        return store.record_stage1_output_usage(rollout_ids)
