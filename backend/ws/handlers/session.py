from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.ws.handler import WebSocketSession

logger = logging.getLogger(__name__)

RUNTIME_PROTOCOL_VERSION = "1.0.0"


def _seq_from_restore_payload(data: dict[str, Any]) -> int:
    from backend.services.session_restore_service import seq_from_restore_payload

    return seq_from_restore_payload(data)


async def handle_session_tasks_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.session_inspect_service import build_tasks_inspect_outcome

    snapshot = session.runtime_snapshot()
    outcome = build_tasks_inspect_outcome(session.session_id, snapshot)
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        data=outcome.data,
    )
    return True


async def handle_session_status_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.api.routes_health import get_mcp_status
    from backend.services.session_inspect_service import build_status_inspect_outcome

    mcp_status = get_mcp_status()
    # Skill selections are turn-scoped contextual input, not session state.
    active_skills: list[str] = []
    snapshot = session.runtime_snapshot()
    outcome = build_status_inspect_outcome(
        session_id=session.session_id,
        selected_model=session.selected_model,
        permission_mode=session.permission_context.mode,
        mcp_status=mcp_status,
        active_skills=active_skills,
        runtime_snapshot=snapshot,
    )
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        data=outcome.data,
    )
    return True


async def handle_session_usage_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.llm.cost_tracker import CostTracker
    from backend.services.session_inspect_service import build_usage_inspect_result

    tracker_summary = CostTracker.get_instance().get_summary(session.session_id)
    state = getattr(session, "_last_agent_state", None)
    if state is None:
        from backend.agent.state import AgentState
        state = AgentState(user_message="")

    # Surface MCP tools connected since this session started before snapshotting.
    session.refresh_tool_registry_if_mcp_changed()

    tool_schemas = None
    try:
        tool_schemas = session.tool_registry.get_schemas(
            budget=getattr(session.config.token_budget, "tool_schemas", 6000),
            permission_checker=session.permission_checker,
            permission_context=session.permission_context,
        )
    except Exception as exc:
        logger.debug("usage tool schema snapshot failed: %s", exc)

    try:
        budget_snapshot = session.context_builder.get_budget_snapshot(
            state=state,
            tool_schemas=tool_schemas,
        )
    except Exception as exc:
        logger.debug("usage budget snapshot failed: %s", exc)
        used = int(getattr(session.context_builder, "token_usage", 0) or 0)
        total = int(getattr(getattr(session.context_builder, "_budget", None), "total", 0) or 0)
        budget_snapshot = {"used": used, "total": total, "breakdown": {}}

    conversation_id = str(session.active_conversation_id or "").strip()
    budget_event, context_event, outcome = build_usage_inspect_result(
        session_id=session.session_id,
        conversation_id=conversation_id,
        tracker_summary=tracker_summary,
        budget_snapshot=budget_snapshot,
        context_ledger=session.context_builder.context_ledger(),
    )
    await session._send_event(budget_event)
    await session._send_event(context_event)
    # `silent` callers (the usage ring's per-turn auto-refresh) only want the
    # context_usage / budget_update events above to refresh the indicator —
    # they must not append a visible "/usage" notice to the transcript.
    if not data.get("silent"):
        await session._emit_command_result(
            outcome.command,
            outcome.message,
            data=outcome.data,
        )
    return True


async def handle_session_permissions_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.session_inspect_service import build_permissions_inspect_outcome

    rules = session._build_permission_rules_payload(conversation=session.active_conversation)
    outcome = build_permissions_inspect_outcome(
        session_id=session.session_id,
        conversation_id=session.active_conversation_id,
        rules=rules,
    )
    await session._emit_command_result(
        outcome.command,
        outcome.message,
        data=outcome.data,
    )
    return True


