"""Event envelope: stamps stable task/turn/seq correlation fields onto events.

The envelope is created by ``QueryEngine.submit`` for each query turn and
stamps every turn-scoped event with:

- ``task_id`` — the multi-agent task/thread ID (from ``AgentLoopSessionContext``).
- ``turn_id`` — the agent run ID (captured from the first ``agent.run.started``).
- ``seq`` — a monotonic per-turn sequence number (1, 2, 3, …).

These fields are stamped with ``setdefault`` so events that already carry them
(e.g. ``tool_call`` with an explicit ``turn_id``) are never overridden.

The frontend's canonical ``ActivityItem`` adapter reads ``taskId`` and
``turnId`` from event data.  Without these fields the adapter was forced to
fall back to ``messageId``, which the optimisation plan (§20.4) explicitly
forbids.
"""

from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent

# Event types that are internal to the loop's iteration and should not
# receive turn-level envelope fields.  These are adapter-internal streaming
# fragments that never reach the UI.
_ADAPTER_INTERNAL: frozenset[str] = frozenset({
    "tool_call_start",
    "tool_call_delta",
})

# Events that carry turn-level correlation.  This mirrors the WS layer's
# ``_TURN_MESSAGE_SCOPED_EVENT_TYPES`` but is kept independent so the
# envelope doesn't import from the WS package.
_TURN_SCOPED: frozenset[str] = frozenset({
    "item.started",
    "agent_message.delta",
    "item.completed",
    "image_chunk",
    "thinking_delta",
    "thinking",
    "tool_call",
    "tool_output_delta",
    "command_output_chunk",
    "tool_result",
    "agent.run.started",
    "agent.run.completed",
    "agent.item",
    "agent.progress",
    "runtime.span",
    "tool_use_summary",
    "task.update",
    "approval_request",
    "approval.file_diff",
    "ask_user",
    "citation.add",
    "artifact.preview",
    "done",
    "error",
    "stream_resume",
    "permission.decision",
    "context_compacted",
    "budget.warning",
    "subagent.start",
    "subagent.progress",
    "subagent.done",
    "session.state_changed",
})


class EventEnvelope:
    """Stamps stable correlation fields onto turn-scoped events.

    Created once per ``QueryEngine.submit`` call.  The ``turn_id`` is
    initially empty and is captured from the first ``agent.run.started``
    event (which carries ``run_id`` in its data).
    """

    __slots__ = ("_task_id", "_turn_id", "_conversation_id", "_seq")

    def __init__(
        self,
        *,
        task_id: str = "",
        conversation_id: str = "",
    ) -> None:
        self._task_id = task_id
        self._turn_id: str = ""
        self._conversation_id = conversation_id
        self._seq: int = 0

    @property
    def turn_id(self) -> str:
        return self._turn_id

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def stamp(self, event: AgentEvent) -> AgentEvent:
        """Stamp envelope fields onto *event* in-place and return it.

        Events not in the turn-scoped set (e.g. ``stream_event``) are
        returned unchanged.  Fields already present on the event are
        preserved via ``setdefault``.
        """
        if event.type in _ADAPTER_INTERNAL:
            return event

        data: dict[str, Any] = event.data

        # Capture turn_id from the first agent.run.started event.
        if event.type == "agent.run.started" and not self._turn_id:
            run_id = str(data.get("run_id") or "").strip()
            if run_id:
                self._turn_id = run_id

        if event.type not in _TURN_SCOPED:
            return event

        # Stamp correlation fields. setdefault preserves any value already
        # set by the event source (e.g. tool_call may carry its own turn_id).
        if self._task_id:
            data.setdefault("task_id", self._task_id)
        if self._turn_id:
            data.setdefault("turn_id", self._turn_id)
        if self._conversation_id:
            data.setdefault("conversation_id", self._conversation_id)
        if "seq" not in data:
            data["seq"] = self._next_seq()

        return event
