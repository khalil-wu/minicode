"""Idle archive lifecycle and lossless cold history representation changes."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.agent.conversation_query_guard import conversation_query_guards
from backend.conversations.repository import ConversationRepository
from backend.ws.conversation_activity import conversation_activity_blockers


def _current_scheduler():
    from backend.api import _state
    from backend.tasks import scheduler as scheduler_module

    return (_state.bootstrap.task_scheduler if _state.bootstrap is not None else None) or scheduler_module._GLOBAL_SCHEDULER


def _heartbeat_targets(scheduler) -> set[str]:
    return {task["conversation_id"] for task in scheduler.list_tasks()
        if task["kind"] == "heartbeat" and task["conversation_id"]} if scheduler is not None else set()


def _conversation_busy(manager, sessions, conversation_id: str, scheduler) -> bool:
    if any(session.active_conversation_id == conversation_id for session in sessions):
        return True
    context = manager.execution_contexts.context_for(conversation_id)
    if context is not None and context.has_active_execution():
        return True
    if scheduler is not None and conversation_id in scheduler.active_conversation_ids():
        return True
    # The manual archive blocker counts every enabled schedule so it can ask
    # before pausing them. Auto-archive only excludes actual scheduler work;
    # future heartbeat targets are retained separately, as Source aQe does.
    return bool(sessions and any(count for name, count in conversation_activity_blockers(sessions[0], conversation_id).items()
        if name != "scheduled_tasks"))


def archive_candidates(repository, *, inactive_days: int, protected_ids: set[str], now: datetime) -> list[Any]:
    if inactive_days == 0:
        return []
    cutoff = now - timedelta(days=inactive_days)
    return sorted((chat for chat in repository.list_conversations()
        if not chat.archived and not chat.pinned and not chat.has_unread_turn
        and chat.id not in protected_ids and chat.thread_source not in {"unknown", "cloud_threads.create", "subagent"}
        and datetime.fromisoformat(chat.updated_at) < cutoff), key=lambda chat: (chat.updated_at, chat.id))


def cold_history_files(repository, *, protected_ids: set[str], now: datetime) -> list[Path]:
    cutoff = now - timedelta(days=7)
    files = []
    for chat in repository.list_conversations():
        if chat.id in protected_ids:
            continue
        # Checkpoints reclaim old generations under this same owner lock.
        # Keep their paths and timestamps in one read; encoding stays outside.
        with repository._store_lock(chat.id):
            files.extend(file for file in repository.history_paths(chat.id)
                if file.is_file() and datetime.fromtimestamp(file.stat().st_mtime, UTC) <= cutoff)
    return files


def compress_cold_history(repository, files: list[Path], *, now: datetime) -> int:
    cutoff_ns = int((now - timedelta(days=7)).timestamp() * 1_000_000_000)
    return sum(repository.compress_history_file(file, cutoff_ns=cutoff_ns) for file in files)


async def maintain_local_chats(manager, *, auto_archive_after_days: int, compress_local_history: bool,
                              repository=None, now: datetime | None = None, compress=compress_cold_history) -> dict[str, Any]:
    from backend.ws.handlers.conversation import _broadcast_conversation_lists, _schedule_long_term_memory_forgetting

    current_time = now or datetime.now(UTC)
    sessions = list(manager.resource_sessions())
    repository = repository or (sessions[0].conversation_repo if sessions else ConversationRepository())
    scheduler = _current_scheduler()
    protected_ids = {chat.id for chat in repository.list_conversations()
        if _conversation_busy(manager, sessions, chat.id, scheduler)}
    archived = []
    candidates = archive_candidates(repository, inactive_days=auto_archive_after_days,
        protected_ids=protected_ids | _heartbeat_targets(scheduler), now=current_time) if sessions else []
    for candidate in candidates:
        if len(archived) == 50:
            break
        claim = conversation_query_guards().try_start(candidate.id, owner_id="maintenance:auto_archive")
        if claim is None:
            continue
        try:
            if _conversation_busy(manager, sessions, candidate.id, scheduler):
                continue
            latest = repository.get_conversation_summary(candidate.id)
            if latest is None or latest.revision != candidate.revision:
                continue
            # The normal archive lifecycle unloads the real core. This removes
            # parent-notification subscriptions, code stores and environments;
            # durable notifications and queued user input remain intact.
            if not await manager.unload_conversation_execution(candidate.id, reason="conversation_archived"):
                raise RuntimeError(f"本地聊天 {candidate.id} 的执行环境尚未释放，自动归档未完成。")
            if _conversation_busy(manager, sessions, candidate.id, scheduler) or candidate.id in _heartbeat_targets(scheduler):
                continue
            # The store lock shares metadata publication with every real write.
            # Recheck recency after claiming so a just-finished turn is retained.
            with repository._store_lock(candidate.id):
                latest = repository.get_conversation_summary(candidate.id)
                if latest is None or latest.revision != candidate.revision:
                    continue
                updated = repository.set_archived(candidate.id, True)
            # No yield occurs between the final live-run check, the archive
            # commit and pause_for_conversation disabling future schedules.
            # There is no active worker to cancel on this auto-archive path.
            if scheduler is not None:
                await scheduler.pause_for_conversation(candidate.id)
            archived.append(candidate.id)
            if sessions:
                _schedule_long_term_memory_forgetting(sessions[0], updated)
        finally:
            conversation_query_guards().end(claim)
    if archived and sessions:
        await _broadcast_conversation_lists(sessions[0])
    compressed_files = 0
    removed_history_temporaries = 0
    if compress_local_history:
        removed_history_temporaries = await asyncio.to_thread(repository.cleanup_history_temporaries,
            older_than_ns=int((current_time - timedelta(hours=6)).timestamp() * 1_000_000_000))
        files = cold_history_files(repository, protected_ids=protected_ids, now=current_time)
        compressed_files = await asyncio.to_thread(compress, repository, files, now=current_time)
    return {"archived_conversation_ids": archived, "compressed_files": compressed_files,
        "removed_history_temporaries": removed_history_temporaries}
