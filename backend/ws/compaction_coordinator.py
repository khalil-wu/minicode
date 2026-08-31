"""Conversation-owned compaction transaction."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from backend.agent.context import clone_context_builder
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.conversations.repository import ConversationWriteConflict


@dataclass(frozen=True, slots=True)
class CompactionCommit:
    summary: str
    before_snapshot: dict[str, Any]
    after_snapshot: dict[str, Any]


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
    before_history: list[Any],
    after_history: list[Any],
) -> int:
    limit = min(len(before_history), len(after_history))
    matched = 0
    while matched < limit:
        if before_history[-(matched + 1)] != after_history[-(matched + 1)]:
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
    retained: dict[str, dict[str, Any]] = {}
    for message_id, raw_boundary in raw_admissions.items():
        if not isinstance(raw_boundary, dict):
            continue
        history_start = int(raw_boundary.get("history_start") or 0)
        history_end = int(raw_boundary.get("history_end") or 0)
        if history_start < removed_prefix or history_end < removed_prefix:
            continue
        retained[str(message_id)] = {
            **deepcopy(raw_boundary),
            "history_start": inserted_prefix + history_start - removed_prefix,
            "history_end": inserted_prefix + history_end - removed_prefix,
        }
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
) -> CompactionCommit:
    """Compact and CAS-publish one conversation snapshot."""

    clean_id = str(conversation_id or "").strip()
    if not clean_id:
        raise ValueError("conversation id is required for compaction")
    claim = conversation_query_guards().try_start(
        clean_id,
        owner_id=f"mutation:context.compact:{uuid4().hex}",
    )
    if claim is None:
        raise RuntimeError(
            "This conversation has an active turn. Stop it before compacting context."
        )
    projection_lock = session._conversation_projection_lock(clean_id)
    before_snapshot: dict[str, Any] | None = None
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
            summary = await transaction_builder.compact(
                focus=str(focus or "").strip() or None,
                restore_state=restore_state,
            )
            summary_text = str(summary or "").strip()
            if not summary_text:
                from backend.agent.context import CompactionNoopError

                raise CompactionNoopError()
            saved_snapshot = transaction_builder.export_snapshot()
            for key, value in previous_snapshot.items():
                if key not in saved_snapshot:
                    saved_snapshot[key] = deepcopy(value)
            rebase_turn_admissions_after_compaction(
                previous_snapshot,
                saved_snapshot,
            )
            committed = await asyncio.to_thread(
                session.conversation_repo.commit_compaction,
                clean_id,
                context_snapshot=saved_snapshot,
                state="compacted",
                summary=summary_text,
                expected_revision=expected_revision,
            )
            if committed is None:
                raise RuntimeError(
                    "Conversation disappeared while committing compaction"
                )
            _publish_snapshot_to_live_builder(
                session,
                conversation_id=clean_id,
                context_builder=context_builder,
                snapshot=saved_snapshot,
            )
            return CompactionCommit(
                summary=summary_text,
                before_snapshot=before_snapshot,
                after_snapshot=saved_snapshot,
            )
    except ConversationWriteConflict:
        current = await asyncio.to_thread(
            session.conversation_repo.get_conversation,
            clean_id,
        )
        if current is not None:
            _publish_snapshot_to_live_builder(
                session,
                conversation_id=clean_id,
                context_builder=context_builder,
                snapshot=dict(getattr(current, "context_snapshot", {}) or {}),
            )
        elif before_snapshot is not None:
            _publish_snapshot_to_live_builder(
                session,
                conversation_id=clean_id,
                context_builder=context_builder,
                snapshot=before_snapshot,
            )
        raise
    finally:
        conversation_query_guards().end(claim)
