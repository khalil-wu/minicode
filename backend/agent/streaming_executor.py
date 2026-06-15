"""
Streaming tool executor — execute tools as they arrive from the LLM stream.

Inspired by Claude Code's StreamingToolExecutor. Instead of waiting for the
entire LLM response to finish before executing tools, this module starts
executing concurrency-safe tools the moment their arguments are fully received.

Design:
  - Concurrency-safe (read-only) tools execute in parallel
  - Non-concurrent (mutating) tools get exclusive access
  - Bash/command errors abort all sibling tools via sibling_abort event
  - Results are buffered and returned in the original order
  - Each tool has its own child abort controller for granular cancellation
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from backend.agent.message import AgentEvent
from backend.llm.base import ToolCallEvent

logger = logging.getLogger(__name__)

# Maximum number of concurrent tool executions
MAX_CONCURRENT_TOOLS = 10


class ToolSlotStatus(Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ToolSlot:
    """Tracks a single tool call through the streaming executor pipeline."""
    index: int                          # original order in the tool call sequence
    tool_call: ToolCallEvent
    status: ToolSlotStatus = ToolSlotStatus.PENDING
    result: AgentEvent | None = None
    task: asyncio.Task[None] | None = None
    is_concurrency_safe: bool = False
    started_at: float = 0.0
    completed_at: float | None = None
    error: Exception | None = None


class StreamingToolExecutor:
    """Execute tools as they stream in from the LLM response.

    Usage::

        executor = StreamingToolExecutor(execute_fn=my_execute_single)
        executor.add_tool_call(tool_call_1, is_concurrency_safe=True)
        executor.add_tool_call(tool_call_2, is_concurrency_safe=False)
        executor.finalize()  # no more tools will be added

        async for event in executor.stream_results():
            yield event
    """

    def __init__(
        self,
        execute_single: Callable[..., AsyncIterator[AgentEvent]],
        is_concurrency_safe_fn: Callable[[ToolCallEvent], bool] | None = None,
        max_concurrent: int = MAX_CONCURRENT_TOOLS,
        abort_event: asyncio.Event | None = None,
    ) -> None:
        self._execute_single = execute_single
        self._is_concurrency_safe_fn = is_concurrency_safe_fn or (lambda tc: False)
        self._max_concurrent = max_concurrent
        self._abort_event = abort_event or asyncio.Event()

        self._slots: list[ToolSlot] = []
        self._finalized = False
        self._has_errored = False

        # Sibling abort: when a Bash/command tool fails, cancel all siblings
        self._sibling_abort = asyncio.Event()

        # Concurrency gate
        self._concurrency_sem = asyncio.Semaphore(max_concurrent)

        # Result ordering
        self._results_queue: asyncio.Queue[int] = asyncio.Queue()
        self._completed_count = 0

    @property
    def slot_count(self) -> int:
        return len(self._slots)

    @property
    def has_errored(self) -> bool:
        return self._has_errored

    def add_tool_call(
        self,
        tool_call: ToolCallEvent,
        is_concurrency_safe: bool | None = None,
    ) -> int:
        """Register a new tool call for execution.

        Returns the slot index.
        """
        if self._finalized:
            raise RuntimeError("Cannot add tool calls after finalize()")

        if is_concurrency_safe is None:
            is_concurrency_safe = self._is_concurrency_safe_fn(tool_call)

        slot = ToolSlot(
            index=len(self._slots),
            tool_call=tool_call,
            is_concurrency_safe=is_concurrency_safe,
        )
        self._slots.append(slot)

        # Start execution immediately if conditions allow
        slot.task = asyncio.create_task(
            self._execute_slot(slot),
            name=f"streaming-tool-{tool_call.name}-{slot.index}",
        )

        logger.debug(
            "[StreamingExecutor] Added tool %s (index=%d, concurrent=%s)",
            tool_call.name, slot.index, is_concurrency_safe,
        )
        return slot.index

    def finalize(self) -> None:
        """Signal that no more tool calls will be added."""
        self._finalized = True

    async def _execute_slot(self, slot: ToolSlot) -> None:
        """Execute a single tool call slot with concurrency control."""
        # Wait if a non-concurrent tool is executing or sibling has errored
        if not slot.is_concurrency_safe:
            # Non-concurrent tools need exclusive access — wait for all executing
            await self._wait_for_all_executing()

        if self._should_abort(slot):
            slot.status = ToolSlotStatus.CANCELLED
            slot.result = self._make_cancelled_event(slot)
            self._completed_count += 1
            await self._results_queue.put(slot.index)
            return

        slot.status = ToolSlotStatus.EXECUTING
        slot.started_at = time.time()

        try:
            if slot.is_concurrency_safe:
                async with self._concurrency_sem:
                    if self._should_abort(slot):
                        slot.status = ToolSlotStatus.CANCELLED
                        slot.result = self._make_cancelled_event(slot)
                        self._completed_count += 1
                        await self._results_queue.put(slot.index)
                        return
                    await self._run_tool(slot)
            else:
                await self._run_tool(slot)

        except asyncio.CancelledError:
            slot.status = ToolSlotStatus.CANCELLED
            slot.result = self._make_cancelled_event(slot)
        except Exception as exc:
            slot.status = ToolSlotStatus.FAILED
            slot.error = exc
            slot.result = self._make_error_event(slot, exc)
            logger.error(
                "[StreamingExecutor] Tool %s (index=%d) failed: %s",
                slot.tool_call.name, slot.index, exc,
            )

        slot.completed_at = time.time()
        self._completed_count += 1
        await self._results_queue.put(slot.index)

    async def _run_tool(self, slot: ToolSlot) -> None:
        """Run the actual tool execution and collect results."""
        collected_events: list[AgentEvent] = []

        try:
            async for event in self._execute_single(slot.tool_call):
                if self._should_abort(slot):
                    break
                collected_events.append(event)
        except Exception as exc:
            # Check if this is a command/shell tool that should abort siblings
            if slot.tool_call.name in {"run_command", "bash", "powershell"}:
                self._has_errored = True
                self._sibling_abort.set()
                logger.warning(
                    "[StreamingExecutor] Command tool %s failed, aborting siblings",
                    slot.tool_call.name,
                )
            raise

        # The last event is typically the tool result
        if collected_events:
            slot.result = collected_events[-1]
            slot.status = ToolSlotStatus.COMPLETED
        else:
            slot.result = self._make_empty_result_event(slot)
            slot.status = ToolSlotStatus.COMPLETED

    def _should_abort(self, slot: ToolSlot) -> bool:
        """Check if this slot should be aborted."""
        if self._abort_event.is_set():
            return True
        if self._sibling_abort.is_set() and slot.status == ToolSlotStatus.PENDING:
            return True
        return False

    async def _wait_for_all_executing(self) -> None:
        """Wait until all currently executing slots are done."""
        for s in self._slots:
            if s.status == ToolSlotStatus.EXECUTING and s.task and not s.task.done():
                try:
                    await s.task
                except (asyncio.CancelledError, Exception):
                    pass

    async def wait_all(self) -> list[ToolSlot]:
        """Wait for all tool slots to complete and return them in order."""
        self.finalize()

        # Wait for all tasks
        tasks = [s.task for s in self._slots if s.task]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        return self._slots

    async def stream_results(self) -> AsyncIterator[AgentEvent]:
        """Stream tool results in the original order as they complete."""
        self.finalize()
        total = len(self._slots)

        yielded = 0
        while yielded < total:
            try:
                idx = await asyncio.wait_for(
                    self._results_queue.get(),
                    timeout=300.0,  # 5 minute safety timeout
                )
            except asyncio.TimeoutError:
                logger.error("[StreamingExecutor] Timeout waiting for tool results")
                break

            slot = self._slots[idx]
            if slot.result is not None:
                yield slot.result
                yielded += 1

    def cancel_all(self) -> None:
        """Cancel all pending and executing tools."""
        self._abort_event.set()
        for slot in self._slots:
            if slot.task and not slot.task.done():
                slot.task.cancel()

    def discard(self) -> None:
        """Discard all results (used when falling back to non-streaming)."""
        self.cancel_all()
        self._slots.clear()

    def _make_cancelled_event(self, slot: ToolSlot) -> AgentEvent:
        return AgentEvent(
            type="tool_result",
            data={
                "tool_call_id": slot.tool_call.id,
                "name": slot.tool_call.name,
                "content": f"Tool call cancelled: {slot.tool_call.name}",
                "is_error": False,
                "status": "cancelled",
            },
        )

    def _make_error_event(self, slot: ToolSlot, exc: Exception) -> AgentEvent:
        return AgentEvent(
            type="tool_result",
            data={
                "tool_call_id": slot.tool_call.id,
                "name": slot.tool_call.name,
                "content": f"Tool execution failed: {exc}",
                "is_error": True,
                "status": "error",
            },
        )

    def _make_empty_result_event(self, slot: ToolSlot) -> AgentEvent:
        return AgentEvent(
            type="tool_result",
            data={
                "tool_call_id": slot.tool_call.id,
                "name": slot.tool_call.name,
                "content": "",
                "is_error": False,
                "status": "completed",
            },
        )


def is_tool_concurrency_safe(
    tool_name: str,
    tool_registry: Any | None = None,
) -> bool:
    """Determine if a tool can execute concurrently with others.

    Read-only tools and idempotent queries are concurrency-safe.
    Mutating tools and commands need exclusive access.
    """
    # MCP tools: check annotations
    if tool_name.startswith("mcp__"):
        if tool_registry:
            tool = tool_registry.get_tool(tool_name)
            if tool and getattr(tool, "read_only", False):
                return True
        return False

    # Known read-only tools
    READ_ONLY_TOOLS = {
        "read_file", "list_files", "grep_files", "glob_files",
        "fuzzy_search", "web_search", "web_fetch",
        "git_status", "git_diff", "git_log",
        "read_memory", "recall_memory",
        "read_artifact", "todo_read",
        "list_mcp_resources", "read_mcp_resource",
        "tool_search", "tool_describe",
        "go_to_definition", "find_references",
    }

    if tool_name in READ_ONLY_TOOLS:
        return True

    # Check tool registry metadata
    if tool_registry:
        tool = tool_registry.get_tool(tool_name)
        if tool:
            try:
                return bool(tool.is_read_only(None))
            except Exception:
                pass
            try:
                return bool(tool.is_concurrency_safe(None))
            except Exception:
                pass

    # Default: not safe (fail closed, like Claude Code)
    return False
