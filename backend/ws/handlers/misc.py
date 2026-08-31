from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.ws.command_results import emit_command_error
from backend.ws.command_scope import CommandScope, resolve_command_scope
from backend.config import get_available_models, get_models_source

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, list_checkpoints

    try:
        scope = resolve_command_scope(session, data)
        result = list_checkpoints(
            session.checkpoint_manager,
            conversation_id=scope.conversation_id,
            session_id=str(session.session_id or ""),
            workspace_root=scope.workspace_root,
            limit=int(data.get("limit", 50) or 50),
        )
    except (CheckpointServiceError, ValueError) as exc:
        await emit_command_error(session, "checkpoint.list", exc)
        return True
    await session.send_payload(
        scope.apply({"type": "checkpoint.list", "checkpoints": result.checkpoints}),
        log_context="checkpoint.list",
    )
    return True


async def handle_checkpoint_rewind(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, rewind_checkpoint

    try:
        scope = resolve_command_scope(session, data)
        running_task = session.running_agent_task_for(scope.conversation_id)
        if running_task is not None and not running_task.done():
            raise CheckpointServiceError(
                "Cannot rewind a checkpoint while the conversation has an active agent turn. "
                "Stop the turn and retry the rewind."
            )
        result = await rewind_checkpoint(
            session.checkpoint_manager,
            str(data.get("checkpoint_id") or data.get("id") or ""),
            conversation_id=scope.conversation_id,
            session_id=session.session_id,
            workspace_root=scope.workspace_root,
        )
    except (CheckpointServiceError, ValueError) as exc:
        await emit_command_error(session, "checkpoint.rewind", exc)
        return True
    await session.send_payload(
        scope.apply({"type": "checkpoint.rewound", "checkpoint": result.checkpoint}),
        log_context="checkpoint.rewound",
    )
    return True


async def handle_run_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, list_run_checkpoints

    try:
        scope = resolve_command_scope(session, data)
        result = list_run_checkpoints(
            session_id=str(session.session_id or ""),
            conversation_id=scope.conversation_id,
        )
    except (CheckpointServiceError, ValueError) as exc:
        await emit_command_error(session, "checkpoint.run.list", exc)
        return True
    await session.send_payload(
        scope.apply({
            "type": "checkpoint.run.list",
            "session_id": result.session_id,
            "checkpoints": result.checkpoints,
            **result.runtime_snapshot,
        }),
        log_context="checkpoint.run.list",
    )
    return True


async def handle_subagent_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.runtime import default_runtime
    from backend.services.subagent_service import build_subagent_cancelling_event, build_subagent_status_event

    subagent_id = str(data.get("subagent_id", "")).strip()
    if not subagent_id:
        await emit_command_error(session, "subagent.cancel", "subagent_id is required")
        return True
    runtime = default_runtime()
    try:
        scope = resolve_command_scope(session, data)
        conversation_id = _require_subagent_owner(
            session,
            runtime,
            subagent_id,
            scope=scope,
        )
    except ValueError as exc:
        await emit_command_error(session, "subagent.cancel", exc)
        return True
    runtime_cancel_status = runtime.cancel_subagent_task(subagent_id)
    if runtime_cancel_status == "cancelled":
        await session.send_event(
            build_subagent_cancelling_event(subagent_id, conversation_id=conversation_id)
        )
        await session.emit_command_result(
            "subagent.cancel",
            "Subagent cancellation accepted.",
            data={"subagent_id": subagent_id, "conversation_id": conversation_id},
        )
        return True
    if runtime_cancel_status == "done":
        snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=True)
        if snapshot is not None:
            await session.send_event(
                build_subagent_status_event(
                    subagent_id,
                    snapshot,
                    conversation_id=conversation_id,
                )
            )
        await session.emit_command_result(
            "subagent.cancel",
            "Subagent already finished.",
            data={"subagent_id": subagent_id, "conversation_id": conversation_id},
        )
        return True

    # A manager cancellation is merely an accepted request.  The owning
    # runtime must publish the actual terminal event after its cleanup has
    # completed; publishing `subagent.done(cancelled)` here would make the UI
    # ignore that later authoritative result.
    await session.emit_command_result(
        "subagent.cancel",
        f"No running subagent found for {subagent_id}.",
        level="warning",
        data={"subagent_id": subagent_id, "conversation_id": conversation_id},
    )
    return True


