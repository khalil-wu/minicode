from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class SleepTool(BaseTool):
    name = "sleep"
    should_defer = False
    description = "Pause execution for a specified duration. New input for the active turn ends the sleep early without consuming it. Returns elapsed wall-clock time."
    permission = PermissionLevel.AUTO
    read_only = True
    timeout_seconds = None
    result_kind = "status"
    display_label = "Sleep"
    max_result_chars = None

    def get_spec(self):
        return ToolSpec(name=self.name, capability="time.sleep", toolset="core", exposure="direct_model_only", required_args=("duration_ms",))

    def get_schema(self):
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object", "properties": {
            "duration_ms": {"type": "integer", "minimum": 1, "maximum": 43_200_000}},
            "required": ["duration_ms"], "additionalProperties": False})

    async def execute(self, args, context=None):
        duration = args["duration_ms"]
        if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 43_200_000:
            return self._error_result("duration_ms must be an integer between 1 and 43200000")
        started = time.monotonic()
        owner = context.run_context if context is not None else None
        queue = owner.turn_input_queue if owner is not None else None
        waiters = []
        if queue is not None and not queue.pending_count():
            waiters.append(asyncio.create_task(queue.wait_for_activity(after_seq=queue.activity_cursor(), timeout=duration / 1000)))
        if context is not None and context.cancel_event is not None:
            waiters.append(asyncio.create_task(context.cancel_event.wait()))
        try:
            if queue is not None and queue.pending_count():
                pass
            elif waiters:
                await asyncio.wait(waiters, timeout=duration / 1000, return_when=asyncio.FIRST_COMPLETED)
            else:
                await asyncio.sleep(duration / 1000)
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        elapsed = time.monotonic() - started
        return ToolResult(content=json.dumps({"elapsed_seconds": elapsed}), result_kind="status", duration_ms=int(elapsed * 1000))


class CurrentTimeTool(BaseTool):
    name = "curr_time"
    should_defer = False
    description = "Return the current time in UTC, formatted as YYYY-MM-DD HH:MM:SS UTC."
    permission = PermissionLevel.AUTO
    read_only = True
    result_kind = "status"

    def get_spec(self):
        return ToolSpec(name=self.name, capability="time.read", toolset="core", exposure="core")

    def get_schema(self):
        return ToolSchema(name=self.name, description=self.description, parameters={"type": "object", "properties": {}, "additionalProperties": False})

    async def execute(self, args, context=None):
        return ToolResult(content=json.dumps({"current_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}), result_kind="status")
