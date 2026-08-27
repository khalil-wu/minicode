from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


ConversationMemoryMode = Literal["enabled", "disabled", "polluted"]
ConversationCompactionState = Literal["clean", "compacted", "retrieved"]
ConversationType = Literal["main", "side_chat"]
ConversationPermissionMode = Literal[
    "plan", "confirm", "bypass", "auto"
]
ConversationPermissionRuleLevel = Literal["auto", "confirm", "diff", "deny"]
DEFAULT_CONVERSATION_PERMISSION_MODE: ConversationPermissionMode = "confirm"
DEFAULT_CONVERSATION_TYPE: ConversationType = "main"


def normalize_conversation_type(value: Any) -> ConversationType:
    conversation_type = str(value or "").strip().lower().replace("-", "_")
    if conversation_type == "side_chat":
        return "side_chat"
    return DEFAULT_CONVERSATION_TYPE


def normalize_memory_mode(
    value: Any,
    *,
    conversation_type: ConversationType | str = DEFAULT_CONVERSATION_TYPE,
    polluted: bool = False,
) -> ConversationMemoryMode:
    """Normalize the single MiniCode memory generation mode."""

    if polluted:
        return "polluted"
    mode = str(value or "").strip().lower()
    if mode in {"enabled", "disabled", "polluted"}:
        return mode  # type: ignore[return-value]
    if normalize_conversation_type(conversation_type) == "side_chat":
        return "disabled"
    return "enabled"


def normalize_permission_mode(value: Any) -> ConversationPermissionMode:
    mode = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if mode in {"plan", "confirm", "bypass", "auto"}:
        return mode
    raise ValueError(f"Unsupported permission mode: {value!r}")


def _normalize_previous_permission_mode(value: Any) -> ConversationPermissionMode | str:
    if not str(value or "").strip():
        return ""
    mode = normalize_permission_mode(value)
    return "" if mode == "plan" else mode


def normalize_permission_rule_level(value: Any) -> ConversationPermissionRuleLevel | None:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    aliases = {
        "diff_review": "diff",
        "diffreview": "diff",
        "always_deny": "deny",
        "block": "deny",
    }
    level = aliases.get(raw, raw)
    if level in {"auto", "confirm", "diff", "deny"}:
        return level
    return None


def normalize_permission_deny_rules(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        pattern = str(item or "").strip()
        if not pattern or pattern in seen:
            continue
        seen.add(pattern)
        normalized.append(pattern)
    return normalized


def normalize_permission_overrides(value: Any) -> dict[str, ConversationPermissionRuleLevel]:
    if not isinstance(value, dict):
        return {}

    normalized: dict[str, ConversationPermissionRuleLevel] = {}
    for raw_pattern, raw_level in value.items():
        pattern = str(raw_pattern or "").strip()
        if not pattern:
            continue
        level = normalize_permission_rule_level(raw_level)
        if level is None:
            continue
        normalized[pattern] = level
    return normalized


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
    return normalized


def _normalize_revision(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        revision = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, revision)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ConversationSummary:
    id: str
    title: str
    created_at: str
    updated_at: str
    revision: int = 0
    conversation_type: ConversationType = DEFAULT_CONVERSATION_TYPE
    memory_mode: ConversationMemoryMode = "enabled"
    memory_polluted: bool = False
    memory_pollution_sources: list[str] = field(default_factory=list)
    permission_mode: ConversationPermissionMode = DEFAULT_CONVERSATION_PERMISSION_MODE
    summary: str = ""
    compaction_state: ConversationCompactionState = "clean"
    message_count: int = 0
    archived: bool = False
    archived_at: str = ""
    workspace_root: str = ""
    git_branch: str = ""
    worktree_path: str = ""
    git_isolated: bool = False
    goal: dict[str, Any] = field(default_factory=dict)
    parent_conversation_id: str = ""
    parent_message_index: int | None = None
    fork_id: str = ""
    branch_kind: str = ""
    merged_into_conversation_id: str = ""
    merged_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConversationSummary":
        conversation_type = normalize_conversation_type(payload.get("conversation_type"))
        memory_polluted = bool(
            payload.get("memory_polluted")
            or payload.get("memory_pollution_sources")
            or payload.get("memory_generation_mode") == "polluted"
        )
        return cls(
            id=str(payload["id"]),
            title=str(payload.get("title") or "New chat"),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or utc_now_iso()),
            revision=_normalize_revision(payload.get("revision")),
            conversation_type=conversation_type,
            memory_mode=normalize_memory_mode(
                payload.get("memory_generation_mode", payload.get("memory_mode")),
                conversation_type=conversation_type,
                polluted=memory_polluted,
            ),
            memory_polluted=memory_polluted,
            memory_pollution_sources=_normalize_string_list(
                payload.get("memory_pollution_sources")
            ),
            permission_mode=normalize_permission_mode(
                payload.get("permission_mode", DEFAULT_CONVERSATION_PERMISSION_MODE)
            ),
            summary=str(payload.get("summary") or ""),
            compaction_state=payload.get("compaction_state", "clean"),
            message_count=int(payload.get("message_count") or 0),
            archived=bool(payload.get("archived") or False),
            archived_at=str(payload.get("archived_at") or ""),
            workspace_root=str(payload.get("workspace_root") or ""),
            git_branch=str(payload.get("git_branch") or ""),
            worktree_path=str(payload.get("worktree_path") or ""),
            git_isolated=bool(payload.get("git_isolated") or False),
            goal=dict(payload.get("goal") or {}),
            parent_conversation_id=str(payload.get("parent_conversation_id") or ""),
            parent_message_index=(
                int(payload["parent_message_index"])
                if payload.get("parent_message_index") is not None
                else None
            ),
            fork_id=str(payload.get("fork_id") or ""),
            branch_kind=str(payload.get("branch_kind") or ""),
            merged_into_conversation_id=str(payload.get("merged_into_conversation_id") or ""),
            merged_at=str(payload.get("merged_at") or ""),
        )


