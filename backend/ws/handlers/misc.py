from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any, TYPE_CHECKING
from fastapi import HTTPException
from backend.environments.errors import ExecutorError

from backend.agent.message import AgentEvent
from backend.ws.command_results import emit_command_error
from backend.ws.command_scope import CommandScope, resolve_command_scope
from backend.config import get_available_models, get_models_source

if TYPE_CHECKING:
    from backend.agent.runtime import AgentRuntime
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)
_SUBAGENT_TERMINAL_STATUSES = {"completed", "partial", "failed", "cancelled", "interrupted"}


def _checkpoint_command_scope(session, data):
    # URI cwd belongs to the selected executor. The conversation command owner
    # still carries its separately bound desktop workspace.
    return resolve_command_scope(session, {key: value for key, value in data.items()
        if key not in {"execution_cwd", "workspace_root", "workspace"} or not str(value).startswith("file:")})


@asynccontextmanager
async def _checkpoint_execution(session, scope, data):
    from backend.services.workspace_api_service import capture_workspace_execution
    from backend.workspace.executor_paths import ExecutorPath
    async with capture_workspace_execution(session, scope.conversation_id, environment_id=data.get("environment_id")) as context:
        if context is None and (data.get("environment_id") or any(str(data.get(key) or "").startswith("file:")
                for key in ("cwd_uri", "execution_cwd", "workspace_root", "workspace"))):
            raise ValueError("No usable execution environment is attached to this checkpoint request.")
        if context is not None:
            for key in ("cwd_uri", "execution_cwd", "workspace_root", "workspace"):
                value = str(data.get(key) or "")
                if value.startswith("file:") and ExecutorPath.parse(value) != ExecutorPath.parse(context.permission.environment_scope.cwd_uri):
                    raise ValueError("Checkpoint execution owner is stale.")
        yield context


async def handle_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, list_checkpoints

    try:
        scope = _checkpoint_command_scope(session, data)
        async with _checkpoint_execution(session, scope, data) as context:
            result = list_checkpoints(session.checkpoint_manager, conversation_id=scope.conversation_id,
                workspace_root=scope.workspace_root, limit=int(data.get("limit", 50) or 50), execution_context=context)
    except (CheckpointServiceError, ValueError, ExecutorError, HTTPException) as exc:
        await emit_command_error(session, "checkpoint.list", str(exc.detail) if isinstance(exc, HTTPException) else exc)
        return True
    await session.send_payload(
        scope.apply({"type": "checkpoint.list", "checkpoints": result.checkpoints}),
        log_context="checkpoint.list",
    )
    return True


