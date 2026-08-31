from __future__ import annotations

import asyncio
import copy
import json
import logging
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Any, TYPE_CHECKING

from backend.agent.message import AgentEvent
from backend.memory.pollution import pollution_sources_from_transcript
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    await_with_deadline,
    retain_cleanup_task,
)
from backend.ws.conversation_errors import emit_conversation_not_found
if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)


async def _emit_context_error(
    session: "WebSocketSession",
    message: str,
    *,
    recoverable: bool,
    conversation_id: str = "",
) -> None:
    """Report a context-domain failure through the validated event constructor.

    The client's inbound contract for ``error`` requires ``message``,
    ``error_type`` **and** ``recoverable``. Hand-built ``{"type": "error"}``
    dicts omitted ``recoverable``, so every one of these rejections was dropped
    by the renderer's validator and the user saw nothing at all.
    """

    event = AgentEvent.error(message, recoverable=recoverable, error_type="context")
    if conversation_id:
        event.data["conversation_id"] = conversation_id
    await session.send_event(event)


def _all_live_sessions(session: "WebSocketSession") -> list["WebSocketSession"]:
    manager = session.ws_manager
    if manager is None:
        return [session]
    sessions = list(manager.iter_sessions())
    return sessions if session in sessions else [session, *sessions]


def _conversation_cleanup_owner(
    session: "WebSocketSession",
    conversation_id: str,
) -> set[Any] | None:
    """Return the owner that outlives the websocket for destructive cleanup."""

    manager = session.ws_manager
    if manager is not None:
        owner = manager.conversation_delete_cleanup_owner(conversation_id)
        if isinstance(owner, set):
            return owner
    return session.cleanup_tasks


async def _broadcast_conversation_lists(session: "WebSocketSession") -> list[str]:
    """Publish one authoritative inventory snapshot to every connected renderer."""

    errors: list[str] = []
    for owner_session in _all_live_sessions(session):
        if not owner_session.is_connected:
            continue
        try:
            sent = await await_with_deadline(
                owner_session.send_conversation_list(),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"conversation inventory for session {owner_session.session_id}",
            )
            if not sent:
                errors.append(str(owner_session.session_id))
        except Exception:
            logger.exception(
                "Failed to broadcast conversation inventory to session %s",
                owner_session.session_id,
            )
            errors.append(str(owner_session.session_id))
    return errors


async def _switch_active_sessions_to_conversation_workspace(
    session: "WebSocketSession",
    conversation: Any,
    *,
    announce_initiator: bool,
    source: str,
) -> list[str]:
    """Move every live runtime that currently owns the conversation."""

    from backend.services.conversation_payload_service import build_conversation_switched_payload

    conversation_id = str(getattr(conversation, "id", "") or "").strip()
    errors: list[str] = []
    for owner_session in _all_live_sessions(session):
        if owner_session.active_conversation_id != conversation_id:
            continue
        try:
            switched = await owner_session.switch_workspace_for_conversation(
                conversation,
                announce=bool(announce_initiator and owner_session is session),
                wait_for_initialize=True,
            )
            if not switched:
                raise RuntimeError("workspace activation did not complete")
            current = owner_session.conversation_repo.get_conversation(conversation_id)
            if current is None:
                raise RuntimeError("conversation disappeared after workspace activation")
            await owner_session.send_payload(
                build_conversation_switched_payload(
                    current,
                    is_hydrating=False,
                    runtime_snapshot=owner_session.runtime_snapshot(),
                ),
                log_context="conversation.switched",
            )
        except Exception:
            logger.exception(
                "Failed to align %s workspace for session %s during %s",
                conversation_id,
                getattr(owner_session, "session_id", ""),
                source,
            )
            errors.append(str(getattr(owner_session, "session_id", "")))
    return errors


async def _release_active_sessions_from_conversation_workspace(
    session: "WebSocketSession",
    conversation_id: str,
) -> list["WebSocketSession"]:
    """Stop workspace-owned tasks before a checkout is moved or removed."""

    released: list["WebSocketSession"] = []
    pending_workspace_tasks: list[asyncio.Task[Any]] = []
    for owner_session in _all_live_sessions(session):
        if owner_session.active_conversation_id != conversation_id:
            continue
        released.append(owner_session)
        lifecycle = owner_session.session_lifecycle
        for task in (lifecycle.workspace_context_task, lifecycle.workspace_mcp_task):
            if task is not None and not task.done():
                pending_workspace_tasks.append(task)
        lifecycle.clear_workspace_runtime()
    if pending_workspace_tasks:
        await asyncio.gather(*pending_workspace_tasks, return_exceptions=True)
    return released


def _conversation_has_active_run(session: "WebSocketSession", conversation_id: str) -> bool:
    if any(
        callable(getattr(owner_session, "running_agent_task_for", None))
        and owner_session.running_agent_task_for(conversation_id) is not None
        for owner_session in _all_live_sessions(session)
    ):
        return True
    try:
        from backend.agent.conversation_query_guard import conversation_query_guards

        return conversation_query_guards().active_claim(conversation_id) is not None
    except Exception:
        logger.exception("Failed to inspect query ownership for %s", conversation_id)
        # Lifecycle mutations fail closed when process-wide ownership cannot be
        # established; otherwise REST/scheduled work could be mutated under a
        # detached writer.
        return True


def _try_claim_conversation_mutation(
    session: "WebSocketSession",
    conversation_id: str,
    *,
    operation: str,
) -> Any | None:
    from backend.agent.conversation_query_guard import conversation_query_guards

    owner_id = (
        f"mutation:{operation}:{str(getattr(session, 'session_id', '') or 'session')}:"
        f"{secrets.token_hex(8)}"
    )
    return conversation_query_guards().try_start(
        conversation_id,
        owner_id=owner_id,
    )


def _release_conversation_mutation(claim: Any | None) -> None:
    if claim is None:
        return
    from backend.agent.conversation_query_guard import conversation_query_guards

    if not conversation_query_guards().end(claim):
        logger.error(
            "Conversation mutation query claim was replaced before release: %s",
            getattr(claim, "conversation_id", ""),
        )


def _conversation_activity_blockers(
    session: "WebSocketSession",
    conversation_id: str,
) -> dict[str, int]:
    """Count live resources that would remain hidden after archival/handoff."""

    owner = str(conversation_id or "").strip()
    counts = {
        "background_commands": 0,
        "terminal_sessions": 0,
        "preview_processes": 0,
        "scheduled_tasks": 0,
    }
    if not owner:
        return counts

    live_sessions = _all_live_sessions(session)
    for owner_session in live_sessions:
        try:
            counts["background_commands"] += len(
                owner_session.background_manager.list_commands(
                    conversation_id=owner,
                )
            )
        except Exception:
            logger.exception(
                "Failed to inspect background commands for %s in session %s",
                owner,
                getattr(owner_session, "session_id", ""),
            )
            counts["background_commands"] += 1
        try:
            counts["terminal_sessions"] += len(
                owner_session.terminal_manager.list_sessions_for_conversation(owner)
            )
        except Exception:
            logger.exception(
                "Failed to inspect terminal sessions for %s in session %s",
                owner,
                getattr(owner_session, "session_id", ""),
            )
            counts["terminal_sessions"] += 1

    try:
        from backend.preview.launcher import running_preview_processes

        counts["preview_processes"] = sum(
            len(running_preview_processes(
                session_id=str(getattr(owner_session, "session_id", "") or ""),
                conversation_id=owner,
            ))
            for owner_session in live_sessions
        )
    except Exception:
        logger.exception("Failed to inspect preview processes for %s", owner)
        counts["preview_processes"] = 1

    try:
        from backend.api import _state as api_state
        from backend.tasks import scheduler as scheduler_module

        bootstrap = getattr(api_state, "bootstrap", None)
        scheduler = getattr(bootstrap, "task_scheduler", None) or getattr(
            scheduler_module,
            "_GLOBAL_SCHEDULER",
            None,
        )
        if scheduler is not None:
            counts["scheduled_tasks"] = sum(
                1
                for task in scheduler.list_tasks()
                if str(task.get("conversation_id") or "").strip() == owner
                and bool(task.get("enabled", True))
            )
    except Exception:
        logger.exception("Failed to inspect scheduled tasks for %s", owner)
        counts["scheduled_tasks"] = 1

    return counts


def _apply_handoff_runtime_blockers(
    preflight: dict[str, Any],
    resource_counts: dict[str, int],
) -> dict[str, Any]:
    relevant = {
        name: int(resource_counts.get(name, 0) or 0)
        for name in ("background_commands", "terminal_sessions", "preview_processes")
        if int(resource_counts.get(name, 0) or 0) > 0
    }
    if not relevant:
        return preflight
    checks = list(preflight.get("checks") or [])
    checks.append({
        "code": "runtime.resources_active",
        "severity": "blocking",
        "message": "Stop live background commands, terminals, and previews before moving this task.",
        "details": {"resources": relevant},
    })
    return {
        **preflight,
        "checks": checks,
        "allowed": False,
        "runtime_resources": relevant,
    }


async def _stop_conversation_run(session: "WebSocketSession", conversation_id: str, *, reason: str) -> bool:
    """Cancel a run and report whether its lifecycle actually converged."""
    session.run_manager.clear_user_message_queue(conversation_id)
    task = session.running_agent_task_for(conversation_id)
    await session.cancel_agent_runs(conversation_id=conversation_id, reason=reason)
    # RunManager.cancel performs the shared bounded drain.  Never follow it
    # with an unbounded await: a cancellation-resistant tool must block the
    # destructive operation, not the websocket forever.
    parent_stopped = task is None or task.done()
    if task is not None and task.done():
        with suppress(asyncio.CancelledError, Exception):
            task.result()

    children_stopped = True
    try:
        from backend.agent.runtime import default_runtime_if_initialized

        runtime = default_runtime_if_initialized()
        if runtime is not None:
            children_stopped = bool(
                await runtime.stop_subagent_tasks_for_conversation(conversation_id, reason=reason)
            )
    except Exception:
        logger.exception("Failed to stop subagents for conversation %s", conversation_id)
        children_stopped = False
    return parent_stopped and children_stopped


async def _purge_conversation_runtime_state(
    session: "WebSocketSession",
    conversation_id: str,
) -> tuple[dict[str, int], list[str]]:
    """Remove non-transcript state only after every writer has stopped."""
    owner = str(conversation_id or "").strip()
    counts: dict[str, int] = {}
    errors: list[str] = []
    cleanup_owner = _conversation_cleanup_owner(session, owner)

    ui_tasks = getattr(session, "_ui_agent_state_tasks", {})
    ui_task = ui_tasks.pop(owner, None) if isinstance(ui_tasks, dict) else None
    if ui_task is not None and not ui_task.done():
        ui_task.cancel()
        drained = await await_with_deadline(
            asyncio.gather(ui_task, return_exceptions=True),
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"UI agent state task for conversation {owner}",
            owner=cleanup_owner,
        )
        if not drained:
            errors.append("ui_agent_state_task")
    for attr in ("_ui_agent_state_pending", "_ui_agent_state_cache", "_conversation_streams"):
        mapping = getattr(session, attr, None)
        if isinstance(mapping, dict):
            mapping.pop(owner, None)
    interrupted = getattr(session, "_interrupted_conversation_ids", None)
    if isinstance(interrupted, set):
        interrupted.discard(owner)
    session.run_manager.forget_conversation(owner)

    from backend.agent.checkpoint import clear_checkpoints_for_conversation
    from backend.agent.runtime import (
        default_runtime_if_initialized,
        purge_persisted_conversation_runtime,
    )

    # These stores are independent and can contain hundreds of historical
    # session files. Run their bounded, lock-protected I/O concurrently off the
    # websocket event loop so one hard delete cannot freeze every renderer.
    async def clear_legacy_run_checkpoints() -> None:
        # Historical checkpoints are partitioned by websocket session, so a
        # hard delete may need to inspect hundreds of tiny directories. The
        # run fence above guarantees there are no remaining writers; finish
        # this secondary cleanup in a session-owned worker instead of making
        # the renderer wait on a legacy global scan.
        inner = asyncio.create_task(
            asyncio.to_thread(clear_checkpoints_for_conversation, owner)
        )
        try:
            removed = await asyncio.shield(inner)
        except asyncio.CancelledError:
            while not inner.done():
                try:
                    await asyncio.shield(inner)
                except asyncio.CancelledError:
                    continue
            with suppress(Exception):
                inner.result()
            raise
        except Exception as exc:
            logger.warning(
                "Failed to purge run_checkpoints for %s: %s",
                owner,
                exc,
            )
            if session.is_connected:
                await session.emit_command_result(
                    "conversation.delete",
                    "Conversation deleted, but legacy run checkpoints could not be removed.",
                    level="warning",
                    data={
                        "conversation_id": owner,
                        "cleanup_errors": ["run_checkpoints"],
                    },
                )
            return
        logger.info(
            "Deferred run checkpoint cleanup removed %d record(s) for %s",
            int(removed or 0),
            owner,
        )

    checkpoint_cleanup_task = asyncio.create_task(
        clear_legacy_run_checkpoints(),
        name=f"conversation-checkpoint-cleanup:{owner}",
    )
    # The immediate result reports the state at command completion. The
    # historical cross-session scan is deliberately deferred, so zero records
    # have been synchronously removed and one cleanup worker remains pending.
    counts["run_checkpoints"] = 0
    counts["run_checkpoints_pending"] = 1
    if cleanup_owner is not None:
        retain_cleanup_task(checkpoint_cleanup_task, cleanup_owner)
    else:
        session.command_dispatcher.track_command_task(checkpoint_cleanup_task)

    cleanup_actions: list[tuple[str, Any]] = [
        ("file_checkpoints", session.checkpoint_manager.delete_for_conversation),
        ("attachments", session.attachment_store.delete_for_conversation),
        ("artifacts", session.artifact_store.delete_for_conversation),
        ("forks", session.fork_registry.delete_for_conversation_across_sessions),
    ]
    runtime = default_runtime_if_initialized()
    if runtime is None:
        cleanup_actions.append(("agent_runtime", purge_persisted_conversation_runtime))

    cleanup_task = asyncio.ensure_future(asyncio.gather(
        *(asyncio.to_thread(action, owner) for _, action in cleanup_actions),
        return_exceptions=True,
    ))
    cleanup_completed = await await_with_deadline(
        cleanup_task,
        timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
        label=f"secondary cleanup for conversation {owner}",
        owner=cleanup_owner,
    )
    cleanup_results = (
        list(cleanup_task.result())
        if cleanup_completed
        else []
    )
    if not cleanup_completed:
        errors.append("secondary_cleanup")
    runtime_records: dict[str, Any] | None = None
    for (name, _), result in zip(cleanup_actions, cleanup_results):
        if isinstance(result, BaseException):
            logger.warning("Failed to purge %s for %s: %s", name, owner, result)
            errors.append(name)
            continue
        if name == "agent_runtime":
            runtime_records = dict(result or {})
        else:
            counts[name] = int(result or 0)

    try:
        counts["diagnostics"] = int(
            session.diagnostic_store.delete_for_conversation(owner)
        )
    except Exception as exc:
        logger.warning("Failed to purge diagnostics for %s: %s", owner, exc)
        errors.append("diagnostics")

    if runtime is not None:
        try:
            runtime_records = runtime.purge_conversation(owner)
        except Exception as exc:
            logger.warning("Failed to purge agent runtime for %s: %s", owner, exc)
            errors.append("agent_runtime")
    if runtime_records is not None:
        counts["agent_runs"] = len(runtime_records.get("run_ids", []))
        counts["subagents"] = len(runtime_records.get("subagent_ids", []))
        counts["swarm_tasks"] = len(runtime_records.get("task_ids", []))
        counts["swarm_messages"] = len(runtime_records.get("message_ids", []))
        counts["swarm_teams"] = len(runtime_records.get("team_ids", []))
    return counts, errors


