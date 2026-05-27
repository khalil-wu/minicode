from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.config import get_available_models, get_llm_provider

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


async def handle_plan_edit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.agent.plan import PLAN_REGISTRY
    plan_id = str(data.get("plan_id", "")).strip()
    action = str(data.get("action", "")).strip().lower()
    steps = data.get("steps") if isinstance(data.get("steps"), list) else None
    accept = bool(data.get("accept", False)) or action == "accept"
    if action == "reject":
        PLAN_REGISTRY.cancel(plan_id)
        plan = PLAN_REGISTRY.get(plan_id)
        if plan is None:
            await session._send_event(AgentEvent.error(f"Unknown plan '{plan_id}'", recoverable=True))
            return True
        await session._send_event(AgentEvent(type="plan.update", data=plan.to_payload()))
        return True
    plan = PLAN_REGISTRY.edit(plan_id, steps, accept=accept)
    if plan is None:
        await session._send_event(AgentEvent.error(f"Unknown plan '{plan_id}'", recoverable=True))
        return True
    await session._send_event(AgentEvent(type="plan.update", data=plan.to_payload()))
    return True


async def handle_task_edit(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    todo_id = str(data.get("todo_id", "")).strip()
    status = str(data.get("status", "")).strip()
    if not todo_id or status not in {"pending", "in_progress", "completed", "blocked"}:
        await session._send_event(AgentEvent.error("task.edit requires todo_id + valid status", recoverable=True))
        return True
    await session._send_event(AgentEvent.task_update(todo_id=todo_id, status=status, content=str(data.get("content", ""))))
    return True


async def handle_subagent_cancel(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    subagent_id = str(data.get("subagent_id", "")).strip()
    if not subagent_id:
        await session._send_event(AgentEvent.error("subagent_id is required", recoverable=True))
        return True
    await session._send_event(AgentEvent.subagent_done(subagent_id=subagent_id, error="cancelled by user"))
    return True


async def handle_inspector_focus(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    target_kind = str(data.get("target_kind", "")).strip() or "message"
    target_id = str(data.get("target_id", "")).strip()
    await session._send_event(
        AgentEvent.inspector_update(target_kind=target_kind, target_id=target_id, payload={"acknowledged": True})
    )
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
    if session._active_task_id:
        session.task_manager.cancel(session._active_task_id)
    task_was_running = False
    if session._active_run_task and not session._active_run_task.done():
        session._active_run_task.cancel()
        task_was_running = True
    await session._cancel_pending_approvals(reason="user_interrupted")
    session._active_task_id = None
    if not task_was_running:
        from backend.agent.message import AgentEvent
        await session._send_event(AgentEvent.done())
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
        approval_manager.remove_approval(approval_id)
        await session._emit_command_result("approval.respond", f"Approved: {approval.title}", level="success", data={"approval_id": approval_id, "action": "approve"})
    else:
        approval_manager.remove_approval(approval_id)
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
        get_llm_settings_payload,
        load_config,
        save_llm_settings,
        _normalize_provider,
    )

    raw_provider = str(data.get("provider", "")).strip()
    model_hint = str(data.get("model", "")).strip()
    reasoning_effort = str(data.get("reasoning_effort") or "").strip().lower()
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
                section["reasoning_effort"] = reasoning_effort
                save_llm_settings(saved_payload)
                session.config = load_config()
                saved_payload = get_llm_settings_payload()
        else:
            await session._send_event(
                AgentEvent(type="system_notice", data={"content": "Reasoning effort applies to OpenAI-compatible providers."})
            )

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
    await session._send_event(AgentEvent(type="system_notice", data={"content": notice}))
    await session._send_llm_state()
    return True


HANDLERS: dict[str, Any] = {
    "checkpoint.list": handle_checkpoint_list,
    "checkpoint.rewind": handle_checkpoint_rewind,
    "plan.edit": handle_plan_edit,
    "task.edit": handle_task_edit,
    "subagent.cancel": handle_subagent_cancel,
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