async def handle_checkpoint_rewind(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from uuid import uuid4

    from backend.agent.conversation_query_guard import conversation_query_guards
    from backend.services.checkpoint_service import CheckpointServiceError, rewind_checkpoint

    guards = conversation_query_guards()
    claim = None
    try:
        scope = _checkpoint_command_scope(session, data)
        from backend.ws.conversation_activity import live_subagent_count

        if live_subagent_count(scope.conversation_id):
            raise CheckpointServiceError("Stop the conversation's live subagents before rewinding its files.")
        claim = guards.try_start(scope.conversation_id, owner_id=f"mutation:checkpoint.rewind:{uuid4().hex}")
        if claim is None:
            raise CheckpointServiceError(
                "Cannot rewind a checkpoint while the conversation has an active agent turn. "
                "Stop the turn and retry the rewind."
            )
        async with _checkpoint_execution(session, scope, data) as context:
            result = await rewind_checkpoint(session.checkpoint_manager, str(data.get("checkpoint_id") or data.get("id") or ""),
                conversation_id=scope.conversation_id, workspace_root=scope.workspace_root, execution_context=context)
    except (CheckpointServiceError, ValueError, ExecutorError, HTTPException) as exc:
        await emit_command_error(session, "checkpoint.rewind", str(exc.detail) if isinstance(exc, HTTPException) else exc)
        return True
    finally:
        if claim is not None:
            guards.end(claim)
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
            read_only=True,
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

    try:
        await session.start_agent_run(
            resume.user_message,
            conversation_id=resume.conversation_id,
            metadata={
                "resume_from_checkpoint": True,
                "resume_checkpoint_run_id": resume.run_id,
                "resume_checkpoint_session_id": resume.checkpoint_session_id,
                "conversation_id": resume.conversation_id,
            },
        )
    except (RuntimeError, ValueError) as exc:
        await session.send_payload(
            scope.apply({"type": "checkpoint.run.resume", "resumed": False, "message": str(exc)}),
            log_context="checkpoint.run.resume",
        )
        await emit_command_error(session, str(data.get("type") or "agent.resume"), exc)
        return True
    await session.send_payload(scope.apply(resume.to_payload()), log_context="checkpoint.run.resume")
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


async def handle_turn_settings_update(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    command = "turn.settings.update"
    if any(name in data for name in ("approvals_reviewer", "environments", "summary", "service_tier")):
        await emit_command_error(session, command, "Only model and effort turn settings are supported")
        return True
    conversation_id, turn_id = data["conversation_id"], data["turn_id"]
    owner = _model_execution_owner(session, conversation_id)
    try:
        applied = await owner.update_turn_model_settings(conversation_id, turn_id,
            model=data.get("model"), effort=data.get("effort"))
    except ValueError as error:
        await emit_command_error(session, command, error)
        return True
    await session.emit_command_result(command, "", level="success", data={
        "conversation_id": conversation_id, "turn_id": turn_id, "status": "applied" if applied else "targetUnavailable"})
    return True


async def handle_model_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.misc_command_service import parse_model_command

    request = parse_model_command(data)
    if request.error_event is not None:
        await emit_command_error(session, "model.set", request.error_event)
        return True
    conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "")
    owner = _model_execution_owner(session, conversation_id)
    changed = False
    try:
        changed = (await owner.restore_default_model_selection(conversation_id or None)
            if request.selection_mode == "default" else await owner.set_selected_model(request.model,
                manual_override=True, conversation_id=conversation_id or None,
                reasoning_effort=request.reasoning_effort, selection_mode="model"))
        if not changed and owner is not session:
            await emit_command_error(session, "model.set", "The requested model is unavailable for this conversation.")
    except ValueError as exc:
        await emit_command_error(session, "model.set", exc)
    # A rejected selection must still restore the client from the session's
    # authoritative model state. This is an explicit command response, not a
    # duplicate background runtime projection.
    await session.send_llm_state(force=True)
    await session.session_lifecycle.send_runtime_capabilities(source="llm.model.set", include_catalogs=False)
    if changed:
        await session.emit_command_result("model.set", "", level="success", data={
            "conversation_id": conversation_id, "selection_mode": request.selection_mode,
        })
    return True


def _model_execution_owner(session: "WebSocketSession", conversation_id: str) -> "WebSocketSession":
    if session.ws_manager is not None and conversation_id:
        owner = session.ws_manager.running_session_for_conversation(conversation_id)
        if owner is not None:
            return owner
    return session


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
            read_only=True,
        )
    except ValueError as exc:
        await emit_command_error(session, "subagent.transcript", exc)
        return True

    from backend.agent.public_projection import project_public_subagent_run

    snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=False)
    public_snapshot = project_public_subagent_run(snapshot)
    execution_metadata = {key: public_snapshot[key] for key in (
        "model", "provider", "reasoning_effort", "agent_path", "mailbox_epoch",
        "workspace_root", "accepted_at", "started_at", "completed_at",
    ) if key in public_snapshot}
    transcript = runtime.load_agent_transcript(subagent_id)
    events = transcript.get("events") if isinstance(transcript, dict) else None
    if not isinstance(events, list) or not events:
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
                    **execution_metadata,
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
                **execution_metadata,
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
            **execution_metadata,
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


