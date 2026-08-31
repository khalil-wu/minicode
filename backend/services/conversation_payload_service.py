from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import shutil
from typing import Any

from backend.agent.message import AgentEvent
from backend.conversations.models import (
    DEFAULT_CONVERSATION_PERMISSION_MODE,
    ConversationType,
    normalize_conversation_type,
)
from backend.conversations.public_projection import project_public_conversation
from backend.services.runtime_control_service import CommandOutcome
from backend.ws.utils import normalize_permission_mode


@dataclass(frozen=True)
class ConversationCreateRequest:
    conversation_id: str | None
    title: str
    conversation_type: ConversationType
    memory_mode: str
    permission_mode: str
    workspace_root: str
    git_isolated: bool
    activate: bool
    workspace_required_error: AgentEvent | None = None


@dataclass(frozen=True)
class ConversationTruncateRequest:
    conversation_id: str
    message_id: str
    error: CommandOutcome | None = None



@dataclass(frozen=True)
class ConversationRenameRequest:
    conversation_id: str
    title: str


@dataclass(frozen=True)
class ConversationDeleteRequest:
    conversation_id: str
    cleanup_worktree: bool
    force_cleanup: bool


@dataclass(frozen=True)
class ConversationClearRequest:
    conversation_id: str
    preserve_plan: bool = False
    plan_content: str = ""


@dataclass(frozen=True)
class ConversationMemoryModeRequest:
    conversation_id: str
    memory_mode: str


@dataclass(frozen=True)
class IsolatedWorktreeCreationResult:
    created: bool
    conversation_id: str
    workspace_root: str = ""
    git_branch: str = ""
    worktree_path: str = ""
    error_event: AgentEvent | None = None
    notice_event: AgentEvent | None = None


def copy_worktree_includes(source_root: Path, worktree_root: Path) -> tuple[list[str], list[str]]:
    """Copy explicitly allowlisted ignored runtime files into a new worktree.

    ``.worktreeinclude`` is intentionally opt-in.  Entries must remain under
    the repository, symbolic links are never followed, and existing targets
    are never overwritten.  This lets projects bring across local `.env`
    templates or generated config without turning ignored files into an
    implicit, unsafe bulk copy.
    """

    include_file = source_root / ".worktreeinclude"
    if not include_file.is_file() or include_file.is_symlink():
        return [], []
    copied: list[str] = []
    skipped: list[str] = []
    try:
        entries = include_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return [], [".worktreeinclude (unreadable)"]

    for line in entries:
        value = line.strip().replace("\\", "/")
        if not value or value.startswith("#"):
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            skipped.append(value)
            continue
        source = (source_root / relative).resolve()
        destination = (worktree_root / relative).resolve()
        try:
            source.relative_to(source_root.resolve())
            destination.relative_to(worktree_root.resolve())
        except ValueError:
            skipped.append(value)
            continue
        if not source.exists() or source.is_symlink() or destination.exists():
            skipped.append(value)
            continue
        try:
            if source.is_dir():
                shutil.copytree(source, destination, symlinks=True, ignore_dangling_symlinks=True)
                if any(path.is_symlink() for path in destination.rglob("*")):
                    shutil.rmtree(destination)
                    skipped.append(value)
                    continue
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination, follow_symlinks=False)
            else:
                skipped.append(value)
                continue
            copied.append(value)
        except OSError:
            skipped.append(value)
    return copied, skipped


def parse_conversation_rename_request(data: dict[str, Any]) -> ConversationRenameRequest:
    return ConversationRenameRequest(
        conversation_id=str(data.get("conversation_id", "")),
        title=str(data.get("title", "")),
    )


def parse_conversation_delete_request(data: dict[str, Any]) -> ConversationDeleteRequest:
    return ConversationDeleteRequest(
        conversation_id=str(data.get("conversation_id", "")),
        cleanup_worktree=bool(data.get("cleanup_worktree")),
        force_cleanup=bool(data.get("force")),
    )


