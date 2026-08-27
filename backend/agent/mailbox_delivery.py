"""Iteration-boundary delivery for subagent and parent mailboxes."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.tools.base import truncate_tool_result
from backend.permissions.checker import normalize_permission_mode_token


logger = logging.getLogger(__name__)


def _plan_approval_request(message: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(getattr(message, "content", "") or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "plan_approval_request":
        return None
    required = ("from", "timestamp", "plan_file_path", "plan_content", "request_id")
    if any(not isinstance(payload.get(key), str) or not payload[key].strip() for key in required):
        return None
    return payload


def _plan_approval_response(message: Any) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(getattr(message, "content", "") or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("type") != "plan_approval_response":
        return None
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id or not isinstance(payload.get("approved"), bool):
        return None
    return payload


async def _handle_teammate_plan_approval_responses(
    *,
    runtime: AgentRuntime,
    participant_id: str,
    mailbox_epoch: int,
    agent_path: str,
    conversation_id: str,
    metadata: dict[str, Any],
) -> int:
    record = runtime.get_subagent(participant_id)
    if (
        record is None
        or not bool(getattr(record, "plan_mode_required", False))
        or not bool(getattr(record, "awaiting_plan_approval", False))
    ):
        return 0
    active_request_id = str(getattr(record, "active_plan_request_id", "") or "")
    if not active_request_id:
        return 0
    parent_run_id = str(getattr(record, "parent_run_id", "") or "")
    team_name = str(getattr(record, "team_name", "") or "")
    messages = runtime.list_swarm_messages(
        participant_id=participant_id,
        conversation_id=conversation_id,
        since_seq=0,
        limit=1,
        mailbox_epoch=mailbox_epoch,
        message_kind="plan_approval_response",
        correlation_id=active_request_id,
    )
    for message in messages:
        response = _plan_approval_response(message)
        if response is None or response.get("request_id") != active_request_id:
            continue
        if str(getattr(message, "sender_id", "") or "") != parent_run_id:
            continue
        if str(getattr(message, "team_name", "") or "") != team_name:
            continue
        if int(getattr(message, "recipient_mailbox_epoch", 0) or 0) != mailbox_epoch:
            continue
        if bool(response.get("approved")):
            # Reject an unsupported mode instead of silently weakening it to
            # The parent ceiling is applied by the setter downstream.
            target_mode = normalize_permission_mode_token(
                response.get("permission_mode")
            )
            setter = metadata.get("permission_mode_setter")
            if callable(setter):
                result = setter(target_mode, source="teammate.plan_approved")
                if hasattr(result, "__await__"):
                    await result
            metadata["awaiting_plan_approval"] = False
            metadata["active_plan_request_id"] = ""
            runtime.update_subagent_lifecycle(
                participant_id,
                permission_mode=target_mode,
                awaiting_plan_approval=False,
                active_plan_request_id="",
                current_activity="approved",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
        else:
            metadata["awaiting_plan_approval"] = False
            metadata["active_plan_request_id"] = ""
            runtime.update_subagent_lifecycle(
                participant_id,
                awaiting_plan_approval=False,
                active_plan_request_id="",
                current_activity="plan_rejected",
                agent_path=agent_path,
                mailbox_epoch=mailbox_epoch,
            )
        consumed_ids = metadata.get("_consumed_lifecycle_message_ids")
        if not isinstance(consumed_ids, list):
            consumed_ids = []
        message_id = str(getattr(message, "message_id", "") or "").strip()
        if message_id and message_id not in consumed_ids:
            consumed_ids.append(message_id)
        metadata["_consumed_lifecycle_message_ids"] = consumed_ids[-256:]
        return 1
    return 0


_PLAN_APPROVAL_AUTO_MODES = {"bypass"}
_PLAN_APPROVAL_GRANTABLE_MODES = {"confirm", "auto", "bypass", "plan"}


def _leader_plan_review_required(metadata: dict[str, Any] | None) -> bool:
    """Approval belongs to the user unless the session pre-authorized it.

    ``bypass`` sessions already grant broad execution without
    per-action confirmation, so the leader may answer teammate plan requests
    directly.  Every other mode must surface the request to the client and
    wait for an explicit ``subagent.plan_review`` decision.
    """

    provider = metadata.get("permission_context_provider") if isinstance(metadata, dict) else None
    mode = ""
    if callable(provider):
        try:
            context = provider()
        except Exception as exc:
            logger.warning("plan approval permission probe failed: %s", exc)
            context = None
        mode = str(getattr(context, "mode", "") or "").strip().lower()
    if not mode and isinstance(metadata, dict):
        mode = str(
            metadata.get("prompt_mode") or metadata.get("permission_mode") or ""
        ).strip().lower()
    return mode not in _PLAN_APPROVAL_AUTO_MODES


async def _handle_parent_plan_approval_requests(
    *,
    runtime: AgentRuntime,
    parent_run_id: str,
    conversation_id: str,
    emit_event: Any | None,
    metadata: dict[str, Any] | None = None,
) -> int:
    """Leader-side handling of required-plan teammate requests.

    The mailbox record and sender epoch remain authoritative; a sealed/stale
    sender cannot submit a request.  Approval is the user's decision: only
    sessions that pre-authorize broad execution (bypass) are
    answered by the leader directly; every other request is surfaced to the
    client as ``subagent.plan_approval_requested`` and stays pending until
    the user decides via ``subagent.plan_review`` (or the requester's own
    deadline converts it into an explicit rejection).
    """

    if not parent_run_id:
        return 0
    review_required = _leader_plan_review_required(metadata)
    surfaced_ids: list[str] = []
    if isinstance(metadata, dict):
        raw_surfaced = metadata.get("_surfaced_plan_request_ids")
        if isinstance(raw_surfaced, list):
            surfaced_ids = [str(item) for item in raw_surfaced if str(item).strip()]
    try:
        messages = runtime.list_swarm_messages(
            # The leader is addressed through Claude's stable virtual mailbox;
            # ``parent_run_id`` is an ownership fence, never the recipient id.
            participant_id="parent",
            conversation_id=conversation_id,
            since_seq=0,
            limit=1000,
            message_kind="plan_approval_request",
        )
    except Exception as exc:
        logger.warning("parent plan approval mailbox read failed: %s", exc)
        return 0

    handled = 0
    for message in messages:
        if str(getattr(message, "recipient_id", "") or "") != "parent":
            continue
        request = _plan_approval_request(message)
        if request is None:
            continue
        request_id = request["request_id"].strip()
        sender_id = str(getattr(message, "sender_id", "") or "").strip()
        sender = runtime.get_subagent(sender_id)
        if sender is None or str(sender.status or "") != "running":
            continue
        if str(getattr(sender, "parent_run_id", "") or "") != parent_run_id:
            continue
        parent_run = runtime.get_run(parent_run_id)
        if (
            parent_run is None
            or str(getattr(parent_run, "conversation_id", "") or "") != conversation_id
        ):
            continue
        sender_team = str(getattr(sender, "team_name", "") or "")
        message_team = str(getattr(message, "team_name", "") or "")
        if not sender_team or sender_team != message_team:
            continue
        if str(request.get("from") or "") != str(
            getattr(sender, "teammate_name", "") or ""
        ):
            continue
        if int(getattr(message, "sender_mailbox_epoch", 0) or 0) != int(
            sender.mailbox_epoch or 0
        ):
            continue
        sender_epoch = int(sender.mailbox_epoch or 0)
        if review_required:
            # The user decides.  Surface the pending request once and leave
            # it unanswered; the requester's deadline converts silence into
            # an explicit rejection on its own.
            if request_id not in surfaced_ids:
                surfaced_ids.append(request_id)
                if isinstance(metadata, dict):
                    metadata["_surfaced_plan_request_ids"] = surfaced_ids[-256:]
                if emit_event is not None:
                    try:
                        await emit_event(
                            "subagent.plan_approval_requested",
                            {
                                "conversation_id": conversation_id,
                                "subagent_id": sender_id,
                                "request_id": request_id,
                                "teammate_name": str(
                                    getattr(sender, "teammate_name", "") or ""
                                ),
                                "team_name": str(getattr(message, "team_name", "") or ""),
                                "plan_file_path": str(request.get("plan_file_path") or ""),
                                "plan_content": str(request.get("plan_content") or ""),
                            },
                        )
                    except Exception as exc:
                        logger.warning(
                            "plan approval surface event failed: %s", exc
                        )
                else:
                    logger.warning(
                        "Teammate plan request %s from %s awaits user review; no client channel available.",
                        request_id,
                        sender_id,
                    )
            continue
        reservation_token = runtime.reserve_lifecycle_response(
            response_kind="plan_approval_response",
            participant_id=sender_id,
            mailbox_epoch=sender_epoch,
            request_id=request_id,
            target_id=parent_run_id,
            expected_active_plan_request_id=request_id,
        )
        if not reservation_token:
            continue
        response = {
            "type": "plan_approval_response",
            "request_id": request_id,
            "approved": True,
            "timestamp": datetime.now(UTC).isoformat(),
            # Plan approval never widens a teammate beyond default execution
            # permissions; the leader's own mode is not transferable.
            "permission_mode": "confirm",
        }
        reservation = {
            "response_kind": "plan_approval_response",
            "participant_id": sender_id,
            "mailbox_epoch": sender_epoch,
            "request_id": request_id,
            "reservation_token": reservation_token,
        }
        try:
            runtime.send_swarm_message(
                sender_id=parent_run_id,
                recipient_id=sender_id,
                content=json.dumps(response, ensure_ascii=False),
                conversation_id=conversation_id,
                team_name=str(getattr(message, "team_name", "") or ""),
                recipient_mailbox_epoch=sender_epoch,
            )
        except Exception:
            runtime.release_lifecycle_response(**reservation)
            raise
        if not runtime.commit_lifecycle_response(**reservation):
            logger.error(
                "plan approval response delivered but lifecycle fence commit failed: %s",
                request_id,
            )
            continue
        handled += 1
        if emit_event is not None:
            try:
                await emit_event(
                    "subagent.event",
                    {
                        "conversation_id": conversation_id,
                        "subagent_id": sender_id,
                        "event": {
                            "type": "message",
                            "message": message.public_dict(),
                        },
                    },
                )
                # Structured evidence: a teammate plan was approved without
                # user review. This must be visible, never silent.
                await emit_event(
                    "system_notice",
                    {
                        "title": "Teammate plan auto-approved",
                        "message": (
                            f"Plan request {request_id} from teammate "
                            f"'{request.get('from')}' was approved by the leader "
                            "without user review (granted mode: confirm)."
                        ),
                        "severity": "warning",
                        "plan_approval_auto": True,
                        "request_id": request_id,
                        "teammate_id": sender_id,
                        "granted_permission_mode": "confirm",
                        "plan_file_path": str(request.get("plan_file_path") or ""),
                    },
                )
            except Exception as exc:
                logger.warning("plan approval request event failed: %s", exc)
        logger.warning(
            "Leader auto-approved teammate plan request %s from %s; granted mode 'confirm' without user review.",
            request_id,
            sender_id,
        )
    return handled


def _mailbox_deliverable(metadata: dict[str, Any]) -> bool:
    owner = metadata.get("turn_input_queue") or metadata.get("turn_execution_state")
    predicate = getattr(owner, "mailbox_deliverable", None)
    if not callable(predicate):
        return True
    run_id = str(metadata.get("run_id") or "").strip()
    try:
        return bool(predicate(run_id))
    except Exception as exc:
        logger.debug("turn mailbox phase check failed: %s", exc)
        return False


def subagent_mailbox_participant_id(metadata: dict[str, Any]) -> str:
    if str(metadata.get("agent_mode") or "").strip().lower() != "subagent":
        return "parent" if str(metadata.get("run_id") or "").strip() else ""
    return str(
        metadata.get("run_id")
        or metadata.get("agent_id")
        or metadata.get("task_id")
        or ""
    ).strip()


def format_subagent_mailbox_injection(messages: list[Any]) -> str:
    lines = [
        "<subagent_mailbox>",
        "New coordination messages addressed to this agent arrived while it was running. Treat them as current parent/teammate instructions and adjust the next step accordingly.",
    ]
    for message in messages:
        seq = int(getattr(message, "seq", 0) or 0)
        sender = str(getattr(message, "sender_id", "") or "unknown")
        recipient = str(getattr(message, "recipient_id", "") or "")
        task_id = str(getattr(message, "task_id", "") or "")
        task_suffix = f" task={task_id}" if task_id else ""
        lines.append(
            f"- seq={seq} from={sender} to={recipient}{task_suffix}: "
            f"{str(getattr(message, 'content', '') or '').strip()}"
        )
    lines.append("</subagent_mailbox>")
    return "\n".join(lines)


async def inject_subagent_mailbox_updates(
    *,
    ctx: ContextBuilder,
    state: AgentState,
    metadata: dict[str, Any],
    conversation_id: str,
    emit_event: Any | None = None,
) -> int:
    """Pull addressed messages into a subagent at an iteration boundary."""
    if not _mailbox_deliverable(metadata):
        return 0
    participant_id = subagent_mailbox_participant_id(metadata)
    runtime = metadata.get("agent_runtime")
    list_messages = getattr(runtime, "list_swarm_messages", None)
    if not participant_id or not callable(list_messages):
        return 0

    prompt_context = state.prompt_context if isinstance(state.prompt_context, dict) else {}
    highwater_key = f"subagent_mailbox_highwater:{participant_id}"
    try:
        since_seq = int(prompt_context.get(highwater_key) or 0)
    except (TypeError, ValueError):
        since_seq = 0
    participant_record = getattr(runtime, "get_subagent", lambda _id: None)(participant_id)
    current_epoch = int(getattr(participant_record, "mailbox_epoch", 0) or 0)
    current_agent_path = str(getattr(participant_record, "agent_path", "") or "")
    current_started_at = int(getattr(participant_record, "started_at", 0) or 0)
    if participant_record is not None:
        await _handle_teammate_plan_approval_responses(
            runtime=runtime,
            participant_id=participant_id,
            mailbox_epoch=current_epoch,
            agent_path=current_agent_path,
            conversation_id=conversation_id,
            metadata=metadata,
        )

    def belongs_to_current_incarnation(message: Any) -> bool:
        recipient = str(getattr(message, "recipient_id", "") or "")
        if recipient in {"all", "*"}:
            recipient_epochs = getattr(message, "recipient_mailbox_epochs", None)
            if isinstance(recipient_epochs, dict) and participant_id in recipient_epochs:
                return int(recipient_epochs.get(participant_id) or 0) == current_epoch
            created_at = int(getattr(message, "created_at", 0) or 0)
            return current_epoch <= 1 and (not current_started_at or created_at >= current_started_at)
        target_epoch = int(getattr(message, "recipient_mailbox_epoch", 0) or 0)
        return target_epoch == current_epoch or (target_epoch == 0 and current_epoch <= 1)

    # Claims remain the only delivery authority. The read-only history scan is
    # solely for recording old-incarnation messages once, so operators can see
    # that they were fenced instead of silently disappearing.
    addressed_messages = [
        message
        for message in list_messages(
            participant_id=participant_id,
            conversation_id=conversation_id,
            since_seq=since_seq,
            limit=100,
        )
        if str(getattr(message, "recipient_id", "") or "") in {participant_id, "all", "*"}
    ]
    stale_messages = [
        message for message in addressed_messages
        if not belongs_to_current_incarnation(message)
    ]
    stale_count = len(stale_messages)
    observed_highwater = max(
        (int(getattr(message, "seq", 0) or 0) for message in addressed_messages),
        default=since_seq,
    )

    claim_messages = getattr(runtime, "claim_swarm_messages", None)
    claims: list[Any] = []
    if callable(claim_messages):
        # The durable delivery ledger is authoritative. Do not filter claims by
        # the in-memory high-water: an unacked lease may expire behind a newer
        # sequence and must remain replayable after a crash.
        claims = claim_messages(
            participant_id=participant_id,
            mailbox_epoch=current_epoch,
            conversation_id=conversation_id,
            since_seq=0,
            limit=100,
        )
        consumed_ids = {
            str(item).strip()
            for item in (metadata.get("_consumed_lifecycle_message_ids") or [])
            if str(item).strip()
        }
        lifecycle_claims = []
        messages = []
        for claim in claims:
            if str(getattr(claim.message, "message_id", "") or "").strip() in consumed_ids:
                lifecycle_claims.append(claim)
            else:
                messages.append(claim.message)
        if lifecycle_claims:
            runtime.ack_swarm_message_claims(lifecycle_claims)
            claims = [claim for claim in claims if claim not in lifecycle_claims]
    else:
        if not addressed_messages:
            return 0
        messages = [message for message in addressed_messages if belongs_to_current_incarnation(message)]
    if not messages:
        if stale_count:
            prompt_context[highwater_key] = max(since_seq, observed_highwater)
            state.prompt_context = prompt_context
            state.mark_transition(
                "subagent_mailbox_stale_sealed",
                stale_count=stale_count,
                mailbox_epoch=current_epoch,
                high_water=observed_highwater,
            )
        return 0

    max_seq = max(int(getattr(message, "seq", 0) or 0) for message in messages)

    try:
        ctx.append_user(format_subagent_mailbox_injection(messages))
    except Exception:
        release_claims = getattr(runtime, "release_swarm_message_claims", None)
        if claims and callable(release_claims):
            release_claims(claims)
        raise
    if claims:
        pending_claims = prompt_context.get("delivered_swarm_message_claims")
        if not isinstance(pending_claims, list):
            pending_claims = []
        known_tokens = {
            str(getattr(claim, "claim_token", "") or "")
            for claim in pending_claims
        }
        pending_claims.extend(
            claim
            for claim in claims
            if str(getattr(claim, "claim_token", "") or "") not in known_tokens
        )
        prompt_context["delivered_swarm_message_claims"] = pending_claims
    high_water = max(since_seq, max_seq, observed_highwater)
    prompt_context[highwater_key] = high_water
    state.prompt_context = prompt_context
    if stale_count:
        state.mark_transition(
            "subagent_mailbox_stale_sealed",
            stale_count=stale_count,
            mailbox_epoch=current_epoch,
            high_water=high_water,
        )
    state.mark_transition(
        "subagent_mailbox_update",
        message_count=len(messages),
        high_water=high_water,
    )
    if emit_event is not None:
        try:
            await emit_event(
                "subagent.mailbox",
                {
                    "conversation_id": conversation_id,
                    "subagent_id": participant_id,
                    "count": len(messages),
                    "high_water": high_water,
                    "mailbox_epoch": current_epoch,
                    "stale_sealed": stale_count,
                },
            )
        except Exception as exc:
            logger.debug("subagent mailbox event failed: %s", exc)
    return len(messages)


def format_parent_notification_message(item: dict[str, Any]) -> str:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    subagent_id = str(item.get("subagent_id") or payload.get("subagent_id") or "").strip()
    if str(item.get("kind") or "").strip() == "async_hook":
        hook_id = str(payload.get("hook_id") or subagent_id or "unknown").strip()
        hook_event = str(payload.get("event") or "hook").strip()
        content = truncate_tool_result(
            str(payload.get("content") or payload.get("error") or "").strip()
        )
        return "\n".join(
            (
                "<hook-notification>",
                f"hook_id: {hook_id}",
                f"event: {hook_event}",
                "status: blocked",
                "message:",
                content or "Hook blocked event",
                "</hook-notification>",
            )
        )
    agent_type = str(payload.get("agent_type") or "").strip() or "subagent"
    status = str(payload.get("status") or item.get("status") or "completed").strip()
    summary = str(payload.get("content") or payload.get("error") or "").strip()
    summary = truncate_tool_result(summary)
    lines = [
        "<task-notification>",
        f"subagent_id: {subagent_id or 'unknown'}",
        f"agent_type: {agent_type}",
        f"status: {status}",
    ]
    prompt_summary = str(payload.get("prompt_summary") or "").strip()
    if prompt_summary:
        lines.append(f"task: {prompt_summary}")
    artifact_id = str(payload.get("artifact_id") or "").strip()
    if artifact_id:
        lines.append(f"artifact_id: {artifact_id}")
    for key in ("duration_ms", "iterations", "tool_call_count"):
        if payload.get(key) is not None:
            lines.append(f"{key}: {payload[key]}")
    if bool(payload.get("timed_out", False)):
        lines.append("timed_out: true")
    if bool(payload.get("detach_from_parent", False)):
        lines.append("detach_from_parent: true")
    lines.extend(("summary:", summary or "(no summary)", "</task-notification>"))
    return "\n".join(lines)


async def inject_parent_notifications(
    *,
    ctx: ContextBuilder,
    state: AgentState,
    metadata: dict[str, Any],
    runtime: AgentRuntime | None,
    parent_run_id: str = "",
    conversation_id: str = "",
    emit_event: Any | None = None,
) -> int:
    """Inject durable child completion notifications into a parent turn."""
    if not _mailbox_deliverable(metadata):
        return 0
    agent_mode = str(metadata.get("agent_mode") or "").strip().lower()
    agent_role = str(metadata.get("agent_role") or metadata.get("role") or "main").strip().lower()
    if agent_mode == "subagent" or agent_role in {"subagent", "side_query", "background"}:
        return 0
    if runtime is None:
        return 0

    parent_run_id = str(parent_run_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if not parent_run_id and not conversation_id:
        return 0
    await _handle_parent_plan_approval_requests(
        runtime=runtime,
        parent_run_id=parent_run_id,
        conversation_id=conversation_id,
        emit_event=emit_event,
        metadata=metadata,
    )

    list_notifications = getattr(runtime, "list_parent_notifications", None)
    ack_notification = getattr(runtime, "ack_parent_notification", None)
    mark_delivered = getattr(runtime, "mark_parent_notification_delivered", None)
    mark_failed = getattr(runtime, "mark_parent_notification_failed", None)
    if not callable(list_notifications) or not callable(ack_notification):
        return 0
    try:
        notifications = list_notifications(
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        logger.debug("list parent notifications failed: %s", exc)
        return 0
    prompt_context = state.prompt_context if isinstance(state.prompt_context, dict) else {}
    already_injected = {
        str(value or "").strip()
        for value in prompt_context.get("delivered_parent_notification_ids", [])
        if str(value or "").strip()
    }
    pending = [
        item for item in notifications
        if isinstance(item, dict)
        and str(item.get("status") or "pending") in {"pending", "delivered", "failed"}
        and str(item.get("notification_id") or "").strip() not in already_injected
    ]
    if not pending:
        return 0

    injected = 0
    for item in pending:
        notification_id = str(item.get("notification_id") or "").strip()
        if not notification_id:
            continue
        subagent_id = str(item.get("subagent_id") or "").strip()
        item_epoch = int(item.get("mailbox_epoch") or 0)
        if subagent_id and item_epoch:
            accepts_notification = getattr(runtime, "accepts_parent_notification", None)
            if callable(accepts_notification):
                accepted = bool(accepts_notification(
                    subagent_id=subagent_id,
                    mailbox_epoch=item_epoch,
                    parent_run_id=parent_run_id,
                ))
            else:
                get_subagent = getattr(runtime, "get_subagent", None)
                current = get_subagent(subagent_id) if callable(get_subagent) else None
                current_epoch = int(getattr(current, "mailbox_epoch", 0) or 0) if current else 0
                accepted = not current_epoch or current_epoch == item_epoch
            if not accepted:
                ack_notification(
                    notification_id,
                    parent_run_id=parent_run_id,
                    conversation_id=conversation_id,
                )
                continue
        try:
            ctx.append_user(format_parent_notification_message(item))
            if subagent_id:
                raw_collected = prompt_context.get("collected_subagent_ids", [])
                collected = {
                    str(value or "").strip()
                    for value in raw_collected
                    if str(value or "").strip()
                } if isinstance(raw_collected, (list, tuple, set)) else set()
                collected.add(subagent_id)
                prompt_context["collected_subagent_ids"] = sorted(collected)
            already_injected.add(notification_id)
            prompt_context["delivered_parent_notification_ids"] = sorted(already_injected)
            state.prompt_context = prompt_context
            if callable(mark_delivered):
                mark_delivered(
                    notification_id,
                    parent_run_id=parent_run_id,
                    conversation_id=conversation_id,
                )
            else:
                # Compatibility for older/headless runtimes without the
                # delivered state. Production acknowledges only after a model
                # request has consumed the notification.
                ack_notification(
                    notification_id,
                    parent_run_id=parent_run_id,
                    conversation_id=conversation_id,
                )
            injected += 1
        except Exception as exc:
            if callable(mark_failed):
                mark_failed(
                    notification_id,
                    str(exc),
                    parent_run_id=parent_run_id,
                    conversation_id=conversation_id,
                )
            logger.debug("parent notification inject failed for %s: %s", notification_id, exc)

    if injected:
        state.mark_transition(
            "parent_notification_inject",
            message_count=injected,
            parent_run_id=parent_run_id,
            conversation_id=conversation_id,
        )
        if emit_event is not None:
            try:
                await emit_event(
                    "parent.notifications",
                    {
                        "count": injected,
                        "parent_run_id": parent_run_id,
                        "conversation_id": conversation_id,
                    },
                )
            except Exception as exc:
                logger.debug("parent notification event failed: %s", exc)
    return injected
