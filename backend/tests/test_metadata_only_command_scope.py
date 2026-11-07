from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from backend.conversations.repository import ConversationRepository
from backend.ws.command_scope import resolve_command_scope, resolve_resource_command_scope
from backend.ws.session_lifecycle import SessionLifecycle
from backend.services.workspace_api_service import capture_workspace_execution
from backend.ws.agent_runner import SessionAgentRunnerMixin
from backend.config import AppConfig, LLMSettings
from backend.agent.plans import PLAN_SLUG_KEY


def owner(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    repo = ConversationRepository(tmp_path / "conversations")
    record = repo.create_conversation(workspace_root=str(root), execution_cwd=str(root),
        transcript=[{"id": f"m{i}", "role": "user", "content": "retained"} for i in range(1000)])
    session = SimpleNamespace(session_id="metadata-owner", active_conversation_id=record.id,
        conversation_repo=repo, resolve_requested_workspace=lambda _: root)
    session.session_lifecycle = SessionLifecycle(session)
    session.session_lifecycle.workspace_root = root
    return session, record, root


def test_command_scope_never_copies_history_just_to_find_its_workspace(tmp_path, monkeypatch):
    session, record, root = owner(tmp_path)
    def forbidden(*_args, **_kwargs):
        raise AssertionError("scope resolution copied the whole conversation")
    monkeypatch.setattr(session.conversation_repo, "get_conversation", forbidden)
    scope = resolve_command_scope(session, {"conversation_id": record.id, "workspace_root": str(root)})
    assert scope.conversation_id == record.id and scope.workspace_root == str(root)
    session.active_conversation_id = "another-owner"
    scope = resolve_resource_command_scope(session, {"owner_conversation_id": record.id})
    assert scope.conversation_id == record.id and scope.workspace_root == str(root)


def test_direct_summary_reads_reuse_metadata_without_loading_the_full_inventory(tmp_path, monkeypatch):
    session, record, _ = owner(tmp_path)
    reader = ConversationRepository(session.conversation_repo.storage_root)
    loads = []
    load = reader._load_summary

    def read_metadata(identity, **kwargs):
        loads.append(identity)
        return load(identity, **kwargs)

    def forbidden():
        raise AssertionError("One summary must not scan the full conversation directory")

    monkeypatch.setattr(reader, "_load_summary", read_metadata)
    monkeypatch.setattr(reader, "_discover_summary_stamps", forbidden)
    first = reader.get_conversation_summary(record.id)
    first.title = "Caller-owned edit"
    assert reader.get_conversation_summary(record.id).title == record.title
    assert loads == [record.id]
    session.conversation_repo.rename_conversation(record.id, "Changed in another window")
    assert reader.get_conversation_summary(record.id).title == "Changed in another window"
    assert reader.get_conversation_summary(record.id).title == "Changed in another window"
    assert loads == [record.id, record.id]
    session.conversation_repo.delete_conversation(record.id)
    assert reader.get_conversation_summary(record.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", [False, True])
async def test_workspace_capture_uses_metadata_except_for_plan_constraints(tmp_path, monkeypatch, plan):
    session, record, root = owner(tmp_path)
    if plan:
        record.permission_mode = "plan"
        record.context_snapshot = {"plan_slug": "owned-plan-state"}
        session.conversation_repo.save_conversation(record)
    reads = []
    read = session.conversation_repo.get_conversation
    def observe(*args):
        reads.append(args)
        if not plan:
            raise AssertionError("workspace capture copied the whole conversation")
        return read(*args)
    monkeypatch.setattr(session.conversation_repo, "get_conversation", observe)
    session.environment_manager = object()
    session.ws_manager = SimpleNamespace(execution_contexts=object())
    session.config = SimpleNamespace(config_layer_stack=None)
    captured = []
    session.permission_context_for_conversation = lambda conversation, **kwargs: captured.append(conversation) or object()
    monkeypatch.setattr("backend.agent.environment_runtime.capture_conversation_environments",
                        AsyncMock(side_effect=RuntimeError("reached environment capture")))
    with pytest.raises(RuntimeError, match="reached environment capture"):
        async with capture_workspace_execution(session, record.id):
            pass
    assert bool(reads) is plan
    assert captured[0].execution_cwd == str(root)
    if plan:
        assert captured[0].context_snapshot == record.context_snapshot


@pytest.mark.parametrize("mode", ["normal", "plan", "enter_plan", "legacy_cwd"])
def test_turn_request_capture_needs_history_only_for_plan_state(tmp_path, monkeypatch, mode):
    from backend.ws.turn_settings import PreparedTurnSettings

    session, record, root = owner(tmp_path)
    plan = mode in {"plan", "enter_plan"}
    if mode == "plan":
        record.permission_mode = "plan"
    if plan:
        record.context_snapshot = {PLAN_SLUG_KEY: "owned-plan-state"}
    if mode == "legacy_cwd":
        read_summary = session.conversation_repo.get_conversation_summary
        monkeypatch.setattr(session.conversation_repo, "get_conversation_summary",
            lambda identity: replace(read_summary(identity), execution_cwd=""))
    if mode != "normal":
        session.conversation_repo.save_conversation(record)
    reads = []
    read = session.conversation_repo.get_conversation
    def observe(identity):
        reads.append(identity)
        if mode == "normal":
            raise AssertionError("request capture copied history just for model/workspace metadata")
        return read(identity)
    monkeypatch.setattr(session.conversation_repo, "get_conversation", observe)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", lambda **kwargs: AppConfig(llm=LLMSettings(api_key="offline", model="fixture")))
    session.skill_manager = None
    session.skill_executor = None
    session.memory_manager = None
    session.provider = "custom"
    session.selected_model = "fixture"
    session._provider_override_active = session._model_override_active = False
    session.permission_context_for_conversation = lambda conversation, **kwargs: SimpleNamespace(mode=conversation.permission_mode)
    session._conversation_tool_registry = lambda *args, **kwargs: SimpleNamespace(fork=lambda: object())
    session._mcp_manager_for_workspace = lambda _: None
    settings = PreparedTurnSettings(permission_mode="plan") if mode == "enter_plan" else None
    request = SessionAgentRunnerMixin.capture_agent_run_request(session, record.id, turn_settings=settings)
    assert bool(reads) is (mode != "normal")
    assert request.workspace_root == root
    assert request.permission.mode == ("plan" if plan else "confirm")
    if plan:
        assert request.turn_settings.plan_slug == "owned-plan-state"
