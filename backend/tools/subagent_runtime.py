from __future__ import annotations

from typing import Any
from copy import deepcopy

from backend.agent.runtime import AgentRuntime
from backend.permissions.context import ToolExecutionContext


def runtime_from_context(context: ToolExecutionContext | None) -> AgentRuntime | None:
    if context is None:
        return None
    run_context = context.run_context
    runtime = run_context.agent_runtime if run_context is not None else None
    return runtime if isinstance(runtime, AgentRuntime) else None


def require_runtime_from_context(
    context: ToolExecutionContext | None,
) -> AgentRuntime:
    runtime = runtime_from_context(context)
    if runtime is None:
        raise RuntimeError(
            "Tool execution context does not own an AgentRuntime"
        )
    return runtime


def metadata_from_context(context: ToolExecutionContext | None) -> dict[str, Any]:
    if context is None or not isinstance(context.metadata, dict):
        return {}
    return dict(context.metadata)


def route_child_approval_request(data: dict[str, Any], *, child_id: str, parent: ToolExecutionContext) -> dict[str, Any]:
    """Route the frozen child request through its parent without re-authorizing it."""
    local_id = str(data.get("tool_call_id") or data.get("id") or "").strip()
    metadata = parent.metadata
    return {**deepcopy(data), "tool_call_id": f"{child_id}:{local_id}",
        "subagent_id": child_id, "source_agent": child_id, "source_thread": child_id,
        "source_tool": str(data.get("tool_name") or ""),
        "child_approval_owner": {"tool_call_id": local_id, "turn_id": data.get("turn_id", ""),
            "conversation_id": data.get("conversation_id", ""), "message_id": data.get("message_id", "")},
        "conversation_id": parent.conversation_id,
        "turn_id": str(metadata.get("turn_id") or metadata.get("run_id") or parent.turn_id),
        "message_id": str(metadata.get("assistant_message_id") or metadata.get("message_id") or "")}
