"""Runtime validation for session/conversation websocket projections.

The wire registry names the events, but the reconnect boundary also needs to
validate the nested snapshots carried by those events.  These checks are kept
small and deterministic so a malformed or unexpectedly large persisted
conversation cannot reach the browser merely because it came from an internal
producer rather than an ``AgentEvent`` factory.
"""

from __future__ import annotations

import math
from typing import Any


MAX_ID_CHARS = 1_024
MAX_SESSION_REPLAY_EVENTS = 1_000
MAX_CONVERSATIONS = 4_096
MAX_TRANSCRIPT_MESSAGES = 4_096
MAX_STREAM_RESUME_TOOLS = 512
MAX_STREAM_RESUME_BLOCKS = 1_024
MAX_GOAL_TEXT_CHARS = 4_096
MAX_JSON_NODES = 65_536
MAX_JSON_DEPTH = 20
MAX_JSON_STRING_CHARS = 16_777_216

SESSION_PROJECTION_EVENT_TYPES = frozenset(
    {
        "approval.cancelled",
        "conversation.list",
        "conversation.switched",
        "goal.updated",
        "session.replay",
        "session.restored",
        "session.synced",
        "stream_resume",
    }
)

# Protocol-sync uses this explicit partition as the review boundary for
# session projection invariants.  Every event in SESSION_PROJECTION_EVENT_TYPES
# must be acknowledged here: either it has a validator branch below, or it is
# deliberately documented as carrying no additional nested invariant.  Keep
# this registry beside the runtime validator so adding a projection event
# cannot silently bypass the reconnect boundary.
SESSION_PROJECTION_EVENTS_WITH_VALIDATION = frozenset(
    {
        "approval.cancelled",
        "conversation.list",
        "conversation.switched",
        "goal.updated",
        "session.replay",
        "session.restored",
        "session.synced",
        "stream_resume",
    }
)
SESSION_PROJECTION_EVENTS_WITHOUT_EXTRA_VALIDATION = frozenset()


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    clean = value.strip()
    if required and not clean:
        raise ValueError(f"{field} is required")
    return clean


def _optional_text(value: Any, field: str, maximum: int) -> None:
    if value is None:
        return
    _text(value, field, maximum)


def _non_negative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    if value > 9_007_199_254_740_991:
        raise ValueError(f"{field} exceeds the JavaScript safe integer range")
    return value


def _bounded_json(
    value: Any,
    *,
    field: str,
    max_nodes: int = MAX_JSON_NODES,
    max_depth: int = MAX_JSON_DEPTH,
    max_string_chars: int = MAX_JSON_STRING_CHARS,
    max_array_items: int = 8_192,
    max_object_items: int = 4_096,
) -> None:
    pending: list[tuple[Any, int, str]] = [(value, 0, field)]
    nodes = 0
    string_chars = 0
    while pending:
        current, depth, current_path = pending.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError(f"{current_path} exceeds the JSON structure budget")
        if isinstance(current, str):
            string_chars += len(current)
            if string_chars > max_string_chars:
                raise ValueError(f"{current_path} exceeds the JSON string budget")
            continue
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, (int, float)):
            if isinstance(current, float) and not math.isfinite(current):
                raise ValueError(f"{field} contains a non-finite number")
            continue
        if isinstance(current, list):
            if len(current) > max_array_items:
                raise ValueError(f"{current_path} contains too many list items")
            pending.extend(
                (item, depth + 1, f"{current_path}[{index}]")
                for index, item in enumerate(current)
            )
            continue
        if not isinstance(current, dict):
            raise ValueError(
                f"{current_path} contains a non-JSON value ({type(current).__name__})"
            )
        if len(current) > max_object_items:
            raise ValueError(f"{current_path} contains too many object fields")
        for key, item in current.items():
            _text(key, f"{current_path} key", 1_024, required=True)
            string_chars += len(key)
            if string_chars > max_string_chars:
                raise ValueError(f"{current_path} exceeds the JSON string budget")
            pending.append((item, depth + 1, f"{current_path}.{key}"))


