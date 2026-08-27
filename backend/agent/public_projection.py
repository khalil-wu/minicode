"""Allowlisted public projections for Agent runtime records.

Durable runtime records intentionally contain process ownership and recovery
fences that must never cross WebSocket, transcript, or model-facing result
boundaries.  These helpers keep persistence lossless while making every public
projection explicit and secret-redacted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from backend.secret_redaction import redact_secrets


_PUBLIC_USAGE_COUNT_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_deleted_input_tokens",
    "reasoning_output_tokens",
    "ordinary_input_tokens",
    "iterations",
    "prompt_cache_total_tokens",
)


def public_text(value: Any, *, max_chars: int = 50_000, single_line: bool = False) -> str:
    rendered = redact_secrets(str(value or ""))
    if single_line:
        rendered = " ".join(rendered.split())
    if len(rendered) <= max_chars:
        return rendered
    if max_chars <= 3:
        return rendered[:max_chars]
    return rendered[: max_chars - 3] + "..."


def _public_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def project_public_usage(value: Any) -> dict[str, Any]:
    """Return the provider-neutral public usage envelope.

    Unknown keys are deliberately dropped.  Provider response bodies,
    headers, diagnostics, credentials, and arbitrary nested objects therefore
    cannot piggyback on a subagent completion event.
    """

    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in _PUBLIC_USAGE_COUNT_FIELDS:
        if key not in source:
            continue
        count = _public_nonnegative_int(source.get(key))
        if count is not None:
            projected[key] = count

    if "cost_usd" in source and not isinstance(source.get("cost_usd"), bool):
        try:
            cost = float(source.get("cost_usd"))
        except (TypeError, ValueError):
            cost = -1.0
        if math.isfinite(cost) and cost >= 0:
            projected["cost_usd"] = cost

    if "prompt_cache_hit_rate" in source and not isinstance(
        source.get("prompt_cache_hit_rate"), bool
    ):
        try:
            hit_rate = float(source.get("prompt_cache_hit_rate"))
        except (TypeError, ValueError):
            hit_rate = -1.0
        if math.isfinite(hit_rate) and 0 <= hit_rate <= 100:
            projected["prompt_cache_hit_rate"] = hit_rate

    if isinstance(source.get("input_includes_cache_read"), bool):
        projected["input_includes_cache_read"] = bool(
            source.get("input_includes_cache_read")
        )
    if isinstance(source.get("input_includes_cache_write"), bool):
        projected["input_includes_cache_write"] = bool(
            source.get("input_includes_cache_write")
        )

    for key in ("service_tier", "inference_geo", "speed"):
        rendered = public_text(source.get(key), max_chars=64, single_line=True)
        if rendered:
            projected[key] = rendered

    nested_counts = {
        "server_tool_use": ("web_search_requests", "web_fetch_requests"),
        "cache_creation": (
            "ephemeral_1h_input_tokens",
            "ephemeral_5m_input_tokens",
        ),
        "output_tokens_details": ("thinking_tokens",),
    }
    for container, fields in nested_counts.items():
        raw_nested = source.get(container)
        if not isinstance(raw_nested, Mapping):
            continue
        nested: dict[str, int] = {}
        for field in fields:
            count = _public_nonnegative_int(raw_nested.get(field))
            if count is not None:
                nested[field] = count
        if nested:
            projected[container] = nested
    return projected


_AGENT_RUN_PUBLIC_FIELDS = (
    "run_id",
    "conversation_id",
    "parent_run_id",
    "role",
    "phase",
    "status",
    "started_at",
    "completed_at",
    "cleanup_pending",
    "cleanup_reason",
    "cleanup_requested_at",
    "cleanup_completed_at",
    "task_id",
    "session_id",
    "summary",
    "terminal_reason",
    "error",
    "agent_path",
    "mailbox_epoch",
)

_SUBAGENT_RUN_PUBLIC_FIELDS = (
    "subagent_id",
    "parent_run_id",
    "agent_type",
    "role",
    "write_scope_strategy",
    "prompt_summary",
    "background",
    "task_id",
    "task_name",
    "objective",
    "depends_on",
    "blocked_by",
    "cancel_with_parent",
    "detach_from_parent",
    "read_only",
    "write_scope",
    "current_activity",
    "status",
    "tool_count",
    "result_summary",
    "checkpoint_id",
    "started_at",
    "completed_at",
    "cleanup_pending",
    "cleanup_reason",
    "cleanup_requested_at",
    "cleanup_completed_at",
    "agent_path",
    "mailbox_epoch",
    "teammate_name",
    "team_name",
    "permission_mode",
    "plan_mode_required",
    "awaiting_plan_approval",
    "active_plan_request_id",
    "is_idle",
    "background_task",
    "result_available",
    "cancel_requested",
    "session_id",
)

_PUBLIC_LONG_TEXT_FIELDS = frozenset(
    {
        "summary",
        "error",
        "prompt_summary",
        "objective",
        "current_activity",
        "result_summary",
        "cleanup_reason",
    }
)

_PUBLIC_BOOLEAN_FIELDS = frozenset(
    {
        "background",
        "cancel_with_parent",
        "detach_from_parent",
        "read_only",
        "plan_mode_required",
        "awaiting_plan_approval",
        "is_idle",
        "result_available",
        "cancel_requested",
        "cleanup_pending",
    }
)

_PUBLIC_INTEGER_FIELDS = frozenset(
    {
        "started_at",
        "completed_at",
        "mailbox_epoch",
        "tool_count",
        "cleanup_requested_at",
        "cleanup_completed_at",
    }
)

_PUBLIC_STRING_LIST_FIELDS = frozenset(
    {
        "depends_on",
        "blocked_by",
        "write_scope",
    }
)


def _allowlisted_mapping(value: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in fields:
        if key not in source:
            continue
        raw = source.get(key)
        if key in _PUBLIC_LONG_TEXT_FIELDS:
            projected[key] = public_text(raw, max_chars=12_000)
            continue
        if key in _PUBLIC_BOOLEAN_FIELDS:
            if isinstance(raw, bool):
                projected[key] = raw
            continue
        if key in _PUBLIC_INTEGER_FIELDS:
            if raw is None and key == "completed_at":
                projected[key] = None
                continue
            number = _public_nonnegative_int(raw)
            if number is not None:
                projected[key] = number
            continue
        if key in _PUBLIC_STRING_LIST_FIELDS:
            projected[key] = _public_string_list(raw)
            continue
        projected[key] = public_text(raw, max_chars=2_048, single_line=True)
    return projected


def project_public_agent_run(value: Any) -> dict[str, Any]:
    return _allowlisted_mapping(value, _AGENT_RUN_PUBLIC_FIELDS)


def project_public_subagent_run(value: Any) -> dict[str, Any]:
    return _allowlisted_mapping(value, _SUBAGENT_RUN_PUBLIC_FIELDS)


def project_public_subagent_result(
    value: Any,
    *,
    content_override: Any | None = None,
) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in (
        "subagent_id",
        "status",
        "artifact_id",
        "agent_path",
    ):
        if key in source:
            projected[key] = public_text(
                source.get(key),
                max_chars=2_048,
                single_line=True,
            )
    for key in (
        "duration_ms",
        "iterations",
        "tool_call_count",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "mailbox_epoch",
        "completed_at",
    ):
        count = _public_nonnegative_int(source.get(key))
        if count is not None:
            projected[key] = count
    if isinstance(source.get("timed_out"), bool):
        projected["timed_out"] = bool(source.get("timed_out"))
    projected["content"] = public_text(
        source.get("content") if content_override is None else content_override,
        max_chars=50_000,
    )
    projected["error"] = public_text(source.get("error"), max_chars=12_000)
    projected["terminal_reason"] = public_text(
        source.get("terminal_reason"),
        max_chars=256,
        single_line=True,
    )
    usage = project_public_usage(source.get("usage"))
    if usage:
        projected["usage"] = usage
    return projected


def project_public_swarm_message(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in (
        "message_id",
        "sender_id",
        "recipient_id",
        "conversation_id",
        "team_name",
        "task_id",
    ):
        if key in source:
            projected[key] = public_text(
                source.get(key),
                max_chars=2_048,
                single_line=True,
            )
    for key in (
        "sender_mailbox_epoch",
        "recipient_mailbox_epoch",
        "created_at",
        "seq",
    ):
        count = _public_nonnegative_int(source.get(key))
        if count is not None:
            projected[key] = count
    raw_epochs = source.get("recipient_mailbox_epochs")
    if isinstance(raw_epochs, Mapping):
        epochs: dict[str, int] = {}
        for raw_id, raw_epoch in list(raw_epochs.items())[:256]:
            participant_id = public_text(
                raw_id,
                max_chars=2_048,
                single_line=True,
            )
            mailbox_epoch = _public_nonnegative_int(raw_epoch)
            if participant_id and mailbox_epoch is not None:
                epochs[participant_id] = mailbox_epoch
        if epochs:
            projected["recipient_mailbox_epochs"] = epochs
    projected["content"] = public_text(source.get("content"), max_chars=12_000)
    return projected


def _public_string_list(value: Any, *, maximum: int = 256) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:maximum]:
        rendered = public_text(item, max_chars=2_048, single_line=True)
        if rendered and rendered not in result:
            result.append(rendered)
    return result


def project_public_swarm_task_output(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in ("output_id", "author_id"):
        if key in source:
            projected[key] = public_text(
                source.get(key),
                max_chars=2_048,
                single_line=True,
            )
    for key in ("created_at", "seq"):
        count = _public_nonnegative_int(source.get(key))
        if count is not None:
            projected[key] = count
    projected["content"] = public_text(source.get("content"), max_chars=12_000)
    return projected


def project_public_swarm_task(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in (
        "task_id",
        "assignee",
        "conversation_id",
        "agent_type",
        "role",
        "status",
        "priority",
        "team_name",
        "created_by",
    ):
        if key in source:
            projected[key] = public_text(
                source.get(key),
                max_chars=2_048,
                single_line=True,
            )
    if isinstance(source.get("read_only"), bool):
        projected["read_only"] = bool(source.get("read_only"))
    for key in ("created_at", "updated_at", "completed_at", "seq"):
        raw = source.get(key)
        if raw is None and key == "completed_at" and key in source:
            projected[key] = None
            continue
        count = _public_nonnegative_int(raw)
        if count is not None:
            projected[key] = count
    for key, maximum in (
        ("title", 2_000),
        ("description", 12_000),
        ("objective", 12_000),
    ):
        projected[key] = public_text(source.get(key), max_chars=maximum)
    for key in ("write_scope", "blocks", "blocked_by"):
        values = _public_string_list(source.get(key))
        if values:
            projected[key] = values
    raw_outputs = source.get("outputs")
    if isinstance(raw_outputs, list):
        projected["outputs"] = [
            project_public_swarm_task_output(item)
            for item in raw_outputs[:256]
            if isinstance(item, Mapping)
        ]
    return projected


def project_public_swarm_team_member(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in ("id", "role", "agent_type"):
        rendered = public_text(source.get(key), max_chars=256, single_line=True)
        if rendered:
            projected[key] = rendered
    projected["description"] = public_text(
        source.get("description"),
        max_chars=4_000,
    )
    return projected


def project_public_swarm_team(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    projected: dict[str, Any] = {}
    for key in (
        "team_id",
        "team_name",
        "conversation_id",
        "created_by",
    ):
        if key in source:
            projected[key] = public_text(
                source.get(key),
                max_chars=2_048,
                single_line=True,
            )
    for key in ("created_at", "updated_at", "seq", "deleted_at", "deleted_seq"):
        raw = source.get(key)
        if raw is None and key == "deleted_at" and key in source:
            projected[key] = None
            continue
        count = _public_nonnegative_int(raw)
        if count is not None:
            projected[key] = count
    projected["description"] = public_text(
        source.get("description"),
        max_chars=12_000,
    )
    raw_members = source.get("members")
    if isinstance(raw_members, list):
        projected["members"] = [
            project_public_swarm_team_member(item)
            for item in raw_members[:256]
            if isinstance(item, Mapping)
        ]
    return projected


_METRIC_GENERIC_STRING_FIELDS = frozenset(
    {
        "run_id",
        "parent_run_id",
        "subagent_id",
        "task_id",
        "session_id",
        "conversation_id",
        "notification_id",
        "notification_status",
        "status",
        "operation",
        "reason",
        "received_agent_path",
        "current_agent_path",
    }
)

_METRIC_GENERIC_INTEGER_FIELDS = frozenset(
    {
        "mailbox_epoch",
        "received_mailbox_epoch",
        "current_mailbox_epoch",
        "duration_ms",
        "iterations",
        "tool_call_count",
        "total_tokens",
        "run_count",
        "subagent_count",
        "task_count",
        "message_count",
        "team_count",
        # Generic test/extension metrics may use a stable integer index. Keep
        # it bounded like the other counters rather than dropping the whole
        # observation line.
        "index",
    }
)

_METRIC_GENERIC_BOOLEAN_FIELDS = frozenset(
    {
        "timed_out",
        "drained_to_completion",
        "removed",
    }
)


def project_public_metric_payload(event: Any, value: Any) -> dict[str, Any]:
    """Return the explicit, secret-redacted metrics payload for one event.

    Metrics are observability data rather than a recovery source. They must not
    retain runtime ownership fences, provider bodies, prompts, or arbitrary
    future record fields merely because a persistence ``to_dict`` grew.
    """

    event_name = public_text(event, max_chars=128, single_line=True)
    source = value if isinstance(value, Mapping) else {}
    if event_name in {"run_started", "phase_updated", "run_completed"}:
        return project_public_agent_run(source)
    if event_name in {"subagent_started", "subagent_completed"}:
        return project_public_subagent_run(source)
    if event_name == "swarm_message_sent":
        return project_public_swarm_message(source)
    if event_name in {
        "swarm_task_created",
        "swarm_task_updated",
        "swarm_task_output",
    }:
        return project_public_swarm_task(source)
    if event_name in {"swarm_team_created", "swarm_team_deleted"}:
        return project_public_swarm_team(source)

    projected: dict[str, Any] = {}
    for key in _METRIC_GENERIC_STRING_FIELDS:
        if key in source:
            projected[key] = public_text(
                source.get(key),
                max_chars=2_048 if key != "reason" else 12_000,
                single_line=key != "reason",
            )
    for key in _METRIC_GENERIC_INTEGER_FIELDS:
        count = _public_nonnegative_int(source.get(key))
        if count is not None:
            projected[key] = count
    for key in _METRIC_GENERIC_BOOLEAN_FIELDS:
        if isinstance(source.get(key), bool):
            projected[key] = bool(source.get(key))
    if isinstance(source.get("subagent_ids"), (list, tuple)):
        projected["subagent_ids"] = _public_string_list(
            source.get("subagent_ids"),
            maximum=512,
        )
    return projected


__all__ = [
    "project_public_agent_run",
    "project_public_metric_payload",
    "project_public_subagent_result",
    "project_public_subagent_run",
    "project_public_swarm_message",
    "project_public_swarm_task",
    "project_public_swarm_task_output",
    "project_public_swarm_team",
    "project_public_swarm_team_member",
    "project_public_usage",
    "public_text",
]
