"""Authoritative waiters owned by one websocket session."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TurnWaitState:
    """Authoritative session-owned state for turn interaction waits.

    The websocket can host more than one conversation, so the durable
    follow-up queue remains keyed by conversation.  This object owns the
    queue instances and every interactive waiter that can outlive one model
    request: approvals, user questions, MCP elicitation, and provider OAuth.
    Each waiter belongs to exactly one kind-specific lane.  The lane maps are
    intentionally independent: an approval is in ``pending_approvals``, an
    in-turn question is in ``pending_user_input``, an MCP/control elicitation
    is in ``pending_elicitations``, and a provider OAuth prompt is in
    ``provider_oauth_pending``.
    """

    pending_approvals: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    pending_approval_payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_approval_responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    pending_user_input: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    pending_elicitations: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    provider_oauth_pending: dict[str, asyncio.Future[Any]] = field(default_factory=dict)
    turn_input_queues: dict[str, Any] = field(default_factory=dict)
    interrupted_conversation_ids: set[str] = field(default_factory=set)

    @classmethod
    def for_session(cls, session: Any) -> "TurnWaitState":
        """Return the session-owned wait state, creating it when needed."""

        state = getattr(session, "turn_wait_state", None)
        if state is not None:
            return state
        state = cls()
        setattr(session, "turn_wait_state", state)
        return state

    def register_waiter(
        self,
        request_id: str,
        future: asyncio.Future[Any],
        *,
        kind: str = "approval",
    ) -> None:
        """Register one future in exactly the lane selected by ``kind``."""

        key = str(request_id or "").strip()
        if not key:
            raise ValueError("waiter request id is required")
        if kind == "approval":
            self.pending_approvals[key] = future
        elif kind == "user_input":
            self.pending_user_input[key] = future
        elif kind == "elicitation":
            self.pending_elicitations[key] = future
        elif kind == "provider_oauth":
            self.provider_oauth_pending[key] = future
        else:
            raise ValueError(f"unknown waiter kind: {kind}")

    def remove_waiter(self, request_id: str) -> asyncio.Future[Any] | None:
        """Remove one waiter from every index and return its future."""

        key = str(request_id or "").strip()
        future = self.pending_approvals.pop(key, None)
        for waiters in (
            self.pending_user_input,
            self.pending_elicitations,
            self.provider_oauth_pending,
        ):
            candidate = waiters.pop(key, None)
            if future is None:
                future = candidate
        return future

    def waiter_ids(self) -> set[str]:
        return set(
            self.pending_approvals
            ) | set(self.pending_user_input) | set(self.pending_elicitations) | set(
                self.provider_oauth_pending
            ) | set(self.pending_approval_payloads) | set(self.pending_approval_responses)

    def clear_pending_waiters(self) -> None:
        futures: set[asyncio.Future[Any]] = set()
        for waiters in (
            self.pending_approvals,
            self.pending_user_input,
            self.pending_elicitations,
            self.provider_oauth_pending,
        ):
            futures.update(waiters.values())
        for future in futures:
            if not future.done():
                future.cancel()
        self.pending_approvals.clear()
        self.pending_approval_payloads.clear()
        self.pending_approval_responses.clear()
        self.pending_user_input.clear()
        self.pending_elicitations.clear()
        self.provider_oauth_pending.clear()
