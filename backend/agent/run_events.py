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

from copy import deepcopy

from backend.agent.message import AgentEvent


# Event types emitted internally by LLM adapters (streaming tool-call
# argument fragments).  These must never reach the UI.
_ADAPTER_ONLY_EVENT_TYPES: frozenset[str] = frozenset({
    "tool_call_start",
    "tool_call_delta",
})


def public_provider_trace(provider_raw: dict, private_call_ids: set[str]) -> dict:
    """Project named provider items without publishing private service facts."""
    traces = [provider_raw[key] for key in ("provider_timeline", "output_items") if key in provider_raw]
    if not private_call_ids:
        return provider_raw
    projected = deepcopy(provider_raw)
    private_items = {identifier for trace in traces for item in trace
        if item.get("call_id") in private_call_ids
        and (identifier := item.get("item_id") or item.get("id"))}
    for key in ("provider_timeline", "output_items"):
        if key in projected:
            projected[key] = [item for item in projected[key]
                if item.get("call_id") not in private_call_ids
                and item.get("item_id") not in private_items
                and item.get("id") not in private_items]
    summary = projected.get("request_summary", {})
    for item in summary.get("largest_input_items", []):
        if item.get("name") in private_call_ids:
            del item["name"]
    return projected


def mark_model_only_tool_event(event: AgentEvent, tool_registry) -> None:
    """Keep private service facts in the journal rather than public events."""
    if tool_registry is None:
        return
    # An explicitly authored policy can require human authorization for a
    # private service. Its control request must still reach the approval UI.
    if event.type in {"approval_request", "ask_user", "request_user_input"}:
        return

    def private_call(call) -> bool:
        name = call.get("name") or call.get("tool_name")
        namespace = call.get("namespace")
        if namespace and namespace != "functions":
            name = f"{namespace}__{name}"
        tool = tool_registry.get_tool(name)
        return tool is not None and tool.private_model_only

    if event.type in {"stream_event", "item.completed"}:
        payload = event.data["data"] if event.type == "stream_event" else event.data
        calls = payload.get("tool_calls")
        if isinstance(calls, list):
            visible_calls = [call for call in calls if not private_call(call)]
            if len(visible_calls) != len(calls):
                payload["tool_calls"] = visible_calls
                if event.type == "stream_event" and not visible_calls:
                    event.data["private_model_only"] = True
        return
    if event.type in {
        "tool_call", "tool_result", "tool_progress", "tool_call_start", "tool_call_delta",
    }:
        tool_name = event.data.get("name") or event.data.get("tool_name")
    elif "tool_name" in event.data:
        tool_name = event.data["tool_name"]
    else:
        return
    tool = tool_registry.get_tool(tool_name)
    if tool is not None and tool.private_model_only:
        event.data["private_model_only"] = True

def should_emit_event(event: AgentEvent) -> bool:
    """Return True if *event* should be forwarded to the UI layer.

    Adapter fragments and Source private model services remain internal.
    Ordinary tool lifecycle and progress facts remain observable.
    """
    if event.type in _ADAPTER_ONLY_EVENT_TYPES:
        return False
    if event.data.get("private_model_only") is True:
        return False
    return True
