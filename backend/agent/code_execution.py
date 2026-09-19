"""Code cells compose canonical tools; only selected outputs enter model history."""
from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from dataclasses import dataclass, field, fields, replace
from typing import Any
from uuid import uuid4

from backend.agent.code_execution_vm import CodeVM
from backend.agent.code_execution_store import CodeCellReceipt
from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.agent.nested_tool_events import publish_tool_event
from backend.agent.state import AgentState
from backend.agent.tool_batch_runner import ToolBatchRunner
from backend.agent.turn_kernel import TurnKernel
from backend.async_cleanup import CANCELLATION_DRAIN_TIMEOUT_SECONDS, cancel_and_drain_receipt, to_thread_cancel_safe
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolCallSource, ToolExecutionContext
from backend.tools.base import ToolResult
from backend.tools.registry import ToolRegistry
from backend.tools.toolsets import ACTIVE_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy

CODE_TOOLS = frozenset({"tool_exec", "tool_wait"})


@dataclass
class CodeCell:
    id: str
    parent: ToolExecutionContext
    code: str
    allowed_names: frozenset[str]
    catalog: list[dict]
    registry: ToolRegistry
    policy: ToolsetPolicy
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    replies: asyncio.Queue[dict] = field(default_factory=asyncio.Queue)
    output: list[dict] = field(default_factory=list)
    hook_context: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None
    status: str = "running"
    error: str = ""
    discarded_calls: int = 0


