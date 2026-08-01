from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.ws.command_results import emit_command_error
from backend.config import get_available_models, get_models_source
from backend.ws.utils import normalize_permission_mode

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def _emit_command_result_safe(
    session: "WebSocketSession",
    command: str,
    message: str,
    *,
    level: str = "info",
    title: str | None = None,
    data: dict[str, Any] | None = None,
) -> None:
    emit = getattr(session, "_emit_command_result", None)
    kwargs: dict[str, Any] = {}
    if level != "info":
        kwargs["level"] = level
    if title is not None:
        kwargs["title"] = title
    if data is not None:
        kwargs["data"] = data
    if callable(emit):
        await emit(command, message, **kwargs)
        return
    if level == "error":
        await emit_command_error(session, command, message)
    else:
        await session._send_event(AgentEvent(type="system_notice", data={"content": message, **({"data": data} if data else {})}))


async def handle_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, list_checkpoints

    try:
        result = list_checkpoints(
            session.checkpoint_manager,
            conversation_id=str(data.get("conversation_id") or session.active_conversation_id or ""),
            limit=int(data.get("limit", 50) or 50),
        )
    except CheckpointServiceError as exc:
        await emit_command_error(session, "checkpoint.list", exc)
        return True
    await session._send_ws_payload(
        {"type": "checkpoint.list", "conversation_id": result.conversation_id, "checkpoints": result.checkpoints},
        log_context="checkpoint.list",
    )
    return True


