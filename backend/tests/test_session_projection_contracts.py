from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from backend.agent.message import AgentEvent
from backend.conversations.models import ConversationRecord
from backend.llm.capabilities import ProviderCapabilities
from backend.services.conversation_payload_service import build_conversation_switched_payload
from backend.services.conversation_goal_service import build_goal_updated_payload
from backend.services.session_restore_service import (
    build_session_restored_payload,
    build_session_synced_payload,
)
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import (
    WORKSPACE_SCOPED_EVENT_TYPES,
    WebSocketSession,
    _requires_conversation_owner,
)
from backend.ws.payload_contracts import validate_session_projection_payload
from backend.ws.turn_wait_state import TurnWaitState


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _conversation() -> ConversationRecord:
    now = _now()
    return ConversationRecord(
        id="conversation-1",
        title="Release audit",
        created_at=now,
        updated_at=now,
        workspace_root="C:\\workspace",
        transcript=[{
            "id": "message-1",
            "role": "assistant",
            "content": "verified",
            "timestamp": 1,
        }],
        context_snapshot={
            "context_ledger": {
                "schema_version": 1,
                "estimated_tokens": 10,
                "actual_tokens": 10,
                "compaction_count": 0,
                "native_attachment_tokens": 0,
                "native_attachment_count": 0,
                "entries": [],
            },
        },
        goal={
            "id": "goal-1",
            "text": "Ship a verified release",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
    )


def _runtime_snapshot(conversation: ConversationRecord) -> dict[str, Any]:
    capabilities = ProviderCapabilities(
        provider="openai",
        model="gpt-test",
        wire_api="responses",
        reasoning_effort_levels=("low", "high"),
        limitations=("test-only",),
        adapters=(
            ProviderCapabilities(
                provider="fallback-child",
                reasoning_effort_levels=("medium",),
            ),
        ),
    ).to_dict()
    return {
        "session_id": "session-1",
        "active_conversation_id": conversation.id,
        "active_conversation": conversation.to_meta_dict(),
        "workspace_root": conversation.workspace_root,
        "selected_model": "gpt-test",
        "permission_mode": "confirm",
        "permission_profile": "confirm",
        "permission_source": "conversation",
        "workspace_scope": "workspace",
        "active_stream_conversation_ids": [],
        "invoked_skill_names": [],
        "pending_approval_count": 0,
        "pending_approvals": [],
        "queued_user_messages": [],
        "pending_turn_inputs": [],
        "forks": [],
        "running_tasks": [],
        "task_summary": {"total": 0, "running": 0},
        "capabilities": {"provider_capabilities": capabilities},
        "provider_capabilities": capabilities,
        "sandbox_status": {"probe_status": "ready"},
        "mcp": {"connected": 0, "servers": []},
    }


def test_provider_capability_projection_is_strict_json() -> None:
    payload = _runtime_snapshot(_conversation())["provider_capabilities"]

    assert payload["reasoning_effort_levels"] == ["low", "high"]
    assert payload["limitations"] == ["test-only"]
    assert payload["adapters"][0]["reasoning_effort_levels"] == ["medium"]
    assert json.loads(json.dumps(payload)) == payload


def test_agent_event_session_factories_match_wire_contracts() -> None:
    cancelled = AgentEvent.approval_cancelled(
        ["approval-1", "approval-2"],
        reason="user_interrupted",
        conversation_id="conversation-1",
    )
    assert cancelled.data == {
        "request_ids": ["approval-1", "approval-2"],
        "reason": "user_interrupted",
        "conversation_id": "conversation-1",
    }

    long_answer = "x" * 1_100_000
    resumed = AgentEvent.stream_resume(
        "conversation-1",
        "assistant-1",
        [{"id": "tool-1", "name": "read_file", "args": {"path": "README.md"}}],
        [{"type": "text", "content": long_answer}],
        turn_id="turn-1",
        event_seq=7,
        tool_states=[{
            "id": "tool-1",
            "name": "read_file",
            "args": {"path": "README.md"},
            "status": "running",
        }],
    )
    assert resumed.data["content_blocks"][0]["content"] == long_answer
    validate_session_projection_payload(resumed.to_ws_message())

    with pytest.raises(ValueError, match="must not be empty"):
        AgentEvent.approval_cancelled([], conversation_id="conversation-1")
    with pytest.raises(ValueError, match="duplicates"):
        AgentEvent.approval_cancelled(
            ["approval-1", "approval-1"],
            conversation_id="conversation-1",
        )
    with pytest.raises(ValueError, match="conversation_id is required"):
        AgentEvent.approval_cancelled(["approval-1"])
    with pytest.raises(ValueError, match="tool_calls_pending.name is required"):
        AgentEvent.stream_resume(
            "conversation-1",
            "assistant-1",
            [{"id": "tool-1", "args": {}}],
        )
    with pytest.raises(ValueError, match="tool_calls_pending.args must be an object"):
        AgentEvent.stream_resume(
            "conversation-1",
            "assistant-1",
            [{"id": "tool-1", "name": "read_file"}],
        )


def test_real_session_projection_builders_pass_runtime_contract() -> None:
    conversation = _conversation()
    runtime = _runtime_snapshot(conversation)
    restored = build_session_restored_payload(
        {
            "session_id": "session-1",
            "restored": True,
            "messages": list(conversation.transcript),
        },
        restored_conversation_id=conversation.id,
        active_payload=conversation.to_dict(),
        restored_workspace={"root_path": conversation.workspace_root},
        runtime_snapshot=runtime,
        selected_model="gpt-test",
        provider="openai",
        available_models=["gpt-test"],
        missed_events=False,
        last_seq=10,
        current_seq=12,
        replayed_events=2,
    )
    switched = build_conversation_switched_payload(
        conversation,
        is_hydrating=False,
        runtime_snapshot=runtime,
    )
    synced = build_session_synced_payload(
        {"session_id": "session-1", "synced": True, "session": runtime},
        protocol_version="1.0.0",
        active_conversation=conversation,
        active_conversation_id=conversation.id,
        workspace_root=conversation.workspace_root,
        selected_model="gpt-test",
        provider="openai",
        available_models=["gpt-test"],
        last_seq=10,
        current_seq=12,
        replayed_events=2,
    )
    listing = {
        "type": "conversation.list",
        "conversation_id": conversation.id,
        "active_conversation_id": conversation.id,
        "conversations": [conversation.to_dict()],
        "active_conversation": conversation.to_dict(),
        "session": runtime,
        "snapshot_at": _now(),
    }

    for payload in (restored, switched, synced, listing):
        validate_session_projection_payload(payload)
        json.dumps(payload)


def test_goal_revision_builder_and_projection_contract_require_safe_integers() -> None:
    payload = build_goal_updated_payload(
        conversation_id="conversation-1",
        goal={"id": "goal-1", "status": "active"},
        source="test",
        revision=7,
    )
    validate_session_projection_payload(payload)
    assert payload["revision"] == 7

    for invalid in (-1, 1.5, True, 9_007_199_254_740_992):
        with pytest.raises(ValueError, match="non-negative integer"):
            build_goal_updated_payload(
                conversation_id="conversation-1",
                goal={"id": "goal-1", "status": "active"},
                source="test",
                revision=invalid,  # type: ignore[arg-type]
            )
        with pytest.raises(ValueError):
            validate_session_projection_payload({
                "type": "goal.updated",
                "conversation_id": "conversation-1",
                "goal": {"id": "goal-1", "status": "active"},
                "revision": invalid,
            })


def test_conversation_inventory_epoch_fields_are_atomic() -> None:
    conversation = _conversation()
    valid = {
        "type": "conversation.list",
        "conversations": [conversation.to_dict()],
        "inventory_instance_id": "0123456789abcdef0123456789abcdef",
        "inventory_revision": 3,
    }
    validate_session_projection_payload(valid)

    invalid_payloads = [
        {**valid, "inventory_instance_id": ""},
        {**valid, "inventory_revision": -1},
        {**valid, "inventory_revision": 1.5},
        {**valid, "inventory_revision": True},
        {**valid, "inventory_revision": 9_007_199_254_740_992},
        {key: value for key, value in valid.items() if key != "inventory_instance_id"},
        {key: value for key, value in valid.items() if key != "inventory_revision"},
    ]
    for payload in invalid_payloads:
        with pytest.raises(ValueError):
            validate_session_projection_payload(payload)


def _conversation_list_session(versioned_listing: Any) -> WebSocketSession:
    session = object.__new__(WebSocketSession)
    session.conversation_runtime = SimpleNamespace(
        active_conversation_id=None,
        active_conversation=None,
    )
    session.runtime_snapshot = lambda: {"session_id": "session-list"}
    session.send_payload = AsyncMock(return_value=True)
    session.conversation_repo = SimpleNamespace(
        list_conversations_with_revision=lambda: versioned_listing,
    )
    return session


def test_send_conversation_list_projects_production_epoch_and_legacy_double() -> None:
    summary = _conversation().to_summary()
    versioned = _conversation_list_session((
        "0123456789abcdef0123456789abcdef",
        9,
        [summary],
    ))
    asyncio.run(versioned.send_conversation_list())
    versioned_payload = versioned.send_payload.await_args.args[0]
    assert versioned_payload["inventory_instance_id"] == "0123456789abcdef0123456789abcdef"
    assert versioned_payload["inventory_revision"] == 9

    legacy = _conversation_list_session((9, [summary]))
    asyncio.run(legacy.send_conversation_list())
    legacy_payload = legacy.send_payload.await_args.args[0]
    assert "inventory_instance_id" not in legacy_payload
    assert "inventory_revision" not in legacy_payload


@pytest.mark.parametrize(
    "versioned_listing",
    [
        ("", 1, []),
        ("0123456789abcdef0123456789abcdef", -1, []),
        ("0123456789abcdef0123456789abcdef", 1.5, []),
        ("0123456789abcdef0123456789abcdef", True, []),
        ("0123456789abcdef0123456789abcdef", 9_007_199_254_740_992, []),
        ("0123456789abcdef0123456789abcdef",),
    ],
)
def test_send_conversation_list_rejects_malformed_versioned_repository_results(
    versioned_listing: Any,
) -> None:
    session = _conversation_list_session(versioned_listing)
    with pytest.raises(ValueError, match="list_conversations_with_revision"):
        asyncio.run(session.send_conversation_list())
    session.send_payload.assert_not_awaited()


def test_session_replay_contract_accepts_durable_links_across_transient_seq_gaps() -> None:
    payload = {
        "type": "session.replay",
        "last_seq": 5,
        "current_seq": 11,
        "replayed_events": 2,
        "events": [
            {
                "type": "goal.updated",
                "conversation_id": "conversation-1",
                "goal": {"id": "goal-1", "status": "active"},
                "seq": 8,
                "previous_replay_seq": 5,
            },
            {
                "type": "done",
                "conversation_id": "conversation-1",
                "status": "completed",
                "usage": {},
                "seq": 11,
                "previous_replay_seq": 8,
            },
        ],
    }

    validate_session_projection_payload(payload)


def test_session_snapshot_contract_allows_an_explicit_authoritative_cursor_rebase() -> None:
    conversation = _conversation()
    payload = build_session_restored_payload(
        {
            "session_id": "session-1",
            "restored": True,
            "messages": list(conversation.transcript),
        },
        restored_conversation_id=conversation.id,
        active_payload=conversation.to_dict(),
        restored_workspace={"root_path": conversation.workspace_root},
        runtime_snapshot=_runtime_snapshot(conversation),
        selected_model="gpt-test",
        provider="openai",
        available_models=["gpt-test"],
        missed_events=True,
        event_log_gap=True,
        snapshot_required=True,
        cursor_reset=True,
        requested_last_seq=12,
        last_seq=8,
        current_seq=8,
        replayed_events=0,
    )

    validate_session_projection_payload(payload)
    assert payload["cursor_reset"] is True
    assert payload["requested_last_seq"] == 12
    assert payload["last_seq"] == payload["current_seq"] == 8


@pytest.mark.parametrize(
    "payload,match",
    [
        (
            {
                "type": "conversation.list",
                "conversations": [{"id": "conversation-1"}, {"id": "conversation-1"}],
            },
            "duplicate conversation ids",
        ),
        (
            {
                "type": "session.replay",
                "last_seq": 5,
                "current_seq": 8,
                "replayed_events": 1,
                "events": [{
                    "type": "session.replay",
                    "seq": 6,
                    "previous_replay_seq": 5,
                    "events": [],
                }],
            },
            "non-replayable event",
        ),
        (
            {
                "type": "session.replay",
                "last_seq": 5,
                "current_seq": 8,
                "replayed_events": 2,
                "events": [
                    {
                        "type": "goal.updated",
                        "seq": 7,
                        "previous_replay_seq": 5,
                    },
                    {
                        "type": "goal.updated",
                        "seq": 6,
                        "previous_replay_seq": 7,
                    },
                ],
            },
            "seq must follow previous_replay_seq",
        ),
        (
            {
                "type": "session.replay",
                "last_seq": 5,
                "current_seq": 8,
                "replayed_events": 1,
                "events": [{
                    "type": "goal.updated",
                    "seq": 8,
                    "previous_replay_seq": 7,
                }],
            },
            "durable chain is discontinuous",
        ),
        (
            {
                "type": "session.replay",
                "last_seq": 5,
                "current_seq": 9,
                "replayed_events": 1,
                "events": [{
                    "type": "goal.updated",
                    "seq": 8,
                    "previous_replay_seq": 5,
                }],
            },
            "does not reach current_seq",
        ),
        (
            {
                "type": "stream_resume",
                "conversation_id": "conversation-1",
                "message_id": "assistant-1",
                "tool_calls_pending": [
                    {"id": "tool-1", "name": "read_file", "args": {}},
                    {"id": "tool-1", "name": "read_file", "args": {}},
                ],
            },
            "duplicate ids",
        ),
    ],
)
def test_session_projection_contract_rejects_malformed_nested_state(
    payload: dict[str, Any],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        validate_session_projection_payload(payload)


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)


def _transport_session(tmp_path: Path) -> tuple[WebSocketSession, _FakeWebSocket]:
    session = object.__new__(WebSocketSession)
    websocket = _FakeWebSocket()
    session.session_id = "session-1"
    session.event_outbox = EventOutbox(
        session_id=session.session_id,
        websocket=websocket,
        replay_root=tmp_path,
        replay_limit=1000,
        cleanup_tasks=set(),
        has_active_run=lambda: False,
        requires_conversation_owner=_requires_conversation_owner,
        workspace_scoped_event_types=WORKSPACE_SCOPED_EVENT_TYPES,
    )
    return session, websocket


def test_transport_allows_global_task_snapshot_and_stamps_command_owner(tmp_path: Path) -> None:
    session, websocket = _transport_session(tmp_path)
    with session.event_outbox.bind_client_command("command-1", "session.sync"):
        sent = asyncio.run(session.send_payload(
            {
                "type": "task.update",
                "session": {"session_id": "session-1", "pending_approval_count": 0},
            },
            connection_generation=1,
            log_context="test",
            envelope=False,
        ))

    assert sent is True
    assert websocket.sent == [{
        "type": "task.update",
        "session": {"session_id": "session-1", "pending_approval_count": 0},
        "client_command_id": "command-1",
        "client_command_type": "session.sync",
    }]


def test_transport_rejects_invalid_session_projection_before_wire(tmp_path: Path) -> None:
    session, websocket = _transport_session(tmp_path)

    sent = asyncio.run(session.send_payload(
        {
            "type": "session.restored",
            "session": {"session_id": "session-1", "capabilities": {"bad": ("tuple",)}},
        },
        connection_generation=1,
        log_context="test",
        envelope=False,
    ))

    assert sent is False
    assert websocket.sent == []


class _ApprovalRuntime(SessionApprovalRuntimeMixin):
    def __init__(self) -> None:
        self.session_id = "session-1"
        self.turn_wait_state = TurnWaitState()
        self.turn_wait_state.pending_approval_payloads = {
            "approval-a": {
                "type": "control_request",
                "request_id": "approval-a",
                "request": {"subtype": "can_use_tool", "tool_name": "read_file", "input": {}},
                "conversation_id": "conversation-a",
            },
            "approval-b": {
                "type": "control_request",
                "request_id": "approval-b",
                "request": {"subtype": "can_use_tool", "tool_name": "write_file", "input": {}},
                "conversation_id": "conversation-b",
            },
        }
        self._settled_approval_notifications: set[str] = set()
        self.send_event = AsyncMock()
        self._resolve_pending_approval = lambda request_id, _payload: request_id in {
            "approval-a",
            "approval-b",
        }


def test_global_auto_approval_emits_one_cancellation_per_conversation() -> None:
    runtime = _ApprovalRuntime()

    approved = asyncio.run(runtime.auto_approve_pending_tool_approvals(
        reason="permission_mode_changed",
    ))

    assert approved == ["approval-a", "approval-b"]
    events = [call.args[0] for call in runtime.send_event.await_args_list]
    assert [(event.data["conversation_id"], event.data["request_ids"]) for event in events] == [
        ("conversation-a", ["approval-a"]),
        ("conversation-b", ["approval-b"]),
    ]
