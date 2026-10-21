"""Desktop worktree preferences and the worktrees actually created by MiniCode."""
from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.agent.markdown_scopes import get_minicode_config_home_dir
from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.conversations.models import utc_now_iso
from backend.runtime_env import sanitized_git_env


logger = logging.getLogger(__name__)
WORKTREE_PREFERENCES_FILE = get_minicode_config_home_dir() / "worktree-preferences.json"
MANAGED_WORKTREES_FILE = get_minicode_config_home_dir() / "managed-worktrees.json"


class WorktreePreferences(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    worktree_root: str = ""
    auto_cleanup_enabled: bool = True
    keep_count: int = Field(default=15, ge=1)
    upstream_refresh_mode: Literal["never", "best-effort"] = "never"

    @field_validator("worktree_root")
    @classmethod
    def absolute_root(cls, value: str) -> str:
        value = value.strip()
        if value and not Path(value).is_absolute():
            raise ValueError("Worktree root must be an absolute path")
        return value


def _read_preferences() -> WorktreePreferences:
    try:
        source = WORKTREE_PREFERENCES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return WorktreePreferences()
    return WorktreePreferences.model_validate_json(source)


def get_worktree_preferences() -> WorktreePreferences:
    with file_mutation_locks([WORKTREE_PREFERENCES_FILE]):
        return _read_preferences()


def update_worktree_preferences(updates: WorktreePreferences) -> WorktreePreferences:
    with file_mutation_locks([WORKTREE_PREFERENCES_FILE]):
        preferences = _read_preferences().model_copy(update=updates.model_dump(exclude_unset=True))
        atomic_write_text(WORKTREE_PREFERENCES_FILE, json.dumps(preferences.model_dump(), ensure_ascii=False, indent=2) + "\n")
        return preferences


def default_worktree_root() -> Path:
    return (WORKTREE_PREFERENCES_FILE.parent / "worktrees").resolve()


def managed_worktree_root() -> Path:
    return Path(get_worktree_preferences().worktree_root or default_worktree_root()).resolve()


def _read_registry() -> list[dict]:
    try:
        return json.loads(MANAGED_WORKTREES_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []


def register_managed_worktree(path: Path, repo_root: Path, *, branch: str = "") -> None:
    path = path.resolve()
    legacy_root = (repo_root / ".minicode" / "worktrees").resolve()
    current_root = managed_worktree_root()
    with file_mutation_locks([MANAGED_WORKTREES_FILE]):
        records = _read_registry()
        owner = next((record for record in records if canonical_file_path_key(record["path"]) == canonical_file_path_key(repo_root)), None)
        if owner is not None:
            repo_root = Path(owner["repo_root"])
        previous_parent = any(Path(record["path"]).parent == path.parent and Path(record["repo_root"]) == repo_root for record in records)
        if not previous_parent and not any(path != root and path.is_relative_to(root) for root in (current_root, legacy_root)):
            return
        prior = next((record for record in records if canonical_file_path_key(record["path"]) == canonical_file_path_key(path)), None)
        if prior is not None and not prior.get("removed_at"):
            return
        if prior is not None:
            records.remove(prior)
        now = utc_now_iso()
        records.append({"path": str(path), "repo_root": str(repo_root.resolve()), "branch": branch,
            "created_at": now, "last_used_at": now})
        atomic_write_text(MANAGED_WORKTREES_FILE, json.dumps(records, ensure_ascii=False, indent=2) + "\n")


def is_managed_worktree_path(path: Path, repo_root: Path) -> bool:
    path = path.resolve()
    legacy_root = (repo_root / ".minicode" / "worktrees").resolve()
    if path != legacy_root and path.is_relative_to(legacy_root):
        return True
    with file_mutation_locks([MANAGED_WORKTREES_FILE]):
        return any(canonical_file_path_key(record["path"]) == canonical_file_path_key(path)
            and canonical_file_path_key(record["repo_root"]) == canonical_file_path_key(repo_root) for record in _read_registry())


def managed_worktree_record(path: Path) -> dict | None:
    with file_mutation_locks([MANAGED_WORKTREES_FILE]):
        return next((record for record in _read_registry()
            if canonical_file_path_key(record["path"]) == canonical_file_path_key(path)), None)


def unregister_managed_worktree(path: Path) -> None:
    with file_mutation_locks([MANAGED_WORKTREES_FILE]):
        records = _read_registry()
        record = next((record for record in records if canonical_file_path_key(record["path"]) == canonical_file_path_key(path)), None)
        if record is not None:
            record["removed_at"] = utc_now_iso()
            atomic_write_text(MANAGED_WORKTREES_FILE, json.dumps(records, ensure_ascii=False, indent=2) + "\n")


def is_managed_worktree_parent(parent: Path, repo_root: Path) -> bool:
    legacy_root = (repo_root / ".minicode" / "worktrees").resolve()
    if parent == legacy_root or parent.is_relative_to(legacy_root):
        return True
    with file_mutation_locks([MANAGED_WORKTREES_FILE]):
        return any(Path(record["path"]).parent == parent and Path(record["repo_root"]) == repo_root for record in _read_registry())


def refresh_worktree_upstream(repo_root: Path) -> None:
    if get_worktree_preferences().upstream_refresh_mode == "never":
        return
    try:
        result = subprocess.run(["git", "fetch", "--all", "--prune"], cwd=repo_root,
            env={**sanitized_git_env(), "GIT_TERMINAL_PROMPT": "0"}, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, encoding="utf-8", timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("Worktree upstream refresh did not complete: %s", error)
        return
    if result.returncode:
        logger.warning("Worktree upstream refresh failed: %s", result.stderr.strip())


def list_managed_worktrees(*, repository=None, ws_manager=None) -> list[dict]:
    from backend.agent.conversation_query_guard import conversation_query_guards
    from backend.agent.runtime import default_runtime_if_initialized
    from backend.conversations.repository import ConversationRepository
    from backend.ws.conversation_activity import conversation_activity_blockers
    from backend.workspace.trust import is_workspace_trusted

    repository = repository if repository is not None else ConversationRepository()
    conversations = repository.list_conversations()
    loaded = {context.conversation_id for context in ws_manager.execution_contexts.contexts()} if ws_manager is not None else set()
    sessions = ws_manager.resource_sessions() if ws_manager is not None else ()
    runtime = default_runtime_if_initialized()
    agents = runtime.list_runs(include_subagents=True)["subagents"] if runtime is not None else []
    with file_mutation_locks([MANAGED_WORKTREES_FILE]):
        records = _read_registry()
    known_paths = {canonical_file_path_key(record["path"]) for record in records}
    for conversation in conversations:
        if not conversation.worktree_path:
            continue
        path = Path(conversation.worktree_path).resolve()
        if (canonical_file_path_key(path) not in known_paths
                and path.parent.name == "worktrees" and path.parent.parent.name == ".minicode"):
            records.append({"path": str(path), "repo_root": str(path.parents[2]), "branch": conversation.git_branch,
                "created_at": conversation.created_at, "last_used_at": conversation.updated_at, "auto_cleanup_eligible": False})
            known_paths.add(canonical_file_path_key(path))
    results = []
    for record in records:
        path = Path(record["path"])
        if not path.is_dir():
            continue
        matches = [conversation for conversation in conversations
            if any(value and Path(value).resolve().is_relative_to(path) for value in (
                conversation.worktree_path, conversation.execution_cwd, conversation.workspace_root))]
        ids = [conversation.id for conversation in matches]
        reason = ""
        if any(conversation.pinned for conversation in matches):
            reason = "pinned"
        elif not is_workspace_trusted(Path(record["repo_root"])):
            reason = "repository_not_trusted"
        elif any(conversation.id in loaded or conversation_query_guards().active_claim(conversation.id) is not None
                 or conversation_query_guards().has_transition(conversation.id) for conversation in matches):
            reason = "loaded_or_running_conversation"
        elif any((root := session.session_lifecycle.current_workspace_root()) is not None
                 and Path(root).resolve().is_relative_to(path) for session in sessions):
            reason = "loaded_workspace"
        elif sessions and any(any(conversation_activity_blockers(sessions[0], conversation.id).values()) for conversation in matches):
            reason = "live_workspace_resources"
        elif any(agent.get("worktree_path") and Path(agent["worktree_path"]).resolve() == path
                 and (agent["status"] in {"running", "pending", "blocked"} or agent.get("cleanup_pending")) for agent in agents):
            reason = "live_agent"
        results.append({**record, "conversation_ids": ids, "pinned": any(conversation.pinned for conversation in matches),
            "auto_cleanup_eligible": record.get("auto_cleanup_eligible", True),
            "last_used_at": max([record["last_used_at"], *(conversation.updated_at for conversation in matches)]),
            "can_remove": not reason, "protection_reason": reason})
    return sorted(results, key=lambda record: record["last_used_at"], reverse=True)


def remove_managed_worktree(path: str, *, repository=None, ws_manager=None) -> dict:
    from backend.agent.conversation_query_guard import conversation_query_guards
    from backend.conversations.repository import ConversationRepository
    from backend.workspace.worktree import NotGitRepositoryError, WorktreeManager
    from backend.sandbox.runner import SandboxUnavailableError

    repository = repository if repository is not None else ConversationRepository()
    record = next((record for record in list_managed_worktrees(repository=repository, ws_manager=ws_manager)
        if canonical_file_path_key(record["path"]) == canonical_file_path_key(path)), None)
    if record is None:
        return {"removed": False, "error": "Managed worktree not found"}
    if not record["can_remove"]:
        return {"removed": False, "error": record["protection_reason"]}
    guards, transitions = conversation_query_guards(), []
    try:
        for conversation_id in record["conversation_ids"]:
            transition = guards.try_transition(conversation_id, owner_id=f"worktree-cleanup:{uuid4().hex}")
            if transition is None:
                return {"removed": False, "error": "Conversation started running before worktree cleanup"}
            transitions.append(transition)
        target = Path(record["path"]).resolve()
        try:
            manager = WorktreeManager(Path(record["repo_root"]))
            dirty = manager.has_local_changes(target)
        except (NotGitRepositoryError, OSError, subprocess.SubprocessError, SandboxUnavailableError) as error:
            return {"removed": False, "error": f"Could not inspect worktree: {error}"}
        if dirty:
            return {"removed": False, "error": "Worktree has local changes"}
        snapshot = manager.snapshot_worktree(target, branch=record["branch"], label="Before managed worktree cleanup")
        if snapshot is None:
            return {"removed": False, "error": "Worktree snapshot failed"}
        removal = manager.safe_remove_worktree(target, snapshot=False)
        if not removal.removed:
            return {"removed": False, "snapshot_id": snapshot.id, "error": removal.error}
        for conversation_id in record["conversation_ids"]:
            repository.thread_resources.add(conversation_id, "worktree_snapshot", snapshot.id, snapshot.to_dict())
            repository.update_workspace_binding(conversation_id, workspace_root=record["repo_root"],
                git_branch="", worktree_path="", git_isolated=False)
        return {"removed": True, "snapshot_id": snapshot.id, "error": None}
    finally:
        for transition in transitions:
            guards.release_transition(transition)


def cleanup_managed_worktrees(*, repository=None, ws_manager=None) -> list[dict]:
    preferences = get_worktree_preferences()
    if not preferences.auto_cleanup_enabled:
        return []
    records = [record for record in list_managed_worktrees(repository=repository, ws_manager=ws_manager) if record["auto_cleanup_eligible"]]
    return [{"path": record["path"], **remove_managed_worktree(record["path"], repository=repository, ws_manager=ws_manager)}
        for record in records[preferences.keep_count:] if record["can_remove"]]
