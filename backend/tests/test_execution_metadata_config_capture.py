"""Execution metadata avoids duplicate work without dropping permission authority."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.agent.plans import bind_plan_owner
from backend.config import load_llm_settings
from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.conversations.repository import ConversationRepository
from backend.tests.test_projectless_execution_cwd_chain import owner_session
from backend.tests.test_user_input_presentation_chain import session_for
from backend.tools.base import PermissionLevel


def test_bound_metadata_binds_fresh_cwd_without_loading_the_full_record(tmp_path, monkeypatch, request):
    workspace = tmp_path / "actual-owner"
    workspace.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    conversation = repo.create_conversation(workspace_root=str(workspace), execution_cwd=str(workspace),
        permission_deny_rules=["run_command"], permission_overrides={"read_file": "auto"},
        context_snapshot={"history": [{"role": "user", "content": "Retained history"}]})
    session = owner_session(repo, conversation.id, tmp_path)
    request.addfinalizer(session.artifact_store.shutdown)
    stale = replace(repo.get_conversation_summary(conversation.id), execution_cwd=str(tmp_path / "stale-owner"))
    full = Mock(wraps=repo.get_conversation)
    summary = Mock(wraps=repo.get_conversation_summary)
    monkeypatch.setattr(repo, "get_conversation", full)
    monkeypatch.setattr(repo, "get_conversation_summary", summary)

    bound = session.session_lifecycle.bind_execution_for_operation(stale, metadata_only=True)

    assert bound.execution_cwd == str(workspace)
    summary.assert_called_once_with(conversation.id)
    full.assert_not_called()
    complete = session.session_lifecycle.bind_execution_for_operation(stale)
    full.assert_called_once_with(conversation.id)
    assert complete.permission_deny_rules == ["run_command"]
    assert complete.permission_overrides == {"read_file": "auto"}
    assert complete.context_snapshot["history"][0]["content"] == "Retained history"


@pytest.mark.asyncio
async def test_capture_keeps_full_plan_permissions_and_reuses_config_without_catalog_credentials(tmp_path, monkeypatch):
    session, original, _adapter = session_for(tmp_path, monkeypatch)
    workspace = Path(original.execution_cwd)
    raw = {"llm": {"provider": "custom", "custom": {
        "base_url": "https://owned.invalid/v1", "model": session.selected_model, "wire_api": "responses",
    }}}
    stack = ConfigLayerStack((ConfigLayer(ConfigLayerSource("user", file=str(tmp_path / "settings.json")), raw),))
    config = replace(session.config, config_layer_stack=stack,
        llm=replace(session.config.llm, base_url="https://owned.invalid/v1", wire_api="responses",
                    api_key="private-execution-key", default_headers=(("Authorization", "private-execution-header"),)))
    session.config = config
    record = session.conversation_repo.create_conversation(workspace_root=str(workspace), execution_cwd=str(workspace),
        permission_deny_rules=["run_command"], permission_overrides={"read_file": "auto"})
    session.active_conversation_id = record.id
    session.conversation_repo.update_permission_mode(record.id, "plan")
    bind_plan_owner(session.conversation_repo, record.id, workspace)
    complete = session.conversation_repo.get_conversation(record.id)
    expected_permission = session.permission_context_for_conversation(complete, source="agent.run")
    assert expected_permission.filesystem_constraints
    loads, built = [], []

    def load(*, cwd, resolve_credentials=True):
        assert cwd == workspace
        loads.append(resolve_credentials)
        return config if resolve_credentials else replace(config, llm=load_llm_settings(raw, resolve_credentials=False))

    def build(owner, **kwargs):
        assert owner == record.id
        built.append(kwargs)
        return session.tool_registry

    monkeypatch.setattr("backend.ws.agent_runner.load_config", load)
    monkeypatch.setattr(session, "_build_conversation_tool_registry", build)
    key_read = Mock(side_effect=AssertionError("Catalog metadata must not resolve provider credentials"))
    monkeypatch.setattr("backend.config_helpers._custom_provider_api_key", key_read)
    full = Mock(wraps=session.conversation_repo.get_conversation)
    monkeypatch.setattr(session.conversation_repo, "get_conversation", full)
    try:
        captured = session.capture_agent_run_request(record.id)
        assert loads == [True]
        full.assert_called_once_with(record.id)
        assert captured.permission == expected_permission
        assert captured.permission.tool_deny_rules == ["run_command"]
        assert captured.permission.session_overrides == {"read_file": PermissionLevel.AUTO}
        assert captured.permission.pre_plan_mode == "confirm"
        assert captured.permission.filesystem_constraints == expected_permission.filesystem_constraints
        assert captured.config.llm.api_key == "private-execution-key"
        assert captured.config.llm.default_headers == (("Authorization", "private-execution-header"),)
        catalog = built[0]["config"]
        assert catalog is not captured.config and catalog.config_layer_stack is captured.config.config_layer_stack
        assert catalog.llm == load_llm_settings(raw, resolve_credentials=False)
        assert catalog.llm.api_key == "" and catalog.llm.default_headers == ()
        assert catalog.permissions == captured.config.permissions and catalog.feature_flags == captured.config.feature_flags
        assert built[0]["workspace_root"] == workspace
        key_read.assert_not_called()
        session.capture_agent_run_request(record.id)
        assert loads == [True, True] and len(built) == 1
        session._conversation_tool_registry(record.id, workspace_root=workspace, force_rebuild=True)
        assert loads == [True, True, False] and len(built) == 2
        assert built[-1]["config"].llm.api_key == ""
        key_read.assert_not_called()
    finally:
        await session.session_lifecycle.shutdown(reason="metadata_config_capture_test")


@pytest.mark.asyncio
async def test_real_turn_uses_metadata_binding_but_keeps_history_compaction_and_durable_admission(tmp_path, monkeypatch):
    session, conversation, adapter = session_for(tmp_path, monkeypatch)
    repo = session.conversation_repo
    repo.update_compaction(conversation.id, "compacted", "Keep the original compaction summary")
    repo.commit_turn_admission(conversation.id,
        user_message={"id": "prior-user", "role": "user", "content": "Keep the earlier request"},
        context_snapshot={"history": [{"role": "user", "content": "Keep the earlier request"}]})
    bind = session.session_lifecycle.bind_execution_for_operation
    calls = []

    def observed_bind(record, *, metadata_only=False):
        calls.append((record.id, metadata_only))
        return bind(record, metadata_only=metadata_only)

    monkeypatch.setattr(session.session_lifecycle, "bind_execution_for_operation", observed_bind)
    from backend.agent.message import UserCommand
    try:
        await session.command_dispatcher._handle_command_inner(UserCommand("user_message", {
            "conversation_id": conversation.id, "content": "Continue the accepted work",
            "client_command_id": "metadata-owned-command", "user_message_id": "metadata-owned-user",
            "assistant_message_id": "metadata-owned-answer",
        }))
        await asyncio.gather(*tuple(session.execution_cleanup_tasks))
        await session.event_outbox.drain_delivery()
        await session.event_outbox.drain_persistence()
        # A prepared input uses a detached settings preview until admission;
        # startup must not eagerly bind or publish a different execution cwd.
        assert calls == []
        assert len(adapter.requests) == 1
        fresh = ConversationRepository(repo._base_dir).get_conversation(conversation.id)
        assert fresh.compaction_summary == "Keep the original compaction summary"
        assert "Earlier: Keep the original compaction summary" in fresh.summary
        assert [entry["id"] for entry in fresh.transcript] == ["prior-user", "metadata-owned-user", "metadata-owned-answer"]
        receipt = next(event for event in session.ws.messages if event.get("type") == "command.result"
            and event.get("command") == "user_message" and event.get("data", {}).get("admitted"))
        assert receipt["data"]["user_message_id"] == "metadata-owned-user"
    finally:
        await session.session_lifecycle.shutdown(reason="metadata_real_turn_test")
