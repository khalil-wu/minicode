"""Stable identities for runs and subagents.

The runtime historically keyed coordination by a mutable ``subagent_id``.
That is convenient for a UI but insufficient after restart, retries, or late
mailbox delivery.  ``AgentPath`` gives every run an immutable, human-readable
path while ``MailboxEpoch`` lets callers reject messages from an older
incarnation of the same logical agent.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentPath:
    segments: tuple[str, ...]

    @classmethod
    def main(cls, run_id: str) -> "AgentPath":
        return cls((str(run_id or "main").strip() or "main",))

    def child(self, subagent_id: str) -> "AgentPath":
        value = str(subagent_id or "").strip()
        if not value:
            raise ValueError("subagent_id is required for an AgentPath child")
        return AgentPath((*self.segments, value))

    @property
    def value(self) -> str:
        return "/".join(self.segments)

    @classmethod
    def parse(cls, value: str) -> "AgentPath":
        segments = tuple(part for part in str(value or "").split("/") if part)
        return cls(segments or ("main",))


@dataclass(frozen=True)
class MailboxEpoch:
    value: int = 0

    def next(self) -> "MailboxEpoch":
        return MailboxEpoch(max(0, int(self.value)) + 1)
