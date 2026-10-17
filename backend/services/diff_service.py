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
        "old_path": file.old_path,
        "status": file.status,
        "old_mode": file.old_mode,
        "new_mode": file.new_mode,
        "old_oid": file.old_oid,
        "new_oid": file.new_oid,
        "revision": file.revision,
    }


def working_tree_diff_event(result: Any, *, untracked: list[str], is_git_repo: bool = True,
                            untracked_files=(), hide_whitespace=False, snapshot_generation="", source="unstaged",
                            status="success", reason=None, comparison=None) -> AgentEvent:
    return AgentEvent(
        type="diff.git_working_tree",
        data={
            "is_git_repo": is_git_repo,
            "files": [diff_file_payload(file) for file in result.files],
            "untracked": untracked,
            "untracked_files": [diff_file_payload(file) for file in untracked_files],
            "source": source,
            "status": status,
            "reason": reason,
            "comparison": comparison or {},
            "hide_whitespace": hide_whitespace,
            "snapshot_generation": snapshot_generation,
            "total_additions": result.total_additions + sum(file.additions for file in untracked_files),
            "total_deletions": result.total_deletions + sum(file.deletions for file in untracked_files),
        },
    )


def staged_diff_event(result: Any, *, is_git_repo: bool = True, hide_whitespace=False, snapshot_generation="") -> AgentEvent:
    return AgentEvent(
        type="diff.git_staged",
        data={
            "is_git_repo": is_git_repo,
            "source": "staged",
            "hide_whitespace": hide_whitespace,
            "snapshot_generation": snapshot_generation,
            "files": [diff_file_payload(file) for file in result.files],
            "total_additions": result.total_additions,
            "total_deletions": result.total_deletions,
        },
    )


def git_file_action_event(event_type: str, *, path: str, ok: bool) -> AgentEvent:
    return AgentEvent(type=event_type, data={"path": path, "ok": bool(ok)})


def git_all_action_event(event_type: str, *, ok: bool) -> AgentEvent:
    return AgentEvent(type=event_type, data={"ok": bool(ok)})
