from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.config import get_available_models
from backend.ws.utils import normalize_permission_mode

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def handle_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    if not conversation_id:
        await session._send_event(AgentEvent.error("No active conversation for checkpoint.list", recoverable=True))
        return True
    limit = int(data.get("limit", 50) or 50)
    records = session.checkpoint_manager.list_for_conversation(conversation_id, limit=max(1, min(limit, 200)))
    await session._send_ws_payload(
        {"type": "checkpoint.list", "conversation_id": conversation_id, "checkpoints": [record.to_dict() for record in records]},
        log_context="checkpoint.list",
    )
    return True


async def handle_checkpoint_rewind(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    checkpoint_id = str(data.get("checkpoint_id") or data.get("id") or "").strip()
    if not checkpoint_id:
        await session._send_event(AgentEvent.error("checkpoint_id is required", recoverable=True))
        return True
    try:
        record = await session.checkpoint_manager.rewind(checkpoint_id)
    except Exception as exc:
        await session._send_event(AgentEvent.error(f"Checkpoint rewind failed: {exc}", recoverable=True))
        return True
    await session._send_ws_payload(
        {"type": "checkpoint.rewound", "conversation_id": record.conversation_id, "checkpoint": record.to_dict()},
        log_context="checkpoint.rewound",
    )
    return True


async def handle_run_checkpoint_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.checkpoint import get_checkpoint_dir
    from backend.agent.runtime import default_runtime
    import json

    session_id = str(data.get("session_id") or session.session_id or "").strip()
    conversation_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    checkpoints: list[dict[str, Any]] = []
    if session_id:
        checkpoint_dir = get_checkpoint_dir(session_id)
        for path in sorted(checkpoint_dir.glob("*.json"), reverse=True)[:50]:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if conversation_id and str(payload.get("conversation_id") or "").strip() != conversation_id:
                continue
            checkpoints.append(
                {
                    "run_id": str(payload.get("run_id") or ""),
                    "session_id": str(payload.get("session_id") or session_id),
                    "conversation_id": str(payload.get("conversation_id") or ""),
                    "iteration": int(payload.get("iterations") or 0),
                    "iterations": int(payload.get("iterations") or 0),
                    "stopped_reason": payload.get("stopped_reason"),
                    "timestamp": payload.get("timestamp"),
                    "created_at": payload.get("timestamp"),
                }
            )
    runtime_snapshot = default_runtime().list_runs(conversation_id=conversation_id, include_subagents=True)
    await session._send_ws_payload(
        {
            "type": "checkpoint.run.list",
            "session_id": session_id,
            "conversation_id": conversation_id,
            "checkpoints": checkpoints,
            **runtime_snapshot,
        },
        log_context="checkpoint.run.list",
    )
    return True


async def handle_task_edit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    todo_id = str(data.get("todo_id", "")).strip()
    status = str(data.get("status", "")).strip()
    if not todo_id or status not in {"pending", "in_progress", "completed", "blocked"}:
        await session._send_event(AgentEvent.error("task.edit requires todo_id + valid status", recoverable=True))
        return True
    event = AgentEvent.task_update(todo_id=todo_id, status=status, content=str(data.get("content", "")))
    if session.active_conversation_id:
        event.data["conversation_id"] = session.active_conversation_id
    await session._send_event(event)
    return True


def _normalize_plan_steps(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []
    valid_statuses = {"pending", "running", "done", "skipped", "failed"}
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            continue
        title = str(raw_step.get("title") or raw_step.get("step") or "").strip()
        if not title:
            continue
        status = str(raw_step.get("status") or "pending").strip()
        steps.append({
            "id": str(raw_step.get("id") or f"step-{index}"),
            "title": title,
            "status": status if status in valid_statuses else "pending",
            **({"detail": str(raw_step.get("detail"))} if raw_step.get("detail") else {}),
        })
    return steps


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
        await session._send_event(AgentEvent.error("No active conversation for plan.edit", recoverable=True))
        return

    running_for_target = session._conversation_run_tasks.get(target_conversation_id)
    if running_for_target and not running_for_target.done():
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

    managed_run = session.task_manager.create(
        "agent.run",
        session._run_agent(
            message,
            conversation_id=target_conversation_id,
            metadata={"plan_id": plan_id, "plan_action": action},
        ),
    )
    session._conversation_run_tasks[target_conversation_id] = managed_run.task
    session._conversation_run_task_ids[target_conversation_id] = managed_run.id
    if target_conversation_id == session.active_conversation_id:
        session._active_run_task = managed_run.task
        session._active_task_id = managed_run.id
    session._schedule_task_runtime_update()

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
            if session._conversation_run_tasks.get(target_conversation_id) is managed_run.task:
                session._conversation_run_tasks.pop(target_conversation_id, None)
            if session._conversation_run_task_ids.get(target_conversation_id) == managed_run.id:
                session._conversation_run_task_ids.pop(target_conversation_id, None)
            session._conversation_run_locks.pop(target_conversation_id, None)
            if session._active_run_task is managed_run.task:
                session._active_run_task = None
            if session._active_task_id == managed_run.id:
                session._active_task_id = None
            session._schedule_task_runtime_update()

    asyncio.create_task(_wait_and_cleanup())


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
    plan_id = str(data.get("plan_id") or data.get("planId") or "plan").strip() or "plan"
    action = str(data.get("action") or "").strip().lower()
    if action not in {"accept", "reject"}:
        await session._send_event(AgentEvent.error("plan.edit requires action 'accept' or 'reject'", recoverable=True))
        return True

    steps = _normalize_plan_steps(data.get("steps"))
    current_step = int(data.get("current_step") or data.get("currentStep") or 0)
    status = "accepted" if action == "accept" else "cancelled"
    event = AgentEvent.plan_updated(
        plan_id=plan_id,
        steps=steps,
        status=status,
        current_step=current_step,
        explanation="Plan accepted by user." if action == "accept" else "Plan rejected by user.",
    )
    if session.active_conversation_id:
        event.data["conversation_id"] = session.active_conversation_id
    await session._send_event(event)

    if action == "accept":
        await _exit_plan_mode_for_accept(session)
        await _start_plan_followup_run(
            session,
            plan_id=plan_id,
            action=action,
            message=(
                "用户已批准当前执行计划。请按这个计划开始实施；先把第一步标记为 in_progress，"
                "然后执行、验证，并在推进时持续更新计划状态。"
            ),
        )
    else:
        await session._emit_command_result(
            "plan.edit",
            "Plan rejected. Ask the agent for a revised plan with the desired changes.",
            level="warning",
            data={"plan_id": plan_id, "action": action},
        )
    return True


async def handle_subagent_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    subagent_id = str(data.get("subagent_id", "")).strip()
    if not subagent_id:
        await session._send_event(AgentEvent.error("subagent_id is required", recoverable=True))
        return True
    cancelled = False
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
    event = AgentEvent.subagent_done(subagent_id=subagent_id, error="cancelled by user")
    event.data["cancel_requested"] = True
    event.data["cancelled"] = cancelled
    if session.active_conversation_id:
        event.data["conversation_id"] = session.active_conversation_id
    await session._send_event(event)
    return True


async def handle_agent_resume(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.checkpoint import load_latest_run_checkpoint

    session_id = session.session_id
    if not session_id:
        await session._emit_command_result("agent.resume", "No active session ID. Cannot resume.", level="error")
        return True

    checkpoint = load_latest_run_checkpoint(session_id)
    if checkpoint is None:
        await session._send_ws_payload(
            {
                "type": "checkpoint.run.resume",
                "resumed": False,
                "message": "No incomplete run checkpoint found.",
            },
            log_context="checkpoint.run.resume",
        )
        return True

    conversation_id = str(data.get("conversation_id") or checkpoint.conversation_id or session.active_conversation_id or "").strip()
    if not conversation_id:
        await session._emit_command_result("agent.resume", "No active conversation. Cannot resume.", level="error")
        return True

    await session._send_ws_payload(
        {
            "type": "checkpoint.run.resume",
            "resumed": True,
            "session_id": session_id,
            "conversation_id": conversation_id,
            "run_id": checkpoint.run_id,
            "iteration": checkpoint.iterations,
            "stopped_reason": checkpoint.stopped_reason,
        },
        log_context="checkpoint.run.resume",
    )
    await session._run_agent(
        user_message=checkpoint.user_message,
        conversation_id=conversation_id,
        metadata={
            "resume_from_checkpoint": True,
            "run_id": checkpoint.run_id,
            "conversation_id": conversation_id,
        },
    )
    return True


async def handle_verification_run(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.loop import _run_verify_command
    from backend.agent.runtime import new_run_id

    config = getattr(session, "config", None)
    command = str(getattr(getattr(config, "agent", None), "verify_command", "") or "").strip()
    supplied_command = str(data.get("command") or "").strip()
    if supplied_command and supplied_command != command:
        await session._emit_command_result(
            "verification.run",
            "Ad hoc verification commands are not allowed. Configure agent.verify_command instead.",
            level="error",
        )
        return True
    if not command:
        await session._emit_command_result("verification.run", "No verify command configured.", level="warning")
        return True
    workspace_root = session._workspace_root_for_conversation(
        session.conversation_repo.get_conversation(session.active_conversation_id or "")
    ) if session.active_conversation_id else None
    if workspace_root is None:
        workspace_root = getattr(session, "workspace_root", None)
    if workspace_root is None:
        await session._emit_command_result("verification.run", "No workspace is available for verification.", level="error")
        return True

    run_id = new_run_id("verify")
    await session._send_event(AgentEvent.verification_started(run_id, command=command, conversation_id=session.active_conversation_id or ""))
    try:
        timeout = float(data.get("timeout_seconds") or getattr(getattr(config, "agent", None), "verify_timeout_seconds", 120.0) or 120.0)
    except (TypeError, ValueError):
        timeout = float(getattr(getattr(config, "agent", None), "verify_timeout_seconds", 120.0) or 120.0)
    timeout = max(1.0, min(timeout, 600.0))
    passed, output = await _run_verify_command(command, workspace_root, timeout)
    await session._send_event(
        AgentEvent.verification_result(
            run_id,
            passed=passed,
            output=output,
            command=command,
            conversation_id=session.active_conversation_id or "",
        )
    )
    await session._emit_command_result(
        "verification.run",
        "Verification passed." if passed else "Verification failed.",
        level="success" if passed else "error",
        data={"run_id": run_id, "passed": passed, "output": output},
    )
    return True


async def handle_inspector_focus(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    target_kind = str(data.get("target_kind", "")).strip() or "message"
    target_id = str(data.get("target_id", "")).strip()
    event = AgentEvent.inspector_update(target_kind=target_kind, target_id=target_id, payload={"acknowledged": True})
    if session.active_conversation_id:
        event.data["conversation_id"] = session.active_conversation_id
    await session._send_event(event)
    return True


async def handle_model_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    requested_model = str(data.get("model", "")).strip()
    if not requested_model:
        await session._send_event(AgentEvent.error("Model name is required", recoverable=True))
        return True
    await session._set_selected_model(requested_model, manual_override=True)
    await session._send_llm_state()
    return True


async def handle_read_artifact_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    artifact_id = str(data.get("artifact_id", ""))
    content = session.artifact_store.get(artifact_id)
    if content is None:
        content = session.attachment_store.get(artifact_id)
    if content is None:
        await session._send_event(
            AgentEvent.error(f"Artifact '{artifact_id}' does not exist or has been cleared", recoverable=True)
        )
        return True
    meta = session.artifact_store.get_meta(artifact_id)
    media_type = "image/png" if getattr(meta, "type", "") == "image" else ""
    await session._send_event(
        AgentEvent(
            type="artifact_content",
            data={
                "artifact_id": artifact_id,
                "content": content,
                "preview": session.artifact_store.get_preview(artifact_id) or session.attachment_store.get_preview(artifact_id) or "",
                **({"media_type": media_type, "url": f"data:{media_type};base64,{content}"} if media_type else {}),
            },
        )
    )
    return True


async def handle_approval_file_diff_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    tool_call_id = str(data.get("tool_call_id", "")).strip()
    path = str(data.get("path", "")).strip()
    if not tool_call_id or not path:
        await session._send_event(AgentEvent.error("Approval file diff requires tool_call_id and path", recoverable=True))
        return True

    payload = session._approval_diff_cache.get(tool_call_id)
    files = payload.get("files") if isinstance(payload, dict) else None
    if not isinstance(files, list):
        await session._send_event(AgentEvent.error(f"Approval diff '{tool_call_id}' is no longer available", recoverable=True))
        return True

    matched = next((item for item in files if isinstance(item, dict) and str(item.get("path", "")).strip() == path), None)
    if matched is None:
        await session._send_event(AgentEvent.error(f"Approval diff file '{path}' was not found", recoverable=True))
        return True

    patch = matched.get("patch")
    if not isinstance(patch, str):
        await session._send_event(AgentEvent.error(f"Approval diff patch for '{path}' is unavailable", recoverable=True))
        return True

    await session._send_ws_payload(
        {
            "type": "approval.file_diff", "conversation_id": session.active_conversation_id,
            "tool_call_id": tool_call_id, "path": path, "patch": patch,
            "is_large": bool(matched.get("is_large")), "is_truncated": bool(matched.get("is_truncated")),
        },
        log_context="approval.file_diff",
    )
    return True


async def handle_interrupt_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    session._interrupted = True
    target_conversation_id = str(data.get("conversation_id") or data.get("conversationId") or "").strip()
    target_task_id = (
        getattr(session, "_conversation_run_task_ids", {}).get(target_conversation_id)
        if target_conversation_id
        else session._active_task_id
    )
    if target_task_id:
        session.task_manager.cancel(target_task_id)
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
    else:
        session._active_task_id = None
    if not task_was_running:
        from backend.agent.message import AgentEvent
        done = AgentEvent.done()
        if target_conversation_id:
            done.data["conversation_id"] = target_conversation_id
        await session._send_event(done)
    return True


async def handle_task_stop(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    task_id = str(data.get("task_id", "")).strip()
    if not task_id:
        await session._emit_command_result("task.stop", "Task ID is required", level="error")
        return True
    product_manager = getattr(session, "_product_task_manager", None)
    if product_manager is None:
        await session._emit_command_result("task.stop", "Task manager not available", level="error")
        return True
    snapshot = product_manager.cancel_task(task_id)
    if snapshot is None:
        await session._emit_command_result("task.stop", f"Task '{task_id}' not found or cannot be stopped", level="warning")
        return True
    await session._emit_command_result("task.stop", f"Task '{snapshot.label}' stopped", level="success", data={"task_id": task_id, "task": snapshot.to_dict()})
    return True


async def handle_approval_respond(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    approval_id = str(data.get("approval_id", "")).strip()
    action = str(data.get("action", "")).strip().lower()
    guidance = data.get("guidance")

    if not approval_id:
        await session._emit_command_result("approval.respond", "Approval ID is required", level="error")
        return True
    if action not in ("approve", "reject"):
        await session._emit_command_result("approval.respond", "Action must be 'approve' or 'reject'", level="error")
        return True

    approval_manager = getattr(session, "_product_approval_manager", None)
    if approval_manager is None:
        await session._emit_command_result("approval.respond", "Approval manager not available", level="error")
        return True
    approval = approval_manager.get_approval(approval_id)
    if approval is None:
        await session._emit_command_result("approval.respond", f"Approval '{approval_id}' not found", level="warning")
        return True

    if action == "approve":
        approval_manager.resolve_approval(approval_id, "approve")
        await session._emit_command_result("approval.respond", f"Approved: {approval.title}", level="success", data={"approval_id": approval_id, "action": "approve"})
    else:
        approval_manager.resolve_approval(approval_id, "reject", guidance=guidance if isinstance(guidance, str) else None)
        guidance_text = f" with guidance: {guidance}" if guidance else ""
        await session._emit_command_result("approval.respond", f"Rejected: {approval.title}{guidance_text}", level="success", data={"approval_id": approval_id, "action": "reject", "guidance": guidance})
    return True


async def handle_load_skill_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    await session._toggle_skill(str(data.get("skill_name", "")), activate=True)
    return True


async def handle_unload_skill_command(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    await session._toggle_skill(str(data.get("skill_name", "")), activate=False)
    return True


async def handle_skills_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    skills: list[dict[str, Any]] = []
    if session.skill_manager:
        loader = getattr(session.skill_manager, "_loader", None)
        if loader:
            for name in loader.list_skill_names():
                meta = loader.get_meta(name)
                entry: dict[str, Any] = {
                    "name": name,
                    "description": getattr(meta, "description", "") if meta else "",
                }
                if meta:
                    entry.update({
                        "version": getattr(meta, "version", ""),
                        "triggers": getattr(meta, "triggers", []),
                        "tools_required": getattr(meta, "tools_required", []),
                        "source_level": getattr(meta, "source_level", "builtin"),
                    })
                skills.append(entry)
    await session._send_ws_payload({"type": "skills.list", "skills": skills}, log_context="skills.list")
    return True


async def handle_skills_install(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    name = str(data.get("name", "")).strip()
    if not name:
        await session._send_event(AgentEvent.error("Skill name is required", recoverable=True))
        return True
    if session.skill_manager:
        try:
            await session.skill_manager.install(name)
            await session._send_event(AgentEvent(type="system_notice", data={"content": f"Skill '{name}' installed successfully"}))
            await handle_skills_list(session, {})
        except Exception as exc:
            await session._send_event(AgentEvent.error(f"Failed to install skill '{name}': {exc}", recoverable=True))
    else:
        await session._send_event(AgentEvent(type="system_notice", data={"content": f"Skill '{name}' registered (skill manager not available)"}))
    return True


async def handle_skills_marketplace_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.skills.marketplace import CURATED_SKILLS

    installed_names: set[str] = set()
    if session.skill_manager:
        loader = getattr(session.skill_manager, "_loader", None)
        if loader:
            installed_names = set(loader.list_skill_names())

    marketplace_skills = []
    for name, info in CURATED_SKILLS.items():
        marketplace_skills.append({
            "name": name,
            "title": info.get("title", name),
            "description": info.get("description", ""),
            "triggers": info.get("triggers", []),
            "installed": name in installed_names,
        })
    await session._send_ws_payload({"type": "skills.marketplace.list", "skills": marketplace_skills}, log_context="skills.marketplace.list")
    return True


async def handle_commands_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.commands.catalog import get_enabled_composer_command_catalog
    commands = get_enabled_composer_command_catalog()
    await session._send_ws_payload({"type": "commands.list", "commands": commands}, log_context="commands.list")
    return True


async def handle_llm_config_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.config import (
        active_provider_supports_reasoning_effort,
        get_llm_settings_payload,
        load_config,
        save_llm_settings,
        _normalize_provider,
    )

    raw_provider = str(data.get("provider", "")).strip()
    model_hint = str(data.get("model", "")).strip()
    reasoning_effort = str(data.get("reasoning_effort") or "").strip().lower()
    reasoning_effort_requested = bool(reasoning_effort)
    if reasoning_effort and reasoning_effort not in {"low", "medium", "high", "max"}:
        await session._send_event(AgentEvent.error("reasoning_effort must be low, medium, high, or max", recoverable=True))
        return True

    provider = _normalize_provider(raw_provider) if raw_provider else "openai"

    session.config = load_config()
    saved_payload = get_llm_settings_payload()
    if reasoning_effort:
        target_provider = str(saved_payload.get("provider") or provider)
        if target_provider in {"openai", "custom"}:
            section = saved_payload.get(target_provider)
            if isinstance(section, dict):
                if not active_provider_supports_reasoning_effort(saved_payload):
                    await session._send_event(
                        AgentEvent(
                            type="system_notice",
                            data={
                                "content": (
                                    "Reasoning effort was not applied because the active provider "
                                    "uses Chat Completions. Switch to a Responses-compatible model/API format."
                                )
                            },
                        )
                    )
                    reasoning_effort = ""
                else:
                    section["reasoning_effort"] = reasoning_effort
                    save_llm_settings(saved_payload)
                    session.config = load_config()
                    saved_payload = get_llm_settings_payload()
        else:
            await session._send_event(
                AgentEvent(type="system_notice", data={"content": "Reasoning effort applies to OpenAI-compatible providers."})
            )
            reasoning_effort = ""

    session.provider = str(saved_payload.get("provider") or provider)
    section = saved_payload.get(session.provider)
    if not isinstance(section, dict):
        section = {}
    session.available_models = list(section.get("available_models") or get_available_models(session.provider))
    session.selected_model = str(saved_payload.get("active_model") or section.get("model") or "").strip()
    session._model_override_active = False
    if session.selected_model and session.selected_model not in session.available_models:
        session.available_models.insert(0, session.selected_model)
    if not session.available_models:
        session.available_models = [session.selected_model] if session.selected_model else ["default"]

    from backend.llm.model_registry import create_session_llm
    session.llm = create_session_llm(session.config, model_override=session.selected_model)
    session.context_builder._llm = session.llm

    notice = f"Provider updated to {provider} ({session.selected_model})"
    if reasoning_effort:
        notice = f"Reasoning effort set to {reasoning_effort}"
    if reasoning_effort or not reasoning_effort_requested:
        await session._send_event(AgentEvent(type="system_notice", data={"content": notice}))
    await session._send_llm_state()
    return True


HANDLERS: dict[str, Any] = {
    "checkpoint.list": handle_checkpoint_list,
    "checkpoint.rewind": handle_checkpoint_rewind,
    "checkpoint.run.list": handle_run_checkpoint_list,
    "task.edit": handle_task_edit,
    "plan.edit": handle_plan_edit,
    "agent.resume": handle_agent_resume,
    "subagent.cancel": handle_subagent_cancel,
    "verification.run": handle_verification_run,
    "inspector.focus": handle_inspector_focus,
    "llm.model.set": handle_model_command,
    "read_artifact": handle_read_artifact_command,
    "approval.file_diff": handle_approval_file_diff_command,
    "interrupt": handle_interrupt_command,
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
