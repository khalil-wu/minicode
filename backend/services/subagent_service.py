from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent


def _attach_conversation(event: AgentEvent, conversation_id: str) -> AgentEvent:
    clean_conversation_id = str(conversation_id or "").strip()
    if clean_conversation_id:
        event.data["conversation_id"] = clean_conversation_id
    return event


def build_subagent_cancelling_event(subagent_id: str, *, conversation_id: str = "") -> AgentEvent:
    event = AgentEvent.subagent_progress(
        subagent_id=subagent_id,
        detail="cancelling",
        activity_kind="lifecycle",
        activity_summary="正在停止子任务",
        user_visible=True,
    )
    event.data["cancel_requested"] = True
    return _attach_conversation(event, conversation_id)


def build_subagent_status_event(
    subagent_id: str,
    snapshot: dict[str, Any],
    *,
    conversation_id: str = "",
) -> AgentEvent:
    status = str(snapshot.get("status") or "running")
    result = snapshot.get("result")
    result_content = ""
    result_error = ""
    if isinstance(result, dict):
        result_content = str(result.get("content") or "").strip()
        result_error = str(result.get("error") or "").strip()

    if status in {"pending", "running", "blocked"}:
        event = AgentEvent.subagent_progress(
            subagent_id=subagent_id,
            iteration=int(snapshot.get("iteration") or 0),
            max_iterations=int(snapshot.get("max_iterations") or 0),
            tool_name=str(snapshot.get("current_tool") or ""),
            detail=str(snapshot.get("detail") or status),
            activity_kind="status",
            activity_summary=str(
                snapshot.get("current_activity")
                or snapshot.get("detail")
                or ("等待子任务启动" if status == "pending" else "子任务正在等待" if status == "blocked" else "正在执行子任务")
            ),
            user_visible=True,
        )
        # A status refresh is an observation, not a lifecycle transition.  In
        # particular, pending/blocked must never be represented as
        # `subagent.done`, because the renderer intentionally makes terminal
        # rows sticky to reject stale progress events.
        event.data["status"] = status
        event.data["snapshot"] = snapshot
    else:
        termination_reason = str(
            snapshot.get("termination_reason")
            or snapshot.get("reason")
            or ("success" if status == "completed" else status)
        )
        event = AgentEvent.subagent_done(
            subagent_id=subagent_id,
            summary=str(snapshot.get("summary") or ""),
            error=result_error,
            duration_ms=int(snapshot.get("duration_ms") or 0) or None,
            iterations=int(snapshot.get("iterations") or 0),
            tool_call_count=int(snapshot.get("tool_call_count") or snapshot.get("tool_count") or 0),
            timed_out=bool(snapshot.get("timed_out")),
            status=status,
            termination_reason=termination_reason,
            initiator=str(snapshot.get("initiator") or "runtime"),
        )
        event.data["snapshot"] = snapshot
        event.data["result"] = result if isinstance(result, dict) else None
    return _attach_conversation(event, conversation_id)
