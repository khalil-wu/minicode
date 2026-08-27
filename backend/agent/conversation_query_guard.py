"""Process-wide query ownership using the Claude Code/Pi lifecycle contract.

Claude Code's QueryGuard synchronously transitions idle -> running, rejects a
second start, and uses a generation to fence stale cleanup. Pi likewise rejects
prompt() while activeRun exists and requires steer()/followUp() for queued input.
MiniCode applies that same state machine at process scope because WebSocket,
REST, and scheduled entry points can address the same durable conversation.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock


@dataclass
class _QueryState:
    status: str = "idle"
    generation: int = 0
    owner_id: str = ""


@dataclass(frozen=True)
class ConversationQueryClaim:
    conversation_id: str
    owner_id: str
    generation: int


class ConversationQueryGuardRegistry:
    """Synchronous QueryGuard registry keyed by durable conversation id."""

    def __init__(self) -> None:
        self._states: dict[str, _QueryState] = {}
        # Admission is synchronous by contract, but REST/scheduler hosts may
        # call it from different worker threads. Guard the check-and-transition
        # as one atomic operation so two entry points cannot both observe idle.
        self._lock = RLock()

    def try_start(
        self,
        conversation_id: str,
        *,
        owner_id: str,
    ) -> ConversationQueryClaim | None:
        conversation_id = str(conversation_id or "").strip()
        owner_id = str(owner_id or "").strip() or "anonymous"
        if not conversation_id:
            return ConversationQueryClaim("", owner_id, 0)
        with self._lock:
            state = self._states.setdefault(conversation_id, _QueryState())
            if state.status == "running":
                return None
            state.status = "running"
            state.generation += 1
            state.owner_id = owner_id
            return ConversationQueryClaim(
                conversation_id=conversation_id,
                owner_id=owner_id,
                generation=state.generation,
            )

    def end(self, claim: ConversationQueryClaim) -> bool:
        if not claim.conversation_id:
            return True
        with self._lock:
            state = self._states.get(claim.conversation_id)
            if (
                state is None
                or state.status != "running"
                or state.generation != claim.generation
                or state.owner_id != claim.owner_id
            ):
                return False
            state.status = "idle"
            state.owner_id = ""
            return True

    def active_claim(self, conversation_id: str) -> ConversationQueryClaim | None:
        conversation_id = str(conversation_id or "").strip()
        with self._lock:
            state = self._states.get(conversation_id)
            if state is None or state.status != "running":
                return None
            return ConversationQueryClaim(conversation_id, state.owner_id, state.generation)

    def owns(self, claim: ConversationQueryClaim) -> bool:
        """Return whether ``claim`` still owns the active generation."""

        if not claim.conversation_id:
            return True
        return self.active_claim(claim.conversation_id) == claim


_PROCESS_QUERY_GUARDS = ConversationQueryGuardRegistry()


def conversation_query_guards() -> ConversationQueryGuardRegistry:
    return _PROCESS_QUERY_GUARDS


__all__ = [
    "ConversationQueryClaim",
    "ConversationQueryGuardRegistry",
    "conversation_query_guards",
]
