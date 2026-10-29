"""Local disk usage and reviewable, complete conversation trees."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Any

from backend.agent.runtime import SWARM_DIR, default_runtime_if_initialized
from backend.agent.swarm_store import FileSwarmStore
from backend.agent.checkpoint import validate_storage_id
from backend.config import DATA_ROOT
from backend.atomic_io import file_mutation_locks
from backend.conversations.compression import (
    compressed_history_path, is_history_path, is_history_temporary, plain_history_path,
)
from backend.services.file_disk_usage import file_disk_bytes


def storage_records() -> FileSwarmStore:
    runtime = default_runtime_if_initialized()
    return FileSwarmStore(runtime.swarm_store_path.parent if runtime is not None else SWARM_DIR)


def disk_usage(root: Path = DATA_ROOT, *, repository=None, worktree_paths=(), records: FileSwarmStore | None = None) -> dict[str, Any]:
    sizes = dict.fromkeys(("chats", "archived", "subagents", "unclassified", "worktrees", "other"), 0)
    conversations = repository.list_conversations() if repository is not None else []
    journal_categories = {}
    if records is not None:
        for conversation in conversations:
            scope = records.conversation_agent_ids(conversation.id)
            journal_categories.update((identity, "archived" if conversation.archived else "chats") for identity in scope["run_ids"])
            journal_categories.update((identity, "subagents") for identity in scope["subagent_ids"])
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
        elif entry.name == "sidechains" and entry.is_dir():
            for journal in entry.iterdir():
                if not _is_link(journal):
                    sizes[journal_categories.get(journal.name, "other")] += _disk_bytes(journal)
        else:
            category = "worktrees" if entry.name == "worktrees" else "other"
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
    return disk_usage(repository=repository, records=storage_records(),
                      worktree_paths=[record["path"] for record in list_managed_worktrees(repository=repository)])


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
    runs = {run["run_id"]: run for run in store.list_agent_runs()}
    children = {child["subagent_id"]: child for child in store.list_subagents()}
    results = {}
    fields = ("status", "started_at", "accepted_at", "completed_at", "last_progress_at", "iteration", "tool_count", "checkpoint_id", "mailbox_epoch")
    for conversation in repo.list_conversations():
        if conversation.thread_source in {"unknown", "cloud_threads.create", "subagent"}:
            continue
        scope = store.conversation_agent_ids(conversation.id)
        if any(run_id not in runs for run_id in scope["run_ids"]) or any(child_id not in children for child_id in scope["subagent_ids"]):
            continue  # The durable inventory changed between reads; this is not a complete reviewable tree.
        run_records = [runs[run_id] for run_id in scope["run_ids"]]
        child_records = [children[child_id] for child_id in scope["subagent_ids"]]
        activity = [*run_records, *child_records]
        activity_times = [max(record.get(field) or 0 for field in ("started_at", "accepted_at", "completed_at", "last_progress_at")) for record in activity]
        if any(updated <= 0 for updated in activity_times):
            continue
        last_updated = max([datetime.fromisoformat(conversation.updated_at).timestamp() * 1000,
                            *activity_times])
        def snapshot_record(record, identity):
            return {"id": record[identity], **{field: record.get(field) for field in fields},
                    "record_hash": hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()}
        snapshot = {"revision": conversation.revision, "updated_at": conversation.updated_at,
                    "archived": conversation.archived,
                    "runs": [snapshot_record(run, "run_id") for run in sorted(run_records, key=lambda item: item["run_id"])],
                    "subagents": [snapshot_record(child, "subagent_id") for child in sorted(child_records, key=lambda item: item["subagent_id"])]}
        results[conversation.id] = {"id": conversation.id, "title": conversation.title,
            "updated_at": datetime.fromtimestamp(last_updated / 1000, UTC).isoformat(),
            "archived": conversation.archived, "subagent_count": len(child_records),
            "running": any(record["status"] in {"pending", "running"} or record.get("cleanup_pending") for record in activity),
            "agent_ids": [*scope["run_ids"], *scope["subagent_ids"]],
            "snapshot": snapshot}
    return results


def _tree_storage_bytes(repo, tree: dict[str, Any], store: FileSwarmStore) -> int:
    # Use the same conversation filename ownership as disk_usage. Count both
    # physical history representations under their publication lock, never the
    # decompressed content or a share of the global SQLite file.
    paths = {plain_history_path(path) if is_history_path(path) else path
             for path in repo.storage_root.glob(f"{tree['id']}.*")
             if path.is_file() and not is_history_temporary(path) and not _is_link(path)}
    size = 0
    for path in paths:
        if is_history_path(path):
            with file_mutation_locks([path]):
                size += sum(file_disk_bytes(actual) for actual in (path, compressed_history_path(path))
                            if actual.exists() and not _is_link(actual))
        else:
            size += file_disk_bytes(path)
    journal_root = store.root.parent / "sidechains"
    for agent_id in set(tree["agent_ids"]):
        journal = journal_root / validate_storage_id(agent_id, field_name="agent_id")
        if journal.exists() and not _is_link(journal):
            size += _disk_bytes(journal)
    return size


def review_storage(repo, *, days: int, archived_only: bool, store: FileSwarmStore | None = None, now: datetime | None = None) -> dict[str, Any]:
    cutoff = (now or datetime.now(UTC)) - timedelta(days=days)
    records = store if store is not None else storage_records()
    trees = _tree_activity(repo, records)
    inventory = [{**{key: value for key, value in tree.items() if key not in {"agent_ids", "running"}},
                  "bytes": _tree_storage_bytes(repo, tree, records)} for tree in trees.values() if not tree["running"]]
    inventory.sort(key=lambda tree: (tree["updated_at"], tree["id"]))
    candidates = [tree for tree in inventory if (not archived_only or tree["archived"])
                  and datetime.fromisoformat(tree["updated_at"]) < cutoff]
    return {"cutoff": cutoff.isoformat(), "days": days, "archived_only": archived_only, "chats": candidates, "inventory": inventory}


def storage_review_changed(repo, conversation_id: str, snapshot: dict[str, Any], *, store: FileSwarmStore | None = None) -> bool:
    tree = _tree_activity(repo, store if store is not None else storage_records()).get(conversation_id)
    return tree is None or tree["running"] or tree["snapshot"] != snapshot
