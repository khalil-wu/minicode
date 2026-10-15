"""Conversation-owned compaction transaction."""

from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe

import asyncio
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Awaitable, Callable
from uuid import uuid4

from backend.agent.context import clone_context_builder
from backend.agent.context_window import is_context_window_rollover
from backend.agent.context_compaction_hooks import (
    ContextCompactionStopped, context_compaction_hook_events, run_context_compaction_hook,
)
from backend.agent.message import AgentEvent
from backend.hooks.manager import HookEvent
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.agent.run_context import RunContext
from backend.conversations.repository import ConversationWriteConflict
from backend.conversations.context_delta import rebase_turn_admissions


@dataclass(frozen=True, slots=True)
class CompactionCommit:
    summary: str
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]
    compaction_item: dict[str, Any] | None = None


class CompactionCommittedProjectionError(RuntimeError):
    """The durable compaction committed, but its live builder did not refresh."""

    def __init__(self, committed: CompactionCommit) -> None:
        super().__init__("Context compaction was saved, but the active context could not be refreshed")
        self.committed = committed


def _publish_snapshot_to_live_builder(
    session: Any,
    *,
    conversation_id: str,
    context_builder: Any,
    snapshot: dict[str, Any],
) -> None:
    conversation_runtime = getattr(session, "conversation_runtime", None)
    shared_builder = getattr(conversation_runtime, "_context_builder", None)
    if (
        context_builder is shared_builder
        and str(
            getattr(conversation_runtime, "active_conversation_id", "") or ""
        ).strip()
        != conversation_id
    ):
        return
    load_snapshot = getattr(context_builder, "load_snapshot", None)
    if callable(load_snapshot):
        load_snapshot(deepcopy(snapshot))


def _common_history_suffix_length(
    before_history: list[dict[str, Any]],
    after_history: list[dict[str, Any]],
) -> int:
    limit = min(len(before_history), len(after_history))
    matched = 0
    while matched < limit:
        before = before_history[-(matched + 1)]
        after = after_history[-(matched + 1)]
        # Loading a legacy admission annotates user origin without changing
        # the retained provider item or its admission boundary.
        if (before | {"is_user_input": None}) != (after | {"is_user_input": None}):
            break
        matched += 1
    return matched


def rebase_turn_admissions_after_compaction(
    before_snapshot: dict[str, Any],
    after_snapshot: dict[str, Any],
) -> None:
    """Keep only admission boundaries that survived the compacted prefix."""

    raw_admissions = before_snapshot.get("turn_admissions")
    if not isinstance(raw_admissions, dict):
        after_snapshot.pop("turn_admissions", None)
        return
    before_history = list(before_snapshot.get("history") or [])
    after_history = list(after_snapshot.get("history") or [])
    suffix_length = _common_history_suffix_length(before_history, after_history)
    removed_prefix = len(before_history) - suffix_length
    inserted_prefix = len(after_history) - suffix_length
    normalized = {
        str(message_id): {**deepcopy(boundary), "history_start": int(boundary.get("history_start") or 0),
                          "history_end": int(boundary.get("history_end") or 0)}
        for message_id, boundary in raw_admissions.items() if isinstance(boundary, dict)
    }
    retained = rebase_turn_admissions(normalized, removed_prefix=removed_prefix, inserted_prefix=inserted_prefix)
    if retained:
        after_snapshot["turn_admissions"] = retained
    else:
        after_snapshot.pop("turn_admissions", None)


