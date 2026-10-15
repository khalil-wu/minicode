"""Source Guardian context mode, private checkpoint and synchronous policy."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from typing import Any

from backend.agent.guardian.contract import GuardianReviewError


@dataclass(frozen=True)
class GuardianParentCheckpoint:
    item: dict[str, Any]
    origin: str
    model_hash: str | None = None

    def __post_init__(self):
        # Capture action-time state independently of later history mutations.
        object.__setattr__(self, "item", deepcopy(self.item))

    @property
    def is_usable(self) -> bool:
        return (
            self.item.get("type") in {"compaction", "context_compaction"}
            and isinstance(self.item.get("id"), str)
            and isinstance(self.item.get("encrypted_content"), str)
            and self.item["encrypted_content"] != ""
        )

    def is_compatible_with(self, reviewer_hash: str | None) -> bool:
        return self.model_hash is not None and self.model_hash != "" and self.model_hash == reviewer_hash

    def payload(self) -> dict[str, Any]:
        return {"item": deepcopy(self.item), "origin": self.origin, "model_hash": self.model_hash}

    def model_message(self):
        from backend.llm.base import LLMMessage
        from backend.llm.native_compaction import NATIVE_COMPACTION_TYPE, validate_compaction_window

        window = validate_compaction_window({"type": NATIVE_COMPACTION_TYPE,
            "origin": self.origin, "output": [deepcopy(self.item)], "compaction_model_hash": self.model_hash})
        return LLMMessage(role="assistant", content="", provider_items=[window])


def checkpoint_in_history(messages, checkpoint: GuardianParentCheckpoint | None) -> bool:
    from backend.llm.native_compaction import native_compaction_windows

    if checkpoint is None:
        return False
    latest = None
    for message in messages:
        for window in native_compaction_windows(message):
            for item in window["output"]:
                if item["type"] in {"compaction", "context_compaction"}:
                    latest = item
    return latest == checkpoint.item


@dataclass(frozen=True)
class GuardianReviewerReuseKey:
    configuration_signature: str
    parent_history_version: int
    parent_reset_version: int
    root_review_version: Any | None


class GuardianContextMode(str, Enum):
    LEGACY = "legacy"
    INDEPENDENT = "independent"
    THREAD_OWNED = "thread_owned"

    @classmethod
    def for_checkpoint(cls, checkpoint: GuardianParentCheckpoint | None, reviewer_hash: str | None):
        return cls.THREAD_OWNED if checkpoint is None or checkpoint.is_compatible_with(reviewer_hash) else cls.LEGACY


class ReviewContextPolicy(str, Enum):
    LEGACY = "legacy"
    LEGACY_WITH_CHECKPOINT_REUSE = "legacy_with_checkpoint_reuse"
    INDEPENDENT = "independent"
    THREAD_OWNED = "thread_owned"

    @classmethod
    def for_context(cls, mode: GuardianContextMode | str, *, reuse_parent_compaction: bool):
        selected = GuardianContextMode(mode)
        if selected is GuardianContextMode.LEGACY:
            return cls.LEGACY_WITH_CHECKPOINT_REUSE if reuse_parent_compaction else cls.LEGACY
        return cls(selected.value)

    def parent_compaction(self, checkpoint: GuardianParentCheckpoint | None) -> GuardianParentCheckpoint | None:
        if self in {self.LEGACY, self.INDEPENDENT} or checkpoint is None:
            return None
        if not checkpoint.is_usable:
            if self is self.LEGACY_WITH_CHECKPOINT_REUSE:
                return None
            raise GuardianReviewError("prompt_build", "parent compaction checkpoint is unusable for Guardian review")
        # Source synchronous review lets the backend validate cross-comp_hash
        # payloads; do not fabricate a reviewer producer hash or decrypt here.
        return checkpoint

    @property
    def uses_root_review_version(self) -> bool:
        return self in {self.INDEPENDENT, self.THREAD_OWNED}


class GuardianCheckpointRecoveryNeeded(GuardianReviewError):
    """A reused reviewer needs one restart from its captured parent checkpoint."""

    def __init__(self):
        super().__init__("input_budget", "Guardian needs compaction; restart from the parent checkpoint")
