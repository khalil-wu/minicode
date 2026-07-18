from __future__ import annotations

import asyncio
import time
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class SleepTool(BaseTool):
    """Wait briefly without occupying a shell process."""

    name = "sleep"
    description = (
        "Wait for a short period before checking background work or external state. "
        "Prefer monitor for background command status; use sleep only when a real delay is needed."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    timeout_seconds = 65.0
    result_kind = "status"
    display_label = "Sleep"
    max_result_chars = None

    _MAX_SECONDS = 60.0

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="time.sleep",
            toolset="core",
            exposure="core",
            required_args=("seconds",),
            arg_roles={"seconds": "control"},
            repair_policy={"seconds": "runtime_control"},
            empty_args_policy="block",
            blocked_guidance="Missing seconds. Use a small delay such as 1, 2, or 5.",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": self._MAX_SECONDS,
                        "description": "Seconds to wait. Maximum 60; use small values.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Optional short reason for the wait.",
                    },
                },
                "required": ["seconds"],
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        try:
            seconds = float(args.get("seconds"))
        except (TypeError, ValueError):
            return self._error_result("seconds must be a number")
        if seconds < 0:
            return self._error_result("seconds must be non-negative")
        if seconds > self._MAX_SECONDS:
            return self._error_result(f"seconds must be <= {self._MAX_SECONDS:g}")

        started = time.perf_counter()
        cancel_event = getattr(context, "cancel_event", None) if context else None
        try:
            if cancel_event is not None:
                await asyncio.wait_for(cancel_event.wait(), timeout=seconds)
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                return ToolResult(
                    content=f"Sleep cancelled after {elapsed_ms / 1000:.1f}s.",
                    is_error=True,
                    status="cancelled",
                    result_kind=self.result_kind,
                    display_summary="Sleep cancelled",
                    duration_ms=elapsed_ms,
                )
            await asyncio.sleep(seconds)
        except asyncio.TimeoutError:
            pass

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        waited = elapsed_ms / 1000.0
        return ToolResult(
            content=f"Waited {waited:.1f}s.",
            result_kind=self.result_kind,
            display_summary=f"Waited {waited:.1f}s",
            duration_ms=elapsed_ms,
        )
