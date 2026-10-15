"""Feature-gated Codex context tools; new_context only requests a boundary."""

from __future__ import annotations

from typing import Any

from backend.agent.context_window import ContextWindowConfig, NEW_CONTEXT_MESSAGE
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.contracts import ToolSpec


class NewContextTool(BaseTool):
    name = "new_context"
    description = "Start a new context window. Does not clear, reset, or otherwise affect environment state."
    should_defer = False
    permission = PermissionLevel.AUTO
    read_only = True
    idempotent = False
    max_result_chars = None
    # These handlers emit no ordinary tool UI item in Codex. The actual
    # window replacement emits a separate ContextCompaction lifecycle.
    projection_visibility = "debug"

    def __init__(self, config: ContextWindowConfig = ContextWindowConfig()) -> None:
        self.config = config

    def get_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, capability="context.new", exposure="direct_model_only")

    def get_schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}, "additionalProperties": False})

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        context.run_context.context_builder.request_new_context_window()
        return ToolResult(NEW_CONTEXT_MESSAGE)


class GetContextRemainingTool(BaseTool):
    name = "get_context_remaining"
    description = "Get the remaining tokens in the current context window."
    should_defer = False
    permission = PermissionLevel.AUTO
    read_only = True
    idempotent = False
    max_result_chars = None
    projection_visibility = "debug"

    def get_spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, capability="context.remaining", exposure="core")

    def get_schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, {"type": "object", "properties": {}, "additionalProperties": False}, output_schema={
            "type": "object", "properties": {"tokens_left": {
                "anyOf": [{"type": "integer"}, {"type": "null"}],
                "description": "Remaining tokens in the current context window, or null when unavailable.",
            }}, "required": ["tokens_left"], "additionalProperties": False,
        })

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        tokens_left = context.run_context.context_builder.context_window_status()["tokens_left"]
        value = "unknown" if tokens_left is None else str(tokens_left)
        return ToolResult(f"You have {value} tokens left in this context window.", runtime_metadata={"code_mode_result": {"tokens_left": tokens_left}})

