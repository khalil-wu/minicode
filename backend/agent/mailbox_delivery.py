"""Iteration-boundary delivery for subagent and parent mailboxes."""

from __future__ import annotations

import logging
from typing import Any

from backend.agent.context import ContextBuilder
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.tools.base import truncate_tool_result


logger = logging.getLogger(__name__)


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
    current_started_at = int(getattr(participant_record, "started_at", 0) or 0)

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
        messages = [claim.message for claim in claims]
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
    ack_claims = getattr(runtime, "ack_swarm_message_claims", None)
    if claims and callable(ack_claims):
        acked = int(ack_claims(claims) or 0)
        if acked != len(claims):
            raise RuntimeError(
                f"mailbox acknowledgement was fenced: acked {acked} of {len(claims)} claims"
            )
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
    agent_mode = str(metadata.get("agent_mode") or metadata.get("agentMode") or "").strip().lower()
    agent_role = str(metadata.get("agent_role") or metadata.get("role") or "main").strip().lower()
    if agent_mode == "subagent" or agent_role in {"subagent", "side_query", "background"}:
        return 0
    if runtime is None:
        return 0

    parent_run_id = str(parent_run_id or "").strip()
    conversation_id = str(conversation_id or "").strip()
    if not parent_run_id and not conversation_id:
        return 0
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
        and str(item.get("status") or "pending") in {"pending", "failed", "delivered"}
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
