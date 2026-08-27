"""Detached Agent execution for project-scoped scheduled tasks.

The WebSocket session runner is intentionally tied to a live client.  A
scheduled task must still work after the desktop window is closed, so this
module runs the same QueryEngine path without borrowing a connected session.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.agent.conversation_query_guard import (
    ConversationQueryClaim,
    conversation_query_guards,
)
from backend.conversations.repository import ConversationRepository
from backend.services.chat_api_service import run_owned_rest_chat
from backend.services.workspace_service import git_branch_for, main_worktree_root


async def run_scheduled_task(
    task: Any,
    run: Any,
    *,
    bootstrap: Any,
) -> dict[str, Any]:
    """Run one task and persist a self-contained conversation transcript."""

    workspace_root_text = str(getattr(task, "workspace_root", "") or "").strip()
    workspace_root = Path(workspace_root_text).expanduser() if workspace_root_text else None
    if workspace_root is None or not workspace_root.exists() or not workspace_root.is_dir():
        return {
            "status": "failed",
            "workspace_root": workspace_root_text,
            "error": "Scheduled task workspace is unavailable. Open the project and update the task.",
        }

    repository = ConversationRepository()
    from backend.services.scheduler_service import scheduled_permission_mode

    # Scheduled runs use the same explicit MiniCode permission tokens.
    permission_mode = scheduled_permission_mode(getattr(task, "permission_mode", "confirm"))
    requested_conversation_id = str(getattr(task, "conversation_id", "") or "").strip()
    conversation = repository.get_conversation(requested_conversation_id) if requested_conversation_id else None
    invalid_conversation_reason = ""
    if requested_conversation_id and conversation is None:
        invalid_conversation_reason = "The heartbeat conversation no longer exists."
    if conversation is not None:
        bound_text = str(getattr(conversation, "workspace_root", "") or "").strip()
        bound_root = Path(bound_text).expanduser() if bound_text else None
        if bound_root is None or main_worktree_root(bound_root) != main_worktree_root(workspace_root):
            invalid_conversation_reason = "The heartbeat conversation belongs to another project."
            conversation = None
        elif getattr(conversation, "archived", False):
            invalid_conversation_reason = "The heartbeat conversation is archived."
            conversation = None
    if requested_conversation_id and conversation is None:
        return {
            "status": "failed",
            "workspace_root": str(workspace_root.resolve()),
            "conversation_id": requested_conversation_id,
            "error": invalid_conversation_reason or "The heartbeat conversation is unavailable.",
        }
    if conversation is None:
        conversation = repository.create_conversation(
            title=f"定时任务：{str(getattr(task, 'name', '') or '未命名任务')}",
            workspace_root=str(workspace_root.resolve()),
            git_branch=git_branch_for(workspace_root),
            permission_mode=permission_mode,
        )

    query_guards = conversation_query_guards()
    query_claim = query_guards.try_start(
        conversation.id,
        owner_id=f"scheduled:{str(getattr(run, 'id', '') or '')}",
    )
    if query_claim is None:
        return {
            "status": "failed",
            "workspace_root": str(workspace_root.resolve()),
            "conversation_id": conversation.id,
            "error": "The heartbeat conversation already has an active turn.",
        }
    try:
        return await _run_scheduled_task_owned(
            task,
            run,
            bootstrap=bootstrap,
            repository=repository,
            conversation=conversation,
            workspace_root=workspace_root,
            permission_mode=permission_mode,
            query_claim=query_claim,
        )
    finally:
        query_guards.end(query_claim)


async def _run_scheduled_task_owned(
    task: Any,
    run: Any,
    *,
    bootstrap: Any,
    repository: ConversationRepository,
    conversation: Any,
    workspace_root: Path,
    permission_mode: str,
    query_claim: ConversationQueryClaim,
) -> dict[str, Any]:
    execution_root = workspace_root.resolve()
    isolation = str(getattr(task, "isolation", "worktree") or "worktree").strip().lower()
    if isolation == "worktree":
        existing_worktree_text = str(getattr(conversation, "worktree_path", "") or "").strip()
        existing_worktree = Path(existing_worktree_text).expanduser() if existing_worktree_text else None
        existing_matches_project = False
        if existing_worktree is not None and existing_worktree.is_dir():
            try:
                existing_matches_project = main_worktree_root(existing_worktree) == main_worktree_root(workspace_root)
            except (OSError, RuntimeError, ValueError):
                existing_matches_project = False
        if getattr(conversation, "git_isolated", False) and existing_matches_project and existing_worktree is not None:
            execution_root = existing_worktree.resolve()
        else:
            from backend.services.conversation_payload_service import create_isolated_worktree_binding

            creation = await asyncio.to_thread(
                create_isolated_worktree_binding,
                conversation,
                current_workspace_root=workspace_root,
                main_worktree_root=main_worktree_root,
            )
            if not creation.created:
                error = getattr(creation.error_event, "data", {}).get("message") if creation.error_event else ""
                return {
                    "status": "failed",
                    "workspace_root": str(workspace_root.resolve()),
                    "conversation_id": conversation.id,
                    "error": str(error or "Unable to create an isolated worktree for the scheduled task."),
                }
            updated = repository.update_workspace_binding(
                conversation.id,
                workspace_root=creation.workspace_root,
                git_branch=creation.git_branch,
                worktree_path=creation.worktree_path,
                git_isolated=True,
            )
            if updated is None:
                return {
                    "status": "failed",
                    "workspace_root": str(workspace_root.resolve()),
                    "conversation_id": conversation.id,
                    "error": "The scheduled worktree was created but the conversation binding could not be saved.",
                }
            conversation = updated
            execution_root = Path(creation.workspace_root).resolve()
    now = datetime.now(UTC).isoformat()
    repository.append_transcript_message(
        conversation.id,
        {
            "id": f"schedule_{getattr(run, 'id', '')}",
            "role": "user",
            "content": str(getattr(task, "prompt", "") or ""),
            "timestamp": now,
            "metadata": {
                "source": "scheduled_task",
                "scheduled_task_id": str(getattr(task, "id", "")),
                "scheduled_run_id": str(getattr(run, "id", "")),
            },
        },
    )

    result = await run_owned_rest_chat(
        message=str(getattr(task, "prompt", "") or ""),
        max_iterations=None,
        bootstrap=bootstrap,
        query_engine=None,
        workspace_root=execution_root,
        permission_mode=permission_mode,
        conversation_id=conversation.id,
        run_id=str(getattr(run, "id", "")),
        query_claim=query_claim,
    )
    if not conversation_query_guards().owns(query_claim):
        return {
            "status": "cancelled",
            "conversation_id": conversation.id,
            "workspace_root": str(workspace_root.resolve()),
            "execution_workspace_root": str(execution_root),
            "summary": "",
            "error": "The heartbeat conversation query generation changed before terminal commit.",
        }
    stopped_reason = str(result.get("stopped_reason") or "completed")
    reported_status = str(result.get("status") or "").strip().lower()
    status = (
        reported_status
        if reported_status in {"completed", "partial", "failed", "cancelled"}
        else "completed"
        if stopped_reason == "completed"
        else "failed"
    )
    reply = str(result.get("reply") or "")
    errors = [str(item) for item in result.get("errors", []) if str(item).strip()]
    repository.append_transcript_message(
        conversation.id,
        {
            "id": f"assistant_schedule_{getattr(run, 'id', '')}",
            "role": "assistant",
            "content": reply,
            "timestamp": datetime.now(UTC).isoformat(),
            "terminal_status": status,
            "termination_reason": stopped_reason,
            **({"failure_message": errors[-1]} if status == "failed" and errors else {}),
            "metadata": {
                "source": "scheduled_task",
                "scheduled_task_id": str(getattr(task, "id", "")),
                "scheduled_run_id": str(getattr(run, "id", "")),
                "stopped_reason": stopped_reason,
                "iterations": int(result.get("iterations") or 0),
                "errors": errors,
            },
        },
    )
    snapshot_patch = {
        "scheduled_task": {
            "task_id": str(getattr(task, "id", "")),
            "run_id": str(getattr(run, "id", "")),
            "status": status,
            "stopped_reason": stopped_reason,
        }
    }
    patch_snapshot = getattr(repository, "patch_context_snapshot", None)
    if callable(patch_snapshot):
        patch_snapshot(conversation.id, snapshot_patch)
    else:
        # Keep older repository adapters usable without erasing concurrent
        # snapshot fields: merge against the latest record before saving.
        get_conversation = getattr(repository, "get_conversation", None)
        save_snapshot = getattr(repository, "save_context_snapshot", None)
        record = get_conversation(conversation.id) if callable(get_conversation) else None
        if callable(save_snapshot):
            snapshot = dict(getattr(record, "context_snapshot", {}) or {})
            snapshot.update(snapshot_patch)
            save_snapshot(conversation.id, snapshot)
    return {
        "status": status,
        "conversation_id": conversation.id,
        # History remains grouped under the configured project even when the
        # actual run happens in a nested managed worktree.
        "workspace_root": str(workspace_root.resolve()),
        "execution_workspace_root": str(execution_root),
        "summary": reply,
        "error": stopped_reason if status in {"failed", "cancelled"} else "",
    }
