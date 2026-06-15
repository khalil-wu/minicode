"""Event-stream filtering helpers for the agent/UI boundary.

Previously this module defined a parallel ``RunEvent`` type together with
``normalize_agent_event`` (AgentEvent -> RunEvent) and
``run_event_to_agent_event`` (RunEvent -> AgentEvent) conversion functions.
That dual-layer abstraction was eliminated: the project now uses
``AgentEvent`` (defined in ``backend.agent.message``) as the single
canonical event type throughout the agent pipeline.

The only remaining responsibility of this module is to decide which events
produced by the agent loop should be forwarded to the UI layer.
"""

from __future__ import annotations

from backend.agent.message import AgentEvent


def is_tool_lifecycle_progress(event: AgentEvent) -> bool:
    """Return True when an ``agent.progress`` event belongs to tool/approval
    lifecycle noise that should not reach the UI timeline."""
    if event.type != "agent.progress":
        return False
    data = event.data
    stage = str(data.get("stage") or "").strip()
    phase = str(data.get("phase") or "").strip()
    event_id = str(data.get("id") or "").strip()
    return (
        stage in {"tool", "approval"}
        or phase in {"tool", "approval"}
        or bool(str(data.get("tool_call_id") or "").strip())
        or event_id.startswith(("tool:", "approval:", "ask:"))
    )


# Event types emitted internally by LLM adapters (streaming tool-call
# argument fragments).  These must never reach the UI.
_ADAPTER_ONLY_EVENT_TYPES: frozenset[str] = frozenset({
    "tool_call_start",
    "tool_call_delta",
})


def should_emit_event(event: AgentEvent) -> bool:
    """Return True if *event* should be forwarded to the UI layer.

    Filters out adapter-internal streaming events (``tool_call_start``,
    ``tool_call_delta``) and tool-lifecycle progress noise that the old
    ``normalize_agent_event`` function used to swallow silently.
    """
    if event.type in _ADAPTER_ONLY_EVENT_TYPES:
        return False
    if event.type == "agent.progress" and is_tool_lifecycle_progress(event):
        return False
    return True
