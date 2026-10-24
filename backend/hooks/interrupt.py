"""Interrupt observers run after the turn journal commits, before public DONE."""
from __future__ import annotations

import json
from uuid import uuid4

from backend.agent.message import AgentEvent
from backend.hooks.manager import HookEvent


async def interrupt_hook_events(turn_context, *, transcript_path: str = "") -> list[AgentEvent]:
    run = turn_context.run_context
    manager = run.hook_manager
    if run.is_subagent or manager is None or not manager.has_hooks(HookEvent.INTERRUPT):
        return []
    context = manager._tool_context
    model = run.active_model_execution or run.model_execution
    result = await manager.run_interrupt(
        turn_id=str(turn_context.metadata.get("turn_id") or turn_context.metadata.get("run_id") or ""),
        model=model.model if model is not None else "",
        permission_mode=(
            "bypassPermissions"
            if context is not None and context.permission.approval_policy == "never" else "default"
        ),
        transcript_path=transcript_path,
    )
    events = [
        AgentEvent("system_notice", {"content": message, "source": "hook", "hook_event_name": "Interrupt"})
        for message in (result.system_message, *result.errors) if message
    ]
    for summary in result.run_summaries:
        events.append(AgentEvent.agent_item(
            id=f"interrupt_hook_{uuid4().hex}", kind="hook_response",
            content=json.dumps(summary, ensure_ascii=False), role="runtime", source="hook",
            status="failed" if summary["status"] == "failed" else "completed", title="Interrupt hook",
            visibility="timeline", default_collapsed=True,
            summary=summary["status_message"] or "Interrupt hook",
        ))
    return events