async def handle_checkpoint_rewind(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, rewind_checkpoint

    try:
        result = await rewind_checkpoint(
            session.checkpoint_manager,
            str(data.get("checkpoint_id") or data.get("id") or ""),
            conversation_id=str(data.get("conversation_id") or session.active_conversation_id or ""),
            session_id=session.session_id,
        )
    except CheckpointServiceError as exc:
        await emit_command_error(session, "checkpoint.rewind", exc)
        return True
    await session._send_ws_payload(
        {"type": "checkpoint.rewound", "conversation_id": result.conversation_id, "checkpoint": result.checkpoint},
        log_context="checkpoint.rewound",
    )
    return True


async def handle_run_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, list_run_checkpoints

    try:
        result = list_run_checkpoints(
            session_id=str(data.get("session_id") or session.session_id or ""),
            conversation_id=str(data.get("conversation_id") or session.active_conversation_id or ""),
        )
    except CheckpointServiceError as exc:
        await emit_command_error(session, "checkpoint.run.list", exc)
        return True
    await session._send_ws_payload(
        {
            "type": "checkpoint.run.list",
            "session_id": result.session_id,
            "conversation_id": result.conversation_id,
            "checkpoints": result.checkpoints,
            **result.runtime_snapshot,
        },
        log_context="checkpoint.run.list",
    )
    return True


async def handle_task_edit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.plan_edit_service import build_task_update_event

    try:
        event = build_task_update_event(data, conversation_id=str(session.active_conversation_id or ""))
    except ValueError as exc:
        await emit_command_error(session, "task.edit", exc)
        return True
    await session._send_event(event)
    return True


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _exit_plan_mode_for_accept(session: "WebSocketSession") -> None:
    conversation_id = str(getattr(session, "active_conversation_id", "") or "").strip()
    context = getattr(session, "permission_context", None)
    context_mode = str(getattr(context, "mode", "") or "").strip()
    conversation_was_plan = False
    conversation_updated = False
    restore_mode = "auto"

    repo = getattr(session, "conversation_repo", None)
    if conversation_id and repo is not None:
        record = None
        get_conversation = getattr(repo, "get_conversation", None)
        if callable(get_conversation):
            record = get_conversation(conversation_id)
            conversation_was_plan = str(getattr(record, "permission_mode", "") or "").strip() == "plan"
            previous_mode = normalize_permission_mode(str(getattr(record, "permission_previous_mode", "") or ""))
            if previous_mode and previous_mode != "plan":
                restore_mode = previous_mode
        if conversation_was_plan:
            update_permission_mode = getattr(repo, "update_permission_mode", None)
            if callable(update_permission_mode):
                conversation_updated = update_permission_mode(conversation_id, restore_mode) is not None

    should_exit = context_mode == "plan" or conversation_was_plan
    if not should_exit:
        return

    set_mode = getattr(session, "_set_permission_context_mode", None)
    context_changed = False
    if callable(set_mode):
        context_changed = bool(set_mode(restore_mode, source="plan.edit"))

    if context_changed or context_mode == "plan":
        emit_mode = getattr(session, "_emit_permission_mode_updated", None)
        if callable(emit_mode):
            await _maybe_await(emit_mode())
        send_runtime = getattr(session, "_send_task_runtime_update", None)
        if callable(send_runtime):
            await _maybe_await(send_runtime())

    if conversation_updated:
        send_conversations = getattr(session, "_send_conversation_list", None)
        if callable(send_conversations):
            await _maybe_await(send_conversations())


async def handle_plan_edit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.plan_edit_service import build_plan_edit_result

    try:
        result = build_plan_edit_result(data, conversation_id=str(session.active_conversation_id or ""))
    except ValueError as exc:
        await emit_command_error(session, "plan.edit", exc)
        return True

    await session._send_event(result.event)

    if result.action == "accept":
        await _exit_plan_mode_for_accept(session)
    else:
        await session._emit_command_result(
            "plan.edit",
            result.rejection_message,
            level="warning",
            data={"plan_id": result.plan_id, "action": result.action},
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
    record = runtime.get_subagent(subagent_id)
    parent = runtime.get_run(str(getattr(record, "parent_run_id", "") or "")) if record is not None else None
    conversation_id = str(getattr(parent, "conversation_id", "") or session.active_conversation_id or "")
    runtime_cancel_status = runtime.cancel_subagent_task(subagent_id)
    if runtime_cancel_status == "cancelled":
        await session._send_event(
            build_subagent_cancelling_event(subagent_id, conversation_id=conversation_id)
        )
        return True
    if runtime_cancel_status == "done":
        snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=True)
        if snapshot is not None:
            await session._send_event(
                build_subagent_status_event(
                    subagent_id,
                    snapshot,
                    conversation_id=conversation_id,
                )
            )
        return True

    # A manager cancellation is merely an accepted request.  The owning
    # runtime must publish the actual terminal event after its cleanup has
    # completed; publishing `subagent.done(cancelled)` here would make the UI
    # ignore that later authoritative result.
    cancellation_requested = False
    task_manager = getattr(session, "task_manager", None)
    if task_manager is not None:
        try:
            cancellation_requested = bool(task_manager.cancel(subagent_id))
        except Exception:
            cancellation_requested = False
    product_manager = getattr(session, "_product_task_manager", None)
    if not cancellation_requested and product_manager is not None:
        try:
            cancellation_requested = product_manager.cancel_task(subagent_id) is not None
        except Exception:
            cancellation_requested = False
    if cancellation_requested:
        await session._send_event(
            build_subagent_cancelling_event(subagent_id, conversation_id=conversation_id)
        )
    else:
        await session._emit_command_result(
            "subagent.cancel",
            f"No running subagent found for {subagent_id}.",
            level="warning",
            data={"subagent_id": subagent_id},
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
    snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=include_result)
    if snapshot is None:
        await session._emit_command_result(
            "subagent.status",
            f"No subagent found for {subagent_id}.",
            level="warning",
            data={"subagent_id": subagent_id},
        )
        return True

    status = str(snapshot.get("status") or "running")
    record = runtime.get_subagent(subagent_id)
    parent = runtime.get_run(str(getattr(record, "parent_run_id", "") or "")) if record is not None else None
    conversation_id = str(getattr(parent, "conversation_id", "") or session.active_conversation_id or "")
    await session._send_event(
        build_subagent_status_event(
            subagent_id,
            snapshot,
            conversation_id=conversation_id,
        )
    )
    await session._emit_command_result(
        "subagent.status",
        f"{subagent_id}: {status}",
        data={"subagent_id": subagent_id, "snapshot": snapshot},
    )
    return True


async def handle_agent_resume(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.checkpoint_service import CheckpointServiceError, prepare_run_checkpoint_resume

    try:
        resume = prepare_run_checkpoint_resume(
            session_id=str(session.session_id or ""),
            requested_conversation_id=str(data.get("conversation_id") or ""),
            active_conversation_id=str(session.active_conversation_id or ""),
        )
    except CheckpointServiceError as exc:
        await session._emit_command_result("agent.resume", str(exc), level="error")
        return True

    if resume is None:
        await session._send_ws_payload(
            {
                "type": "checkpoint.run.resume",
                "resumed": False,
                "message": "No incomplete run checkpoint found.",
            },
            log_context="checkpoint.run.resume",
        )
        return True

    await session._send_ws_payload(resume.to_payload(), log_context="checkpoint.run.resume")
    await session._run_agent(
        user_message=resume.user_message,
        conversation_id=resume.conversation_id,
        metadata={
            "resume_from_checkpoint": True,
            "run_id": resume.run_id,
            "conversation_id": resume.conversation_id,
        },
    )
    return True


async def handle_inspector_focus(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    target_kind = str(data.get("target_kind", "")).strip() or "message"
    target_id = str(data.get("target_id", "")).strip()
    stored = session._diagnostic_store.get(target_kind, target_id) if target_id else None
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
    event.data.setdefault("conversation_id", str(session.active_conversation_id or ""))
    await session._send_event(event)
    return True


async def handle_model_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.misc_command_service import parse_model_command

    request = parse_model_command(data)
    if request.error_event is not None:
        await emit_command_error(session, "model.set", request.error_event)
        return True
    await session._set_selected_model(request.model, manual_override=True)
    await session._send_llm_state()
    await session._send_runtime_capabilities(source="llm.model.set")
    return True


async def handle_read_artifact_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.artifact_service import read_artifact_content

    try:
        result = read_artifact_content(
            session.artifact_store,
            session.attachment_store,
            str(data.get("artifact_id", "")),
            purpose=str(data.get("purpose") or ""),
            conversation_id=str(session.active_conversation_id or ""),
            workspace_root=str(session._workspace_root_for_conversation() or ""),
        )
    except ValueError as exc:
        await emit_command_error(session, "read_artifact", exc)
        return True
    await session._send_event(result.to_event())
    return True


async def handle_approval_file_diff_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.approval_diff_service import get_approval_file_diff

    try:
        result = get_approval_file_diff(
            session._approval_diff_cache,
            tool_call_id=str(data.get("tool_call_id", "")),
            path=str(data.get("path", "")),
            conversation_id=str(session.active_conversation_id or ""),
        )
    except ValueError as exc:
        await emit_command_error(session, "approval.file_diff", exc)
        return True

    await session._send_ws_payload(result.to_payload(), log_context="approval.file_diff")
    return True


async def handle_interrupt_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    target_conversation_id = str(data.get("conversation_id") or data.get("conversationId") or "").strip()
    target_conversation_id = target_conversation_id or str(session.active_conversation_id or "").strip()
    cancel_runs = getattr(session, "_cancel_agent_runs", None)
    if callable(cancel_runs):
        task_was_running = await cancel_runs(
            conversation_id=target_conversation_id or None,
            reason="user_interrupted",
        )
    else:
        target_task_id = (
            getattr(session, "_conversation_run_task_ids", {}).get(target_conversation_id)
            if target_conversation_id
            else session._active_task_id
        )
        if target_task_id:
            session.task_manager.cancel(target_task_id)
        cancel_event = (
            getattr(session, "_conversation_run_cancel_events", {}).get(target_conversation_id)
            if target_conversation_id
            else getattr(session, "_active_run_cancel_event", None)
        )
        if isinstance(cancel_event, asyncio.Event):
            cancel_event.set()
        task_was_running = False
        target_task = (
            getattr(session, "_conversation_run_tasks", {}).get(target_conversation_id)
            if target_conversation_id
            else session._active_run_task
        )
        if target_task and not target_task.done():
            target_task.cancel()
            task_was_running = True
        await session._cancel_pending_approvals(
            reason="user_interrupted",
            conversation_id=target_conversation_id or None,
        )
        if target_conversation_id:
            getattr(session, "_conversation_run_tasks", {}).pop(target_conversation_id, None)
            getattr(session, "_conversation_run_task_ids", {}).pop(target_conversation_id, None)
            getattr(session, "_conversation_run_cancel_events", {}).pop(target_conversation_id, None)
        else:
            session._active_task_id = None
            session._active_run_cancel_event = None
    # The run owner emits the terminal DONE from its finally block after tool,
    # subprocess, subagent, persistence, and approval cleanup has settled.
    # Emitting DONE here races that cleanup and can claim cancellation while
    # external side effects are still active.
    return True


async def handle_send_message(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.runtime import default_runtime

    recipient = str(data.get("recipient") or data.get("recipient_id") or "").strip()
    content = str(data.get("message") or data.get("content") or "").strip()
    if not recipient or not content:
        await session._emit_command_result(
            "send_message",
            "recipient and message are required.",
            level="error",
            data={"recipient": recipient},
        )
        return True

    runtime = default_runtime()
    subagent = runtime.get_subagent(recipient)
    if subagent is None:
        await session._emit_command_result(
            "send_message",
            f"No subagent found for {recipient}.",
            level="error",
            data={"recipient": recipient},
        )
        return True
    parent = runtime.get_run(str(subagent.parent_run_id or ""))
    target_conversation_id = str(getattr(parent, "conversation_id", "") or "").strip()
    active_conversation_id = str(session.active_conversation_id or "").strip()
    if target_conversation_id and active_conversation_id and target_conversation_id != active_conversation_id:
        await session._emit_command_result(
            "send_message",
            "The subagent belongs to a different conversation.",
            level="error",
            data={"recipient": recipient},
        )
        return True
    if str(subagent.status or "") not in {"running", "pending", "blocked"}:
        await session._emit_command_result(
            "send_message",
            "This subagent is no longer running.",
            level="warning",
            data={"recipient": recipient, "status": subagent.status},
        )
        return True

    record = runtime.send_swarm_message(
        sender_id=str(data.get("sender") or data.get("sender_id") or "user"),
        recipient_id=recipient,
        content=content,
        conversation_id=target_conversation_id or active_conversation_id,
        team_name=str(data.get("team_name") or ""),
        task_id=str(data.get("task_id") or subagent.task_id or ""),
        message_id=str(data.get("message_id") or ""),
    )
    message_event_type = "message"
    await session._send_ws_payload(
        {
            "type": "subagent.event",
            "subagent_id": recipient,
            "conversation_id": target_conversation_id or active_conversation_id,
            "event": {"type": message_event_type, "message": record.to_dict()},
        },
        log_context="subagent.message",
    )
    await session._emit_command_result(
        "send_message",
        "Message sent to subagent.",
        data={"recipient": recipient, "message_id": record.message_id},
    )
    return True


async def handle_user_message_queue_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(
        data.get("conversation_id") or data.get("conversationId") or session.active_conversation_id or ""
    ).strip()
    message_id = str(data.get("message_id") or data.get("messageId") or "").strip()
    if not conversation_id or not message_id:
        return True
    if session._run_manager.remove_queued_user_message(conversation_id, message_id):
        await session._send_event(
            AgentEvent.user_message_queue_updated(
                status="cancelled",
                conversation_id=conversation_id,
                message_id=message_id,
                user_message_id=str(data.get("user_message_id") or data.get("userMessageId") or ""),
                reason="user_cancelled",
            )
        )
    return True


async def handle_user_message_queue_steer(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(
        data.get("conversation_id") or data.get("conversationId") or session.active_conversation_id or ""
    ).strip()
    message_id = str(data.get("message_id") or data.get("messageId") or "").strip()
    if not conversation_id or not message_id:
        return True
    if not session._run_manager.begin_queue_steering(conversation_id):
        return True
    try:
        selected_command = session._run_manager.pop_queued_user_message(conversation_id, message_id)
        if selected_command is None:
            return True

        running = session._running_agent_task_for(conversation_id)
        stream_state = getattr(session, "_conversation_streams", {}).get(conversation_id) or {}
        target_message_id = str(stream_state.get("message_id") or "").strip()
        steered = (
            session._run_manager.enqueue_turn_steer(
                conversation_id,
                selected_command,
                target_message_id=target_message_id,
            )
            if running is not None
            else None
        )
        if steered is not None:
            await session._send_event(
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
            for position, command in enumerate(session._run_manager.queued_user_messages(conversation_id), 1):
                command_data = getattr(command, "data", {})
                await session._send_event(
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
        queue = session._run_manager.restore_turn_input_as_follow_up(
            conversation_id,
            selected_command,
        )
        for position, command in enumerate(queue, 1):
            command_data = getattr(command, "data", {})
            await session._send_event(
                AgentEvent.user_message_queue_updated(
                    status="queued",
                    conversation_id=conversation_id,
                    message_id=str(command_data.get("assistant_message_id") or ""),
                    user_message_id=str(command_data.get("user_message_id") or ""),
                    position=position,
                    reason="user_steered" if position == 1 else "queue_reordered",
                )
            )
        await session._cancel_agent_runs(conversation_id=conversation_id, reason="user_steered")
    finally:
        session._run_manager.finish_queue_steering(conversation_id)
    if session._running_agent_task_for(conversation_id) is None:
        session._schedule_next_queued_user_message(conversation_id)
    return True


async def handle_task_stop(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.runtime_control_service import stop_task

    outcome = stop_task(getattr(session, "_product_task_manager", None), str(data.get("task_id", "")))
    await _emit_command_result_safe(
        session,
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    return True


async def handle_skills_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.skills_service import list_skills

    skills = list_skills(session.skill_manager)
    await session._send_ws_payload({"type": "skills.list", "skills": skills}, log_context="skills.list")
    return True


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
    await session._send_event(AgentEvent(type="system_notice", data={"content": result.notice}))
    if result.installed:
        await session._send_ws_payload({"type": "skills.list", "skills": result.skills}, log_context="skills.list")
    return True


async def handle_skills_marketplace_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.skills.marketplace import list_extensions_marketplace
    from backend.services.skills_api_service import installed_skill_names

    payload = await list_extensions_marketplace(installed_names=installed_skill_names(session.skill_manager))
    marketplace_skills = payload["skills"]
    await session._send_ws_payload({"type": "skills.marketplace.list", "skills": marketplace_skills}, log_context="skills.marketplace.list")
    return True


async def handle_commands_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.skills_service import list_commands

    commands = list_commands()
    await session._send_ws_payload({"type": "commands.list", "commands": commands}, log_context="commands.list")
    return True


async def handle_llm_config_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    model_hint = str(data.get("model", "")).strip()
    source = str(data.get("source") or "").strip()
    from_slash_command = source.startswith("slash:")
    from backend.services.llm_config_service import apply_llm_config_update

    try:
        update = await apply_llm_config_update(data)
    except ValueError as exc:
        await emit_command_error(session, "llm.config.set", exc)
        return True

    session.config = update.config
    saved_payload = update.saved_payload
    reasoning_effort = update.reasoning_effort
    reasoning_effort_requested = update.reasoning_effort_requested
    if update.notice is not None:
        await _emit_command_result_safe(
            session,
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
    session._model_override_active = False
    if not session.available_models:
        session.available_models = [session.selected_model] if session.selected_model else ["default"]

    from backend.ws.agent_runner import _clear_session_llm_cache, _get_or_create_session_llm

    _clear_session_llm_cache(session)
    session.llm = _get_or_create_session_llm(
        session,
        config=session.config,
        provider=session.provider,
        model=session.selected_model,
    )
    session.context_builder._llm = session.llm

    if reasoning_effort and not from_slash_command:
        await _emit_command_result_safe(
            session,
            "effort",
            f"Reasoning effort set to '{reasoning_effort}'.",
            data={
                "reasoning_effort": reasoning_effort,
                "applied": True,
            },
        )
    await session._send_llm_state()
    send_runtime_capabilities = getattr(session, "_send_runtime_capabilities", None)
    if callable(send_runtime_capabilities):
        await send_runtime_capabilities(source="llm.config.set")
    return True


HANDLERS: dict[str, Any] = {
    "checkpoint.list": handle_checkpoint_list,
    "checkpoint.rewind": handle_checkpoint_rewind,
    "checkpoint.run.list": handle_run_checkpoint_list,
    "task.edit": handle_task_edit,
    "plan.edit": handle_plan_edit,
    "agent.resume": handle_agent_resume,
    "subagent.cancel": handle_subagent_cancel,
    "subagent.status": handle_subagent_status,
    "send_message": handle_send_message,
    "inspector.focus": handle_inspector_focus,
    "llm.model.set": handle_model_command,
    "read_artifact": handle_read_artifact_command,
    "approval.file_diff": handle_approval_file_diff_command,
    "interrupt": handle_interrupt_command,
    "user_message.queue.cancel": handle_user_message_queue_cancel,
    "user_message.queue.steer": handle_user_message_queue_steer,
    "task.stop": handle_task_stop,
    "skills.list": handle_skills_list,
    "skills.install": handle_skills_install,
    "skills.marketplace.list": handle_skills_marketplace_list,
    "commands.list": handle_commands_list,
    "llm.config.set": handle_llm_config_set,
}
