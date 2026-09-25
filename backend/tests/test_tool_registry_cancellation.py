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
