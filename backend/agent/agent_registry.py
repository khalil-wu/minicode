"""Authoritative identity and lifecycle registry for agent runs.

The UI may address an agent by a short id, but coordination must use a stable
path and mailbox epoch.  This registry is deliberately small and independent
of ``AgentRuntime`` so it can be used by recovery, replay, and tests without
creating another event loop or persistence dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AgentRegistration:
    kind: str
    agent_id: str
    agent_path: str
    parent_id: str = ""
    mailbox_epoch: int = 0
    sealed: bool = False


class AgentRegistry:
    """Tracks immutable paths and rejects stale mailbox/lifecycle updates."""

    def __init__(self) -> None:
        self._registrations: dict[tuple[str, str], AgentRegistration] = {}

    @staticmethod
    def _key(kind: str, agent_id: str) -> tuple[str, str]:
        clean_kind = str(kind or "").strip() or "run"
        clean_id = str(agent_id or "").strip()
        if not clean_id:
            raise ValueError("agent_id is required")
        return clean_kind, clean_id

    def register(self, record: Any, *, kind: str) -> AgentRegistration:
        agent_id = str(getattr(record, "run_id", None) or getattr(record, "subagent_id", "")).strip()
        key = self._key(kind, agent_id)
        path = str(getattr(record, "agent_path", "") or agent_id).strip() or agent_id
        parent_id = str(getattr(record, "parent_run_id", "") or "").strip()
        epoch = max(0, int(getattr(record, "mailbox_epoch", 0) or 0))
        previous = self._registrations.get(key)
        # A reused id must advance its epoch and may never change its path.
        if previous is not None and previous.agent_path != path:
            raise ValueError(f"agent path is immutable for {kind}:{agent_id}")
        if previous is not None and previous.sealed and epoch <= previous.mailbox_epoch:
            raise ValueError(f"cannot reopen sealed {kind}:{agent_id} without a new mailbox epoch")
        registration = AgentRegistration(
            kind=key[0],
            agent_id=key[1],
            agent_path=previous.agent_path if previous else path,
            parent_id=parent_id or (previous.parent_id if previous else ""),
            mailbox_epoch=max(epoch, previous.mailbox_epoch if previous else 0),
            sealed=False,
        )
        self._registrations[key] = registration
        return registration

    def get(self, agent_id: str, *, kind: str = "run") -> AgentRegistration | None:
        return self._registrations.get(self._key(kind, agent_id))

    def seal(self, agent_id: str, *, kind: str) -> AgentRegistration | None:
        key = self._key(kind, agent_id)
        current = self._registrations.get(key)
        if current is None:
            return None
        sealed = AgentRegistration(
            kind=current.kind,
            agent_id=current.agent_id,
            agent_path=current.agent_path,
            parent_id=current.parent_id,
            mailbox_epoch=current.mailbox_epoch,
            sealed=True,
        )
        self._registrations[key] = sealed
        return sealed

    def discard(self, agent_id: str, *, kind: str) -> bool:
        """Forget a registration after its owning conversation is deleted."""
        return self._registrations.pop(self._key(kind, agent_id), None) is not None

    def accepts_mailbox(
        self,
        *,
        subagent_id: str,
        mailbox_epoch: int,
        parent_run_id: str = "",
    ) -> bool:
        current = self.get(subagent_id, kind="subagent")
        if current is None:
            return False
        if int(mailbox_epoch or 0) != current.mailbox_epoch:
            return False
        expected_parent = str(parent_run_id or "").strip()
        return not expected_parent or current.parent_id == expected_parent

    def accepts_update(
        self,
        *,
        agent_id: str,
        kind: str,
        agent_path: str,
        mailbox_epoch: int = 0,
    ) -> bool:
        """Validate a mutable lifecycle update, rejecting sealed/stale state."""
        current = self.get(agent_id, kind=kind)
        if current is None or current.sealed:
            return False
        if str(agent_path or "").strip() != current.agent_path:
            return False
        if kind == "subagent" and int(mailbox_epoch or 0) != current.mailbox_epoch:
            return False
        return True

    def snapshot(self) -> tuple[AgentRegistration, ...]:
        return tuple(self._registrations.values())
