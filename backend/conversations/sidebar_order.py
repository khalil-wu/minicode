"""Source sidebar occurrence order; independent from pins and execution cwd."""
from __future__ import annotations

from typing import Any

from backend.atomic_io import canonical_file_path_key


def empty_sidebar_order() -> dict[str, Any]:
    return {"revision": 0, "sort_mode": "updated_at", "project_order": [],
            "project_thread_orders": {}, "recent_thread_ids": []}


def project_key(project_id: str) -> str:
    return canonical_file_path_key(project_id).replace("\\", "/").rstrip("/") if project_id else ""


def conversation_project_key(conversation: Any) -> str:
    membership = conversation.project_id
    return project_key(membership if membership is not None else conversation.worktree_path or conversation.workspace_root)


def ordered_members(saved: list[str], current: list[str]) -> list[str]:
    membership = set(current)
    return [identifier for identifier in saved if identifier in membership] + [identifier for identifier in current if identifier not in saved]


def replace_visible_slots(current: list[str], visible_ids: list[str], moved_id: str, before_id: str | None) -> list[str]:
    visible = set(visible_ids)
    if (len(visible) != len(visible_ids) or not visible.issubset(current) or moved_id not in visible
            or before_id is not None and before_id not in visible):
        raise ValueError("The sidebar items changed before this reorder")
    if before_id == moved_id:
        return current
    next_visible = [identifier for identifier in visible_ids if identifier != moved_id]
    next_visible.insert(len(next_visible) if before_id is None else next_visible.index(before_id), moved_id)
    replacements = iter(next_visible)
    return [next(replacements) if identifier in visible else identifier for identifier in current]


def thread_orders(conversations: list[Any], saved: dict[str, Any]) -> tuple[list[str], dict[str, list[str]]]:
    # Archived and pinned rows retain their occurrence slots while hidden.
    threads = sorted((item for item in conversations if item.conversation_type == "main"),
                     key=lambda item: (item.updated_at, item.id), reverse=True)
    projects: dict[str, list[str]] = {}
    for item in threads:
        key = conversation_project_key(item)
        if key:
            projects.setdefault(key, []).append(item.id)
    return (ordered_members(saved["recent_thread_ids"], [item.id for item in threads]),
            {key: ordered_members(saved["project_thread_orders"].get(key, []), identifiers) for key, identifiers in projects.items()})
