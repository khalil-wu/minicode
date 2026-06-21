from __future__ import annotations

from contextlib import contextmanager
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT
from backend.encoding_repair import repair_mojibake_payload

from .models import (
    DEFAULT_CONVERSATION_PERMISSION_MODE,
    ConversationRecord,
    ConversationSummary,
    normalize_permission_mode,
    utc_now_iso,
)

logger = logging.getLogger(__name__)

CONVERSATION_DATA_DIR = PROJECT_ROOT / "data" / "conversations"
_CONVERSATION_ID_PATTERN = re.compile(
    r"^(?:conv|side|local)_[A-Za-z0-9_-]{6,80}$|^(?:conv|side)-[A-Za-z0-9_-]{6,80}$"
)
_FAILED_TOOL_STATUSES = {"error", "failed", "blocked"}
_TOOL_DETAIL_FIELDS = (
    "summary",
    "displaySummary",
    "display_summary",
    "contentPreview",
    "content_preview",
    "outputPreview",
    "output_preview",
    "inputSummary",
    "input_summary",
)


class ConversationRepository:
    _MAX_RECORD_CACHE = 64

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or CONVERSATION_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._process_lock = threading.RLock()
        self._store_lock_path = self._base_dir / ".conversation-store.lock"
        self._summary_index: dict[str, ConversationSummary] | None = None
        self._record_cache: dict[str, ConversationRecord] = {}
        self._record_cache_order: list[str] = []

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str | None = None,
        memory_mode: str = "none",
        permission_mode: str = DEFAULT_CONVERSATION_PERMISSION_MODE,
        permission_deny_rules: list[str] | None = None,
        permission_overrides: dict[str, str] | None = None,
        summary: str = "",
        inherited_facts: list[dict[str, Any]] | None = None,
        local_facts: list[dict[str, Any]] | None = None,
        transcript: list[dict[str, Any]] | None = None,
        context_snapshot: dict[str, Any] | None = None,
        workspace_root: str = "",
        git_branch: str = "",
        worktree_path: str = "",
        git_isolated: bool = False,
    ) -> ConversationRecord:
        requested_id = str(conversation_id or "").strip()
        if requested_id and _CONVERSATION_ID_PATTERN.fullmatch(requested_id):
            conversation_id = requested_id
        else:
            conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        if self.get_conversation(conversation_id) is not None:
            conversation_id = f"conv_{uuid.uuid4().hex[:12]}"
        initial_transcript = list(transcript or [])
        record = ConversationRecord(
            id=conversation_id,
            title=(title or "New chat").strip() or "New chat",
            memory_mode=memory_mode,
            permission_mode=permission_mode,
            permission_deny_rules=list(permission_deny_rules or []),
            permission_overrides=dict(permission_overrides or {}),
            summary=summary,
            inherited_facts=list(inherited_facts or []),
            local_facts=list(local_facts or []),
            message_count=len(initial_transcript),
            transcript=initial_transcript,
            context_snapshot=dict(context_snapshot or {}),
            workspace_root=workspace_root,
            git_branch=git_branch,
            worktree_path=worktree_path,
            git_isolated=git_isolated,
        )
        self.save_conversation(record)
        return record

    def get_conversation(self, conversation_id: str) -> ConversationRecord | None:
        cached = self._record_cache.get(conversation_id)
        if cached is not None:
            return cached
        # 优化：不直接使用 summary_index 作排他检查，优先尝试从磁盘读取 record 以防同步延迟导致会话加载为 None
        record = self._load_record(conversation_id)
        if record is not None:
            self._cache_record(record)
        return record

    def save_conversation(self, record: ConversationRecord) -> ConversationRecord:
        with self._store_lock():
            record.updated_at = utc_now_iso()
            record.message_count = len(record.transcript)
            self._write_meta(record)
            self._write_transcript(record.id, record.transcript)
            self._write_snapshot(record.id, record.context_snapshot)
            self._delete_legacy_file(record.id)
            self._cache_record(record)
            return record

    def list_conversations(self) -> list[ConversationSummary]:
        self._ensure_summary_index_loaded()
        conversations = list((self._summary_index or {}).values())
        conversations.sort(key=lambda item: item.updated_at, reverse=True)
        return conversations

    def delete_conversation(self, conversation_id: str) -> bool:
        with self._store_lock():
            removed = False
            for path in (
                self._meta_path_for(conversation_id),
                self._transcript_path_for(conversation_id),
                self._snapshot_path_for(conversation_id),
                self._legacy_path_for(conversation_id),
            ):
                if path.exists():
                    path.unlink()
                    removed = True
            if not removed:
                return False
            self._record_cache.pop(conversation_id, None)
            if self._summary_index is not None:
                self._summary_index.pop(conversation_id, None)
            return True

    def append_transcript_message(
        self, conversation_id: str, message: dict[str, Any]
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._record_cache.get(conversation_id) or self._load_record(conversation_id)
            if record is None:
                return None
            next_message = dict(message)
            record.transcript.append(next_message)
            record.message_count = len(record.transcript)
            if record.title == "New chat" and next_message.get("role") == "user":
                record.title = _derive_title(str(next_message.get("content", "")))
            record.updated_at = utc_now_iso()
            self._write_transcript(conversation_id, record.transcript)
            if self._requires_snapshot_rewrite(conversation_id):
                self._write_snapshot(conversation_id, record.context_snapshot)
            self._write_meta(record)
            self._delete_legacy_file(conversation_id)
            self._cache_record(record)
            return record

    def replace_transcript(
        self, conversation_id: str, transcript: list[dict[str, Any]]
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._record_cache.get(conversation_id) or self._load_record(conversation_id)
            if record is None:
                return None
            record.transcript = list(transcript)
            record.message_count = len(record.transcript)
            record.updated_at = utc_now_iso()
            self._write_transcript(conversation_id, record.transcript)
            if self._requires_snapshot_rewrite(conversation_id):
                self._write_snapshot(conversation_id, record.context_snapshot)
            self._write_meta(record)
            self._delete_legacy_file(conversation_id)
            self._cache_record(record)
            return record

    def update_summary(
        self, conversation_id: str, summary: str
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        record.summary = summary
        return self._persist_meta_only(record)

    def update_facts(
        self,
        conversation_id: str,
        *,
        inherited_facts: list[dict[str, Any]] | None = None,
        local_facts: list[dict[str, Any]] | None = None,
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        if inherited_facts is not None:
            record.inherited_facts = list(inherited_facts)
        if local_facts is not None:
            record.local_facts = list(local_facts)
        return self._persist_meta_only(record)

    def update_memory_mode(
        self, conversation_id: str, memory_mode: str
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        record.memory_mode = memory_mode
        return self._persist_meta_only(record)

    def update_permission_mode(
        self, conversation_id: str, permission_mode: str
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        next_mode = normalize_permission_mode(permission_mode)
        if next_mode == "plan" and record.permission_mode != "plan":
            record.permission_previous_mode = record.permission_mode
        elif next_mode != "plan":
            record.permission_previous_mode = ""
        record.permission_mode = next_mode
        return self._persist_meta_only(record)

    def update_permission_rules(
        self,
        conversation_id: str,
        *,
        deny_rules: list[str] | None = None,
        overrides: dict[str, str] | None = None,
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        if deny_rules is not None:
            record.permission_deny_rules = list(deny_rules)
        if overrides is not None:
            record.permission_overrides = dict(overrides)
        return self._persist_meta_only(record)

    def update_workspace_binding(
        self,
        conversation_id: str,
        *,
        workspace_root: str = "",
        git_branch: str = "",
        worktree_path: str = "",
        git_isolated: bool | None = None,
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        record.workspace_root = workspace_root
        record.git_branch = git_branch
        record.worktree_path = worktree_path
        if git_isolated is not None:
            record.git_isolated = bool(git_isolated)
        return self._persist_meta_only(record)

    def update_goal(
        self,
        conversation_id: str,
        goal: dict[str, Any],
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        record.goal = dict(goal)
        return self._persist_meta_only(record)

    def rename_conversation(
        self, conversation_id: str, title: str
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        cleaned = title.strip() or "New chat"
        record.title = cleaned[:120]
        return self._persist_meta_only(record)

    def set_archived(
        self, conversation_id: str, archived: bool
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        record.archived = bool(archived)
        record.archived_at = utc_now_iso() if archived else ""
        return self._persist_meta_only(record)

    def update_compaction(
        self,
        conversation_id: str,
        state: str,
        summary: str = "",
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        record.compaction_state = state
        record.compaction_summary = summary
        return self._persist_meta_only(record)

    def save_context_snapshot(
        self, conversation_id: str, context_snapshot: dict[str, Any]
    ) -> ConversationRecord | None:
        with self._store_lock():
            record = self._record_cache.get(conversation_id) or self._load_record(conversation_id)
            if record is None:
                return None
            record.context_snapshot = dict(context_snapshot)
            record.updated_at = utc_now_iso()
            if self._requires_transcript_rewrite(conversation_id):
                self._write_transcript(conversation_id, record.transcript)
            self._write_snapshot(conversation_id, record.context_snapshot)
            self._write_meta(record)
            self._delete_legacy_file(conversation_id)
            self._cache_record(record)
            return record

    def _persist_meta_only(self, record: ConversationRecord) -> ConversationRecord:
        with self._store_lock():
            record.updated_at = utc_now_iso()
            record.message_count = len(record.transcript)
            if self._requires_transcript_rewrite(record.id):
                self._write_transcript(record.id, record.transcript)
            if self._requires_snapshot_rewrite(record.id):
                self._write_snapshot(record.id, record.context_snapshot)
            self._write_meta(record)
            self._delete_legacy_file(record.id)
            self._cache_record(record)
            return record

    def _cache_record(self, record: ConversationRecord) -> None:
        self._record_cache[record.id] = record
        if record.id in self._record_cache_order:
            self._record_cache_order.remove(record.id)
        self._record_cache_order.append(record.id)
        while len(self._record_cache_order) > self._MAX_RECORD_CACHE:
            evict_id = self._record_cache_order.pop(0)
            self._record_cache.pop(evict_id, None)
        if self._summary_index is not None:
            self._summary_index[record.id] = record.to_summary()

    def _requires_transcript_rewrite(self, conversation_id: str) -> bool:
        return self._legacy_path_for(conversation_id).exists() or not self._transcript_path_for(conversation_id).exists()

    def _requires_snapshot_rewrite(self, conversation_id: str) -> bool:
        return self._legacy_path_for(conversation_id).exists() or not self._snapshot_path_for(conversation_id).exists()

    @contextmanager
    def _store_lock(self):
        with self._process_lock:
            fd: int | None = None
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    fd = os.open(self._store_lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.write(fd, f"{os.getpid()} {time.time():.6f}\n".encode("ascii", "ignore"))
                    break
                except FileExistsError:
                    try:
                        lock_age = time.time() - self._store_lock_path.stat().st_mtime
                    except OSError:
                        lock_age = 0.0
                    if lock_age > 30.0:
                        try:
                            self._store_lock_path.unlink()
                            continue
                        except OSError:
                            pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Timed out waiting for conversation store lock: {self._store_lock_path}")
                    time.sleep(0.05)

            try:
                yield
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                try:
                    self._store_lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _safe_write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        for attempt in range(5):
            tmp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                tmp_path.write_text(text, encoding=encoding)
                os.replace(tmp_path, path)
                return
            except (IOError, PermissionError) as e:
                try:
                    tmp_path.unlink()
                except FileNotFoundError:
                    pass
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

    def _load_record(self, conversation_id: str) -> ConversationRecord | None:
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                meta_payload = repair_mojibake_payload(
                    json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
                )
            except Exception as e:
                logger.error("Failed to read meta for %s: %s", conversation_id, e)
                return None

            transcript = self._read_transcript(conversation_id)
            snapshot = self._read_snapshot(conversation_id)

            # ── 自动容灾恢复逻辑 ──
            if not transcript and snapshot and "history" in snapshot and snapshot["history"]:
                logger.warning(
                    "Transcript for %s is empty but snapshot contains history. Rebuilding from snapshot.",
                    conversation_id
                )
                reconstructed = []
                for idx, msg in enumerate(snapshot["history"]):
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    rec_msg = {
                        "id": f"restored_{role}_{idx}_{uuid.uuid4().hex[:6]}",
                        "role": role,
                        "content": content,
                        "timestamp": meta_payload.get("updated_at") or utc_now_iso(),
                    }
                    if msg.get("tool_calls"):
                        rec_msg["tool_calls"] = msg["tool_calls"]
                    if msg.get("name"):
                        rec_msg["name"] = msg["name"]
                    if msg.get("tool_call_id"):
                        rec_msg["tool_call_id"] = msg["tool_call_id"]
                    reconstructed.append(rec_msg)

                transcript = reconstructed
                try:
                    self._write_transcript(conversation_id, transcript)
                except Exception as e:
                    logger.error("Failed to auto-heal transcript file for %s: %s", conversation_id, e)

            transcript = _normalize_loaded_transcript(transcript)
            return ConversationRecord.from_dict(
                {
                    **meta_payload,
                    "transcript": transcript,
                    "context_snapshot": snapshot,
                }
            )

        legacy_path = self._legacy_path_for(conversation_id)
        if not legacy_path.exists():
            return None
        try:
            payload = repair_mojibake_payload(json.loads(self._safe_read_text(legacy_path, encoding="utf-8")))
            payload["transcript"] = _normalize_loaded_transcript(list(payload.get("transcript") or []))
            return ConversationRecord.from_dict(payload)
        except Exception as e:
            logger.error("Failed to load legacy record for %s: %s", conversation_id, e)
            return None

    def _load_summary(self, conversation_id: str) -> ConversationSummary | None:
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
        path = self._transcript_path_for(conversation_id)
        if not path.exists():
            return []
        transcript: list[dict[str, Any]] = []
        try:
            content = self._safe_read_text(path, encoding="utf-8")
            for line in content.splitlines():
                stripped = line.strip()
                if stripped:
                    transcript.append(repair_mojibake_payload(json.loads(stripped)))
        except Exception as e:
            logger.error("Failed to read transcript for %s: %s", conversation_id, e)
        return transcript

    def _read_snapshot(self, conversation_id: str) -> dict[str, Any]:
        path = self._snapshot_path_for(conversation_id)
        if not path.exists():
            return {}
        try:
            content = self._safe_read_text(path, encoding="utf-8")
            return dict(repair_mojibake_payload(json.loads(content)))
        except Exception as e:
            logger.error("Failed to read snapshot for %s: %s", conversation_id, e)
            return {}

    def _write_meta(self, record: ConversationRecord) -> None:
        self._safe_write_text(
            self._meta_path_for(record.id),
            json.dumps(record.to_meta_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_transcript(
        self, conversation_id: str, transcript: list[dict[str, Any]]
    ) -> None:
        path = self._transcript_path_for(conversation_id)
        if not transcript:
            self._safe_write_text(path, "", encoding="utf-8")
            return
        lines = [json.dumps(item, ensure_ascii=False) for item in transcript]
        self._safe_write_text(path, "\n".join(lines) + "\n", encoding="utf-8")

    def _append_transcript_message(
        self, conversation_id: str, message: dict[str, Any]
    ) -> None:
        import time
        path = self._transcript_path_for(conversation_id)
        for attempt in range(5):
            try:
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(message, ensure_ascii=False))
                    handle.write("\n")
                return
            except (IOError, PermissionError) as e:
                if attempt == 4:
                    logger.error("Failed to append message to %s after 5 attempts: %s", path, e)
                    raise e
                time.sleep(0.05 * (attempt + 1))

    def _write_snapshot(self, conversation_id: str, context_snapshot: dict[str, Any]) -> None:
        self._safe_write_text(
            self._snapshot_path_for(conversation_id),
            json.dumps(context_snapshot or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _delete_legacy_file(self, conversation_id: str) -> None:
        legacy_path = self._legacy_path_for(conversation_id)
        if legacy_path.exists():
            try:
                legacy_path.unlink()
            except Exception as e:
                logger.warning("Failed to delete legacy file %s: %s", legacy_path, e)

    def _safe_id(self, conversation_id: str) -> str:
        """Validate conversation_id to prevent path traversal."""
        cid = str(conversation_id or "").strip()
        if not cid or ".." in cid or "/" in cid or "\\" in cid or "\x00" in cid:
            raise ValueError(f"Invalid conversation ID: {cid!r}")
        return cid

    def _meta_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.meta.json"

    def _transcript_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.transcript.jsonl"

    def _snapshot_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.snapshot.json"

    def _legacy_path_for(self, conversation_id: str) -> Path:
        return self._base_dir / f"{self._safe_id(conversation_id)}.json"

    def _ensure_summary_index_loaded(self) -> None:
        if self._summary_index is not None:
            return

        self._summary_index = {}
        for conversation_id in self._discover_conversation_ids():
            summary = self._load_summary(conversation_id)
            if summary is not None:
                self._summary_index[summary.id] = summary

    def _discover_conversation_ids(self) -> list[str]:
        conversation_ids = {
            path.name[: -len(".meta.json")]
            for path in self._base_dir.glob("*.meta.json")
        }
        for path in self._base_dir.glob("*.json"):
            if path.name.endswith(".meta.json") or path.name.endswith(".snapshot.json"):
                continue
            conversation_ids.add(path.stem)
        return sorted(conversation_ids)


def _normalize_loaded_transcript(
    transcript: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(transcript):
        if not isinstance(message, dict):
            normalized.append(message)
            continue
        if not _is_legacy_tool_only_assistant_needing_text(transcript, index):
            normalized.append(message)
            continue

        records = _message_tool_records(message)
        fallback = _format_legacy_tool_activity_without_final_reply(
            records,
            user_message=_previous_user_content(transcript, index),
        )
        if not fallback:
            normalized.append(message)
            continue

        next_message = dict(message)
        next_message["content"] = fallback
        next_message["blocks"] = _blocks_with_text_fallback(message, records, fallback)
        normalized.append(next_message)
    return normalized


def _is_legacy_tool_only_assistant_needing_text(
    transcript: list[dict[str, Any]],
    index: int,
) -> bool:
    message = transcript[index]
    if str(message.get("role") or "") != "assistant":
        return False
    if _message_has_visible_text(message):
        return False
    if not _message_tool_records(message):
        return False
    return not _later_visible_assistant_before_next_user(transcript, index)


def _later_visible_assistant_before_next_user(
    transcript: list[dict[str, Any]],
    index: int,
) -> bool:
    for candidate in transcript[index + 1:]:
        if not isinstance(candidate, dict):
            continue
        role = str(candidate.get("role") or "")
        if role == "user":
            return False
        if role == "assistant" and _message_has_visible_text(candidate):
            return True
    return False


def _message_has_visible_text(message: dict[str, Any]) -> bool:
    if str(message.get("content") or "").strip():
        return True
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        return False
    return any(
        isinstance(block, dict)
        and str(block.get("type") or "") == "text"
        and str(block.get("content") or "").strip()
        for block in blocks
    )


def _message_tool_records(message: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_record(value: Any) -> None:
        if not isinstance(value, dict):
            return
        record_id = str(value.get("id") or "").strip()
        name = str(value.get("name") or "").strip()
        if not record_id or not name:
            return
        dedupe_key = record_id or f"{name}:{len(records)}"
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        records.append(value)

    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for record in tool_calls:
            add_record(record)

    blocks = message.get("blocks")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or str(block.get("type") or "") != "tool_call":
                continue
            add_record(block.get("record") or block)

    return records


def _blocks_with_text_fallback(
    message: dict[str, Any],
    records: list[dict[str, Any]],
    fallback: str,
) -> list[dict[str, Any]]:
    raw_blocks = message.get("blocks")
    blocks = [
        dict(block)
        for block in raw_blocks
        if isinstance(block, dict)
    ] if isinstance(raw_blocks, list) else []
    if not blocks:
        blocks = [{"type": "tool_call", "record": dict(record)} for record in records]
    blocks.append({"type": "text", "content": fallback})
    return blocks


def _previous_user_content(transcript: list[dict[str, Any]], index: int) -> str:
    for candidate in reversed(transcript[:index]):
        if isinstance(candidate, dict) and str(candidate.get("role") or "") == "user":
            return str(candidate.get("content") or "")
    return ""


def _contains_cjk(text: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" or "\uf900" <= char <= "\ufaff" for char in text)


def _tool_record_detail(record: dict[str, Any], *, fallback: str) -> str:
    for field in _TOOL_DETAIL_FIELDS:
        detail = str(record.get(field) or "").strip()
        if detail:
            return detail[:700].rstrip() + ("..." if len(detail) > 700 else "")
    return fallback


def _format_legacy_tool_activity_without_final_reply(
    records: list[dict[str, Any]],
    *,
    user_message: str,
) -> str:
    if not records:
        return ""
    failed = [
        record
        for record in records
        if str(record.get("status") or "").strip().lower() in _FAILED_TOOL_STATUSES
    ]
    records_to_show = failed or records
    if _contains_cjk(user_message):
        if failed:
            intro = (
                "\u5de5\u5177\u8c03\u7528\u5931\u8d25\uff0c\u800c\u4e14\u6a21\u578b"
                "\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u56de\u590d\u3002\u8fd9\u8f6e"
                "\u4e0d\u80fd\u5f53\u4f5c\u6210\u529f\u5b8c\u6210\uff0c\u5931\u8d25"
                "\u70b9\u5982\u4e0b\uff1a"
            )
            no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u5931\u8d25\u7ec6\u8282\u3002"
        else:
            intro = (
                "\u5de5\u5177\u5df2\u7ecf\u8fd4\u56de\u7ed3\u679c\uff0c\u4f46\u6a21"
                "\u578b\u6ca1\u6709\u751f\u6210\u6700\u7ec8\u56de\u590d\u3002\u8fd9"
                "\u8f6e\u4e0d\u80fd\u5f53\u4f5c\u6210\u529f\u5b8c\u6210\uff1b\u5df2"
                "\u4fdd\u7559\u7684\u5de5\u5177\u7ed3\u679c\u5982\u4e0b\uff1a"
            )
            no_details = "\u5de5\u5177\u672a\u8fd4\u56de\u53ef\u7528\u7684\u6458\u8981\u3002"
    elif failed:
        intro = (
            "Tool calls failed and the model did not produce a final reply. "
            "This turn cannot be treated as completed; here is what failed:"
        )
        no_details = "The tool did not return usable failure details."
    else:
        intro = (
            "Tool calls completed, but the model did not produce a final reply. "
            "This turn cannot be treated as completed; the preserved tool results are:"
        )
        no_details = "The tool did not return a usable summary."

    parts = [intro]
    for item_index, record in enumerate(records_to_show[-3:], start=1):
        name = str(record.get("name") or "tool")
        status = str(record.get("status") or ("failed" if failed else "completed"))
        detail = _tool_record_detail(record, fallback=no_details)
        parts.append(f"{item_index}. {name} [{status}]\n{detail}")
    return "\n\n".join(parts)


def _derive_title(content: str) -> str:
    cleaned = " ".join(content.strip().split())
    if not cleaned:
        return "New chat"
    return cleaned[:48]
