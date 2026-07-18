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
    from backend.services.checkpoint_service import list_run_checkpoints

    result = list_run_checkpoints(
        session_id=str(data.get("session_id") or session.session_id or ""),
        conversation_id=str(data.get("conversation_id") or session.active_conversation_id or ""),
    )
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


async def _start_plan_followup_run(
    session: "WebSocketSession",
    *,
    plan_id: str,
    action: str,
    message: str,
) -> None:
    target_conversation_id = str(session.active_conversation_id or "").strip()
    if not target_conversation_id:
        session._ensure_active_conversation()
        target_conversation_id = str(session.active_conversation_id or "").strip()
    if not target_conversation_id:
        await emit_command_error(session, "plan.edit", "No active conversation for plan.edit")
        return

    running_for_target = session._running_agent_task_for(target_conversation_id)
    if running_for_target:
        await session._send_event(
            AgentEvent(
                type="error",
                data={
                    "message": "A response is already running in this conversation. Resolve it before editing the plan.",
                    "recoverable": True,
                    "error_type": "tool",
                    "error_code": "agent.busy",
                    "conversation_id": target_conversation_id,
                },
            )
        )
        return

    run_cancel_event = asyncio.Event()
    event_generation_token = session._event_connection_generation.set(None)
    try:
        managed_run = session.task_manager.create(
            "agent.run",
            session._run_agent(
                message,
                conversation_id=target_conversation_id,
                metadata={"plan_id": plan_id, "plan_action": action},
                cancel_event=run_cancel_event,
            ),
        )
    finally:
        session._event_connection_generation.reset(event_generation_token)
    session._register_agent_run(
        conversation_id=target_conversation_id,
        task=managed_run.task,
        task_id=managed_run.id,
        cancel_event=run_cancel_event,
    )

    async def _wait_and_cleanup() -> None:
        try:
            if managed_run.task is not None:
                await managed_run.task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Plan follow-up run failed: %s", exc, exc_info=True)
            error_event = AgentEvent.error(
                f"Plan follow-up run failed: {exc}",
                recoverable=True,
                error_type="api",
            )
            error_event.data["conversation_id"] = target_conversation_id
            await session._send_event(error_event)
        finally:
            session._cleanup_agent_run(
                conversation_id=target_conversation_id,
                task=managed_run.task,
                task_id=managed_run.id,
                cancel_event=run_cancel_event,
            )

    event_generation_token = session._event_connection_generation.set(None)
    try:
        asyncio.create_task(_wait_and_cleanup())
    finally:
        session._event_connection_generation.reset(event_generation_token)


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
        await _start_plan_followup_run(
            session,
            plan_id=result.plan_id,
            action=result.action,
            message=result.followup_message,
        )
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
    from backend.services.subagent_service import build_subagent_cancelled_event, build_subagent_cancelling_event

    subagent_id = str(data.get("subagent_id", "")).strip()
    if not subagent_id:
        await emit_command_error(session, "subagent.cancel", "subagent_id is required")
        return True
    runtime = default_runtime()
    record = runtime.get_subagent(subagent_id)
    parent = runtime.get_run(str(getattr(record, "parent_run_id", "") or "")) if record is not None else None
    conversation_id = str(getattr(parent, "conversation_id", "") or session.active_conversation_id or "")
    cancelled = False
    runtime_cancel_status = runtime.cancel_subagent_task(subagent_id)
    if runtime_cancel_status == "cancelled":
        await session._send_event(
            build_subagent_cancelling_event(subagent_id, conversation_id=conversation_id)
        )
        return True
    if runtime_cancel_status == "done":
        cancelled = True
    task_manager = getattr(session, "task_manager", None)
    if task_manager is not None:
        try:
            cancelled = bool(task_manager.cancel(subagent_id))
        except Exception:
            cancelled = False
    product_manager = getattr(session, "_product_task_manager", None)
    if not cancelled and product_manager is not None:
        try:
            cancelled = product_manager.cancel_task(subagent_id) is not None
        except Exception:
            cancelled = False
    await session._send_event(
        build_subagent_cancelled_event(
            subagent_id,
            cancelled=cancelled,
            conversation_id=conversation_id,
        )
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


async def handle_workflow_resume(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.runtime import default_runtime
    from backend.permissions.context import ToolExecutionContext
    from backend.tools.workflow_tool import WorkflowTool

    workflow_id = str(data.get("workflow_id") or "").strip()
    if not workflow_id:
        await session._emit_command_result(
            "workflow.resume",
            "workflow_id is required.",
            level="error",
        )
        return True

    target_conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    if not target_conversation_id:
        ensure_active = getattr(session, "_ensure_active_conversation", None)
        if callable(ensure_active):
            ensure_active()
        target_conversation_id = str(session.active_conversation_id or "").strip()

    conversation = None
    if target_conversation_id:
        conversation = session.conversation_repo.get_conversation(target_conversation_id)
    if conversation is None:
        conversation = session.active_conversation

    permission_context = session._permission_context_for_conversation(
        conversation,
        source="workflow.resume",
    )
    workspace_root = session._workspace_root_for_conversation(conversation)
    workspace_context = session._workspace_context_for_conversation(conversation)

    async def emit_runtime_event(event_type: str, payload: dict[str, Any]) -> None:
        event_payload = dict(payload)
        if target_conversation_id:
            event_payload.setdefault("conversation_id", target_conversation_id)
        persist = getattr(session, "_persist_ui_agent_state_event", None)
        if callable(persist) and target_conversation_id:
            persist(target_conversation_id, event_type, event_payload)
        await session._send_event(AgentEvent(type=event_type, data=event_payload))

    refresh_registry = getattr(session, "refresh_tool_registry_if_mcp_changed", None)
    if callable(refresh_registry):
        refresh_registry()

    context = ToolExecutionContext(
        permission=permission_context,
        session_id=str(session.session_id or ""),
        task_id=f"workflow-resume:{workflow_id}",
        metadata={
            "agent_runtime": default_runtime(),
            "run_id": f"workflow-resume:{workflow_id}",
            "conversation_id": target_conversation_id,
            **({"workspace_context": workspace_context} if workspace_context is not None else {}),
        },
        emit_event=emit_runtime_event,
        workspace_root=workspace_root,
        allow_network=permission_context.mode == "bypass",
        task_manager=getattr(session, "task_manager", None),
        background_manager=getattr(session, "background_manager", None),
        terminal_manager=getattr(session, "terminal_manager", None),
        checkpoint_manager=getattr(session, "checkpoint_manager", None),
        permission_checker=getattr(session, "permission_checker", None),
        conversation_id=target_conversation_id,
        llm=getattr(session, "llm", None),
        artifact_store=getattr(session, "artifact_store", None),
    )
    tool = WorkflowTool(
        llm_provider=lambda: session.llm,
        tool_registry_provider=lambda: session.tool_registry,
        artifact_store=session.artifact_store,
        permission_checker_provider=lambda: session.permission_checker,
        agent_settings_provider=lambda: session.config.agent,
        token_budget_provider=lambda: session.config.token_budget,
    )
    result = await tool.execute(
        {
            "workflow_id": workflow_id,
            **({"timeout_seconds": data.get("timeout_seconds")} if data.get("timeout_seconds") else {}),
        },
        context=context,
    )
    await session._emit_command_result(
        "workflow.resume",
        result.content,
        level="error" if result.is_error else "info",
        data={
            "workflow_id": workflow_id,
            "status": result.status,
            "display_summary": result.display_summary,
        },
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


async def handle_verification_run(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.loop import _run_verify_command
    from backend.agent.runtime import new_run_id
    from backend.services.verification_service import (
        no_workspace_outcome,
        prepare_verification_plan,
        verification_result_event,
        verification_result_outcome,
        verification_started_event,
    )

    config = getattr(session, "config", None)
    plan = prepare_verification_plan(data, config)
    if plan.error is not None:
        await session._emit_command_result(
            plan.error.command,
            plan.error.message,
            level=plan.error.level,
            data=plan.error.data,
        )
        return True
    workspace_root = session._workspace_root_for_conversation(
        session.conversation_repo.get_conversation(session.active_conversation_id or "")
    ) if session.active_conversation_id else None
    if workspace_root is None:
        workspace_root = getattr(session, "workspace_root", None)
    if workspace_root is None:
        outcome = no_workspace_outcome()
        await session._emit_command_result(outcome.command, outcome.message, level=outcome.level, data=outcome.data)
        return True

    run_id = new_run_id("verify")
    await session._send_event(
        verification_started_event(run_id, command=plan.command, conversation_id=session.active_conversation_id or "")
    )
    passed, output = await _run_verify_command(plan.command, workspace_root, plan.timeout)
    await session._send_event(
        verification_result_event(
            run_id,
            passed=passed,
            output=output,
            command=plan.command,
            conversation_id=session.active_conversation_id or "",
        )
    )
    outcome = verification_result_outcome(run_id, passed=passed, output=output)
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    return True


async def handle_inspector_focus(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.misc_command_service import build_inspector_focus_event

    await session._send_event(
        build_inspector_focus_event(data, conversation_id=str(session.active_conversation_id or ""))
    )
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
    interrupted_conversations = getattr(session, "_interrupted_conversation_ids", None)
    if isinstance(interrupted_conversations, set) and target_conversation_id:
        interrupted_conversations.add(target_conversation_id)
    else:
        session._interrupted = True
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
    if not task_was_running:
        from backend.agent.message import AgentEvent
        done = AgentEvent.done(status="cancelled", reason="user_interrupted")
        if target_conversation_id:
            done.data["conversation_id"] = target_conversation_id
        await session._send_event(done)
    return True


async def handle_subagent_resume(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.runtime import default_runtime
    from backend.permissions.context import ToolExecutionContext
    from backend.tools.agent_tools import TaskTool

    subagent_id = str(data.get("subagent_id") or "").strip()
    runtime = default_runtime()
    record = runtime.get_subagent(subagent_id)
    if not subagent_id or record is None:
        await session._emit_command_result(
            "subagent.resume",
            "The delegated task is no longer available to resume.",
            level="error",
            data={"subagent_id": subagent_id},
        )
        return True

    parent = runtime.get_run(str(getattr(record, "parent_run_id", "") or ""))
    target_conversation_id = str(getattr(parent, "conversation_id", "") or session.active_conversation_id or "")
    resume_messages = []
    if bool(data.get("include_messages", True)):
        resume_messages = [
            message.to_dict()
            for message in runtime.list_swarm_messages(
                participant_id=subagent_id,
                conversation_id=target_conversation_id,
                limit=100,
            )
        ]
    if record.status == "running":
        await session._emit_command_result(
            "subagent.resume",
            "The delegated task is already running.",
            level="warning",
            data={"subagent_id": subagent_id},
        )
        return True

    conversation = session.conversation_repo.get_conversation(target_conversation_id)
    conversation_id = str(target_conversation_id or "").strip()
    if conversation is None or not conversation_id:
        await session._emit_command_result(
            "subagent.resume",
            "The delegated task's original conversation is no longer available.",
            level="error",
            data={"subagent_id": subagent_id},
        )
        return True
    permission_context = session._permission_context_for_conversation(conversation, source="subagent.resume")
    workspace_root = session._workspace_root_for_conversation(conversation)
    workspace_context = session._workspace_context_for_conversation(conversation)

    async def emit_runtime_event(event_type: str, payload: dict[str, Any]) -> None:
        event_payload = {**payload, "conversation_id": conversation_id}
        persist = getattr(session, "_persist_ui_agent_state_event", None)
        if callable(persist) and conversation_id:
            persist(conversation_id, event_type, event_payload)
        await session._send_event(AgentEvent(type=event_type, data=event_payload))

    context = ToolExecutionContext(
        permission=permission_context,
        session_id=str(session.session_id or ""),
        task_id=record.parent_run_id or f"subagent-resume:{subagent_id}",
        metadata={
            "agent_runtime": runtime,
            "run_id": record.parent_run_id,
            "conversation_id": conversation_id,
            **({"workspace_context": workspace_context} if workspace_context is not None else {}),
        },
        emit_event=emit_runtime_event,
        workspace_root=workspace_root,
        allow_network=permission_context.mode == "bypass",
        task_manager=getattr(session, "task_manager", None),
        background_manager=getattr(session, "background_manager", None),
        terminal_manager=getattr(session, "terminal_manager", None),
        checkpoint_manager=getattr(session, "checkpoint_manager", None),
        permission_checker=getattr(session, "permission_checker", None),
        conversation_id=conversation_id,
        llm=getattr(session, "llm", None),
        artifact_store=getattr(session, "artifact_store", None),
    )
    tool = TaskTool(
        llm_provider=lambda: session.llm,
        tool_registry_provider=lambda: session.tool_registry,
        artifact_store=session.artifact_store,
        permission_checker_provider=lambda: session.permission_checker,
        agent_settings_provider=lambda: session.config.agent,
        token_budget_provider=lambda: session.config.token_budget,
    )
    description = record.objective or record.prompt_summary or "Continue delegated task"
    retained_messages = "\n".join(
        f"- {str(message.get('sender_id') or 'agent')}: {str(message.get('content') or '').strip()}"
        for message in resume_messages
        if str(message.get("content") or "").strip()
    )
    resume_prompt = f"Continue only the retained delegated task: {description}"
    if retained_messages:
        resume_prompt = f"{resume_prompt}\n\nRetained team messages:\n{retained_messages}"
    result = await tool.execute(
        {
            "description": description,
            "prompt": resume_prompt,
            "agent_type": record.agent_type,
            "resume_subagent_id": subagent_id,
            "run_in_background": True,
            "required_for_final": record.required_for_final,
            "read_only": record.read_only,
            "write_scope": record.write_scope,
        },
        context=context,
    )
    await session._emit_command_result(
        "subagent.resume",
        result.content,
        level="error" if result.is_error else "info",
        data={"subagent_id": subagent_id, "status": result.status},
    )
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
        queue = session._run_manager.promote_queued_user_message(conversation_id, message_id)
        if queue is None:
            return True
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
        await session._cancel_agent_runs(
            conversation_id=conversation_id,
            reason="user_steered",
        )
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


async def handle_approval_respond(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.runtime_control_service import respond_to_approval

    outcome = respond_to_approval(
        getattr(session, "_product_approval_manager", None),
        str(data.get("approval_id", "")),
        str(data.get("action", "")),
        guidance=data.get("guidance"),
    )
    await _emit_command_result_safe(
        session,
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    return True


async def handle_load_skill_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    await session._toggle_skill(str(data.get("skill_name", "")), activate=True)
    return True


async def handle_unload_skill_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    await session._toggle_skill(str(data.get("skill_name", "")), activate=False)
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
    from backend.services.skills_service import list_skill_marketplace

    marketplace_skills = list_skill_marketplace(session.skill_manager)
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
    "subagent.resume": handle_subagent_resume,
    "send_message": handle_send_message,
    "workflow.resume": handle_workflow_resume,
    "verification.run": handle_verification_run,
    "inspector.focus": handle_inspector_focus,
    "llm.model.set": handle_model_command,
    "read_artifact": handle_read_artifact_command,
    "approval.file_diff": handle_approval_file_diff_command,
    "interrupt": handle_interrupt_command,
    "user_message.queue.cancel": handle_user_message_queue_cancel,
    "user_message.queue.steer": handle_user_message_queue_steer,
    "task.stop": handle_task_stop,
    "approval.respond": handle_approval_respond,
    "load_skill": handle_load_skill_command,
    "unload_skill": handle_unload_skill_command,
    "skills.list": handle_skills_list,
    "skills.install": handle_skills_install,
    "skills.marketplace.list": handle_skills_marketplace_list,
    "commands.list": handle_commands_list,
    "llm.config.set": handle_llm_config_set,
}
