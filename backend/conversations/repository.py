from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

from backend.config import PROJECT_ROOT

from .models import ConversationRecord, ConversationSummary, utc_now_iso

CONVERSATION_DATA_DIR = PROJECT_ROOT / "data" / "conversations"
_CONVERSATION_ID_PATTERN = re.compile(r"^(?:conv|side|local)_[A-Za-z0-9_-]{6,80}$|^side-[A-Za-z0-9_-]{6,80}$")


class ConversationRepository:
    _MAX_RECORD_CACHE = 64

    def __init__(self, base_dir: Path | None = None) -> None:
        self._base_dir = Path(base_dir or CONVERSATION_DATA_DIR)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._summary_index: dict[str, ConversationSummary] | None = None
        self._record_cache: dict[str, ConversationRecord] = {}
        self._record_cache_order: list[str] = []

    def create_conversation(
        self,
        *,
        conversation_id: str | None = None,
        title: str | None = None,
        memory_mode: str = "none",
        permission_mode: str = "default",
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
        record = self.get_conversation(conversation_id)
        if record is None:
            return None
        next_message = dict(message)
        record.transcript.append(next_message)
        record.message_count = len(record.transcript)
        if record.title == "New chat" and next_message.get("role") == "user":
            record.title = _derive_title(str(next_message.get("content", "")))
        record.updated_at = utc_now_iso()
        if self._requires_transcript_rewrite(conversation_id):
            self._write_transcript(conversation_id, record.transcript)
        else:
            self._append_transcript_message(conversation_id, next_message)
        if self._requires_snapshot_rewrite(conversation_id):
            self._write_snapshot(conversation_id, record.context_snapshot)
        self._write_meta(record)
        self._delete_legacy_file(conversation_id)
        self._cache_record(record)
        return record

    def replace_transcript(
        self, conversation_id: str, transcript: list[dict[str, Any]]
    ) -> ConversationRecord | None:
        record = self.get_conversation(conversation_id)
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
        record.permission_mode = permission_mode
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
        record = self.get_conversation(conversation_id)
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

    def _safe_write_text(self, path: Path, text: str, encoding: str = "utf-8") -> None:
        import time
        for attempt in range(5):
            try:
                path.write_text(text, encoding=encoding)
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

    def _load_record(self, conversation_id: str) -> ConversationRecord | None:
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                meta_payload = json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
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
            payload = json.loads(self._safe_read_text(legacy_path, encoding="utf-8"))
            return ConversationRecord.from_dict(payload)
        except Exception as e:
            logger.error("Failed to load legacy record for %s: %s", conversation_id, e)
            return None

    def _load_summary(self, conversation_id: str) -> ConversationSummary | None:
        meta_path = self._meta_path_for(conversation_id)
        if meta_path.exists():
            try:
                payload = json.loads(self._safe_read_text(meta_path, encoding="utf-8"))
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
                    transcript.append(json.loads(stripped))
        except Exception as e:
            logger.error("Failed to read transcript for %s: %s", conversation_id, e)
        return transcript

    def _read_snapshot(self, conversation_id: str) -> dict[str, Any]:
        path = self._snapshot_path_for(conversation_id)
        if not path.exists():
            return {}
        try:
            content = self._safe_read_text(path, encoding="utf-8")
            return dict(json.loads(content))
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


def _derive_title(content: str) -> str:
    cleaned = " ".join(content.strip().split())
    if not cleaned:
        return "New chat"
    return cleaned[:48]
