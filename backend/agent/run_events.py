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


# Event types emitted internally by LLM adapters (streaming tool-call
# argument fragments).  These must never reach the UI.
_ADAPTER_ONLY_EVENT_TYPES: frozenset[str] = frozenset({
    "tool_call_start",
    "tool_call_delta",
})

# Events that are SDK-only (raw provider passthrough). These are forwarded
# to WebSocket consumers but the UI should suppress them.
_SDK_ONLY_EVENT_TYPES: frozenset[str] = frozenset({
    "stream_event",
})


def should_emit_event(event: AgentEvent) -> bool:
    """Return True if *event* should be forwarded to the UI layer.

    Filters out adapter-internal streaming events (``tool_call_start``,
    ``tool_call_delta``). Tool and approval lifecycle progress now flows
    through as ``agent.progress`` so the UI can explain execution state.
    ``stream_event`` is SDK-only and forwarded but flagged for UI suppression.
    """
    if event.type in _ADAPTER_ONLY_EVENT_TYPES:
        return False
    return True


def is_sdk_only_event(event_type: str) -> bool:
    """Return True if events of this type are SDK-only (UI should suppress)."""
    return event_type in _SDK_ONLY_EVENT_TYPES
