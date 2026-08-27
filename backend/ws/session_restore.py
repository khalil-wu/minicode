"""Session restoration logic for WebSocket sessions."""

from __future__ import annotations

import logging
from typing import Any

from backend.conversations.repository import ConversationRepository
from backend.workspace.path_utils import normalize_project_import_path

logger = logging.getLogger(__name__)


class SessionRestoreManager:
    """Manages session restoration and synchronization."""

    def __init__(self, conversation_repo: ConversationRepository):
        self.conversation_repo = conversation_repo

    async def restore_session(
        self,
        session_id: str,
        last_conversation_id: str | None = None,
        last_workspace_root: str | None = None,
    ) -> dict[str, Any]:
        """
        Restore session state from persistence.

        Returns a RuntimeSessionSnapshot with:
        - Active conversation
        - Active workspace
        - Recent messages
        - Task summary
        """
        result: dict[str, Any] = {
            "session_id": session_id,
            "restored": False,
            "conversation": None,
            "workspace": None,
            "messages": [],
            "error": None,
        }

        bound_workspace_root = ""

        # Restore conversation
        if last_conversation_id:
            try:
                conversation = self.conversation_repo.get_conversation(last_conversation_id)
                if (
                    conversation
                    and not getattr(conversation, "archived", False)
                    and getattr(conversation, "conversation_type", "main") == "main"
                ):
                    bound_workspace_root = str(conversation.worktree_path or conversation.workspace_root or "").strip()
                    result["conversation"] = {
                        "id": conversation.id,
                        "title": conversation.title,
                        "conversation_type": conversation.conversation_type,
                        "archived": conversation.archived,
                        "memory_mode": conversation.memory_mode,
                        "permission_mode": conversation.permission_mode,
                        "message_count": conversation.message_count,
                        "updated_at": conversation.updated_at,
                        "workspace_root": conversation.workspace_root,
                        "git_branch": conversation.git_branch,
                        "worktree_path": conversation.worktree_path,
                        "git_isolated": conversation.git_isolated,
                        "goal": conversation.goal,
                    }
                    # Return last N messages for UI
                    result["messages"] = conversation.transcript[-50:] if conversation.transcript else []
                    result["restored"] = True
                    logger.info(f"Restored conversation {last_conversation_id} with {len(result['messages'])} messages")
            except Exception as e:
                logger.error(f"Failed to restore conversation {last_conversation_id}: {e}")
                result["error"] = f"Failed to restore conversation: {str(e)}"

        # Restore project workspace only from the conversation binding. A
        # client-supplied workspace can be stale after deleting/switching into
        # a global chat, and must not turn an unbound conversation back into a
        # project workspace.
        workspace_candidates = [
            value
            for value in (bound_workspace_root,)
            if str(value or "").strip()
        ]
        seen_workspaces: set[str] = set()
        workspace_errors: list[str] = []
        for workspace_candidate in workspace_candidates:
            candidate = str(workspace_candidate or "").strip()
            if not candidate or candidate in seen_workspaces:
                continue
            seen_workspaces.add(candidate)
            try:
                workspace_root = normalize_project_import_path(candidate)
                if not workspace_root.exists() or not workspace_root.is_dir():
                    raise ValueError(f"Workspace does not exist: {candidate}")
                result["workspace"] = {
                    "root_path": str(workspace_root),
                    "name": workspace_root.name,
                }
                logger.info(f"Restored workspace: {workspace_root}")
                break
            except Exception as e:
                logger.error(f"Failed to restore workspace {candidate}: {e}")
                workspace_errors.append(str(e))

        if result["workspace"] is None and workspace_errors:
            result["error"] = f"Failed to restore workspace: {workspace_errors[-1]}"

        return result

    async def sync_session(
        self,
        session_id: str,
        *,
        session_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return the authoritative session snapshot after reconnection.

        Event replay uses the WebSocket sequence cursor. Message count is not a
        valid version because edits can change content without changing length.
        """
        return {
            "session_id": session_id,
            "synced": True,
            "session": session_snapshot
            or {
                "session_id": session_id,
                "task_summary": {
                    "total": 0,
                    "pending": 0,
                    "running": 0,
                    "completed": 0,
                    "failed": 0,
                    "cancelled": 0,
                },
                "running_tasks": [],
                "pending_approval_count": 0,
            },
        }
