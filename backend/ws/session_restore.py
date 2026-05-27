"""Session restoration logic for WebSocket sessions."""

from __future__ import annotations

import logging
from typing import Any

from backend.conversations.repository import ConversationRepository
from backend.workspace.state import get_active_workspace_root

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

        # Restore conversation
        if last_conversation_id:
            try:
                conversation = self.conversation_repo.get_conversation(last_conversation_id)
                if conversation:
                    result["conversation"] = {
                        "id": conversation.id,
                        "title": conversation.title,
                        "memory_mode": conversation.memory_mode,
                        "permission_mode": conversation.permission_mode,
                        "message_count": conversation.message_count,
                        "updated_at": conversation.updated_at,
                    }
                    # Return last N messages for UI
                    result["messages"] = conversation.transcript[-50:] if conversation.transcript else []
                    result["restored"] = True
                    logger.info(f"Restored conversation {last_conversation_id} with {len(result['messages'])} messages")
            except Exception as e:
                logger.error(f"Failed to restore conversation {last_conversation_id}: {e}")
                result["error"] = f"Failed to restore conversation: {str(e)}"

        # Restore workspace
        if last_workspace_root:
            try:
                workspace_root = get_active_workspace_root(last_workspace_root)
                result["workspace"] = {
                    "root_path": str(workspace_root),
                    "name": workspace_root.name,
                }
                logger.info(f"Restored workspace: {workspace_root}")
            except Exception as e:
                logger.error(f"Failed to restore workspace {last_workspace_root}: {e}")
                result["error"] = f"Failed to restore workspace: {str(e)}"

        return result

    async def sync_session(
        self,
        session_id: str,
        client_version: int = 0,
        *,
        session_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Sync session state after reconnection.

        Returns incremental changes since client_version.
        """
        # For now, return full snapshot
        # TODO: Implement incremental sync based on version
        return {
            "session_id": session_id,
            "synced": True,
            "incremental": False,
            "changes": [],
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