def parse_conversation_clear_request(data: dict[str, Any], *, active_conversation_id: str = "") -> ConversationClearRequest:
    return ConversationClearRequest(
        conversation_id=str(data.get("conversation_id") or active_conversation_id or "").strip(),
        preserve_plan=bool(data.get("preserve_plan")),
        plan_content=str(data.get("plan_content") or ""),
    )


def parse_conversation_memory_mode_request(data: dict[str, Any], *, active_conversation_id: str = "") -> ConversationMemoryModeRequest:
    return ConversationMemoryModeRequest(
        conversation_id=str(data.get("conversation_id") or active_conversation_id or ""),
        memory_mode=str(data.get("memory_mode") or "").strip().lower(),
    )


def build_conversation_clear_outcome() -> CommandOutcome:
    return CommandOutcome("clear", "Conversation history cleared.")


def build_worktree_cleanup_outcome(cleanup: dict[str, Any]) -> CommandOutcome:
    return CommandOutcome(
        "conversation.worktree.cleanup",
        str(cleanup.get("message") or cleanup.get("error") or "Worktree cleanup finished"),
        level="success" if cleanup.get("removed") else "warning",
        data=cleanup,
    )


def build_worktree_cleanup_force_required_outcome(cleanup: dict[str, Any]) -> CommandOutcome:
    return CommandOutcome(
        "conversation.worktree.cleanup",
        str(cleanup.get("error") or "Worktree has local changes; force is required"),
        level="warning",
        data=cleanup,
    )


def parse_conversation_create_request(data: dict[str, Any]) -> ConversationCreateRequest:
    git_isolated = bool(data.get("git_isolated"))
    raw_conversation_type = data.get("conversation_type")
    if raw_conversation_type is None and bool(data.get("side_chat")):
        raw_conversation_type = "side_chat"
    conversation_type = normalize_conversation_type(raw_conversation_type)
    requested_memory_mode = str(data.get("memory_mode") or "").strip().lower()
    memory_mode = (
        requested_memory_mode
        if requested_memory_mode in {"enabled", "disabled"}
        else ("disabled" if conversation_type == "side_chat" else "enabled")
    )
    activate = conversation_type == "main"
    workspace_root = str(data.get("workspace_root") or "").strip()
    permission_mode = normalize_permission_mode(
        str(data.get("permission_mode") or DEFAULT_CONVERSATION_PERMISSION_MODE)
    ) or DEFAULT_CONVERSATION_PERMISSION_MODE
    workspace_required_error = None
    if git_isolated and not workspace_root:
        workspace_required_error = AgentEvent.error(
            "Open a workspace folder before creating an isolated Git session.",
            recoverable=True,
            error_type="workspace",
            error_code="workspace_required",
        )
    return ConversationCreateRequest(
        conversation_id=str(data.get("conversation_id") or "").strip() or None,
        title=str(data.get("title") or "New chat"),
        conversation_type=conversation_type,
        memory_mode=memory_mode,
        permission_mode=permission_mode,
        workspace_root=workspace_root,
        git_isolated=git_isolated,
        activate=activate,
        workspace_required_error=workspace_required_error,
    )


