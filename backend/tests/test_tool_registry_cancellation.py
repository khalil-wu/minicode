from __future__ import annotations

import asyncio

import pytest

from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools import registry as registry_module
from backend.tools.base import BaseTool, ToolResult, ToolSchema


def test_registry_cancel_event_drains_the_tool_once(monkeypatch: pytest.MonkeyPatch) -> None:
    class WaitingTool(BaseTool):
        name = "waiting_tool"

        def __init__(self) -> None:
            self.started = asyncio.Event()

        def get_schema(self) -> ToolSchema:
            return ToolSchema(self.name, "Wait for cancellation", {"type": "object"})

        async def execute(self, args, context=None) -> ToolResult:
            self.started.set()
            await asyncio.Event().wait()
            return ToolResult("finished")

    labels: list[str] = []
    original = registry_module.cancel_and_drain_receipt

    async def tracked(*args, **kwargs):
        labels.append(kwargs["label"])
        return await original(*args, **kwargs)

    monkeypatch.setattr(registry_module, "cancel_and_drain_receipt", tracked)

    async def scenario() -> dict:
        registry = registry_module.ToolRegistry()
        tool = WaitingTool()
        registry.register(tool)
        cancel_event = asyncio.Event()
        context = ToolExecutionContext(
            permission=PermissionContext(),
            cancel_event=cancel_event,
            tool_call_id="call-1",
        )
        call = asyncio.create_task(registry.execute(tool.name, {}, context=context))
        await tool.started.wait()
        cancel_event.set()
        with pytest.raises(asyncio.CancelledError):
            await call
        return context.cleanup_receipts["call-1"]

    evidence = asyncio.run(scenario())
    assert labels == ["interrupted registry tool waiting_tool"]
    assert evidence["reason"] == "interrupted"
    assert evidence["completed"] is True


def test_registry_keeps_its_child_and_one_drain_when_event_cancel_is_followed_by_task_cancel():
    class GatedCleanupTool(BaseTool):
        name = "gated_cleanup"

        def __init__(self):
            self.started = asyncio.Event()
            self.cleanup_started = asyncio.Event()
            self.release_cleanup = asyncio.Event()
            self.finished = asyncio.Event()
            self.task = None

        def get_schema(self):
            return ToolSchema(self.name, "Hold real cleanup", {"type": "object"})

        async def execute(self, args, context=None):
            self.task = asyncio.current_task()
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleanup_started.set()
                await self.release_cleanup.wait()
                self.finished.set()

    async def scenario():
        registry = registry_module.ToolRegistry()
        tool = GatedCleanupTool()
        registry.register(tool)
        cancel_event = asyncio.Event()
        context = ToolExecutionContext(permission=PermissionContext(), cancel_event=cancel_event, tool_call_id="double-cancel")
        call = asyncio.create_task(registry.execute(tool.name, {}, context=context))
        try:
            await tool.started.wait()
            cancel_event.set()
            await tool.cleanup_started.wait()
            call.cancel()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(call), 0.02)
            assert tool.task in context.pending_cleanup_tasks and tool.task.cancelling() == 1
            tool.release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await call
            assert tool.finished.is_set() and tool.task.done()
            assert context.cleanup_receipts["double-cancel"]["completed"] is True
            assert context.cleanup_receipts["double-cancel"]["reason"] == "interrupted"
            assert not context.pending_cleanup_tasks
        finally:
            tool.release_cleanup.set()
            await asyncio.gather(call, tool.task, return_exceptions=True)

    asyncio.run(scenario())
