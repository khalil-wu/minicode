"""Session-owned code cells, receipts and volatile JSON values."""
from __future__ import annotations

import json
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.agent.code_execution import CodeCell, CodeExecutionRuntime


@dataclass
class CodeCellReceipt:
    id: str
    status: str
    error: str
    output: list[dict]
    hook_context: list[str]
    discarded_calls: int


class CodeExecutionStore:
    def __init__(self):
        self.values: dict[str, Any] = {}
        self.receipts: dict[str, CodeCellReceipt] = {}
        self._sizes: dict[str, int] = {}
        self._bytes = 0
        self.cells: dict[str, CodeCell] = {}
        self.runtimes: set[CodeExecutionRuntime] = set()
        self.retirements: dict[CodeExecutionRuntime, asyncio.Task] = {}

    def pending_tasks(self, llm: Any = None) -> set[asyncio.Task]:
        tasks = {cell.task for cell in self.cells.values()
                 if cell.task is not None and not cell.task.done() and (llm is None or cell.parent.llm is llm)}
        tasks.update(task for runtime, task in self.retirements.items()
                     if not task.done() and (llm is None or runtime.tool_context.llm is llm))
        for runtime in self.runtimes:
            if llm is None or runtime.tool_context.llm is llm or any(cell.parent.llm is llm for cell in runtime.cells.values()):
                tasks.update(runtime.pending_tasks())
        return tasks

    def live_tool_calls(self) -> dict[str, CodeCell]:
        """Project actual nested call ownership from its original turn."""
        live = {}
        for runtime in tuple(self.runtimes):
            for cell in tuple(runtime.cells.values()):
                for call_id in tuple(cell.pending_tools):
                    task = runtime.tool_context.cleanup_tasks_by_call.get(call_id)
                    if (cell.task is not None and not cell.task.done()) or (task is not None and not task.done()):
                        live[call_id] = cell
            for record in tuple(runtime.state.tool_calls):
                source = record.call_source
                if source.get("kind") != "code_mode":
                    continue
                cell = runtime.cells.get(source.get("cell_id"))
                task = runtime.tool_context.cleanup_tasks_by_call.get(record.tool_call_id)
                if cell is not None and task is not None and not task.done():
                    live[record.tool_call_id] = cell
        return live

    def retire(self, runtime: CodeExecutionRuntime, release: Callable[[], Awaitable[None]]) -> None:
        if runtime in self.retirements:
            return

        async def finish() -> None:
            results = []
            try:
                while pending := runtime.pending_tasks():
                    results.extend(await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True))
            finally:
                await release()
            while pending := runtime.pending_tasks():
                results.extend(await asyncio.gather(*(asyncio.shield(task) for task in pending), return_exceptions=True))
            errors = [result for result in results if isinstance(result, Exception)]
            if errors:
                raise ExceptionGroup("Background code cells did not settle", errors)

        task = asyncio.create_task(finish(), name="code-turn-environments")
        self.retirements[runtime] = task

        def settled(done: asyncio.Task) -> None:
            # Failed retirement stays owned and is surfaced by session close.
            if not done.cancelled() and done.exception() is None:
                self.retirements.pop(runtime)
                self.runtimes.discard(runtime)

        task.add_done_callback(settled)

    async def aclose(self) -> None:
        await asyncio.gather(*(runtime.aclose() for runtime in tuple(self.runtimes)))
        pending = {task for runtime in self.runtimes for task in runtime.pending_tasks()}
        if pending:
            raise RuntimeError(f"Code cells still own {len(pending)} pending tool cleanup task(s)")
        await asyncio.gather(*tuple(self.retirements.values()))
        self.runtimes.clear()

    def put(self, key: str, value: Any) -> None:
        size = len(json.dumps({key: value}, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        total = self._bytes - self._sizes.get(key, 0) + size
        if total > 8 * 1024 * 1024:
            raise ValueError("Code-mode store exceeded 8 MiB; store artifact references instead of complete datasets.")
        self.values[key] = value
        self._sizes[key] = size
        self._bytes = total
