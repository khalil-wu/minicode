"""Publish existing journal terminal facts into their conversation owner."""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from backend.agent.execution_journal import ExecutionJournal, conversation_projection_owner_fields, execution_journal_owner
from backend.async_cleanup import to_thread_cancel_safe
from backend.conversations.repository import ConversationRepository, ConversationWriteConflict

if TYPE_CHECKING:
    from backend.agent.runtime import AgentRuntime


async def _replay_projection(repository: ConversationRepository, journal: ExecutionJournal, *, conversation_id: str,
                             payload: dict[str, Any], source_event_id: str, minimal: bool = False) -> None:
    record = await to_thread_cancel_safe(repository.get_conversation, conversation_id)
    if record is None:
        raise RuntimeError("conversation disappeared while replaying terminal journal")
    snapshot = dict(payload.get("context_snapshot") or {})
    owner = conversation_projection_owner_fields(payload, {
        **snapshot, **dict((payload.get("context_delta") or {}).get("set", {})),
    })
    source_user_ids = payload.get("source_user_message_ids", owner["source_user_message_ids"] or None)
    applied = True
    superseded_reason = ""
    expected_revision = payload.get("expected_revision")
    if (minimal and (not source_user_ids or "context_delta" not in payload)) or (
        expected_revision is None and not source_user_ids
    ):
        # Legacy terminal facts without an admitted identity/delta cannot be
        # merged into a newer conversation head. Keep the original fact while
        # acknowledging that its public projection is no longer applicable.
        applied = False
        superseded_reason = "legacy_projection_has_no_owned_delta" if minimal else "legacy_projection_has_no_owner_or_revision"
    else:
        try:
            record = await to_thread_cancel_safe(
                repository.commit_turn_projection,
                conversation_id,
                assistant_message=payload.get("assistant_message"),
                context_snapshot=snapshot,
                summary=payload.get("summary"),
                expected_revision=expected_revision if expected_revision is not None else record.revision,
                source_user_message_ids=source_user_ids,
                source_run_id=owner["run_id"],
                **({"context_delta": payload["context_delta"], "partial": bool(payload.get("partial"))}
                   if "context_delta" in payload else {}),
            )
            if record is None:
                raise RuntimeError("conversation no longer exists")
        except ConversationWriteConflict:
            record = await to_thread_cancel_safe(repository.get_conversation, conversation_id)
            if record is None:
                raise RuntimeError("conversation no longer exists")
            applied = False
            superseded_reason = "source_turn_or_snapshot_superseded"
    await to_thread_cancel_safe(journal.append_lifecycle,
        "conversation_projection_committed",
        {
            "conversation_id": conversation_id,
            "pending_event_id": source_event_id,
            "conversation_revision": record.revision,
            "message_id": str((payload.get("assistant_message") or {}).get("id") or ""),
            "run_id": owner["run_id"],
            "recovered": True,
            "applied": applied,
            **({"partial": True} if payload.get("partial") else {}),
            **({"minimal_projection": True} if minimal else {}),
            **({"superseded": True, "superseded_reason": superseded_reason} if not applied else {}),
        },
    )


async def replay_pending_conversation_projections(repository: ConversationRepository, journal: ExecutionJournal, *, conversation_id: str) -> None:
    """Recover applicable facts; settle superseded facts without changing history."""
    for pending in await to_thread_cancel_safe(journal.pending_conversation_projections):
        payload = dict(pending.payload)
        if str(payload.get("conversation_id") or "") == conversation_id:
            await _replay_projection(repository, journal, conversation_id=conversation_id,
                                     payload=payload, source_event_id=pending.event_id)
    for projection in await to_thread_cancel_safe(journal.unprojected_terminal_projections):
        if str(projection.get("conversation_id") or "") == conversation_id:
            await _replay_projection(repository, journal, conversation_id=conversation_id,
                                     payload=projection, source_event_id=projection["source_event_id"], minimal=True)


async def recover_persisted_conversation_projections(repository: ConversationRepository, *, conversation_id: str,
                                                   runtime: AgentRuntime | None = None) -> None:
    """Recover the conversation's journal before exposing its cold snapshot."""
    from backend.agent.runtime import SWARM_DIR, default_runtime_if_initialized
    from backend.agent.swarm_store import FileSwarmStore
    from backend.agent.conversation_query_guard import conversation_query_guards

    if conversation_query_guards().active_claim(conversation_id) is not None:
        return

    owner = execution_journal_owner("conversation", await to_thread_cancel_safe(repository.store_instance_id), conversation_id)
    runtime = runtime if runtime is not None else default_runtime_if_initialized()
    journal_root = runtime._journal_root if runtime is not None else SWARM_DIR.parent / "sidechains"
    if not (journal_root / owner / "events.jsonl").is_file():
        return
    if runtime is not None:
        journal = await to_thread_cancel_safe(runtime.execution_journal, owner)
    else:
        store = FileSwarmStore(SWARM_DIR)
        journal = await to_thread_cancel_safe(ExecutionJournal, owner, base_dir=journal_root,
                                             terminal_record_reader=store.get_agent_run)
    await replay_pending_conversation_projections(repository, journal, conversation_id=conversation_id)
