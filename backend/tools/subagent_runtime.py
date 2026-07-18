from __future__ import annotations

from typing import Any

from backend.agent.runtime import AgentRuntime
from backend.permissions.context import ToolExecutionContext


def runtime_from_context(context: ToolExecutionContext | None) -> AgentRuntime | None:
    if context is None:
        return None
    runtime = context.metadata.get("agent_runtime") if isinstance(context.metadata, dict) else None
    return runtime if isinstance(runtime, AgentRuntime) else None


def metadata_from_context(context: ToolExecutionContext | None) -> dict[str, Any]:
    if context is None or not isinstance(context.metadata, dict):
        return {}
    return dict(context.metadata)