def _record(value: Any, field: str, *, large: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    _bounded_json(
        value,
        field=field,
        max_nodes=MAX_JSON_NODES if large else 16_384,
        max_depth=MAX_JSON_DEPTH if large else 18,
        max_string_chars=MAX_JSON_STRING_CHARS if large else 4_194_304,
    )
    return value


def _optional_iso(value: Any, field: str) -> None:
    if value is None:
        return
    text = _text(value, field, 256)
    if text:
        from datetime import datetime

        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO timestamp") from exc


def _goal(value: Any, field: str = "goal") -> None:
    record = _record(value, field)
    for name, maximum in (
        ("id", MAX_ID_CHARS),
        ("text", MAX_GOAL_TEXT_CHARS),
        ("status", 64),
        ("created_at", 256),
        ("updated_at", 256),
        ("source", 256),
    ):
        if name in record and record[name] is not None:
            _text(record[name], f"{field}.{name}", maximum)
    if str(record.get("status") or "") and str(record.get("status")) not in {"active", "paused"}:
        raise ValueError(f"{field}.status is invalid")
    _optional_iso(record.get("created_at"), f"{field}.created_at")
    _optional_iso(record.get("updated_at"), f"{field}.updated_at")


def _conversation_summary(value: Any, field: str) -> dict[str, Any]:
    record = _record(value, field, large=True)
    _text(record.get("id"), f"{field}.id", MAX_ID_CHARS, required=True)
    for name, maximum in (
        ("title", 4_096),
        ("created_at", 256),
        ("updated_at", 256),
        ("archived_at", 256),
        ("workspace_root", 32_768),
        ("git_branch", 4_096),
        ("worktree_path", 32_768),
        ("summary", 65_536),
        ("parent_conversation_id", MAX_ID_CHARS),
        ("fork_id", MAX_ID_CHARS),
        ("branch_kind", 256),
        ("merged_into_conversation_id", MAX_ID_CHARS),
        ("merged_at", 256),
    ):
        if name in record and record[name] is not None:
            _text(record[name], f"{field}.{name}", maximum)
    for name in ("created_at", "updated_at", "archived_at", "merged_at"):
        _optional_iso(record.get(name), f"{field}.{name}")
    if record.get("conversation_type") not in (None, "main", "side_chat"):
        raise ValueError(f"{field}.conversation_type is invalid")
    if record.get("memory_mode") not in (None, "enabled", "disabled", "polluted"):
        raise ValueError(f"{field}.memory_mode is invalid")
    for name in ("memory_polluted", "archived", "git_isolated"):
        if name in record and not isinstance(record[name], bool):
            raise ValueError(f"{field}.{name} must be boolean")
    for name in ("revision", "message_count", "parent_message_index"):
        if name in record and record[name] is not None:
            _non_negative_int(record[name], f"{field}.{name}")
    if "memory_pollution_sources" in record and record["memory_pollution_sources"] is not None:
        sources = record["memory_pollution_sources"]
        if not isinstance(sources, list) or len(sources) > 256:
            raise ValueError(f"{field}.memory_pollution_sources is too large")
        cleaned = [_text(item, f"{field}.memory_pollution_sources[]", 1_024, required=True) for item in sources]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError(f"{field}.memory_pollution_sources contains duplicates")
    if "goal" in record and record["goal"] is not None:
        _goal(record["goal"], f"{field}.goal")
    return record


def _conversation_record(value: Any, field: str) -> dict[str, Any]:
    record = _conversation_summary(value, field)
    for name in ("transcript", "messages"):
        if name not in record or record[name] is None:
            continue
        messages = record[name]
        if not isinstance(messages, list) or len(messages) > MAX_TRANSCRIPT_MESSAGES:
            raise ValueError(f"{field}.{name} is too large")
        for index, message in enumerate(messages):
            _record(message, f"{field}.{name}[{index}]")
    if "permission_deny_rules" in record and record["permission_deny_rules"] is not None:
        rules = record["permission_deny_rules"]
        if not isinstance(rules, list) or len(rules) > 512:
            raise ValueError(f"{field}.permission_deny_rules is too large")
        for rule in rules:
            _text(rule, f"{field}.permission_deny_rules[]", 4_096, required=True)
    if "permission_overrides" in record and record["permission_overrides"] is not None:
        _record(record["permission_overrides"], f"{field}.permission_overrides")
    if "context_snapshot" in record and record["context_snapshot"] is not None:
        _record(record["context_snapshot"], f"{field}.context_snapshot", large=True)
    return record


def _runtime_snapshot(value: Any, field: str = "session") -> dict[str, Any]:
    record = _record(value, field, large=True)
    for name, maximum in (
        ("session_id", MAX_ID_CHARS),
        ("parent_session_id", MAX_ID_CHARS),
        ("active_conversation_id", MAX_ID_CHARS),
        ("active_task_id", MAX_ID_CHARS),
        ("workspace_root", 32_768),
        ("selected_model", 4_096),
        ("permission_mode", 256),
        ("permission_profile", 256),
        ("permission_source", 256),
        ("workspace_scope", 256),
    ):
        if name in record and record[name] is not None:
            _text(record[name], f"{field}.{name}", maximum)
    for name in ("active_stream_conversation_ids", "invoked_skill_names"):
        if name in record and record[name] is not None:
            values = record[name]
            if not isinstance(values, list) or len(values) > MAX_CONVERSATIONS:
                raise ValueError(f"{field}.{name} is too large")
            for item in values:
                _text(item, f"{field}.{name}[]", 4_096, required=True)
    for name in ("pending_approvals", "queued_user_messages", "pending_turn_inputs", "forks", "running_tasks"):
        if name in record and record[name] is not None:
            values = record[name]
            if not isinstance(values, list) or len(values) > MAX_CONVERSATIONS:
                raise ValueError(f"{field}.{name} is too large")
            for index, item in enumerate(values):
                _record(item, f"{field}.{name}[{index}]")
    if "pending_approval_count" in record:
        _non_negative_int(record["pending_approval_count"], f"{field}.pending_approval_count")
    if "active_conversation" in record and record["active_conversation"] is not None:
        _conversation_record(record["active_conversation"], f"{field}.active_conversation")
        active_id = str(record.get("active_conversation_id") or "").strip()
        nested_id = str(record["active_conversation"].get("id") or "").strip()
        if active_id and active_id != nested_id:
            raise ValueError(f"{field}.active_conversation does not match active_conversation_id")
    for name in ("task_summary", "capabilities", "provider_capabilities", "sandbox_status", "mcp"):
        if name in record and record[name] is not None:
            _record(record[name], f"{field}.{name}")
    return record


def _stream_tool(value: Any, field: str, *, pending: bool) -> dict[str, Any]:
    record = _record(value, field)
    _text(record.get("id"), f"{field}.id", MAX_ID_CHARS, required=True)
    _text(record.get("name"), f"{field}.name", MAX_ID_CHARS, required=True)
    if pending:
        _record(record.get("args"), f"{field}.args")
    elif "args" in record and record["args"] is not None:
        _record(record["args"], f"{field}.args")
    for name in ("status", "transition", "waiting_on", "waitingOn", "blocking_reason", "blockingReason", "display_hint", "displayHint", "input_summary", "inputSummary", "iteration_id", "iterationId", "phase"):
        if name in record and record[name] is not None:
            _text(record[name], f"{field}.{name}", 4_096)
    for name in ("outputPreview", "stdoutPreview", "stderrPreview"):
        if name in record and record[name] is not None:
            _text(record[name], f"{field}.{name}", 1_048_576)
    for name in ("started_at", "startedAt", "finished_at", "finishedAt", "duration_ms", "durationMs"):
        if name in record and record[name] is not None:
            _non_negative_int(record[name], f"{field}.{name}")
    return record


def _sequence_fields(payload: dict[str, Any]) -> None:
    if "last_seq" in payload:
        _non_negative_int(payload["last_seq"], "last_seq")
    if "current_seq" in payload:
        _non_negative_int(payload["current_seq"], "current_seq")
    if "last_seq" in payload and "current_seq" in payload and payload["current_seq"] < payload["last_seq"]:
        raise ValueError("current_seq cannot precede last_seq")
    if "replayed_events" in payload:
        _non_negative_int(payload["replayed_events"], "replayed_events")


# Canonical non-replayable event classification, shared by the staging gate
# (ws.handler) and the session.replay contract validation below. cc keeps one
# replay filter on its bridge; a single definition here prevents the three
# hand-maintained lists from drifting apart.
NON_REPLAYABLE_EVENT_TYPES = frozenset({
    "artifact_content",
    "conversation.list",
    "conversation.switched",
    "llm.model.updated",
    "mcp_status",
    "pong",
    "runtime.capabilities",
    "session.restored",
    "session.synced",
    "stream_resume",
    "stream_event",
})


def is_non_replayable_event_type(event_type: object) -> bool:
    normalized = str(event_type or "").strip()
    return (
        not normalized
        or normalized == "session.replay"
        or normalized.startswith("session.")
        or normalized in NON_REPLAYABLE_EVENT_TYPES
    )


def validate_session_projection_payload(payload: dict[str, Any]) -> None:
    """Raise ``ValueError`` when a session/conversation projection is invalid."""

    event_type = str(payload.get("type") or "").strip()
    if event_type not in SESSION_PROJECTION_EVENT_TYPES:
        return
    if event_type == "approval.cancelled":
        _text(payload.get("conversation_id"), "conversation_id", MAX_ID_CHARS, required=True)
        request_ids = payload.get("request_ids")
        if not isinstance(request_ids, list) or not request_ids or len(request_ids) > 512:
            raise ValueError("approval.cancelled.request_ids must be a non-empty bounded list")
        cleaned = [_text(item, "request_ids[]", MAX_ID_CHARS, required=True) for item in request_ids]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("approval.cancelled.request_ids contains duplicates")
        if "reason" in payload:
            _optional_text(payload["reason"], "reason", 256)
        return
    if event_type == "goal.updated":
        _text(payload.get("conversation_id"), "conversation_id", MAX_ID_CHARS, required=True)
        _goal(payload.get("goal"))
        if "source" in payload:
            _optional_text(payload["source"], "source", 256)
        _optional_iso(payload.get("updated_at"), "updated_at")
        if "revision" in payload:
            _non_negative_int(payload["revision"], "revision")
        return
    if event_type == "stream_resume":
        _text(payload.get("conversation_id"), "conversation_id", MAX_ID_CHARS, required=True)
        if "message_id" not in payload:
            raise ValueError("stream_resume.message_id is required")
        if payload["message_id"] is not None:
            _text(payload["message_id"], "message_id", MAX_ID_CHARS, required=True)
        pending = payload.get("tool_calls_pending")
        if not isinstance(pending, list) or len(pending) > MAX_STREAM_RESUME_TOOLS:
            raise ValueError("stream_resume.tool_calls_pending is invalid")
        pending_ids: set[str] = set()
        for index, item in enumerate(pending):
            record = _stream_tool(item, f"tool_calls_pending[{index}]", pending=True)
            if record["id"] in pending_ids:
                raise ValueError("stream_resume.tool_calls_pending contains duplicate ids")
            pending_ids.add(record["id"])
        for name in ("tool_states",):
            if name in payload and payload[name] is not None:
                states = payload[name]
                if not isinstance(states, list) or len(states) > MAX_STREAM_RESUME_TOOLS:
                    raise ValueError(f"stream_resume.{name} is invalid")
                state_ids: set[str] = set()
                for index, item in enumerate(states):
                    record = _stream_tool(item, f"{name}[{index}]", pending=False)
                    if record["id"] in state_ids:
                        raise ValueError(f"stream_resume.{name} contains duplicate ids")
                    state_ids.add(record["id"])
        if "content_blocks" in payload and payload["content_blocks"] is not None:
            blocks = payload["content_blocks"]
            if not isinstance(blocks, list) or len(blocks) > MAX_STREAM_RESUME_BLOCKS:
                raise ValueError("stream_resume.content_blocks is invalid")
            for index, block in enumerate(blocks):
                record = _record(block, f"content_blocks[{index}]")
                _text(record.get("type"), f"content_blocks[{index}].type", 128, required=True)
        for name, maximum in (("turn_id", MAX_ID_CHARS), ("phase", 256), ("stream_status", 256), ("last_event_type", 256)):
            if name in payload and payload[name] is not None:
                _optional_text(payload[name], name, maximum)
        if "event_seq" in payload:
            _non_negative_int(payload["event_seq"], "event_seq")
        return
    if event_type == "conversation.list":
        has_inventory_instance = "inventory_instance_id" in payload
        has_inventory_revision = "inventory_revision" in payload
        if has_inventory_instance != has_inventory_revision:
            raise ValueError(
                "conversation.list inventory instance and revision must be provided together"
            )
        if "inventory_instance_id" in payload:
            _text(
                payload["inventory_instance_id"],
                "inventory_instance_id",
                128,
                required=True,
            )
        if "inventory_revision" in payload:
            _non_negative_int(payload["inventory_revision"], "inventory_revision")
        conversations = payload.get("conversations")
        if not isinstance(conversations, list) or len(conversations) > MAX_CONVERSATIONS:
            raise ValueError("conversation.list.conversations is invalid")
        ids: set[str] = set()
        for index, conversation in enumerate(conversations):
            record = _conversation_summary(conversation, f"conversations[{index}]")
            identifier = str(record["id"]).strip()
            if identifier in ids:
                raise ValueError("conversation.list contains duplicate conversation ids")
            ids.add(identifier)
        active_id = payload.get("active_conversation_id")
        if active_id is not None:
            _text(active_id, "active_conversation_id", MAX_ID_CHARS, required=True)
            if str(active_id).strip() not in ids:
                raise ValueError("conversation.list active conversation is not present in conversations")
        active = payload.get("active_conversation")
        if active is not None:
            record = _conversation_record(active, "active_conversation")
            if active_id is not None and str(record["id"]).strip() != str(active_id).strip():
                raise ValueError("conversation.list active_conversation does not match active_conversation_id")
        if "conversation_id" in payload and payload["conversation_id"] is not None:
            _optional_text(payload["conversation_id"], "conversation_id", MAX_ID_CHARS)
        if payload.get("session") is not None:
            _runtime_snapshot(payload["session"])
        _optional_iso(payload.get("snapshot_at"), "snapshot_at")
        return
    if event_type == "conversation.switched":
        identifier = payload.get("conversation_id")
        conversation = payload.get("conversation")
        if identifier is None and conversation is None:
            raise ValueError("conversation.switched requires an owner or conversation")
        if identifier is not None:
            _text(identifier, "conversation_id", MAX_ID_CHARS, required=True)
        if conversation is not None:
            record = _conversation_record(conversation, "conversation")
            if identifier is not None and str(record["id"]).strip() != str(identifier).strip():
                raise ValueError("conversation.switched owner does not match conversation.id")
        if "is_hydrating" in payload and not isinstance(payload["is_hydrating"], bool):
            raise ValueError("conversation.switched.is_hydrating must be boolean")
        if payload.get("session") is not None:
            _runtime_snapshot(payload["session"])
        _optional_iso(payload.get("snapshot_at"), "snapshot_at")
        return
    if event_type in {"session.restored", "session.synced"}:
        for name, maximum in (
            ("session_id", MAX_ID_CHARS),
            ("working_directory", 32_768),
            ("workspace_root", 32_768),
            ("model", 4_096),
            ("current_model", 4_096),
            ("provider", 256),
            ("provider_id", MAX_ID_CHARS),
            ("base_url", 4_096),
            ("wire_api", 256),
            ("models_source", 256),
        ):
            if name in payload and payload[name] is not None:
                _optional_text(payload[name], name, maximum)
        for name in (
            "restored",
            "synced",
            "conversation_switched_follows",
            "missed_events",
            "event_log_gap",
            "snapshot_required",
            "cursor_reset",
        ):
            if name in payload and not isinstance(payload[name], bool):
                raise ValueError(f"{name} must be boolean")
        active_id = payload.get("active_conversation_id")
        if active_id is not None:
            _text(active_id, "active_conversation_id", MAX_ID_CHARS, required=True)
        for name in ("conversation", "active_conversation"):
            if payload.get(name) is not None:
                record = _conversation_record(payload[name], name)
                if active_id is not None and str(record["id"]).strip() != str(active_id).strip():
                    raise ValueError(f"{name} does not match active_conversation_id")
        if payload.get("workspace") is not None:
            workspace = payload["workspace"]
            if not isinstance(workspace, dict):
                raise ValueError("workspace must be an object")
            _record(workspace, "workspace")
            if workspace.get("root_path") is not None:
                _text(workspace["root_path"], "workspace.root_path", 32_768)
        if "available_models" in payload and payload["available_models"] is not None:
            models = payload["available_models"]
            if not isinstance(models, list) or len(models) > 4_096:
                raise ValueError("available_models is too large")
            for model in models:
                _text(model, "available_models[]", 4_096, required=True)
        if "messages" in payload and payload["messages"] is not None:
            messages = payload["messages"]
            if not isinstance(messages, list) or len(messages) > MAX_TRANSCRIPT_MESSAGES:
                raise ValueError("messages is too large")
            for index, message in enumerate(messages):
                _record(message, f"messages[{index}]")
        if payload.get("error") is not None:
            _optional_text(payload["error"], "error", 65_536)
        _sequence_fields(payload)
        replayed_event_count = 0
        if "replayed_events" in payload:
            replayed_event_count = _non_negative_int(
                payload["replayed_events"],
                "replayed_events",
            )
        requested_last_seq = None
        if "requested_last_seq" in payload:
            requested_last_seq = _non_negative_int(
                payload["requested_last_seq"],
                "requested_last_seq",
            )
        if payload.get("event_log_gap") and not payload.get("snapshot_required"):
            raise ValueError("event_log_gap requires snapshot_required")
        if payload.get("cursor_reset"):
            if "last_seq" not in payload or "current_seq" not in payload:
                raise ValueError("cursor_reset requires last_seq and current_seq")
            if payload["last_seq"] != payload["current_seq"]:
                raise ValueError("cursor_reset must rebase last_seq to current_seq")
            if requested_last_seq is None or requested_last_seq <= payload["current_seq"]:
                raise ValueError("cursor_reset requires a newer requested_last_seq")
            if not payload.get("snapshot_required"):
                raise ValueError("cursor_reset requires snapshot_required")
            if replayed_event_count != 0:
                raise ValueError("cursor_reset cannot include replayed events")
        elif requested_last_seq is not None and "last_seq" in payload:
            if requested_last_seq != payload["last_seq"]:
                raise ValueError("requested_last_seq must match last_seq")
        if payload.get("session") is not None:
            _runtime_snapshot(payload["session"])
        _optional_iso(payload.get("snapshot_at"), "snapshot_at")
        return
    if event_type == "session.replay":
        events = payload.get("events")
        if not isinstance(events, list) or len(events) > MAX_SESSION_REPLAY_EVENTS:
            raise ValueError("session.replay.events is invalid")
        for field in ("last_seq", "current_seq", "replayed_events"):
            if field not in payload:
                raise ValueError(f"session.replay.{field} is required")
        _sequence_fields(payload)
        replayed_events = _non_negative_int(
            payload["replayed_events"],
            "replayed_events",
        )
        if replayed_events != len(events):
            raise ValueError("session.replay.replayed_events does not match events")
        expected_previous_seq = payload["last_seq"]
        for index, event in enumerate(events):
            record = _record(event, f"events[{index}]")
            if is_non_replayable_event_type(record.get("type")):
                raise ValueError("session.replay contains a non-replayable event")
            if "seq" not in record or "previous_replay_seq" not in record:
                raise ValueError("session.replay event is missing its durable chain link")
            seq = _non_negative_int(record["seq"], f"events[{index}].seq")
            previous_replay_seq = _non_negative_int(
                record["previous_replay_seq"],
                f"events[{index}].previous_replay_seq",
            )
            if seq <= previous_replay_seq:
                raise ValueError("session.replay event seq must follow previous_replay_seq")
            if previous_replay_seq != expected_previous_seq:
                raise ValueError("session.replay durable chain is discontinuous")
            if seq > payload["current_seq"]:
                raise ValueError("session.replay event exceeds current_seq")
            expected_previous_seq = seq
        if events and expected_previous_seq != payload["current_seq"]:
            raise ValueError("session.replay does not reach current_seq")
        if not events and payload["last_seq"] != payload["current_seq"]:
            raise ValueError("empty session.replay cannot advance current_seq")


__all__ = [
    "NON_REPLAYABLE_EVENT_TYPES",
    "SESSION_PROJECTION_EVENT_TYPES",
    "SESSION_PROJECTION_EVENTS_WITH_VALIDATION",
    "SESSION_PROJECTION_EVENTS_WITHOUT_EXTRA_VALIDATION",
    "is_non_replayable_event_type",
    "validate_session_projection_payload",
]
