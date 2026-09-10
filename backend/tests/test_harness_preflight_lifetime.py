from __future__ import annotations

import asyncio
import time

import pytest

from backend.agent.loop_preflight import PhaseDeadlineExceeded, await_preflight


@pytest.mark.parametrize("boundary", ["cancelled", "expired"])
def test_tool_without_hooks_still_observes_turn_boundary(boundary):
    from backend.agent.tool_execution import run_tool
    from backend.llm.base import ToolCallEvent
    from backend.permissions.context import PermissionContext, ToolExecutionContext
    from backend.tools.base import BaseTool, ToolResult, ToolSchema
    from backend.tools.registry import ToolRegistry

    async def scenario():
        effects = []

        class Tool(BaseTool):
            name = "preflight_probe"

            def get_schema(self):
                return ToolSchema(
                    name=self.name, description="Record execution",
                    parameters={"type": "object", "properties": {}},
                )

            async def execute(self, args, context=None):
                effects.append("executed")
                return ToolResult(content="done")

        cancel = asyncio.Event()
        if boundary == "cancelled":
            cancel.set()
        registry = ToolRegistry()
        registry.register(Tool())
        context = ToolExecutionContext(
            permission=PermissionContext(mode="bypass"),
            cancel_event=cancel,
            deadline_monotonic=time.monotonic() - 1 if boundary == "expired" else None,
        )
        call = ToolCallEvent(id="preflight", name="preflight_probe", arguments={})
        if boundary == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await run_tool(call, registry, context)
        else:
            result = await run_tool(call, registry, context)
            assert result.status == "timeout"
        assert effects == []

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ["cancelled", "expired"])
def test_ended_preflight_does_not_start_the_operation(boundary):
    async def scenario():
        effects = []
        cancel = asyncio.Event()
        if boundary == "cancelled":
            cancel.set()

        async def operation():
            effects.append("hook executed")

        error = asyncio.CancelledError if boundary == "cancelled" else PhaseDeadlineExceeded
        with pytest.raises(error):
            await await_preflight(
                operation(), deadline=time.monotonic() - 1 if boundary == "expired" else None,
                cancel_event=cancel,
            )
        assert effects == []

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_source", ["event", "task"])
def test_preflight_cancellation_drains_the_running_hook(cancel_source):
    async def scenario():
        entered = asyncio.Event()
        cleaned = asyncio.Event()
        cancel = asyncio.Event()

        async def operation():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        waiter = asyncio.create_task(await_preflight(operation(), deadline=None, cancel_event=cancel))
        await entered.wait()
        if cancel_source == "event":
            cancel.set()
        else:
            waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert cleaned.is_set()

    asyncio.run(scenario())
