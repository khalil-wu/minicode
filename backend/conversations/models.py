from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


ConversationMemoryMode = Literal["none", "summary", "profile"]
ConversationCompactionState = Literal["clean", "compacted", "retrieved"]
ConversationPermissionMode = Literal["default", "plan", "confirm", "bypass", "auto", "accept_edits"]
ConversationPermissionRuleLevel = Literal["auto", "confirm", "diff", "deny"]


def normalize_permission_mode(value: Any) -> ConversationPermissionMode:
    mode = str(value or "").strip().lower()
    if mode in {"default", "plan", "confirm", "bypass", "auto", "accept_edits"}:
        return mode
    return "default"


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


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ConversationSummary:
    id: str
    title: str
    created_at: str
    updated_at: str
    memory_mode: ConversationMemoryMode = "none"
    permission_mode: ConversationPermissionMode = "default"
    summary: str = ""
    compaction_state: ConversationCompactionState = "clean"
    message_count: int = 0
    archived: bool = False
    archived_at: str = ""
    workspace_root: str = ""
    git_branch: str = ""
    worktree_path: str = ""
    git_isolated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConversationSummary":
        return cls(
            id=str(payload["id"]),
            title=str(payload.get("title") or "New chat"),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or utc_now_iso()),
            memory_mode=payload.get("memory_mode", "none"),
            permission_mode=normalize_permission_mode(payload.get("permission_mode", "default")),
            summary=str(payload.get("summary") or ""),
            compaction_state=payload.get("compaction_state", "clean"),
            message_count=int(payload.get("message_count") or 0),
            archived=bool(payload.get("archived") or False),
            archived_at=str(payload.get("archived_at") or ""),
            workspace_root=str(payload.get("workspace_root") or ""),
            git_branch=str(payload.get("git_branch") or ""),
            worktree_path=str(payload.get("worktree_path") or ""),
            git_isolated=bool(payload.get("git_isolated") or False),
        )


@dataclass
class ConversationRecord:
    id: str
    title: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    memory_mode: ConversationMemoryMode = "none"
    permission_mode: ConversationPermissionMode = "default"
    permission_deny_rules: list[str] = field(default_factory=list)
    permission_overrides: dict[str, ConversationPermissionRuleLevel] = field(default_factory=dict)
    summary: str = ""
    compaction_state: ConversationCompactionState = "clean"
    compaction_summary: str = ""
    inherited_facts: list[dict[str, Any]] = field(default_factory=list)
    local_facts: list[dict[str, Any]] = field(default_factory=list)
    message_count: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    context_snapshot: dict[str, Any] = field(default_factory=dict)
    archived: bool = False
    archived_at: str = ""
    workspace_root: str = ""
    git_branch: str = ""
    worktree_path: str = ""
    git_isolated: bool = False

    def to_summary(self) -> ConversationSummary:
        return ConversationSummary(
            id=self.id,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            memory_mode=self.memory_mode,
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
        )

    def to_meta_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("transcript", None)
        payload.pop("context_snapshot", None)
        payload["message_count"] = self.message_count or len(self.transcript)
        return payload

    def to_dict(self) -> dict[str, Any]:
        self.message_count = self.message_count or len(self.transcript)
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ConversationRecord":
        transcript = list(payload.get("transcript") or [])
        return cls(
            id=str(payload["id"]),
            title=str(payload.get("title") or "New chat"),
            created_at=str(payload.get("created_at") or utc_now_iso()),
            updated_at=str(payload.get("updated_at") or utc_now_iso()),
            memory_mode=payload.get("memory_mode", "none"),
            permission_mode=normalize_permission_mode(payload.get("permission_mode", "default")),
            permission_deny_rules=normalize_permission_deny_rules(payload.get("permission_deny_rules", [])),
            permission_overrides=normalize_permission_overrides(payload.get("permission_overrides", {})),
            summary=str(payload.get("summary") or ""),
            compaction_state=payload.get("compaction_state", "clean"),
            compaction_summary=str(payload.get("compaction_summary") or ""),
            inherited_facts=list(payload.get("inherited_facts") or []),
            local_facts=list(payload.get("local_facts") or []),
            message_count=int(payload.get("message_count") or len(transcript)),
            transcript=transcript,
            context_snapshot=dict(payload.get("context_snapshot") or {}),
            archived=bool(payload.get("archived") or False),
            archived_at=str(payload.get("archived_at") or ""),
            workspace_root=str(payload.get("workspace_root") or ""),
            git_branch=str(payload.get("git_branch") or ""),
            worktree_path=str(payload.get("worktree_path") or ""),
            git_isolated=bool(payload.get("git_isolated") or False),
        )
