from __future__ import annotations

import re
from pathlib import Path
from typing import NotRequired, get_args, get_origin, get_type_hints

from backend.ws.events import (
    BackgroundStalledData,
    ContextForkedData,
    ContextLedgerData,
    ContextLedgerEntryData,
    ContextSideQueryResultData,
    ControlCanUseToolRequestData,
    ControlElicitationRequestData,
    ControlProviderAuthPromptRequestData,
    ControlRequestData,
    ConversationCompactionUpdatedData,
    ConversationSummaryUpdatedData,
    ServerEventType,
)


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_runtime_server_event_registry_matches_backend_protocol() -> None:
    source = (ROOT / "frontend" / "src.v2" / "protocol" / "events.ts").read_text(encoding="utf-8")
    match = re.search(
        r"export const SERVER_EVENT_TYPES:[\s\S]*?new Set<[^>]+>\(\[([\s\S]*?)\]\);",
        source,
    )
    assert match is not None, "frontend SERVER_EVENT_TYPES registry was not found"
    frontend_events = set(re.findall(r'"([^"]+)"', match.group(1)))
    backend_events = set(get_args(ServerEventType))

    assert frontend_events == backend_events, {
        "backend_only": sorted(backend_events - frontend_events),
        "frontend_only": sorted(frontend_events - backend_events),
    }


def test_control_events_use_precise_frontend_payload_contracts() -> None:
    source = (ROOT / "frontend" / "src.v2" / "protocol" / "events.ts").read_text(encoding="utf-8")
    payload_match = re.search(
        r"type ServerEventPayload\s*=([\s\S]*?)export type ServerEvent",
        source,
    )
    untyped_match = re.search(
        r"export interface UntypedServerEvent\s*\{([\s\S]*?)\n\}",
        source,
    )
    assert payload_match is not None, "frontend ServerEventPayload union was not found"
    assert untyped_match is not None, "frontend UntypedServerEvent exclusion was not found"

    expected = {
        "stream_event": "StreamEventEvent",
        "rate_limit": "RateLimitEvent",
        "session.state_changed": "SessionStateEvent",
        "conversation.compaction.updated": "ConversationCompactionUpdatedEvent",
        "conversation.summary.updated": "ConversationSummaryUpdatedEvent",
        "context_forked": "ContextForkedEvent",
        "context_ledger": "ContextLedgerEvent",
        "context_side_query_result": "ContextSideQueryResultEvent",
        "control_request": "ControlRequestEvent",
    }
    payload_block = payload_match.group(1)
    untyped_block = untyped_match.group(1)
    for event_type, interface_name in expected.items():
        assert re.search(rf"\|\s*{re.escape(interface_name)}\b", payload_block), interface_name
        assert re.search(rf'\|\s*"{re.escape(event_type)}"', untyped_block), event_type


def test_conversation_projection_typed_dicts_match_wire_requiredness() -> None:
    compaction_hints = get_type_hints(
        ConversationCompactionUpdatedData,
        include_extras=True,
    )
    summary_hints = get_type_hints(
        ConversationSummaryUpdatedData,
        include_extras=True,
    )
    stalled_hints = get_type_hints(BackgroundStalledData, include_extras=True)
    stalled_optional = {
        key for key, annotation in stalled_hints.items()
        if get_origin(annotation) is NotRequired
    }

    assert set(compaction_hints) == {"conversation_id", "state", "summary"}
    assert set(summary_hints) == {
        "conversation_id",
        "summary",
        "title",
        "updated_at",
        "memory_mode",
        "memory_polluted",
        "memory_pollution_sources",
    }
    assert set(stalled_hints) - stalled_optional == {
        "command_id",
        "conversation_id",
        "tail",
        "advice",
    }
    assert stalled_optional == {"command", "description"}


def test_context_and_control_typed_dicts_match_wire_requiredness() -> None:
    def required_and_optional(payload_type: type) -> tuple[set[str], set[str]]:
        hints = get_type_hints(payload_type, include_extras=True)
        optional = {
            key for key, annotation in hints.items()
            if get_origin(annotation) is NotRequired
        }
        return set(hints) - optional, optional

    assert required_and_optional(ContextForkedData) == (
        {
            "conversation_id",
            "fork_id",
            "message_index",
            "context_history_index",
            "history_length",
            "estimated_tokens",
            "parent_conversation_id",
            "branch_created",
            "branch_activated",
        },
        {"message_id", "created_at", "status", "branch_conversation_id"},
    )
    assert required_and_optional(ContextLedgerEntryData) == (
        {"category", "label", "estimated_tokens", "item_count", "source_count", "sources"},
        set(),
    )
    assert required_and_optional(ContextLedgerData) == (
        {
            "conversation_id",
            "schema_version",
            "estimated_tokens",
            "actual_tokens",
            "compaction_count",
            "native_attachment_tokens",
            "native_attachment_count",
            "entries",
        },
        set(),
    )
    assert required_and_optional(ContextSideQueryResultData) == (
        {"conversation_id", "query", "result", "focus"},
        set(),
    )
    assert required_and_optional(ControlCanUseToolRequestData) == (
        {"subtype", "tool_name", "input", "tool_use_id"},
        {"diff", "source_agent", "source_thread", "source_tool"},
    )
    assert required_and_optional(ControlElicitationRequestData) == (
        {"subtype", "tool_use_id", "prompt", "question"},
        {"schema", "options", "choices", "allowed_values"},
    )
    assert required_and_optional(ControlProviderAuthPromptRequestData) == (
        {"subtype", "prompt", "provider", "prompt_type", "allow_empty", "allow_custom"},
        {"placeholder", "options"},
    )
    assert required_and_optional(ControlRequestData) == (
        {"request_id", "conversation_id", "request"},
        {
            "turn_id",
            "message_id",
            "workspace_root",
            "permission_mode",
            "workspace_scope",
            "timeout_seconds",
            "expires_at",
        },
    )