class CodeExecutionRuntime:
    def __init__(self, *, context: ContextBuilder, state: AgentState,
                 tool_context: ToolExecutionContext, permission_checker: PermissionChecker, skill_manager: Any,
                 turn_kernel: TurnKernel):
        self.context = context
        self.state = state
        self.tool_context = tool_context
        self.permission_checker = permission_checker
        self.skill_manager = skill_manager
        self.kernel = turn_kernel
        self.cells: dict[str, CodeCell] = {}
        self._by_call: dict[str, str] = {}
        self.store = tool_context.run_context.code_store
        self._closed = False

    def _policy(self, registry: ToolRegistry, base: ToolsetPolicy, requested=()) -> ToolsetPolicy:
        loaded = {name for name in requested if name in self.state.loaded_deferred_tools and registry.get_tool(name) is not None
                  and base.is_available(registry.get_tool_spec(name))}
        return replace(base, enabled_tools=base.enabled_tools | loaded).with_disabled_tools(self.state.disabled_tools | CODE_TOOLS)

    async def execute(self, code: str, parent: ToolExecutionContext, *, yield_time_ms: int) -> ToolResult:
        if self._closed:
            return ToolResult("This turn's code runtime has closed.", is_error=True, status="cancelled")
        cell_id = self._by_call.get(parent.tool_call_id)
        if cell_id is None:
            if sum(cell.status == "running" for cell in self.cells.values()) >= 4:
                return ToolResult("Four code cells are active. Use tool_wait to finish or stop one before starting another.", is_error=True, status="blocked")
            registry = parent.tool_registry
            base_policy = parent.metadata[ACTIVE_TOOLSET_POLICY_METADATA_KEY]
            policy = self._policy(registry, base_policy)
            schemas = registry.get_schemas(permission_checker=self.permission_checker, permission_context=self.tool_context.permission,
                toolset_policy=replace(policy, include_deferred_directly=True))
            catalog = [{"name": schema["function"]["name"], "description": schema["function"]["description"],
                        "parameters": schema["function"]["parameters"],
                        **({"freeform": schema["_minicode_freeform"]} if "_minicode_freeform" in schema else {})} for schema in schemas]
            names = frozenset(entry["name"] for entry in catalog)
            cell_id = "cell_" + uuid4().hex
            cell = CodeCell(cell_id, parent, code, names, catalog, registry, base_policy)
            self.cells[cell_id] = cell
            self._by_call[parent.tool_call_id] = cell_id
            cell.task = asyncio.create_task(self._run(cell), name=f"code:{cell_id}")
        else:
            cell = self.cells[cell_id]
            if cell.code != code:
                return ToolResult("The original code for this invocation cannot be replaced.", is_error=True, status="failed")
        return await self.wait(cell_id, yield_time_ms=yield_time_ms)

    async def wait(self, cell_id: str, *, yield_time_ms: int, terminate: bool = False) -> ToolResult:
        live_cell = self.cells.get(cell_id)
        cell = live_cell or self.store.receipts.get(cell_id)
        if cell is None:
            previous = [{"tool": record.tool_name, "status": record.status, "output": record.tool_output,
                         "artifact_id": record.artifact_id, "request_digest": record.request_digest}
                        for record in self.state.tool_calls if record.call_source.get("cell_id") == cell_id]
            report = {"cell_id": cell_id, "status": "unavailable", "completed_tools": previous,
                "error": "The live JavaScript cell is unavailable after this runtime ended or restarted. Do not blindly rerun writes; inspect recorded outcomes and current workspace state."}
            return ToolResult(json.dumps(report, ensure_ascii=False), is_error=True, status="failed", error_kind="code_cell_unavailable",
                              runtime_metadata={"code_cell": report})
        if terminate and cell.status == "running":
            cell.cancel.set()
            cell.task.cancel()
            await asyncio.gather(cell.task, return_exceptions=True)
        elif cell.status == "running" and not cell.output:
            waiter = asyncio.create_task(cell.changed.wait())
            try:
                await asyncio.wait({cell.task, waiter}, timeout=yield_time_ms / 1000, return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                await asyncio.gather(waiter, return_exceptions=True)
        if live_cell is not None:
            live_cell.changed.clear()
        # Completed receipts share these buffers with the originating cell;
        # consuming them in either turn must not replay the same output twice.
        output = list(cell.output)
        cell.output.clear()
        contexts = list(cell.hook_context)
        cell.hook_context.clear()
        images = [{"media_type": item["media_type"], "data": item["data"]} for item in output if item["kind"] == "image"]
        audios = [{"media_type": item["media_type"], "data": item["data"]} for item in output if item["kind"] == "audio"]
        text = [item["text"] for item in output if item["kind"] == "text"]
        report = {"cell_id": cell.id, "status": cell.status, "output": text,
                              **({"error": cell.error} if cell.error else {}), "image_count": len(images), "audio_count": len(audios),
                              **({"discarded_unawaited_tool_calls": cell.discarded_calls} if cell.discarded_calls else {})}
        if contexts:
            report["hook_context"] = contexts
        return ToolResult(json.dumps(report, ensure_ascii=False), images=images, audios=audios, is_error=cell.status in {"failed", "cancelled"},
                          status="cancelled" if cell.status == "cancelled" else "failed" if cell.status == "failed" else "success",
                          display_summary="Script yielded" if cell.status == "running" else f"Script {cell.status}", runtime_metadata={"code_cell": report})

    async def _emit(self, cell: CodeCell, event: AgentEvent, source: ToolCallSource) -> None:
        await publish_tool_event(self.tool_context.run_context, event, source)

    async def _dispatch(self, cell: CodeCell, requests: list[dict]) -> None:
        # Bound submitted batches as well as executing calls. Otherwise a large
        # Promise.all can retain thousands of completed, full-sized results.
        for start in range(0, len(requests), 10):
            if cell.cancel.is_set():
                raise asyncio.CancelledError
            self.kernel.refresh_live_permission_context()
            batch = requests[start:start + 10]
            calls, sources, request_ids = [], {}, {}
            for request in batch:
                call_id = "code_" + uuid4().hex
                calls.append(ToolCallEvent(id=call_id, name=request["name"], arguments=request["args"]))
                request_ids[call_id] = request["id"]
                sources[call_id] = ToolCallSource("code_mode", cell.parent.tool_call_id, cell.id, request["id"])
            results: dict[str, ToolResult] = {}
            def receive(call, result):
                results[call.id] = result
                cell.hook_context.extend(str(value) for value in getattr(call, "_hook_model_context", ()) if str(value).strip())
            async def emit_event(kind, data):
                call_id = str(data.get("tool_call_id") or data.get("step_id") or data.get("id") or "")
                source = sources.get(call_id, ToolCallSource("code_mode", cell.parent.tool_call_id, cell.id))
                await self._emit(cell, AgentEvent(kind, dict(data)), source)
            policy = self._policy(cell.registry, cell.policy, (request["name"] for request in batch)).with_availability_filter(tools=cell.allowed_names)
            nested = replace(self.tool_context, call_sources=sources, iteration_id=cell.parent.iteration_id,
                model_execution=cell.parent.model_execution, llm=cell.parent.llm,
                cancel_event=cell.cancel, result_sink=receive, emit_event=emit_event,
                tool_registry=cell.registry,
                metadata={**cell.parent.metadata, ACTIVE_TOOLSET_POLICY_METADATA_KEY: policy})
            runner = ToolBatchRunner(ctx=self.context, state=self.state, tool_registry=cell.registry,
                permission_checker=self.permission_checker, approval_handler=nested.approval_handler, skill_manager=self.skill_manager,
                permission_context=nested.permission, tool_ctx=nested)
            async with aclosing(runner.run(calls)) as events:
                async for event in events:
                    call_id = str(event.data.get("id") or event.data.get("tool_call_id") or event.data.get("step_id") or "")
                    source = sources.get(call_id, ToolCallSource("code_mode", cell.parent.tool_call_id, cell.id))
                    await self._emit(cell, event, source)
                    if event.type == "tool_result":
                        result = results.pop(call_id)
                        value = {item.name: getattr(result, item.name) for item in fields(result) if item.name != "runtime_metadata"}
                        mcp = result.runtime_metadata.get("mcp")
                        if isinstance(mcp, dict):
                            value["structured_content"] = mcp.get("structuredContent")
                        await cell.replies.put({"id": request_ids[call_id], "value": value})

    async def _run(self, cell: CodeCell) -> None:
        vm = None
        jobs: set[asyncio.Task] = set()
        timers: dict[str, asyncio.Task] = {}
        terminal_status = "failed"
        async def timer(request):
            await asyncio.sleep(request["delay"] / 1000)
            await cell.replies.put({"id": request["id"], "timer": True})
        try:
            vm = CodeVM()
            packet = await vm.step("start", {"code": cell.code, "tools": cell.catalog, "storage": dict(self.store.values)})
            while True:
                cell.output.extend(packet["output"])
                if packet["yielded"]:
                    cell.changed.set()
                for update in packet["updates"]:
                    self.store.put(update["key"], update["value"])
                if packet["done"]:
                    terminal_status = "failed" if packet["error"] else "completed"
                    cell.error = packet["error"]
                    cell.discarded_calls = packet["pending_tools"]
                    break
                tool_requests = [request for request in packet["requests"] if request["kind"] == "tool"]
                if tool_requests:
                    jobs.add(asyncio.create_task(self._dispatch(cell, tool_requests)))
                for request in packet["requests"]:
                    if request["kind"] == "timer":
                        timers[request["id"]] = asyncio.create_task(timer(request))
                    elif request["kind"] == "cancel_timer" and request["id"] in timers:
                        timers[request["id"]].cancel()
                for task in list(jobs):
                    if task.done():
                        task.result()
                        jobs.remove(task)
                if packet["more_jobs"]:
                    packet = await vm.step()
                    continue
                if cell.replies.empty():
                    active = {task for task in jobs | set(timers.values()) if not task.done()}
                    if not active:
                        raise RuntimeError("Script has an unresolved promise with no pending tool, timer or microtask.")
                    waiting = asyncio.create_task(cell.replies.get())
                    try:
                        done, _ = await asyncio.wait(active | {waiting}, return_when=asyncio.FIRST_COMPLETED)
                        if waiting in done:
                            replies = [waiting.result()]
                        else:
                            for task in done: task.result()
                            replies = []
                    finally:
                        waiting.cancel()
                        await asyncio.gather(waiting, return_exceptions=True)
                else:
                    replies = []
                while not cell.replies.empty(): replies.append(cell.replies.get_nowait())
                packet = await vm.step("deliver", replies)
        except asyncio.CancelledError:
            terminal_status = "cancelled"
            cell.error = "Script cancelled; inspect nested tool outcomes before retrying writes."
            raise
        except Exception as error:
            terminal_status = "failed"
            cell.error = f"{type(error).__name__}: {error}"
        finally:
            cell.cancel.set()
            try:
                receipt = await cancel_and_drain_receipt(list(jobs | set(timers.values())), timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                    label="code cell tools and timers", owner=self.tool_context.pending_cleanup_tasks)
                if receipt.pending:
                    terminal_status = "failed"
                    cell.error = "Nested tool cleanup remains pending; inspect execution evidence before retrying."
            finally:
                if vm is not None: vm.close()
                cell.status = terminal_status
                self.store.receipts[cell.id] = CodeCellReceipt(cell.id, cell.status, cell.error, cell.output, cell.hook_context, cell.discarded_calls)
                cell.changed.set()

    async def aclose(self) -> bool:
        self._closed = True
        active = [cell for cell in self.cells.values() if cell.task is not None and not cell.task.done()]
        for cell in active:
            cell.cancel.set()
            cell.task.cancel()
        await asyncio.gather(*(cell.task for cell in active), return_exceptions=True)
        return bool(active)
