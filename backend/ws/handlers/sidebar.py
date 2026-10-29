"""Codex desktop pin/read state and thread project-membership commands."""
from __future__ import annotations

import asyncio
from typing import Any

from backend.async_cleanup import to_thread_cancel_safe
from backend.atomic_io import canonical_file_path_key
from backend.workspace.recent_projects import RecentProjectStore
from backend.ws.command_results import emit_command_error
from backend.ws.handlers.conversation import _broadcast_conversation_lists
from backend.ws.conversation_activity import live_sessions
from backend.ws.lifecycle_scope import handoff_command_lifecycle_prefix


async def _publish(session, command: str, record) -> bool:
    if record is None:
        await emit_command_error(session, command, "Conversation not found")
        return True
    errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(command, "Sidebar metadata updated.", level="warning" if errors else "success", data={
        "conversation_id": record.id, "revision": record.revision, "pinned": record.pinned,
        "pinned_at": record.pinned_at, "has_unread_turn": record.has_unread_turn, "project_id": record.project_id,
        "projection_errors": errors,
    })
    return True


async def handle_pin(session, data: dict[str, Any]) -> bool:
    if not isinstance(data["pinned"], bool):
        raise ValueError("pinned must be a boolean")
    record = await to_thread_cancel_safe(session.conversation_repo.set_conversation_pinned, data["conversation_id"], data["pinned"])
    return await _publish(session, "conversation.pin.set", record)


async def handle_read_state(session, data: dict[str, Any]) -> bool:
    if not isinstance(data["has_unread_turn"], bool):
        raise ValueError("has_unread_turn must be a boolean")
    record = await to_thread_cancel_safe(session.conversation_repo.set_conversation_read_state, data["conversation_id"], data["has_unread_turn"])
    if record is None:
        await emit_command_error(session, "conversation.read_state.set", "Conversation not found")
        return True
    # The mutation is durable. A read receipt changes no navigation, ordering,
    # or transcript; do not hold the next send behind full list hydration.
    await handoff_command_lifecycle_prefix(session.session_id)
    payload = {
        "type": "conversation.summary.updated", "conversation_id": record.id,
        "revision": record.revision, "summary": record.summary, "title": record.title,
        "updated_at": record.updated_at, "has_unread_turn": record.has_unread_turn,
        "memory_mode": record.memory_mode, "memory_polluted": record.memory_polluted,
        "memory_pollution_sources": list(record.memory_pollution_sources),
    }
    readers = [owner for owner in live_sessions(session) if owner.is_connected]
    results = await asyncio.gather(*(owner.send_payload(payload, log_context="conversation.summary.updated",
        wait_for_delivery=False) for owner in readers), return_exceptions=True)
    errors = [owner.session_id for owner, result in zip(readers, results) if isinstance(result, BaseException) or not result]
    await session.emit_command_result("conversation.read_state.set", "Sidebar metadata updated.",
        level="warning" if errors else "success", data={"conversation_id": record.id,
            "revision": record.revision, "has_unread_turn": record.has_unread_turn, "projection_errors": errors})
    return True


async def handle_pin_reorder(session, data: dict[str, Any]) -> bool:
    command = "conversation.pin.reorder"
    try:
        identifiers = await to_thread_cancel_safe(session.conversation_repo.reorder_pinned_conversations,
            data["moved_conversation_id"], data["before_conversation_id"], data["visible_conversation_ids"])
    except ValueError as error:
        await emit_command_error(session, command, error)
        return True
    errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(command, "Pinned order updated.", level="warning" if errors else "success",
        data={"pinned_conversation_ids": identifiers, "pinned_sort_mode": "manual", "projection_errors": errors})
    return True


async def handle_project(session, data: dict[str, Any]) -> bool:
    project_id = data["project_id"]
    if not isinstance(project_id, str):
        raise ValueError("project_id must be a string")
    if project_id:
        # Source checks that the requested project exists. Membership does not
        # register an execution root or grant file access to its directory.
        projects = await to_thread_cancel_safe(RecentProjectStore().list)
        project = next((item for item in projects if canonical_file_path_key(item.path) == canonical_file_path_key(project_id)), None)
        if project is None:
            await emit_command_error(session, "conversation.project.set", f"Project not found: {project_id}", data={"conversation_id": data["conversation_id"]})
            return True
        project_id = project.path
    record = await to_thread_cancel_safe(session.conversation_repo.set_conversation_project, data["conversation_id"], project_id)
    return await _publish(session, "conversation.project.set", record)


async def _publish_order(session, command: str) -> bool:
    errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(command, "", level="warning" if errors else "success",
        data={"projection_errors": errors})
    return True


async def handle_sort_mode(session, data: dict[str, Any]) -> bool:
    command = "sidebar.sort.set"
    try:
        await to_thread_cancel_safe(session.conversation_repo.set_sidebar_sort_mode,
            data["mode"], data["project_thread_seeds"])
    except ValueError as error:
        await emit_command_error(session, command, error)
        return True
    return await _publish_order(session, command)


async def handle_project_reorder(session, data: dict[str, Any]) -> bool:
    command = "sidebar.project.reorder"
    opened = await to_thread_cancel_safe(RecentProjectStore().list)
    try:
        await to_thread_cancel_safe(session.conversation_repo.reorder_sidebar_projects,
            data["moved_project_id"], data["before_project_id"], data["visible_project_ids"], [item.path for item in opened])
    except ValueError as error:
        await emit_command_error(session, command, error)
        return True
    return await _publish_order(session, command)


async def _handle_thread_reorder(session, data: dict[str, Any], *, command: str, project_id: str | None) -> bool:
    try:
        await to_thread_cancel_safe(session.conversation_repo.reorder_sidebar_threads,
            project_id, data["moved_conversation_id"], data["before_conversation_id"], data["visible_conversation_ids"])
    except ValueError as error:
        await emit_command_error(session, command, error)
        return True
    return await _publish_order(session, command)


async def handle_project_thread_reorder(session, data: dict[str, Any]) -> bool:
    return await _handle_thread_reorder(session, data, command="sidebar.project.thread.reorder", project_id=data["project_id"])


async def handle_recent_reorder(session, data: dict[str, Any]) -> bool:
    return await _handle_thread_reorder(session, data, command="sidebar.recent.reorder", project_id=None)


HANDLERS = {
    "conversation.pin.set": handle_pin,
    "conversation.pin.reorder": handle_pin_reorder,
    "conversation.read_state.set": handle_read_state,
    "conversation.project.set": handle_project,
    "sidebar.sort.set": handle_sort_mode,
    "sidebar.project.reorder": handle_project_reorder,
    "sidebar.project.thread.reorder": handle_project_thread_reorder,
    "sidebar.recent.reorder": handle_recent_reorder,
}
