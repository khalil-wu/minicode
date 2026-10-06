"""Conversation-owned live resources shared by mutation and recovery entry points."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession


def live_sessions(session: "WebSocketSession") -> list["WebSocketSession"]:
    manager = session.ws_manager
    if manager is None:
        return [session]
    sessions = list(manager.iter_sessions())
    return sessions if session in sessions else [session, *sessions]


def conversation_activity_blockers(session: "WebSocketSession", conversation_id: str) -> dict[str, int]:
    """Count the live resources owned by a conversation before a mutation."""
    from backend.preview.launcher import running_preview_processes
    from backend.api import _state as api_state
    from backend.tasks import scheduler as scheduler_module

    owners = live_sessions(session)
    bootstrap = api_state.bootstrap
    scheduler = (bootstrap.task_scheduler if bootstrap is not None else None) or scheduler_module._GLOBAL_SCHEDULER
    return {
        "background_commands": sum(
            1 for owner in owners
            for command in owner.background_manager.list_commands(include_completed=True, conversation_id=conversation_id)
            if command["status"] == "running" or command.get("cleanup_pending")
        ),
        "terminal_sessions": sum(
            1 for owner in owners
            for terminal in owner.terminal_manager.list_sessions_for_conversation(conversation_id)
            if terminal.is_alive or terminal.cleanup_pending
        ),
        "preview_processes": sum(len(running_preview_processes(session_id=owner.session_id, conversation_id=conversation_id)) for owner in owners),
        "scheduled_tasks": sum(1 for task in scheduler.list_tasks() if task["conversation_id"] == conversation_id and task["enabled"]) if scheduler is not None else 0,
        "subagents": live_subagent_count(conversation_id),
    }


def live_subagent_count(conversation_id: str) -> int:
    from backend.agent.runtime import default_runtime_if_initialized

    runtime = default_runtime_if_initialized()
    if runtime is None:
        return 0
    children = runtime.list_runs(conversation_id=conversation_id, include_subagents=True)["subagents"]
    return sum(
        1 for child in children
        if child.get("status") in {"running", "pending", "blocked"}
        or child.get("cleanup_pending")
        or child.get("background_task") in {"running", "queued"}
    )
