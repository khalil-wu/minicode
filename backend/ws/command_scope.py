from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping


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
            payload["execution_cwd"] = self.workspace_root
        if self.request_id:
            payload["request_id"] = self.request_id
        return payload


_execution_command_scope: ContextVar[tuple[str, CommandScope] | None] = ContextVar("execution_command_scope", default=None)


def _same_workspace(left: str, right: str) -> bool:
    if left.startswith("file:") or right.startswith("file:"):
        from backend.workspace.executor_paths import ExecutorPath

        return left.startswith("file:") and right.startswith("file:") and ExecutorPath.parse(left) == ExecutorPath.parse(right)
    return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()


@contextmanager
def bind_execution_command_scope(session_id: str, scope: CommandScope) -> Iterator[None]:
    """Bind a server-resolved execution target without changing the reader."""
    token = _execution_command_scope.set((session_id, scope))
    try:
        yield
    finally:
        _execution_command_scope.reset(token)


def resolve_command_scope(
    session: Any,
    data: Mapping[str, Any],
    *,
    require_conversation: bool = True,
    require_workspace: bool = False,
    workspace_keys: tuple[str, ...] = ("execution_cwd", "workspace_root", "workspace"),
    allow_executor_uri: bool = False,
) -> CommandScope:
    """Resolve a MiniCode-style explicit command owner and reject stale owners."""

    active_conversation_id = str(getattr(session, "active_conversation_id", "") or "").strip()
    requested_conversation_id = str(
        data.get("owner_conversation_id")
        or data.get("ownerConversationId")
        or data.get("conversation_id")
        or ""
    ).strip()
    execution = _execution_command_scope.get()
    if execution is not None and execution[0] == session.session_id:
        scope = execution[1]
        if requested_conversation_id and requested_conversation_id != scope.conversation_id:
            raise ValueError("Command conversation does not match its execution owner")
        for key in workspace_keys:
            requested_root = str(data.get(key) or "").strip()
            if requested_root and (not scope.workspace_root or not _same_workspace(requested_root, scope.workspace_root)):
                raise ValueError("Command workspace does not match its execution owner")
        if require_workspace and not scope.workspace_root:
            raise ValueError("Open a workspace before running this command")
        return CommandScope(scope.conversation_id, scope.workspace_root,
            str(data.get("request_id") or data.get("requestId") or data.get("client_command_id") or scope.request_id))
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
        getattr(conversation, "execution_cwd", "")
        or getattr(conversation, "worktree_path", "")
        or getattr(conversation, "workspace_root", "")
        or ""
    ).strip()
    if bound_workspace.startswith("file:") or allow_executor_uri and requested_workspace.startswith("file:"):
        from backend.workspace.executor_paths import ExecutorPath

        if not bound_workspace:
            raise ValueError("No workspace is bound to this command owner")
        if bound_workspace.startswith("file:") and requested_workspace and not _same_workspace(requested_workspace, bound_workspace):
            raise ValueError("Command workspace does not match the conversation workspace")
        # URI owners are compared in the accepted executor namespace. In the
        # native display-path case the asynchronous consumer must also match
        # this URI against its captured attachment before doing any I/O.
        workspace_root = ExecutorPath.parse(requested_workspace or bound_workspace).uri
        return CommandScope(conversation_id, workspace_root, str(
            data.get("request_id") or data.get("requestId") or data.get("client_command_id") or ""
        ).strip())
    resolve_requested = session.resolve_requested_workspace
    current_workspace = session.session_lifecycle.current_workspace_root
    session_workspace = session.session_lifecycle.workspace_root
    workspace: Path | None = None
    session_root_value = session.session_lifecycle.workspace_root
    current_root_value = None
    if callable(current_workspace):
        try:
            current_root_value = current_workspace()
        except RuntimeError:
            current_root_value = None
    has_bound_workspace = bool(
        bound_workspace or session_root_value or session_workspace or current_root_value
    )
    if has_bound_workspace or requested_workspace:
        workspace = Path(resolve_requested(requested_workspace or None)).expanduser().resolve()
    else:
        root_value = (
            current_root_value if has_bound_workspace else session_workspace
        )
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
    if require_workspace and workspace is None:
        raise ValueError("Open a workspace before running this command")
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