async def _purge_conversation_replay_state(
    session: "WebSocketSession",
    conversation_id: str,
) -> tuple[dict[str, int], list[str]]:
    """Remove hard-deleted conversation events from live and dormant sessions."""

    from backend.ws.event_log import delete_replay_events_for_conversation

    owner = str(conversation_id or "").strip()
    counts: dict[str, int] = {}
    errors: list[str] = []
    cleanup_owner = _conversation_cleanup_owner(session, owner)
    live_sessions = _all_live_sessions(session)
    live_paths: set[Path] = set()
    for owner_session in live_sessions:
        live_paths.add(owner_session.event_outbox.replay_path)
        try:
            replay_task = asyncio.create_task(
                owner_session.event_outbox.delete_conversation_events(owner)
            )
            completed = await await_with_deadline(
                replay_task,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"replay events for conversation {owner} in session {owner_session.session_id}",
                owner=_conversation_cleanup_owner(owner_session, owner) or cleanup_owner,
            )
            if not completed:
                errors.append(f"{owner_session.session_id}:replay_events")
                continue
            removed = int(replay_task.result())
            if removed:
                counts["replay_events"] = counts.get("replay_events", 0) + removed
        except Exception:
            logger.exception(
                "Failed to purge replay events for conversation %s in session %s",
                owner,
                owner_session.session_id,
            )
            errors.append(f"{owner_session.session_id}:replay_events")

    replay_root = session.event_outbox.replay_root
    try:
        excluded_paths = {path.resolve(strict=False) for path in live_paths}
        has_dormant_logs = replay_root.exists() and any(
            path.resolve(strict=False) not in excluded_paths
            for path in replay_root.glob("*.jsonl")
        )
        removed = 0
        if has_dormant_logs:
            dormant_task = asyncio.create_task(asyncio.to_thread(
                delete_replay_events_for_conversation,
                replay_root,
                owner,
                exclude_paths=live_paths,
            ))
            completed = await await_with_deadline(
                dormant_task,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"dormant replay events for conversation {owner}",
                owner=cleanup_owner,
            )
            if completed:
                removed = int(dormant_task.result())
            else:
                errors.append("dormant:replay_events")
        if removed:
            counts["dormant_replay_events"] = removed
    except Exception:
        logger.exception("Failed to purge dormant replay events for conversation %s", owner)
        errors.append("dormant:replay_events")

    # A renderer can reconnect while dormant files are scanned. A second live
    # pass catches any session object materialized during that await.
    known_session_ids = {id(item) for item in live_sessions}
    for owner_session in _all_live_sessions(session):
        if id(owner_session) in known_session_ids:
            continue
        try:
            replay_task = asyncio.create_task(
                owner_session.event_outbox.delete_conversation_events(owner)
            )
            completed = await await_with_deadline(
                replay_task,
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"reconnected replay events for conversation {owner} in session {owner_session.session_id}",
                owner=_conversation_cleanup_owner(owner_session, owner) or cleanup_owner,
            )
            if not completed:
                errors.append(f"{owner_session.session_id}:replay_events")
                continue
            counts["replay_events"] = counts.get("replay_events", 0) + int(
                replay_task.result()
            )
        except Exception:
            logger.exception(
                "Failed to purge replay events after reconnect for conversation %s in session %s",
                owner,
                owner_session.session_id,
            )
            errors.append(f"{owner_session.session_id}:replay_events")
    return counts, errors


async def handle_conversation_create(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_conversation_switched_payload,
        parse_conversation_create_request,
    )

    request = parse_conversation_create_request(data)
    if request.workspace_required_error is not None:
        from backend.ws.command_results import emit_command_error
        await emit_command_error(
            session,
            "conversation.create",
            request.workspace_required_error,
            data={"conversation_id": request.conversation_id or ""},
        )
        return True
    created = session.conversation_repo.create_conversation(
        conversation_id=request.conversation_id,
        title=request.title,
        conversation_type=request.conversation_type,
        memory_mode=request.memory_mode,
        permission_mode=request.permission_mode,
        summary="",
        context_snapshot={},
        workspace_root=request.workspace_root,
        git_isolated=request.git_isolated,
    )
    if request.git_isolated:
        created = await session.create_isolated_conversation_worktree(created) or created
    elif request.workspace_root:
        created = session.conversation_repo.update_workspace_binding(
            created.id,
            workspace_root=request.workspace_root,
            git_branch=session.git_branch_for(Path(request.workspace_root)),
            worktree_path="",
            git_isolated=False,
        ) or created
    if request.activate:
        session.active_conversation_id = created.id
    if request.activate:
        if request.workspace_root:
            await session.switch_workspace_for_conversation(created, announce=False)
        else:
            session.session_lifecycle.clear_workspace_runtime()
    is_hydrating = False
    if request.activate:
        is_hydrating = bool(
            session.load_active_conversation_snapshot(
                created.id,
                created.context_snapshot,
                notify=True,
                defer_start=True,
            )
        )
        session.sync_permission_mode_with_active_conversation(source="conversation.create")
    if request.activate:
        # Activation is the causal event; inventory is only the subsequent
        # snapshot. Publish the authoritative switch first so a renderer can
        # never answer the list snapshot by switching the backend back to the
        # conversation that was active before creation.
        await session.send_payload(
            build_conversation_switched_payload(
                created,
                is_hydrating=is_hydrating,
                runtime_snapshot=session.runtime_snapshot(),
            ),
            log_context="conversation.switched",
        )
        if is_hydrating:
            session.start_active_conversation_hydration(created.id)
    projection_errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(
        "conversation.create",
        (
            "Conversation created, but one or more windows need to resynchronize."
            if projection_errors
            else "Conversation created."
        ),
        level="warning" if projection_errors else "success",
        data={
            "conversation_id": created.id,
            "conversation_type": created.conversation_type,
            "revision": int(getattr(created, "revision", 0) or 0),
            "created": True,
            "activated": request.activate,
            "projection_errors": projection_errors,
        },
    )
    return True


