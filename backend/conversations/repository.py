from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import copy
import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout as FileLockTimeout

from backend.agent.turn_state import content_from_blocks
from backend.atomic_io import atomic_write_text
from backend.config import DATA_ROOT
from backend.encoding_repair import repair_mojibake_payload

from .models import (
    DEFAULT_CONVERSATION_PERMISSION_MODE,
    DEFAULT_CONVERSATION_TYPE,
    ConversationRecord,
    ConversationSummary,
    ConversationType,
    normalize_memory_mode,
    normalize_conversation_type,
    normalize_permission_mode,
    utc_now_iso,
)
from .public_projection import (
    project_public_conversation,
    project_public_transcript,
    project_public_transcript_message,
)

logger = logging.getLogger(__name__)

CONVERSATION_DATA_DIR = DATA_ROOT / "conversations"
_CONVERSATION_ID_PATTERN = re.compile(
    r"^(?:conv|side|local)_[A-Za-z0-9_-]{6,80}$|^(?:conv|side)-[A-Za-z0-9_-]{6,80}$"
)
_STORAGE_MANIFEST_SCHEMA = "minicode.conversation.manifest"
_STORAGE_MANIFEST_VERSION = 1


class ConversationWriteConflict(RuntimeError):
    """Raised when a detached record would overwrite a newer committed revision."""

    def __init__(self, conversation_id: str, *, expected: int, current: int) -> None:
        super().__init__(
            f"Conversation '{conversation_id}' changed concurrently "
            f"(expected revision {expected}, current revision {current})"
        )
        self.conversation_id = conversation_id
        self.expected = expected
        self.current = current


