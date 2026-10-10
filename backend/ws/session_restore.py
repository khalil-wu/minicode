"""Session restoration logic for WebSocket sessions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from backend.conversations.repository import ConversationRepository
from backend.workspace.path_utils import normalize_project_import_path

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from backend.agent.runtime import AgentRuntime


class SessionRestoreManager:
    """Manages session restoration and synchronization."""

    def __init__(self, conversation_repo: ConversationRepository, *, agent_runtime: AgentRuntime | None = None):
        from backend.agent.runtime import default_runtime_if_initialized

        self.conversation_repo = conversation_repo
        self.agent_runtime = agent_runtime if agent_runtime is not None else default_runtime_if_initialized()

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
                from backend.services.conversation_projection_service import recover_persisted_conversation_projections

                await recover_persisted_conversation_projections(self.conversation_repo, conversation_id=last_conversation_id,
                                                                runtime=self.agent_runtime)
                conversation = await asyncio.to_thread(
                    self.conversation_repo.get_conversation_view, last_conversation_id,
                )
                if (
                    conversation
                    and not conversation["archived"]
                    and conversation["conversation_type"] == "main"
                ):
                    bound_workspace_root = str(conversation["worktree_path"] or conversation["workspace_root"]).strip()
                    result["conversation"] = conversation
                    result["messages"] = conversation["transcript"]
                    result["restored"] = True
                    logger.info(f"Restored conversation {last_conversation_id} with {len(result['messages'])} messages")
            except Exception as e:
                logger.error(f"Failed to restore conversation {last_conversation_id}: {e}")
                result["error"] = f"Failed to restore conversation: {str(e)}"

        # Restore project workspace only from the conversation binding. A
        # client-supplied workspace can be stale after deleting/switching into
        # a global chat, and must not turn an unbound conversation back into a
        # project workspace.
        if bound_workspace_root:
            try:
                workspace_root = normalize_project_import_path(bound_workspace_root)
                if not workspace_root.is_dir():
                    raise ValueError(f"Workspace does not exist: {bound_workspace_root}")
                result["workspace"] = {
                    "root_path": str(workspace_root),
                    "name": workspace_root.name,
                }
                logger.info(f"Restored workspace: {workspace_root}")
            except Exception as e:
                logger.error(f"Failed to restore workspace {bound_workspace_root}: {e}")
                result["error"] = f"Failed to restore workspace: {e}"

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