async def handle_conversation_clone(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Clone a persisted session without sharing mutable state or worktree ownership."""
    source_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    source = session.conversation_repo.get_conversation(source_id) if source_id else None
    if source is None:
        await emit_conversation_not_found(session, source_id)
        return True
    if _conversation_has_active_run(session, source.id):
        await session.emit_command_result(
            "conversation.clone",
            "Cannot clone a conversation while its agent run is active.",
            level="warning",
            data={"conversation_id": source.id, "reason": "run_active"},
        )
        return True
    title = str(data.get("title") or "").strip() or None
    clone = session.conversation_repo.clone_conversation(source.id, title=title)
    if clone is None:
        await emit_conversation_not_found(session, source.id)
        return True
    clone_workspace_root = str(clone.worktree_path or clone.workspace_root or "")
    try:
        session.attachment_store.share_for_conversation(
            source.id,
            clone.id,
            clone_workspace_root,
        )
        session.artifact_store.share_for_conversation(
            source.id,
            clone.id,
            clone_workspace_root,
        )
        session.diagnostic_store.share_for_conversation(source.id, clone.id)
    except Exception as exc:
        with suppress(Exception):
            session.attachment_store.delete_for_conversation(clone.id)
        with suppress(Exception):
            session.artifact_store.delete_for_conversation(clone.id)
        with suppress(Exception):
            session.diagnostic_store.delete_for_conversation(clone.id)
        session.conversation_repo.delete_conversation(clone.id)
        await session.emit_command_result(
            "conversation.clone",
            f"The conversation copy could not retain its attachments: {exc}",
            level="error",
            data={"conversation_id": source.id, "reason": "attachment_clone_failed"},
        )
        return True
    if bool(data.get("activate")):
        session.active_conversation_id = clone.id
        await session.switch_workspace_for_conversation(clone, announce=False)
        session.load_active_conversation_snapshot(clone.id, clone.context_snapshot)
        session.sync_permission_mode_with_active_conversation(source="conversation.clone")
    projection_errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(
        "conversation.clone",
        (
            f"Cloned conversation as {clone.title}, but one or more windows need to resynchronize."
            if projection_errors
            else f"Cloned conversation as {clone.title}."
        ),
        level="warning" if projection_errors else "success",
        data={
            "conversation_id": clone.id,
            "source_conversation_id": source.id,
            "branch_kind": clone.branch_kind,
            "activated": bool(data.get("activate")),
            "projection_errors": projection_errors,
        },
    )
    return True


async def handle_conversation_merge(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Fast-forward a branch into its direct parent, with explicit conflicts."""
    source_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    source = session.conversation_repo.get_conversation(source_id) if source_id else None
    if source is None:
        await emit_conversation_not_found(session, source_id)
        return True
    target_id = str(data.get("target_conversation_id") or source.parent_conversation_id or "").strip()
    target = session.conversation_repo.get_conversation(target_id) if target_id else None
    if target is None:
        await emit_conversation_not_found(session, target_id)
        return True
    if _conversation_has_active_run(session, source.id) or _conversation_has_active_run(session, target.id):
        await session.emit_command_result(
            "conversation.merge",
            "Stop both agent runs before merging sessions.",
            level="warning",
            data={"conversation_id": source.id, "target_conversation_id": target.id, "reason": "run_active"},
        )
        return True
    _, updated_target, status = session.conversation_repo.merge_conversation_fast_forward(source.id, target.id)
    attachment_merge_error = ""
    if status in {"merged", "already_up_to_date"}:
        target_workspace_root = str(target.worktree_path or target.workspace_root or "")
        try:
            session.attachment_store.share_for_conversation(
                source.id,
                target.id,
                target_workspace_root,
            )
            session.artifact_store.share_for_conversation(
                source.id,
                target.id,
                target_workspace_root,
            )
            session.diagnostic_store.share_for_conversation(source.id, target.id)
        except Exception as exc:
            attachment_merge_error = str(exc)
    messages = {
        "merged": "Branch merged into its parent.",
        "already_up_to_date": "The parent already contains this branch.",
        "target_diverged": "Merge stopped: the parent changed after the branch was created.",
        "source_is_not_direct_child": "Merge stopped: only a direct child can be merged into its parent.",
        "already_merged_elsewhere": "Merge stopped: this branch was already merged elsewhere.",
        "archived_conversation": "Merge stopped: archived sessions cannot be merged.",
        "same_conversation": "A session cannot be merged into itself.",
        "conversation_not_found": "Merge stopped: a session no longer exists.",
    }
    level = "success" if status in {"merged", "already_up_to_date"} and not attachment_merge_error else "warning"
    active_target_merged = status == "merged" and updated_target is not None and session.active_conversation_id == updated_target.id
    if active_target_merged and updated_target is not None:
        from backend.services.conversation_payload_service import build_conversation_switched_payload

        is_hydrating = session.load_active_conversation_snapshot(
            updated_target.id,
            updated_target.context_snapshot,
            notify=True,
            defer_start=True,
        )
        await session.send_payload(
            build_conversation_switched_payload(
                updated_target,
                is_hydrating=is_hydrating,
                runtime_snapshot=session.runtime_snapshot(),
            ),
            log_context="conversation.switched",
        )
        if is_hydrating:
            session.start_active_conversation_hydration(updated_target.id)
    projection_errors: list[str] = []
    if status in {"merged", "already_up_to_date"}:
        projection_errors = await _broadcast_conversation_lists(session)
    merge_message = messages.get(status, f"Merge stopped: {status}")
    if attachment_merge_error:
        merge_message = (
            f"{merge_message} Attachment ownership still needs recovery: {attachment_merge_error}"
        )
    if projection_errors:
        merge_message = f"{merge_message} One or more windows need to resynchronize."
    await session.emit_command_result(
        "conversation.merge",
        merge_message,
        level="warning" if projection_errors and level == "success" else level,
        data={
            "conversation_id": source.id,
            "target_conversation_id": target.id,
            "status": status,
            "projection_errors": projection_errors,
            **({"attachment_error": attachment_merge_error} if attachment_merge_error else {}),
        },
    )
    return True


async def handle_conversation_export(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    source_id = str(data.get("conversation_id") or session.active_conversation_id or "").strip()
    record = session.conversation_repo.get_conversation(source_id) if source_id else None
    if record is None:
        await emit_conversation_not_found(session, source_id)
        return True
    include_descendants = bool(data.get("include_descendants", True))
    payload = session.conversation_repo.export_conversation_tree(
        source_id,
        include_descendants=include_descendants,
    )
    if payload is None:
        await emit_conversation_not_found(session, source_id)
        return True
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    # Keep a single export from monopolizing the WebSocket replay buffer.
    if len(content.encode("utf-8")) > 25 * 1024 * 1024:
        await session.emit_command_result(
            "conversation.export",
            "Export is larger than 25 MiB; narrow the tree and try again.",
            level="warning",
            data={"conversation_id": source_id, "reason": "export_too_large"},
        )
        return True
    safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in source_id)
    await session.emit_command_result(
        "conversation.export",
        "Conversation export is ready to download.",
        level="success",
        data={
            "conversation_id": source_id,
            "filename": f"minicode-{safe_id}.json",
            "mime_type": "application/json;charset=utf-8",
            "content": content,
            "conversation_count": len(payload.get("conversations") or []),
        },
    )
    return True


async def handle_conversation_switch(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import build_conversation_switched_payload

    conversation_id = str(data.get("conversation_id", ""))
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    if getattr(target, "conversation_type", "main") != "main":
        await session.emit_command_result(
            "conversation.switch",
            "Side chats cannot become the active main conversation.",
            level="warning",
            data={"conversation_id": conversation_id, "reason": "side_chat"},
        )
        return True
    if getattr(target, "archived", False):
        await session.send_conversation_list()
        await session.emit_command_result(
            "conversation.switch",
            "Restore this conversation from the archive before switching to it.",
            level="warning",
            data={"conversation_id": conversation_id, "reason": "archived"},
        )
        return True
    target = await session.reconcile_persisted_ui_agent_state(
        target.id,
        conversation=target,
    ) or target
    session.active_conversation_id = target.id
    await session.switch_workspace_for_conversation(target, announce=True)
    is_hydrating = session.load_active_conversation_snapshot(
        target.id,
        target.context_snapshot,
        notify=True,
        defer_start=True,
    )
    session.sync_permission_mode_with_active_conversation(source="conversation.switch")
    logger.info(
        "[handle_conversation_switch] Switch to conv: %s, transcript len: %d, snapshot keys: %s",
        target.id,
        len(target.transcript) if target.transcript else 0,
        list(target.context_snapshot.keys()) if target.context_snapshot else [],
    )
    await session.send_payload(
        build_conversation_switched_payload(
            target,
            is_hydrating=is_hydrating,
            runtime_snapshot=session.runtime_snapshot(),
        ),
        log_context="conversation.switched",
    )
    if is_hydrating:
        session.start_active_conversation_hydration(target.id)
    if data.get("_reemit_pending", True):
        await session.reemit_pending_state(conversation_id=target.id)
    return True


def _clear_context_builder(session: "WebSocketSession", *, reason: str) -> None:
    """Clear the reusable prompt context or fail the lifecycle transition.

    ContextBuilder is reused between conversations.  Treating a failed clear
    as success lets the next conversation inherit the previous transcript and
    tool-result state, which is worse than surfacing the transition failure.
    """

    clear = getattr(getattr(session, "context_builder", None), "clear", None)
    if not callable(clear):
        error = RuntimeError("The session context builder does not expose clear()")
        logger.error("Cannot clear session context during %s", reason)
        raise error
    try:
        clear()
    except Exception as exc:
        logger.exception("Failed to clear session context during %s", reason)
        raise RuntimeError(f"Failed to clear session context during {reason}") from exc


def _clear_active_conversation_runtime(session: "WebSocketSession") -> None:
    _clear_context_builder(session, reason="active conversation reset")
    try:
        session.session_lifecycle.clear_workspace_runtime()
    except Exception as exc:
        logger.exception("Failed to clear workspace runtime during active conversation reset")
        raise RuntimeError(
            "Failed to clear workspace runtime during active conversation reset"
        ) from exc
    session.active_conversation_id = None


async def _activate_conversation_or_blank(
    session: "WebSocketSession",
    preferred_id: str | None = None,
    *,
    reconcile_agent_state: bool = True,
) -> None:
    from backend.services.conversation_payload_service import choose_conversation_activation_target

    target = choose_conversation_activation_target(session.conversation_repo, preferred_id)
    if target is None:
        _clear_active_conversation_runtime(session)
        return

    if reconcile_agent_state:
        target = await session.reconcile_persisted_ui_agent_state(
            target.id,
            conversation=target,
        ) or target
    session.active_conversation_id = target.id
    await session.switch_workspace_for_conversation(target, announce=True)
    session.load_active_conversation_snapshot(target.id, target.context_snapshot)
    session.sync_permission_mode_with_active_conversation(source="conversation.activate")


async def handle_conversation_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    preferred = str(data.get("preferred_conversation_id") or "").strip()
    if preferred:
        await _activate_conversation_or_blank(session, preferred)
    elif session.active_conversation_id:
        active = session.conversation_repo.get_conversation(session.active_conversation_id)
        if active is None or getattr(active, "archived", False):
            await _activate_conversation_or_blank(session)
    await session.send_conversation_list()
    return True


async def handle_conversation_rename(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import parse_conversation_rename_request

    request = parse_conversation_rename_request(data)
    updated = session.conversation_repo.rename_conversation(request.conversation_id, request.title)
    if updated is None:
        await emit_conversation_not_found(session, request.conversation_id)
        from backend.ws.command_results import emit_command_error

        await emit_command_error(
            session,
            "conversation.rename",
            f"Conversation '{request.conversation_id}' not found",
            data={"conversation_id": request.conversation_id},
        )
        return True
    projection_errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(
        "conversation.rename",
        (
            "Conversation renamed, but one or more windows need to resynchronize."
            if projection_errors
            else "Conversation renamed."
        ),
        level="warning" if projection_errors else "success",
        data={
            "conversation_id": updated.id,
            "title": updated.title,
            "revision": int(getattr(updated, "revision", 0) or 0),
            "projection_errors": projection_errors,
        },
    )
    return True


async def handle_conversation_archive(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", "")).strip()
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        from backend.ws.command_results import emit_command_error

        await emit_command_error(
            session,
            "conversation.archive",
            f"Conversation '{conversation_id}' not found",
            data={"conversation_id": conversation_id},
        )
        return True

    resource_counts = _conversation_activity_blockers(session, conversation_id)
    blocking_resources = {
        name: count for name, count in resource_counts.items() if count > 0
    }
    if blocking_resources:
        await session.emit_command_result(
            "conversation.archive",
            "Stop or remove the conversation's live terminals, previews, background commands, and scheduled tasks before archiving it.",
            level="error",
            data={
                "conversation_id": conversation_id,
                "reason": "runtime_resource_active",
                "resources": blocking_resources,
                "retryable": True,
            },
        )
        return True

    mutation_claim = _try_claim_conversation_mutation(
        session,
        conversation_id,
        operation="archive",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "conversation.archive",
            "Stop the running task before archiving this conversation.",
            level="error",
            data={
                "conversation_id": conversation_id,
                "reason": "run_active",
                "retryable": True,
            },
        )
        return True
    try:
        updated = session.conversation_repo.set_archived(conversation_id, True)
        if updated is None:
            await emit_conversation_not_found(session, conversation_id)
            from backend.ws.command_results import emit_command_error

            await emit_command_error(
                session,
                "conversation.archive",
                f"Conversation '{conversation_id}' not found",
                data={"conversation_id": conversation_id},
            )
            return True
        fallback_errors: list[str] = []
        for owner_session in _all_live_sessions(session):
            if owner_session.active_conversation_id != conversation_id:
                continue
            try:
                await _activate_conversation_or_blank(owner_session)
            except Exception:
                logger.exception(
                    "Failed to activate a fallback after archiving %s for session %s",
                    conversation_id,
                    owner_session.session_id,
                )
                fallback_errors.append(str(owner_session.session_id))
        _schedule_long_term_memory_forgetting(session, updated)
        broadcast_errors = await _broadcast_conversation_lists(session)
        projection_errors = list(dict.fromkeys([*fallback_errors, *broadcast_errors]))
        await session.emit_command_result(
            "conversation.archive",
            (
                "Conversation archived, but one or more windows need to resynchronize."
                if projection_errors
                else "Conversation archived."
            ),
            level="warning" if projection_errors else "success",
            data={
                "conversation_id": conversation_id,
                "archived": True,
                "revision": int(getattr(updated, "revision", 0) or 0),
                "projection_errors": projection_errors,
            },
        )
        return True
    finally:
        _release_conversation_mutation(mutation_claim)


async def handle_conversation_unarchive(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", ""))
    updated = session.conversation_repo.set_archived(conversation_id, False)
    if updated is None:
        await emit_conversation_not_found(session, conversation_id)
        from backend.ws.command_results import emit_command_error
        await emit_command_error(
            session,
            "conversation.unarchive",
            f"Conversation '{conversation_id}' not found",
            data={"conversation_id": conversation_id},
        )
        return True
    projection_errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(
        "conversation.unarchive",
        (
            "Conversation restored from the archive, but one or more windows need to resynchronize."
            if projection_errors
            else "Conversation restored from the archive."
        ),
        level="warning" if projection_errors else "success",
        data={
            "conversation_id": conversation_id,
            "archived": False,
            "revision": int(getattr(updated, "revision", 0) or 0),
            "projection_errors": projection_errors,
        },
    )
    return True


async def handle_conversation_delete(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import parse_conversation_delete_request

    request = parse_conversation_delete_request(data)
    manager = session.ws_manager
    delete_token: str | None = None
    if manager is not None:
        delete_token, reason, upload_count = manager.begin_conversation_delete(
            request.conversation_id
        )
        if not delete_token:
            messages = {
                "attachment_upload_active": (
                    "Wait for the active attachment upload to finish before deleting this conversation."
                ),
                "delete_in_progress": "This conversation is already being deleted.",
                "invalid_conversation": "A conversation id is required for deletion.",
            }
            await session.emit_command_result(
                "conversation.delete",
                messages.get(reason, "The conversation cannot be deleted right now."),
                level="error",
                data={
                    "conversation_id": request.conversation_id,
                    "reason": reason,
                    "active_uploads": int(upload_count or 0),
                    "retryable": reason in {"attachment_upload_active", "delete_in_progress"},
                },
            )
            return True
    # The inventory mutation is intentionally detached from the websocket
    # command loop. Worktree snapshots, scheduler/terminal teardown and replay
    # purging can be slow; holding the command loop open made unrelated
    # conversation switches feel frozen. The per-conversation delete fence
    # remains held until the detached task finishes, so duplicate deletes still
    # fail fast and cleanup remains idempotent.
    task = asyncio.create_task(
        _run_conversation_delete_background(
            session,
            data,
            conversation_id=request.conversation_id,
            delete_token=delete_token,
        ),
        name=f"conversation-delete:{request.conversation_id}",
    )
    task.add_done_callback(_conversation_delete_task_done)
    # The delete is process-owned, not websocket-owned. The initiating
    # renderer may disconnect while tombstone, replay, and worktree cleanup
    # are still in flight.
    if manager is not None:
        manager.track_conversation_delete_task(task)
    else:
            session.command_dispatcher.track_command_task(task)
    return True


async def _run_conversation_delete_background(
    session: "WebSocketSession",
    data: dict[str, Any],
    *,
    conversation_id: str,
    delete_token: str | None,
) -> None:
    try:
        await _handle_conversation_delete_fenced(session, data)
    except asyncio.CancelledError:
        logger.info("Conversation delete task cancelled for %s", conversation_id)
        raise
    except Exception as exc:
        logger.exception("Conversation delete failed for %s", conversation_id)
        if session.is_connected:
            await session.emit_command_result(
                "conversation.delete",
                "会话删除失败，原会话已保留，可稍后重试。",
                level="error",
                data={
                    "conversation_id": conversation_id,
                    "reason": "delete_exception",
                    "retryable": True,
                    "error": str(exc),
                },
            )
    finally:
        manager = session.ws_manager
        if delete_token and manager is not None:
            manager.finish_conversation_delete(conversation_id, delete_token)


def _conversation_delete_task_done(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    with suppress(Exception):
        task.result()


async def _handle_conversation_delete_fenced(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_worktree_cleanup_outcome,
        build_worktree_cleanup_force_required_outcome,
        parse_conversation_delete_request,
    )

    request = parse_conversation_delete_request(data)
    target = session.conversation_repo.get_conversation(request.conversation_id)
    if target is None:
        await emit_conversation_not_found(session, request.conversation_id)
        from backend.ws.command_results import emit_command_error
        await emit_command_error(
            session,
            "conversation.delete",
            f"Conversation '{request.conversation_id}' not found",
            data={"conversation_id": request.conversation_id},
        )
        return True

    owner_sessions = _all_live_sessions(session)
    stop_results = []
    for owner_session in owner_sessions:
        try:
            stop_results.append(await _stop_conversation_run(
                owner_session,
                request.conversation_id,
                reason="conversation_deleted",
            ))
        except Exception:
            logger.exception(
                "Failed to stop conversation %s in session %s",
                request.conversation_id,
                owner_session.session_id,
            )
            stop_results.append(False)
    if not all(stop_results):
        await session.emit_command_result(
            "conversation.delete",
            "The agent run did not stop within the lifecycle deadline; the conversation and worktree were kept intact.",
            level="error",
            data={"conversation_id": request.conversation_id, "reason": "run_still_active"},
        )
        return True

    mutation_claim = _try_claim_conversation_mutation(
        session,
        request.conversation_id,
        operation="delete",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "conversation.delete",
            "Another REST, scheduled, or websocket run started before the delete fence was acquired; the conversation and worktree were kept intact.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "run_active",
                "retryable": True,
            },
        )
        return True
    try:
        return await _handle_conversation_delete_after_run_fence(
            session,
            request=request,
            target=target,
            owner_sessions=owner_sessions,
        )
    finally:
        _release_conversation_mutation(mutation_claim)


async def _handle_conversation_delete_after_run_fence(
    session: "WebSocketSession",
    *,
    request: Any,
    target: Any,
    owner_sessions: list["WebSocketSession"],
) -> bool:
    from backend.services.conversation_payload_service import (
        build_worktree_cleanup_outcome,
        build_worktree_cleanup_force_required_outcome,
    )

    # Release resources that can still hold or write into the workspace before
    # taking the recoverable worktree snapshot and removing the checkout.
    from backend.tasks.scheduler import get_global_scheduler

    cleanup_owner = _conversation_cleanup_owner(session, request.conversation_id)

    scheduler_stopped = await await_with_deadline(
        get_global_scheduler().destroy_for_conversation(request.conversation_id),
        timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
        label=f"scheduled work for conversation {request.conversation_id}",
        owner=cleanup_owner,
    )
    if not scheduler_stopped:
        await session.emit_command_result(
            "conversation.delete",
            "Scheduled work could not be stopped within the cleanup deadline; the conversation and worktree were kept intact.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "scheduled_run_active",
                "retryable": True,
            },
        )
        return True
    background_stopped = await await_with_deadline(
        asyncio.gather(*(
            owner_session.background_manager.destroy_for_conversation(request.conversation_id)
            for owner_session in owner_sessions
        )),
        timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
        label=f"background commands for conversation {request.conversation_id}",
        owner=cleanup_owner,
    )
    if not background_stopped:
        await session.emit_command_result(
            "conversation.delete",
            "Background commands could not be stopped within the cleanup deadline; the conversation and worktree were kept intact.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "background_command_active",
                "retryable": True,
            },
        )
        return True
    from backend.preview.launcher import stop_preview_launches_for_conversation

    workspace_resources_stopped = await await_with_deadline(
        asyncio.gather(
            *(
                owner_session.terminal_manager.destroy_sessions_for_conversation(request.conversation_id)
                for owner_session in owner_sessions
            ),
            stop_preview_launches_for_conversation(request.conversation_id),
        ),
        timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
        label=f"terminal and preview resources for conversation {request.conversation_id}",
        owner=cleanup_owner,
    )
    if not workspace_resources_stopped:
        await session.emit_command_result(
            "conversation.delete",
            "Terminal or preview resources could not be stopped within the cleanup deadline; the conversation and worktree were kept intact.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "workspace_resource_active",
                "retryable": True,
            },
        )
        return True

    released_workspace_sessions: list["WebSocketSession"] = []
    if request.cleanup_worktree:
        for owner_session in owner_sessions:
            if owner_session.active_conversation_id != request.conversation_id:
                continue
            owner_session.session_lifecycle.clear_workspace_runtime()
            released_workspace_sessions.append(owner_session)

    if request.cleanup_worktree:
        cleanup = await _cleanup_conversation_worktree(session, target, force=request.force_cleanup)
        if not cleanup.get("removed"):
            for owner_session in released_workspace_sessions:
                await owner_session.switch_workspace_for_conversation(target, announce=False)
            outcome = (
                build_worktree_cleanup_force_required_outcome(cleanup)
                if cleanup.get("needs_force")
                else build_worktree_cleanup_outcome(cleanup)
            )
            await session.emit_command_result(
                outcome.command,
                outcome.message,
                level=outcome.level,
                data=outcome.data,
            )
            return True
    deleted = session.conversation_repo.delete_conversation(request.conversation_id)
    if not deleted:
        await emit_conversation_not_found(session, request.conversation_id)
        return True
    cleanup_counts, cleanup_errors = await _purge_conversation_replay_state(
        session,
        request.conversation_id,
    )
    _schedule_long_term_memory_forgetting(session, target)
    # A renderer can connect while cancellation, scheduler cleanup, worktree
    # cleanup, or dormant replay scanning is awaiting. It cannot run a
    # lifecycle command until the shared lock releases, but it still needs its
    # retained runtime/cache state purged before the deletion is projected.
    final_owner_sessions = _all_live_sessions(session)
    for owner_session in final_owner_sessions:
        session_counts, session_errors = await _purge_conversation_runtime_state(
            owner_session,
            request.conversation_id,
        )
        for key, value in session_counts.items():
            cleanup_counts[key] = cleanup_counts.get(key, 0) + int(value)
        for error in session_errors:
            scoped_error = f"{owner_session.session_id}:{error}"
            if scoped_error not in cleanup_errors:
                cleanup_errors.append(scoped_error)
        if owner_session.active_conversation_id == request.conversation_id:
            activated = await await_with_deadline(
                _activate_conversation_or_blank(
                    owner_session,
                    reconcile_agent_state=False,
                ),
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"fallback conversation activation for session {owner_session.session_id}",
                owner=_conversation_cleanup_owner(owner_session, request.conversation_id) or cleanup_owner,
            )
            if not activated:
                cleanup_errors.append(f"{owner_session.session_id}:fallback_activation")
    for failed_session_id in await _broadcast_conversation_lists(session):
        scoped_error = f"{failed_session_id}:conversation_list"
        if scoped_error not in cleanup_errors:
            cleanup_errors.append(scoped_error)
    await session.emit_command_result(
        "conversation.delete",
        (
            "Conversation deleted, but some secondary runtime records could not be removed."
            if cleanup_errors
            else "Conversation deleted."
        ),
        level="warning" if cleanup_errors else "success",
        data={
            "conversation_id": request.conversation_id,
            "cleanup": cleanup_counts,
            "cleanup_errors": cleanup_errors,
        },
    )
    return True


async def handle_conversation_worktree_cleanup(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    conversation_id = str(data.get("conversation_id", "")).strip()
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True

    from backend.services.conversation_payload_service import build_worktree_cleanup_outcome

    resource_counts = _conversation_activity_blockers(session, conversation_id)
    blocking_resources = {
        name: int(resource_counts.get(name, 0) or 0)
        for name in ("background_commands", "terminal_sessions", "preview_processes")
        if int(resource_counts.get(name, 0) or 0) > 0
    }
    if blocking_resources:
        await session.emit_command_result(
            "conversation.worktree.cleanup",
            "Stop live background commands, terminals, and previews before removing the protected workspace.",
            level="error",
            data={
                "conversation_id": conversation_id,
                "reason": "runtime_resource_active",
                "resources": blocking_resources,
                "retryable": True,
            },
        )
        return True

    mutation_claim = _try_claim_conversation_mutation(
        session,
        conversation_id,
        operation="worktree_cleanup",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "conversation.worktree.cleanup",
            "A conversation run is using this workspace; stop it before cleanup.",
            level="error",
            data={
                "conversation_id": conversation_id,
                "reason": "run_active",
                "retryable": True,
            },
        )
        return True

    try:
        target = session.conversation_repo.get_conversation(conversation_id)
        if target is None:
            await emit_conversation_not_found(session, conversation_id)
            return True
        # External preview/terminal owners are not required to enter the query
        # registry. Recheck them after taking the process-wide writer fence.
        resource_counts = _conversation_activity_blockers(session, conversation_id)
        blocking_resources = {
            name: int(resource_counts.get(name, 0) or 0)
            for name in ("background_commands", "terminal_sessions", "preview_processes")
            if int(resource_counts.get(name, 0) or 0) > 0
        }
        if blocking_resources:
            await session.emit_command_result(
                "conversation.worktree.cleanup",
                "A workspace resource started before cleanup acquired ownership; stop it and retry.",
                level="error",
                data={
                    "conversation_id": conversation_id,
                    "reason": "runtime_resource_active",
                    "resources": blocking_resources,
                    "retryable": True,
                },
            )
            return True

        await _release_active_sessions_from_conversation_workspace(
            session,
            conversation_id,
        )
        cleanup = await _cleanup_conversation_worktree(
            session,
            target,
            force=bool(data.get("force")),
        )
        if not cleanup.get("removed"):
            cleanup["projection_errors"] = await _switch_active_sessions_to_conversation_workspace(
                session,
                target,
                announce_initiator=False,
                source="conversation.worktree.cleanup.rollback",
            )
            outcome = build_worktree_cleanup_outcome(cleanup)
            await session.emit_command_result(
                outcome.command,
                outcome.message,
                level=outcome.level,
                data=outcome.data,
            )
            return True

        main_workspace_root = str(cleanup.get("workspace_root") or "").strip()
        updated, binding_warning = await _persist_workspace_binding(
            session,
            target.id,
            workspace_root=main_workspace_root,
            git_branch="",
            worktree_path="",
            git_isolated=False,
        )
        if updated is None:
            rollback = await _restore_removed_conversation_worktree(
                session,
                target,
                cleanup,
            )
            projection_errors: list[str] = []
            if rollback["restored"]:
                projection_errors = await _switch_active_sessions_to_conversation_workspace(
                    session,
                    target,
                    announce_initiator=False,
                    source="conversation.worktree.cleanup.rollback_binding",
                )
            rollback_errors = list(rollback["errors"])
            rollback_errors.extend(
                f"project_session:{session_id}" for session_id in projection_errors
            )
            await session.emit_command_result(
                "conversation.worktree.cleanup",
                (
                    "The conversation binding could not be committed; the protected workspace was restored."
                    if not rollback_errors
                    else "The conversation binding failed and automatic workspace recovery needs attention."
                ),
                level="error",
                data={
                    **cleanup,
                    "reason": "workspace_binding_failed",
                    "binding_error": binding_warning,
                    "rollback_completed": not rollback_errors,
                    "rollback_errors": rollback_errors,
                    "restored_path": rollback["path"],
                    "projection_errors": projection_errors,
                    "recovery_required": bool(rollback_errors),
                },
            )
            return True

        if binding_warning:
            cleanup["binding_warning"] = binding_warning
        projection_errors = await _switch_active_sessions_to_conversation_workspace(
            session,
            updated,
            announce_initiator=True,
            source="conversation.worktree.cleanup",
        )
        projection_errors.extend(await _broadcast_conversation_lists(session))
        cleanup.update({
            "revision": int(getattr(updated, "revision", 0) or 0),
            "projection_errors": list(dict.fromkeys(projection_errors)),
        })
        outcome = build_worktree_cleanup_outcome(cleanup)
        await session.emit_command_result(
            outcome.command,
            outcome.message,
            level=("warning" if projection_errors or binding_warning else outcome.level),
            data=outcome.data,
        )
        return True
    finally:
        _release_conversation_mutation(mutation_claim)


async def handle_conversation_worktree_handoff_preflight(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_worktree_handoff_service import build_handoff_preflight

    conversation_id = str(data.get("conversation_id") or "").strip()
    target = session.conversation_repo.get_conversation(conversation_id)
    if target is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    preflight = await asyncio.to_thread(
        build_handoff_preflight,
        target,
        target=str(data.get("target") or ("local" if getattr(target, "git_isolated", False) else "worktree")),
        conversation_repo=session.conversation_repo,
        main_worktree_root=session.main_worktree_root,
        has_running_turn=_conversation_has_active_run(session, conversation_id),
        dirty_action=str(data.get("dirty_action") or "block"),
    )
    preflight = _apply_handoff_runtime_blockers(
        preflight,
        _conversation_activity_blockers(session, conversation_id),
    )
    await session.emit_command_result(
        "conversation.worktree.handoff.preflight",
        "Workspace handoff is ready." if preflight["allowed"] else "Workspace handoff is blocked.",
        level="success" if preflight["allowed"] else "warning",
        data=preflight,
    )
    return True


async def handle_conversation_worktree_handoff_execute(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import create_isolated_worktree_binding
    from backend.services.conversation_worktree_handoff_service import (
        build_handoff_preflight,
        restore_workspace_stash,
        stash_workspace_changes,
        switch_main_checkout,
    )
    from backend.workspace.worktree import WorktreeManager

    conversation_id = str(data.get("conversation_id") or "").strip()
    conversation = session.conversation_repo.get_conversation(conversation_id)
    if conversation is None:
        await emit_conversation_not_found(session, conversation_id)
        return True
    target_kind = str(data.get("target") or ("local" if getattr(conversation, "git_isolated", False) else "worktree"))
    dirty_action = str(data.get("dirty_action") or "block")
    preflight = await asyncio.to_thread(
        build_handoff_preflight,
        conversation,
        target=target_kind,
        conversation_repo=session.conversation_repo,
        main_worktree_root=session.main_worktree_root,
        has_running_turn=_conversation_has_active_run(session, conversation_id),
        dirty_action=dirty_action,
    )
    preflight = _apply_handoff_runtime_blockers(
        preflight,
        _conversation_activity_blockers(session, conversation_id),
    )
    if not preflight["allowed"] or str(data.get("fingerprint") or "") != preflight["fingerprint"]:
        await session.emit_command_result(
            "conversation.worktree.handoff.execute",
            "Workspace changed after preflight; review the checks and try again.",
            level="warning",
            data={**preflight, "stale": True},
        )
        return True

    mutation_claim = _try_claim_conversation_mutation(
        session,
        conversation_id,
        operation="worktree_handoff",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "conversation.worktree.handoff.execute",
            "A conversation run started after preflight; stop it and run preflight again.",
            level="warning",
            data={**preflight, "stale": True, "reason": "run_active"},
        )
        return True
    try:
        return await _handle_conversation_worktree_handoff_claimed(
            session,
            conversation=conversation,
            conversation_id=conversation_id,
            target_kind=target_kind,
            dirty_action=dirty_action,
            preflight=preflight,
        )
    finally:
        _release_conversation_mutation(mutation_claim)


async def _handle_conversation_worktree_handoff_claimed(
    session: "WebSocketSession",
    *,
    conversation: Any,
    conversation_id: str,
    target_kind: str,
    dirty_action: str,
    preflight: dict[str, Any],
) -> bool:
    from backend.services.conversation_payload_service import create_isolated_worktree_binding
    from backend.services.conversation_worktree_handoff_service import (
        delete_local_branch,
        restore_main_checkout,
        restore_workspace_stash,
        stash_workspace_changes,
        switch_main_checkout,
    )
    from backend.workspace.worktree import WorktreeManager

    source_path = Path(str(getattr(conversation, "worktree_path", "") or getattr(conversation, "workspace_root", "") or ".")).resolve()
    stash_ref = ""
    handoff_warnings: list[str] = []
    if dirty_action == "stash":
        stashed, stash_ref = await asyncio.to_thread(
            stash_workspace_changes,
            source_path,
            label=f"minicode-handoff-{conversation_id}",
        )
        if not stashed:
            await session.emit_command_result(
                "conversation.worktree.handoff.execute",
                f"Could not safely stash local changes: {stash_ref}",
                level="error",
                data=preflight,
            )
            return True

    if target_kind == "worktree":
        base_root = session.main_worktree_root(source_path)
        creation = await asyncio.to_thread(
            create_isolated_worktree_binding,
            conversation,
            current_workspace_root=source_path,
            main_worktree_root=session.main_worktree_root,
        )
        if not creation.created:
            await session.emit_command_result("conversation.worktree.handoff.execute", "Failed to create protected workspace.", level="error", data=preflight)
            return True
        updated, binding_warning = await _persist_workspace_binding(
            session,
            conversation_id,
            workspace_root=creation.workspace_root,
            git_branch=creation.git_branch,
            worktree_path=creation.worktree_path,
            git_isolated=True,
        )
        if updated is None:
            rollback_errors: list[str] = []
            manager = WorktreeManager(base_root)
            worktree_path = Path(creation.worktree_path or creation.workspace_root).resolve()
            try:
                removed = bool(await asyncio.to_thread(
                    manager.remove_worktree,
                    worktree_path,
                    force=True,
                ))
            except Exception as exc:
                removed = False
                rollback_errors.append(f"remove_created_worktree:{exc}")
            if not removed and not any(
                error.startswith("remove_created_worktree:") for error in rollback_errors
            ):
                rollback_errors.append("remove_created_worktree:failed")
            if removed:
                branch_deleted, branch_error = await asyncio.to_thread(
                    delete_local_branch,
                    base_root,
                    creation.git_branch,
                )
                if not branch_deleted:
                    rollback_errors.append(f"delete_created_branch:{branch_error}")
            if stash_ref:
                restored, restore_error = await asyncio.to_thread(
                    restore_workspace_stash,
                    source_path,
                    stash_ref,
                )
                if not restored:
                    rollback_errors.append(f"restore_source_stash:{restore_error}")
            await session.emit_command_result(
                "conversation.worktree.handoff.execute",
                (
                    "The workspace binding could not be committed; the original local checkout was restored."
                    if not rollback_errors
                    else "The workspace binding failed and automatic rollback needs manual recovery."
                ),
                level="error",
                data={
                    **preflight,
                    "reason": "workspace_binding_failed",
                    "binding_error": binding_warning,
                    "stash_ref": stash_ref,
                    "rollback_completed": not rollback_errors,
                    "rollback_errors": rollback_errors,
                    "recovery_required": bool(rollback_errors),
                },
            )
            return True
        if binding_warning:
            handoff_warnings.append(
                "The workspace binding committed, but storage reported a recoverable warning."
            )
            preflight = {**preflight, "binding_warning": binding_warning}
        if stash_ref:
            restored, error = await asyncio.to_thread(
                restore_workspace_stash,
                Path(creation.workspace_root),
                stash_ref,
            )
            if not restored:
                handoff_warnings.append(
                    "The new worktree was created, but restoring stashed changes conflicted. "
                    "The stash is retained for recovery."
                )
                preflight = {
                    **preflight,
                    "stash_ref": stash_ref,
                    "stash_error": error,
                    "workspace_root": creation.workspace_root,
                }
    else:
        base_root = session.main_worktree_root(source_path)
        branch = str(getattr(conversation, "git_branch", "") or "").strip()
        main_checkout = dict(preflight.get("main_checkout") or {})
        previous_main_branch = str(main_checkout.get("branch") or "").strip()
        previous_main_head = str(main_checkout.get("head") or "").strip()
        await _release_active_sessions_from_conversation_workspace(
            session,
            conversation_id,
        )
        manager = WorktreeManager(base_root)
        if not await asyncio.to_thread(manager.remove_worktree, source_path, force=False):
            await _switch_active_sessions_to_conversation_workspace(
                session,
                conversation,
                announce_initiator=False,
                source="conversation.worktree.handoff.rollback_remove",
            )
            await session.emit_command_result("conversation.worktree.handoff.execute", "Failed to remove the protected workspace.", level="error", data=preflight)
            return True
        switched, error = await asyncio.to_thread(switch_main_checkout, base_root, branch)
        if not switched:
            # The worktree was already destroyed above, so these two are the
            # user's only recovery handles. Discarding their results left the
            # protected workspace gone and the stash unrestored with nothing in
            # the payload to act on. Mirrors the rollback below.
            checkout_rollback_errors: list[str] = []
            recreated = await asyncio.to_thread(
                manager.create_worktree,
                source_path,
                branch=branch,
                new_branch=False,
            )
            if not recreated:
                checkout_rollback_errors.append("create_worktree:failed")
            if stash_ref:
                restored_stash = await asyncio.to_thread(
                    restore_workspace_stash, source_path, stash_ref
                )
                if not restored_stash:
                    checkout_rollback_errors.append(f"restore_workspace_stash:{stash_ref}")
            await _switch_active_sessions_to_conversation_workspace(
                session,
                conversation,
                announce_initiator=False,
                source="conversation.worktree.handoff.rollback_checkout",
            )
            if checkout_rollback_errors:
                logger.error(
                    "Local handoff rollback incomplete for conversation %s: %s",
                    conversation_id,
                    ", ".join(checkout_rollback_errors),
                )
            await session.emit_command_result(
                "conversation.worktree.handoff.execute",
                f"Failed to switch the local checkout: {error}",
                level="error",
                data={
                    **preflight,
                    **({"stash_ref": stash_ref} if stash_ref else {}),
                    **(
                        {"rollback_errors": checkout_rollback_errors}
                        if checkout_rollback_errors
                        else {}
                    ),
                },
            )
            return True
        updated, binding_warning = await _persist_workspace_binding(
            session,
            conversation_id,
            workspace_root=str(base_root),
            git_branch=branch,
            worktree_path="",
            git_isolated=False,
        )
        if updated is None:
            rollback_errors: list[str] = []
            restored_checkout, checkout_error = await asyncio.to_thread(
                restore_main_checkout,
                base_root,
                branch=previous_main_branch,
                head=previous_main_head,
            )
            if not restored_checkout:
                rollback_errors.append(f"restore_main_checkout:{checkout_error}")
            recreated = False
            if restored_checkout:
                try:
                    recreated = bool(await asyncio.to_thread(
                        manager.create_worktree,
                        source_path,
                        branch=branch,
                        new_branch=False,
                    ))
                except Exception as exc:
                    rollback_errors.append(f"recreate_worktree:{exc}")
                if not recreated and not any(
                    error.startswith("recreate_worktree:") for error in rollback_errors
                ):
                    rollback_errors.append("recreate_worktree:failed")
            if recreated and stash_ref:
                restored, restore_error = await asyncio.to_thread(
                    restore_workspace_stash,
                    source_path,
                    stash_ref,
                )
                if not restored:
                    rollback_errors.append(f"restore_worktree_stash:{restore_error}")
            projection_errors = await _switch_active_sessions_to_conversation_workspace(
                session,
                conversation,
                announce_initiator=False,
                source="conversation.worktree.handoff.rollback_binding",
            )
            rollback_errors.extend(
                f"project_session:{session_id}" for session_id in projection_errors
            )
            await session.emit_command_result(
                "conversation.worktree.handoff.execute",
                (
                    "The workspace binding could not be committed; the protected workspace was restored."
                    if not rollback_errors
                    else "The workspace binding failed and automatic rollback needs manual recovery."
                ),
                level="error",
                data={
                    **preflight,
                    "reason": "workspace_binding_failed",
                    "binding_error": binding_warning,
                    "stash_ref": stash_ref,
                    "rollback_completed": not rollback_errors,
                    "rollback_errors": rollback_errors,
                    "recovery_required": bool(rollback_errors),
                },
            )
            return True
        if binding_warning:
            handoff_warnings.append(
                "The workspace binding committed, but storage reported a recoverable warning."
            )
            preflight = {**preflight, "binding_warning": binding_warning}
        if stash_ref:
            restored, error = await asyncio.to_thread(
                restore_workspace_stash,
                base_root,
                stash_ref,
            )
            if not restored:
                handoff_warnings.append(
                    "Local checkout switched, but restoring stashed changes conflicted. "
                    "The stash is retained for recovery."
                )
                preflight = {
                    **preflight,
                    "stash_ref": stash_ref,
                    "stash_error": error,
                    "workspace_root": str(base_root),
                }

    projection_errors = await _switch_active_sessions_to_conversation_workspace(
        session,
        updated,
        announce_initiator=True,
        source="conversation.worktree.handoff",
    )
    projection_errors.extend(await _broadcast_conversation_lists(session))
    projection_errors = list(dict.fromkeys(projection_errors))
    await session.emit_command_result(
        "conversation.worktree.handoff.execute",
        " ".join(handoff_warnings) or (
            "Moved task to protected workspace."
            if target_kind == "worktree"
            else "Moved task to local checkout."
        ),
        level="warning" if handoff_warnings or projection_errors else "success",
        data={
            **preflight,
            "completed": True,
            "workspace_root": str(getattr(updated, "workspace_root", "") or ""),
            "worktree_path": str(getattr(updated, "worktree_path", "") or ""),
            "git_branch": str(getattr(updated, "git_branch", "") or ""),
            "git_isolated": bool(getattr(updated, "git_isolated", False)),
            "revision": int(getattr(updated, "revision", 0) or 0),
            "projection_errors": projection_errors,
        },
    )
    return True


async def _persist_workspace_binding(
    session: "WebSocketSession",
    conversation_id: str,
    *,
    workspace_root: str,
    git_branch: str,
    worktree_path: str,
    git_isolated: bool,
) -> tuple[Any | None, str]:
    """Commit a workspace binding and detect post-commit filesystem errors."""

    try:
        updated = await asyncio.to_thread(
            session.conversation_repo.update_workspace_binding,
            conversation_id,
            workspace_root=workspace_root,
            git_branch=git_branch,
            worktree_path=worktree_path,
            git_isolated=git_isolated,
        )
    except Exception as exc:
        logger.exception(
            "Failed to persist workspace binding for %s",
            conversation_id,
        )
        current = session.conversation_repo.get_conversation(conversation_id)
        if current is not None and (
            str(getattr(current, "workspace_root", "") or "") == str(workspace_root or "")
            and str(getattr(current, "git_branch", "") or "") == str(git_branch or "")
            and str(getattr(current, "worktree_path", "") or "") == str(worktree_path or "")
            and bool(getattr(current, "git_isolated", False)) is bool(git_isolated)
        ):
            return current, str(exc)
        return None, str(exc)
    if updated is None:
        current = session.conversation_repo.get_conversation(conversation_id)
        if current is not None and (
            str(getattr(current, "workspace_root", "") or "") == str(workspace_root or "")
            and str(getattr(current, "git_branch", "") or "") == str(git_branch or "")
            and str(getattr(current, "worktree_path", "") or "") == str(worktree_path or "")
            and bool(getattr(current, "git_isolated", False)) is bool(git_isolated)
        ):
            return current, "Workspace binding committed without a returned record"
        return None, "Conversation disappeared while the workspace binding was committed"
    return updated, ""


async def _restore_removed_conversation_worktree(
    session: "WebSocketSession",
    conversation: Any,
    cleanup: dict[str, Any],
) -> dict[str, Any]:
    """Compensate a cleanup whose conversation binding did not commit."""

    from backend.workspace.worktree import WorktreeManager

    raw_path = str(
        cleanup.get("path")
        or getattr(conversation, "worktree_path", "")
        or getattr(conversation, "workspace_root", "")
        or ""
    ).strip()
    raw_branch = str(
        cleanup.get("branch")
        or getattr(conversation, "git_branch", "")
        or ""
    ).strip()
    raw_base_root = str(cleanup.get("workspace_root") or "").strip()
    if not raw_path:
        return {
            "restored": False,
            "path": "",
            "errors": ["recreate_worktree:missing_path"],
        }
    worktree_path = Path(raw_path).resolve()
    try:
        base_root = (
            Path(raw_base_root).resolve()
            if raw_base_root
            else session.main_worktree_root(worktree_path)
        )
        manager = WorktreeManager(base_root)
        restored = await asyncio.to_thread(
            manager.restore_removed_worktree,
            worktree_path,
            branch=raw_branch,
            expected_head=str(cleanup.get("head") or ""),
            snapshot_id=str(cleanup.get("snapshot_id") or ""),
        )
    except Exception as exc:
        logger.exception(
            "Failed to recreate worktree for conversation %s after binding failure",
            getattr(conversation, "id", ""),
        )
        return {
            "restored": False,
            "path": str(worktree_path),
            "errors": [f"recreate_worktree:{exc}"],
        }
    if not restored.restored:
        return {
            "restored": False,
            "path": str(restored.path or worktree_path),
            "errors": [f"recreate_worktree:{restored.error or 'failed'}"],
        }
    return {
        "restored": True,
        "path": str(restored.path or worktree_path),
        "errors": [],
    }


async def handle_conversation_clear(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_conversation_clear_outcome,
        build_conversation_switched_payload,
        parse_conversation_clear_request,
    )

    request = parse_conversation_clear_request(data, active_conversation_id=str(session.active_conversation_id or ""))
    if not request.conversation_id:
        # Returning falsy here made the dispatcher answer "Unsupported command
        # 'conversation.clear'" / "/clear is unavailable in this runtime", which
        # misattributes "no active conversation" to a missing capability.
        await session.emit_command_result(
            "clear",
            "There is no active conversation to clear.",
            level="error",
            data={"reason": "no_active_conversation"},
        )
        return True
    target = session.conversation_repo.get_conversation(request.conversation_id)
    if target is None:
        await session.emit_command_result(
            "clear",
            f"Conversation '{request.conversation_id}' no longer exists.",
            level="error",
            data={"conversation_id": request.conversation_id, "reason": "conversation_not_found"},
        )
        return True
    owner_sessions = _all_live_sessions(session)
    stopped = True
    for owner_session in owner_sessions:
        try:
            stopped = (
                await _stop_conversation_run(
                    owner_session,
                    request.conversation_id,
                    reason="conversation_cleared",
                )
                and stopped
            )
        except Exception:
            logger.exception(
                "Failed to stop conversation %s before clear in session %s",
                request.conversation_id,
                owner_session.session_id,
            )
            stopped = False
    if not stopped:
        await session.emit_command_result(
            "clear",
            "The agent run did not stop within the lifecycle deadline; conversation history was not cleared.",
            level="error",
            data={"conversation_id": request.conversation_id, "reason": "run_still_active"},
        )
        return True
    mutation_claim = _try_claim_conversation_mutation(
        session,
        request.conversation_id,
        operation="clear",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "clear",
            "Another REST, scheduled, or websocket run started before the clear fence was acquired; conversation history was not cleared.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "run_active",
                "retryable": True,
            },
        )
        return True

    cleanup_owner = _conversation_cleanup_owner(session, request.conversation_id)
    try:
        background_stopped = await await_with_deadline(
            asyncio.gather(*(
                owner_session.background_manager.destroy_for_conversation(request.conversation_id)
                for owner_session in owner_sessions
            )),
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"background commands before clearing conversation {request.conversation_id}",
            owner=cleanup_owner,
        )
        if not background_stopped:
            await session.emit_command_result(
                "clear",
                "Background commands could not be stopped within the cleanup deadline; conversation history was kept intact.",
                level="error",
                data={
                    "conversation_id": request.conversation_id,
                    "reason": "background_command_active",
                    "retryable": True,
                },
            )
            return True
        from backend.preview.launcher import stop_preview_launches_for_conversation

        workspace_resources_stopped = await await_with_deadline(
            asyncio.gather(
                *(
                    owner_session.terminal_manager.destroy_sessions_for_conversation(request.conversation_id)
                    for owner_session in owner_sessions
                ),
                stop_preview_launches_for_conversation(request.conversation_id),
            ),
            timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
            label=f"terminal and preview resources before clearing conversation {request.conversation_id}",
            owner=cleanup_owner,
        )
        if not workspace_resources_stopped:
            await session.emit_command_result(
                "clear",
                "Terminal or preview resources could not be stopped within the cleanup deadline; conversation history was kept intact.",
                level="error",
                data={
                    "conversation_id": request.conversation_id,
                    "reason": "workspace_resource_active",
                    "retryable": True,
                },
            )
            return True

        previous_snapshot = dict(getattr(target, "context_snapshot", {}) or {})
        retained_plan_snapshot: dict[str, Any] = {}
        if request.preserve_plan:
            from backend.agent.plans import (
                PLAN_FILE_REFERENCE_KEY,
                PLAN_RECOVERY_STATUS_KEY,
                PLAN_SLUG_KEY,
                plan_path_for_snapshot,
                plan_file_reference,
                read_plan,
                write_plan,
            )

            slug = previous_snapshot.get(PLAN_SLUG_KEY)
            if slug:
                retained_plan_snapshot[PLAN_SLUG_KEY] = slug
                path = plan_path_for_snapshot(
                    previous_snapshot,
                    getattr(target, "workspace_root", "") or None,
                )
                if path is not None:
                    plan_content = request.plan_content or read_plan(path) or ""
                    if request.plan_content:
                        write_plan(path, request.plan_content)
                    retained_plan_snapshot[PLAN_FILE_REFERENCE_KEY] = plan_file_reference(
                        path,
                        plan_content or None,
                    )
                    retained_plan_snapshot[PLAN_RECOVERY_STATUS_KEY] = (
                        "available" if plan_content else "missing"
                    )

        updated = session.conversation_repo.clear_conversation(
            request.conversation_id,
            context_snapshot=retained_plan_snapshot,
        )
        if updated is None:
            await emit_conversation_not_found(session, request.conversation_id)
            await session.emit_command_result(
                "clear",
                f"Conversation '{request.conversation_id}' no longer exists.",
                level="error",
                data={"conversation_id": request.conversation_id, "reason": "not_found"},
            )
            return True

        _schedule_long_term_memory_forgetting(session, target)
        cleanup_errors: list[str] = []
        # A renderer may connect while cancellation and resource teardown are
        # awaiting. Re-read the live set before purging and projecting the
        # committed atomic clear.
        final_owner_sessions = _all_live_sessions(session)
        for owner_session in final_owner_sessions:
            _counts, runtime_errors = await _purge_conversation_runtime_state(
                owner_session,
                request.conversation_id,
            )
            cleanup_errors.extend(
                f"{owner_session.session_id}:{error}" for error in runtime_errors
            )
            if request.conversation_id != owner_session.active_conversation_id:
                continue
            _clear_context_builder(owner_session, reason="conversation.clear")
            owner_session.load_active_conversation_snapshot(
                request.conversation_id,
                retained_plan_snapshot,
            )
            current = owner_session.conversation_repo.get_conversation(request.conversation_id)
            if current is None:
                cleanup_errors.append(f"{owner_session.session_id}:reload")
                continue
            await owner_session.send_payload(
                build_conversation_switched_payload(
                    current,
                    is_hydrating=False,
                    runtime_snapshot=owner_session.runtime_snapshot(),
                ),
                log_context="conversation.switched",
            )
            try:
                from backend.ws.handlers.session import emit_session_usage_snapshot

                await emit_session_usage_snapshot(owner_session)
            except Exception:
                logger.exception(
                    "Failed to refresh usage after clearing conversation %s for session %s",
                    request.conversation_id,
                    owner_session.session_id,
                )
        cleanup_errors.extend(
            f"{session_id}:conversation_list"
            for session_id in await _broadcast_conversation_lists(session)
        )

        outcome = build_conversation_clear_outcome()
        outcome_data = {
            **dict(outcome.data or {}),
            "conversation_id": request.conversation_id,
            "revision": int(getattr(updated, "revision", 0) or 0),
            "cleanup_errors": list(dict.fromkeys(cleanup_errors)),
        }
        await session.emit_command_result(
            outcome.command,
            (
                "Conversation cleared, but one or more secondary runtime records need resynchronization."
                if cleanup_errors
                else outcome.message
            ),
            level="warning" if cleanup_errors else outcome.level,
            data=outcome_data,
        )
        return True
    finally:
        _release_conversation_mutation(mutation_claim)


async def handle_conversation_truncate(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import (
        build_conversation_switched_payload,
        build_conversation_truncate_failed_outcome,
        build_conversation_truncated_outcome,
        parse_conversation_truncate_request,
    )

    request = parse_conversation_truncate_request(data, active_conversation_id=str(session.active_conversation_id or ""))
    if request.error is not None:
        outcome = request.error
        await session.emit_command_result(
            outcome.command,
            outcome.message,
            level=outcome.level,
            data=outcome.data,
        )
        return True

    target = session.conversation_repo.get_conversation(request.conversation_id)
    if target is None:
        await emit_conversation_not_found(session, request.conversation_id)
        return True
    mutation_claim = _try_claim_conversation_mutation(
        session,
        request.conversation_id,
        operation="truncate",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "conversation.truncate",
            "Stop the running task before rewinding this conversation.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "run_active",
                "retryable": True,
            },
        )
        return True
    try:
        return await _handle_conversation_truncate_claimed(session, request, target)
    finally:
        _release_conversation_mutation(mutation_claim)


async def _handle_conversation_truncate_claimed(
    session: "WebSocketSession",
    request: Any,
    target: Any,
) -> bool:
    from backend.services.conversation_payload_service import (
        build_conversation_switched_payload,
        build_conversation_truncate_failed_outcome,
        build_conversation_truncated_outcome,
    )

    updated = session.conversation_runtime.rewind_to_user_turn(
        conversation=target,
        retry_from_message_id=request.message_id,
    )
    if updated is None:
        outcome = build_conversation_truncate_failed_outcome(
            conversation_id=request.conversation_id,
            message_id=request.message_id,
        )
        await session.emit_command_result(
            outcome.command,
            outcome.message,
            level=outcome.level,
            data=outcome.data,
        )
        return True

    for owner_session in _all_live_sessions(session):
        if request.conversation_id != owner_session.active_conversation_id:
            continue
        current = owner_session.conversation_repo.get_conversation(request.conversation_id)
        if current is None:
            continue
        owner_session.load_active_conversation_snapshot(current.id, current.context_snapshot)
        owner_session.sync_permission_mode_with_active_conversation(source="conversation.truncate")
        await owner_session.send_payload(
            build_conversation_switched_payload(
                current,
                is_hydrating=False,
                runtime_snapshot=owner_session.runtime_snapshot(),
            ),
            log_context="conversation.switched",
        )
        try:
            from backend.ws.handlers.session import emit_session_usage_snapshot

            await emit_session_usage_snapshot(owner_session)
        except Exception:
            logger.exception(
                "Failed to refresh usage after truncating conversation %s for session %s",
                request.conversation_id,
                owner_session.session_id,
            )

    # Truncation removes transcript content like delete/clear/archive do;
    # forgetting follows the same unconditional scheduling they use.
    _schedule_long_term_memory_forgetting(session, updated)

    projection_errors = await _broadcast_conversation_lists(session)
    outcome = build_conversation_truncated_outcome(updated, message_id=request.message_id)
    await session.emit_command_result(
        outcome.command,
        (
            f"{outcome.message} One or more windows need to resynchronize."
            if projection_errors
            else outcome.message
        ),
        level="warning" if projection_errors else outcome.level,
        data={**dict(outcome.data or {}), "projection_errors": projection_errors},
    )
    return True


async def handle_conversation_memory_mode_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_payload_service import parse_conversation_memory_mode_request

    request = parse_conversation_memory_mode_request(data, active_conversation_id=str(session.active_conversation_id or ""))
    existing = session.conversation_repo.get_conversation(request.conversation_id)
    if existing is None:
        await emit_conversation_not_found(session, request.conversation_id)
        from backend.ws.command_results import emit_command_error
        await emit_command_error(
            session,
            "conversation.memory_mode.set",
            f"Conversation '{request.conversation_id}' not found",
            data={"conversation_id": request.conversation_id},
        )
        return True

    if request.memory_mode not in {"enabled", "disabled"}:
        await session.emit_command_result(
            "conversation.memory_mode.set",
            f"Unsupported memory mode '{request.memory_mode}'.",
            level="error",
            data={
                "conversation_id": request.conversation_id,
                "reason": "invalid_memory_mode",
            },
        )
        return True

    if _conversation_has_active_run(session, request.conversation_id):
        await session.emit_command_result(
            "conversation.memory_mode.set",
            "Stop the active agent run before changing long-term memory eligibility.",
            level="warning",
            data={"conversation_id": request.conversation_id, "reason": "run_active"},
        )
        return True
    mutation_claim = _try_claim_conversation_mutation(
        session,
        request.conversation_id,
        operation="memory_mode_set",
    )
    if mutation_claim is None:
        await session.emit_command_result(
            "conversation.memory_mode.set",
            "A conversation run started before the memory-mode update acquired ownership.",
            level="warning",
            data={
                "conversation_id": request.conversation_id,
                "reason": "run_active",
                "retryable": True,
            },
        )
        return True
    try:
        updated = session.conversation_repo.update_memory_mode(
            request.conversation_id,
            request.memory_mode,
        )
        if updated is None:
            await emit_conversation_not_found(session, request.conversation_id)
            return True
        projection_errors = await _broadcast_conversation_lists(session)
        await session.emit_command_result(
            "conversation.memory_mode.set",
            (
                "Memory mode updated, but one or more windows need to resynchronize."
                if projection_errors
                else ""
            ),
            level="warning" if projection_errors else "info",
            data={
                "conversation_id": request.conversation_id,
                "memory_mode": updated.memory_mode,
                "memory_polluted": bool(updated.memory_polluted),
                "memory_pollution_sources": list(updated.memory_pollution_sources),
                "revision": int(getattr(updated, "revision", 0) or 0),
                "projection_errors": projection_errors,
            },
        )
        if updated.memory_mode == "disabled":
            _schedule_long_term_memory_forgetting(session, updated)
        return True
    finally:
        _release_conversation_mutation(mutation_claim)


def _schedule_long_term_memory_forgetting(
    session: "WebSocketSession",
    conversation: Any,
) -> None:
    try:
        from backend.memory.generation import schedule_memory_forgetting

        memory_llm = getattr(session, "llm", None)
        memory_task = schedule_memory_forgetting(
            repository=session.conversation_repo,
            llm=memory_llm,
            workspace_root=session.session_lifecycle.workspace_root_for_conversation(
                conversation
            ),
            conversation_id=str(conversation.id),
            token_budget=int(
                getattr(getattr(session.config, "token_budget", None), "total", 0)
                or 0
            ),
        )
        from backend.ws.agent_runner import _lease_session_llm_for_task

        _lease_session_llm_for_task(session, memory_llm, memory_task)
    except Exception:
        logger.exception(
            "Failed to schedule memory forgetting for conversation %s",
            getattr(conversation, "id", ""),
        )


def _memory_reset_active_conversation_ids(session: "WebSocketSession") -> list[str]:
    active: set[str] = set()
    for summary in session.conversation_repo.list_conversations():
        if _conversation_has_active_run(session, summary.id):
            active.add(summary.id)

    try:
        from backend.agent.runtime import default_runtime

        runtime_snapshot = default_runtime().list_runs(include_subagents=True)
        run_conversations = {
            str(run.get("run_id") or ""): str(run.get("conversation_id") or "")
            for run in runtime_snapshot.get("runs", [])
            if isinstance(run, dict)
        }
        for run in runtime_snapshot.get("runs", []):
            if not isinstance(run, dict) or str(run.get("status") or "") != "running":
                continue
            conversation_id = str(run.get("conversation_id") or "").strip()
            if conversation_id:
                active.add(conversation_id)
        for child in runtime_snapshot.get("subagents", []):
            if not isinstance(child, dict):
                continue
            running = str(child.get("status") or "") == "running" or str(
                child.get("background_task") or ""
            ) in {"queued", "running"}
            if not running:
                continue
            conversation_id = run_conversations.get(
                str(child.get("parent_run_id") or ""),
                "",
            )
            if conversation_id:
                active.add(conversation_id)
    except Exception:
        logger.exception("Failed to inspect runtime before memory reset")
        active.add("runtime-status-unknown")
    return sorted(active)


async def _handle_memory_reset_impl(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Implement MiniCode's destructive global ``memory/reset`` contract."""

    if data.get("confirmed") is not True:
        await session.emit_command_result(
            "memory.reset",
            "Explicit confirmation is required before clearing memory.",
            level="error",
            data={"reason": "confirmation_required"},
        )
        return True

    active_conversation_ids = _memory_reset_active_conversation_ids(session)
    if active_conversation_ids:
        await session.emit_command_result(
            "memory.reset",
            "Stop active agents before clearing memory.",
            level="warning",
            data={
                "reason": "run_active",
                "conversation_ids": active_conversation_ids,
            },
        )
        return True

    manager = getattr(session, "memory_manager", None)
    reset_files = getattr(manager, "reset", None)
    if not callable(reset_files):
        await session.emit_command_result(
            "memory.reset",
            "File-backed memory is not available.",
            level="error",
            data={"reason": "memory_unavailable"},
        )
        return True

    try:
        file_result = await asyncio.to_thread(reset_files)
    except Exception as exc:
        logger.exception("Failed to reset file-backed memory")
        await session.emit_command_result(
            "memory.reset",
            f"Memory files could not be cleared: {exc}",
            level="error",
            data={"reason": "file_reset_failed"},
        )
        return True

    try:
        repository_result = await asyncio.to_thread(
            session.conversation_repo.reset_memory_state
        )
    except Exception as exc:
        logger.exception("Memory files reset but conversation memory reset failed")
        await session.emit_command_result(
            "memory.reset",
            f"Memory files were cleared, but task memory cleanup failed: {exc}",
            level="error",
            data={"reason": "conversation_reset_failed", "files_reset": True},
        )
        return True

    for owner_session in _all_live_sessions(session):
        active_id = str(owner_session.active_conversation_id or "")
        if active_id:
            refreshed = owner_session.conversation_repo.get_conversation(active_id)
            if refreshed is not None:
                owner_session.load_active_conversation_snapshot(
                    active_id,
                    refreshed.context_snapshot,
                )
        await owner_session.send_conversation_list()

    cleanup_pending = bool(getattr(file_result, "cleanup_pending", False))
    payload = {
        "files_removed": int(getattr(file_result, "files_removed", 0)),
        "directories_removed": int(getattr(file_result, "directories_removed", 0)),
        "cleanup_pending": cleanup_pending,
        **repository_result,
    }
    await session.emit_command_result(
        "memory.reset",
        (
            "Memory was reset; an old temporary directory will be cleaned up later."
            if cleanup_pending
            else "Memory was reset. Task transcripts and memory modes were preserved."
        ),
        level="warning" if cleanup_pending else "success",
        data=payload,
    )
    return True


async def handle_memory_reset(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Run memory/reset behind the shared MiniCode maintenance barrier."""

    if data.get("confirmed") is not True:
        return await _handle_memory_reset_impl(session, data)
    active = _memory_reset_active_conversation_ids(session)
    if active:
        return await _handle_memory_reset_impl(session, data)

    from backend.memory.generation import begin_memory_reset, end_memory_reset

    pending = await begin_memory_reset(timeout=5.0)
    if pending is None:
        await session.emit_command_result(
            "memory.reset",
            "Memory maintenance is already stopping or resetting.",
            level="warning",
            data={"reason": "memory_maintenance_busy"},
        )
        return True
    if pending:
        await session.emit_command_result(
            "memory.reset",
            "Memory maintenance did not stop within the safety window; nothing was reset.",
            level="error",
            data={"reason": "memory_maintenance_busy", "pending_workers": len(pending)},
        )
        return True
    try:
        # The implementation performs the second active-run check after all
        # background memory writers have been drained, closing the TOCTOU gap.
        return await _handle_memory_reset_impl(session, data)
    finally:
        end_memory_reset()


async def handle_conversation_permission_mode_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_permission_service import plan_permission_mode_update

    plan = plan_permission_mode_update(data, active_conversation_id=str(session.active_conversation_id or ""))
    if plan.error_event is not None:
        from backend.ws.command_results import emit_command_error
        await emit_command_error(session, "conversation.permission_mode.set", plan.error_event)
        return True

    if plan.session_only:
        if session.set_permission_context_mode(plan.requested, source=plan.source):
            await session.emit_permission_mode_updated()
            await session.session_lifecycle.send_task_runtime_update()
        await session.send_conversation_list()
        await session.emit_command_result(
            "conversation.permission_mode.set",
            "",
            data={"mode": plan.requested, "conversation_id": ""},
        )
        return True

    updated = session.conversation_repo.update_permission_mode(plan.conversation_id, plan.requested)
    if updated is None:
        await emit_conversation_not_found(session, plan.conversation_id)
        from backend.ws.command_results import emit_command_error
        await emit_command_error(
            session,
            "conversation.permission_mode.set",
            f"Conversation '{plan.conversation_id}' not found",
            data={"conversation_id": plan.conversation_id},
        )
        return True

    for owner_session in _all_live_sessions(session):
        if plan.conversation_id != owner_session.active_conversation_id:
            continue
        owner_session.set_permission_context_mode(plan.requested, source=plan.source)
        await owner_session.emit_permission_mode_updated()
        if plan.auto_approve_reason:
            await owner_session.auto_approve_pending_tool_approvals(
                reason=plan.auto_approve_reason,
                conversation_id=plan.conversation_id,
                only_auto_allowed=plan.only_auto_allowed,
            )
        await owner_session.session_lifecycle.send_task_runtime_update()

    projection_errors = await _broadcast_conversation_lists(session)
    await session.emit_command_result(
        "conversation.permission_mode.set",
        (
            "Conversation permission mode updated, but one or more windows need to resynchronize."
            if projection_errors
            else "Conversation permission mode updated."
        ),
        level="warning" if projection_errors else "info",
        data={
            "mode": plan.requested,
            "conversation_id": plan.conversation_id,
            "revision": int(getattr(updated, "revision", 0) or 0),
            "projection_errors": projection_errors,
        },
    )
    return True


async def _emit_goal_updated(
    session: "WebSocketSession",
    *,
    conversation_id: str,
    goal: dict[str, Any],
    source: str,
    updated_at: str = "",
    revision: int | None = None,
) -> None:
    from backend.services.conversation_goal_service import build_goal_updated_payload

    await session.send_payload(
        build_goal_updated_payload(
            conversation_id=conversation_id,
            goal=goal,
            source=source,
            updated_at=updated_at,
            revision=revision,
        ),
        log_context="goal.updated",
    )


async def handle_conversation_goal_set(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.conversation_goal_service import prepare_goal_action, resolve_goal_target

    conversation_id, target = resolve_goal_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    if not conversation_id:
        await session.emit_command_result("goal", "No active conversation to update.", level="warning")
        return True
    if target is None:
        await session.emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
        return True

    current_goal = dict(getattr(target, "goal", {}) or {})
    action = prepare_goal_action(
        data,
        conversation_id=conversation_id,
        current_goal=current_goal,
        source=source,
    )
    if not action.should_update and (
        action.event_scope == "always"
        or (action.event_scope == "active" and conversation_id == session.active_conversation_id)
    ):
        await _emit_goal_updated(
            session,
            conversation_id=conversation_id,
            goal=action.event_goal,
            source=source,
            updated_at=str(getattr(target, "updated_at", "") or ""),
            revision=int(getattr(target, "revision", 0) or 0),
        )

    updated = None
    projection_errors: list[str] = []
    if action.should_update:
        updated = session.conversation_repo.update_goal(conversation_id, action.next_goal)
        if updated is None:
            await session.emit_command_result("goal", f"Conversation '{conversation_id}' not found.", level="error")
            return True
        if action.event_scope == "active":
            for owner_session in _all_live_sessions(session):
                if conversation_id != owner_session.active_conversation_id:
                    continue
                await _emit_goal_updated(
                    owner_session,
                    conversation_id=conversation_id,
                    goal=action.event_goal,
                    source=source,
                    updated_at=str(getattr(updated, "updated_at", "") or ""),
                    revision=int(getattr(updated, "revision", 0) or 0),
                )
        projection_errors = await _broadcast_conversation_lists(session)

    outcome_data = dict(action.outcome.data or {})
    if updated is not None:
        outcome_data["revision"] = int(getattr(updated, "revision", 0) or 0)
    if projection_errors:
        outcome_data["projection_errors"] = projection_errors
    await session.emit_command_result(
        action.outcome.command,
        (
            f"{action.outcome.message} One or more windows need to resynchronize."
            if projection_errors
            else action.outcome.message
        ),
        level="warning" if projection_errors else action.outcome.level,
        data=outcome_data or None,
    )
    return True


async def handle_conversation_permission_rules_list(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.permission_rules_service import build_permission_rules_list_outcome, resolve_permission_rule_target

    conversation_id, target = resolve_permission_rule_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    if not conversation_id:
        await session.emit_command_result("permissions.rules.list", "No active conversation to inspect", level="warning")
        return True
    if target is None:
        await session.emit_command_result("permissions.rules.list", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    await session.emit_permission_rules_updated(conversation_id=conversation_id, source=source)
    rules = session.build_permission_rules_payload(conversation=target)
    outcome = build_permission_rules_list_outcome(conversation_id, rules)
    await session.emit_command_result(
        outcome.command,
        outcome.message,
        data=outcome.data,
    )
    return True


async def handle_conversation_permission_rules_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.permission_rules_service import prepare_permission_rule_add, resolve_permission_rule_target

    conversation_id, target = resolve_permission_rule_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    if not conversation_id:
        await session.emit_command_result("permissions.rules.add", "No active conversation to update", level="warning")
        return True
    if target is None:
        await session.emit_command_result("permissions.rules.add", f"Conversation '{conversation_id}' not found", level="error")
        return True

    mutation = prepare_permission_rule_add(target, data, conversation_id=conversation_id)
    if not mutation.should_update:
        await session.emit_command_result(
            mutation.outcome.command,
            mutation.outcome.message,
            level=mutation.outcome.level,
            data=mutation.outcome.data,
        )
        return True

    updated = session.conversation_repo.update_permission_rules(
        conversation_id, deny_rules=mutation.deny_rules, overrides=mutation.serialized_overrides,
    )
    if updated is None:
        await session.emit_command_result("permissions.rules.add", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    projection_errors = await _project_permission_rules_update(
        session,
        conversation_id=conversation_id,
        source=source,
        overrides=mutation.overrides,
        deny_rules=mutation.deny_rules,
    )
    projection_errors.extend(await _broadcast_conversation_lists(session))
    outcome_data = {
        **dict(mutation.outcome.data or {}),
        "revision": int(getattr(updated, "revision", 0) or 0),
        "projection_errors": list(dict.fromkeys(projection_errors)),
    }
    await session.emit_command_result(
        mutation.outcome.command,
        mutation.outcome.message,
        level="warning" if projection_errors else mutation.outcome.level,
        data=outcome_data,
    )
    return True


async def handle_conversation_permission_rules_remove(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.permission_rules_service import prepare_permission_rule_remove, resolve_permission_rule_target

    conversation_id, target = resolve_permission_rule_target(
        session.conversation_repo,
        data,
        active_conversation_id=str(session.active_conversation_id or ""),
    )
    if not conversation_id:
        await session.emit_command_result("permissions.rules.remove", "No active conversation to update", level="warning")
        return True
    if target is None:
        await session.emit_command_result("permissions.rules.remove", f"Conversation '{conversation_id}' not found", level="error")
        return True

    mutation = prepare_permission_rule_remove(target, data, conversation_id=conversation_id)
    if not mutation.should_update:
        await session.emit_command_result(
            mutation.outcome.command,
            mutation.outcome.message,
            level=mutation.outcome.level,
            data=mutation.outcome.data,
        )
        return True

    updated = session.conversation_repo.update_permission_rules(
        conversation_id, deny_rules=mutation.deny_rules, overrides=mutation.serialized_overrides,
    )
    if updated is None:
        await session.emit_command_result("permissions.rules.remove", f"Conversation '{conversation_id}' not found", level="error")
        return True

    source = str(data.get("source") or "websocket.command").strip() or "websocket.command"
    projection_errors = await _project_permission_rules_update(
        session,
        conversation_id=conversation_id,
        source=source,
        overrides=mutation.overrides,
        deny_rules=mutation.deny_rules,
    )
    projection_errors.extend(await _broadcast_conversation_lists(session))
    outcome_data = {
        **dict(mutation.outcome.data or {}),
        "revision": int(getattr(updated, "revision", 0) or 0),
        "projection_errors": list(dict.fromkeys(projection_errors)),
    }
    await session.emit_command_result(
        mutation.outcome.command,
        mutation.outcome.message,
        level="warning" if projection_errors else mutation.outcome.level,
        data=outcome_data,
    )
    return True


async def _project_permission_rules_update(
    session: "WebSocketSession",
    *,
    conversation_id: str,
    source: str,
    overrides: dict[str, Any],
    deny_rules: list[str],
) -> list[str]:
    """Apply one authoritative rule mutation to every connected renderer."""

    errors: list[str] = []
    for owner_session in _all_live_sessions(session):
        try:
            if conversation_id == owner_session.active_conversation_id:
                owner_session.set_permission_context_rules(
                    session_overrides=overrides,
                    tool_deny_rules=deny_rules,
                    source=source,
                )
                await owner_session.session_lifecycle.send_task_runtime_update()
            await owner_session.emit_permission_rules_updated(
                conversation_id=conversation_id,
                source=source,
            )
        except Exception:
            logger.exception(
                "Failed to project permission rules for %s to session %s",
                conversation_id,
                getattr(owner_session, "session_id", ""),
            )
            errors.append(str(getattr(owner_session, "session_id", "")))
    return errors


async def _cleanup_conversation_worktree(session: "WebSocketSession", conversation: Any, *, force: bool = False) -> dict[str, Any]:
    from backend.services.conversation_payload_service import cleanup_isolated_worktree

    current_workspace_root = session.session_lifecycle.current_workspace_root()
    return await asyncio.to_thread(
        cleanup_isolated_worktree,
        conversation,
        force=force,
        current_workspace_root=current_workspace_root,
        main_worktree_root=session.main_worktree_root,
        is_path_within=session.is_path_within,
        worktree_has_local_changes=session.worktree_has_local_changes,
    )


async def handle_permissions_content_rule_add(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Persist a global Tool(content) permission rule (settings.json).

    Drives the approval dialog's "always allow/deny this" action. The runtime
    PermissionChecker is rebuilt from load_config() on each tool call, so the
    saved rule takes effect immediately for subsequent calls.
    """
    from backend.config import SETTINGS_FILE
    from backend.hooks.runtime import run_config_change_hook
    from backend.services.permission_content_service import add_permission_content_rule

    result = add_permission_content_rule(
        str(data.get("rule") or ""),
        deny=bool(data.get("deny", False)),
        scope=str(data.get("scope") or "global"),
    )
    if result.should_emit_config_change:
        await run_config_change_hook(source="permissions", file_path=str(SETTINGS_FILE))
    outcome = result.outcome
    await session.emit_command_result(
        outcome.command,
        outcome.message,
        level=outcome.level,
        data=outcome.data,
    )
    return True


async def handle_context_compact(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Manually trigger context compaction with an optional focus string."""
    from backend.agent.context import CompactionNoopError
    from backend.agent.message import AgentEvent
    from backend.services.context_budget import (
        build_context_budget_snapshot,
        build_context_compacted_event,
        context_ledger_snapshot,
    )

    focus = str(data.get("focus") or "").strip()
    ctx = session.context_builder
    conversation_id = str(session.active_conversation_id or "").strip()
    if not conversation_id:
        await session.send_event(
            AgentEvent.error(
                "No active conversation to compact",
                recoverable=True,
                error_type="context",
                error_code="conversation.required",
            )
        )
        return True
    state = getattr(session, "last_agent_state", None)
    current = session.conversation_repo.get_conversation(conversation_id)
    if current is None:
        await session.send_event(
            AgentEvent.error(
                "Conversation not found",
                recoverable=True,
                error_type="context",
                error_code="conversation.not_found",
            )
        )
        return True
    if _conversation_has_active_run(session, conversation_id):
        await session.send_event(
            AgentEvent.error(
                "This conversation has an active turn. Stop it before compacting context.",
                recoverable=True,
                error_type="conversation_busy",
                error_code="conversation.active_run",
            )
        )
        return True
    from backend.ws.compaction_coordinator import compact_conversation

    before_ledger = context_ledger_snapshot(ctx)
    try:
        before_budget = build_context_budget_snapshot(session, ctx)
    except Exception:
        before_budget = None
    try:
        committed = await compact_conversation(
            session,
            conversation_id=conversation_id,
            context_builder=ctx,
            focus=focus,
            restore_state=state,
        )
        summary_text = committed.summary
        after_ledger = context_ledger_snapshot(ctx)
        try:
            after_budget = build_context_budget_snapshot(session, ctx)
        except Exception:
            after_budget = None
        compacted_event = build_context_compacted_event(
            summary_text,
            before_ledger,
            after_ledger,
        )
        compacted_event.data["conversation_id"] = conversation_id
        if isinstance(before_budget, dict):
            compacted_event.data["before_tokens"] = max(
                0, int(before_budget.get("used") or 0)
            )
        if isinstance(after_budget, dict):
            compacted_event.data["after_tokens"] = max(
                0, int(after_budget.get("used") or 0)
            )
        await session.send_event(compacted_event)
        if isinstance(after_budget, dict):
            used = max(0, int(after_budget.get("used") or 0))
            total = max(0, int(after_budget.get("total") or 0))
            raw_breakdown = after_budget.get("breakdown")
            breakdown = {
                str(name): max(0, int(tokens or 0))
                for name, tokens in raw_breakdown.items()
            } if isinstance(raw_breakdown, dict) else {}
            await session.send_event(
                AgentEvent.budget_update(
                    used=used,
                    total=total,
                    breakdown=breakdown,
                    conversation_id=conversation_id,
                )
            )
            await session.send_event(
                AgentEvent.context_usage(
                    used=used,
                    limit=total,
                    conversation_id=conversation_id,
                    ledger=after_ledger,
                )
            )
    except CompactionNoopError as exc:
        error_event = AgentEvent.error(
            str(exc),
            recoverable=True,
            error_type="context",
            error_code="context.nothing_to_compact",
        )
        error_event.data["conversation_id"] = conversation_id
        await session.send_event(error_event)
    except RuntimeError as exc:
        if "active turn" in str(exc):
            error_event = AgentEvent.error(
                str(exc),
                recoverable=True,
                error_type="conversation_busy",
                error_code="conversation.active_run",
            )
            error_event.data["conversation_id"] = conversation_id
            await session.send_event(error_event)
            return True
        logger.warning("Manual compact failed: %s", exc)
        error_event = AgentEvent.error(
            f"Compact failed: {exc}",
            recoverable=True,
            error_type="context",
            error_code="context.compact_failed",
        )
        error_event.data["conversation_id"] = conversation_id
        await session.send_event(error_event)
    except Exception as exc:
        logger.warning("Manual compact failed: %s", exc)
        error_event = AgentEvent.error(
            f"Compact failed: {exc}",
            recoverable=True,
            error_type="context",
            error_code="context.compact_failed",
        )
        error_event.data["conversation_id"] = conversation_id
        await session.send_event(error_event)
    return True


def _fork_text(value: Any) -> str:
    """Normalize transcript/context content for fork-boundary matching."""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, list):
        return " ".join(_fork_text(item) for item in value if item is not None).strip()
    if isinstance(value, dict):
        for key in ("text", "content", "value"):
            if key in value:
                return _fork_text(value.get(key))
    return str(value or "").strip()


def _history_message_matches_transcript(entry: dict[str, Any], history_message: Any) -> bool:
    """Match one persisted transcript item to one model-context message.

    Context history may contain runtime-injected prefixes, so exact equality is
    intentionally not required for user/assistant text. The ordered walk in
    ``_resolve_context_history_index`` prevents duplicate text from selecting
    an earlier turn.
    """
    role = str(entry.get("role") or "").strip().lower()
    history_role = str(getattr(history_message, "role", "") or "").strip().lower()
    if role != history_role or role not in {"user", "assistant"}:
        return False

    transcript_content = _fork_text(entry.get("content"))
    history_content = _fork_text(getattr(history_message, "content", ""))
    if transcript_content and history_content:
        if transcript_content == history_content:
            return True
        # Runtime context and attachment fallbacks are prepended to user turns.
        if role == "user" and transcript_content in history_content:
            return True
        # Providers may normalize assistant whitespace or append structured
        # text around the persisted final answer.
        if role == "assistant" and (
            transcript_content in history_content or history_content in transcript_content
        ):
            return True
        return False

    if transcript_content or history_content:
        return False
    transcript_calls = entry.get("tool_calls")
    history_calls = getattr(history_message, "tool_calls", None)
    return bool(transcript_calls or history_calls or role == "assistant")


def _resolve_context_history_index(
    context_builder: Any,
    transcript: list[dict[str, Any]],
    target_transcript_index: int,
) -> int | None:
    """Resolve a transcript boundary to the corresponding model-history index.

    The two sequences are deliberately not assumed to have the same length:
    model history can contain runtime context, tool messages, or compaction
    boundaries that are not persisted as user-facing transcript messages.
    """
    history = list(getattr(context_builder, "_history", []) or [])
    if target_transcript_index < 0 or target_transcript_index >= len(transcript):
        return None
    cursor = 0
    target_history_index: int | None = None
    forward_complete = True
    for transcript_index, entry in enumerate(transcript[: target_transcript_index + 1]):
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        match_index: int | None = None
        for history_index in range(cursor, len(history)):
            if _history_message_matches_transcript(entry, history[history_index]):
                match_index = history_index
                break
        if match_index is None:
            forward_complete = False
            break
        cursor = match_index + 1
        if transcript_index == target_transcript_index:
            target_history_index = match_index
    if forward_complete and target_history_index is not None:
        return target_history_index

    # Compaction can replace an old transcript prefix with a summary message.
    # Align the target against the retained suffix from the end so a recent
    # message remains forkable without pretending the old prefix still exists.
    cursor = len(history) - 1
    for transcript_index in range(len(transcript) - 1, target_transcript_index - 1, -1):
        entry = transcript[transcript_index]
        role = str(entry.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        match_index = None
        for history_index in range(cursor, -1, -1):
            if _history_message_matches_transcript(entry, history[history_index]):
                match_index = history_index
                break
        if match_index is None:
            return None
        cursor = match_index - 1
        if transcript_index == target_transcript_index:
            return match_index
    return None


async def handle_context_fork(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Fork from a stable transcript message id, with index compatibility."""
    requested_message_id = str(data.get("message_id") or "").strip()
    source_conversation = getattr(session, "active_conversation", None)
    source_conversation_id = str(
        getattr(source_conversation, "id", "")
        or getattr(session, "active_conversation_id", "")
        or ""
    ).strip()
    if source_conversation is None or not source_conversation_id:
        await _emit_context_error(
            session,
            "Cannot fork context without an active conversation.",
            recoverable=True,
        )
        return True

    requested_message_index = -1
    if not requested_message_id:
        raw_message_index = data.get("message_index", -1)
        try:
            if isinstance(raw_message_index, bool):
                raise ValueError
            if isinstance(raw_message_index, float) and not raw_message_index.is_integer():
                raise ValueError
            requested_message_index = int(raw_message_index)
        except (TypeError, ValueError, OverflowError):
            await _emit_context_error(
                session,
                "Fork message_index must be an integer.",
                recoverable=True,
                conversation_id=source_conversation_id,
            )
            return True

    source_transcript = list(getattr(source_conversation, "transcript", []) or [])
    transcript_index = requested_message_index
    if requested_message_id:
        transcript_index = next(
            (
                index
                for index, entry in enumerate(source_transcript)
                if str(entry.get("id") or "").strip() == requested_message_id
            ),
            -1,
        )
        if transcript_index < 0:
            await _emit_context_error(
                session,
                f"Fork target message not found: {requested_message_id}",
                recoverable=True,
                conversation_id=source_conversation_id,
            )
            return True
    else:
        if not source_transcript:
            await _emit_context_error(
                session,
                "Cannot fork context before the conversation has a visible message.",
                recoverable=True,
                conversation_id=source_conversation_id,
            )
            return True
        if transcript_index < 0:
            transcript_index = len(source_transcript) + transcript_index
        if transcript_index < 0 or transcript_index >= len(source_transcript):
            await _emit_context_error(
                session,
                f"Fork message_index is outside the transcript: {requested_message_index}",
                recoverable=True,
                conversation_id=source_conversation_id,
            )
            return True

    if _conversation_has_active_run(session, source_conversation_id):
        await _emit_context_error(
            session,
            "Cannot fork context while the conversation has an active turn.",
            recoverable=True,
            conversation_id=source_conversation_id,
        )
        return True

    mutation_claim = _try_claim_conversation_mutation(
        session,
        source_conversation_id,
        operation="context_fork",
    )
    if mutation_claim is None:
        await _emit_context_error(
            session,
            "Cannot fork context while another lifecycle operation owns the conversation.",
            recoverable=True,
            conversation_id=source_conversation_id,
        )
        return True

    ctx = session.context_builder
    try:
        context_history_index = _resolve_context_history_index(
            ctx,
            source_transcript,
            transcript_index,
        )
        if context_history_index is None:
            await _emit_context_error(
                session,
                (
                    "Fork target exists in the transcript but is no longer "
                    "available in the active model context. Compact or restore "
                    "the conversation before forking from it."
                ),
                recoverable=True,
                conversation_id=source_conversation_id,
            )
            return True
        forked = ctx.fork_from(context_history_index)
        fork_record = session.fork_registry.create(
            parent_conversation_id=source_conversation_id,
            message_index=transcript_index,
            history_length=len(forked._history),
            estimated_tokens=forked._history_tokens_total,
        )
        fork_id = fork_record.fork_id
        fork_data = {
            "fork_id": fork_id,
            "message_index": transcript_index,
            "context_history_index": context_history_index,
            "history_length": len(forked._history),
            "estimated_tokens": forked._history_tokens_total,
            "parent_conversation_id": source_conversation_id,
            "branch_created": False,
            "branch_activated": False,
        }
        if requested_message_id:
            fork_data["message_id"] = requested_message_id
        durable_record = session.fork_registry.get(fork_id)
        if durable_record is not None:
            durable_data = durable_record.to_dict()
            if not str(durable_data.get("branch_conversation_id") or "").strip():
                durable_data.pop("branch_conversation_id", None)
            fork_data.update(durable_data)
        if source_conversation is not None and bool(data.get("create_branch", True)):
            from backend.agent.plans import cleanup_plan_file, copy_plan_for_fork

            branch_transcript = source_transcript[: max(0, transcript_index + 1)]
            branch_pollution_sources = pollution_sources_from_transcript(
                branch_transcript
            )
            branch_id = fork_id
            branch_workspace_root = str(
                getattr(source_conversation, "worktree_path", "")
                or getattr(source_conversation, "workspace_root", "")
                or ""
            )
            branch_snapshot = forked.export_snapshot()
            copied_plan_path = None
            branch = None
            try:
                # The fork owns a separate mutable plan file even though its
                # context history and workspace are copied/shared from the
                # parent. Use the live context snapshot as the source of plan
                # ownership because it is the snapshot that was actually
                # forked, not a possibly stale repository projection.
                _, copied_plan_path = copy_plan_for_fork(
                    branch_snapshot,
                    branch_snapshot,
                    branch_workspace_root or None,
                )
                branch = session.conversation_repo.create_conversation(
                    conversation_id=branch_id,
                    title=f"{getattr(source_conversation, 'title', 'Conversation')} · 分支",
                    memory_mode=(
                        "polluted"
                        if branch_pollution_sources
                        else getattr(source_conversation, "memory_mode", "enabled")
                    ),
                    memory_polluted=bool(branch_pollution_sources),
                    memory_pollution_sources=branch_pollution_sources,
                    permission_mode=getattr(source_conversation, "permission_mode", "confirm"),
                    permission_deny_rules=list(getattr(source_conversation, "permission_deny_rules", []) or []),
                    permission_overrides=dict(getattr(source_conversation, "permission_overrides", {}) or {}),
                    summary=str(getattr(source_conversation, "summary", "") or ""),
                    transcript=copy.deepcopy(branch_transcript),
                    context_snapshot=branch_snapshot,
                    workspace_root=branch_workspace_root,
                    git_branch=str(getattr(source_conversation, "git_branch", "") or ""),
                    worktree_path="",
                    # Context branches share the checkout but never own/delete the
                    # source conversation's isolated worktree.
                    git_isolated=False,
                    parent_conversation_id=str(source_conversation.id),
                    parent_message_index=transcript_index,
                    fork_id=fork_id,
                    branch_kind="context_fork",
                )
                branch_workspace_root = str(branch.worktree_path or branch.workspace_root or "")
                session.attachment_store.share_for_conversation(
                    source_conversation.id,
                    branch.id,
                    branch_workspace_root,
                )
                session.artifact_store.share_for_conversation(
                    source_conversation.id,
                    branch.id,
                    branch_workspace_root,
                )
                session.diagnostic_store.share_for_conversation(source_conversation.id, branch.id)
            except Exception:
                if branch is not None:
                    with suppress(Exception):
                        session.attachment_store.delete_for_conversation(branch.id)
                    with suppress(Exception):
                        session.artifact_store.delete_for_conversation(branch.id)
                    with suppress(Exception):
                        session.diagnostic_store.delete_for_conversation(branch.id)
                    session.conversation_repo.delete_conversation(branch.id)
                cleanup_plan_file(copied_plan_path)
                session.fork_registry.discard(fork_id)
                raise
            try:
                bound = session.fork_registry.bind_branch(fork_id, branch.id)
                if bound is not None:
                    fork_data.update(bound.to_dict())
            except Exception as exc:
                logger.exception("Failed to bind fork %s to branch %s", fork_id, branch.id)
                fork_data["registry_warning"] = str(exc)
            fork_data.update({
                "branch_conversation_id": branch.id,
                "branch_created": True,
            })
            activate_branch = bool(data.get("activate", False))
            if activate_branch:
                previous_active_id = str(session.active_conversation_id or "").strip()
                previous_active = (
                    session.conversation_repo.get_conversation(previous_active_id)
                    if previous_active_id
                    else source_conversation
                )
                try:
                    switched = await session.switch_workspace_for_conversation(
                        branch,
                        announce=False,
                    )
                    if switched is False:
                        raise RuntimeError("branch workspace activation did not complete")
                    session.active_conversation_id = branch.id
                    session.load_active_conversation_snapshot(branch.id, branch.context_snapshot)
                    session.sync_permission_mode_with_active_conversation(source="context.fork")
                    fork_data["branch_activated"] = True
                except Exception as exc:
                    logger.exception("Failed to activate fork branch %s", branch.id)
                    fork_data["activation_warning"] = str(exc)
                    session.active_conversation_id = previous_active_id or source_conversation_id
                    try:
                        if previous_active is not None:
                            restored = await session.switch_workspace_for_conversation(
                                previous_active,
                                announce=False,
                            )
                            if restored is False:
                                raise RuntimeError("parent workspace restoration did not complete")
                            session.load_active_conversation_snapshot(
                                previous_active.id,
                                previous_active.context_snapshot,
                            )
                            session.sync_permission_mode_with_active_conversation(
                                source="context.fork.rollback"
                            )
                    except Exception as rollback_exc:
                        logger.exception("Failed to restore active conversation after fork activation", exc_info=rollback_exc)
                        fork_data["activation_recovery_required"] = True
            try:
                await session.send_conversation_list()
            except Exception as exc:
                logger.exception("Failed to publish conversation list after fork %s", branch.id)
                fork_data["inventory_warning"] = str(exc)
        event_conversation_id = (
            str(fork_data.get("branch_conversation_id") or "").strip()
            if bool(fork_data.get("branch_activated"))
            else source_conversation_id
        )
        await session.send_event(
            AgentEvent(
                type="context_forked",
                data={**fork_data, "conversation_id": event_conversation_id},
            )
        )
    except Exception as exc:
        logger.warning("Context fork failed: %s", exc)
        await _emit_context_error(
            session,
            f"Fork failed: {exc}",
            recoverable=True,
            conversation_id=source_conversation_id,
        )
    finally:
        _release_conversation_mutation(mutation_claim)
    return True


async def handle_context_side_query(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Run a transient side query without modifying the main context."""
    query = str(data.get("query") or "").strip()
    focus = str(data.get("focus") or "").strip()
    if not query:
        return True
    conversation_id = str(getattr(session, "active_conversation_id", "") or "").strip()
    if not conversation_id:
        await _emit_context_error(
            session,
            "Cannot run a context side query without an active conversation.",
            recoverable=True,
        )
        return True
    ctx = session.context_builder
    try:
        result = await ctx.side_query(
            query,
            focus=focus,
            state=getattr(session, "agent_state", None),
        )
        await session.send_event(
            AgentEvent(
                type="context_side_query_result",
                data={
                    "query": query,
                    "result": result,
                    "focus": focus,
                    "conversation_id": conversation_id,
                },
            )
        )
    except Exception as exc:
        logger.warning("Side query failed: %s", exc)
        await _emit_context_error(
            session,
            f"Side query failed: {exc}",
            recoverable=True,
            conversation_id=conversation_id,
        )
    return True


async def handle_context_ledger(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    """Return the Context Ledger — a structured audit of context composition."""
    conversation_id = str(getattr(session, "active_conversation_id", "") or "").strip()
    if not conversation_id:
        await _emit_context_error(
            session,
            "Cannot inspect the context ledger without an active conversation.",
            recoverable=True,
        )
        return True
    ctx = session.context_builder
    try:
        ledger = ctx.context_ledger()
        await session.send_event(
            AgentEvent(
                type="context_ledger",
                data={**ledger, "conversation_id": conversation_id},
            )
        )
    except Exception as exc:
        logger.warning("Context ledger failed: %s", exc)
        await _emit_context_error(
            session,
            f"Ledger failed: {exc}",
            recoverable=True,
            conversation_id=conversation_id,
        )
    return True


HANDLERS: dict[str, Any] = {
    "conversation.create": handle_conversation_create,
    "conversation.clone": handle_conversation_clone,
    "conversation.merge": handle_conversation_merge,
    "conversation.export": handle_conversation_export,
    "conversation.switch": handle_conversation_switch,
    "conversation.list": handle_conversation_list,
    "conversation.rename": handle_conversation_rename,
    "conversation.archive": handle_conversation_archive,
    "conversation.unarchive": handle_conversation_unarchive,
    "conversation.delete": handle_conversation_delete,
    "conversation.clear": handle_conversation_clear,
    "conversation.truncate": handle_conversation_truncate,
    "conversation.worktree.cleanup": handle_conversation_worktree_cleanup,
    "conversation.worktree.handoff.preflight": handle_conversation_worktree_handoff_preflight,
    "conversation.worktree.handoff.execute": handle_conversation_worktree_handoff_execute,
    "conversation.memory_mode.set": handle_conversation_memory_mode_set,
    "memory.reset": handle_memory_reset,
    "conversation.permission_mode.set": handle_conversation_permission_mode_set,
    "conversation.goal.set": handle_conversation_goal_set,
    "conversation.permission.rules.list": handle_conversation_permission_rules_list,
    "conversation.permission.rules.add": handle_conversation_permission_rules_add,
    "conversation.permission.rules.remove": handle_conversation_permission_rules_remove,
    "permissions.content_rule.add": handle_permissions_content_rule_add,
    "context.compact": handle_context_compact,
    "context.fork": handle_context_fork,
    "context.side_query": handle_context_side_query,
    "context.ledger": handle_context_ledger,
}
