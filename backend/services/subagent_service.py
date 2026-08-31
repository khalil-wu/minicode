from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.public_projection import (
    project_public_subagent_result,
    project_public_subagent_run,
    public_text,
)
from backend.conversations.public_projection import project_public_tool_call


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
    public_snapshot = project_public_subagent_run(snapshot)
    status = str(public_snapshot.get("status") or snapshot.get("status") or "running")
    raw_result = snapshot.get("result")
    result = (
        project_public_subagent_result(raw_result)
        if isinstance(raw_result, dict)
        else None
    )
    result_error = ""
    if isinstance(result, dict):
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
        event.data["snapshot"] = public_snapshot
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
        event.data["snapshot"] = public_snapshot
        event.data["result"] = result if isinstance(result, dict) else None
    return _attach_conversation(event, conversation_id)


def build_subagent_transcript_messages(
    transcript: dict[str, Any],
) -> list[dict[str, Any]]:
    """Project a durable child journal into the ordinary chat transcript schema.

    MiniCode replays a selected parent-owned child thread through the same chat
    renderer as the primary thread.  MiniCode's execution journal is the
    canonical child-thread history, so this projection deliberately returns
    normal transcript messages instead of inventing a second detail-card
    protocol.
    """

    raw_events = transcript.get("events")
    if not isinstance(raw_events, list):
        return []

    def _timestamp(event: dict[str, Any]) -> int:
        try:
            return int(event.get("ts_ms") or 0)
        except (TypeError, ValueError):
            return 0

    # A child journal is an event stream, while the chat surface consumes one
    # user/assistant pair per run.  Keeping that boundary here is what makes a
    # child replay use the exact same turn reducer as the primary conversation;
    # emitting one assistant message per tool call creates fake turns and loses
    # the run's terminal timing.
    messages: list[dict[str, Any]] = []
    current_user: dict[str, Any] | None = None
    current_assistant: dict[str, Any] | None = None
    tool_records: dict[str, dict[str, Any]] = {}
    run_started_at = 0
    final_text: str = ""
    terminal_event: dict[str, Any] | None = None
    last_error: dict[str, Any] | None = None

    def _ensure_assistant(timestamp: int, event_id: str) -> dict[str, Any]:
        nonlocal current_assistant
        if current_assistant is None:
            current_assistant = {
                "id": f"subagent-turn-{event_id}",
                "role": "assistant",
                "content": "",
                "timestamp": run_started_at or timestamp,
                "blocks": [],
                "turn_id": str(transcript.get("agent_id") or "") or None,
                "is_streaming": True,
            }
            if current_assistant["turn_id"] is None:
                current_assistant.pop("turn_id")
        return current_assistant

    def _flush_run() -> None:
        nonlocal current_user, current_assistant, tool_records
        nonlocal run_started_at, final_text, terminal_event, last_error
        if current_user is None and current_assistant is None:
            current_assistant = None
            tool_records = {}
            run_started_at = 0
            final_text = ""
            terminal_event = None
            last_error = None
            return
        if current_assistant is None and current_user is not None and terminal_event is None:
            messages.append(current_user)
            current_user = None
            tool_records = {}
            run_started_at = 0
            final_text = ""
            terminal_event = None
            last_error = None
            return
        assistant = _ensure_assistant(
            int((terminal_event or {}).get("ts_ms") or run_started_at),
            str((terminal_event or {}).get("event_id") or "terminal"),
        )
        blocks = assistant.get("blocks")
        if not isinstance(blocks, list):
            blocks = []
            assistant["blocks"] = blocks
        if final_text:
            # One authoritative final text block belongs at the end of the
            # assistant turn.  Repeated assistant journal observations update
            # this block instead of duplicating the answer on every refresh.
            blocks[:] = [block for block in blocks if block.get("type") != "text"]
            blocks.append({
                "type": "text",
                "item_id": f"{assistant['id']}-final",
                "content": final_text,
                "source": "model_final",
                "status": "completed" if terminal_event else "in_progress",
                "is_streaming": terminal_event is None,
            })
            assistant["content"] = final_text
        if terminal_event is not None:
            terminal_payload = terminal_event.get("payload")
            if not isinstance(terminal_payload, dict):
                terminal_payload = {}
            raw_status = str(terminal_payload.get("status") or "completed").strip().lower()
            terminal_status = "interrupted" if raw_status in {"cancelled", "canceled"} else raw_status
            if terminal_status not in {"completed", "partial", "failed", "interrupted"}:
                terminal_status = "failed"
            terminal_ts = _timestamp(terminal_event)
            raw_duration = terminal_payload.get("duration_ms")
            try:
                duration_ms = int(raw_duration) if raw_duration is not None else max(0, terminal_ts - run_started_at)
            except (TypeError, ValueError):
                duration_ms = max(0, terminal_ts - run_started_at)
            assistant["completed_at"] = terminal_ts
            assistant["duration_ms"] = max(0, duration_ms)
            assistant["terminal_status"] = terminal_status
            assistant["termination_reason"] = str(
                terminal_payload.get("reason") or terminal_payload.get("termination_reason") or terminal_status
            )
            failure_message = public_text(
                terminal_payload.get("failure_message")
                or terminal_payload.get("error"),
                max_chars=12_000,
            ).strip()
            if not failure_message and terminal_status == "failed" and last_error is not None:
                failure_message = public_text(
                    last_error.get("message"),
                    max_chars=12_000,
                ).strip()
            if not failure_message and terminal_status == "failed":
                failure_message = public_text(
                    terminal_payload.get("summary"),
                    max_chars=12_000,
                ).strip()
            if failure_message:
                assistant["failure_message"] = failure_message
            assistant["is_streaming"] = False
            if not final_text:
                summary = public_text(terminal_payload.get("summary"), max_chars=262_144).strip()
                if summary:
                    assistant["content"] = summary
                    blocks.append({
                        "type": "text",
                        "item_id": f"{assistant['id']}-final",
                        "content": summary,
                        "source": "model_final",
                        "status": "completed",
                        "is_streaming": False,
                    })
        if blocks:
            assistant["blocks"] = blocks
        if current_user is not None:
            messages.extend((current_user, assistant))
        else:
            # Pre-boundary journals may begin with a tool event. Preserve that
            # evidence as one assistant transcript item instead of inventing a
            # blank user turn that the main reducer would render as a fake task.
            messages.append(assistant)
        current_user = None
        current_assistant = None
        tool_records = {}
        run_started_at = 0
        final_text = ""
        terminal_event = None
        last_error = None

    for index, raw_event in enumerate(raw_events):
        if not isinstance(raw_event, dict):
            continue
        event_type = str(raw_event.get("event_type") or "").strip()
        payload = raw_event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        event_id = str(raw_event.get("event_id") or f"subagent-event-{index}")
        timestamp = _timestamp(raw_event)

        if event_type == "user_prompt":
            _flush_run()
            content = public_text(payload.get("content") or payload.get("prompt"), max_chars=262_144).strip()
            if not content:
                continue
            current_user = {
                "id": event_id,
                "role": "user",
                "content": content,
                "timestamp": timestamp,
            }
            run_started_at = timestamp
            continue

        if event_type == "assistant":
            _ensure_assistant(timestamp, event_id)
            content = public_text(payload.get("content") or payload.get("text"), max_chars=262_144).strip()
            if content:
                final_text = content
            continue

        if event_type == "tool_use":
            assistant = _ensure_assistant(timestamp, event_id)
            blocks = assistant["blocks"]
            tool_call = payload.get("tool_call")
            if not isinstance(tool_call, dict):
                raise ValueError(f"Subagent journal tool use {event_id!r} has no tool_call object")
            call_id = str(tool_call.get("id") or "").strip()
            tool_name = str(tool_call.get("name") or "").strip()
            if not call_id:
                raise ValueError(f"Subagent journal tool use {event_id!r} has no call id")
            if not tool_name:
                raise ValueError(f"Subagent journal tool use {call_id!r} has no tool name")
            public_call = project_public_tool_call({
                **tool_call,
                "id": call_id,
                "name": tool_name,
                "args": tool_call.get("arguments", tool_call.get("args", {})),
                "status": str(tool_call.get("status") or "running"),
                "started_at": timestamp,
            })
            record = tool_records.get(call_id)
            if record is None:
                record = public_call
                tool_records[call_id] = record
                blocks.append({"type": "tool_call", "record": record})
            else:
                started_at = min(int(record.get("startedAt") or timestamp), timestamp)
                record.clear()
                record.update(public_call)
                record["startedAt"] = started_at
            continue

        if event_type == "tool_result":
            assistant = _ensure_assistant(timestamp, event_id)
            blocks = assistant["blocks"]
            call_id = str(payload.get("tool_call_id") or payload.get("call_id") or "").strip()
            if not call_id:
                raise ValueError(f"Subagent journal tool result {event_id!r} has no call id")
            raw_status = str(payload.get("status") or "").strip().lower()
            status = {
                "completed": "success", "success": "success", "failed": "failed",
                "error": "failed", "blocked": "blocked", "partial": "partial",
                "timeout": "timeout", "cancelled": "cancelled", "canceled": "cancelled",
            }.get(raw_status, "failed")
            if bool(payload.get("synthetic")) and status == "success":
                status = "failed"
            output = public_text(payload.get("content") or payload.get("output"), max_chars=60_000)
            result_started_at: int | None = None
            raw_started_at = payload.get("started_at", payload.get("startedAt"))
            if raw_started_at is not None:
                try:
                    result_started_at = int(raw_started_at)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Subagent journal tool result {call_id!r} has an invalid started_at"
                    ) from exc
            result_finished_at = timestamp
            raw_finished_at = payload.get("finished_at", payload.get("finishedAt"))
            if raw_finished_at is not None:
                try:
                    result_finished_at = int(raw_finished_at)
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        f"Subagent journal tool result {call_id!r} has an invalid finished_at"
                    ) from exc
            record = tool_records.get(call_id)
            if record is None:
                tool_name = str(payload.get("tool_name") or payload.get("name") or "").strip()
                if not tool_name:
                    raise ValueError(
                        f"Subagent journal tool result {call_id!r} has no matching tool use or tool name"
                    )
                record = project_public_tool_call({
                    **payload,
                    "id": call_id,
                    "name": tool_name,
                    "args": {},
                    "status": status,
                    "started_at": result_started_at if result_started_at is not None else timestamp,
                    "finished_at": result_finished_at,
                    "summary": output,
                    "output_preview": output,
                })
                tool_records[call_id] = record
                blocks.append({"type": "tool_call", "record": record})
            else:
                raw_duration = payload.get("duration_ms")
                if raw_duration is not None:
                    try:
                        duration_ms = max(0, int(raw_duration))
                    except (TypeError, ValueError) as exc:
                        raise ValueError(
                            f"Subagent journal tool result {call_id!r} has an invalid duration_ms"
                        ) from exc
                else:
                    duration_ms = max(
                        0,
                        result_finished_at - int(record.get("startedAt") or result_finished_at),
                    )
                projected = project_public_tool_call({
                    **record,
                    **payload,
                    "id": call_id,
                    "name": str(record.get("name") or payload.get("tool_name") or ""),
                    "args": record.get("args") if isinstance(record.get("args"), dict) else {},
                    "status": status,
                    "started_at": result_started_at if result_started_at is not None else record.get("startedAt", timestamp),
                    "finished_at": result_finished_at,
                    "duration_ms": duration_ms,
                    "summary": output,
                    "output_preview": output,
                })
                record.clear()
                record.update(projected)
            superseded_ids = {
                str(item).strip()
                for item in (
                    payload.get("superseded_tool_call_ids")
                    or payload.get("supersededToolCallIds")
                    or []
                )
                if str(item or "").strip()
            }
            for superseded_id in superseded_ids:
                superseded = tool_records.get(superseded_id)
                if superseded is None:
                    continue
                superseded["temporaryRemoved"] = True
                superseded.pop("diff", None)
            continue

        if event_type == "progress" and str(payload.get("kind") or "") == "assistant_message":
            assistant = _ensure_assistant(timestamp, event_id)
            blocks = assistant["blocks"]
            item_id = str(payload.get("item_id") or event_id).strip()
            content = public_text(payload.get("content"), max_chars=262_144)
            source = str(payload.get("source") or "pending").strip() or "pending"
            status = str(payload.get("status") or "running").strip().lower()
            block = {
                "type": "text",
                "item_id": item_id,
                "content": content,
                "source": source,
                "status": status,
                "is_streaming": status not in {"completed", "partial"},
            }
            replaced = False
            for block_index, existing in enumerate(blocks):
                if existing.get("type") == "text" and existing.get("item_id") == item_id:
                    blocks[block_index] = block
                    replaced = True
                    break
            if not replaced:
                blocks.append(block)
            assistant["content"] = content
            assistant["is_streaming"] = True
            continue

        if event_type == "system" and bool(payload.get("transcript_only")):
            assistant = _ensure_assistant(timestamp, event_id)
            blocks = assistant["blocks"]
            content = public_text(payload.get("content"), max_chars=262_144).strip()
            if content:
                blocks.append({
                    "type": "process",
                    "id": event_id,
                    "item_kind": str(payload.get("kind") or "process_text"),
                    "content": content,
                    "source": str(payload.get("source") or "model_preamble"),
                    "status": "completed",
                    "visibility": "timeline",
                    "timestamp": timestamp,
                })
            continue

        if event_type == "system" and str(payload.get("lifecycle") or "") == "error":
            last_error = payload
            continue

        if event_type == "terminal":
            terminal_event = raw_event

    _flush_run()
    return messages