async def _emit_user_queue_state(session: "WebSocketSession", conversation_id: str) -> None:
    for item in session.run_manager.queued_user_message_snapshot(conversation_id):
        await session.send_event(AgentEvent.user_message_queue_updated(
            status="queued", conversation_id=conversation_id,
            message_id=item["message_id"], user_message_id=item["user_message_id"],
            position=item["position"], paused=item["paused"],
        ))


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
        return True
    if (
        (expected_turn_id and expected_turn_id != current_turn_id)
        or (expected_message_id and expected_message_id != current_message_id)
        or (expected_task_id and expected_task_id != current_task_id)
    ):
        if expected_message_id and not (expected_turn_id or expected_task_id):
            from backend.ws.conversation_activity import live_sessions
            stopped = False
            for submitting in live_sessions(session):
                if await submitting.command_dispatcher.interrupt_user_message_admission(target_conversation_id, expected_message_id):
                    stopped = True
                    break
            if stopped:
                await _emit_user_queue_state(session, target_conversation_id)
        return True
    if stream_state.get("terminal_fenced"):
        return True
    # ``_cancel_agent_runs`` is the one cancellation entry: it stops the run
    # task, its child subagents and its pending approvals, then drains them and
    # retains ownership of whatever refused to stop.
    await session.cancel_agent_runs(
        conversation_id=target_conversation_id or None,
        reason="user_interrupted",
    )
    await _emit_user_queue_state(session, target_conversation_id)
    # The run owner emits the terminal DONE from its finally block after tool,
    # subprocess, subagent, persistence, and approval cleanup has settled.
    # Emitting DONE here races that cleanup and can claim cancellation while
    # external side effects are still active.
    return True


