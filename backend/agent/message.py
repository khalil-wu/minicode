"""Agent event and WebSocket command conversion helpers.

Provider text with an explicit phase is projected at its source. Providers that
do not expose phases are resolved within one provider response: text preceding
a tool call is process output, while text from a response without tools is the
answer. Assistant text follows the same item lifecycle used by Codex and pi:
started, delta, completed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.agent.context_ledger import ContextLedger
from backend.agent.public_projection import (
    project_public_agent_run,
    project_public_usage,
    public_text,
)
from backend.agent.prompt_cache import prompt_cache_usage_stats
from backend.llm.base import ToolCallEvent
from backend.ws.events import ClientCommandType, ServerEventType


_MAX_EVENT_ID_CHARS = 1024
_MAX_STREAM_DELTA_CHARS = 1_048_576
_MAX_MESSAGE_TEXT_CHARS = 4_194_304
_MAX_EVENT_CONTENT_CHARS = 1_048_576
_MAX_EVENT_SUMMARY_CHARS = 65_536
_MAX_EVENT_METADATA_CHARS = 16_384
_MAX_EVENT_COLLECTION_ITEMS = 4_096
_MAX_EVENT_JSON_DEPTH = 12
_MAX_EVENT_JSON_NODES = 4_096
_MAX_EVENT_JSON_STRING_CHARS = 262_144
_MAX_SAFE_INTEGER = 9_007_199_254_740_991
_USER_MESSAGE_QUEUE_STATUSES = frozenset({"queued", "dequeued", "cancelled"})
_USER_MESSAGE_TURN_MODES = frozenset({"follow_up", "steer"})
_AGENT_MESSAGE_COMPLETION_STATUSES = frozenset(
    {"completed", "partial", "cancelled", "failed"}
)
_THINKING_LIFECYCLES = frozenset({"start", "delta", "end"})
_AGENT_ITEM_STATUSES = frozenset(
    {"running", "completed", "partial", "failed", "cancelled", "info", "retracted"}
)
_AGENT_ITEM_VISIBILITIES = frozenset({"timeline", "compact", "debug"})
_AGENT_PROGRESS_STAGES = frozenset(
    {
        "status",
        "planning",
        "tool",
        "approval",
        "verification",
        "image_generation",
        "cache",
        "final",
    }
)
_AGENT_PROGRESS_STATUSES = frozenset(
    {"running", "completed", "partial", "failed", "info"}
)
_AGENT_PROGRESS_PHASES = frozenset(
    {
        "orienting",
        "planning",
        "model",
        "tool",
        "image_generation",
        "approval",
        "verify",
        "final",
        "recover",
        "status",
        "iteration",
        "subagent",
        "cache",
    }
)
# Public protocol constants are consumed by the WebSocket/session projection;
# keep one backend source of truth for validation and persisted UI state.
AGENT_PROGRESS_STAGES = _AGENT_PROGRESS_STAGES
AGENT_PROGRESS_STATUSES = _AGENT_PROGRESS_STATUSES
AGENT_PROGRESS_PHASES = _AGENT_PROGRESS_PHASES
_DONE_STATUSES = frozenset({"completed", "partial", "cancelled", "failed"})


def _required_event_text(value: Any, *, field_name: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    if len(text) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return text


def _optional_event_text(value: Any, *, field_name: str, maximum: int) -> str:
    text = str(value or "").strip()
    if len(text) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    return text


def _bounded_error_message(value: Any, *, maximum: int) -> str:
    """Keep error reporting total even when an upstream exception is enormous."""
    text = str(value or "").strip() or "Unknown error"
    if len(text) <= maximum:
        return text
    marker = "\n...[error message truncated]...\n"
    retained = max(0, maximum - len(marker))
    head = retained // 2
    tail = retained - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


def _event_string(
    value: Any,
    *,
    field_name: str,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{field_name} exceeds {maximum} characters")
    if not allow_empty and not value.strip():
        raise ValueError(f"{field_name} is required")
    return value


def _non_negative_event_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < 0 or value > _MAX_SAFE_INTEGER:
        raise ValueError(f"{field_name} must be a non-negative safe integer")
    return value


def _non_negative_event_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return number


def _bounded_event_record(
    value: Any,
    *,
    field_name: str,
    max_nodes: int = _MAX_EVENT_JSON_NODES,
    max_depth: int = _MAX_EVENT_JSON_DEPTH,
    max_string_characters: int = _MAX_EVENT_JSON_STRING_CHARS,
    max_collection_items: int = _MAX_EVENT_COLLECTION_ITEMS,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be an object")
    pending: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    string_characters = 0
    while pending:
        current, depth = pending.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise ValueError(f"{field_name} exceeds the JSON structure budget")
        if isinstance(current, str):
            string_characters += len(current)
            if string_characters > max_string_characters:
                raise ValueError(f"{field_name} exceeds the JSON string budget")
            continue
        if current is None or isinstance(current, bool):
            continue
        if isinstance(current, (int, float)) and not isinstance(current, bool):
            if isinstance(current, float) and not math.isfinite(current):
                raise ValueError(f"{field_name} contains a non-finite number")
            continue
        if isinstance(current, list):
            if len(current) > max_collection_items:
                raise ValueError(f"{field_name} contains too many list items")
            pending.extend((item, depth + 1) for item in current)
            continue
        if not isinstance(current, dict):
            raise ValueError(f"{field_name} must contain JSON-compatible values")
        if len(current) > 1_024:
            raise ValueError(f"{field_name} contains too many object fields")
        for key, item in current.items():
            if not isinstance(key, str) or not key or len(key) > 1_024:
                raise ValueError(f"{field_name} contains an invalid object key")
            string_characters += len(key)
            if string_characters > max_string_characters:
                raise ValueError(f"{field_name} exceeds the JSON string budget")
            pending.append((item, depth + 1))
    return dict(value)


def _bounded_event_string_list(
    value: Any,
    *,
    field_name: str,
    maximum_items: int,
    maximum_item_chars: int,
) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")
    if len(value) > maximum_items:
        raise ValueError(f"{field_name} contains too many items")
    result = [
        _required_event_text(
            item,
            field_name=f"{field_name} item",
            maximum=maximum_item_chars,
        )
        for item in value
    ]
    if len(set(result)) != len(result):
        raise ValueError(f"{field_name} must not contain duplicates")
    return result


@dataclass
class AgentEvent:
    """Server-to-client event. See backend/ws/events.py for the full vocabulary."""

    type: ServerEventType
    data: dict[str, Any] = field(default_factory=dict)

    def to_ws_message(self) -> dict[str, Any]:
        """Serialize this event as WebSocket JSON."""
        return {"type": self.type, **self.data}

    @classmethod
    def user_message_queue_updated(
        cls,
        *,
        status: str,
        conversation_id: str,
        message_id: str,
        user_message_id: str = "",
        position: int = 0,
        reason: str = "",
        target_message_id: str = "",
        turn_mode: str = "",
    ) -> AgentEvent:
        clean_status = _required_event_text(
            status,
            field_name="status",
            maximum=32,
        ).lower()
        if clean_status not in _USER_MESSAGE_QUEUE_STATUSES:
            raise ValueError(f"Unsupported user-message queue status: {clean_status}")
        clean_conversation_id = _required_event_text(
            conversation_id,
            field_name="conversation_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_message_id = _required_event_text(
            message_id,
            field_name="message_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_user_message_id = _optional_event_text(
            user_message_id,
            field_name="user_message_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_reason = _optional_event_text(
            reason,
            field_name="reason",
            maximum=16_384,
        )
        clean_target_message_id = _optional_event_text(
            target_message_id,
            field_name="target_message_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_turn_mode = _optional_event_text(
            turn_mode,
            field_name="turn_mode",
            maximum=32,
        ).lower()
        if clean_turn_mode and clean_turn_mode not in _USER_MESSAGE_TURN_MODES:
            raise ValueError(f"Unsupported user-message turn mode: {clean_turn_mode}")
        if isinstance(position, bool) or not isinstance(position, int):
            raise ValueError("position must be an integer")
        if clean_status == "queued" and position <= 0:
            raise ValueError("queued user messages require a positive position")
        if clean_status != "queued" and position != 0:
            raise ValueError("position is only valid for queued user messages")
        if clean_turn_mode and clean_status != "dequeued":
            raise ValueError("turn_mode is only valid for dequeued user messages")
        if clean_turn_mode == "steer" and clean_reason != "steered_current_turn":
            raise ValueError("steered user messages require steered_current_turn reason")
        if clean_reason == "steered_current_turn" and clean_status != "dequeued":
            raise ValueError("steered_current_turn is only valid for dequeued user messages")
        data: dict[str, Any] = {
            "status": clean_status,
            "conversation_id": clean_conversation_id,
            "message_id": clean_message_id,
        }
        if clean_user_message_id:
            data["user_message_id"] = clean_user_message_id
        if position > 0:
            data["position"] = position
        if clean_reason:
            data["reason"] = clean_reason
        if clean_target_message_id:
            data["target_message_id"] = clean_target_message_id
        if clean_turn_mode:
            data["turn_mode"] = clean_turn_mode
        return cls(type="user_message.queue.updated", data=data)

    @classmethod
    def agent_message_started(
        cls,
        *,
        item_id: str = "agent-message",
        source: str = "",
    ) -> AgentEvent:
        clean_item_id = _required_event_text(
            item_id,
            field_name="item_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        data: dict[str, Any] = {
            "item": {
                "id": clean_item_id,
                "type": "agent_message",
                "text": "",
                "status": "in_progress",
            },
        }
        clean_source = _optional_event_text(source, field_name="source", maximum=256)
        if clean_source:
            data["item"]["source"] = clean_source
        return cls(
            type="item.started",
            data=data,
        )

    @classmethod
    def agent_message_delta(
        cls,
        delta: str,
        *,
        item_id: str = "agent-message",
        source: str = "",
    ) -> AgentEvent:
        clean_item_id = _required_event_text(
            item_id,
            field_name="item_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        if not isinstance(delta, str) or not delta:
            raise ValueError("delta must be a non-empty string")
        if len(delta) > _MAX_STREAM_DELTA_CHARS:
            raise ValueError(
                f"delta exceeds {_MAX_STREAM_DELTA_CHARS} characters"
            )
        data = {"item_id": clean_item_id, "delta": delta}
        clean_source = _optional_event_text(source, field_name="source", maximum=256)
        if clean_source:
            data["source"] = clean_source
        return cls(type="agent_message.delta", data=data)

    @classmethod
    def agent_message_completed(
        cls,
        text: str,
        *,
        item_id: str = "agent-message",
        source: str = "model_final",
        status: str = "completed",
        finish_reason: str = "",
        provider_raw: dict[str, Any] | None = None,
        tool_calls: list[ToolCallEvent] | None = None,
    ) -> AgentEvent:
        clean_item_id = _required_event_text(
            item_id,
            field_name="item_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_text = _event_string(
            text,
            field_name="text",
            maximum=_MAX_MESSAGE_TEXT_CHARS,
            allow_empty=True,
        )
        clean_source = _required_event_text(
            source,
            field_name="source",
            maximum=256,
        )
        clean_status = _required_event_text(
            status,
            field_name="status",
            maximum=32,
        ).lower()
        if clean_status not in _AGENT_MESSAGE_COMPLETION_STATUSES:
            raise ValueError(f"Unsupported agent-message completion status: {clean_status}")
        clean_finish_reason = _optional_event_text(
            finish_reason,
            field_name="finish_reason",
            maximum=_MAX_EVENT_METADATA_CHARS,
        )
        if provider_raw is not None:
            # Keep adapter-internal diagnostics lossless until the event
            # boundary, then rebuild the renderer/transcript envelope from the
            # explicit provider allowlist. A newly-added raw field must never
            # become public merely because it is JSON-compatible.
            from backend.agent.provider_protocol import provider_raw_for_projection

            clean_provider_raw = _bounded_event_record(
                provider_raw_for_projection(provider_raw),
                field_name="provider_raw",
            )
        else:
            clean_provider_raw = None
        item: dict[str, Any] = {
            "id": clean_item_id,
            "type": "agent_message",
            "text": clean_text,
            "source": clean_source,
            "status": clean_status,
        }
        data: dict[str, Any] = {"item": item}
        if tool_calls:
            # Keep provider-native tool blocks out of the visible MiniCode
            # answer item. Pi's bridge consumes this adapter-local metadata to
            # reconstruct its assistant message, while the host protocol
            # continues to represent text and tool execution separately.
            data["tool_calls"] = [
                {
                    "id": str(tool_call.id or ""),
                    "name": str(tool_call.name or ""),
                    "arguments": dict(tool_call.arguments or {}),
                }
                for tool_call in tool_calls
                if str(tool_call.id or "").strip()
            ]
        if clean_finish_reason:
            data["finish_reason"] = clean_finish_reason
        if clean_provider_raw:
            data["provider_raw"] = clean_provider_raw
        return cls(type="item.completed", data=data)

    @classmethod
    def thinking_chunk(
        cls,
        content: str,
        *,
        source: str = "",
        visibility: str = "",
        phase: str = "",
        item_id: str = "",
        content_index: int | None = None,
        lifecycle: str = "delta",
    ) -> AgentEvent:
        clean_lifecycle = _required_event_text(
            lifecycle,
            field_name="lifecycle",
            maximum=32,
        ).lower()
        if clean_lifecycle not in _THINKING_LIFECYCLES:
            raise ValueError(f"Unsupported thinking lifecycle: {clean_lifecycle}")
        clean_content = _event_string(
            content,
            field_name="content",
            maximum=_MAX_STREAM_DELTA_CHARS,
            allow_empty=clean_lifecycle in {"start", "end"},
        )
        clean_source = _optional_event_text(
            source,
            field_name="source",
            maximum=256,
        )
        clean_visibility = _optional_event_text(
            visibility,
            field_name="visibility",
            maximum=32,
        ).lower()
        if clean_visibility and clean_visibility not in _AGENT_ITEM_VISIBILITIES:
            raise ValueError(f"Unsupported thinking visibility: {clean_visibility}")
        clean_phase = _optional_event_text(
            phase,
            field_name="phase",
            maximum=64,
        )
        clean_item_id = _optional_event_text(
            item_id,
            field_name="item_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        data: dict[str, Any] = {
            "content": clean_content,
            "lifecycle": clean_lifecycle,
        }
        if clean_source:
            data["source"] = clean_source
        if clean_visibility:
            data["visibility"] = clean_visibility
        if clean_phase:
            data["phase"] = clean_phase
        if clean_item_id:
            data["item_id"] = clean_item_id
        if content_index is not None:
            data["content_index"] = _non_negative_event_int(
                content_index,
                field_name="content_index",
            )
        return cls(type="thinking_delta", data=data)

    @classmethod
    def image_chunk(cls, data: str, media_type: str = "image/png") -> AgentEvent:
        return cls(
            type="image_chunk", data={"image_data": data, "media_type": media_type}
        )

    @classmethod
    def tool_call(
        cls,
        id: str,
        name: str,
        args: dict[str, Any],
        *,
        status: str = "running",
        started_at: int | None = None,
        display_hint: str = "",
        result_kind: str = "",
        activity_kind: str = "",
        visibility: str = "timeline",
        group_id: str = "",
        step_id: str = "",
        turn_id: str = "",
        iteration_id: str = "",
        phase: str = "",
        side_effect_kind: str = "",
        idempotent: bool | None = None,
        idempotency_key: str = "",
        request_digest: str = "",
        diff: dict[str, Any] | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "id": id,
            "name": name,
            "args": args,
            "status": status,
        }
        if started_at is not None:
            data["started_at"] = started_at
        if display_hint:
            data["display_hint"] = display_hint
        if result_kind:
            data["result_kind"] = result_kind
        if activity_kind:
            data["activity_kind"] = activity_kind
        clean_visibility = _required_event_text(
            visibility,
            field_name="visibility",
            maximum=32,
        )
        if clean_visibility not in _AGENT_ITEM_VISIBILITIES:
            raise ValueError(f"Unsupported tool visibility: {clean_visibility}")
        data["visibility"] = clean_visibility
        if group_id:
            data["group_id"] = group_id
        if step_id:
            data["step_id"] = step_id
        if turn_id:
            data["turn_id"] = turn_id
        if iteration_id:
            data["iteration_id"] = iteration_id
        if phase:
            data["phase"] = phase
        if side_effect_kind:
            data["side_effect_kind"] = side_effect_kind
        if idempotent is not None:
            data["idempotent"] = idempotent
        if idempotency_key:
            data["idempotency_key"] = idempotency_key
        clean_request_digest = _optional_event_text(
            request_digest,
            field_name="request_digest",
            maximum=128,
        )
        if clean_request_digest:
            data["request_digest"] = clean_request_digest
        if isinstance(diff, dict) and diff:
            plus = max(0, int(diff.get("plus") or 0))
            minus = max(0, int(diff.get("minus") or 0))
            if plus or minus:
                data["diff"] = {"plus": plus, "minus": minus}
        return cls(type="tool_call", data=data)

    @classmethod
    def tool_output_delta(
        cls,
        id: str,
        output: str,
        *,
        stream: str = "stdout",
        turn_id: str = "",
        iteration_id: str = "",
        step_id: str = "",
    ) -> AgentEvent:
        """工具执行期间的增量输出（如命令的 stdout/stderr）。"""
        data = {"id": id, "output": output, "stream": stream}
        if turn_id:
            data["turn_id"] = turn_id
        if iteration_id:
            data["iteration_id"] = iteration_id
        if step_id:
            data["step_id"] = step_id
        return cls(
            type="tool_output_delta",
            data=data,
        )

    @classmethod
    def tool_result(
        cls,
        id: str,
        summary: str,
        artifact_id: str | None = None,
        is_error: bool = False,
        diff: Any | None = None,
        source_url: str | None = None,
        extraction_status: str | None = None,
        content_preview: str | None = None,
        evidence_type: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        display_summary: str = "",
        result_kind: str = "",
        activity_kind: str = "",
        visibility: str = "timeline",
        group_id: str = "",
        step_id: str = "",
        limitation: str = "",
        provider: str = "",
        provider_error_type: str = "",
        error_info: dict[str, Any] | None = None,
        error_kind: str = "",
        user_summary: str = "",
        developer_detail: str = "",
        recoverable: bool | None = None,
        projection: str = "",
        turn_id: str = "",
        iteration_id: str = "",
        phase: str = "",
        side_effect_kind: str = "",
        idempotent: bool | None = None,
        idempotency_key: str = "",
        cleanup_receipt: dict[str, Any] | None = None,
        output_files: list[dict[str, Any]] | None = None,
        superseded_tool_call_ids: list[str] | None = None,
        removed_file_paths: list[str] | None = None,
        request_digest: str = "",
        tool_name: str = "",
    ) -> AgentEvent:
        result: dict[str, Any] = {
            "id": id,
            "summary": summary,
            "is_error": is_error,
            "status": status or ("failed" if is_error else "success"),
        }
        if artifact_id:
            result["artifact_id"] = artifact_id
        if diff is not None:
            result["diff"] = diff
        if source_url:
            result["source_url"] = source_url
        if extraction_status:
            result["extraction_status"] = extraction_status
        if content_preview:
            result["content_preview"] = content_preview
        if evidence_type:
            result["evidence_type"] = evidence_type
        if duration_ms is not None:
            result["duration_ms"] = duration_ms
        if display_summary:
            result["display_summary"] = display_summary
        if result_kind:
            result["result_kind"] = result_kind
        if activity_kind:
            result["activity_kind"] = activity_kind
        clean_visibility = _required_event_text(
            visibility,
            field_name="visibility",
            maximum=32,
        )
        if clean_visibility not in _AGENT_ITEM_VISIBILITIES:
            raise ValueError(f"Unsupported tool visibility: {clean_visibility}")
        result["visibility"] = clean_visibility
        if group_id:
            result["group_id"] = group_id
        if step_id:
            result["step_id"] = step_id
        if limitation:
            result["limitation"] = limitation
        if provider:
            result["provider"] = provider
        if provider_error_type:
            result["provider_error_type"] = provider_error_type
        if error_info:
            result["error_info"] = error_info
        if error_kind:
            result["error_kind"] = error_kind
        if user_summary:
            result["user_summary"] = user_summary
        if developer_detail:
            result["developer_detail"] = developer_detail
        if recoverable is not None:
            result["recoverable"] = recoverable
        if projection:
            result["projection"] = projection
        if turn_id:
            result["turn_id"] = turn_id
        if iteration_id:
            result["iteration_id"] = iteration_id
        if phase:
            result["phase"] = phase
        if side_effect_kind:
            result["side_effect_kind"] = side_effect_kind
        if idempotent is not None:
            result["idempotent"] = idempotent
        if idempotency_key:
            result["idempotency_key"] = idempotency_key
        if cleanup_receipt:
            result["cleanup_receipt"] = {
                str(key): value
                for key, value in cleanup_receipt.items()
                if str(key) in {
                    "resource_kind",
                    "resource_id",
                    "reason",
                    "requested",
                    "acknowledged",
                    "completed",
                    "timed_out",
                    "pending",
                    "side_effect_kind",
                    "request_digest",
                    "retry_safe",
                    "manual_recovery_required",
                    "cleanup_completed_after_deadline",
                }
            }
        if output_files:
            result["output_files"] = [dict(item) for item in output_files]
        if superseded_tool_call_ids:
            result["superseded_tool_call_ids"] = list(superseded_tool_call_ids)
        if removed_file_paths:
            result["removed_file_paths"] = list(removed_file_paths)
        clean_tool_name = _optional_event_text(
            tool_name,
            field_name="tool_name",
            maximum=1_024,
        )
        if clean_tool_name:
            result["tool_name"] = clean_tool_name
        clean_request_digest = _optional_event_text(
            request_digest,
            field_name="request_digest",
            maximum=128,
        )
        if clean_request_digest:
            result["request_digest"] = clean_request_digest
        return cls(type="tool_result", data=result)

    @classmethod
    def agent_item(
        cls,
        *,
        id: str,
        kind: str,
        content: str = "",
        loop_id: str = "",
        iteration_id: str = "",
        parent_id: str = "",
        role: str = "assistant",
        source: str = "",
        status: str = "completed",
        title: str = "",
        summary: str = "",
        visibility: str = "timeline",
        created_at: int | None = None,
        order: int | None = None,
        seq: int | None = None,
        default_collapsed: bool | None = None,
        group_id: str = "",
        step_id: str = "",
        tool_call_ids: list[str] | None = None,
        skill_name: str = "",
        trigger_mode: str = "",
        source_level: str = "",
        reason: str = "",
        token_estimate: int | None = None,
    ) -> AgentEvent:
        clean_id = _required_event_text(
            id,
            field_name="id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_kind = _required_event_text(
            kind,
            field_name="kind",
            maximum=256,
        )
        clean_content = _event_string(
            content,
            field_name="content",
            maximum=_MAX_EVENT_CONTENT_CHARS,
            allow_empty=True,
        )
        clean_role = _required_event_text(
            role,
            field_name="role",
            maximum=128,
        )
        clean_status = _required_event_text(
            status,
            field_name="status",
            maximum=32,
        ).lower()
        if clean_status not in _AGENT_ITEM_STATUSES:
            raise ValueError(f"Unsupported agent-item status: {clean_status}")
        clean_visibility = _required_event_text(
            visibility,
            field_name="visibility",
            maximum=32,
        ).lower()
        if clean_visibility not in _AGENT_ITEM_VISIBILITIES:
            raise ValueError(f"Unsupported agent-item visibility: {clean_visibility}")
        clean_summary = _optional_event_text(
            summary,
            field_name="summary",
            maximum=_MAX_EVENT_SUMMARY_CHARS,
        )
        if (
            clean_status != "retracted"
            and clean_visibility != "debug"
            and not clean_content.strip()
            and not clean_summary
        ):
            raise ValueError("agent.item requires content or summary unless retracted/debug")
        if seq is not None:
            legacy_order = _non_negative_event_int(seq, field_name="seq")
            if order is not None and legacy_order != order:
                raise ValueError("agent.item seq and order disagree")
            # ``seq`` is transport-owned. Preserve legacy callers that used it
            # for item ordering without preventing EventEnvelope from stamping
            # the canonical per-turn sequence.
            order = legacy_order
        clean_tool_call_ids = (
            _bounded_event_string_list(
                tool_call_ids,
                field_name="tool_call_ids",
                maximum_items=256,
                maximum_item_chars=_MAX_EVENT_ID_CHARS,
            )
            if tool_call_ids is not None
            else []
        )
        payload: dict[str, Any] = {
            "id": clean_id,
            "item_id": clean_id,
            "kind": clean_kind,
            "role": clean_role,
            "status": clean_status,
            "visibility": clean_visibility,
        }
        optional_text_fields = (
            ("source", source, 256),
            ("loop_id", loop_id, _MAX_EVENT_ID_CHARS),
            ("iteration_id", iteration_id, _MAX_EVENT_ID_CHARS),
            ("parent_id", parent_id, _MAX_EVENT_ID_CHARS),
            ("title", title, _MAX_EVENT_SUMMARY_CHARS),
            ("group_id", group_id, _MAX_EVENT_ID_CHARS),
            ("step_id", step_id, _MAX_EVENT_ID_CHARS),
            ("skill_name", skill_name, 1_024),
            ("trigger_mode", trigger_mode, 64),
            ("source_level", source_level, 1_024),
            ("reason", reason, _MAX_EVENT_METADATA_CHARS),
        )
        for field_name, value, maximum in optional_text_fields:
            clean_value = _optional_event_text(
                value,
                field_name=field_name,
                maximum=maximum,
            )
            if clean_value:
                payload[field_name] = clean_value
        if clean_content:
            payload["content"] = clean_content
        if clean_summary:
            payload["summary"] = clean_summary
        if created_at is not None:
            payload["created_at"] = _non_negative_event_int(
                created_at,
                field_name="created_at",
            )
        if order is not None:
            payload["order"] = _non_negative_event_int(order, field_name="order")
        if default_collapsed is not None:
            if not isinstance(default_collapsed, bool):
                raise ValueError("default_collapsed must be a boolean")
            payload["default_collapsed"] = default_collapsed
        if clean_tool_call_ids:
            payload["tool_call_ids"] = clean_tool_call_ids
        if token_estimate is not None:
            payload["token_estimate"] = _non_negative_event_int(
                token_estimate,
                field_name="token_estimate",
            )
        return cls(type="agent.item", data=payload)

    @classmethod
    def progress(
        cls,
        message: str,
        *,
        stage: str = "status",
        status: str = "info",
        id: str | None = None,
        detail: str = "",
        tool_call_id: str = "",
        tool_name: str = "",
        count: int | None = None,
        phase: str | None = None,
        label: str = "",
        summary: str = "",
        visibility: str = "timeline",
        group_id: str = "",
        step_id: str = "",
        iteration_id: str = "",
        ephemeral: bool = False,
    ) -> AgentEvent:
        clean_message = _required_event_text(
            message,
            field_name="message",
            maximum=_MAX_EVENT_SUMMARY_CHARS,
        )
        clean_stage = _required_event_text(
            stage,
            field_name="stage",
            maximum=32,
        ).lower()
        if clean_stage not in _AGENT_PROGRESS_STAGES:
            raise ValueError(f"Unsupported agent-progress stage: {clean_stage}")
        clean_status = _required_event_text(
            status,
            field_name="status",
            maximum=32,
        ).lower()
        if clean_status not in _AGENT_PROGRESS_STATUSES:
            raise ValueError(f"Unsupported agent-progress status: {clean_status}")
        clean_id = _required_event_text(
            id or f"{clean_stage}:{clean_message}",
            field_name="id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        default_phase = "verify" if clean_stage == "verification" else clean_stage
        clean_phase = _required_event_text(
            phase or default_phase,
            field_name="phase",
            maximum=32,
        ).lower()
        if clean_phase not in _AGENT_PROGRESS_PHASES:
            raise ValueError(f"Unsupported agent-progress phase: {clean_phase}")
        clean_visibility = _required_event_text(
            visibility,
            field_name="visibility",
            maximum=32,
        ).lower()
        if clean_visibility not in _AGENT_ITEM_VISIBILITIES:
            raise ValueError(f"Unsupported agent-progress visibility: {clean_visibility}")
        clean_summary = _optional_event_text(
            summary or clean_message,
            field_name="summary",
            maximum=_MAX_EVENT_SUMMARY_CHARS,
        )
        payload: dict[str, Any] = {
            "id": clean_id,
            "stage": clean_stage,
            "status": clean_status,
            "message": clean_message,
            "phase": clean_phase,
            "summary": clean_summary,
            "visibility": clean_visibility,
        }
        clean_label = _optional_event_text(
            label,
            field_name="label",
            maximum=4_096,
        )
        if clean_label:
            payload["label"] = clean_label
        if not isinstance(ephemeral, bool):
            raise ValueError("ephemeral must be a boolean")
        if ephemeral:
            payload["ephemeral"] = True
        optional_text_fields = (
            ("detail", detail, _MAX_EVENT_SUMMARY_CHARS),
            ("tool_call_id", tool_call_id, _MAX_EVENT_ID_CHARS),
            ("tool_name", tool_name, 1_024),
            ("group_id", group_id, _MAX_EVENT_ID_CHARS),
            ("step_id", step_id, _MAX_EVENT_ID_CHARS),
            ("iteration_id", iteration_id, _MAX_EVENT_ID_CHARS),
        )
        for field_name, value, maximum in optional_text_fields:
            clean_value = _optional_event_text(
                value,
                field_name=field_name,
                maximum=maximum,
            )
            if clean_value:
                payload[field_name] = clean_value
        if payload.get("tool_call_id") and not payload.get("tool_name"):
            raise ValueError("tool-owned agent.progress requires tool_name")
        if count is not None:
            payload["count"] = _non_negative_event_int(count, field_name="count")
        return cls(type="agent.progress", data=payload)

    @classmethod
    def agent_run_started(cls, record: Any) -> AgentEvent:
        public_dict = getattr(record, "public_dict", None)
        payload = (
            public_dict()
            if callable(public_dict)
            else record.to_dict()
            if hasattr(record, "to_dict")
            else dict(record or {})
        )
        payload = project_public_agent_run(payload)
        cls._add_canonical_lifecycle(payload, kind="agent")
        return cls(type="agent.run.started", data=payload)

    @classmethod
    def agent_run_completed(cls, record: Any) -> AgentEvent:
        public_dict = getattr(record, "public_dict", None)
        payload = (
            public_dict()
            if callable(public_dict)
            else record.to_dict()
            if hasattr(record, "to_dict")
            else dict(record or {})
        )
        payload = project_public_agent_run(payload)
        cls._add_canonical_lifecycle(payload, kind="agent")
        return cls(type="agent.run.completed", data=payload)

    @staticmethod
    def _add_canonical_lifecycle(payload: dict[str, Any], *, kind: str) -> None:
        run_id = str(payload.get("run_id") or payload.get("subagent_id") or "")
        mailbox_epoch = int(payload.get("mailbox_epoch") or 0)
        agent_path = str(payload.get("agent_path") or "")
        payload.setdefault("run_id", run_id)
        payload.setdefault("task_id", str(payload.get("task_id") or run_id))
        payload.setdefault(
            "parent_run_id",
            str(payload.get("parent_run_id") or payload.get("parent_id") or ""),
        )
        payload.setdefault(
            "incarnation", f"{agent_path or run_id}:{mailbox_epoch}"
        )
        payload.setdefault("kind", kind)
        payload.setdefault("phase", str(payload.get("phase") or "running"))
        payload.setdefault("status", str(payload.get("status") or "running"))
        payload.setdefault(
            "updated_at",
            int(
                payload.get("completed_at")
                or payload.get("last_progress_at")
                or time.time() * 1000
            ),
        )
        payload.setdefault("result", {})

    @classmethod
    def turn_plan_updated(
        cls,
        *,
        thread_id: str,
        turn_id: str,
        plan: list[dict[str, Any]],
        explanation: str | None = None,
    ) -> AgentEvent:
        """Codex app-server ``turn/plan/updated`` notification."""

        return cls(
            type="turn.plan.updated",
            data={
                "thread_id": thread_id,
                "turn_id": turn_id,
                "explanation": explanation,
                "plan": plan,
            },
        )

    @classmethod
    def turn_diff_updated(
        cls,
        *,
        thread_id: str,
        turn_id: str,
        diff: str,
        revision: int | None = None,
        tool_call_id: str = "",
    ) -> AgentEvent:
        """Codex app-server ``turn/diff/updated`` notification."""

        data: dict[str, Any] = {
            "thread_id": thread_id,
            "turn_id": turn_id,
            "diff": diff,
        }
        if revision is not None:
            data["revision"] = revision
        if tool_call_id:
            data["tool_call_id"] = tool_call_id
        return cls(type="turn.diff.updated", data=data)

    @classmethod
    def approval_request(
        cls,
        tool_call_id: str,
        tool_name: str,
        args: dict[str, Any],
        diff: Any | None = None,
        source_agent: str = "",
        source_thread: str = "",
        source_tool: str = "",
        request_digest: str = "",
    ) -> AgentEvent:
        clean_tool_call_id = _required_event_text(
            tool_call_id,
            field_name="tool_call_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_tool_name = _required_event_text(
            tool_name,
            field_name="tool_name",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        if not isinstance(args, dict):
            raise ValueError("args must be an object")
        clean_source_agent = _optional_event_text(
            source_agent,
            field_name="source_agent",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_source_thread = _optional_event_text(
            source_thread,
            field_name="source_thread",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_source_tool = _optional_event_text(
            source_tool,
            field_name="source_tool",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_request_digest = _optional_event_text(
            request_digest,
            field_name="request_digest",
            maximum=128,
        )
        data: dict[str, Any] = {
            "tool_call_id": clean_tool_call_id,
            "tool_name": clean_tool_name,
            "args": dict(args),
        }
        if clean_source_agent:
            data["source_agent"] = clean_source_agent
        if clean_source_thread:
            data["source_thread"] = clean_source_thread
        if clean_source_tool:
            data["source_tool"] = clean_source_tool
        if clean_request_digest:
            data["request_digest"] = clean_request_digest
        if diff is not None:
            data["diff"] = diff
        return cls(type="approval_request", data=data)

    @classmethod
    def permission_decision(
        cls,
        *,
        tool_call_id: str,
        tool_name: str,
        decision: str,
        source: str = "hook",
        permission_level: str = "",
        message: str = "",
        capability: dict[str, Any] | None = None,
        approval_policy: str = "",
        matched_rule: dict[str, str] | None = None,
        risk: str = "",
        scope: dict[str, Any] | None = None,
        expiry: str = "",
        request_digest: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "decision": decision,
            "source": source,
        }
        if permission_level:
            data["permission_level"] = permission_level
        if message:
            data["message"] = message
        if capability:
            data["capability"] = capability
        if approval_policy:
            data["approval_policy"] = approval_policy
        if matched_rule:
            data["matched_rule"] = matched_rule
        if risk:
            data["risk"] = risk
        if scope:
            data["scope"] = scope
        if expiry:
            data["expiry"] = expiry
        clean_request_digest = _optional_event_text(
            request_digest,
            field_name="request_digest",
            maximum=128,
        )
        if clean_request_digest:
            data["request_digest"] = clean_request_digest
        return cls(type="permission.decision", data=data)

    @classmethod
    def done(
        cls,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
        cache_deleted_input_tokens: int = 0,
        reasoning_output_tokens: int = 0,
        cost_usd: float = 0.0,
        input_includes_cache_read: bool = True,
        input_includes_cache_write: bool = True,
        ordinary_input_tokens: int = 0,
        prompt_cache_total_tokens: int = 0,
        provider_raw: dict[str, Any] | None = None,
        status: str = "completed",
        reason: str = "",
        duration_ms: int | None = None,
    ) -> AgentEvent:
        normalized_status = _required_event_text(
            status,
            field_name="status",
            maximum=32,
        ).lower()
        if normalized_status not in _DONE_STATUSES:
            raise ValueError(f"Unsupported done status: {normalized_status}")
        clean_reason = _optional_event_text(
            reason,
            field_name="reason",
            maximum=_MAX_EVENT_METADATA_CHARS,
        )
        if not isinstance(input_includes_cache_read, bool):
            raise ValueError("input_includes_cache_read must be a boolean")
        if not isinstance(input_includes_cache_write, bool):
            raise ValueError("input_includes_cache_write must be a boolean")
        usage = {
            "input_tokens": _non_negative_event_int(
                input_tokens,
                field_name="input_tokens",
            ),
            "output_tokens": _non_negative_event_int(
                output_tokens,
                field_name="output_tokens",
            ),
            "cache_creation_input_tokens": _non_negative_event_int(
                cache_creation_input_tokens,
                field_name="cache_creation_input_tokens",
            ),
            "cache_read_input_tokens": _non_negative_event_int(
                cache_read_input_tokens,
                field_name="cache_read_input_tokens",
            ),
            "input_includes_cache_read": input_includes_cache_read,
            "input_includes_cache_write": input_includes_cache_write,
            "ordinary_input_tokens": _non_negative_event_int(
                ordinary_input_tokens,
                field_name="ordinary_input_tokens",
            ),
            "prompt_cache_total_tokens": _non_negative_event_int(
                prompt_cache_total_tokens,
                field_name="prompt_cache_total_tokens",
            ),
        }
        clean_cache_deleted = _non_negative_event_int(
            cache_deleted_input_tokens,
            field_name="cache_deleted_input_tokens",
        )
        clean_reasoning_output = _non_negative_event_int(
            reasoning_output_tokens,
            field_name="reasoning_output_tokens",
        )
        clean_cost = _non_negative_event_number(cost_usd, field_name="cost_usd")
        if clean_cache_deleted:
            usage["cache_deleted_input_tokens"] = clean_cache_deleted
        if clean_reasoning_output:
            usage["reasoning_output_tokens"] = clean_reasoning_output
        if clean_cost:
            usage["cost_usd"] = clean_cost
        if cache_read_input_tokens or cache_creation_input_tokens:
            usage.update(prompt_cache_usage_stats(usage, provider_raw))
        data: dict[str, Any] = {"usage": usage, "status": normalized_status}
        if clean_reason:
            data["reason"] = clean_reason
        if duration_ms is not None:
            data["duration_ms"] = _non_negative_event_int(
                duration_ms,
                field_name="duration_ms",
            )
        if provider_raw is not None:
            from backend.agent.provider_protocol import provider_raw_for_projection

            data["provider_raw"] = _bounded_event_record(
                provider_raw_for_projection(provider_raw),
                field_name="provider_raw",
            )
        return cls(
            type="done",
            data=data,
        )

    @classmethod
    def error(
        cls,
        message: str,
        recoverable: bool = True,
        error_type: str = "api",
        error_code: str = "",
        provider_error_type: str = "",
    ) -> AgentEvent:
        clean_message = _bounded_error_message(
            message,
            maximum=_MAX_EVENT_SUMMARY_CHARS,
        )
        if not isinstance(recoverable, bool):
            raise ValueError("recoverable must be a boolean")
        clean_error_type = _required_event_text(
            error_type,
            field_name="error_type",
            maximum=256,
        )
        data: dict[str, Any] = {
            "message": clean_message,
            "recoverable": recoverable,
            "error_type": clean_error_type,
        }
        clean_error_code = _optional_event_text(
            error_code,
            field_name="error_code",
            maximum=256,
        )
        clean_provider_error_type = _optional_event_text(
            provider_error_type,
            field_name="provider_error_type",
            maximum=256,
        )
        if clean_error_code:
            data["error_code"] = clean_error_code
        if clean_provider_error_type:
            data["provider_error_type"] = clean_provider_error_type
        return cls(type="error", data=data)

    @classmethod
    def approval_cancelled(
        cls,
        request_ids: list[str],
        *,
        reason: str = "run_cancelled",
        conversation_id: str = "",
    ) -> AgentEvent:
        clean_request_ids = _bounded_event_string_list(
            request_ids,
            field_name="request_ids",
            maximum_items=512,
            maximum_item_chars=_MAX_EVENT_ID_CHARS,
        )
        if not clean_request_ids:
            raise ValueError("request_ids must not be empty")
        clean_reason = _optional_event_text(
            reason,
            field_name="reason",
            maximum=256,
        )
        clean_conversation_id = _required_event_text(
            conversation_id,
            field_name="conversation_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        data: dict[str, Any] = {
            "request_ids": clean_request_ids,
            "reason": clean_reason or "run_cancelled",
        }
        data["conversation_id"] = clean_conversation_id
        return cls(
            type="approval.cancelled",
            data=data,
        )

    @classmethod
    def context_compacted(
        cls,
        summary: str,
        *,
        before_tokens: int | None = None,
        after_tokens: int | None = None,
        retained_categories: list[str] | None = None,
        ledger: ContextLedger | None = None,
    ) -> AgentEvent:
        clean_summary = _required_event_text(
            summary,
            field_name="summary",
            maximum=_MAX_EVENT_JSON_STRING_CHARS,
        )
        data: dict[str, Any] = {"summary": clean_summary}
        if before_tokens is not None:
            data["before_tokens"] = _non_negative_event_int(
                before_tokens,
                field_name="before_tokens",
            )
        if after_tokens is not None:
            data["after_tokens"] = _non_negative_event_int(
                after_tokens,
                field_name="after_tokens",
            )
        if retained_categories is not None:
            data["retained_categories"] = _bounded_event_string_list(
                retained_categories,
                field_name="retained_categories",
                maximum_items=64,
                maximum_item_chars=256,
            )
        if ledger is not None:
            data["ledger"] = _bounded_event_record(ledger, field_name="ledger")
        return cls(type="context_compacted", data=data)

    @classmethod
    def context_usage(
        cls,
        *,
        used: int,
        limit: int,
        conversation_id: str = "",
        ledger: ContextLedger | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "used": _non_negative_event_int(used, field_name="used"),
            "limit": _non_negative_event_int(limit, field_name="limit"),
        }
        clean_conversation_id = _optional_event_text(
            conversation_id,
            field_name="conversation_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        if clean_conversation_id:
            data["conversation_id"] = clean_conversation_id
        if ledger is not None:
            data["ledger"] = _bounded_event_record(ledger, field_name="ledger")
        return cls(type="context_usage", data=data)

    @classmethod
    def budget_update(
        cls,
        *,
        used: int,
        total: int,
        breakdown: dict[str, int] | None = None,
        conversation_id: str = "",
    ) -> AgentEvent:
        clean_breakdown: dict[str, int] = {}
        if breakdown is not None:
            if not isinstance(breakdown, dict) or len(breakdown) > 128:
                raise ValueError("breakdown must be an object with at most 128 fields")
            for raw_name, raw_tokens in breakdown.items():
                name = _required_event_text(
                    raw_name,
                    field_name="breakdown key",
                    maximum=256,
                )
                clean_breakdown[name] = _non_negative_event_int(
                    raw_tokens,
                    field_name=f"breakdown.{name}",
                )
        data: dict[str, Any] = {
            "used": _non_negative_event_int(used, field_name="used"),
            "total": _non_negative_event_int(total, field_name="total"),
            "breakdown": clean_breakdown,
        }
        clean_conversation_id = _optional_event_text(
            conversation_id,
            field_name="conversation_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        if clean_conversation_id:
            data["conversation_id"] = clean_conversation_id
        return cls(type="budget_update", data=data)

    @classmethod
    def stream_resume(
        cls,
        conversation_id: str,
        message_id: str | None,
        tool_calls_pending: list[dict[str, Any]] | None = None,
        content_blocks: list[dict[str, Any]] | None = None,
        *,
        turn_id: str = "",
        phase: str = "",
        stream_status: str = "",
        event_seq: int | None = None,
        last_event_type: str = "",
        tool_states: list[dict[str, Any]] | None = None,
    ) -> AgentEvent:
        clean_conversation_id = _required_event_text(
            conversation_id,
            field_name="conversation_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_message_id: str | None
        if message_id is None:
            clean_message_id = None
        else:
            clean_message_id = _required_event_text(
                message_id,
                field_name="message_id",
                maximum=_MAX_EVENT_ID_CHARS,
            )

        def clean_record_list(
            value: list[dict[str, Any]] | None,
            *,
            field_name: str,
            maximum_items: int,
            require_args: bool,
        ) -> list[dict[str, Any]]:
            records = value or []
            if not isinstance(records, list) or len(records) > maximum_items:
                raise ValueError(f"{field_name} must contain at most {maximum_items} records")
            cleaned: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            for record in records:
                clean = _bounded_event_record(record, field_name=field_name)
                record_id = _required_event_text(
                    clean.get("id"),
                    field_name=f"{field_name}.id",
                    maximum=_MAX_EVENT_ID_CHARS,
                )
                if record_id in seen_ids:
                    raise ValueError(f"{field_name} contains duplicate ids")
                seen_ids.add(record_id)
                clean["id"] = record_id
                clean["name"] = _required_event_text(
                    clean.get("name"),
                    field_name=f"{field_name}.name",
                    maximum=_MAX_EVENT_ID_CHARS,
                )
                if require_args:
                    clean["args"] = _bounded_event_record(
                        clean.get("args"),
                        field_name=f"{field_name}.args",
                    )
                elif clean.get("args") is not None:
                    clean["args"] = _bounded_event_record(
                        clean["args"],
                        field_name=f"{field_name}.args",
                    )
                cleaned.append(clean)
            return cleaned

        pending = clean_record_list(
            tool_calls_pending,
            field_name="tool_calls_pending",
            maximum_items=512,
            require_args=True,
        )
        states = clean_record_list(
            tool_states,
            field_name="tool_states",
            maximum_items=512,
            require_args=False,
        ) if tool_states is not None else None
        blocks = content_blocks or []
        if not isinstance(blocks, list) or len(blocks) > 1_024:
            raise ValueError("content_blocks must contain at most 1024 records")
        clean_blocks = [
            _bounded_event_record(
                block,
                field_name="content_blocks",
                max_nodes=16_384,
                max_depth=18,
                max_string_characters=_MAX_MESSAGE_TEXT_CHARS,
                max_collection_items=8_192,
            )
            for block in blocks
        ]
        data: dict[str, Any] = {
            "conversation_id": clean_conversation_id,
            "message_id": clean_message_id,
            "tool_calls_pending": pending,
            "content_blocks": clean_blocks,
        }
        for field_name, raw_value, maximum in (
            ("turn_id", turn_id, _MAX_EVENT_ID_CHARS),
            ("phase", phase, 256),
            ("stream_status", stream_status, 256),
            ("last_event_type", last_event_type, 256),
        ):
            clean_value = _optional_event_text(
                raw_value,
                field_name=field_name,
                maximum=maximum,
            )
            if clean_value:
                data[field_name] = clean_value
        if event_seq is not None:
            data["event_seq"] = _non_negative_event_int(event_seq, field_name="event_seq")
        if states is not None:
            data["tool_states"] = states
        return cls(
            type="stream_resume",
            data=data,
        )

    @classmethod
    def command_result(
        cls,
        command: str,
        message: str,
        *,
        level: str = "info",
        title: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent:
        payload: dict[str, Any] = {
            "command": command,
            "level": level,
            "message": message,
        }
        if title:
            payload["title"] = title
        if data:
            payload["data"] = data
        return cls(type="command.result", data=payload)

    @classmethod
    def task_update(
        cls, todo_id: str, status: str, content: str, active_form: str = ""
    ) -> AgentEvent:
        return cls(
            type="task.update",
            data={
                "todo_id": todo_id,
                "status": status,
                "content": content,
                "activeForm": active_form,
            },
        )

    @classmethod
    def subagent_start(
        cls,
        subagent_id: str,
        parent_id: str,
        role: str,
        prompt: str = "",
        *,
        current_activity: str = "",
        waiting_on: str = "",
        last_progress_at: int | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "parent_id": parent_id,
            "role": role,
            "prompt": public_text(prompt, max_chars=12_000),
        }
        if current_activity:
            data["current_activity"] = public_text(
                current_activity,
                max_chars=2_000,
            )
        if waiting_on:
            data["waiting_on"] = waiting_on
        if last_progress_at is not None:
            data["last_progress_at"] = last_progress_at
        if agent_path:
            data["agent_path"] = agent_path
        if mailbox_epoch is not None:
            data["mailbox_epoch"] = max(0, int(mailbox_epoch))
        cls._add_canonical_lifecycle(data, kind="subagent")
        return cls(type="subagent.start", data=data)

    @classmethod
    def subagent_progress(
        cls,
        subagent_id: str,
        *,
        iteration: int = 0,
        max_iterations: int = 0,
        tool_name: str = "",
        detail: str = "",
        current_activity: str = "",
        waiting_on: str = "",
        last_progress_at: int | None = None,
        activity_kind: str = "",
        activity_summary: str = "",
        user_visible: bool | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> AgentEvent:
        """Emitted during subagent execution to report intermediate progress."""
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "iteration": iteration,
        }
        if max_iterations:
            data["max_iterations"] = max_iterations
        if tool_name:
            data["tool_name"] = tool_name
        if detail:
            data["detail"] = public_text(detail, max_chars=4_000)
        if current_activity:
            data["current_activity"] = public_text(
                current_activity,
                max_chars=2_000,
            )
        if waiting_on:
            data["waiting_on"] = waiting_on
        if last_progress_at is not None:
            data["last_progress_at"] = last_progress_at
        if activity_kind:
            data["activity_kind"] = activity_kind
        if activity_summary:
            data["activity_summary"] = activity_summary
        if user_visible is not None:
            data["user_visible"] = user_visible
        if agent_path:
            data["agent_path"] = agent_path
        if mailbox_epoch is not None:
            data["mailbox_epoch"] = max(0, int(mailbox_epoch))
        cls._add_canonical_lifecycle(data, kind="subagent")
        return cls(type="subagent.progress", data=data)

    @classmethod
    def subagent_done(
        cls,
        subagent_id: str,
        summary: str = "",
        error: str = "",
        *,
        duration_ms: int | None = None,
        iterations: int = 0,
        tool_call_count: int = 0,
        timed_out: bool = False,
        status: str = "completed",
        termination_reason: str = "success",
        initiator: str = "runtime",
        usage: dict[str, Any] | None = None,
        agent_path: str = "",
        mailbox_epoch: int | None = None,
    ) -> AgentEvent:
        data: dict[str, Any] = {
            "subagent_id": subagent_id,
            "summary": public_text(summary, max_chars=12_000),
            "status": status,
            "termination_reason": public_text(
                termination_reason,
                max_chars=256,
                single_line=True,
            ),
            "initiator": initiator,
        }
        if error:
            data["error"] = public_text(error, max_chars=12_000)
        if duration_ms is not None:
            data["duration_ms"] = duration_ms
        if iterations:
            data["iterations"] = iterations
        if tool_call_count:
            data["tool_call_count"] = tool_call_count
        if timed_out:
            data["timed_out"] = True
        if usage:
            public_usage = project_public_usage(usage)
            if public_usage:
                data["usage"] = public_usage
        if agent_path:
            data["agent_path"] = agent_path
        if mailbox_epoch is not None:
            data["mailbox_epoch"] = max(0, int(mailbox_epoch))
        data["phase"] = "completed"
        data["result"] = (
            {"summary": public_text(summary, max_chars=12_000)}
            if summary
            else {}
        )
        cls._add_canonical_lifecycle(data, kind="subagent")
        return cls(type="subagent.done", data=data)

    @classmethod
    def stream_event(
        cls,
        *,
        provider: str,
        event_type: str,
        data: dict[str, Any],
        sdk_only: bool = True,
    ) -> AgentEvent:
        """Raw provider stream event passthrough for SDK consumers.

        When sdk_only is True (default), the UI should not render this — it's
        intended for programmatic consumers that need access to the underlying
        provider stream (e.g. RawMessageStreamEvent from Anthropic SDK).
        """
        clean_provider = _required_event_text(
            provider,
            field_name="provider",
            maximum=256,
        )
        clean_event_type = _required_event_text(
            event_type,
            field_name="event_type",
            maximum=512,
        )
        if not isinstance(sdk_only, bool):
            raise ValueError("sdk_only must be a boolean")
        clean_data = _bounded_event_record(data, field_name="stream_event data")
        return cls(
            type="stream_event",
            data={
                "provider": clean_provider,
                "event_type": clean_event_type,
                "data": clean_data,
                "sdk_only": sdk_only,
            },
        )

    @classmethod
    def rate_limit(
        cls,
        *,
        provider: str = "",
        error_type: str = "rate_limit",
        retry_after_seconds: float = 0.0,
        message: str = "",
        recoverable: bool = True,
        conversation_id: str = "",
    ) -> AgentEvent:
        import time as _time

        data: dict[str, Any] = {
            "provider": provider,
            "error_type": error_type,
            "recoverable": recoverable,
        }
        if retry_after_seconds > 0:
            data["retry_after_seconds"] = retry_after_seconds
            data["retry_at"] = int(_time.time() * 1000) + int(
                retry_after_seconds * 1000
            )
        if message:
            data["message"] = message
        if conversation_id:
            data["conversation_id"] = conversation_id
        return cls(type="rate_limit", data=data)

    @classmethod
    def session_state_changed(
        cls,
        *,
        state: Literal["idle", "working"],
        conversation_id: str = "",
        run_id: str = "",
        reason: str = "",
    ) -> AgentEvent:
        data: dict[str, Any] = {"state": state}
        if conversation_id:
            data["conversation_id"] = conversation_id
        if run_id:
            data["run_id"] = run_id
        if reason:
            data["reason"] = reason
        return cls(type="session.state_changed", data=data)

    @classmethod
    def budget_warning(
        cls, bucket: str, percent: float, will_compact: bool = False
    ) -> AgentEvent:
        clean_bucket = _required_event_text(
            bucket,
            field_name="bucket",
            maximum=256,
        )
        clean_percent = _non_negative_event_number(percent, field_name="percent")
        if clean_percent > 1:
            raise ValueError("percent must be between 0 and 1")
        if not isinstance(will_compact, bool):
            raise ValueError("will_compact must be a boolean")
        return cls(
            type="budget.warning",
            data={
                "bucket": clean_bucket,
                "percent": clean_percent,
                "will_compact": will_compact,
            },
        )

    @classmethod
    def inspector_update(
        cls,
        target_kind: str,
        target_id: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        clean_target_kind = _required_event_text(
            target_kind,
            field_name="target_kind",
            maximum=256,
        )
        clean_target_id = _required_event_text(
            target_id,
            field_name="target_id",
            maximum=_MAX_EVENT_ID_CHARS,
        )
        clean_payload = _bounded_event_record(
            payload,
            field_name="inspector payload",
        )
        data: dict[str, Any] = {
            "target_kind": clean_target_kind,
            "target_id": clean_target_id,
            "payload": clean_payload,
        }
        return cls(
            type="inspector.update",
            data=data,
        )


@dataclass
class UserCommand:
    """Client-to-server WebSocket message. See backend/ws/events.py."""

    type: ClientCommandType
    data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_ws_message(cls, msg: dict[str, Any]) -> UserCommand:
        """Deserialize a WebSocket JSON message."""
        msg_type = str(msg.get("type", "user_message"))
        data = {k: v for k, v in msg.items() if k != "type"}

        if msg_type == "control_cancel_request":
            request_id = data.get("request_id")
            if not request_id and "requestId" in data:
                data["request_id"] = data.pop("requestId")

        if msg_type == "control_response":
            if "request_id" not in data and "requestId" in data:
                data["request_id"] = data.pop("requestId")
            response = data.get("response")
            if isinstance(response, dict):
                if "request_id" not in response and "requestId" in response:
                    response = dict(response)
                    response["request_id"] = response.pop("requestId")
                data["response"] = response

        return cls(type=msg_type, data=data)