@dataclass
class ConversationRecord:
    id: str
    title: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    revision: int = 0
    conversation_type: ConversationType = DEFAULT_CONVERSATION_TYPE
    memory_mode: ConversationMemoryMode = "enabled"
    memory_polluted: bool = False
    memory_pollution_sources: list[str] = field(default_factory=list)
    permission_mode: ConversationPermissionMode = DEFAULT_CONVERSATION_PERMISSION_MODE
    permission_previous_mode: ConversationPermissionMode | str = ""
    permission_deny_rules: list[str] = field(default_factory=list)
    permission_overrides: dict[str, ConversationPermissionRuleLevel] = field(default_factory=dict)
    summary: str = ""
    compaction_state: ConversationCompactionState = "clean"
    compaction_summary: str = ""
    message_count: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    archived_at: str = ""
    workspace_root: str = ""
    git_branch: str = ""
    worktree_path: str = ""
    git_isolated: bool = False
    goal: dict[str, Any] = field(default_factory=dict)
    parent_conversation_id: str = ""
    parent_message_index: int | None = None
    fork_id: str = ""
    branch_kind: str = ""
    merged_into_conversation_id: str = ""
    merged_at: str = ""

    def to_summary(self) -> ConversationSummary:
        return ConversationSummary(
            id=self.id,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            revision=self.revision,
            conversation_type=self.conversation_type,
            memory_mode=self.memory_mode,
            memory_polluted=self.memory_polluted,
            memory_pollution_sources=list(self.memory_pollution_sources),
            permission_mode=self.permission_mode,
            summary=self.summary,
            compaction_state=self.compaction_state,
            message_count=self.message_count or len(self.transcript),
            archived=self.archived,
            archived_at=self.archived_at,
            workspace_root=self.workspace_root,
            git_branch=self.git_branch,
            worktree_path=self.worktree_path,
            git_isolated=self.git_isolated,
            goal=dict(self.goal or {}),
            parent_conversation_id=self.parent_conversation_id,
            parent_message_index=self.parent_message_index,
            fork_id=self.fork_id,
            branch_kind=self.branch_kind,
            merged_into_conversation_id=self.merged_into_conversation_id,
            merged_at=self.merged_at,
        )

    def to_meta_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("transcript", None)
        payload.pop("context_snapshot", None)
        payload["message_count"] = self.message_count or len(self.transcript)
        payload["encoding_version"] = "utf-8-v1"  # Mark all new writes as UTF-8
        return payload

    def to_dict(self) -> dict[str, Any]:
        self.message_count = self.message_count or len(self.transcript)
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConversationRecord":
        transcript = list(payload.get("transcript") or [])
        conversation_type = normalize_conversation_type(payload.get("conversation_type"))
        memory_polluted = bool(
            payload.get("memory_polluted")
            or payload.get("memory_pollution_sources")
            or payload.get("memory_generation_mode") == "polluted"
        )
        return cls(
            id=str(payload["id"]),
            title=str(payload.get("title") or "New chat"),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or utc_now_iso()),
            revision=_normalize_revision(payload.get("revision")),
            conversation_type=conversation_type,
            memory_mode=normalize_memory_mode(
                payload.get("memory_generation_mode", payload.get("memory_mode")),
                conversation_type=conversation_type,
                polluted=memory_polluted,
            ),
            memory_polluted=memory_polluted,
            memory_pollution_sources=_normalize_string_list(
                payload.get("memory_pollution_sources")
            ),
            permission_mode=normalize_permission_mode(
                payload.get("permission_mode", DEFAULT_CONVERSATION_PERMISSION_MODE)
            ),
            permission_previous_mode=_normalize_previous_permission_mode(payload.get("permission_previous_mode", "")),
            permission_deny_rules=normalize_permission_deny_rules(payload.get("permission_deny_rules", [])),
            permission_overrides=normalize_permission_overrides(payload.get("permission_overrides", {})),
            summary=str(payload.get("summary") or ""),
            compaction_state=payload.get("compaction_state", "clean"),
            compaction_summary=str(payload.get("compaction_summary") or ""),
            message_count=int(payload.get("message_count") or len(transcript)),
            transcript=transcript,
            context_snapshot=dict(payload.get("context_snapshot") or {}),
            archived=bool(payload.get("archived") or False),
            archived_at=str(payload.get("archived_at") or ""),
            workspace_root=str(payload.get("workspace_root") or ""),
            git_branch=str(payload.get("git_branch") or ""),
            worktree_path=str(payload.get("worktree_path") or ""),
            git_isolated=bool(payload.get("git_isolated") or False),
            goal=dict(payload.get("goal") or {}),
            parent_conversation_id=str(payload.get("parent_conversation_id") or ""),
            parent_message_index=(
                int(payload["parent_message_index"])
                if payload.get("parent_message_index") is not None
                else None
            ),
            fork_id=str(payload.get("fork_id") or ""),
            branch_kind=str(payload.get("branch_kind") or ""),
            merged_into_conversation_id=str(payload.get("merged_into_conversation_id") or ""),
            merged_at=str(payload.get("merged_at") or ""),
        )
