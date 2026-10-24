"""Actual snapshot producer -> restore/sync handler -> validated WS wire.

Provider metadata is controlled; no model adapter, network or package GUI is
started. The snapshot shape and capability summary come from the production
WebSocketSession methods rather than a copied fixture snapshot.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.conversations.repository import ConversationRepository
from backend.llm.capabilities import ProviderCapabilities
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.ws.event_outbox import EventOutbox
from backend.ws.handler import (
    WORKSPACE_SCOPED_EVENT_TYPES,
    WebSocketSession,
    _requires_conversation_owner,
)
from backend.ws.handlers.session import handle_session_restore, handle_session_sync


class RecordingSocket:
    application_state = None
    client_state = None

    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class MetadataSession:
    runtime_snapshot = WebSocketSession.runtime_snapshot
    runtime_capability_summary = WebSocketSession.runtime_capability_summary
    send_payload = WebSocketSession.send_payload
    session_id = "provider-identity-chain"
    ws_manager = None
    _conversation_streams = {}
    active_conversation_id = None
    artifact_store = None
    selected_model = "selected-model"
    available_models = ["selected-model"]
    models_source = "configured"
    permission_checker = None
    _mcp_registry_version_snapshot = 0

    def __init__(self, root, provider, capability):
        self.provider = provider
        self.capability = capability
        self.socket = RecordingSocket()
        self.cleanup_tasks = set()
        self.event_outbox = EventOutbox(
            session_id=self.session_id, websocket=self.socket,
            replay_root=root / "replay", replay_limit=100,
            cleanup_tasks=self.cleanup_tasks, has_active_run=lambda: False,
            requires_conversation_owner=_requires_conversation_owner,
            workspace_scoped_event_types=WORKSPACE_SCOPED_EVENT_TYPES,
        )
        self.conversation_repo = ConversationRepository(root / "conversations")
        self.tool_registry = ToolRegistry()
        self.permission_context = PermissionContext(mode="confirm")
        self.task_manager = SimpleNamespace(summary=lambda: {"total": 0, "running": 0}, list=lambda: [])
        self.fork_registry = SimpleNamespace(list=lambda **_kwargs: [])
        self.run_manager = SimpleNamespace(queued_user_message_snapshot=lambda: [], pending_turn_input_snapshot=lambda: [],
            run_tasks={}, active_task_id=None)
        self.session_lifecycle = SimpleNamespace(current_workspace_root=lambda: None, workspace_root=None)

    def _pending_approval_runtime_items(self):
        return []

    def _mcp_summary(self):
        return {"connected": 0, "servers": []}

    def _runtime_permission_payload(self, **_kwargs):
        return {"profile": "confirm", "sandbox_status": {"probe_status": "ready"}}

    def _llm_selection_payload(self):
        return self.capability

    def _provider_capabilities_payload(self, _selection=None):
        return self.capability

    def refresh_tool_registry_if_mcp_changed(self, **_kwargs):
        pass

    def runtime_toolset_policy(self, _registry, **_kwargs):
        return None

    async def reemit_pending_state(self):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,provider_id,wire_api", [
    ("openai", "openai", "responses"),
    ("custom", "custom_anthropic", "anthropic"),
])
async def test_actual_snapshot_identity_is_preserved_in_restore_and_sync_wire(
    tmp_path, provider, provider_id, wire_api,
):
    capability = ProviderCapabilities(provider=provider, provider_id=provider_id,
        model="selected-model", wire_api=wire_api,
        base_url=f"https://{provider_id}.invalid/v1",
        configured_reasoning_effort="high" if wire_api == "responses" else "").to_dict()
    session = MetadataSession(tmp_path, provider, capability)
    produced = session.runtime_snapshot()
    assert "provider_capabilities" not in produced
    assert produced["capabilities"]["provider_capabilities"] == capability
    try:
        assert await handle_session_restore(session, {}) is True
        assert await handle_session_sync(session, {}) is True
        await session.event_outbox.drain_delivery()
        assert [event["type"] for event in session.socket.sent] == ["session.restored", "session.synced"]
        for event in session.socket.sent:
            assert event["provider"] == provider
            assert event["current_model"] == "selected-model"
            assert event["session"]["selected_model"] == "selected-model"
            assert "provider_capabilities" not in event["session"]
            nested = event["session"]["capabilities"]["provider_capabilities"]
            assert nested == capability
            assert {key: event[key] for key in ("provider_id", "base_url", "wire_api")} == {
                "provider_id": provider_id, "base_url": capability["base_url"], "wire_api": wire_api,
            }
    finally:
        await session.event_outbox.drain_delivery()
        await session.event_outbox.drain_persistence()
