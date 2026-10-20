"""Local disk usage and reviewable, complete conversation trees."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import stat
from typing import Any

from backend.agent.runtime import SWARM_DIR, default_runtime_if_initialized
from backend.agent.swarm_store import FileSwarmStore
from backend.config import DATA_ROOT
from backend.atomic_io import file_mutation_locks
from backend.conversations.compression import (
    compressed_history_path, is_history_path, is_history_temporary, plain_history_path,
)
from backend.services.file_disk_usage import file_disk_bytes


def storage_records() -> FileSwarmStore:
    runtime = default_runtime_if_initialized()
    return FileSwarmStore(runtime.swarm_store_path.parent if runtime is not None else SWARM_DIR)


def disk_usage(root: Path = DATA_ROOT, *, repository=None, worktree_paths=()) -> dict[str, Any]:
    sizes = dict.fromkeys(("chats", "archived", "subagents", "unclassified", "worktrees", "other"), 0)
    conversations = repository.list_conversations() if repository is not None else []
    for entry in sorted(root.iterdir()):
        if _is_link(entry):
            continue
        if entry.name == "conversations" and entry.is_dir():
            history_seen: set[Path] = set()
            for file in entry.iterdir():
                if is_history_temporary(file):
                    continue
                logical = plain_history_path(file) if is_history_path(file) else file
                if is_history_path(logical):
                    if logical in history_seen:
                        continue
                    history_seen.add(logical)
                    owner = next((chat for chat in conversations if logical.name.startswith(chat.id + ".")), None)
                    category = "archived" if owner is not None and owner.archived else "chats" if owner is not None else "unclassified"
                    # Publication uses this same existing file lock. Resolve
                    # representations after acquiring it: scandir may have
                    # observed plain just before a .zst publication completed.
                    with file_mutation_locks([logical]):
                        sizes[category] += sum(_disk_bytes(actual) for actual in (logical, compressed_history_path(logical))
                            if actual.exists() and not _is_link(actual))
                    continue
                if _is_link(file):
                    continue
                owner = next((chat for chat in conversations if file.name.startswith(chat.id + ".")), None)
                category = "archived" if owner is not None and owner.archived else "chats" if owner is not None else "unclassified"
                sizes[category] += _disk_bytes(file)
        else:
            category = "subagents" if entry.name == "swarm" else "worktrees" if entry.name == "worktrees" else "other"
            sizes[category] += _disk_bytes(entry)
    for path in worktree_paths:
        worktree = Path(path)
        if not worktree.is_relative_to(root) and not _is_link(worktree):
            sizes["worktrees"] += _disk_bytes(worktree)
    categories = [{"name": name, "bytes": size} for name, size in sizes.items()]
    return {"path": str(root), "total_bytes": sum(item["bytes"] for item in categories), "categories": categories}


def app_disk_usage() -> dict[str, Any]:
    from backend.conversations.repository import ConversationRepository
    from backend.services.worktree_settings_service import list_managed_worktrees
    repository = ConversationRepository()
    return disk_usage(repository=repository, worktree_paths=[record["path"] for record in list_managed_worktrees(repository=repository)])


def _disk_bytes(path: Path) -> int:
    if path.is_file():
        return file_disk_bytes(path)
    size = 0
    for current, directories, files in os.walk(path, followlinks=False):
        directories[:] = [name for name in directories if not _is_link(Path(current, name))]
        size += sum(file_disk_bytes(Path(current, name)) for name in files if not _is_link(Path(current, name)))
    return size


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT))


def _tree_activity(repo, store: FileSwarmStore) -> dict[str, dict[str, Any]]:
    runs = store.list_agent_runs()
    owners = {run["run_id"]: run["conversation_id"] for run in runs}
    children: dict[str, list[dict[str, Any]]] = {}
    remaining = store.list_subagents()
    while remaining:
        unresolved = []
        for child in remaining:
            owner = owners.get(child["parent_run_id"])
            if owner is None:
                unresolved.append(child)
                continue
            owners[child["subagent_id"]] = owner
            children.setdefault(owner, []).append(child)
        if len(unresolved) == len(remaining):
            break
        remaining = unresolved
    results = {}
    fields = ("status", "started_at", "accepted_at", "completed_at", "last_progress_at", "iteration", "tool_count", "checkpoint_id", "mailbox_epoch")
    for conversation in repo.list_conversations():
        if conversation.thread_source in {"unknown", "cloud_threads.create", "subagent"}:
            continue
        child_records = children.get(conversation.id, [])
        run_records = [run for run in runs if run["conversation_id"] == conversation.id and run["role"] == "main"]
        activity = [*run_records, *child_records]
        last_updated = max([datetime.fromisoformat(conversation.updated_at).timestamp() * 1000,
                            *(record.get(field) or 0 for record in activity for field in ("started_at", "completed_at", "last_progress_at"))])
        snapshot = {"revision": conversation.revision, "updated_at": conversation.updated_at,
                    "archived": conversation.archived,
                    "runs": [{"id": run["run_id"], **{field: run.get(field) for field in fields}} for run in sorted(run_records, key=lambda item: item["run_id"])],
                    "subagents": [{"id": child["subagent_id"], **{field: child.get(field) for field in fields}} for child in sorted(child_records, key=lambda item: item["subagent_id"])]}
        results[conversation.id] = {"id": conversation.id, "title": conversation.title,
            "updated_at": datetime.fromtimestamp(last_updated / 1000, UTC).isoformat(),
            "archived": conversation.archived, "subagent_count": len(child_records),
            "running": any(record["status"] in {"pending", "running"} or record.get("cleanup_pending") for record in activity),
            "snapshot": snapshot}
    return results


def review_storage(repo, *, days: int, archived_only: bool, store: FileSwarmStore | None = None, now: datetime | None = None) -> dict[str, Any]:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    trees = _tree_activity(repo, store if store is not None else storage_records())
    candidates = [tree for tree in trees.values() if not tree["running"] and (not archived_only or tree["archived"])
                  and datetime.fromisoformat(tree["updated_at"]) < cutoff]
    candidates.sort(key=lambda tree: (tree["updated_at"], tree["id"]))
    return {"cutoff": cutoff.isoformat(), "days": days, "archived_only": archived_only, "chats": candidates}


def storage_review_changed(repo, conversation_id: str, snapshot: dict[str, Any], *, store: FileSwarmStore | None = None) -> bool:
    tree = _tree_activity(repo, store if store is not None else storage_records()).get(conversation_id)
    return tree is None or tree["running"] or tree["snapshot"] != snapshot