async def handle_send_message(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_agent_message(session, data, trigger_turn=False)


async def handle_followup_task(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    return await _handle_agent_message(session, data, trigger_turn=True)


async def _handle_agent_message(session: "WebSocketSession", data: dict[str, Any], *, trigger_turn: bool) -> bool:
    from backend.agent.message import AgentEvent
    from backend.agent.runtime import default_runtime
    from backend.agent.run_context import RunContext
    from backend.permissions.context import ToolExecutionContext

    command = "followup_task" if trigger_turn else "send_message"
    recipient = str(data.get("target") or data.get("recipient") or data.get("recipient_id") or "").strip()
    content = str(data.get("message") or data.get("content") or "").strip()
    message_id = str(data.get("message_id") or "").strip()
    if not recipient or not content:
        await session.emit_command_result(
            command,
            "recipient and message are required.",
            level="error",
            data={"recipient": recipient, "message_id": message_id},
        )
        return True

    runtime = default_runtime()
    subagent = _load_subagent_record(runtime, recipient)
    if subagent is None:
        await session.emit_command_result(
            command,
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
            allow_terminal_resume=True,
        )
    except ValueError as exc:
        await session.emit_command_result(
            command,
            str(exc),
            level="error",
            data={"recipient": recipient, "message_id": message_id},
        )
        return True
    parent_run_id = str(getattr(subagent, "parent_run_id", "") or "").strip()
    subagent_status = str(getattr(subagent, "status", "") or "evicted")
    if trigger_turn and subagent_status in _SUBAGENT_TERMINAL_STATUSES:
        registry = session._conversation_tool_registry(target_conversation_id)
        task_tool = registry.get_tool("task")
        resume = getattr(task_tool, "resume_background_subtask", None)
        if not callable(resume):
            await session.emit_command_result(
                command,
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
                    permission=session.permission_context_for_conversation(
                        session.conversation_repo.get_conversation(target_conversation_id), source=command),
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
                    tool_registry=registry,
                ),
            )
        except Exception as exc:
            await session.emit_command_result(
                command,
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
            command,
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
        delivery_mode="trigger_turn" if trigger_turn else "queue_only",
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
        command,
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
            data={"conversation_id": conversation_id, "message_id": message_id, "cancelled": True},
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
    conversation_id = str(data.get("conversation_id") or "").strip()
    message_id = str(data.get("message_id") or "").strip()
    expected_turn_id = str(data.get("expected_turn_id") or "").strip()
    if not conversation_id or not message_id:
        await emit_command_error(session, "user_message.queue.steer", "conversation_id and message_id are required")
        return True
    rejection = session.run_manager.steer_target_rejection(conversation_id, expected_turn_id)
    if rejection is not None:
        await emit_command_error(session, "user_message.queue.steer",
            "The requested turn is not available for steering; the queued message was retained.",
            data={**rejection, "message_id": message_id})
        return True
    if not session.run_manager.begin_queue_steering(conversation_id):
        await session.emit_command_result("user_message.queue.steer", "Another queued message is already being steered.",
            level="warning", data={"conversation_id": conversation_id, "message_id": message_id, "reason": "steer_in_progress"})
        return True
    accepted = False
    try:
        selected_command = next((command for command in session.run_manager.queued_user_messages(conversation_id)
            if str(command.data.get("assistant_message_id") or "").strip() == message_id), None)
        if selected_command is None:
            await session.emit_command_result("user_message.queue.steer", "Queued message is no longer available.",
                level="warning", data={"conversation_id": conversation_id, "message_id": message_id})
            return True
        stream_state = session._conversation_streams[conversation_id]
        steered = session.run_manager.enqueue_turn_steer(conversation_id, selected_command,
            expected_turn_id=expected_turn_id, target_message_id=str(stream_state["message_id"]), queued_message_id=message_id)
        if steered is None:
            rejection = session.run_manager.steer_target_rejection(conversation_id, expected_turn_id) or {
                "conversation_id": conversation_id, "expected_turn_id": expected_turn_id,
                "active_turn_id": str(stream_state.get("turn_id") or ""), "reason": "steer_target_unavailable",
                "error_code": "steer_target_unavailable", "admitted": False, "queued": False}
            await emit_command_error(session, "user_message.queue.steer",
                "The requested turn is not available for steering; the queued message was retained.",
                data={**rejection, "message_id": message_id})
            return True
        accepted = True
        session.run_manager.set_user_queue_paused(conversation_id, "")
        await session.send_event(AgentEvent.user_message_queue_updated(status="dequeued", conversation_id=conversation_id,
            message_id=steered.message_id or message_id, user_message_id=steered.user_message_id,
            reason="steered_current_turn", target_message_id=steered.target_message_id, turn_mode="steer"))
        await session._reject_pending_approvals(reason="user_steer",
            guidance="The user redirected the current task; this pending action was superseded.", conversation_id=conversation_id)
        for position, command in enumerate(session.run_manager.queued_user_messages(conversation_id), 1):
            await session.send_event(AgentEvent.user_message_queue_updated(status="queued", conversation_id=conversation_id,
                message_id=str(command.data.get("assistant_message_id") or ""), user_message_id=str(command.data.get("user_message_id") or ""),
                position=position, reason="queue_reordered", paused=False))
        await session.emit_command_result("user_message.queue.steer", "", level="success",
            data={"conversation_id": conversation_id, "message_id": message_id, "turn_id": expected_turn_id,
                  "expected_turn_id": expected_turn_id, "queued": True, "turn_mode": "steer"})
    finally:
        session.run_manager.finish_queue_steering(conversation_id)
        if accepted and session.running_agent_task_for(conversation_id) is None:
            session.schedule_next_queued_user_message(conversation_id)
    return True


async def handle_user_message_queue_start(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.conversation_query_guard import conversation_query_guards

    conversation_id = str(data.get("conversation_id") or "").strip()
    message_id = str(data.get("message_id") or "").strip()
    if not conversation_id or not message_id:
        await emit_command_error(session, "user_message.queue.start", "conversation_id and message_id are required")
        return True
    manager = session.run_manager
    if (session.running_agent_task_for(conversation_id) is not None or manager.is_queue_dispatching(conversation_id)
            or manager.has_pending_lifecycle_cleanup(conversation_id) or conversation_query_guards().active_claim(conversation_id) is not None
            or conversation_query_guards().has_transition(conversation_id)
            or not manager.begin_queue_steering(conversation_id)):
        await emit_command_error(session, "user_message.queue.start", "The conversation already has an active or pending turn; the queued message was retained.",
            data={"conversation_id": conversation_id, "message_id": message_id, "reason": "queue_start_busy", "queued": False})
        return True
    try:
        queue = manager.promote_queued_user_message(conversation_id, message_id)
        if queue is None:
            await emit_command_error(session, "user_message.queue.start", "Queued message is no longer available.",
                data={"conversation_id": conversation_id, "message_id": message_id, "reason": "queue_message_unavailable"})
            return True
        manager.set_user_queue_paused(conversation_id, "")
    finally:
        manager.finish_queue_steering(conversation_id)
    # This synchronous boundary claims the existing durable dispatcher before
    # another command can start a turn. The normal admission owns actual start.
    session.schedule_next_queued_user_message(conversation_id)
    await session.emit_command_result("user_message.queue.start", "", level="success",
        data={"conversation_id": conversation_id, "message_id": message_id, "queued": True, "turn_mode": "follow_up"})
    return True


async def handle_skills_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.async_cleanup import to_thread_cancel_safe

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
    except ValueError as exc:
        await emit_command_error(session, "skills.list", exc)
        return True
    manager = session.skill_manager
    skills = (
        await to_thread_cancel_safe(lambda: manager.snapshot(scope.workspace_root or None).list_all())
        if manager is not None else []
    )
    await session.send_payload({
        "type": "skills.list", "skills": skills,
        "conversation_id": scope.conversation_id, "workspace_root": scope.workspace_root,
    }, log_context="skills.list")
    await session.send_event(
        AgentEvent.command_result("skills.list", "", data=scope.apply({"count": len(skills)}))
    )
    return True


async def handle_subagent_plan_review(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """User decision on a teammate plan approval request.

    This is the only approval path for sessions whose permission mode does
    not pre-authorize broad execution; the leader poller deliberately leaves
    those requests pending instead of approving them itself.
    """
    import json as _json

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
        if str(payload.get("request_id") or "").strip() != request_id:
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
    sender_epoch = int(getattr(sender, "mailbox_epoch", 0) or 0)
    # Grant stays capped at default execution permissions: user approval
    # authorizes the plan to proceed, it does not transfer the leader's own
    # permission mode to a teammate context.
    granted_mode = "confirm" if approved else ""
    try:
        response = runtime.respond_to_teammate_plan(
            leader_run_id=parent_run_id, subagent_id=subagent_id,
            conversation_id=conversation_id, request_id=request_id,
            mailbox_epoch=sender_epoch, approved=approved,
        )
    except Exception as exc:
        await emit_command_error(session, "subagent.plan_review", exc)
        return True
    if response is None:
        await emit_command_error(
            session, "subagent.plan_review", f"Plan request '{request_id}' is no longer active.",
        )
        return True
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
    runtime: "AgentRuntime",
    subagent_id: str,
    *,
    scope: CommandScope,
    read_only: bool = False,
    allow_terminal_resume: bool = False,
) -> str:
    record = _load_subagent_record(runtime, subagent_id)
    metadata = runtime.get_subagent_task_metadata(subagent_id) or {}
    if record is None and not metadata:
        raise ValueError(f"No subagent found for {subagent_id}.")
    owner = runtime.get_agent_owner_run(subagent_id)
    conversation_id = str(owner.conversation_id or "").strip() if owner is not None else ""
    if not conversation_id:
        raise ValueError("The subagent owner could not be verified.")
    if conversation_id != scope.conversation_id:
        raise ValueError("The subagent belongs to a different conversation.")
    # resolve_command_scope has already bound the active conversation and its
    # workspace. History belongs to that durable scope, not its old websocket.
    if not read_only:
        if allow_terminal_resume and record is not None and record.status in _SUBAGENT_TERMINAL_STATUSES:
            if record.cleanup_pending or runtime.has_live_subagent_task(subagent_id):
                raise ValueError("The subagent is still finishing its previous execution; retry after cleanup completes.")
            if not str(session.session_id or "").strip():
                raise ValueError("The new subagent execution session could not be verified.")
            # The user's explicit follow-up starts a new incarnation through
            # TaskTool.resume_background_subtask, which claims the new session.
            return conversation_id
        owner_session_id = str(
            metadata.get("session_id")
            or (record.session_id if record is not None else "")
            or owner.session_id
            or ""
        ).strip()
        if not owner_session_id:
            raise ValueError("The subagent execution session could not be verified.")
        if owner_session_id != str(session.session_id or "").strip():
            raise ValueError("The subagent belongs to a different session.")
    return conversation_id


def _load_subagent_record(runtime: "AgentRuntime", subagent_id: str) -> Any | None:
    record = runtime.get_subagent(subagent_id)
    if record is not None:
        return record
    return runtime.load_persisted_subagent(subagent_id)


async def handle_skills_install(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.async_cleanup import to_thread_cancel_safe
    from backend.services.skills_api_service import install_skill_from_marketplace

    try:
        scope = resolve_command_scope(session, data, require_conversation=False)
    except ValueError as exc:
        await emit_command_error(session, "skills.install", exc)
        return True
    conversation_id = scope.conversation_id
    manager = session.skill_manager
    skill_manager = (
        await to_thread_cancel_safe(manager.snapshot, scope.workspace_root or None)
        if manager is not None else None
    )
    try:
        result = await install_skill_from_marketplace(
            str(data.get("name", "")), skill_manager=skill_manager,
        )
    except ValueError as exc:
        await emit_command_error(session, "skills.install", exc, data={"conversation_id": conversation_id})
        return True
    except Exception as exc:
        await emit_command_error(session, "skills.install", f"Failed to install skill '{data.get('name', '')}': {exc}", data={"conversation_id": conversation_id})
        return True
    notice = f"Skill '{result['skill']['name']}' installed successfully"
    await session.send_event(AgentEvent(type="system_notice", data={"content": notice, "conversation_id": conversation_id}))
    await session.send_payload({
        "type": "skills.list",
        "skills": result["skills"],
        "conversation_id": conversation_id,
        "workspace_root": scope.workspace_root,
    }, log_context="skills.list")
    await session.send_event(
        AgentEvent.command_result(
            "skills.install",
            notice,
            data={
                "conversation_id": conversation_id,
                "name": result["skill"]["name"],
                "installed": result["installed"],
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
    from backend.commands.catalog import get_enabled_composer_command_catalog

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
        *get_enabled_composer_command_catalog(
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
    if source.startswith("settings.provider."):
        from backend.config import load_config

        # A settings HTTP save can finish after the renderer selects another
        # task. Apply the persisted profile through the existing model owner.
        conversation_id = str(data.get("conversation_id") or "").strip()
        requested_workspace = str(data.get("workspace_root") or "").strip()
        conversation = session.conversation_repo.get_conversation_summary(conversation_id) if conversation_id else None
        try:
            if conversation_id and (conversation is None or conversation.archived):
                raise ValueError("The conversation that requested this provider change is unavailable")
            if not conversation_id and session.active_conversation_id:
                raise ValueError("The blank conversation that requested this provider change is no longer active")
            workspace = session.session_lifecycle.workspace_root_for_conversation(conversation)
            if bool(requested_workspace) != bool(workspace) or (workspace and Path(requested_workspace).resolve() != Path(workspace).resolve()):
                raise ValueError("The conversation workspace changed while the provider was being saved")
            owner = _model_execution_owner(session, conversation_id)
            changed = await owner._set_selected_provider_model(
                str(data.get("provider") or ""), str(data.get("model") or ""),
                manual_override=True, conversation_id=conversation_id or None,
                config_override=load_config(cwd=workspace, resolve_credentials=False), emit_unavailable=False,
            )
            if not changed:
                raise ValueError("The saved provider model is unavailable for this conversation")
        except ValueError as exc:
            await emit_command_error(session, "llm.config.set", exc)
            return True
        if conversation_id == str(session.active_conversation_id or ""):
            await session.send_llm_state(force=True)
            await session.session_lifecycle.send_runtime_capabilities(source="llm.config.set")
        await session.emit_command_result("llm.config.set", "", level="success", data={"conversation_id": conversation_id})
        return True
    conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "")
    owner = _model_execution_owner(session, conversation_id)
    from backend.services.misc_command_service import is_conversation_effort_command

    if is_conversation_effort_command(data, conversation_id):
        level = str(data["reasoning_effort"]).strip().lower()
        try:
            changed = await owner.set_selected_model("", manual_override=True, conversation_id=conversation_id, reasoning_effort=level)
        except ValueError as exc:
            await emit_command_error(session, "effort", exc)
            return True
        if not changed:
            if owner is not session:
                await emit_command_error(session, "effort", "The task's selected model is unavailable; reasoning effort was not changed.")
            return True
        await session.emit_command_result("effort", f"Reasoning effort set to '{level}'.",
            data={"reasoning_effort": level, "applied": True, "conversation_id": conversation_id})
        await session.send_llm_state()
        await session.session_lifecycle.send_runtime_capabilities(source="llm.config.set", include_catalogs=False)
        return True
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
        model_runtime.refresh(
            settings_snapshot=(
                session.config.config_layer_stack.effective_config()
                if session.config.config_layer_stack is not None
                else None
            )
        )
        composed_models = [
            model.id for model in model_runtime.get_models(session.provider)
        ]
        if composed_models:
            session.available_models = composed_models
    # Discovery is informational. It must never invent a selected model when
    # the persisted configuration has none; run admission owns the explicit
    # capability failure for that state.

    from backend.ws.agent_runner import _config_with_runtime_model_budget

    session.config = _config_with_runtime_model_budget(
        session.config,
        model_runtime=model_runtime,
        provider=session.provider,
        model=session.selected_model,
    )

    if conversation_id and session.selected_model:
        await owner._set_selected_provider_model(session.provider, session.selected_model,
            manual_override=True, conversation_id=conversation_id, config_override=session.config,
            reasoning_effort=reasoning_effort or None)
    elif conversation_id:
        session.conversation_repo.update_model_selection(
            conversation_id, provider=session.provider,
            model=session.selected_model,
            reasoning_effort=str(session.config.llm.reasoning_effort or ""),
        )
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
    await session.session_lifecycle.send_runtime_capabilities(source="llm.config.set", include_catalogs=False)
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
    "followup_task": handle_followup_task,
    "inspector.focus": handle_inspector_focus,
    "llm.model.set": handle_model_command,
    "turn.settings.update": handle_turn_settings_update,
    "read_artifact": handle_read_artifact_command,
    "approval.file_diff": handle_approval_file_diff_command,
    "interrupt": handle_interrupt_command,
    "user_message.queue.cancel": handle_user_message_queue_cancel,
    "user_message.queue.steer": handle_user_message_queue_steer,
    "user_message.queue.start": handle_user_message_queue_start,
    "skills.list": handle_skills_list,
    "skills.install": handle_skills_install,
    "skills.marketplace.list": handle_skills_marketplace_list,
    "commands.list": handle_commands_list,
    "llm.config.set": handle_llm_config_set,
}
