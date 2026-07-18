from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent


def diff_file_payload(file: Any) -> dict[str, Any]:
    return {
        "path": file.path,
        "patch": file.patch,
        "additions": file.additions,
        "deletions": file.deletions,
        "is_binary": file.is_binary,
    }


def working_tree_diff_event(result: Any, *, untracked: list[str]) -> AgentEvent:
    return AgentEvent(
        type="diff.git_working_tree",
        data={
            "files": [diff_file_payload(file) for file in result.files],
            "untracked": untracked,
            "total_additions": result.total_additions,
            "total_deletions": result.total_deletions,
        },
    )


def staged_diff_event(result: Any) -> AgentEvent:
    return AgentEvent(
        type="diff.git_staged",
        data={
            "files": [diff_file_payload(file) for file in result.files],
            "total_additions": result.total_additions,
            "total_deletions": result.total_deletions,
        },
    )


def git_file_action_event(event_type: str, *, path: str, ok: bool) -> AgentEvent:
    return AgentEvent(type=event_type, data={"path": path, "ok": bool(ok)})


def git_all_action_event(event_type: str, *, ok: bool) -> AgentEvent:
    return AgentEvent(type=event_type, data={"ok": bool(ok)})