def build_conversation_switched_payload(
    conversation: Any,
    *,
    is_hydrating: bool,
    runtime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "conversation.switched",
        "conversation_id": conversation.id,
        "conversation": project_public_conversation(conversation),
        "is_hydrating": bool(is_hydrating),
        "session": runtime_snapshot,
        "snapshot_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


def parse_conversation_truncate_request(
    data: dict[str, Any],
    *,
    active_conversation_id: str = "",
) -> ConversationTruncateRequest:
    conversation_id = str(data.get("conversation_id") or active_conversation_id or "").strip()
    message_id = str(
        data.get("truncate_before_message_id")
        or data.get("retry_from_message_id")
        or data.get("message_id")
        or ""
    ).strip()
    if not conversation_id or not message_id:
        return ConversationTruncateRequest(
            conversation_id,
            message_id,
            CommandOutcome(
                "conversation.truncate",
                "Missing conversation_id or truncate_before_message_id.",
                level="warning",
            ),
        )
    return ConversationTruncateRequest(conversation_id, message_id)


def build_conversation_truncated_outcome(
    conversation: Any,
    *,
    message_id: str,
) -> CommandOutcome:
    return CommandOutcome(
        "conversation.truncate",
        "Conversation rewound to the recalled message.",
        data={
            "conversation_id": conversation.id,
            "message_id": message_id,
            "message_count": len(conversation.transcript or []),
        },
    )


def build_conversation_truncate_failed_outcome(
    *,
    conversation_id: str,
    message_id: str,
) -> CommandOutcome:
    return CommandOutcome(
        "conversation.truncate",
        f"Could not truncate before message '{message_id}'.",
        level="error",
        data={"conversation_id": conversation_id, "message_id": message_id},
    )


def choose_conversation_activation_target(conversation_repo: Any, preferred_id: str | None = None) -> Any | None:
    clean_preferred_id = str(preferred_id or "").strip()
    if clean_preferred_id:
        candidate = conversation_repo.get_conversation(clean_preferred_id)
        if (
            candidate is not None
            and not getattr(candidate, "archived", False)
            and normalize_conversation_type(getattr(candidate, "conversation_type", None)) == "main"
        ):
            return candidate
        return None

    conversations = [
        item
        for item in conversation_repo.list_conversations()
        if (
            not getattr(item, "archived", False)
            and normalize_conversation_type(getattr(item, "conversation_type", None)) == "main"
        )
    ]
    if not conversations:
        return None
    return conversation_repo.get_conversation(conversations[0].id)


def create_isolated_worktree_binding(
    conversation: Any,
    *,
    current_workspace_root: Path | None,
    main_worktree_root: Any,
    worktree_manager_factory: Any | None = None,
) -> IsolatedWorktreeCreationResult:
    from backend.workspace.worktree import WorktreeManager, isolated_worktree_root

    conversation_id = str(getattr(conversation, "id", "") or "")
    raw_base_source = str(getattr(conversation, "workspace_root", "") or "").strip()
    base_source = (
        Path(raw_base_source).expanduser().resolve()
        if raw_base_source
        else current_workspace_root
    )
    if base_source is None:
        return IsolatedWorktreeCreationResult(
            created=False,
            conversation_id=conversation_id,
            error_event=AgentEvent.error(
                "Open a workspace folder before creating an isolated Git session.",
                recoverable=True,
                error_type="workspace",
                error_code="workspace_required",
            ),
        )
    base_root = main_worktree_root(base_source)
    try:
        manager_factory = worktree_manager_factory or WorktreeManager
        manager = manager_factory(base_root)
    except Exception as exc:
        return IsolatedWorktreeCreationResult(
            created=False,
            conversation_id=conversation_id,
            error_event=AgentEvent.error(f"Git isolation unavailable for this workspace: {exc}", recoverable=True),
        )

    worktree_root = isolated_worktree_root(base_root)
    worktree_path = worktree_root / conversation_id
    branch = f"minicode/{conversation_id}"
    try:
        worktree_root.mkdir(parents=True, exist_ok=True)
        created = manager.create_worktree(worktree_path, branch=branch, new_branch=True)
    except Exception as exc:
        return IsolatedWorktreeCreationResult(
            created=False,
            conversation_id=conversation_id,
            error_event=AgentEvent.error(f"Failed to create isolated Git worktree: {exc}", recoverable=True),
        )

    if not created:
        return IsolatedWorktreeCreationResult(
            created=False,
            conversation_id=conversation_id,
            error_event=AgentEvent.error("Failed to create isolated Git worktree", recoverable=True),
        )

    copied, skipped = copy_worktree_includes(base_root, worktree_path)
    include_note = ""
    if copied:
        include_note = f" Copied {len(copied)} managed local file(s) from .worktreeinclude."
    if skipped:
        include_note += f" Skipped {len(skipped)} unsafe or existing include entry(s)."

    return IsolatedWorktreeCreationResult(
        created=True,
        conversation_id=conversation_id,
        workspace_root=str(worktree_path),
        git_branch=branch,
        worktree_path=str(worktree_path),
        notice_event=AgentEvent(
            type="system_notice",
            data={
                "content": (
                    "Created an isolated workspace for this session. "
                    "Edits in this session are separated from your main checkout until you review or merge them."
                    + include_note
                )
            },
        ),
    )


def cleanup_isolated_worktree(
    conversation: Any,
    *,
    force: bool = False,
    current_workspace_root: Path | None,
    main_worktree_root: Any,
    is_path_within: Any,
    worktree_has_local_changes: Any,
    worktree_manager_factory: Any | None = None,
) -> dict[str, Any]:
    from backend.workspace.worktree import WorktreeManager, isolated_worktree_root

    # Check the raw value before resolving: Path("").resolve() yields the
    # process CWD, so an unbound conversation looked like it owned the current
    # working directory and the "not bound" guard below never fired.
    raw_worktree_path = str(getattr(conversation, "worktree_path", "") or "").strip()
    conversation_id = getattr(conversation, "id", "")
    if not getattr(conversation, "git_isolated", False):
        return {
            "removed": False,
            "conversation_id": conversation_id,
            "path": raw_worktree_path,
            "error": "Conversation is not bound to an isolated worktree",
        }
    if not raw_worktree_path:
        # It claims isolation but never got a path, so there is provably no
        # worktree to leave behind. Reporting failure here blocked deletion
        # forever for a record written by a failed creation.
        return {
            "removed": True,
            "conversation_id": conversation_id,
            "path": "",
            "detail": "Isolated worktree was never created; nothing to remove.",
        }
    worktree_path = Path(raw_worktree_path).resolve()
    fallback_base_root = worktree_path.parents[2] if len(worktree_path.parents) >= 3 and worktree_path.parent.name == "worktrees" else worktree_path.parent
    if not worktree_path.exists():
        return {
            "removed": True,
            "conversation_id": conversation_id,
            "path": str(worktree_path),
            "branch": str(getattr(conversation, "git_branch", "") or ""),
            "head": "",
            "workspace_root": str(fallback_base_root),
            "message": "Isolated worktree already removed",
        }
    if current_workspace_root is not None and worktree_path == current_workspace_root:
        return {
            "removed": False,
            "conversation_id": conversation_id,
            "path": str(worktree_path),
            "error": "Cannot remove the active workspace worktree",
        }

    base_root = main_worktree_root(worktree_path)
    isolated_root = isolated_worktree_root(base_root)
    if not is_path_within(worktree_path, isolated_root):
        return {
            "removed": False,
            "conversation_id": conversation_id,
            "path": str(worktree_path),
            "error": "Only isolated worktrees under .minicode/worktrees can be removed",
        }

    dirty = worktree_has_local_changes(worktree_path)
    if dirty and not force:
        return {
            "removed": False,
            "conversation_id": conversation_id,
            "path": str(worktree_path),
            "needs_force": True,
            "error": "Worktree has local changes; confirm force cleanup to remove it",
        }

    try:
        manager_factory = worktree_manager_factory or WorktreeManager
        manager = manager_factory(base_root)
        removal = manager.safe_remove_worktree(
            worktree_path,
            force=force,
            conversation_id=conversation_id,
            branch=str(getattr(conversation, "git_branch", "") or ""),
        )
    except Exception as exc:
        return {"removed": False, "conversation_id": conversation_id, "path": str(worktree_path), "error": str(exc)}

    if removal.needs_force:
        return {
            "removed": False,
            "conversation_id": conversation_id,
            "path": str(worktree_path),
            "needs_force": True,
            "error": removal.error,
        }

    result: dict[str, Any] = {
        "removed": removal.removed,
        "conversation_id": conversation_id,
        "path": str(worktree_path),
        "branch": getattr(conversation, "git_branch", ""),
        "head": str(removal.head or ""),
        "workspace_root": str(base_root),
        "message": "Removed isolated worktree" if removal.removed else (removal.error or "git worktree remove failed"),
    }
    if not removal.removed and removal.error:
        result["error"] = removal.error
    if removal.snapshot is not None:
        result["snapshot_id"] = removal.snapshot.id
        result["snapshot_ref"] = removal.snapshot.snapshot_ref
        if removal.removed:
            result["message"] = "Removed isolated worktree (snapshot saved; recoverable)"
    return result
