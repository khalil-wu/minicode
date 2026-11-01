from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
import copy
import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from filelock import FileLock, Timeout as FileLockTimeout

from backend.agent.turn_state import content_from_blocks
from backend.atomic_io import atomic_write_text, canonical_file_path_key, file_mutation_locks
from backend.conversations.context_delta import apply_context_snapshot_delta, compose_context_snapshot_deltas
from backend.conversations.projection_log import append_projection, read_projection
from backend.conversations.transcript_index import TranscriptIndexError, encode_transcript, read_page as read_transcript_page, read_message as read_indexed_message, find_match as find_indexed_match
from backend.conversations.compression import (
    HistoryCompressionError, compressed_history_path, compress_history_file,
    history_exists, history_stat, is_history_path, is_history_temporary, physical_history_path, read_history_bytes,
)
from backend.config import DATA_ROOT
from backend.workspace.executor_paths import ExecutorPath

from .models import (
    CONVERSATION_ID_PATTERN as _CONVERSATION_ID_PATTERN,
    inherited_execution_cwd,
    DEFAULT_CONVERSATION_PERMISSION_MODE,
    DEFAULT_CONVERSATION_TYPE,
    ConversationRecord,
    ConversationSummary,
    ConversationType,
    normalize_memory_mode,
    normalize_conversation_type,
    normalize_permission_mode,
    normalize_conversation_agent_mode,
    utc_now_iso,
)
from .public_projection import (
    project_public_conversation,
    project_public_transcript,
    project_public_transcript_message,
    project_transcript_page,
    project_tool_window,
    project_tool_items,
    merge_delegation_facts,
    merge_message_delegations,
)

logger = logging.getLogger(__name__)

CONVERSATION_DATA_DIR = DATA_ROOT / "conversations"
_STORAGE_MANIFEST_SCHEMA = "minicode.conversation.manifest"
_STORAGE_MANIFEST_VERSION = 8


def rebind_transcript_turn_diffs(messages: list[dict[str, Any]], conversation_id: str) -> None:
    """A copied patch is retained by its new transcript, with its original turn evidence."""
    for message in messages:
        turn_diff = message.get("metadata", {}).get("turn_diff")
        if turn_diff is not None:
            turn_diff["thread_id"] = conversation_id
            turn_diff["conversation_id"] = conversation_id


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


class ConversationStorageCorruptError(RuntimeError):
    """Raised when committed conversation metadata exists but is unreadable."""


class ConversationCreationUnavailableError(ValueError):
    """A durable creation command addresses a deleted or occupied lifecycle."""