class ConversationRepository:
    _MAX_RECORD_CACHE = 64

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or CONVERSATION_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._process_lock = threading.RLock()
        self._store_lock_path = self._base_dir / ".conversation-store.lock"
        self._store_revision_path = self._base_dir / ".conversation-store.revision"
        self._store_instance_path = self._base_dir / ".conversation-store.instance"
        self._store_file_lock = FileLock(self._store_lock_path, timeout=60)
        self._summary_index: dict[str, ConversationSummary] | None = None
        self._summary_index_stamps: dict[str, tuple[tuple[int, int], ...]] = {}
        self._record_cache: OrderedDict[str, ConversationRecord] = OrderedDict()
        self._record_cache_stamps: dict[str, tuple[tuple[int, int], ...]] = {}
        self._manifest_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str | None = None,
        conversation_type: ConversationType | str = DEFAULT_CONVERSATION_TYPE,
        memory_mode: str | None = None,
        memory_polluted: bool = False,
        memory_pollution_sources: list[str] | None = None,
        permission_mode: str = DEFAULT_CONVERSATION_PERMISSION_MODE,
        permission_deny_rules: list[str] | None = None,
        permission_overrides: dict[str, str] | None = None,
        summary: str = "",
        transcript: list[dict[str, Any]] | None = None,
        context_snapshot: dict[str, Any] | None = None,
        workspace_root: str = "",
        git_branch: str = "",
        worktree_path: str = "",
        git_isolated: bool = False,
        parent_conversation_id: str = "",
        parent_message_index: int | None = None,
        fork_id: str = "",
        branch_kind: str = "",
    ) -> ConversationRecord:
        requested_id = str(conversation_id or "").strip()
        initial_transcript = project_public_transcript(transcript or [])
        normalized_pollution_sources = list(
            dict.fromkeys(
                str(source or "").strip().lower()
                for source in (memory_pollution_sources or [])
                if str(source or "").strip()
            )
        )
        normalized_conversation_type = normalize_conversation_type(conversation_type)
        is_memory_polluted = bool(
            memory_polluted
            or normalized_pollution_sources
            or str(memory_mode or "").strip().lower() == "polluted"
        )
        normalized_memory_mode = normalize_memory_mode(
            memory_mode,
            conversation_type=normalized_conversation_type,
            polluted=is_memory_polluted,
        )
        with self._store_lock():
            candidate_id = (
                requested_id
                if requested_id and _CONVERSATION_ID_PATTERN.fullmatch(requested_id)
                else f"conv_{uuid.uuid4().hex[:12]}"
            )
            # A delete tombstone permanently reserves the old lifecycle id.
            # Reusing it would let delayed events, uploads, or detached writers
            # from the deleted lifecycle attach to an unrelated new record.
            while (
                self._manifest_path_for(candidate_id).exists()
                or self._load_record(candidate_id) is not None
            ):
                candidate_id = f"conv_{uuid.uuid4().hex[:12]}"
            record = ConversationRecord(
                id=candidate_id,
                title=(title or "New chat").strip() or "New chat",
                conversation_type=normalized_conversation_type,
                memory_mode=normalized_memory_mode,
                memory_polluted=is_memory_polluted,
                memory_pollution_sources=normalized_pollution_sources,
                permission_mode=permission_mode,
                permission_deny_rules=copy.deepcopy(list(permission_deny_rules or [])),
                permission_overrides=copy.deepcopy(dict(permission_overrides or {})),
                summary=summary,
                message_count=len(initial_transcript),
                transcript=initial_transcript,
                context_snapshot=copy.deepcopy(dict(context_snapshot or {})),
                workspace_root=workspace_root,
                git_branch=git_branch,
                worktree_path=worktree_path,
                git_isolated=git_isolated,
                parent_conversation_id=str(parent_conversation_id or ""),
                parent_message_index=parent_message_index,
                fork_id=str(fork_id or ""),
                branch_kind=str(branch_kind or ""),
            )
            self._commit_record(record)
            self._cache_record(record)
            return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        cached = self._record_cache.get(conversation_id)
        if cached is not None and self._record_cache_stamps.get(conversation_id) == self._record_disk_stamp(conversation_id):
            return copy.deepcopy(cached)
        # 优化：不直接使用 summary_index 作排他检查，优先尝试从磁盘读取 record 以防同步延迟导致会话加载为 None
        record = self._load_record(conversation_id)
        if record is not None:
            self._cache_record(record)
        return copy.deepcopy(record) if record is not None else None

    def clone_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        branch_kind: str = "clone",
    ) -> ConversationRecord | None:
        """Create an independent, replayable copy of a conversation.

        The copy is deep (transcript, tool blocks, and context snapshot are not
        shared in memory) and never owns the source's protected worktree. A
        clone can therefore be resumed safely without allowing either session
        to delete the same worktree.
        """
        with self._store_lock():
            source = self._load_record_for_mutation(conversation_id)
            if source is None:
                return None
            clone = copy.deepcopy(source)
            clone.id = f"conv_{uuid.uuid4().hex[:12]}"
            clone.revision = 0
            clone.created_at = utc_now_iso()
            clone.updated_at = clone.created_at
            clone.title = (title or f"{source.title} · 副本").strip()[:120] or "New chat"
            clone.archived = False
            clone.archived_at = ""
            clone.parent_conversation_id = source.id
            clone.parent_message_index = len(source.transcript) - 1 if source.transcript else None
            clone.fork_id = f"fork_{uuid.uuid4().hex[:16]}"
            clone.branch_kind = branch_kind
            clone.merged_into_conversation_id = ""
            clone.merged_at = ""

            # A branch may inspect the same checkout, but it must not become a
            # second owner of an isolated worktree. Preserve the effective path
            # as a shared workspace instead.
            if clone.git_isolated:
                clone.workspace_root = clone.worktree_path or clone.workspace_root
                clone.worktree_path = ""
                clone.git_isolated = False
            # Claude Code forks never share a plan slug/file with the source.
            # Copy the plan before committing the clone so a filesystem error
            # cannot leave a half-created conversation that points at the
            # source owner's mutable plan.
            from backend.agent.plans import cleanup_plan_file, copy_plan_for_fork

            copied_plan_path = None
            try:
                _new_slug, copied_plan_path = copy_plan_for_fork(
                    source.context_snapshot,
                    clone.context_snapshot,
                    clone.workspace_root or source.workspace_root or None,
                )
                self._commit_record(clone)
            except Exception:
                cleanup_plan_file(copied_plan_path)
                raise
            self._cache_record(clone)
            return clone

    @staticmethod
    def _messages_equal(left: Any, right: Any) -> bool:
        try:
            return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
                right, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            return left == right

    def merge_conversation_fast_forward(
        self,
        source_conversation_id: str,
        target_conversation_id: str,
    ) -> tuple[ConversationRecord | None, ConversationRecord | None, str]:
        """Fast-forward a branch into its direct parent with explicit conflicts.

        Merging is intentionally not a heuristic transcript splice. It is
        allowed only when the target still equals the exact branch prefix (or
        already equals the complete source transcript); otherwise the caller
        receives a conflict and no record is modified.
        """
        with self._store_lock():
            source = self._load_record_for_mutation(source_conversation_id)
            target = self._load_record_for_mutation(target_conversation_id)
            if source is None or target is None:
                return source, target, "conversation_not_found"
            if source.id == target.id:
                return source, target, "same_conversation"
            if str(source.parent_conversation_id or "") != target.id:
                return source, target, "source_is_not_direct_child"
            if source.archived or target.archived:
                return source, target, "archived_conversation"
            if source.merged_into_conversation_id and source.merged_into_conversation_id != target.id:
                return source, target, "already_merged_elsewhere"

            source_messages = list(source.transcript or [])
            target_messages = list(target.transcript or [])
            parent_index = source.parent_message_index
            if parent_index is None:
                parent_index = len(source_messages) - 1
            prefix_length = max(0, min(len(source_messages), int(parent_index) + 1))
            prefix = source_messages[:prefix_length]

            if len(target_messages) == len(source_messages) and all(
                self._messages_equal(a, b) for a, b in zip(target_messages, source_messages)
            ):
                source.merged_into_conversation_id = target.id
                source.merged_at = source.merged_at or utc_now_iso()
                source.updated_at = source.merged_at
                self._commit_record(source)
                self._cache_record(source)
                return source, target, "already_up_to_date"

            if len(target_messages) != len(prefix) or not all(
                self._messages_equal(a, b) for a, b in zip(target_messages, prefix)
            ):
                return source, target, "target_diverged"

            target.transcript = copy.deepcopy(source_messages)
            target.message_count = len(target.transcript)
            target.context_snapshot = copy.deepcopy(source.context_snapshot)
            target.summary = source.summary
            target.compaction_state = source.compaction_state
            target.compaction_summary = source.compaction_summary
            target.memory_mode = source.memory_mode
            target.memory_polluted = source.memory_polluted
            target.memory_pollution_sources = copy.deepcopy(
                source.memory_pollution_sources
            )
            target.goal = copy.deepcopy(source.goal)
            target.updated_at = utc_now_iso()
            source.merged_into_conversation_id = target.id
            source.merged_at = target.updated_at
            source.updated_at = target.updated_at
            self._commit_record(target)
            self._commit_record(source)
            self._cache_record(target)
            self._cache_record(source)
            return source, target, "merged"

    def export_conversation_tree(
        self,
        conversation_id: str,
        *,
        include_descendants: bool = True,
    ) -> dict[str, Any] | None:
        """Return a versioned, self-contained session-tree export payload."""
        selected = self.get_conversation(conversation_id)
        if selected is None:
            return None
        summaries = {item.id: item for item in self.list_conversations()}
        root_id = selected.id
        seen: set[str] = set()
        while root_id and root_id not in seen:
            seen.add(root_id)
            parent_id = str(getattr(summaries.get(root_id), "parent_conversation_id", "") or "")
            if not parent_id or parent_id not in summaries:
                break
            root_id = parent_id

        ids = [selected.id]
        if include_descendants:
            ids = [root_id]
            queue = [root_id]
            while queue:
                parent_id = queue.pop(0)
                child_ids = [
                    item.id for item in summaries.values()
                    if str(getattr(item, "parent_conversation_id", "") or "") == parent_id
                ]
                for child_id in sorted(child_ids):
                    if child_id not in ids:
                        ids.append(child_id)
                        queue.append(child_id)

        records: list[dict[str, Any]] = []
        for item_id in ids:
            record = self.get_conversation(item_id)
            if record is not None:
                records.append(project_public_conversation(record))
        return {
            "schema": "minicode.conversation.export",
            "version": 1,
            "exported_at": utc_now_iso(),
            "root_conversation_id": root_id,
            "selected_conversation_id": selected.id,
            "include_descendants": bool(include_descendants),
            "conversations": records,
        }

    def save_conversation(self, record: ConversationRecord) -> ConversationRecord:
        with self._store_lock():
            record.updated_at = utc_now_iso()
            record.message_count = len(record.transcript)
            self._commit_record(record)
            self._cache_record(record)
            return record

    def list_conversations(
        self,
        *,
        conversation_type: ConversationType | None = None,
    ) -> list[ConversationSummary]:
        _, _, conversations = self.list_conversations_with_revision(
            conversation_type=conversation_type,
        )
        return conversations

    def list_conversations_with_revision(
        self,
        *,
        conversation_type: ConversationType | None = None,
    ) -> tuple[str, int, list[ConversationSummary]]:
        with self._store_lock():
            conversations = self._list_conversations_unlocked(
                conversation_type=conversation_type,
            )
            return (
                self._read_or_create_store_instance_id_unlocked(),
                self._read_store_revision_unlocked(),
                conversations,
            )

    def _list_conversations_unlocked(
        self,
        *,
        conversation_type: ConversationType | None = None,
    ) -> list[ConversationSummary]:
        self._ensure_summary_index_loaded()
        conversations = [
            copy.deepcopy(item)
            for item in (self._summary_index or {}).values()
        ]
        if conversation_type is not None:
            normalized_type = normalize_conversation_type(conversation_type)
            conversations = [
                item
                for item in conversations
                if normalize_conversation_type(getattr(item, "conversation_type", None)) == normalized_type
            ]
        # Keep the sidebar stable while a task is running. updated_at changes
        # for messages, snapshots, goals, renames, and permission updates, so
        # using it here made conversations and entire workspace groups jump.
        # Creation time changes only for an explicit new conversation.
        conversations.sort(key=lambda item: (item.created_at, item.id), reverse=True)
        return conversations

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._store_lock():
            safe_id = self._safe_id(conversation_id)
            manifest_path = self._manifest_path_for(conversation_id)
            manifest = self._read_manifest(conversation_id, log_errors=False)
            if manifest is not None and self._manifest_is_deleted(manifest):
                return False
            paths = {
                self._meta_path_for(conversation_id),
                self._transcript_path_for(conversation_id),
                self._snapshot_path_for(conversation_id),
                self._legacy_path_for(conversation_id),
            }
            paths.update(self._base_dir.glob(f"{safe_id}.g*.meta.json"))
            paths.update(self._base_dir.glob(f"{safe_id}.g*.transcript.jsonl"))
            paths.update(self._base_dir.glob(f"{safe_id}.g*.snapshot.json"))
            if not manifest_path.exists() and not any(path.exists() for path in paths):
                return False

            known_generations = self._manifest_generations(manifest or {})
            loaded = self._load_record(conversation_id)
            last_generation = max(
                [*known_generations, int(getattr(loaded, "revision", 0) or 0)],
                default=0,
            )
            deletion_generation = last_generation + 1
            tombstone = {
                "schema": _STORAGE_MANIFEST_SCHEMA,
                "version": _STORAGE_MANIFEST_VERSION,
                "conversation_id": safe_id,
                "deleted": True,
                "deleted_at": utc_now_iso(),
                "deletion_generation": deletion_generation,
            }

            # The atomic tombstone replacement is the delete commit point.
            # Generation/legacy cleanup happens only after readers can no
            # longer recover the record, so a crash cannot expose a partially
            # unlinked conversation or resurrect a legacy fallback.
            try:
                self._advance_store_revision_unlocked()
                self._safe_write_text(
                    manifest_path,
                    json.dumps(tombstone, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception:
                published = self._read_manifest(conversation_id, log_errors=False)
                if not (
                    published is not None
                    and self._manifest_is_deleted(published)
                    and self._manifest_deletion_generation(published)
                    == deletion_generation
                ):
                    raise

            manifest_stat = manifest_path.stat()
            self._manifest_cache[conversation_id] = (
                (manifest_stat.st_mtime_ns, manifest_stat.st_size),
                tombstone,
            )
            self._record_cache.pop(conversation_id, None)
            self._record_cache_stamps.pop(conversation_id, None)
            if self._summary_index is not None:
                self._summary_index.pop(conversation_id, None)
            self._summary_index_stamps.pop(conversation_id, None)

            for path in paths:
                if path.exists():
                    try:
                        path.unlink()
                    except OSError as exc:
                        # The deletion is already authoritative. Retaining an
                        # unreachable generation is safer than reporting that
                        # the old record may still be live.
                        logger.warning(
                            "Failed to remove deleted conversation file %s: %s",
                            path,
                            exc,
                        )
            return True

    def append_transcript_message(
        self, conversation_id: str, message: dict[str, Any]
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            next_message = project_public_transcript_message(message)
            # User input may be re-dispatched after a crash while it is still
            # marked inflight in the durable queue.  The message id is the
            # lifecycle key, so appending it twice would permanently duplicate
            # the user's turn in the transcript.  Replace the existing entry
            # in place when a stable id is present; legacy callers without an
            # id retain the historical append behaviour.
            next_id = str(next_message.get("id") or "").strip()
            replace_index = -1
            if next_id and str(next_message.get("role") or "") == "user":
                replace_index = next(
                    (
                        index
                        for index in range(len(record.transcript) - 1, -1, -1)
                        if str(record.transcript[index].get("id") or "").strip() == next_id
                        and str(record.transcript[index].get("role") or "") == "user"
                    ),
                    -1,
                )
            if replace_index >= 0:
                record.transcript[replace_index] = next_message
            else:
                record.transcript.append(next_message)
            record.message_count = len(record.transcript)
            if record.title == "New chat" and next_message.get("role") == "user":
                record.title = _derive_title(str(next_message.get("content", "")))
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def upsert_transcript_message(
        self, conversation_id: str, message: dict[str, Any]
    ) -> ConversationRecord | None:
        """Persist one message lifecycle by its stable id.

        Codex rollout items and Claude Code transcript messages are durable
        before the whole turn finishes. MiniCode's assistant message id is
        already stable for the full turn, so use it as the same lifecycle key:
        insert the first partial projection, then replace that record at tool
        and terminal boundaries. This keeps completed process work after an app
        restart without creating duplicate assistant messages.
        """
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            next_message = project_public_transcript_message(message)
            message_id = str(next_message.get("id") or "").strip()
            replace_index = -1
            if message_id:
                replace_index = next(
                    (
                        index
                        for index in range(len(record.transcript) - 1, -1, -1)
                        if str(record.transcript[index].get("id") or "").strip() == message_id
                    ),
                    -1,
                )
            if replace_index >= 0:
                record.transcript[replace_index] = next_message
            else:
                record.transcript.append(next_message)
            record.message_count = len(record.transcript)
            if record.title == "New chat" and next_message.get("role") == "user":
                record.title = _derive_title(str(next_message.get("content", "")))
            record.updated_at = utc_now_iso()
            # Partial turn snapshots are committed only at meaningful
            # lifecycle boundaries, never for every streamed token.
            self._commit_record(record)
            self._cache_record(record)
            return record

    def commit_turn_projection(
        self,
        conversation_id: str,
        *,
        assistant_message: dict[str, Any] | None,
        context_snapshot: dict[str, Any],
        summary: str | None = None,
        expected_revision: int | None = None,
    ) -> ConversationRecord | None:
        """Atomically publish one terminal conversation projection.

        The execution journal remains the recovery source of truth. This
        method publishes its transcript, context, and summary projections in
        one repository generation so readers never observe a terminal message
        paired with the previous context snapshot (or vice versa).
        """

        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            projected_message = (
                project_public_transcript_message(assistant_message)
                if assistant_message is not None
                else None
            )
            current_revision = max(0, int(getattr(record, "revision", 0) or 0))
            if expected_revision is not None and current_revision != expected_revision:
                message_id = str((projected_message or {}).get("id") or "").strip()
                existing_message = next(
                    (
                        item
                        for item in reversed(record.transcript)
                        if message_id
                        and str(item.get("id") or "").strip() == message_id
                    ),
                    None,
                )
                # Crash recovery is idempotent when the atomic generation was
                # published but its journal receipt was not. Never overwrite a
                # newer generation merely to replay an older pending record.
                if (
                    projected_message is not None
                    and existing_message == projected_message
                ):
                    return record
                raise ConversationWriteConflict(
                    conversation_id,
                    expected=expected_revision,
                    current=current_revision,
                )
            if assistant_message is not None:
                next_message = projected_message or {}
                message_id = str(next_message.get("id") or "").strip()
                replace_index = -1
                if message_id:
                    replace_index = next(
                        (
                            index
                            for index in range(len(record.transcript) - 1, -1, -1)
                            if str(record.transcript[index].get("id") or "").strip()
                            == message_id
                        ),
                        -1,
                    )
                if replace_index >= 0:
                    record.transcript[replace_index] = next_message
                else:
                    record.transcript.append(next_message)
            record.context_snapshot = copy.deepcopy(dict(context_snapshot or {}))
            if summary is not None:
                record.summary = str(summary)
            record.message_count = len(record.transcript)
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def replace_transcript(
        self, conversation_id: str, transcript: list[dict[str, Any]]
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            record.transcript = project_public_transcript(transcript)
            record.message_count = len(record.transcript)
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def clear_conversation(
        self,
        conversation_id: str,
        *,
        context_snapshot: dict[str, Any] | None = None,
    ) -> ConversationRecord | None:
        """Atomically reset durable conversation history and derived context.

        A clear is one user-visible mutation. Committing transcript, summary,
        memory, compaction, and context in separate generations allowed a
        crash or write failure to expose a partially cleared conversation and
        made concurrent readers observe states that never represented a
        completed command.
        """

        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            record.transcript = []
            record.message_count = 0
            record.summary = ""
            record.memory_mode = normalize_memory_mode(
                "enabled",
                conversation_type=record.conversation_type,
            )
            record.memory_polluted = False
            record.memory_pollution_sources = []
            record.compaction_state = "clean"
            record.compaction_summary = ""
            record.context_snapshot = copy.deepcopy(dict(context_snapshot or {}))
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def update_summary(
        self, conversation_id: str, summary: str
    ) -> ConversationRecord | None:
        return self._mutate_meta(
            conversation_id,
            lambda record: setattr(record, "summary", summary),
        )

    def update_memory_mode(
        self,
        conversation_id: str,
        memory_mode: str,
    ) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> None:
            normalized = normalize_memory_mode(memory_mode)
            if normalized == "polluted":
                raise ValueError("polluted is an internal memory state")
            record.memory_mode = normalized
            record.memory_polluted = False
            record.memory_pollution_sources = []

        return self._mutate_meta(conversation_id, mutate)

    def set_memory_pollution(
        self,
        conversation_id: str,
        sources: list[str],
    ) -> ConversationRecord | None:
        normalized = list(
            dict.fromkeys(
                str(source or "").strip().lower()
                for source in sources
                if str(source or "").strip()
            )
        )

        def mutate(record: ConversationRecord) -> None:
            record.memory_polluted = bool(normalized)
            record.memory_pollution_sources = list(normalized)
            if normalized:
                record.memory_mode = "polluted"
            elif record.memory_mode == "polluted":
                record.memory_mode = "enabled"

        return self._mutate_meta(conversation_id, mutate)

    def mark_memory_polluted(
        self,
        conversation_id: str,
        sources: list[str],
    ) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> None:
            merged = list(record.memory_pollution_sources)
            seen = {str(source).casefold() for source in merged}
            for raw_source in sources:
                source = str(raw_source or "").strip().lower()
                if not source or source.casefold() in seen:
                    continue
                seen.add(source.casefold())
                merged.append(source)
            if not merged:
                return
            record.memory_polluted = True
            record.memory_mode = "polluted"
            record.memory_pollution_sources = merged

        return self._mutate_meta(conversation_id, mutate)

    def reset_memory_state(self) -> dict[str, int]:
        """Clear generated memory projections while preserving conversations.

        This is the repository half of Codex's ``memory/reset`` contract.
        Transcripts, titles, goals, compaction continuity, and each thread's
        ``memory_mode`` remains intact. Generated summaries and legacy
        inherited memory notes are removed.
        """

        conversation_ids = [item.id for item in self.list_conversations()]
        conversations_reset = 0
        notes_removed = 0
        with self._store_lock():
            for conversation_id in conversation_ids:
                record = self._load_record_for_mutation(conversation_id)
                if record is None:
                    continue
                snapshot = copy.deepcopy(record.context_snapshot or {})
                raw_notes = snapshot.get("persistent_notes")
                notes = list(raw_notes) if isinstance(raw_notes, list) else []
                retained_notes = [
                    note
                    for note in notes
                    if not isinstance(note, dict)
                    or str(note.get("kind") or "").strip().lower()
                    not in {"memory", "profile", "summary"}
                ]
                removed_notes = len(notes) - len(retained_notes)
                changed = bool(
                    record.summary
                    or removed_notes
                )
                if not changed:
                    continue

                record.summary = ""
                if isinstance(raw_notes, list):
                    snapshot["persistent_notes"] = retained_notes
                    record.context_snapshot = snapshot
                record.updated_at = utc_now_iso()
                self._commit_record(record)
                self._cache_record(record)
                conversations_reset += 1
                notes_removed += removed_notes

        return {
            "conversations_scanned": len(conversation_ids),
            "conversations_reset": conversations_reset,
            "notes_removed": notes_removed,
        }

    def update_permission_mode(
        self, conversation_id: str, permission_mode: str
    ) -> ConversationRecord | None:
        next_mode = normalize_permission_mode(permission_mode)

        def mutate(record: ConversationRecord) -> None:
            if next_mode == "plan" and record.permission_mode != "plan":
                record.permission_previous_mode = record.permission_mode
            elif next_mode != "plan":
                record.permission_previous_mode = ""
            record.permission_mode = next_mode

        return self._mutate_meta(conversation_id, mutate)

    def update_permission_rules(
        self,
        conversation_id: str,
        *,
        deny_rules: list[str] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> None:
            if deny_rules is not None:
                record.permission_deny_rules = list(deny_rules)
            if overrides is not None:
                record.permission_overrides = dict(overrides)

        return self._mutate_meta(conversation_id, mutate)

    def update_workspace_binding(
        self,
        conversation_id: str,
        *,
        workspace_root: str = "",
        git_branch: str = "",
        worktree_path: str = "",
        git_isolated: bool | None = None,
    ) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> None:
            record.workspace_root = workspace_root
            record.git_branch = git_branch
            record.worktree_path = worktree_path
            if git_isolated is not None:
                record.git_isolated = bool(git_isolated)

        return self._mutate_meta(conversation_id, mutate)

    def update_goal(
        self,
        conversation_id: str,
        goal: dict[str, Any],
    ) -> ConversationRecord | None:
        return self._mutate_meta(
            conversation_id,
            lambda record: setattr(record, "goal", dict(goal)),
        )

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> ConversationRecord | None:
        cleaned = title.strip() or "New chat"
        return self._mutate_meta(
            conversation_id,
            lambda record: setattr(record, "title", cleaned[:120]),
        )

    def set_archived(
        self, conversation_id: str, archived: bool
    ) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> None:
            record.archived = bool(archived)
            record.archived_at = utc_now_iso() if archived else ""

        return self._mutate_meta(conversation_id, mutate)

    def update_compaction(
        self,
        conversation_id: str,
        state: str,
        summary: str = "",
    ) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> None:
            record.compaction_state = state
            record.compaction_summary = summary

        return self._mutate_meta(conversation_id, mutate)

    def commit_compaction(
        self,
        conversation_id: str,
        *,
        context_snapshot: dict[str, Any],
        state: str,
        summary: str,
        expected_revision: int | None = None,
    ) -> ConversationRecord | None:
        """Commit the compacted context and its metadata as one generation."""
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            current_revision = max(0, int(getattr(record, "revision", 0) or 0))
            if expected_revision is not None and current_revision != expected_revision:
                raise ConversationWriteConflict(
                    conversation_id,
                    expected=expected_revision,
                    current=current_revision,
                )
            record.context_snapshot = copy.deepcopy(dict(context_snapshot))
            record.compaction_state = str(state)
            record.compaction_summary = str(summary)
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def save_context_snapshot(
        self, conversation_id: str, context_snapshot: dict[str, Any]
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            record.context_snapshot = dict(context_snapshot)
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def patch_context_snapshot(
        self,
        conversation_id: str,
        patch: dict[str, Any],
        *,
        revision: int | None = None,
        revision_key: str = "_snapshot_patch_revision",
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            snapshot = dict(record.context_snapshot or {})
            if revision is not None:
                try:
                    current_revision = int(snapshot.get(revision_key) or 0)
                except (TypeError, ValueError):
                    current_revision = 0
                if revision <= current_revision:
                    return record
                snapshot[revision_key] = revision
            snapshot.update(dict(patch or {}))
            record.context_snapshot = snapshot
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def _mutate_meta(
        self,
        conversation_id: str,
        mutate: Callable[[ConversationRecord], None],
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            mutate(record)
            record.updated_at = utc_now_iso()
            record.message_count = len(record.transcript)
            self._commit_record(record)
            self._cache_record(record)
            return record

    def _cache_record(self, record: ConversationRecord) -> None:
        self._record_cache[record.id] = copy.deepcopy(record)
        self._record_cache_stamps[record.id] = self._record_disk_stamp(record.id)
        self._record_cache.move_to_end(record.id)
        while len(self._record_cache) > self._MAX_RECORD_CACHE:
            removed_id, _ = self._record_cache.popitem(last=False)
            self._record_cache_stamps.pop(removed_id, None)
        if self._summary_index is not None:
            self._summary_index[record.id] = record.to_summary()
            self._summary_index_stamps[record.id] = self._summary_disk_stamp(record.id)

    def _record_disk_stamp(self, conversation_id: str) -> tuple[tuple[int, int], ...]:
        paths = [
            self._manifest_path_for(conversation_id),
            self._meta_path_for(conversation_id),
            self._transcript_path_for(conversation_id),
            self._snapshot_path_for(conversation_id),
            self._legacy_path_for(conversation_id),
        ]
        manifest = self._read_manifest(conversation_id, log_errors=False)
        if manifest is not None:
            for generation in self._manifest_generations(manifest):
                paths.extend(self._generation_paths(conversation_id, generation))
        stamps: list[tuple[int, int]] = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                stamps.append((-1, -1))
            else:
                stamps.append((stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    def _summary_disk_stamp(self, conversation_id: str) -> tuple[tuple[int, int], ...]:
        return self._record_disk_stamp(conversation_id)

    @staticmethod
    def _manifest_generations(manifest: dict[str, Any]) -> tuple[int, ...]:
        generations: list[int] = []
        for key in ("current_generation", "previous_generation"):
            raw = manifest.get(key)
            if raw is None:
                continue
            if isinstance(raw, bool):
                continue
            try:
                generation = int(raw)
            except (TypeError, ValueError):
                continue
            if generation > 0 and generation not in generations:
                generations.append(generation)
        return tuple(generations)

    @staticmethod
    def _manifest_is_deleted(manifest: dict[str, Any]) -> bool:
        return manifest.get("deleted") is True

    @staticmethod
    def _manifest_deletion_generation(manifest: dict[str, Any]) -> int:
        raw = manifest.get("deletion_generation")
        if isinstance(raw, bool) or not isinstance(raw, int):
            return 0
        return raw if 0 < raw <= 9_007_199_254_740_991 else 0

    def _read_manifest(
        self,
        conversation_id: str,
        *,
        log_errors: bool = True,
    ) -> dict[str, Any] | None:
        path = self._manifest_path_for(conversation_id)
        if not path.exists():
            self._manifest_cache.pop(conversation_id, None)
            return None
        try:
            stat = path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            cached = self._manifest_cache.get(conversation_id)
            if cached is not None and cached[0] == stamp:
                return cached[1]
            payload = json.loads(self._safe_read_text(path, encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("manifest must be an object")
            if payload.get("schema") != _STORAGE_MANIFEST_SCHEMA:
                raise ValueError("unsupported manifest schema")
            if int(payload.get("version") or 0) != _STORAGE_MANIFEST_VERSION:
                raise ValueError("unsupported manifest version")
            if str(payload.get("conversation_id") or "") != self._safe_id(conversation_id):
                raise ValueError("manifest conversation id mismatch")
            if "deleted" in payload and not isinstance(payload.get("deleted"), bool):
                raise ValueError("manifest deleted flag must be boolean")
            if self._manifest_is_deleted(payload):
                if self._manifest_deletion_generation(payload) <= 0:
                    raise ValueError("manifest has no valid deletion generation")
                deleted_at = payload.get("deleted_at")
                if not isinstance(deleted_at, str) or not deleted_at.strip():
                    raise ValueError("manifest has no deletion timestamp")
                self._manifest_cache[conversation_id] = (stamp, payload)
                return payload
            generations = self._manifest_generations(payload)
            if not generations or generations[0] != int(payload.get("current_generation") or 0):
                raise ValueError("manifest has no valid current generation")
            self._manifest_cache[conversation_id] = (stamp, payload)
            return payload
        except Exception as exc:
            self._manifest_cache.pop(conversation_id, None)
            if log_errors:
                logger.error("Failed to read conversation manifest for %s: %s", conversation_id, exc)
            return None

    def _commit_record(self, record: ConversationRecord) -> None:
        """Commit meta, transcript, and snapshot behind one atomic marker.

        Generation files are immutable once published.  The manifest rename
        is the commit point for this repository's generation protocol: a crash
        before it leaves the prior generation authoritative; a crash after it
        can only expose a fully written generation.
        """
        # Conversation transcript files are a public/hydration surface, not an
        # execution-recovery journal. Enforce the same allowlist for every
        # write path, including imports, clones, branch merges, and detached
        # ConversationRecord callers that bypass append/upsert helpers.
        record.transcript = project_public_transcript(record.transcript)
        record.message_count = len(record.transcript)
        manifest = self._read_manifest(record.id, log_errors=False)
        if manifest is not None and self._manifest_is_deleted(manifest):
            raise ConversationWriteConflict(
                record.id,
                expected=max(0, int(getattr(record, "revision", 0) or 0)),
                current=self._manifest_deletion_generation(manifest),
            )
        known_generations = list(self._manifest_generations(manifest or {}))
        current_generation = known_generations[0] if known_generations else 0
        expected_revision = max(0, int(getattr(record, "revision", 0) or 0))
        if expected_revision != current_generation:
            raise ConversationWriteConflict(
                record.id,
                expected=expected_revision,
                current=current_generation,
            )
        previous_generation: int | None = None
        for generation in known_generations:
            if self._read_generation(record.id, generation, log_errors=False) is not None:
                previous_generation = generation
                break

        # On the first mutation of a split/legacy record, preserve its exact
        # pre-mutation state as the explicit fallback generation.
        if manifest is None:
            legacy = self._load_legacy_record(record.id, log_errors=False)
            if legacy is not None:
                previous_generation = 1
                self._write_generation(legacy, previous_generation)
                known_generations.append(previous_generation)

        next_generation = max(known_generations, default=0) + 1
        record.revision = next_generation
        # Advance before publishing the record. A crash can leave a harmless
        # gap in the global inventory sequence, but can never publish a record
        # whose authoritative list revision moved backwards.
        manifest_payload = {
            "schema": _STORAGE_MANIFEST_SCHEMA,
            "version": _STORAGE_MANIFEST_VERSION,
            "conversation_id": record.id,
            "current_generation": next_generation,
            "previous_generation": previous_generation,
        }
        manifest_path = self._manifest_path_for(record.id)
        try:
            self._advance_store_revision_unlocked()
            self._write_generation(record, next_generation)
            self._safe_write_text(
                manifest_path,
                json.dumps(manifest_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            # Keep detached callers retryable after a pre-commit failure. If
            # the atomic manifest replacement actually became authoritative
            # before the filesystem surfaced an error, retain the published
            # generation instead of lying about the record's revision.
            published = self._read_manifest(record.id, log_errors=False)
            published_generations = self._manifest_generations(published or {})
            record.revision = (
                next_generation
                if published_generations and published_generations[0] == next_generation
                else expected_revision
            )
            raise
        manifest_stat = manifest_path.stat()
        self._manifest_cache[record.id] = (
            (manifest_stat.st_mtime_ns, manifest_stat.st_size),
            manifest_payload,
        )
        self._delete_legacy_files(record.id)
        self._cleanup_generations(
            record.id,
            keep={next_generation, *({previous_generation} if previous_generation is not None else set())},
        )

    def _write_generation(self, record: ConversationRecord, generation: int) -> None:
        meta_path, transcript_path, snapshot_path = self._generation_paths(record.id, generation)
        self._safe_write_text(
            meta_path,
            json.dumps(record.to_meta_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        transcript_text = ""
        if record.transcript:
            transcript_text = "\n".join(
                json.dumps(item, ensure_ascii=False) for item in record.transcript
            ) + "\n"
        self._safe_write_text(transcript_path, transcript_text, encoding="utf-8")
        self._safe_write_text(
            snapshot_path,
            json.dumps(record.context_snapshot or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_generation(
        self,
        conversation_id: str,
        generation: int,
        *,
        log_errors: bool = True,
    ) -> ConversationRecord | None:
        meta_path, transcript_path, snapshot_path = self._generation_paths(conversation_id, generation)
        try:
            if not meta_path.exists() or not transcript_path.exists() or not snapshot_path.exists():
                raise FileNotFoundError(f"generation {generation} is incomplete")
            meta_payload = repair_mojibake_payload(
                json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
            )
            if not isinstance(meta_payload, dict):
                raise ValueError("generation meta must be an object")
            if str(meta_payload.get("id") or "") != conversation_id:
                raise ValueError("generation meta conversation id mismatch")
            transcript = self._read_transcript_path(transcript_path, strict=True)
            snapshot = self._read_snapshot_path(snapshot_path, strict=True)
            if "message_count" in meta_payload and int(meta_payload["message_count"]) != len(transcript):
                raise ValueError("generation message count mismatch")
            record = ConversationRecord.from_dict(
                {
                    **meta_payload,
                    "transcript": _normalize_loaded_transcript(transcript),
                    "context_snapshot": snapshot,
                }
            )
            record.revision = generation
            return record
        except Exception as exc:
            if log_errors:
                logger.warning(
                    "Failed to load conversation %s generation %s: %s",
                    conversation_id,
                    generation,
                    exc,
                )
            return None

    def _cleanup_generations(self, conversation_id: str, *, keep: set[int]) -> None:
        safe_id = self._safe_id(conversation_id)
        pattern = re.compile(
            rf"^{re.escape(safe_id)}\.g(\d+)\.(?:meta\.json|transcript\.jsonl|snapshot\.json)$"
        )
        for path in self._base_dir.glob(f"{safe_id}.g*"):
            match = pattern.fullmatch(path.name)
            if match is None or int(match.group(1)) in keep:
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("Failed to remove stale conversation generation %s: %s", path, exc)

    @contextmanager
    def _store_lock(self):
        with self._process_lock:
            try:
                with self._store_file_lock.acquire(timeout=5.0):
                    yield
            except FileLockTimeout as exc:
                raise TimeoutError(
                    f"Timed out waiting for conversation store lock: {self._store_lock_path}"
                ) from exc

    def _safe_write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        for attempt in range(5):
            try:
                atomic_write_text(path, text, encoding=encoding)
                return
            except (IOError, PermissionError) as e:
                if attempt == 4:
                    logger.error("Failed to write text to %s after 5 attempts: %s", path, e)
                    raise e
                time.sleep(0.05 * (attempt + 1))

    def _safe_read_text(self, path: Path, encoding: str = "utf-8") -> str:
        import time
        for attempt in range(5):
            try:
                return path.read_text(encoding=encoding)
            except (IOError, PermissionError) as e:
                if attempt == 4:
                    logger.error("Failed to read text from %s after 5 attempts: %s", path, e)
                    raise e
                time.sleep(0.05 * (attempt + 1))
        return ""

    def _read_store_revision_unlocked(self) -> int:
        if not self._store_revision_path.exists():
            return 0
        try:
            raw = self._safe_read_text(self._store_revision_path, encoding="utf-8").strip()
        except FileNotFoundError:
            return 0
        except OSError as exc:
            raise RuntimeError("Failed to read conversation store revision") from exc
        try:
            revision = int(raw or 0)
        except ValueError as exc:
            raise ValueError(f"Conversation store revision is malformed: {raw!r}") from exc
        if revision < 0 or revision > 9_007_199_254_740_991:
            raise ValueError(f"Conversation store revision is malformed: {raw!r}")
        return revision

    def _read_or_create_store_instance_id_unlocked(self) -> str:
        if self._store_instance_path.exists():
            raw = self._safe_read_text(
                self._store_instance_path,
                encoding="utf-8",
            ).strip().lower()
            if re.fullmatch(r"[0-9a-f]{32}", raw):
                return raw
            raise ValueError(f"Conversation store instance id is malformed: {raw!r}")
        instance_id = uuid.uuid4().hex
        self._safe_write_text(
            self._store_instance_path,
            f"{instance_id}\n",
            encoding="utf-8",
        )
        return instance_id

    def _advance_store_revision_unlocked(self) -> int:
        revision = self._read_store_revision_unlocked() + 1
        self._safe_write_text(
            self._store_revision_path,
            f"{revision}\n",
            encoding="utf-8",
        )
        return revision

    def _load_record_for_mutation(
        self, conversation_id: str
    ) -> ConversationRecord | None:
        cached = self._record_cache.get(conversation_id)
        if (
            cached is not None
            and self._record_cache_stamps.get(conversation_id)
            == self._record_disk_stamp(conversation_id)
        ):
            return copy.deepcopy(cached)
        loaded = self._load_record(conversation_id)
        return copy.deepcopy(loaded) if loaded is not None else None

    def _load_record(self, conversation_id: str) -> ConversationRecord | None:
        if self._manifest_path_for(conversation_id).exists():
            return self._load_committed_record(conversation_id)
        return self._load_legacy_record(conversation_id)

    def _load_committed_record(self, conversation_id: str) -> ConversationRecord | None:
        # A reader can race two rapid commits without taking the writer lock.
        # Re-reading the atomic manifest once lets it move to the newly
        # published generation if the older one was cleaned up meanwhile.
        for attempt in range(2):
            manifest = self._read_manifest(conversation_id, log_errors=attempt > 0)
            if manifest is None:
                return None
            if self._manifest_is_deleted(manifest):
                return None
            for index, generation in enumerate(self._manifest_generations(manifest)):
                record = self._read_generation(
                    conversation_id,
                    generation,
                    log_errors=attempt > 0,
                )
                if record is None:
                    continue
                if index > 0:
                    logger.warning(
                        "Conversation %s recovered from previous generation %s",
                        conversation_id,
                        generation,
                    )
                return record
        logger.error("No committed generation could be loaded for conversation %s", conversation_id)
        return None

    def _load_legacy_record(
        self,
        conversation_id: str,
        *,
        log_errors: bool = True,
    ) -> ConversationRecord | None:
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                meta_payload = repair_mojibake_payload(
                    json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
                )
                if not isinstance(meta_payload, dict):
                    raise ValueError("legacy meta must be an object")
                transcript = self._read_transcript_path(
                    self._transcript_path_for(conversation_id),
                    strict=False,
                )
                snapshot = self._read_snapshot_path(
                    self._snapshot_path_for(conversation_id),
                    strict=False,
                )

                if not transcript and snapshot.get("history"):
                    logger.warning(
                        "Transcript for %s is empty but snapshot contains history; using the snapshot fallback.",
                        conversation_id,
                    )
                    reconstructed: list[dict[str, Any]] = []
                    for idx, msg in enumerate(snapshot["history"]):
                        role = msg.get("role", "user")
                        rec_msg: dict[str, Any] = {
                            "id": f"restored_{role}_{idx}_{uuid.uuid4().hex[:6]}",
                            "role": role,
                            "content": msg.get("content", ""),
                            "timestamp": meta_payload.get("updated_at") or utc_now_iso(),
                        }
                        for key in ("tool_calls", "name", "tool_call_id"):
                            if msg.get(key):
                                rec_msg[key] = msg[key]
                        reconstructed.append(rec_msg)
                    transcript = reconstructed

                return ConversationRecord.from_dict(
                    {
                        **meta_payload,
                        "transcript": _normalize_loaded_transcript(transcript),
                        "context_snapshot": snapshot,
                    }
                )
            except Exception as exc:
                if log_errors:
                    logger.error("Failed to load split conversation %s: %s", conversation_id, exc)
                return None

        legacy_path = self._legacy_path_for(conversation_id)
        if not legacy_path.exists():
            return None
        try:
            payload = repair_mojibake_payload(
                json.loads(self._safe_read_text(legacy_path, encoding="utf-8"))
            )
            if not isinstance(payload, dict):
                raise ValueError("legacy conversation must be an object")
            payload["transcript"] = _normalize_loaded_transcript(
                list(payload.get("transcript") or [])
            )
            return ConversationRecord.from_dict(payload)
        except Exception as exc:
            if log_errors:
                logger.error("Failed to load legacy record for %s: %s", conversation_id, exc)
            return None

    def _load_summary(self, conversation_id: str) -> ConversationSummary | None:
        if self._manifest_path_for(conversation_id).exists():
            record = self._load_committed_record(conversation_id)
            return record.to_summary() if record is not None else None
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                payload = repair_mojibake_payload(json.loads(self._safe_read_text(meta_path, encoding="utf-8")))
                return ConversationSummary.from_dict(payload)
            except Exception as e:
                logger.error("Failed to load summary for %s: %s", conversation_id, e)
                return None

        record = self._load_record(conversation_id)
        if record is None:
            return None
        return record.to_summary()

    def _read_transcript(self, conversation_id: str) -> list[dict[str, Any]]:
        return self._read_transcript_path(
            self._transcript_path_for(conversation_id),
            strict=False,
        )

    def _read_transcript_path(
        self,
        path: Path,
        *,
        strict: bool,
    ) -> list[dict[str, Any]]:
        if not path.exists():
            if strict:
                raise FileNotFoundError(path)
            return []
        transcript: list[dict[str, Any]] = []
        try:
            content = self._safe_read_text(path, encoding="utf-8")
        except Exception:
            if strict:
                raise
            logger.error("Failed to read transcript %s", path, exc_info=True)
            return []
        for line_number, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                transcript.append(repair_mojibake_payload(json.loads(stripped)))
            except (json.JSONDecodeError, TypeError) as exc:
                if strict:
                    raise ValueError(f"malformed transcript line {line_number}") from exc
                logger.warning(
                    "Skipping malformed transcript line %d in %s: %s",
                    line_number,
                    path,
                    exc,
                )
        return transcript

    def _read_snapshot(self, conversation_id: str) -> dict[str, Any]:
        return self._read_snapshot_path(
            self._snapshot_path_for(conversation_id),
            strict=False,
        )

    def _read_snapshot_path(self, path: Path, *, strict: bool) -> dict[str, Any]:
        if not path.exists():
            if strict:
                raise FileNotFoundError(path)
            return {}
        try:
            content = self._safe_read_text(path, encoding="utf-8")
            payload = repair_mojibake_payload(json.loads(content))
            if not isinstance(payload, dict):
                raise ValueError("snapshot must be an object")
            return dict(payload)
        except Exception:
            if strict:
                raise
            logger.error("Failed to read snapshot %s", path, exc_info=True)
            return {}

    def _delete_legacy_files(self, conversation_id: str) -> None:
        for legacy_path in (
            self._meta_path_for(conversation_id),
            self._transcript_path_for(conversation_id),
            self._snapshot_path_for(conversation_id),
            self._legacy_path_for(conversation_id),
        ):
            if not legacy_path.exists():
                continue
            try:
                legacy_path.unlink()
            except OSError as exc:
                logger.warning("Failed to delete legacy file %s: %s", legacy_path, exc)

    def _safe_id(self, conversation_id: str) -> str:
        """Validate conversation_id to prevent path traversal.

        Enforce the same strict allowlist that create_conversation uses
        (_CONVERSATION_ID_PATTERN) on every read/delete/switch path. The prior
        blocklist (only ../\\\x00) permitted ':' , which on Windows lets a
        client-supplied id like 'x:evil' resolve to a drive-relative path
        outside the data directory (arbitrary file delete/overwrite) or write
        an NTFS alternate data stream.
        """
        cid = str(conversation_id or "").strip()
        if not cid or not _CONVERSATION_ID_PATTERN.fullmatch(cid):
            raise ValueError(f"Invalid conversation ID: {cid!r}")
        return cid

    def _meta_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.meta.json"

    def transcript_path(self, conversation_id: str) -> Path:
        """Return the durable rollout path used by memory provenance."""

        manifest = self._read_manifest(conversation_id, log_errors=False)
        if manifest is not None:
            generation = int(manifest.get("current_generation") or 0)
            if generation > 0:
                return self._generation_paths(conversation_id, generation)[1]
        return self._transcript_path_for(conversation_id)

    def _transcript_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.transcript.jsonl"

    def _snapshot_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.snapshot.json"

    def _legacy_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.json"

    def _manifest_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.manifest.json"

    def _generation_paths(
        self,
        conversation_id: str,
        generation: int,
    ) -> tuple[Path, Path, Path]:
        if isinstance(generation, bool) or int(generation) <= 0:
            raise ValueError(f"Invalid conversation generation: {generation!r}")
        prefix = self._base_dir / f"{self._safe_id(conversation_id)}.g{int(generation)}"
        return (
            Path(f"{prefix}.meta.json"),
            Path(f"{prefix}.transcript.jsonl"),
            Path(f"{prefix}.snapshot.json"),
        )

    def _ensure_summary_index_loaded(self) -> None:
        if self._summary_index is None:
            self._summary_index = {}
        discovered_ids = set(self._discover_conversation_ids())
        for conversation_id in set(self._summary_index) - discovered_ids:
            self._summary_index.pop(conversation_id, None)
            self._summary_index_stamps.pop(conversation_id, None)
        for conversation_id in discovered_ids:
            stamp = self._summary_disk_stamp(conversation_id)
            if (
                conversation_id in self._summary_index
                and self._summary_index_stamps.get(conversation_id) == stamp
            ):
                continue
            summary = self._load_summary(conversation_id)
            if summary is not None:
                self._summary_index[summary.id] = summary
                self._summary_index_stamps[conversation_id] = stamp
            else:
                self._summary_index.pop(conversation_id, None)
                self._summary_index_stamps.pop(conversation_id, None)

    def _discover_conversation_ids(self) -> list[str]:
        conversation_ids = {
            path.name[: -len(".manifest.json")]
            for path in self._base_dir.glob("*.manifest.json")
        }
        conversation_ids.update({
            path.name[: -len(".meta.json")]
            for path in self._base_dir.glob("*.meta.json")
            if ".g" not in path.name
        })
        for path in self._base_dir.glob("*.json"):
            if (
                path.name.endswith(".meta.json")
                or path.name.endswith(".snapshot.json")
                or path.name.endswith(".manifest.json")
                or re.search(r"\.g\d+\.", path.name)
            ):
                continue
            conversation_ids.add(path.stem)
        return sorted(
            conversation_id
            for conversation_id in conversation_ids
            if _CONVERSATION_ID_PATTERN.fullmatch(conversation_id)
        )


def _normalize_loaded_transcript(
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for message in transcript:
        blocks = message.get("blocks")
        if str(message.get("role") or "") != "assistant" or not isinstance(blocks, list):
            normalized.append(message)
            continue
        visible_blocks = [
            block
            for block in blocks
            if not _is_legacy_raw_provider_reasoning_block(block)
        ]
        next_message = dict(message) if len(visible_blocks) != len(blocks) else message
        if next_message is not message:
            # Codex keeps raw reasoning behind an explicit opt-in that defaults
            # to false.  Older MiniCode transcripts persisted that provider
            # material in the public block list, so fail closed when loading
            # those records while leaving provider replay items untouched.
            next_message["blocks"] = visible_blocks
        typed_text_blocks = [
            block
            for block in visible_blocks
            if isinstance(block, dict)
            and block.get("type") == "text"
            and any(
                key in block
                for key in ("itemId", "item_id", "source", "status", "isStreaming")
            )
        ]
        if not typed_text_blocks:
            # Pre-item transcripts can contain {type: text, content: ...} only.
            # Their phase is genuinely unknown, so retain the legacy top-level
            # answer exactly as Codex retains completion semantics for phase=None.
            normalized.append(next_message)
            continue
        # Structured blocks are authoritative, matching the live turn and
        # frontend hydration contracts. Rebuild only the derived answer field;
        # reasoning, commentary, tools, and the authored block text stay intact.
        if next_message is message:
            next_message = dict(message)
        next_message["content"] = content_from_blocks(
            [block for block in visible_blocks if isinstance(block, dict)]
        )
        normalized.append(next_message)
    # Legacy generations may predate the public transcript boundary. Project
    # on every load so hydration/export cannot re-expose raw provider frames,
    # credentials, runtime ownership fences, or arbitrary extension metadata.
    return project_public_transcript(normalized)


def _is_legacy_raw_provider_reasoning_block(block: Any) -> bool:
    """Hide raw provider reasoning persisted by pre-alignment builds."""

    if not isinstance(block, dict) or str(block.get("type") or "") != "thinking":
        return False
    if bool(
        block.get("is_raw_provider_reasoning")
        or block.get("isRawProviderReasoning")
    ):
        return True
    visibility = str(block.get("visibility") or "").strip().lower()
    if visibility in {"hidden", "internal", "redacted"}:
        return True
    reasoning_type = str(
        block.get("provider_reasoning_type")
        or block.get("providerReasoningType")
        or ""
    ).strip().lower()
    return reasoning_type in {
        "reasoning_text",
        "reasoning_content",
        "raw_reasoning",
        "raw_provider_reasoning",
        "thinking",
        "thinking_delta",
    }


def _derive_title(content: str) -> str:
    cleaned = " ".join(content.strip().split())
    if not cleaned:
        return "New chat"
    return cleaned[:48]