async def handle_runtime_capabilities_inspect(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    await session._send_runtime_capabilities(source=str(data.get("source") or "runtime.inspect"))
    return True


async def handle_session_restore(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.session_restore_service import (
        build_restore_conversation_switched_payload,
        build_restored_runtime_snapshot,
        build_session_restored_payload,
        seq_from_restore_payload,
    )
    from backend.ws.session_restore import SessionRestoreManager

    last_conversation_id = data.get("last_conversation_id")
    last_workspace_root = data.get("last_workspace_root")
    last_seq = seq_from_restore_payload(data)
    current_seq = int(getattr(session, "_ws_event_seq", 0) or 0)
    missed_by_seq = bool(last_seq and last_seq < current_seq)
    replay_candidates = session._replayable_events_after(last_seq) if last_seq else []
    event_log_gap = session._event_log_has_gap_after(last_seq) if last_seq else False
    replay_can_cover_miss = bool(replay_candidates) and not event_log_gap
    replay_terminal_conversation_ids = {
        str(payload.get("conversation_id") or "").strip()
        for payload in replay_candidates
        if payload.get("type") == "done" and str(payload.get("conversation_id") or "").strip()
    }

    restore_manager = SessionRestoreManager(session.conversation_repo)
    result = await restore_manager.restore_session(
        session_id=session.session_id,
        last_conversation_id=last_conversation_id,
        last_workspace_root=last_workspace_root,
    )
    restored_conversation = result.get("conversation") if isinstance(result.get("conversation"), dict) else None
    restored_conversation_id = restored_conversation.get("id") if restored_conversation else None
    restored_workspace = result.get("workspace") if isinstance(result.get("workspace"), dict) else None
    active_payload = restored_conversation
    is_hydrating = False
    if restored_conversation_id:
        target = session.conversation_repo.get_conversation(str(restored_conversation_id))
        if target is not None and not getattr(target, "archived", False):
            session.active_conversation_id = target.id
            await session._switch_workspace_for_conversation(target, announce=False)
            is_hydrating = session._load_active_conversation_snapshot(target.id, target.context_snapshot)
            session._sync_permission_mode_with_active_conversation(source="session.restore")
            active_payload = target.to_dict()
        else:
            restored_conversation_id = None
            active_payload = None

    if not restored_conversation_id and last_conversation_id:
        session.active_conversation_id = None
        session.context_builder.clear()
        clear_runtime = getattr(session, "_clear_workspace_runtime", None)
        if callable(clear_runtime):
            clear_runtime()

    runtime_snapshot = build_restored_runtime_snapshot(
        session.runtime_snapshot(),
        restored_conversation_id=restored_conversation_id,
        active_payload=active_payload,
        restored_workspace=restored_workspace,
    )
    provider_capabilities = runtime_snapshot.get("provider_capabilities") if isinstance(runtime_snapshot, dict) else {}
    provider_id = str((provider_capabilities or {}).get("provider_id") or "").strip()
    base_url = str((provider_capabilities or {}).get("base_url") or "").strip()
    wire_api = str((provider_capabilities or {}).get("wire_api") or "").strip()

    await session._send_ws_payload(
        build_session_restored_payload(
            result,
            restored_conversation_id=restored_conversation_id,
            active_payload=active_payload,
            restored_workspace=restored_workspace,
            runtime_snapshot=runtime_snapshot,
                        selected_model=session.selected_model,
            provider=session.provider,
            available_models=session.available_models,
            models_source=session.models_source,
            missed_events=bool(
                event_log_gap
                or (
                    session._events_dropped_during_disconnect
                    and missed_by_seq
                    and not replay_can_cover_miss
                )
            ),
            last_seq=last_seq,
            current_seq=current_seq,
            replayed_events=len(replay_candidates),
            provider_id=provider_id,
            base_url=base_url,
            wire_api=wire_api,
        ),
        log_context="session.restored",
    )
    session._events_dropped_during_disconnect = False

    if restored_conversation_id and active_payload:
        await session._send_ws_payload(
            build_restore_conversation_switched_payload(
                restored_conversation_id=restored_conversation_id,
                active_payload=active_payload,
                is_hydrating=is_hydrating,
                runtime_snapshot=runtime_snapshot,
            ),
            log_context="conversation.switched",
        )

    if replay_candidates:
        await session._replay_missed_events(
            last_seq,
            events=replay_candidates,
            current_seq=current_seq,
        )
    # Replay the frozen incremental window first, then replace it with the
    # authoritative accumulated stream snapshot. This avoids duplicated text.
    await session._reemit_pending_state(
        skip_stream_conversation_ids=replay_terminal_conversation_ids,
    )
    if restored_conversation_id:
        # Durable follow-ups survive a process/WebSocket restart. Dispatch only
        # after the authoritative session snapshot and replay have been applied.
        session._schedule_next_queued_user_message(restored_conversation_id)
    return True


async def handle_session_sync(session: "WebSocketSession", data: dict[str, Any]) -> bool:
    from backend.services.session_restore_service import build_session_synced_payload, seq_from_restore_payload
    from backend.ws.session_restore import SessionRestoreManager

    last_seq = seq_from_restore_payload(data)
    current_seq = int(getattr(session, "_ws_event_seq", 0) or 0)
    replay_candidates = session._replayable_events_after(last_seq) if last_seq else []
    event_log_gap = session._event_log_has_gap_after(last_seq) if last_seq else False
    replay_can_cover_miss = bool(replay_candidates) and not event_log_gap
    replay_terminal_conversation_ids = {
        str(payload.get("conversation_id") or "").strip()
        for payload in replay_candidates
        if payload.get("type") == "done" and str(payload.get("conversation_id") or "").strip()
    }

    restore_manager = SessionRestoreManager(session.conversation_repo)
    result = await restore_manager.sync_session(
        session_id=session.session_id,
        session_snapshot=session.runtime_snapshot(),
    )
    workspace_root = session._workspace_root_for_conversation()
    runtime_snapshot = result.get("session") if isinstance(result.get("session"), dict) else {}
    provider_capabilities = runtime_snapshot.get("provider_capabilities") if isinstance(runtime_snapshot, dict) else {}
    provider_id = str((provider_capabilities or {}).get("provider_id") or "").strip()
    base_url = str((provider_capabilities or {}).get("base_url") or "").strip()
    wire_api = str((provider_capabilities or {}).get("wire_api") or "").strip()

    await session._send_ws_payload(
        build_session_synced_payload(
            result,
            protocol_version=RUNTIME_PROTOCOL_VERSION,
            active_conversation=session.active_conversation,
            active_conversation_id=session.active_conversation_id,
            workspace_root=workspace_root,
                        selected_model=session.selected_model,
            provider=session.provider,
            available_models=session.available_models,
            models_source=session.models_source,
            last_seq=last_seq,
            current_seq=current_seq,
            replayed_events=len(replay_candidates),
            event_log_gap=event_log_gap,
            snapshot_required=bool(last_seq and last_seq < current_seq and not replay_can_cover_miss),
            provider_id=provider_id,
            base_url=base_url,
            wire_api=wire_api,
        ),
        log_context="session.synced",
    )
    if replay_candidates:
        await session._replay_missed_events(
            last_seq,
            events=replay_candidates,
            current_seq=current_seq,
        )
    # A stream or approval can change without changing transcript length.  The
    # replay log is the source of truth for those mutations; this snapshot is
    # only the authoritative fallback when the bounded log has a gap.
    await session._reemit_pending_state(
        skip_stream_conversation_ids=replay_terminal_conversation_ids,
    )
    active_conversation_id = str(session.active_conversation_id or "").strip()
    if active_conversation_id:
        session._schedule_next_queued_user_message(active_conversation_id)
    return True


HANDLERS: dict[str, Any] = {
    "session.tasks.inspect": handle_session_tasks_inspect,
    "session.status.inspect": handle_session_status_inspect,
    "session.usage.inspect": handle_session_usage_inspect,
    "session.permissions.inspect": handle_session_permissions_inspect,
    "runtime.capabilities.inspect": handle_runtime_capabilities_inspect,
    "session.restore": handle_session_restore,
    "session.sync": handle_session_sync,
}