async def compact_conversation(
    session: Any,
    *,
    conversation_id: str,
    context_builder: Any,
    focus: str = "",
    restore_state: Any = None,
    live_context_builder: Any = None,
    on_started: Callable[[], Awaitable[None]] | None = None,
    on_completed: Callable[[CompactionCommit], Awaitable[None]] | None = None,
    on_hook_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    compaction_item: dict[str, Any] | None = None,
    operation_turn_id: str = "",
) -> CompactionCommit:
    """Compact and CAS-publish one conversation snapshot."""

    clean_id = str(conversation_id or "").strip()
    if not clean_id:
        raise ValueError("conversation id is required for compaction")
    claim = conversation_query_guards().try_start(
        clean_id,
        owner_id=f"mutation:context.compact:{uuid4().hex}",
        owner=RunContext(turn_kind="compact"),
    )
    if claim is None:
        raise RuntimeError(
            "This conversation has an active turn. Stop it before compacting context."
        )
    projection_lock = session._conversation_projection_lock(clean_id)
    before_snapshot: dict[str, Any] | None = None
    projection_builder = context_builder if live_context_builder is None else live_context_builder
    try:
        conversation_runtime = getattr(session, "conversation_runtime", None)
        wait_for_hydration = getattr(
            conversation_runtime,
            "wait_for_hydration",
            None,
        )
        if callable(wait_for_hydration):
            await wait_for_hydration(clean_id)
        async with projection_lock:
            current = await asyncio.to_thread(
                session.conversation_repo.get_conversation,
                clean_id,
            )
            if current is None:
                raise LookupError("Conversation not found")
            previous_snapshot = deepcopy(
                dict(getattr(current, "context_snapshot", {}) or {})
            )
            before_snapshot = deepcopy(previous_snapshot)
            expected_revision = max(0, int(getattr(current, "revision", 0) or 0))
            load_snapshot = getattr(context_builder, "load_snapshot", None)
            if callable(load_snapshot):
                transaction_builder = clone_context_builder(context_builder)
                transaction_builder.load_snapshot(previous_snapshot)
            else:
                transaction_builder = context_builder
            outcome = await run_context_compaction_hook(transaction_builder, HookEvent.PRE_COMPACT, trigger="manual")
            for event in context_compaction_hook_events(outcome):
                event.data["conversation_id"] = clean_id
                if on_hook_event is not None:
                    await on_hook_event(event)
                else:
                    await session.send_event(event)
            if outcome is not None and outcome.prevent_continuation:
                raise ContextCompactionStopped(HookEvent.PRE_COMPACT, outcome)
            if on_started is not None:
                await on_started()
            summary = await transaction_builder.compact(
                focus=str(focus or "").strip() or None,
                restore_state=restore_state,
                manual=True,
            )
            summary_text = str(summary or "").strip()
            saved_snapshot = transaction_builder.export_snapshot()
            if not summary_text and not is_context_window_rollover(previous_snapshot, saved_snapshot):
                from backend.agent.context import CompactionNoopError

                raise CompactionNoopError()
            for key, value in previous_snapshot.items():
                if key not in saved_snapshot:
                    saved_snapshot[key] = deepcopy(value)
            rebase_turn_admissions_after_compaction(
                previous_snapshot,
                saved_snapshot,
            )
            if callable(load_snapshot):
                # The live builder clears itself before decoding. Verify the
                # committed snapshot on the transaction clone first, so bad
                # serialized context never reaches disk or a live builder.
                transaction_builder.load_snapshot(deepcopy(saved_snapshot))
            completed_item = {**compaction_item, "status": "completed", "finished_at": int(time.time() * 1_000)} if compaction_item is not None else None
            committed = await to_thread_cancel_safe(
                session.conversation_repo.commit_compaction,
                clean_id,
                context_snapshot=saved_snapshot,
                state="compacted",
                summary=summary_text,
                expected_revision=expected_revision,
                **({"compaction_item": completed_item,
                    "operation_turn_id": operation_turn_id} if compaction_item is not None else {}),
            )
            if committed is None:
                raise RuntimeError(
                    "Conversation disappeared while committing compaction"
                )
            result = CompactionCommit(
                summary=summary_text,
                before_snapshot=before_snapshot,
                after_snapshot=saved_snapshot,
                compaction_item=completed_item,
            )
            try:
                _publish_snapshot_to_live_builder(
                    session,
                    conversation_id=clean_id,
                    context_builder=context_builder,
                    snapshot=saved_snapshot,
                )
                if projection_builder is not context_builder:
                    _publish_snapshot_to_live_builder(session, conversation_id=clean_id,
                        context_builder=projection_builder, snapshot=saved_snapshot)
            except Exception as exc:
                if (
                    conversation_runtime is not None
                    and projection_builder is getattr(conversation_runtime, "_context_builder", None)
                ):
                    # A failed load may have cleared or partially populated the
                    # shared builder. Its next query must hydrate from the CAS
                    # committed repository snapshot before constructing a prompt.
                    conversation_runtime.defer_repository_hydration(
                        clean_id,
                        on_hydration_complete=session._on_conversation_hydration_complete,
                    )
                raise CompactionCommittedProjectionError(result) from exc
            if on_completed is not None:
                await on_completed(result)
            outcome = await run_context_compaction_hook(context_builder, HookEvent.POST_COMPACT, trigger="manual")
            for event in context_compaction_hook_events(outcome):
                event.data["conversation_id"] = clean_id
                if on_hook_event is not None:
                    await on_hook_event(event)
                else:
                    await session.send_event(event)
            if outcome is not None and outcome.prevent_continuation:
                raise ContextCompactionStopped(HookEvent.POST_COMPACT, outcome, committed=result)
            return result
    except asyncio.CancelledError:
        # The blocking CAS may have committed before cancellation was delivered.
        # Reserve the existing repository-hydration path before another query
        # can use the shared builder's pre-compaction history.
        conversation_runtime = getattr(session, "conversation_runtime", None)
        if (
            before_snapshot is not None
            and projection_builder is getattr(conversation_runtime, "_context_builder", None)
            and conversation_runtime.active_conversation_id == clean_id
        ):
            conversation_runtime.defer_repository_hydration(
                clean_id,
                on_hydration_complete=session._on_conversation_hydration_complete,
            )
        raise
    except ConversationWriteConflict:
        current = await asyncio.to_thread(
            session.conversation_repo.get_conversation,
            clean_id,
        )
        if current is not None:
            _publish_snapshot_to_live_builder(
                session,
                conversation_id=clean_id,
                context_builder=projection_builder,
                snapshot=dict(getattr(current, "context_snapshot", {}) or {}),
            )
        elif before_snapshot is not None:
            _publish_snapshot_to_live_builder(
                session,
                conversation_id=clean_id,
                context_builder=projection_builder,
                snapshot=before_snapshot,
            )
        raise
    finally:
        conversation_query_guards().end(claim)