class ConversationRepository:
    _MAX_RECORD_CACHE = 64

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or CONVERSATION_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._process_lock = threading.RLock()
        self._store_lock_path = self._base_dir / ".conversation-store.lock"
        self._store_revision_path = self._base_dir / ".conversation-store.revision"
        self._store_instance_path = self._base_dir / ".conversation-store.instance"
        self._pinned_order_path = self._base_dir / ".pinned-thread-order.json"
        self._sidebar_order_path = self._base_dir / ".sidebar-occurrence-order.json"
        from backend.conversations.thread_sections import ThreadSectionStore
        from backend.conversations.thread_resources import ThreadResourceReferences
        self.thread_sections = ThreadSectionStore(self)
        self.thread_resources = ThreadResourceReferences(self)
        self._store_file_lock = FileLock(self._store_lock_path, timeout=60)
        self._summary_index: dict[str, ConversationSummary] | None = None
        self._summary_index_stamps: dict[str, tuple[tuple[int, int], ...]] = {}
        self._record_cache: OrderedDict[str, ConversationRecord] = OrderedDict()
        self._record_cache_stamps: dict[str, tuple[tuple[int, int], ...]] = {}
        self._manifest_cache: dict[str, tuple[tuple[int, int], dict[str, Any]]] = {}
        self._partial_projection_cache: OrderedDict[str, tuple[int, int, tuple[int, int], dict[str, Any]]] = OrderedDict()

    @property
    def storage_root(self) -> Path:
        return self._base_dir.resolve()

    def history_paths(self, conversation_id: str) -> tuple[Path, ...]:
        """Logical history files referenced by this conversation's commit."""
        with self._store_lock(conversation_id):
            manifest = self._read_manifest(conversation_id, log_errors=False)
            if manifest is None:
                return (self._transcript_path_for(conversation_id), self._snapshot_path_for(conversation_id)) if self._meta_path_for(conversation_id).exists() else ()
            if self._manifest_is_deleted(manifest):
                return ()
            paths = [path for generation in self._manifest_generations(manifest)
                for path in self._generation_paths(conversation_id, generation)[1:]]
            for key in ("projection_log", "previous_projection_log"):
                if key in manifest:
                    paths.append(self._partial_projection_path(conversation_id, manifest[key]["generation"]))
            return tuple(dict.fromkeys(paths))

    def compress_history_file(self, path: Path, *, cutoff_ns: int) -> bool:
        """Coordinate a representation change with the existing commit writer."""
        path = Path(path)
        if path.resolve().parent != self.storage_root:
            raise ValueError("History compression must stay inside its conversation store")
        conversation_id = self._safe_id(path.name.partition(".")[0])

        @contextmanager
        def publication():
            with self._store_lock(conversation_id):
                # The encoded file may have been deleted or superseded while
                # compression ran. Do not publish an unreachable generation.
                yield path.resolve() in {candidate.resolve() for candidate in self.history_paths(conversation_id)}

        return compress_history_file(path, cutoff_ns=cutoff_ns, publication_lock=publication())

    def cleanup_history_temporaries(self, *, older_than_ns: int) -> int:
        """Remove only old unpublished files created by history transitions."""
        removed = 0
        for path in self.storage_root.iterdir():
            if not is_history_temporary(path):
                continue
            conversation_id = path.name[1:].partition(".")[0]
            if not _CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
                continue
            with self._store_lock(conversation_id):
                if path.is_symlink() or not path.is_file():
                    continue
                if path.stat().st_mtime_ns <= older_than_ns:
                    path.unlink()
                    removed += 1
        return removed

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str | None = None,
        conversation_type: ConversationType | str = DEFAULT_CONVERSATION_TYPE,
        thread_source: str = "user",
        memory_mode: str | None = None,
        memory_polluted: bool = False,
        memory_pollution_sources: list[str] | None = None,
        permission_mode: str = DEFAULT_CONVERSATION_PERMISSION_MODE,
        agent_mode: str = "build",
        permission_deny_rules: list[str] | None = None,
        permission_overrides: dict[str, str] | None = None,
        summary: str = "",
        transcript: list[dict[str, Any]] | None = None,
        context_snapshot: dict[str, Any] | None = None,
        workspace_root: str = "",
        execution_cwd: str = "",
        projectless_base: str = "",
        initial_prompt: str = "",
        git_branch: str = "",
        worktree_path: str = "",
        git_isolated: bool = False,
        parent_conversation_id: str = "",
        parent_message_index: int | None = None,
        fork_id: str = "",
        branch_kind: str = "",
        model_selection: dict[str, str] | None = None,
        reuse_existing: bool = False,
        creation_command_id: str = "",
        sidebar_section_id: str | None = None,
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
        if memory_mode is None and normalized_conversation_type == "main":
            from backend.memory.settings import memory_settings_for_workspace
            memory_settings = memory_settings_for_workspace(workspace_root)
            memory_mode = "enabled" if memory_settings.generate_memories else "disabled"
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
        candidate_id = (
            requested_id if requested_id and _CONVERSATION_ID_PATTERN.fullmatch(requested_id)
            else f"conv_{uuid.uuid4().hex[:12]}"
        )
        command_conversation_id = (
            "conv_" + hashlib.sha256(creation_command_id.encode("utf-8")).hexdigest()[:24]
            if creation_command_id else ""
        )
        if creation_command_id and candidate_id != requested_id:
            candidate_id = command_conversation_id
        # Creation owns ID allocation, including replacement of an occupied ID.
        with self._store_lock(), file_mutation_locks([self.thread_sections.path]):
            if sidebar_section_id is not None and not any(section["id"] == sidebar_section_id for section in self.thread_sections.list()):
                raise ValueError("The requested sidebar section no longer exists")
            if creation_command_id:
                for owned_id in dict.fromkeys((candidate_id, command_conversation_id)):
                    existing = self._load_record(owned_id)
                    if existing is not None and existing.creation_command_id == creation_command_id:
                        if not reuse_existing:
                            return existing
                    manifest = self._read_manifest(owned_id, log_errors=False)
                    if existing is None and manifest is not None and manifest.get("creation_command_id") == creation_command_id:
                        raise ConversationCreationUnavailableError("The conversation created by this command has been deleted")
            if reuse_existing:
                if not requested_id or candidate_id != requested_id:
                    raise ValueError("Reusing a conversation requires its valid explicit id")
                existing = self._load_record(requested_id)
                if existing is not None:
                    if (existing.archived or existing.conversation_type != normalized_conversation_type
                            or existing.workspace_root != workspace_root or existing.git_isolated != git_isolated
                            or existing.parent_conversation_id != parent_conversation_id):
                        raise ValueError("The existing conversation does not match the requested scope")
                    return existing
                if self._manifest_path_for(requested_id).exists():
                    raise ValueError("The requested conversation id belongs to a deleted or unavailable conversation")
            # A delete tombstone permanently reserves the old lifecycle id.
            # Reusing it would let delayed events, uploads, or detached writers
            # from the deleted lifecycle attach to an unrelated new record.
            while (
                self._manifest_path_for(candidate_id).exists()
                or self._load_record(candidate_id) is not None
            ):
                if creation_command_id:
                    if candidate_id == command_conversation_id:
                        raise ConversationCreationUnavailableError("The creation command's conversation identity is already occupied")
                    candidate_id = command_conversation_id
                else:
                    candidate_id = f"conv_{uuid.uuid4().hex[:12]}"
            projectless = None
            if projectless_base and not workspace_root and not execution_cwd:
                from backend.services.projectless_directory_service import create_projectless_directory

                projectless = create_projectless_directory(projectless_base, initial_prompt)
                execution_cwd = projectless.cwd
            execution_cwd = execution_cwd or inherited_execution_cwd({
                "workspace_root": workspace_root, "worktree_path": worktree_path,
                "context_snapshot": context_snapshot or {},
            })
            from backend.workspace.recent_projects import RecentProjectStore
            registered_project = (RecentProjectStore().project_for_root(workspace_root)
                if workspace_root and not workspace_root.startswith("file:") else None)
            record = ConversationRecord(
                id=candidate_id,
                title=(title or "New chat").strip() or "New chat",
                conversation_type=normalized_conversation_type,
                thread_source=thread_source,
                memory_mode=normalized_memory_mode,
                memory_polluted=is_memory_polluted,
                memory_pollution_sources=normalized_pollution_sources,
                permission_mode=permission_mode,
                agent_mode=normalize_conversation_agent_mode(agent_mode),
                permission_deny_rules=copy.deepcopy(list(permission_deny_rules or [])),
                permission_overrides=copy.deepcopy(dict(permission_overrides or {})),
                summary=summary,
                message_count=len(initial_transcript),
                transcript=initial_transcript,
                context_snapshot=copy.deepcopy(dict(context_snapshot or {})),
                workspace_root=workspace_root,
                project_id=registered_project.path if registered_project is not None and registered_project.named else None,
                execution_cwd=(ExecutorPath.parse(execution_cwd).uri
                    if execution_cwd.startswith("file:") else str(Path(execution_cwd).expanduser().resolve()) if execution_cwd else ""),
                projectless_output_dir=projectless.output_directory if projectless is not None else "",
                projectless_browser_root=projectless.browser_root if projectless is not None else "",
                git_branch=git_branch,
                worktree_path=worktree_path,
                git_isolated=git_isolated,
                parent_conversation_id=str(parent_conversation_id or ""),
                parent_message_index=parent_message_index,
                fork_id=str(fork_id or ""),
                branch_kind=str(branch_kind or ""),
                model_selection=dict(model_selection or {}),
                creation_command_id=creation_command_id,
            )
            if worktree_path:
                from backend.conversations.thread_resources import set_worktree_reference
                set_worktree_reference(record, worktree_path, workspace_root)
            self._commit_record(record)
            if sidebar_section_id is not None:
                self.thread_sections.move("thread:" + record.id, sidebar_section_id)
            self._cache_record(record)
            self._prepend_sidebar_occurrence(record)
            return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        with self._store_lock(conversation_id):
            cached = self._record_cache.get(conversation_id)
            if cached is not None and self._record_cache_stamps.get(conversation_id) == self._record_disk_stamp(conversation_id):
                return copy.deepcopy(cached)
            record = self._load_record(conversation_id)
            if record is not None:
                self._cache_record(record)
            return copy.deepcopy(record) if record is not None else None

    def get_conversation_summary(self, conversation_id: str) -> ConversationSummary | None:
        with self._store_lock(conversation_id):
            stamp = self._summary_disk_stamp(conversation_id)
            cached = (self._summary_index or {}).get(conversation_id)
            if cached is not None and self._summary_index_stamps.get(conversation_id) == stamp:
                return copy.deepcopy(cached)
            summary = self._load_summary(conversation_id)
            if self._summary_index is not None:
                if summary is None:
                    self._summary_index.pop(conversation_id, None)
                    self._summary_index_stamps.pop(conversation_id, None)
                else:
                    self._summary_index[conversation_id] = copy.deepcopy(summary)
                    self._summary_index_stamps[conversation_id] = stamp
            return summary

    def get_conversation_for_resume(self, conversation_id: str) -> ConversationRecord | None:
        """Read resume context, locking a mutation only when a Plan needs recovery."""
        from backend.agent.plans import (
            PLAN_FILE_REFERENCE_KEY, PLAN_RECOVERY_STATUS_KEY, PLAN_SLUG_KEY, ensure_plan_file_for_resume,
        )

        record = self.get_conversation(conversation_id)
        if record is None or not any(
            key in record.context_snapshot for key in (PLAN_SLUG_KEY, PLAN_FILE_REFERENCE_KEY, PLAN_RECOVERY_STATUS_KEY)
        ):
            return record

        with self._store_lock(conversation_id):
            record = self.get_conversation(conversation_id)
            if record is None:
                return None
            snapshot = dict(record.context_snapshot)
            ensure_plan_file_for_resume(snapshot, record.transcript, record.workspace_root or None)
            if snapshot != record.context_snapshot:
                record.context_snapshot = snapshot
                record.updated_at = utc_now_iso()
                self._commit_record(record)
                self._cache_record(record)
            return record

    def get_context_snapshot_revision(self, conversation_id: str) -> int | None:
        """Read the existing checkpoint/overlay boundary without copying history."""
        with self._store_lock(conversation_id):
            summary = self.get_conversation_summary(conversation_id)
            if summary is None:
                return None
            manifest = self._read_manifest(conversation_id)
            if manifest is None:
                return summary.revision
            # A recovered readable generation may precede a damaged current
            # checkpoint. Its summary is the source actually used for resume.
            checkpoint_revision = int(manifest.get("projection_revision") or manifest["current_generation"])
            return min(summary.revision, checkpoint_revision)

    def get_transcript_page(
        self, conversation_id: str, *, limit: int = 80, before_message_id: str = "",
    ) -> dict[str, Any] | None:
        view = self.get_conversation_view(conversation_id, limit=limit, before_message_id=before_message_id)
        if view is None:
            return None
        return {"conversation_id": conversation_id, "revision": view["revision"],
                "transcript": view["transcript"], "transcript_page": view["transcript_page"]}

    def find_transcript_match(self, conversation_id: str, pattern: re.Pattern[str]) -> dict[str, str] | None:
        """Find the first visible match without reading the private provider checkpoint.

        One forward indexed stream handles cold/compressed history and the
        in-flight projection. Legacy and damaged generations use the existing
        public history reader and its recovery contract.
        """
        from .search import message_match

        with self._store_lock(conversation_id):
            cached = self._record_cache.get(conversation_id)
            if cached is not None and self._record_cache_stamps.get(conversation_id) == self._record_disk_stamp(conversation_id):
                return next((match for message in cached.transcript
                             if (match := message_match(message, pattern)) is not None), None)
            manifest = self._read_manifest(conversation_id, log_errors=False)
            if manifest is not None and self._manifest_is_deleted(manifest):
                return None
            try:
                metadata = self._read_generation_metadata(conversation_id, manifest["current_generation"]) if manifest else {}
                if metadata.get("transcript_index_version") == 1:
                    path = self._generation_paths(conversation_id, manifest["current_generation"])[1]
                    replacement = self._load_partial_projection(conversation_id, manifest).get("assistant_message")
                    return find_indexed_match(path, metadata["transcript_index"], pattern, replacement=replacement)
            except (ConversationStorageCorruptError, OSError, UnicodeDecodeError, json.JSONDecodeError, TranscriptIndexError):
                logger.warning("Indexed search read failed for %s; using the public history reader", conversation_id, exc_info=True)
            before_message_id = ""
            first_match = None
            while True:
                page = self.get_transcript_page(conversation_id, limit=200, before_message_id=before_message_id)
                if page is None:
                    return None
                for message in page["transcript"]:
                    match = message_match(message, pattern)
                    if match is not None:
                        first_match = match
                        break
                if not page["transcript_page"]["has_more"]:
                    return first_match
                before_message_id = page["transcript_page"]["before_message_id"]

    def get_conversation_view(
        self, conversation_id: str, *, limit: int = 80, before_message_id: str = "",
    ) -> dict[str, Any] | None:
        """Read a UI page without loading the private provider checkpoint."""
        with self._store_lock(conversation_id):
            record = self._record_cache.get(conversation_id)
            if record is not None and self._record_cache_stamps.get(conversation_id) == self._record_disk_stamp(conversation_id):
                return {**project_public_conversation(record, include_transcript=False),
                        **project_transcript_page(record.transcript, limit=limit, before_message_id=before_message_id)}
            manifest = self._read_manifest(conversation_id, log_errors=False)
            if manifest is not None and self._manifest_is_deleted(manifest):
                return None
            try:
                if manifest is not None:
                    meta_path, transcript_path, _ = self._generation_paths(conversation_id, manifest["current_generation"])
                    metadata = self._read_generation_metadata(conversation_id, manifest["current_generation"])
                    if metadata.get("transcript_index_version") == 1:
                        projection = self._load_partial_projection(conversation_id, manifest)
                        snapshot = dict(metadata["public_context_snapshot"])
                        delta = projection.get("context_delta", {})
                        snapshot.update(delta.get("set", {}))
                        for key in delta.get("removed", []):
                            snapshot.pop(key, None)
                        current = {**metadata, **manifest.get("metadata", {}), "context_snapshot": snapshot}
                        page = read_transcript_page(transcript_path, metadata["transcript_index"], limit=limit,
                                                    before_message_id=before_message_id, replacement=projection.get("assistant_message"))
                        # Indexed generations already contain normalized public messages.
                        # Project only the displayed tool window, not every stored block.
                        page["transcript"] = [project_tool_window(message) for message in page["transcript"]]
                        view = project_public_conversation(current, include_transcript=False)
                        view.update(page)
                        view["message_count"] = page["transcript_page"]["total_messages"]
                        return view
            except (ConversationStorageCorruptError, OSError, json.JSONDecodeError, UnicodeDecodeError, TranscriptIndexError):
                # Use the existing generation recovery reader for damaged files;
                # a missing page cursor remains a caller-visible conflict.
                logger.warning("Indexed history read failed for %s; loading the committed generation", conversation_id, exc_info=True)
            # Legacy generations have no byte index. They keep their existing
            # reader and acquire an index on the next normal checkpoint write.
            if record is None or self._record_cache_stamps.get(conversation_id) != self._record_disk_stamp(conversation_id):
                record = self._load_record(conversation_id)
                if record is None:
                    return None
                self._cache_record(record)
            return {**project_public_conversation(record, include_transcript=False),
                    **project_transcript_page(record.transcript, limit=limit, before_message_id=before_message_id)}

    def get_message_tool_items(
        self, conversation_id: str, message_id: str, *, before: int, limit: int = 40, revision: str = "",
    ) -> dict[str, Any] | None:
        with self._store_lock(conversation_id):
            message = self._get_transcript_message(conversation_id, message_id)
            return project_tool_items(message, before=before, limit=limit, revision=revision) if message is not None else None

    def _get_transcript_message(self, conversation_id: str, message_id: str) -> dict[str, Any] | None:
        """Read one indexed message under the caller's conversation lock."""
        cached = self._record_cache.get(conversation_id)
        if cached is not None and self._record_cache_stamps.get(conversation_id) == self._record_disk_stamp(conversation_id):
            return next((item for item in cached.transcript if item["id"] == message_id), None)
        manifest = self._read_manifest(conversation_id, log_errors=False)
        if manifest is not None and self._manifest_is_deleted(manifest):
            return None
        try:
            metadata = self._read_generation_metadata(conversation_id, manifest["current_generation"]) if manifest else {}
            if metadata.get("transcript_index_version") == 1:
                message = self._load_partial_projection(conversation_id, manifest).get("assistant_message")
                if message is None or message.get("id") != message_id:
                    path = self._generation_paths(conversation_id, manifest["current_generation"])[1]
                    message = read_indexed_message(path, metadata["transcript_index"], message_id)
                return message
        except (ConversationStorageCorruptError, OSError, UnicodeDecodeError, json.JSONDecodeError, TranscriptIndexError):
            # The existing generation reader owns damaged-index recovery.
            logger.warning("Indexed message read failed for %s; loading the committed generation", conversation_id, exc_info=True)
        record = self._load_record(conversation_id)
        return next((item for item in record.transcript if item["id"] == message_id), None) if record else None

    def get_message_turn_diff(
        self, conversation_id: str, message_id: str, *, turn_id: str, revision: int | None = None,
    ) -> dict[str, Any] | None:
        with self._store_lock(conversation_id):
            message = self._get_transcript_message(conversation_id, message_id)
            if message is None:
                return None
            payload = message.get("metadata", {}).get("turn_diff")
            if not isinstance(payload, dict):
                return None
            if (payload.get("thread_id") != conversation_id or payload.get("conversation_id") != conversation_id
                    or payload.get("message_id") != message_id or payload.get("turn_id") != turn_id
                    or revision is not None and payload.get("revision") != revision):
                raise ValueError("The turn diff owner or revision changed; reload the conversation.")
            projected = project_public_transcript_message({"id": message_id, "role": "assistant", "metadata": {"turn_diff": payload}})
            return {"conversation_id": conversation_id, "message_id": message_id, **projected["metadata"]["turn_diff"]}

    def clone_conversation(
        self,
        conversation_id: str,
        *,
        title: str | None = None,
        branch_kind: str = "clone",
        idempotency_key: str = "",
        activate: bool = False,
    ) -> ConversationRecord | None:
        """Create an independent, replayable copy of a conversation.

        The copy is deep (transcript, tool blocks, and context snapshot are not
        shared in memory) and never owns the source's protected worktree. A
        clone can therefore be resumed safely without allowing either session
        to delete the same worktree.
        """
        if idempotency_key:
            identity = f"clone:{idempotency_key}"
            clone_id = "conv_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
            request = json.dumps({"source": conversation_id, "title": title, "branch_kind": branch_kind, "activate": activate},
                                 ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            creation_command_id = identity + ":" + hashlib.sha256(request.encode("utf-8")).hexdigest()
            lock_ids = (conversation_id, clone_id)
        else:
            clone_id = f"conv_{uuid.uuid4().hex[:12]}"
            creation_command_id = ""
            lock_ids = (conversation_id,)
        with self._store_lock(*lock_ids):
            source = self._load_record_for_mutation(conversation_id)
            if source is None:
                return None
            if idempotency_key:
                existing = self._load_record(clone_id)
                if existing is not None:
                    if existing.creation_command_id != creation_command_id:
                        raise ConversationCreationUnavailableError("The clone command id is already bound to a different request")
                    return existing
                if self._read_manifest(clone_id, log_errors=False) is not None:
                    raise ConversationCreationUnavailableError("The conversation created by this clone command is unavailable")
            clone = copy.deepcopy(source)
            clone.id = clone_id
            rebind_transcript_turn_diffs(clone.transcript, clone.id)
            clone.creation_command_id = creation_command_id
            clone.revision = 0
            clone.agent_mode_revision = 0
            clone.permission_mode_revision = 0
            clone.workspace_binding_revision = 0
            clone.created_at = utc_now_iso()
            clone.updated_at = clone.created_at
            clone.title = (title or f"{source.title} · 副本").strip()[:120] or "New chat"
            clone.archived = False
            clone.archived_at = ""
            clone.parent_conversation_id = source.id
            clone.parent_message_index = len(source.transcript) - 1 if source.transcript else None
            clone.fork_id = f"fork_{uuid.uuid4().hex[:16]}"
            clone.branch_kind = branch_kind
            from backend.agent.extension_history import DELIVERY_KEYS
            for key in DELIVERY_KEYS:
                clone.context_snapshot.get("extension_state", {}).pop(key, None)
            clone.context_snapshot.pop("extension_cursor", None)
            clone.merged_into_conversation_id = ""
            clone.merged_at = ""
            clone.worktree_registrations = []
            from backend.conversations.thread_resources import resource_reference
            clone.resource_references = [resource_reference(item["attachment_type"], item["identity_key"], item["payload"])
                for item in source.resource_references]

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
                    clone.execution_cwd or source.execution_cwd or None,
                )
                self._commit_record(clone)
            except Exception:
                cleanup_plan_file(copied_plan_path)
                raise
            self._cache_record(clone)
            self.thread_sections.copy_thread_placement(source.id, clone.id)
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
        with self._store_lock(source_conversation_id, target_conversation_id):
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

            source_messages = copy.deepcopy(source.transcript)
            rebind_transcript_turn_diffs(source_messages, target.id)
            target_messages = target.transcript
            parent_index = source.parent_message_index
            if parent_index is None:
                parent_index = -1
            prefix_length = max(0, min(len(source_messages), parent_index + 1))
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

            target.transcript = source_messages
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
                records.append(project_public_conversation(record, defer_turn_diff=False))
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
        with self._store_lock(record.id):
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

    def store_instance_id(self) -> str:
        """Return the durable identity of this conversation store."""

        with self._store_lock():
            return self._read_or_create_store_instance_id_unlocked()

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
        with self._store_lock(conversation_id):
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
            paths.update(self._base_dir.glob(f"{safe_id}.g*.projection.jsonl"))
            paths.update(compressed_history_path(path) for path in tuple(paths))
            paths.update(self._base_dir.glob(f"{safe_id}.g*.transcript.jsonl.zst"))
            paths.update(self._base_dir.glob(f"{safe_id}.g*.snapshot.json.zst"))
            paths.update(self._base_dir.glob(f"{safe_id}.g*.projection.jsonl.zst"))
            if not manifest_path.exists() and not any(path.exists() for path in paths):
                return False

            known_generations = self._manifest_generations(manifest or {})
            loaded = self._load_record(conversation_id) if manifest is None else None
            creation_command_id = (
                manifest.get("metadata", {}).get("creation_command_id", "") if manifest is not None
                else loaded.creation_command_id if loaded is not None else ""
            )
            last_generation = max(
                [*known_generations, self._manifest_revision(manifest or {}), int(getattr(loaded, "revision", 0) or 0)],
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
                "creation_command_id": creation_command_id,
            }

            # The atomic tombstone replacement is the delete commit point.
            # Generation/legacy cleanup happens only after readers can no
            # longer recover the record, so a crash cannot expose a partially
            # unlinked conversation or resurrect a legacy fallback.
            try:
                with self._store_lock():
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
            self.thread_sections.remove_thread(conversation_id)
            self._manifest_cache[conversation_id] = (
                (manifest_stat.st_mtime_ns, manifest_stat.st_size),
                tombstone,
            )
            with self._process_lock:
                self._record_cache.pop(conversation_id, None)
                self._partial_projection_cache.pop(conversation_id, None)
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
        with self._store_lock(conversation_id):
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
        with self._store_lock(conversation_id):
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
        context_snapshot: dict[str, Any] | None = None,
        summary: str | None = None,
        expected_revision: int | None = None,
        context_delta: dict[str, Any] | None = None,
        partial: bool = False,
        return_record: bool = True,
        source_user_message_ids: list[str] | None = None,
        source_run_id: str = "",
    ) -> ConversationRecord | ConversationSummary | None:
        """Atomically publish one terminal conversation projection.

        The execution journal remains the recovery source of truth. This
        method publishes its transcript, context, and summary projections in
        one repository generation so readers never observe a terminal message
        paired with the previous context snapshot (or vice versa).
        """

        if partial and (assistant_message is None or context_delta is None):
            raise ValueError("Partial projections require an assistant message and context delta")

        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id, copy_history=return_record)
            if record is None:
                return None
            projected_message = (
                project_public_transcript_message(assistant_message)
                if assistant_message is not None
                else None
            )
            current_revision = max(0, int(getattr(record, "revision", 0) or 0))
            if source_user_message_ids is not None:
                admissions = record.context_snapshot.get("turn_admissions", {})
                user_ids = [str(item.get("id") or "") for item in record.transcript if item.get("role") == "user"
                    and (str(item.get("id") or "") in admissions
                        or not any(block.get("type") == "realtime" for block in item.get("blocks", [])))]
                if (
                    not source_user_message_ids
                    or not set(source_user_message_ids).issubset(user_ids)
                    or user_ids[-1] not in source_user_message_ids
                    or any(
                        source_run_id and message_id in admissions
                        and str(admissions[message_id].get("run_id") or "") != source_run_id
                        for message_id in source_user_message_ids
                    )
                ):
                    raise ConversationWriteConflict(conversation_id, expected=expected_revision if expected_revision is not None else current_revision, current=current_revision)
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
                    and (context_delta is None or apply_context_snapshot_delta(record.context_snapshot, context_delta) == record.context_snapshot)
                ):
                    return record
                manifest = self._read_manifest(conversation_id, log_errors=False) or {}
                projection_revision = int(manifest.get("projection_revision") or manifest.get("current_generation") or current_revision)
                # Renaming/archiving while a turn streams does not change its
                # input history. Preserve that newer metadata and commit the
                # projection only when its context checkpoint is still current.
                owned_delta = context_delta is not None and source_user_message_ids is not None
                if expected_revision > current_revision or (expected_revision < projection_revision and not owned_delta):
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
                    previous_notification = record.transcript[replace_index].get("metadata", {}).get("completion_notification")
                    merge_message_delegations(record.transcript[replace_index], next_message)
                    record.transcript[replace_index] = next_message
                else:
                    previous_notification = None
                    record.transcript.append(next_message)
                notification = next_message.get("metadata", {}).get("completion_notification")
                if not partial and notification is not None and previous_notification is None:
                    record.has_unread_turn |= notification["should_mark_unread"]
            record.context_snapshot = (
                apply_context_snapshot_delta(record.context_snapshot, context_delta)
                if context_delta is not None
                else copy.deepcopy(dict(context_snapshot or {}))
            )
            if summary is not None:
                record.summary = str(summary)
            record.message_count = len(record.transcript)
            record.updated_at = utc_now_iso()
            if context_delta is not None:
                self._commit_metadata(record, assistant_message=projected_message, context_delta=context_delta)
            else:
                self._commit_record(record)
            self._cache_record(record, copy_record=return_record)
            return record if return_record else record.to_summary()

    def update_tool_call(self, conversation_id: str, *, message_id: str, tool_call: dict[str, Any]) -> bool:
        """Update one owned tool fact without replacing its completed answer or context."""
        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return False
            message = next((item for item in record.transcript if item.get("id") == message_id), None)
            if message is None:
                return False
            projected = copy.deepcopy(tool_call)
            calls = message.setdefault("tool_calls", [])
            previous = next((tool for tool in calls if tool["id"] == projected["id"]), None)
            if previous is not None and (previous.get("delegations") or projected.get("delegations")):
                projected["delegations"] = merge_delegation_facts(previous.get("delegations", []), projected.get("delegations", []))
            for index, tool in enumerate(calls):
                if tool["id"] == projected["id"]:
                    calls[index] = projected
                    break
            else:
                calls.append(projected)
            blocks = message.setdefault("blocks", [])
            for block in blocks:
                if block.get("type") == "tool_call" and block["record"]["id"] == projected["id"]:
                    block["record"] = copy.deepcopy(projected)
                    break
            else:
                blocks.append({"type": "tool_call", "record": copy.deepcopy(projected)})
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return True

    def update_tool_delegation(
        self, conversation_id: str, *, message_id: str, tool_call_id: str, delegation: dict[str, Any],
    ) -> bool:
        """Publish a real detached-child lifecycle to its original parent item."""
        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return False
            message = next((item for item in record.transcript if item.get("id") == message_id), None)
            if message is None:
                return False
            records = [*message.get("tool_calls", []), *(block["record"] for block in message.get("blocks", []) if block.get("type") == "tool_call")]
            changed = False
            for tool in records:
                if tool["id"] == tool_call_id:
                    tool["delegations"] = merge_delegation_facts(tool.get("delegations", []), [delegation])
                    changed = True
            if not changed:
                return False
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return True

    def commit_turn_admission(
        self,
        conversation_id: str,
        *,
        user_message: dict[str, Any],
        context_snapshot: dict[str, Any],
        assistant_message: dict[str, Any] | None = None,
        expected_revision: int | None = None,
        rewind_projection: dict[str, Any] | None = None,
        agent_mode: str | None = None,
        expected_agent_mode_revision: int | None = None,
        permission_mode: str | None = None,
        expected_permission_mode_revision: int | None = None,
        workspace_binding: dict[str, Any] | None = None,
        expected_workspace_binding_revision: int | None = None,
        execution_cwd: str = "",
    ) -> ConversationRecord | None:
        """Admit one user and, for steer, its ordered physical-turn boundary."""

        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            next_message = project_public_transcript_message(user_message)
            next_assistant = project_public_transcript_message(assistant_message) if assistant_message is not None else None
            message_id = str(next_message.get("id") or "").strip()
            current_revision = max(0, int(getattr(record, "revision", 0) or 0))
            if expected_revision is not None and current_revision != expected_revision:
                existing_message = next(
                    (
                        item
                        for item in reversed(record.transcript)
                        if message_id
                        and str(item.get("id") or "").strip() == message_id
                    ),
                    None,
                )
                if (
                    existing_message == next_message
                    and record.context_snapshot == context_snapshot
                    and (next_assistant is None or next_assistant in record.transcript)
                ):
                    return record
                raise ConversationWriteConflict(
                    conversation_id,
                    expected=expected_revision,
                    current=current_revision,
                )

            if rewind_projection is not None:
                record.transcript = project_public_transcript(rewind_projection["transcript"])
                record.summary = rewind_projection["summary"]
                record.memory_pollution_sources = list(rewind_projection["memory_pollution_sources"])
                record.memory_polluted = bool(record.memory_pollution_sources)
                if record.memory_polluted:
                    record.memory_mode = "polluted"
                elif record.memory_mode == "polluted":
                    record.memory_mode = "enabled"
            if next_assistant is not None:
                assistant_index = next((index for index, item in enumerate(record.transcript)
                    if item.get("id") == next_assistant["id"]), -1)
                if assistant_index >= 0:
                    merge_message_delegations(record.transcript[assistant_index], next_assistant)
                    record.transcript[assistant_index] = next_assistant
                else:
                    record.transcript.append(next_assistant)
            replace_index = next(
                (
                    index
                    for index in range(len(record.transcript) - 1, -1, -1)
                    if message_id
                    and str(record.transcript[index].get("id") or "").strip()
                    == message_id
                ),
                -1,
            )
            if replace_index >= 0:
                realtime_blocks = [block for block in record.transcript[replace_index].get("blocks", [])
                    if block.get("type") == "realtime"]
                if realtime_blocks and not next_message.get("blocks"):
                    next_message["blocks"] = realtime_blocks
                record.transcript[replace_index] = next_message
            else:
                record.transcript.append(next_message)
            record.context_snapshot = copy.deepcopy(dict(context_snapshot or {}))
            if (agent_mode is not None and expected_agent_mode_revision is not None
                    and record.agent_mode_revision == expected_agent_mode_revision):
                record.agent_mode = normalize_conversation_agent_mode(agent_mode)
                record.agent_mode_revision += 1
            if (permission_mode is not None and expected_permission_mode_revision is not None
                    and record.permission_mode_revision == expected_permission_mode_revision):
                self._apply_permission_mode(record, normalize_permission_mode(permission_mode))
            if (expected_workspace_binding_revision is not None
                    and record.workspace_binding_revision == expected_workspace_binding_revision):
                if workspace_binding is not None:
                    self._apply_workspace_binding(record, **workspace_binding, update_sidebar=False)
                elif execution_cwd and not record.execution_cwd:
                    record.execution_cwd = execution_cwd
                    record.workspace_binding_revision += 1
            record.message_count = len(record.transcript)
            if record.title == "New chat":
                record.title = _derive_title(str(next_message.get("display_content", next_message.get("content", ""))))
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def commit_rewind_projection(
        self,
        conversation_id: str,
        *,
        transcript: list[dict[str, Any]],
        context_snapshot: dict[str, Any],
        summary: str,
        memory_pollution_sources: list[str],
        expected_revision: int | None = None,
    ) -> ConversationRecord | None:
        """Atomically publish a rewind across every conversation projection."""

        normalized_sources = list(
            dict.fromkeys(
                str(source or "").strip().lower()
                for source in memory_pollution_sources
                if str(source or "").strip()
            )
        )
        with self._store_lock(conversation_id):
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
            record.transcript = project_public_transcript(transcript)
            record.context_snapshot = copy.deepcopy(dict(context_snapshot or {}))
            record.summary = str(summary or "")
            record.memory_polluted = bool(normalized_sources)
            record.memory_pollution_sources = normalized_sources
            if normalized_sources:
                record.memory_mode = "polluted"
            elif record.memory_mode == "polluted":
                record.memory_mode = "enabled"
            record.message_count = len(record.transcript)
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def replace_transcript(
        self, conversation_id: str, transcript: list[dict[str, Any]]
    ) -> ConversationRecord | None:
        with self._store_lock(conversation_id):
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

        with self._store_lock(conversation_id):
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
        with self._store_lock(*conversation_ids):
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

        return self._mutate_meta(conversation_id, lambda record: self._apply_permission_mode(record, next_mode))

    @staticmethod
    def _apply_permission_mode(record: ConversationRecord, next_mode: str) -> None:
        if next_mode == "plan" and record.permission_mode != "plan":
            record.permission_previous_mode = record.permission_mode
        elif next_mode != "plan":
            record.permission_previous_mode = ""
        record.permission_mode = next_mode
        record.permission_mode_revision += 1

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
        execution_cwd: str | None = None,
    ) -> ConversationRecord | None:
        return self._mutate_meta(conversation_id, lambda record: self._apply_workspace_binding(record,
            workspace_root=workspace_root, git_branch=git_branch, worktree_path=worktree_path,
            git_isolated=git_isolated, execution_cwd=execution_cwd))

    def _apply_workspace_binding(self, record: ConversationRecord, *, workspace_root: str = "", git_branch: str = "",
                                 worktree_path: str = "", git_isolated: bool | None = None,
                                 execution_cwd: str | None = None, update_sidebar: bool = True) -> None:
        from backend.conversations.sidebar_order import conversation_project_key

        previous_project = conversation_project_key(record)
        previous_directory = record.worktree_path or record.workspace_root
        if record.worktree_path and record.worktree_path != worktree_path:
            from backend.conversations.thread_resources import worktree_identity
            previous_identity = worktree_identity(record.worktree_path)
            record.resource_references = [item for item in record.resource_references
                if item["attachment_type"] != "worktree" or item["identity_key"] != previous_identity]
        if execution_cwd is not None:
            record.execution_cwd = str(Path(execution_cwd).expanduser().resolve())
        elif not record.execution_cwd:
            record.execution_cwd = str(Path(worktree_path or workspace_root).expanduser().resolve()) if worktree_path or workspace_root else record.execution_cwd
        elif previous_directory and (worktree_path or workspace_root) and previous_directory != (worktree_path or workspace_root):
            current_directory = Path(record.execution_cwd).expanduser().resolve()
            previous_root = Path(previous_directory).expanduser().resolve()
            if current_directory.is_relative_to(previous_root):
                record.execution_cwd = str(Path(worktree_path or workspace_root).expanduser().resolve() / current_directory.relative_to(previous_root))
        record.workspace_root = workspace_root
        record.git_branch = git_branch
        record.worktree_path = worktree_path
        if worktree_path:
            from backend.conversations.thread_resources import set_worktree_reference
            set_worktree_reference(record, worktree_path, workspace_root)
        if git_isolated is not None:
            record.git_isolated = bool(git_isolated)
        record.workspace_binding_revision += 1
        if update_sidebar and conversation_project_key(record) != previous_project:
            self._prepend_sidebar_occurrence(record, recent=False)

    def bind_execution_cwd(self, conversation_id: str, actual_cwd: str) -> ConversationRecord | None:
        """Fix an old unbound thread to the directory of its next real operation."""
        def mutate(record: ConversationRecord) -> None:
            if not record.execution_cwd:
                record.execution_cwd = str(Path(actual_cwd).expanduser().resolve())
                record.workspace_binding_revision += 1

        return self._mutate_meta(conversation_id, mutate)

    def commit_started_turn_settings(self, conversation_id: str, *, execution_cwd: str = "",
                                     expected_workspace_binding_revision: int | None = None,
                                     plan_slug: str = "") -> ConversationRecord | None:
        """Persist automatic/resumed turn ownership after its actual start fact."""
        from backend.agent.plans import PLAN_SLUG_KEY, PlanFileError, plan_slug_from_snapshot

        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            changed = False
            if (execution_cwd and not record.execution_cwd
                    and record.workspace_binding_revision == expected_workspace_binding_revision):
                record.execution_cwd = execution_cwd
                record.workspace_binding_revision += 1
                changed = True
            if plan_slug:
                current_slug = plan_slug_from_snapshot(record.context_snapshot)
                if current_slug and current_slug != plan_slug:
                    raise PlanFileError("The conversation plan owner changed while this turn was being prepared")
                if not current_slug:
                    record.context_snapshot = {**record.context_snapshot, PLAN_SLUG_KEY: plan_slug}
                    changed = True
            if changed:
                record.updated_at = utc_now_iso()
                self._commit_record(record)
                self._cache_record(record)
            return record

    def register_worktree(
        self, conversation_id: str, *, path: str, backend: str, workspace_root: str,
    ) -> ConversationRecord | None:
        from backend.atomic_io import canonical_file_path_key

        path_key = canonical_file_path_key(path)
        def mutate(record: ConversationRecord) -> None:
            record.worktree_registrations = [
                entry for entry in record.worktree_registrations
                if canonical_file_path_key(entry["path"]) != path_key
            ]
            record.worktree_registrations.append({
                "path": path, "backend": backend, "workspace_root": workspace_root,
            })
            from backend.conversations.thread_resources import set_worktree_reference
            set_worktree_reference(record, path, workspace_root, backend=backend)

        return self._mutate_meta(conversation_id, mutate)

    def unregister_worktree(self, conversation_id: str, *, path: str) -> ConversationRecord | None:
        from backend.atomic_io import canonical_file_path_key

        path_key = canonical_file_path_key(path)
        def mutate(record: ConversationRecord) -> None:
            record.worktree_registrations = [
                entry for entry in record.worktree_registrations
                if canonical_file_path_key(entry["path"]) != path_key
            ]
            record.resource_references = [item for item in record.resource_references
                if item["attachment_type"] != "worktree" or item["identity_key"] != path_key]

        return self._mutate_meta(conversation_id, mutate)

    def mutate_goal(self, conversation_id, mutation):
        return self._mutate_meta(conversation_id, lambda record: setattr(record, "goal", mutation(dict(record.goal))))

    def update_goal(
        self,
        conversation_id: str,
        goal: dict[str, Any],
    ) -> ConversationRecord | None:
        return self._mutate_meta(
            conversation_id,
            lambda record: setattr(record, "goal", dict(goal)),
        )

    def update_agent_mode(
        self, conversation_id: str, agent_mode: str, *, expected_agent_mode_revision: int | None = None,
    ) -> ConversationRecord | None:
        mode = normalize_conversation_agent_mode(agent_mode)
        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id, copy_history=False)
            if record is None:
                return None
            if expected_agent_mode_revision is not None and record.agent_mode_revision != expected_agent_mode_revision:
                return record
            record.agent_mode = mode
            # Even selecting the same value is a newer explicit configuration.
            # Progress/transcript revisions do not advance this identity.
            record.agent_mode_revision += 1
            record.updated_at = utc_now_iso()
            record.message_count = len(record.transcript)
            self._commit_metadata(record)
            self._cache_record(record)
            return record

    def set_conversation_pinned(self, conversation_id: str, pinned: bool) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> bool:
            if pinned:
                self.thread_sections.remove_thread(record.id)
            if record.pinned == pinned:
                return False
            self._update_pinned_order_membership(record.id, pinned)
            record.pinned = pinned
            record.pinned_at = utc_now_iso() if pinned else ""
            return True
        return self._mutate_meta(conversation_id, mutate, touch_updated_at=False)

    def _read_pinned_order(self) -> dict[str, Any]:
        if not self._pinned_order_path.exists():
            return {"thread_ids": [], "sort_mode": "manual"}
        return json.loads(self._pinned_order_path.read_text(encoding="utf-8"))

    @staticmethod
    def _ordered_pinned_ids(conversations: list[ConversationSummary], saved_ids: list[str]) -> list[str]:
        pinned = sorted((conversation for conversation in conversations if conversation.pinned and conversation.conversation_type == "main"),
                        key=lambda conversation: (conversation.pinned_at, conversation.id))
        current = {conversation.id for conversation in pinned}
        return [identifier for identifier in saved_ids if identifier in current] + [conversation.id for conversation in pinned if conversation.id not in saved_ids]

    def get_pinned_order(self, conversations: list[ConversationSummary]) -> dict[str, Any]:
        with file_mutation_locks([self._pinned_order_path]):
            saved = self._read_pinned_order()
            return {"pinned_conversation_ids": self._ordered_pinned_ids(conversations, saved["thread_ids"]),
                    "pinned_sort_mode": saved["sort_mode"]}

    def _update_pinned_order_membership(self, conversation_id: str, pinned: bool) -> None:
        with file_mutation_locks([self._pinned_order_path]):
            saved = self._read_pinned_order()
            identifiers = self._ordered_pinned_ids(self.list_conversations(), saved["thread_ids"])
            identifiers = [identifier for identifier in identifiers if identifier != conversation_id]
            if pinned:
                identifiers.append(conversation_id)
            atomic_write_text(self._pinned_order_path, json.dumps({**saved, "thread_ids": identifiers}, ensure_ascii=False), encoding="utf-8")

    def reorder_pinned_conversations(self, moved_id: str, before_id: str | None, visible_ids: list[str]) -> list[str]:
        # Source _P / WNi replaces the visible slots; a hidden pinned thread
        # keeps its original place instead of being appended or dropped.
        with file_mutation_locks([self._pinned_order_path]):
            saved = self._read_pinned_order()
            current = self._ordered_pinned_ids(self.list_conversations(), saved["thread_ids"])
            visible = set(visible_ids)
            if len(visible) != len(visible_ids) or not visible.issubset(current) or moved_id not in visible or before_id is not None and before_id not in visible:
                raise ValueError("The pinned threads changed before this reorder")
            if before_id == moved_id:
                return current
            next_visible = [identifier for identifier in visible_ids if identifier != moved_id]
            position = len(next_visible) if before_id is None else next_visible.index(before_id)
            next_visible.insert(position, moved_id)
            replacements = iter(next_visible)
            ordered = [next(replacements) if identifier in visible else identifier for identifier in current]
            if ordered != current or saved["sort_mode"] != "manual":
                atomic_write_text(self._pinned_order_path, json.dumps({"thread_ids": ordered, "sort_mode": "manual"}, ensure_ascii=False), encoding="utf-8")
                self._advance_store_revision_unlocked()
            return ordered

    def _read_sidebar_order(self) -> dict[str, Any]:
        from backend.conversations.sidebar_order import empty_sidebar_order

        if not self._sidebar_order_path.exists():
            return empty_sidebar_order()
        return json.loads(self._sidebar_order_path.read_text(encoding="utf-8"))

    def _write_sidebar_order(self, state: dict[str, Any], *, advance_inventory: bool = True) -> None:
        # Callers own the repository store lock, including the membership they
        # used for this change. Sorting never rewrites conversation generations.
        state = {**state, "revision": state["revision"] + 1}
        atomic_write_text(self._sidebar_order_path, json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if advance_inventory:
            self._advance_store_revision_unlocked()

    def get_sidebar_order(self, conversations: list[ConversationSummary]) -> dict[str, Any]:
        from backend.conversations.sidebar_order import thread_orders

        with self._store_lock():
            state = self._read_sidebar_order()
            recent, projects = thread_orders(conversations, state)
            return {"sidebar_order": {**state, "recent_thread_ids": recent, "project_thread_orders": projects}}

    def set_sidebar_sort_mode(self, mode: str, project_seeds: dict[str, list[str]]) -> None:
        from backend.conversations.sidebar_order import conversation_project_key, project_key, thread_orders

        if mode not in {"manual", "updated_at"}:
            raise ValueError("Sidebar sort mode must be manual or updated_at")
        with self._store_lock():
            state = self._read_sidebar_order()
            if state["sort_mode"] == mode:
                return
            summaries = self._list_conversations_unlocked()
            by_id = {item.id: item for item in summaries if item.conversation_type == "main"}
            orders = dict(state["project_thread_orders"])
            if mode == "manual":
                for raw_project, identifiers in project_seeds.items():
                    key = project_key(raw_project)
                    if len(set(identifiers)) != len(identifiers) or any(
                        identifier not in by_id or conversation_project_key(by_id[identifier]) != key for identifier in identifiers
                    ):
                        raise ValueError("The project chats changed before manual sorting was selected")
                    # Source qoo -> Joo/sAr seeds visible projects once and
                    # restores an existing manual order on later mode changes.
                    if key not in orders:
                        orders[key] = identifiers
                recent, _ = thread_orders(summaries, state)
                state = {**state, "recent_thread_ids": recent, "project_thread_orders": orders}
            self._write_sidebar_order({**state, "sort_mode": mode})

    def reorder_sidebar_threads(self, project_id: str | None, moved_id: str, before_id: str | None, visible_ids: list[str]) -> None:
        from backend.conversations.sidebar_order import conversation_project_key, project_key, replace_visible_slots, thread_orders

        with self._store_lock():
            state = self._read_sidebar_order()
            if state["sort_mode"] != "manual":
                raise ValueError("Select manual sidebar sorting before reordering chats")
            summaries = self._list_conversations_unlocked()
            recent, projects = thread_orders(summaries, state)
            key = project_key(project_id) if project_id is not None else None
            eligible = {item.id for item in summaries if item.conversation_type == "main" and not item.archived and not item.pinned
                        and (key is None or conversation_project_key(item) == key)}
            if not set(visible_ids).issubset(eligible):
                raise ValueError("The project chats changed before this reorder")
            current = recent if key is None else projects.get(key, [])
            ordered = replace_visible_slots(current, visible_ids, moved_id, before_id)
            if ordered == current:
                return
            if key is None:
                state = {**state, "recent_thread_ids": ordered}
            else:
                state = {**state, "project_thread_orders": {**state["project_thread_orders"], key: ordered}}
            self._write_sidebar_order(state)

    def reorder_sidebar_projects(self, moved_id: str, before_id: str | None, visible_ids: list[str], opened_ids: list[str]) -> None:
        from backend.conversations.sidebar_order import project_key, replace_visible_slots

        with self._store_lock():
            state = self._read_sidebar_order()
            opened = [project_key(identifier) for identifier in opened_ids]
            visible = [project_key(identifier) for identifier in visible_ids]
            if not set(visible).issubset(opened):
                raise ValueError("The projects changed before this reorder")
            # Source Ylr keeps saved slots outside the currently shown groups.
            current = state["project_order"] + [identifier for identifier in opened if identifier not in state["project_order"]]
            ordered = replace_visible_slots(current, visible, project_key(moved_id), project_key(before_id) if before_id is not None else None)
            if ordered != current:
                self._write_sidebar_order({**state, "project_order": ordered})

    def _prepend_sidebar_occurrence(self, record: ConversationRecord, *, recent: bool = True) -> None:
        from backend.conversations.sidebar_order import conversation_project_key

        if record.conversation_type != "main":
            return
        # The occurrence file is shared by every conversation. A conversation's
        # manifest lock does not serialize two moves into the same project, so
        # this read-modify-write takes the store lock like the explicit
        # sort/reorder paths do.
        with self._store_lock():
            state = self._read_sidebar_order()
            if state["sort_mode"] != "manual":
                return
            key = conversation_project_key(record)
            if key:
                identifiers = state["project_thread_orders"].get(key, [])
                state = {**state, "project_thread_orders": {**state["project_thread_orders"],
                    key: [record.id, *[identifier for identifier in identifiers if identifier != record.id]]}}
            if recent:
                state = {**state, "recent_thread_ids": [record.id, *[identifier for identifier in state["recent_thread_ids"] if identifier != record.id]]}
            self._write_sidebar_order(state, advance_inventory=False)

    def set_conversation_read_state(self, conversation_id: str, has_unread_turn: bool) -> ConversationRecord | None:
        def mutate(record: ConversationRecord) -> bool:
            if record.has_unread_turn == has_unread_turn:
                return False
            record.has_unread_turn = has_unread_turn
            return True
        return self._mutate_meta(conversation_id, mutate, touch_updated_at=False)

    def set_conversation_project(self, conversation_id: str, project_id: str) -> ConversationRecord | None:
        # Codex thread/metadata/update assigns a project independently of cwd.
        # None retains the existing inferred grouping; "" explicitly clears it.
        def mutate(record: ConversationRecord) -> bool:
            if record.project_id == project_id and not record.pinned:
                return False
            if record.pinned:
                self._update_pinned_order_membership(record.id, False)
            record.project_id = project_id
            record.pinned = False
            record.pinned_at = ""
            self._prepend_sidebar_occurrence(record, recent=False)
            return True
        return self._mutate_meta(conversation_id, mutate, touch_updated_at=False)

    @staticmethod
    def _belongs_to_project(record: ConversationRecord | ConversationSummary, project_path: str, workspace_roots: tuple[str, ...]) -> bool:
        if record.project_id is not None:
            return bool(record.project_id) and canonical_file_path_key(record.project_id) == canonical_file_path_key(project_path)
        root = record.worktree_path or record.workspace_root
        return bool(root) and any(canonical_file_path_key(root) == canonical_file_path_key(folder) for folder in workspace_roots)

    def project_conversation_ids(self, project_path: str, workspace_roots: tuple[str, ...]) -> list[str]:
        keys: dict[str, str] = {}

        def path_key(path: str) -> str:
            if path not in keys:
                keys[path] = canonical_file_path_key(path)
            return keys[path]

        with self._store_lock():
            self._ensure_summary_index_loaded()
            selected = []
            for record in list((self._summary_index or {}).values()):
                if record.project_id is not None:
                    matches = bool(record.project_id) and path_key(record.project_id) == path_key(project_path)
                else:
                    root = record.worktree_path or record.workspace_root
                    matches = bool(root) and any(path_key(root) == path_key(folder) for folder in workspace_roots)
                if matches:
                    selected.append(record)
            selected.sort(key=lambda record: (record.created_at, record.id), reverse=True)
            return [record.id for record in selected]

    def remove_project_memberships(self, project_path: str, workspace_roots: tuple[str, ...]) -> list[str]:
        """Source removeLocal detaches membership without changing pins or execution."""
        changed: list[str] = []
        for conversation_id in self.project_conversation_ids(project_path, workspace_roots):
            def mutate(record: ConversationRecord) -> bool:
                if not self._belongs_to_project(record, project_path, workspace_roots):
                    return False
                record.project_id = ""
                changed.append(record.id)
                return True
            self._mutate_meta(conversation_id, mutate, touch_updated_at=False)
        return changed

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> ConversationRecord | None:
        cleaned = title.strip()
        if not cleaned:
            raise ValueError("Conversation name must not be empty")
        return self._mutate_meta(
            conversation_id,
            lambda record: setattr(record, "title", cleaned),
        )

    def set_archived(
        self, conversation_id: str, archived: bool
    ) -> ConversationSummary | None:
        """Publish archive metadata and return its summary without loading history."""
        def mutate(record: ConversationRecord) -> None:
            record.archived = bool(archived)
            record.archived_at = utc_now_iso() if archived else ""

        with self._store_lock(conversation_id):
            manifest = self._read_manifest(conversation_id, log_errors=False)
            if manifest is None:
                record = self._mutate_meta(conversation_id, mutate)
                return record.to_summary() if record is not None else None
            if self._manifest_is_deleted(manifest):
                return None
            metadata = manifest.get("metadata")
            if metadata is None:
                metadata = self._read_generation_metadata(conversation_id, manifest["current_generation"])
            record = ConversationRecord.from_dict(metadata)
            record.revision = self._manifest_revision(manifest)
            mutate(record)
            record.updated_at = utc_now_iso()
            self._commit_metadata(record)
            summary = record.to_summary()
            # This record contains metadata only. A later history read must
            # load the unchanged checkpoint and its existing projection log.
            with self._process_lock:
                self._record_cache.pop(conversation_id, None)
                self._record_cache_stamps.pop(conversation_id, None)
                if self._summary_index is not None:
                    self._summary_index[conversation_id] = copy.deepcopy(summary)
                    self._summary_index_stamps[conversation_id] = self._summary_disk_stamp(conversation_id)
            return summary

    def update_model_selection(
        self, conversation_id: str, *, provider: str, model: str,
        reasoning_effort: str | None = None,
        selection_mode: str | None = None,
        only_if_unset: bool = False,
    ) -> ConversationRecord | None:
        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            if only_if_unset and record.model_selection:
                return record
            selection = {**record.model_selection, "provider": provider, "model": model}
            if reasoning_effort is not None:
                selection["reasoning_effort"] = reasoning_effort
            if selection_mode is not None:
                selection["selection_mode"] = selection_mode
            if record.model_selection == selection:
                return record
            record.model_selection = selection
            record.updated_at = utc_now_iso()
            self._commit_metadata(record)
            self._cache_record(record)
            return record

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
        compaction_item: dict[str, Any] | None = None,
        operation_turn_id: str = "",
    ) -> ConversationRecord | None:
        """Commit the compacted context and its metadata as one generation."""
        with self._store_lock(conversation_id):
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
            if compaction_item is not None:
                from backend.agent.turn_state import project_context_compaction_item
                blocks: list[dict[str, Any]] = []
                project_context_compaction_item(blocks, compaction_item)
                record.transcript.append({"id": compaction_item["id"], "role": "system", "content": "",
                    "timestamp": compaction_item["started_at"], "turn_id": operation_turn_id,
                    "blocks": blocks})
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def ensure_plan_owner(
        self, conversation_id: str, *, workspace_root: str | Path | None = None,
    ) -> ConversationRecord | None:
        """Allocate one conversation plan slug from the latest durable snapshot."""
        from backend.agent.plans import ensure_plan_slug

        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            snapshot = dict(record.context_snapshot or {})
            ensure_plan_slug(snapshot, workspace_root or record.workspace_root or None)
            if snapshot != record.context_snapshot:
                record.context_snapshot = snapshot
                record.updated_at = utc_now_iso()
                self._commit_record(record)
                self._cache_record(record)
            return record

    def save_context_snapshot(
        self, conversation_id: str, context_snapshot: dict[str, Any]
    ) -> ConversationRecord | None:
        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None
            record.context_snapshot = dict(context_snapshot)
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            return record

    def append_user_goal_update(self, conversation_id: str, *, objective: str | None = None,
        status: str | None = None, clear: bool = False, context_builder=None):
        from backend.agent.context import ContextBuilder

        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id)
            if record is None:
                return None, None
            base = context_builder.export_snapshot() if context_builder is not None else dict(record.context_snapshot)
            prepared = ContextBuilder()
            prepared.load_snapshot(base)
            message = prepared.append_user_goal_update(objective=objective, status=status, clear=clear)
            updated = prepared.export_snapshot()
            record.context_snapshot = {**record.context_snapshot, **base,
                "history": [*base.get("history", []), *([updated["history"][-1]] if context_builder is None else [])],
                "guardian_evidence": updated["guardian_evidence"]}
            record.updated_at = utc_now_iso()
            self._commit_record(record)
            self._cache_record(record)
            if context_builder is not None:
                context_builder.append_recorded_user_goal_update(message, append_history=False)
            return record, message

    def patch_context_snapshot(
        self,
        conversation_id: str,
        patch: dict[str, Any],
        *,
        revision: int | None = None,
        revision_key: str = "_snapshot_patch_revision",
    ) -> ConversationRecord | None:
        with self._store_lock(conversation_id):
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
        mutate: Callable[[ConversationRecord], bool | None],
        *, touch_updated_at: bool = True,
    ) -> ConversationRecord | None:
        with self._store_lock(conversation_id):
            record = self._load_record_for_mutation(conversation_id, copy_history=False)
            if record is None:
                return None
            if mutate(record) is False:
                return record
            if touch_updated_at:
                record.updated_at = utc_now_iso()
            record.message_count = len(record.transcript)
            self._commit_metadata(record)
            self._cache_record(record)
            return record

    def _commit_metadata(
        self, record: ConversationRecord, *,
        assistant_message: dict[str, Any] | None = None,
        context_delta: dict[str, Any] | None = None,
    ) -> None:
        """Publish metadata without rewriting the immutable transcript checkpoint."""
        manifest = self._read_manifest(record.id, log_errors=False)
        if manifest is None or record.revision != self._manifest_revision(manifest):
            # Legacy storage and recovery from a damaged checkpoint need a new
            # complete checkpoint before metadata can refer to it.
            self._commit_record(record)
            return
        revision = record.revision + 1
        metadata = {**record.to_meta_dict(), "revision": revision}
        payload = {**manifest, "version": _STORAGE_MANIFEST_VERSION, "revision": revision, "metadata": metadata}
        if context_delta is not None:
            metadata["content_revision"] = revision
            metadata["content_updated_at"] = record.updated_at
            generation = manifest["current_generation"]
            previous = self._load_partial_projection(record.id, manifest)
            pointer = manifest.get("projection_log")
            partial_projection_state = {
                "assistant_message": assistant_message if assistant_message is not None else previous.get("assistant_message"),
                "context_delta": compose_context_snapshot_deltas(previous.get("context_delta", {}), context_delta),
            }
            position = append_projection(
                self._partial_projection_path(record.id, generation),
                committed_bytes=pointer["bytes"] if pointer else 0, revision=revision,
                previous=previous, current=partial_projection_state,
                seed_revision=(manifest.get("projection_revision", record.revision) if previous and pointer is None else None),
            )
            payload.pop("partial_projection", None)
            payload["projection_log"] = {"generation": generation, "bytes": position, "revision": revision}
            payload["projection_revision"] = revision
        with self._store_lock():
            self._advance_store_revision_unlocked()
            self._safe_write_text(
                self._manifest_path_for(record.id),
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        record.revision = revision
        record.content_revision = metadata["content_revision"]
        record.content_updated_at = metadata["content_updated_at"]
        if context_delta is not None:
            self._cache_partial_projection(record.id, payload["projection_log"], partial_projection_state)

    def _cache_record(self, record: ConversationRecord, *, copy_record: bool = True) -> None:
        with self._process_lock:
            self._record_cache[record.id] = copy.deepcopy(record) if copy_record else record
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
            for key in ("projection_log", "previous_projection_log"):
                if key in manifest:
                    paths.append(self._partial_projection_path(conversation_id, manifest[key]["generation"]))
        stamps: list[tuple[int, int]] = []
        for path in paths:
            try:
                stat = history_stat(path)
            except OSError:
                stamps.append((-1, -1))
            else:
                stamps.append((stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

    def _summary_disk_stamp(self, conversation_id: str) -> tuple[tuple[int, int], ...]:
        # The manifest publishes the summary independently of immutable
        # provider history. Only older layouts need their metadata file.
        paths = [self._manifest_path_for(conversation_id)]
        manifest = self._read_manifest(conversation_id, log_errors=False)
        if manifest is None:
            paths.extend((self._meta_path_for(conversation_id), self._legacy_path_for(conversation_id)))
        elif not self._manifest_is_deleted(manifest) and manifest.get("metadata") is None:
            paths.append(self._generation_paths(conversation_id, manifest["current_generation"])[0])
        stamps = []
        for path in paths:
            try:
                stat = path.stat()
            except OSError:
                stamps.append((-1, -1))
            else:
                stamps.append((stat.st_mtime_ns, stat.st_size))
        return tuple(stamps)

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
    def _manifest_revision(manifest: dict[str, Any]) -> int:
        return int(manifest.get("revision") or manifest.get("current_generation") or 0)

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
            version = payload.get("version")
            if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2, 3, 4, 5, 6, 7, _STORAGE_MANIFEST_VERSION}:
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
            if "metadata" in payload:
                metadata = payload["metadata"]
                revision = payload.get("revision")
                if not isinstance(metadata, dict) or metadata.get("id") != conversation_id:
                    raise ValueError("manifest metadata conversation id mismatch")
                if isinstance(revision, bool) or not isinstance(revision, int) or revision < generations[0]:
                    raise ValueError("manifest metadata revision is invalid")
                if metadata.get("revision") != revision:
                    raise ValueError("manifest metadata revision mismatch")
            if "previous_metadata" in payload:
                metadata = payload["previous_metadata"]
                if not isinstance(metadata, dict) or metadata.get("id") != conversation_id:
                    raise ValueError("previous metadata conversation id mismatch")
                revision = metadata.get("revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or not 0 < revision < generations[0]:
                    raise ValueError("previous metadata revision is invalid")
            if "partial_projection" in payload:
                revision = payload.get("projection_revision")
                if isinstance(revision, bool) or not isinstance(revision, int) or not generations[0] <= revision <= self._manifest_revision(payload):
                    raise ValueError("partial projection revision is invalid")
            for key, generation_key, revision_limit in (
                ("projection_log", "current_generation", self._manifest_revision(payload)),
                ("previous_projection_log", "previous_generation", generations[0] - 1),
            ):
                if key not in payload:
                    continue
                pointer = payload[key]
                if not isinstance(pointer, dict) or any(
                    isinstance(pointer.get(field), bool) or not isinstance(pointer.get(field), int)
                    for field in ("generation", "bytes", "revision")
                ):
                    raise ValueError("invalid projection log pointer")
                if (pointer["generation"] != payload.get(generation_key) or pointer["bytes"] <= 0
                        or not pointer["generation"] <= pointer["revision"] <= revision_limit):
                    raise ValueError("projection log does not address its generation")
            self._manifest_cache[conversation_id] = (stamp, payload)
            return payload
        except Exception as exc:
            self._manifest_cache.pop(conversation_id, None)
            if log_errors:
                logger.error("Failed to read conversation manifest for %s: %s", conversation_id, exc)
            raise ConversationStorageCorruptError(
                f"Conversation manifest for '{conversation_id}' is corrupt: {exc}"
            ) from exc

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
        record.transcript = _normalize_loaded_transcript(record.transcript)
        for index, message in enumerate(record.transcript):
            if not message["id"]:
                message["id"] = f"legacy-message-{index}"
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
        current_revision = self._manifest_revision(manifest or {})
        expected_revision = max(0, int(getattr(record, "revision", 0) or 0))
        # Validate the current checkpoint, using its cached disk identity when
        # available. Only read an older checkpoint when recovery needs it.
        # A reader may have returned ``previous_generation`` because the
        # manifest's current generation is damaged.  That is a recoverable
        # storage state: publish a new generation from the readable record
        # instead of treating the fallback as a stale detached writer.  Any
        # other revision mismatch remains a real write conflict.
        readable_generations: list[int] = []
        generation_errors: list[tuple[int, Exception]] = []
        cached = self._record_cache.get(record.id)
        cache_is_current = (
            cached is not None and cached.revision == current_revision
            and self._record_cache_stamps.get(record.id) == self._record_disk_stamp(record.id)
        )
        readable_record = None
        for index, generation in enumerate(known_generations):
            try:
                readable_record = (
                    cached if index == 0 and cache_is_current else
                    self._read_committed_generation(record.id, manifest, index, log_errors=False)
                )
            except (ConversationStorageCorruptError, ValueError, TypeError, KeyError) as exc:
                generation_errors.append((generation, exc))
                logger.warning(
                    "Conversation %s generation %s is unreadable while committing: %s",
                    record.id,
                    generation,
                    exc,
                )
            else:
                readable_generations.append(generation)
                break

        if known_generations and not readable_generations:
            detail = "; ".join(
                f"generation {generation}: {error}"
                for generation, error in generation_errors
            )
            raise ConversationStorageCorruptError(
                f"Conversation '{record.id}' has no readable generation to commit from"
                + (f": {detail}" if detail else "")
            )

        current_generation_corrupt = bool(
            current_generation and current_generation not in readable_generations
        )
        recovered_revision = readable_record.revision if readable_record is not None else 0
        if expected_revision != current_revision and not (
            current_generation_corrupt and expected_revision == recovered_revision
        ):
            raise ConversationWriteConflict(
                record.id,
                expected=expected_revision,
                current=current_revision,
            )

        previous_generation: int | None = (
            readable_generations[0] if readable_generations else None
        )
        if current_generation_corrupt:
            logger.warning(
                "Recovering conversation %s from generation %s after corrupt current generation %s",
                record.id,
                previous_generation,
                current_generation,
            )

        # On the first mutation of a split/legacy record, preserve its exact
        # pre-mutation state as the explicit fallback generation.
        if manifest is None:
            legacy = self._load_legacy_record(record.id, log_errors=False)
            if legacy is not None:
                previous_generation = 1
                self._write_generation(legacy, previous_generation)
                known_generations.append(previous_generation)

        next_generation = max([current_revision, *known_generations]) + 1
        previous_record = cached if cache_is_current else self._load_record_for_mutation(record.id)
        previous_content_version = (record.content_revision, record.content_updated_at)
        if previous_record is None or previous_record.transcript != record.transcript:
            record.content_revision = next_generation
            record.content_updated_at = record.updated_at
        else:
            record.content_revision = previous_record.content_revision
            record.content_updated_at = previous_record.content_updated_at
        record.revision = next_generation
        # Advance before publishing the record. A crash can leave a harmless
        # gap in the global inventory sequence, but can never publish a record
        # whose authoritative list revision moved backwards. Inventory readers
        # share the store lock across the revision and manifest publication.
        manifest_payload = {
            "schema": _STORAGE_MANIFEST_SCHEMA,
            "version": _STORAGE_MANIFEST_VERSION,
            "conversation_id": record.id,
            "current_generation": next_generation,
            "previous_generation": previous_generation,
            "revision": next_generation,
            "metadata": record.to_meta_dict(),
        }
        if manifest is not None:
            previous_metadata = manifest.get(
                "metadata" if previous_generation == current_generation else "previous_metadata"
            )
            if isinstance(previous_metadata, dict):
                manifest_payload["previous_metadata"] = previous_metadata
            previous_projection = manifest.get(
                "partial_projection" if previous_generation == current_generation else "previous_projection"
            )
            if previous_projection is not None:
                manifest_payload["previous_projection"] = previous_projection
            previous_log = manifest.get(
                "projection_log" if previous_generation == current_generation else "previous_projection_log"
            )
            if previous_log is not None:
                manifest_payload["previous_projection_log"] = previous_log
        manifest_path = self._manifest_path_for(record.id)
        try:
            self._write_generation(record, next_generation)
            with self._store_lock():
                self._advance_store_revision_unlocked()
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
            if record.revision != next_generation:
                record.content_revision, record.content_updated_at = previous_content_version
            raise
        manifest_stat = manifest_path.stat()
        self._manifest_cache[record.id] = (
            (manifest_stat.st_mtime_ns, manifest_stat.st_size),
            manifest_payload,
        )
        # Keep the original legacy bytes after migration. The manifest owns
        # subsequent reads; an explicit conversation delete removes these files.
        self._cleanup_generations(
            record.id,
            keep={
                next_generation,
                *({previous_generation} if previous_generation is not None else set()),
                # Keep damaged generation files as forensic evidence when a
                # readable predecessor was used for recovery. They are no
                # longer authoritative, but deleting them would hide the
                # storage failure that triggered the recovery.
                *(set(known_generations) if current_generation_corrupt else set()),
            },
        )

    def _write_generation(self, record: ConversationRecord, generation: int) -> None:
        meta_path, transcript_path, snapshot_path = self._generation_paths(record.id, generation)
        transcript_text, transcript_index = encode_transcript(record.transcript)
        metadata = {**record.to_meta_dict(), "transcript_index_version": 1,
                    "transcript_index": transcript_index,
                    "public_context_snapshot": project_public_conversation(record, include_transcript=False)["context_snapshot"]}
        self._safe_write_text(
            meta_path,
            json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
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
    ) -> ConversationRecord:
        meta_path, transcript_path, snapshot_path = self._generation_paths(conversation_id, generation)
        try:
            if not meta_path.exists() or not history_exists(transcript_path) or not history_exists(snapshot_path):
                raise FileNotFoundError(f"generation {generation} is incomplete")
            meta_payload = self._read_generation_metadata(conversation_id, generation)
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
            raise ConversationStorageCorruptError(
                f"Conversation '{conversation_id}' generation {generation} is corrupt: {exc}"
            ) from exc

    def _read_generation_metadata(self, conversation_id: str, generation: int) -> dict[str, Any]:
        path = self._generation_paths(conversation_id, generation)[0]
        try:
            value = json.loads(self._safe_read_text(path))
            if not isinstance(value, dict) or value.get("id") != conversation_id:
                raise ValueError("generation metadata owner mismatch")
            return value
        except (OSError, ValueError) as exc:
            raise ConversationStorageCorruptError(f"Conversation metadata is unreadable: {conversation_id}") from exc

    def _cleanup_generations(self, conversation_id: str, *, keep: set[int]) -> None:
        safe_id = self._safe_id(conversation_id)
        pattern = re.compile(
            rf"^{re.escape(safe_id)}\.g(\d+)\.(?:meta\.json|transcript\.jsonl|snapshot\.json|projection\.jsonl)(?:\.zst)?$"
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
    def _store_lock(self, *conversation_ids: str):
        if conversation_ids:
            with file_mutation_locks(self._manifest_path_for(cid) for cid in conversation_ids):
                yield
            return
        with self._process_lock:
            try:
                with self._store_file_lock.acquire(timeout=5.0):
                    yield
            except FileLockTimeout as exc:
                raise TimeoutError(
                    f"Timed out waiting for conversation store lock: {self._store_lock_path}"
                ) from exc

    @contextmanager
    def command_result_publication(self, conversation_ids: tuple[str, ...]):
        """Keep result archival ordered with deletion tombstones."""
        owners = tuple(sorted({value for value in conversation_ids if _CONVERSATION_ID_PATTERN.fullmatch(value)}))
        if not owners:
            yield set()
            return
        with self._store_lock(*owners):
            deleted = set()
            for owner in owners:
                manifest = self._read_manifest(owner, log_errors=False)
                if manifest is not None and self._manifest_is_deleted(manifest):
                    deleted.add(owner)
            yield deleted

    def _safe_write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        for attempt in range(5):
            try:
                if is_history_path(path):
                    with file_mutation_locks([path]):
                        atomic_write_text(path, text, encoding=encoding)
                        compressed_history_path(path).unlink(missing_ok=True)
                else:
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
                return read_history_bytes(path).decode(encoding) if is_history_path(path) else path.read_text(encoding=encoding)
            except HistoryCompressionError:
                raise
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
        with self._store_lock():
            revision = self._read_store_revision_unlocked() + 1
            self._safe_write_text(
                self._store_revision_path,
                f"{revision}\n",
                encoding="utf-8",
            )
            return revision

    def _load_record_for_mutation(
        self, conversation_id: str, *, copy_history: bool = True,
    ) -> ConversationRecord | None:
        cached = self._record_cache.get(conversation_id)
        if (
            cached is not None
            and self._record_cache_stamps.get(conversation_id)
            == self._record_disk_stamp(conversation_id)
        ):
            if copy_history:
                return copy.deepcopy(cached)
            record = copy.copy(cached)
            record.transcript = cached.transcript.copy()
            return record
        loaded = self._load_record(conversation_id)
        return copy.deepcopy(loaded) if loaded is not None else None

    def get_projection_context(self, conversation_id: str) -> tuple[int, dict[str, Any]] | None:
        """Read the owned context as an immutable delta base; callers never mutate it."""
        with self._store_lock(conversation_id):
            cached = self._record_cache.get(conversation_id)
            if cached is None or self._record_cache_stamps.get(conversation_id) != self._record_disk_stamp(conversation_id):
                cached = self._load_record(conversation_id)
                if cached is None:
                    return None
                self._cache_record(cached, copy_record=False)
            return cached.revision, cached.context_snapshot

    def _load_record(self, conversation_id: str) -> ConversationRecord | None:
        if self._manifest_path_for(conversation_id).exists():
            return self._load_committed_record(conversation_id)
        return self._load_legacy_record(conversation_id)

    def _read_committed_generation(
        self, conversation_id: str, manifest: dict[str, Any], index: int,
        *, log_errors: bool = True,
    ) -> ConversationRecord:
        """Read the exact checkpoint and overlays used by both recovery and CAS."""
        generation = self._manifest_generations(manifest)[index]
        record = self._read_generation(conversation_id, generation, log_errors=log_errors)
        metadata = manifest.get("metadata" if index == 0 else "previous_metadata")
        if isinstance(metadata, dict):
            record = ConversationRecord.from_dict({
                **metadata,
                "transcript": record.transcript,
                "context_snapshot": record.context_snapshot,
            })
        projection = self._load_partial_projection(conversation_id, manifest, previous=index > 0)
        if projection:
            record.context_snapshot = apply_context_snapshot_delta(record.context_snapshot, projection["context_delta"])
            if projection.get("assistant_message") is not None:
                message = project_public_transcript_message(projection["assistant_message"])
                found = next((i for i, item in enumerate(record.transcript) if item.get("id") == message.get("id")), None)
                if found is None:
                    record.transcript.append(message)
                else:
                    record.transcript[found] = message
                record.message_count = len(record.transcript)
        return record

    def _load_committed_record(self, conversation_id: str) -> ConversationRecord | None:
        # A reader can race two rapid commits without taking the writer lock.
        # Re-reading the atomic manifest once lets it move to the newly
        # published generation if the older one was cleaned up meanwhile.
        generation_errors: list[str] = []
        for attempt in range(2):
            manifest = self._read_manifest(conversation_id, log_errors=attempt > 0)
            if manifest is None:
                return None
            if self._manifest_is_deleted(manifest):
                return None
            for index, generation in enumerate(self._manifest_generations(manifest)):
                try:
                    record = self._read_committed_generation(
                        conversation_id, manifest, index, log_errors=attempt > 0,
                    )
                except (ConversationStorageCorruptError, ValueError, TypeError, KeyError) as exc:
                    generation_errors.append(str(exc))
                    continue
                if index > 0:
                    logger.warning(
                        "Conversation %s recovered from previous generation %s",
                        conversation_id,
                        generation,
                    )
                return record
        detail = "; ".join(dict.fromkeys(generation_errors))
        logger.error(
            "No committed generation could be loaded for conversation %s: %s",
            conversation_id,
            detail or "no readable generation",
        )
        raise ConversationStorageCorruptError(
            f"Conversation '{conversation_id}' has no readable committed generation"
            + (f": {detail}" if detail else "")
        )

    def _load_legacy_record(
        self,
        conversation_id: str,
        *,
        log_errors: bool = True,
    ) -> ConversationRecord | None:
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                meta_payload = json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
                if not isinstance(meta_payload, dict):
                    raise ValueError("legacy meta must be an object")
                transcript_path = self._transcript_path_for(conversation_id)
                snapshot_path = self._snapshot_path_for(conversation_id)
                transcript = self._read_transcript_path(
                    transcript_path,
                    strict=history_exists(transcript_path),
                )
                snapshot = self._read_snapshot_path(
                    snapshot_path,
                    strict=history_exists(snapshot_path),
                )

                if not transcript and snapshot.get("history"):
                    logger.warning(
                        "Transcript for %s is empty but snapshot contains history; using the snapshot fallback.",
                        conversation_id,
                    )
                    reconstructed: list[dict[str, Any]] = []
                    for idx, msg in enumerate(snapshot["history"]):
                        if str(msg.get("content_kind") or "").startswith("realtime_conversation."):
                            continue
                        if msg.get("role") == "assistant" and msg.get("content_kind") == "agent_message_board.notification":
                            continue
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
                        if role == "assistant" and msg.get("answer_source"):
                            rec_msg["blocks"] = [{"type": "text", "content": msg.get("content", ""), "source": msg["answer_source"]}]
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
                raise ConversationStorageCorruptError(
                    f"Conversation '{conversation_id}' legacy split record is corrupt: {exc}"
                ) from exc

        legacy_path = self._legacy_path_for(conversation_id)
        if not legacy_path.exists():
            return None
        try:
            payload = json.loads(self._safe_read_text(legacy_path, encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("legacy conversation must be an object")
            payload["transcript"] = _normalize_loaded_transcript(
                list(payload.get("transcript") or [])
            )
            return ConversationRecord.from_dict(payload)
        except Exception as exc:
            if log_errors:
                logger.error("Failed to load legacy record for %s: %s", conversation_id, exc)
            raise ConversationStorageCorruptError(
                f"Conversation '{conversation_id}' legacy record is corrupt: {exc}"
            ) from exc

    def _load_summary(self, conversation_id: str) -> ConversationSummary | None:
        if self._manifest_path_for(conversation_id).exists():
            manifest = self._read_manifest(conversation_id)
            if self._manifest_is_deleted(manifest):
                return None
            record = self._record_cache.get(conversation_id)
            if (record is not None and record.revision < self._manifest_revision(manifest)
                    and self._record_cache_stamps[conversation_id][0] == self._manifest_cache[conversation_id][0]
                    and self._record_cache_stamps.get(conversation_id) == self._record_disk_stamp(conversation_id)):
                # History already recovered a readable committed generation.
                # Keep cold inventory and direct summary reads on that version;
                # healthy metadata reads still never scan the history files.
                return record.to_summary()
            metadata = manifest.get("metadata")
            if metadata is None:
                metadata = self._read_generation_metadata(conversation_id, manifest["current_generation"])
            return ConversationSummary.from_dict(metadata)
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                payload = json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
                return ConversationSummary.from_dict(payload)
            except Exception as e:
                logger.error("Failed to load summary for %s: %s", conversation_id, e)
                raise ConversationStorageCorruptError(
                    f"Conversation summary for '{conversation_id}' is corrupt: {e}"
                ) from e

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
        if not history_exists(path):
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
                message = json.loads(stripped)
                transcript.append(message)
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
        if not history_exists(path):
            if strict:
                raise FileNotFoundError(path)
            return {}
        try:
            content = self._safe_read_text(path, encoding="utf-8")
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("snapshot must be an object")
            return dict(payload)
        except Exception:
            if strict:
                raise
            logger.error("Failed to read snapshot %s", path, exc_info=True)
            return {}

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
                logical = self._generation_paths(conversation_id, generation)[1]
                return physical_history_path(logical) if history_exists(logical) else logical
        logical = self._transcript_path_for(conversation_id)
        return physical_history_path(logical) if history_exists(logical) else logical

    def _transcript_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.transcript.jsonl"

    def _snapshot_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.snapshot.json"

    def _legacy_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.json"

    def _partial_projection_path(self, conversation_id: str, generation: int) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.g{generation}.projection.jsonl"

    def _cache_partial_projection(
        self, conversation_id: str, pointer: dict[str, int], projection: dict[str, Any],
    ) -> None:
        stat = history_stat(self._partial_projection_path(conversation_id, pointer["generation"]))
        with self._process_lock:
            self._partial_projection_cache[conversation_id] = (
                pointer["generation"], pointer["bytes"], (stat.st_mtime_ns, stat.st_size), projection,
            )
            self._partial_projection_cache.move_to_end(conversation_id)
            while len(self._partial_projection_cache) > self._MAX_RECORD_CACHE:
                self._partial_projection_cache.popitem(last=False)

    def _load_partial_projection(
        self, conversation_id: str, manifest: dict[str, Any], *, previous: bool = False,
    ) -> dict[str, Any]:
        pointer = manifest.get("previous_projection_log" if previous else "projection_log")
        if pointer is None:
            return manifest.get("previous_projection" if previous else "partial_projection") or {}
        path = self._partial_projection_path(conversation_id, pointer["generation"])
        try:
            stat = history_stat(path)
            cached = self._partial_projection_cache.get(conversation_id)
            if cached is not None and cached[:3] == (
                pointer["generation"], pointer["bytes"], (stat.st_mtime_ns, stat.st_size),
            ):
                return cached[3]
            projection = read_projection(path, committed_bytes=pointer["bytes"], revision=pointer["revision"])
            self._cache_partial_projection(conversation_id, pointer, projection)
            return projection
        except (OSError, UnicodeDecodeError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
            raise ConversationStorageCorruptError(
                f"Conversation projection for '{conversation_id}' is corrupt: {exc}"
            ) from exc

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
        disk_stamps = self._discover_summary_stamps()
        discovered_ids = set(disk_stamps)
        for conversation_id in set(self._summary_index) - discovered_ids:
            self._summary_index.pop(conversation_id, None)
            self._summary_index_stamps.pop(conversation_id, None)
        for conversation_id in discovered_ids:
            stamp = disk_stamps[conversation_id]
            if len(self._summary_index_stamps.get(conversation_id, ())) == 2:
                # Older manifests keep summary metadata in a generation file.
                stamp = self._summary_disk_stamp(conversation_id)
            if (
                conversation_id in self._summary_index
                and self._summary_index_stamps.get(conversation_id) == stamp
            ):
                continue
            try:
                summary = self._load_summary(conversation_id)
            except ConversationStorageCorruptError as exc:
                # Listing is an inventory operation: one unreadable record
                # must not hide healthy conversations. Direct get/read paths
                # still propagate the same corruption error to the caller.
                logger.warning(
                    "Skipping corrupt conversation %s while building summary index: %s",
                    conversation_id,
                    exc,
                )
                summary = None
            if summary is not None:
                manifest = self._manifest_cache.get(conversation_id)
                if manifest is not None and manifest[1].get("metadata") is None:
                    stamp = self._summary_disk_stamp(conversation_id)
                self._summary_index[summary.id] = summary
                self._summary_index_stamps[conversation_id] = stamp
            else:
                self._summary_index.pop(conversation_id, None)
                self._summary_index_stamps.pop(conversation_id, None)

    def _discover_summary_stamps(self) -> dict[str, tuple[tuple[int, int], ...]]:
        """Use one directory inventory for discovery and summary freshness."""
        files: dict[str, dict[str, tuple[int, int]]] = {}
        with os.scandir(self._base_dir) as entries:
            for entry in entries:
                name = entry.name
                if not name.endswith(".json") or name.endswith(".snapshot.json") or re.search(r"\.g\d+\.", name):
                    continue
                suffix = next((suffix for suffix in (".manifest.json", ".meta.json") if name.endswith(suffix)), ".json")
                conversation_id = name[:-len(suffix)]
                if _CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
                    stat = entry.stat()
                    files.setdefault(conversation_id, {})[suffix] = (stat.st_mtime_ns, stat.st_size)
        stamps = {}
        for conversation_id, versions in files.items():
            if ".manifest.json" in versions:
                stamps[conversation_id] = (versions[".manifest.json"],)
            else:
                stamps[conversation_id] = ((-1, -1), versions.get(".meta.json", (-1, -1)), versions.get(".json", (-1, -1)))
        return stamps


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
            # Raw provider reasoning is a live-only surface. Older MiniCode
            # transcripts persisted it in the public block list, so remove it
            # on load while retaining durable reasoning summaries.
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
    projected = project_public_transcript(normalized)
    for index, message in enumerate(projected):
        if not message["id"]:
            message["id"] = f"legacy-message-{index}"
    return projected


def _is_legacy_raw_provider_reasoning_block(block: Any) -> bool:
    """Hide raw provider reasoning persisted by pre-alignment builds."""

    if not isinstance(block, dict) or str(block.get("type") or "") != "thinking":
        return False
    if bool(block.get("is_raw_provider_reasoning") or block.get("isRawProviderReasoning")):
        return True
    visibility = str(block.get("visibility") or "").strip().lower()
    if visibility in {"hidden", "internal", "redacted", "debug"}:
        return True
    # The camelCase variant only exists to catch raw reasoning persisted by
    # pre-alignment builds, whose block casing predates this repo's history;
    # drop it once the oldest supported persisted format is snake_case-only.
    reasoning_type = str(
        block.get("provider_reasoning_type")
        or block.get("providerReasoningType")
        or ""
    ).strip().lower()
    if reasoning_type == "reasoning_summary_text":
        return False
    if reasoning_type in {
        "reasoning_text",
        "reasoning_content",
        "raw_reasoning",
        "raw_provider_reasoning",
        "thinking",
        "thinking_delta",
    }:
        return True
    source = str(block.get("source") or "").strip().lower()
    return source in {"provider", "reasoning"}


def _derive_title(content: str) -> str:
    cleaned = " ".join(content.strip().split())
    if not cleaned:
        return "New chat"
    return cleaned[:48]
