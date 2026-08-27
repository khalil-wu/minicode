from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class CommandScope:
    """Explicit owner metadata for one client command and its result events."""

    conversation_id: str
    workspace_root: str
    request_id: str

    def apply(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.conversation_id:
            payload["conversation_id"] = self.conversation_id
        if self.workspace_root:
            payload["workspace_root"] = self.workspace_root
        if self.request_id:
            payload["request_id"] = self.request_id
        return payload


def resolve_command_scope(
    session: Any,
    data: Mapping[str, Any],
    *,
    require_conversation: bool = True,
    workspace_keys: tuple[str, ...] = ("workspace_root", "workspace"),
) -> CommandScope:
    """Resolve a MiniCode-style explicit command owner and reject stale owners."""

    active_conversation_id = str(getattr(session, "active_conversation_id", "") or "").strip()
    requested_conversation_id = str(
        data.get("owner_conversation_id")
        or data.get("ownerConversationId")
        or data.get("conversation_id")
        or data.get("conversationId")
        or ""
    ).strip()
    if requested_conversation_id and requested_conversation_id != active_conversation_id:
        raise ValueError(
            "Command conversation owner is stale; switch back to the originating conversation and retry"
        )
    conversation_id = requested_conversation_id or active_conversation_id
    if require_conversation and not conversation_id:
        raise ValueError("Select a conversation before running this command")

    repository = getattr(session, "conversation_repo", None)
    conversation = None
    if conversation_id and repository is not None:
        conversation = repository.get_conversation(conversation_id)
        if conversation is None:
            raise ValueError(f"Conversation '{conversation_id}' was not found")

    requested_workspace = ""
    for key in workspace_keys:
        value = str(data.get(key) or "").strip()
        if value:
            requested_workspace = value
            break
    bound_workspace = str(
        getattr(conversation, "worktree_path", "")
        or getattr(conversation, "workspace_root", "")
        or ""
    ).strip()
    resolve_requested = getattr(session, "_resolve_requested_workspace", None)
    current_workspace = getattr(session, "_current_workspace_root", None)
    session_workspace = getattr(session, "workspace_root", None)
    workspace: Path | None = None
    if callable(resolve_requested):
        workspace = Path(resolve_requested(requested_workspace or None)).expanduser().resolve()
    else:
        root_value = current_workspace() if callable(current_workspace) else session_workspace
        root_text = str(root_value or bound_workspace or "").strip()
        if root_text:
            from backend.services.workspace_service import resolve_requested_workspace

            workspace = resolve_requested_workspace(
                Path(root_text).expanduser().resolve(),
                requested_workspace or None,
            )
        elif requested_workspace:
            # A client-supplied absolute path is not an ownership boundary.  A
            # session, conversation, or legacy host must first bind the root.
            raise ValueError("No workspace is bound to this command owner")
    workspace_root = str(workspace) if workspace is not None else ""

    if bound_workspace:
        bound_root = Path(bound_workspace).expanduser().resolve()
        if workspace is not None and bound_root != workspace:
            raise ValueError("Command workspace does not match the conversation workspace")

    request_id = str(
        data.get("request_id")
        or data.get("requestId")
        or data.get("client_command_id")
        or ""
    ).strip()
    return CommandScope(
        conversation_id=conversation_id,
        workspace_root=workspace_root,
        request_id=request_id,
    )