async def handle_subagent_status(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.runtime import default_runtime
    from backend.services.subagent_service import build_subagent_status_event

    subagent_id = str(data.get("subagent_id", "")).strip()
    if not subagent_id:
        await emit_command_error(session, "subagent.status", "subagent_id is required")
        return True

    include_result = bool(data.get("include_result", True))
    runtime = default_runtime()
    try:
        scope = resolve_command_scope(session, data)
        conversation_id = _require_subagent_owner(
            session,
            runtime,
            subagent_id,
            scope=scope,
        )
    except ValueError as exc:
        await emit_command_error(session, "subagent.status", exc)
        return True

    snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=include_result)
    if snapshot is None:
        await session.emit_command_result(
            "subagent.status",
            f"No subagent found for {subagent_id}.",
            level="warning",
            data={"subagent_id": subagent_id},
        )
        return True

    status = str(snapshot.get("status") or "running")
    await session.send_event(
        build_subagent_status_event(
            subagent_id,
            snapshot,
            conversation_id=conversation_id,
        )
    )
    await session.emit_command_result(
        "subagent.status",
        f"{subagent_id}: {status}",
        data={"subagent_id": subagent_id, "snapshot": snapshot},
    )
    return True


async def handle_agent_resume(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, prepare_run_checkpoint_resume

    try:
        scope = resolve_command_scope(session, data)
        resume = prepare_run_checkpoint_resume(
            session_id=str(session.session_id or ""),
            requested_conversation_id=scope.conversation_id,
            active_conversation_id=scope.conversation_id,
        )
    except (CheckpointServiceError, ValueError) as exc:
        await session.emit_command_result("agent.resume", str(exc), level="error")
        return True

    if resume is None:
        await session.send_payload(
            scope.apply({
                "type": "checkpoint.run.resume",
                "resumed": False,
                "message": "No incomplete run checkpoint found.",
            }),
            log_context="checkpoint.run.resume",
        )
        return True

    await session.send_payload(scope.apply(resume.to_payload()), log_context="checkpoint.run.resume")
    await session.start_agent_run(
        resume.user_message,
        conversation_id=resume.conversation_id,
        metadata={
            "resume_from_checkpoint": True,
            "resume_checkpoint_run_id": resume.run_id,
            "conversation_id": resume.conversation_id,
        },
    )
    return True


async def handle_inspector_focus(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    try:
        scope = resolve_command_scope(session, data)
    except ValueError as exc:
        await emit_command_error(session, "inspector.focus", exc)
        return True
    target_kind = str(data.get("target_kind", "")).strip() or "message"
    target_id = str(data.get("target_id", "")).strip()
    stored = session.diagnostic_store.get(target_kind, target_id) if target_id else None
    if stored is not None:
        owners = set(stored.conversation_ids)
        if stored.conversation_id:
            owners.add(stored.conversation_id)
        if not owners or scope.conversation_id not in owners:
            await emit_command_error(
                session,
                "inspector.focus",
                "The diagnostic payload belongs to a different conversation.",
            )
            return True
    if stored is None:
        event = AgentEvent.inspector_update(
            target_kind=target_kind,
            target_id=target_id,
            payload={"diagnostics_loaded": False, "diagnostics_missing": True},
        )
    else:
        event = AgentEvent.inspector_update(
            target_kind=target_kind,
            target_id=target_id,
            payload={**stored.payload, "diagnostics_deferred": False, "diagnostics_loaded": True},
        )
        if stored.conversation_id:
            event.data["conversation_id"] = stored.conversation_id
    event.data["diagnostics_loaded"] = True
    event.data["conversation_id"] = scope.conversation_id
    if scope.request_id:
        event.data["request_id"] = scope.request_id
    await session.send_event(event)
    return True


async def handle_model_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.misc_command_service import parse_model_command

    request = parse_model_command(data)
    if request.error_event is not None:
        await emit_command_error(session, "model.set", request.error_event)
        return True
    await session.set_selected_model(request.model, manual_override=True)
    # A rejected selection must still restore the client from the session's
    # authoritative model state. This is an explicit command response, not a
    # duplicate background runtime projection.
    await session.send_llm_state(force=True)
    await session.session_lifecycle.send_runtime_capabilities(source="llm.model.set")
    return True


async def handle_read_artifact_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.artifact_service import read_artifact_content

    try:
        artifact_id = str(data.get("artifact_id", "")).strip()
        conversation_id = str(
            data.get("conversation_id") or session.active_conversation_id or ""
        ).strip()
        if not conversation_id:
            raise ValueError("read_artifact requires an active or explicit conversation owner")
        # Older clients only sent artifact_id.  Preserve their stable read
        # path while still attaching a server-owned correlation id to the
        # response; newer clients may provide request_id/client_command_id.
        request_id = str(
            data.get("request_id") or data.get("client_command_id") or f"read_artifact:{artifact_id}"
        ).strip()
        conversation = session.conversation_repo.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError("The conversation that owns this file no longer exists")
        if getattr(conversation, "archived", False):
            raise ValueError("Files from an archived conversation cannot be opened")
        workspace_root = str(
            session.session_lifecycle.workspace_root_for_conversation(conversation) or ""
        )
        result = read_artifact_content(
            session.artifact_store,
            session.attachment_store,
            artifact_id,
            purpose=str(data.get("purpose") or ""),
            conversation_id=conversation_id,
            workspace_root=workspace_root,
            request_id=request_id,
        )
    except ValueError as exc:
        await emit_command_error(session, "read_artifact", exc)
        return True
    await session.send_event(result.to_event())
    await session.emit_command_result(
        "read_artifact",
        "",
        data={
            "artifact_id": result.artifact_id,
            "conversation_id": conversation_id,
            "workspace_root": workspace_root,
            "request_id": request_id,
        },
    )
    return True


async def handle_subagent_transcript(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.runtime import default_runtime
    from backend.services.subagent_service import build_subagent_transcript_messages

    subagent_id = str(data.get("subagent_id", "")).strip()
    if not subagent_id:
        await emit_command_error(session, "subagent.transcript", "subagent_id is required")
        return True

    runtime = default_runtime()
    try:
        scope = resolve_command_scope(session, data)
        conversation_id = _require_subagent_owner(
            session,
            runtime,
            subagent_id,
            scope=scope,
        )
    except ValueError as exc:
        await emit_command_error(session, "subagent.transcript", exc)
        return True

    transcript = runtime.load_agent_transcript(subagent_id)
    events = transcript.get("events") if isinstance(transcript, dict) else None
    if not isinstance(events, list) or not events:
        snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=False)
        status = str((snapshot or {}).get("status") or "").strip().lower()
        if status in {"pending", "running", "blocked"}:
            await session.emit_command_result(
                "subagent.transcript",
                f"Subagent {subagent_id} is starting; its durable transcript is not available yet.",
                data={
                    "subagent_id": subagent_id,
                    "conversation_id": conversation_id,
                    "seq": 0,
                    "messages": [],
                    "status": status or "running",
                },
            )
            return True
        await session.emit_command_result(
            "subagent.transcript",
            f"Terminal subagent {subagent_id} has no durable transcript.",
            level="warning",
            data={
                "subagent_id": subagent_id,
                "conversation_id": conversation_id,
                "seq": 0,
                "messages": [],
                "status": status or "unknown",
                "error_kind": "subagent_transcript_missing",
            },
        )
        return True

    messages = build_subagent_transcript_messages(transcript)
    transcript_seq = max(
        (
            int(event.get("seq") or 0)
            for event in events
            if isinstance(event, dict)
        ),
        default=0,
    )
    await session.emit_command_result(
        "subagent.transcript",
        f"Loaded transcript for {subagent_id}.",
        data={
            "subagent_id": subagent_id,
            "conversation_id": conversation_id,
            "seq": transcript_seq,
            "messages": messages,
        },
    )
    return True


async def handle_approval_file_diff_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.approval_diff_service import get_approval_file_diff

    tool_call_id = str(data.get("tool_call_id") or "").strip()
    owner_error = session.approval_response_owner_error(tool_call_id, data)
    if owner_error:
        await emit_command_error(session, "approval.file_diff", owner_error)
        return True
    try:
        pending = session.turn_wait_state.pending_approval_payloads.get(tool_call_id)
        pending_turn_id = str(pending.get("turn_id") or "").strip() if isinstance(pending, dict) else ""
        conversation_id = str(
            data.get("conversation_id")
            or session.active_conversation_id
            or (pending.get("conversation_id") if isinstance(pending, dict) else "")
            or ""
        ).strip()
        turn_id = str(data.get("turn_id") or pending_turn_id).strip()
        result = get_approval_file_diff(
            session.approval_diff_cache,
            tool_call_id=tool_call_id,
            path=str(data.get("path", "")),
            conversation_id=conversation_id,
            turn_id=turn_id,
        )
    except ValueError as exc:
        await emit_command_error(session, "approval.file_diff", exc)
        return True

    await session.send_payload(result.to_payload(), log_context="approval.file_diff")
    return True


async def handle_interrupt_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    target_conversation_id = str(data.get("conversation_id") or "").strip()
    target_conversation_id = target_conversation_id or str(session.active_conversation_id or "").strip()
    expected_turn_id = str(data.get("turn_id") or "").strip()
    expected_message_id = str(data.get("message_id") or "").strip()
    expected_task_id = str(data.get("task_id") or "").strip()
    stream_state = (
        getattr(session, "_conversation_streams", {}).get(target_conversation_id) or {}
        if target_conversation_id
        else {}
    )
    current_turn_id = str(stream_state.get("turn_id") or "").strip()
    current_message_id = str(stream_state.get("message_id") or "").strip()
    current_task_id = str(
        session.run_manager.run_task_ids.get(target_conversation_id) or ""
    ).strip()
    # Scope interruption to a concrete turn. A durable interrupt replayed
    # after reconnect must become a no-op once that turn has completed or a new
    # turn has taken its place; otherwise an old Stop click can kill new work.
    if not (expected_turn_id or expected_message_id or expected_task_id):
        # Fence-less interrupts (the frontend could not attach one because no
        # assistant message exists yet) must still cancel the live run. Only stay
        # a no-op when there is
        # nothing running, which is exactly the stale-replay case.
        if not (current_turn_id or current_message_id or current_task_id):
            return True
    if (
        (expected_turn_id and expected_turn_id != current_turn_id)
        or (expected_message_id and expected_message_id != current_message_id)
        or (expected_task_id and expected_task_id != current_task_id)
    ):
        return True
    # ``_cancel_agent_runs`` is the one cancellation entry: it stops the run
    # task, its child subagents and its pending approvals, then drains them and
    # retains ownership of whatever refused to stop.
    await session.cancel_agent_runs(
        conversation_id=target_conversation_id or None,
        reason="user_interrupted",
    )
    # The run owner emits the terminal DONE from its finally block after tool,
    # subprocess, subagent, persistence, and approval cleanup has settled.
    # Emitting DONE here races that cleanup and can claim cancellation while
    # external side effects are still active.
    return True


async def handle_send_message(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.message import AgentEvent
    from backend.agent.runtime import default_runtime
    from backend.agent.run_context import RunContext
    from backend.permissions.context import ToolExecutionContext

    recipient = str(data.get("recipient") or data.get("recipient_id") or "").strip()
    content = str(data.get("message") or data.get("content") or "").strip()
    message_id = str(data.get("message_id") or "").strip()
    if not recipient or not content:
        await session.emit_command_result(
            "send_message",
            "recipient and message are required.",
            level="error",
            data={"recipient": recipient, "message_id": message_id},
        )
        return True

    runtime = default_runtime()
    subagent = _load_subagent_record(runtime, recipient)
    if subagent is None:
        await session.emit_command_result(
            "send_message",
            f"No subagent found for {recipient}.",
            level="error",
            data={"recipient": recipient, "message_id": message_id},
        )
        return True
    try:
        scope = resolve_command_scope(session, data)
        target_conversation_id = _require_subagent_owner(
            session,
            runtime,
            recipient,
            scope=scope,
        )
    except ValueError as exc:
        await session.emit_command_result(
            "send_message",
            str(exc),
            level="error",
            data={"recipient": recipient, "message_id": message_id},
        )
        return True
    parent_run_id = str(getattr(subagent, "parent_run_id", "") or "").strip()
    subagent_status = str(getattr(subagent, "status", "") or "evicted")
    if subagent_status not in {"running", "pending", "blocked"}:
        task_tool = session.tool_registry.get_tool("task")
        resume = getattr(task_tool, "resume_background_subtask", None)
        if not callable(resume):
            await session.emit_command_result(
                "send_message",
                "The stopped subagent cannot be resumed because TaskTool is unavailable.",
                level="error",
                data={
                    "recipient": recipient,
                    "message_id": message_id,
                    "status": subagent_status,
                },
            )
            return True

        async def emit_subagent_event(event_type: str, payload: dict[str, Any]) -> None:
            await session.send_event(AgentEvent(type=event_type, data=payload))

        try:
            await resume(
                subagent_id=recipient,
                prompt=content,
                context=ToolExecutionContext(
                    permission=session.permission_context,
                    workspace_root=(
                        Path(scope.workspace_root)
                        if scope.workspace_root
                        else None
                    ),
                    session_id=session.session_id,
                    task_id=parent_run_id or session.session_id,
                    conversation_id=target_conversation_id,
                    emit_event=emit_subagent_event,
                    metadata={"run_id": parent_run_id or session.session_id},
                    run_context=RunContext(agent_runtime=runtime),
                    tool_registry=session.tool_registry,
                ),
            )
        except Exception as exc:
            await session.emit_command_result(
                "send_message",
                f"Stopped subagent resume failed: {exc}",
                level="error",
                data={
                    "recipient": recipient,
                    "message_id": message_id,
                    "status": subagent_status,
                },
            )
            return True
        await session.emit_command_result(
            "send_message",
            "Stopped subagent resumed with the message.",
            data={
                "recipient": recipient,
                "message_id": message_id,
                "resumed": True,
            },
        )
        return True

    record = runtime.send_swarm_message(
        sender_id=str(data.get("sender") or data.get("sender_id") or "user"),
        recipient_id=recipient,
        content=content,
        conversation_id=target_conversation_id,
        team_name=str(data.get("team_name") or ""),
        task_id=str(data.get("task_id") or getattr(subagent, "task_id", "") or ""),
        message_id=message_id,
    )
    message_event_type = "message"
    await session.send_payload(
        {
            "type": "subagent.event",
            "subagent_id": recipient,
            "conversation_id": target_conversation_id,
            "event": {
                "type": message_event_type,
                "message": record.public_dict(),
            },
        },
        log_context="subagent.message",
    )
    await session.emit_command_result(
        "send_message",
        "Message sent to subagent.",
        data={"recipient": recipient, "message_id": record.message_id},
    )
    return True


async def handle_user_message_queue_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(
        data.get("conversation_id") or session.active_conversation_id or ""
    ).strip()
    message_id = str(data.get("message_id") or "").strip()
    if not conversation_id or not message_id:
        await emit_command_error(
            session,
            "user_message.queue.cancel",
            "conversation_id and message_id are required",
        )
        return True
    if session.run_manager.remove_queued_user_message(conversation_id, message_id):
        await session.send_event(
            AgentEvent.user_message_queue_updated(
                status="cancelled",
                conversation_id=conversation_id,
                message_id=message_id,
                user_message_id=str(data.get("user_message_id") or ""),
                reason="user_cancelled",
            )
        )
        await session.emit_command_result(
            "user_message.queue.cancel",
            "Queued message cancelled.",
            data={"conversation_id": conversation_id, "message_id": message_id},
        )
    else:
        await session.emit_command_result(
            "user_message.queue.cancel",
            "Queued message is no longer available.",
            level="warning",
            data={"conversation_id": conversation_id, "message_id": message_id},
        )
    return True


async def handle_user_message_queue_steer(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(
        data.get("conversation_id") or session.active_conversation_id or ""
    ).strip()
    message_id = str(data.get("message_id") or "").strip()
    if not conversation_id or not message_id:
        await emit_command_error(
            session,
            "user_message.queue.steer",
            "conversation_id and message_id are required",
        )
        return True
    if not session.run_manager.begin_queue_steering(conversation_id):
        # Another steer already owns this conversation's queue; saying nothing
        # made the steer button look dead on a double click.
        await session.emit_command_result(
            "user_message.queue.steer",
            "Another queued message is already being steered.",
            level="warning",
            data={
                "conversation_id": conversation_id,
                "message_id": message_id,
                "reason": "steer_in_progress",
                "retryable": True,
            },
        )
        return True
    try:
        selected_command = session.run_manager.pop_queued_user_message(conversation_id, message_id)
        if selected_command is None:
            await session.emit_command_result(
                "user_message.queue.steer",
                "Queued message is no longer available.",
                level="warning",
                data={"conversation_id": conversation_id, "message_id": message_id},
            )
            return True

        running = session.running_agent_task_for(conversation_id)
        stream_state = getattr(session, "_conversation_streams", {}).get(conversation_id) or {}
        target_message_id = str(stream_state.get("message_id") or "").strip()
        steered = (
            session.run_manager.enqueue_turn_steer(
                conversation_id,
                selected_command,
                target_message_id=target_message_id,
            )
            if running is not None
            else None
        )
        if steered is not None:
            await session.send_event(
                AgentEvent.user_message_queue_updated(
                    status="dequeued",
                    conversation_id=conversation_id,
                    message_id=steered.message_id or message_id,
                    user_message_id=steered.user_message_id,
                    reason="steered_current_turn",
                    target_message_id=steered.target_message_id,
                    turn_mode="steer",
                )
            )
            reject_pending_approvals = getattr(session, "_reject_pending_approvals", None)
            if callable(reject_pending_approvals):
                await reject_pending_approvals(
                    reason="user_steer",
                    guidance="The user redirected the current task; this pending action was superseded.",
                    conversation_id=conversation_id,
                )
            for position, command in enumerate(session.run_manager.queued_user_messages(conversation_id), 1):
                command_data = getattr(command, "data", {})
                await session.send_event(
                    AgentEvent.user_message_queue_updated(
                        status="queued",
                        conversation_id=conversation_id,
                        message_id=str(command_data.get("assistant_message_id") or ""),
                        user_message_id=str(command_data.get("user_message_id") or ""),
                        position=position,
                        reason="queue_reordered",
                    )
                )
            return True

        # Compatibility fallback for the narrow race before the active run has
        # installed its turn-local queue. Rebuild the normal queue with the
        # selected prompt first, then interrupt as older clients expect.
        queue = session.run_manager.restore_turn_input_as_follow_up(
            conversation_id,
            selected_command,
        )
        for position, command in enumerate(queue, 1):
            command_data = getattr(command, "data", {})
            await session.send_event(
                AgentEvent.user_message_queue_updated(
                    status="queued",
                    conversation_id=conversation_id,
                    message_id=str(command_data.get("assistant_message_id") or ""),
                    user_message_id=str(command_data.get("user_message_id") or ""),
                    position=position,
                    reason="user_steered" if position == 1 else "queue_reordered",
                )
            )
        await session.cancel_agent_runs(conversation_id=conversation_id, reason="user_steered")
    finally:
        session.run_manager.finish_queue_steering(conversation_id)
    if session.running_agent_task_for(conversation_id) is None:
        session.schedule_next_queued_user_message(conversation_id)
    return True


async def handle_skills_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.skills_service import list_skills

    skills = list_skills(session.skill_manager)
    await session.send_payload({"type": "skills.list", "skills": skills}, log_context="skills.list")
    await session.send_event(
        AgentEvent.command_result("skills.list", "", data={"count": len(skills)})
    )
    return True


async def handle_subagent_plan_review(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """User decision on a teammate plan approval request.

    This is the only approval path for sessions whose permission mode does
    not pre-authorize broad execution; the leader poller deliberately leaves
    those requests pending instead of approving them itself.
    """
    import json as _json
    from datetime import UTC, datetime

    from backend.agent.runtime import default_runtime

    subagent_id = str(data.get("subagent_id", "")).strip()
    request_id = str(data.get("request_id", "")).strip()
    approved = bool(data.get("approved"))
    if not subagent_id or not request_id:
        await emit_command_error(
            session, "subagent.plan_review", "subagent_id and request_id are required"
        )
        return True

    runtime = default_runtime()
    try:
        scope = resolve_command_scope(session, data)
        conversation_id = _require_subagent_owner(
            session,
            runtime,
            subagent_id,
            scope=scope,
        )
    except ValueError as exc:
        await emit_command_error(session, "subagent.plan_review", exc)
        return True

    sender = runtime.get_subagent(subagent_id)
    if sender is None or str(getattr(sender, "status", "") or "") != "running":
        await emit_command_error(
            session,
            "subagent.plan_review",
            f"Teammate {subagent_id} is not running; the request is no longer actionable.",
        )
        return True
    parent_run_id = str(getattr(sender, "parent_run_id", "") or "").strip()

    # Locate the pending request and re-verify every ownership fence the
    # leader poller enforces before any response is reserved.
    try:
        messages = runtime.list_swarm_messages(
            participant_id="parent",
            conversation_id=conversation_id,
            since_seq=0,
            limit=1000,
            message_kind="plan_approval_request",
        )
    except Exception as exc:
        await emit_command_error(session, "subagent.plan_review", exc)
        return True
    matched = None
    for message in messages:
        if str(getattr(message, "recipient_id", "") or "") != "parent":
            continue
        try:
            payload = _json.loads(str(getattr(message, "content", "") or ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict) or payload.get("type") != "plan_approval_request":
            continue
        if str(payload.get("requestId") or "").strip() != request_id:
            continue
        if str(getattr(message, "sender_id", "") or "").strip() != subagent_id:
            continue
        sender_team = str(getattr(sender, "team_name", "") or "")
        message_team = str(getattr(message, "team_name", "") or "")
        if not sender_team or sender_team != message_team:
            continue
        if str(payload.get("from") or "") != str(getattr(sender, "teammate_name", "") or ""):
            continue
        if int(getattr(message, "sender_mailbox_epoch", 0) or 0) != int(
            getattr(sender, "mailbox_epoch", 0) or 0
        ):
            continue
        matched = (message, payload)
        break
    if matched is None:
        await emit_command_error(
            session,
            "subagent.plan_review",
            f"No pending plan approval request '{request_id}' from {subagent_id}.",
        )
        return True
    message, payload = matched

    sender_epoch = int(getattr(sender, "mailbox_epoch", 0) or 0)
    reservation_token = runtime.reserve_lifecycle_response(
        response_kind="plan_approval_response",
        participant_id=subagent_id,
        mailbox_epoch=sender_epoch,
        request_id=request_id,
        target_id=parent_run_id,
        expected_active_plan_request_id=request_id,
    )
    if not reservation_token:
        await emit_command_error(
            session,
            "subagent.plan_review",
            f"Plan request '{request_id}' is no longer active.",
        )
        return True
    reservation = {
        "response_kind": "plan_approval_response",
        "participant_id": subagent_id,
        "mailbox_epoch": sender_epoch,
        "request_id": request_id,
        "reservation_token": reservation_token,
    }
    # Grant stays capped at default execution permissions: user approval
    # authorizes the plan to proceed, it does not transfer the leader's own
    # permission mode to a teammate context.
    granted_mode = "confirm" if approved else ""
    response = {
        "type": "plan_approval_response",
        "requestId": request_id,
        "approved": approved,
        "timestamp": datetime.now(UTC).isoformat(),
        **({"permissionMode": granted_mode} if approved else {}),
    }
    try:
        runtime.send_swarm_message(
            sender_id=parent_run_id,
            recipient_id=subagent_id,
            content=_json.dumps(response, ensure_ascii=False),
            conversation_id=conversation_id,
            team_name=str(getattr(message, "team_name", "") or ""),
            recipient_mailbox_epoch=sender_epoch,
        )
    except Exception as exc:
        runtime.release_lifecycle_response(**reservation)
        await emit_command_error(session, "subagent.plan_review", exc)
        return True
    if not runtime.commit_lifecycle_response(**reservation):
        logger.error(
            "plan review response delivered but lifecycle fence commit failed: %s",
            request_id,
        )
    decision = "approved" if approved else "rejected"
    await session.emit_command_result(
        "subagent.plan_review",
        f"Plan request from teammate '{getattr(sender, 'teammate_name', '') or subagent_id}' {decision}.",
        data={
            "subagent_id": subagent_id,
            "conversation_id": conversation_id,
            "request_id": request_id,
            "approved": approved,
            **({"granted_permission_mode": granted_mode} if approved else {}),
        },
    )
    return True


def _require_subagent_owner(
    session: "WebSocketSession",
    runtime: Any,
    subagent_id: str,
    *,
    scope: CommandScope,
) -> str:
    record = _load_subagent_record(runtime, subagent_id)
    metadata_loader = getattr(runtime, "get_subagent_task_metadata", None)
    metadata = metadata_loader(subagent_id) if callable(metadata_loader) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    snapshot_loader = getattr(runtime, "get_subagent_snapshot", None)
    snapshot = (
        snapshot_loader(subagent_id, include_result=False)
        if callable(snapshot_loader)
        else None
    )
    if record is None and not metadata and snapshot is None:
        raise ValueError(f"No subagent found for {subagent_id}.")
    parent_run_id = str(
        getattr(record, "parent_run_id", "")
        or metadata.get("parent_run_id")
        or (snapshot or {}).get("parent_run_id")
        or ""
    ).strip()
    parent = runtime.get_run(parent_run_id) if parent_run_id else None
    conversation_id = str(
        getattr(parent, "conversation_id", "")
        or (snapshot or {}).get("conversation_id")
        or ""
    ).strip()
    if not conversation_id:
        raise ValueError("The subagent owner could not be verified.")
    if conversation_id != scope.conversation_id:
        raise ValueError("The subagent belongs to a different conversation.")
    owner_session_id = str(
        getattr(record, "session_id", "")
        or metadata.get("session_id")
        or (snapshot or {}).get("session_id")
        or ""
    ).strip()
    if owner_session_id and owner_session_id != str(session.session_id or "").strip():
        raise ValueError("The subagent belongs to a different session.")
    return conversation_id


def _load_subagent_record(runtime: Any, subagent_id: str) -> Any | None:
    record = runtime.get_subagent(subagent_id)
    if record is not None:
        return record
    load_persisted = getattr(runtime, "load_persisted_subagent", None)
    return load_persisted(subagent_id) if callable(load_persisted) else None


async def handle_skills_install(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.skills_service import install_skill

    try:
        result = await install_skill(session.skill_manager, str(data.get("name", "")))
    except ValueError as exc:
        await emit_command_error(session, "skills.install", exc)
        return True
    except Exception as exc:
        await emit_command_error(session, "skills.install", f"Failed to install skill '{data.get('name', '')}': {exc}")
        return True
    await session.send_event(AgentEvent(type="system_notice", data={"content": result.notice}))
    if result.installed:
        await session.send_payload({"type": "skills.list", "skills": result.skills}, log_context="skills.list")
    await session.send_event(
        AgentEvent.command_result(
            "skills.install",
            result.notice,
            data={
                "name": str(data.get("name") or "").strip(),
                "installed": bool(result.installed),
            },
        )
    )
    return True


async def handle_skills_marketplace_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.skills.marketplace import list_extensions_marketplace
    from backend.services.skills_api_service import installed_skill_names

    payload = await list_extensions_marketplace(installed_names=installed_skill_names(session.skill_manager))
    marketplace_skills = payload["skills"]
    await session.send_payload({"type": "skills.marketplace.list", "skills": marketplace_skills}, log_context="skills.marketplace.list")
    return True


async def handle_commands_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.skills_service import list_commands

    # Capture the catalog owner before extension materialization yields. A
    # concurrent switch must not relabel the old scope's extension commands as
    # belonging to the new active conversation.
    conversation_id = str(session.active_conversation_id or "").strip()
    conversation = (
        session.conversation_repo.get_conversation(conversation_id)
        if conversation_id
        else None
    )
    workspace_root = (
        session.session_lifecycle.workspace_root_for_conversation(conversation)
        if conversation is not None
        else None
    )
    ensure_extension_commands = getattr(
        session,
        "_ensure_extension_commands_for_conversation",
        None,
    )
    if callable(ensure_extension_commands) and conversation_id:
        await ensure_extension_commands(conversation_id)
    commands = [
        *session.command_registry.list_extension_slash_commands(
            scope_id=conversation_id or None
        ),
        *list_commands(
            workspace_root,
            resolve_active_workspace=False,
        ),
    ]
    request_id = session.event_outbox.client_command_id
    await session.send_payload(
        {
            "type": "commands.list",
            # None is an explicit session catalog owner. The renderer applies
            # it only while it also has no active conversation.
            "conversation_id": conversation_id or None,
            "commands": commands,
            **({"request_id": request_id} if request_id else {}),
        },
        log_context="commands.list",
    )
    return True


async def handle_llm_config_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    source = str(data.get("source") or "").strip()
    from_slash_command = source.startswith("slash:")
    from backend.services.llm_config_service import apply_llm_config_update

    try:
        update = await apply_llm_config_update(data)
    except ValueError as exc:
        error_command = (
            "effort"
            if "reasoning_effort" in data and not from_slash_command
            else "llm.config.set"
        )
        await emit_command_error(session, error_command, exc)
        return True

    session.config = update.config
    saved_payload = update.saved_payload
    reasoning_effort = update.reasoning_effort
    if update.notice is not None:
        await session.emit_command_result(
            update.notice.command,
            update.notice.message,
            level=update.notice.level,
            data=update.notice.data,
        )

    session.provider = update.provider
    section = saved_payload.get(session.provider)
    if not isinstance(section, dict):
        section = {}
    session.available_models = list(section.get("available_models") or get_available_models(session.provider))
    session.models_source = str(section.get("models_source") or get_models_source(session.provider)).strip()
    session.selected_model = str(saved_payload.get("active_model") or section.get("model") or "").strip()
    session.reset_model_selection_overrides()
    model_runtime_resolver = getattr(session, "_model_runtime_for_conversation", None)
    model_runtime = (
        model_runtime_resolver(getattr(session, "active_conversation_id", None))
        if callable(model_runtime_resolver)
        else None
    )
    if model_runtime is not None:
        model_runtime.refresh()
        refresh_oauth = getattr(model_runtime, "refresh_oauth_credentials", None)
        if callable(refresh_oauth):
            await refresh_oauth(session.provider)
        refresh_provider_auth = getattr(model_runtime, "refresh_provider_auth", None)
        if callable(refresh_provider_auth):
            await refresh_provider_auth(session.provider)
        composed_models = [
            model.id for model in model_runtime.get_models(session.provider)
        ]
        if composed_models:
            session.available_models = composed_models
    # Discovery is informational. It must never invent a selected model when
    # the persisted configuration has none; run admission owns the explicit
    # capability failure for that state.

    from backend.ws.agent_runner import (
        _clear_session_llm_cache,
        _config_with_runtime_model_budget,
        _get_or_create_session_llm,
    )

    session.config = _config_with_runtime_model_budget(
        session.config,
        model_runtime=model_runtime,
        provider=session.provider,
        model=session.selected_model,
    )

    _clear_session_llm_cache(session)
    session.llm = _get_or_create_session_llm(
        session,
        config=session.config,
        provider=session.provider,
        model=session.selected_model,
        model_runtime=model_runtime,
    )
    session.context_builder._llm = session.llm
    session.context_builder._budget = session.config.token_budget

    if reasoning_effort and not from_slash_command:
        await session.emit_command_result(
            "effort",
            f"Reasoning effort set to '{reasoning_effort}'.",
            data={
                "reasoning_effort": reasoning_effort,
                "applied": True,
            },
        )
    await session.send_llm_state()
    await session.session_lifecycle.send_runtime_capabilities(source="llm.config.set")
    return True


HANDLERS: dict[str, Any] = {
    "checkpoint.list": handle_checkpoint_list,
    "checkpoint.rewind": handle_checkpoint_rewind,
    "checkpoint.run.list": handle_run_checkpoint_list,
    "agent.resume": handle_agent_resume,
    "subagent.cancel": handle_subagent_cancel,
    "subagent.status": handle_subagent_status,
    "subagent.plan_review": handle_subagent_plan_review,
    "subagent.transcript": handle_subagent_transcript,
    "send_message": handle_send_message,
    "inspector.focus": handle_inspector_focus,
    "llm.model.set": handle_model_command,
    "read_artifact": handle_read_artifact_command,
    "approval.file_diff": handle_approval_file_diff_command,
    "interrupt": handle_interrupt_command,
    "user_message.queue.cancel": handle_user_message_queue_cancel,
    "user_message.queue.steer": handle_user_message_queue_steer,
    "skills.list": handle_skills_list,
    "skills.install": handle_skills_install,
    "skills.marketplace.list": handle_skills_marketplace_list,
    "commands.list": handle_commands_list,
    "llm.config.set": handle_llm_config_set,
}
