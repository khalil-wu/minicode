from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.config import AppConfig, LLMSettings
from backend.permissions.context import PermissionContext
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession
from backend.ws.session_lifecycle import SessionLifecycle


@pytest.mark.asyncio
@pytest.mark.parametrize("include_catalogs", [False, True])
@pytest.mark.parametrize("change", ["none", "workspace", "config", "permission", "shutdown"])
async def test_slow_capability_disk_capture_yields_and_only_publishes_to_its_owner(tmp_path, monkeypatch, include_catalogs, change):
    config = AppConfig(llm=LLMSettings(api_key=""))
    owner = SimpleNamespace(
        active_conversation_id="reader", session_id="session", config=config, root=tmp_path,
        permission_context=PermissionContext(mode="bypass"), permission_checker=None, is_connected=False,
        tool_registry=ToolRegistry(), _mcp_registry_version_snapshot=0,
        refresh_tool_registry_if_mcp_changed=Mock(), runtime_toolset_policy=lambda *args, **kwargs: None,
        _llm_selection_payload=lambda **_: {"provider": "custom", "base_url": "https://fixture.invalid", "wire_api": "chat", "models_source": "unavailable"},
        _model_runtime_for_conversation=lambda _: None, _provider_capabilities_payload=lambda *_: {},
        _runtime_permission_payload=lambda: {}, send_payload=AsyncMock(),
        command_registry=SimpleNamespace(list_extension_slash_commands=lambda **_: []),
    )
    owner.conversation_repo = SimpleNamespace(get_conversation_summary=lambda _: SimpleNamespace(workspace_root=str(owner.root)))
    lifecycle = SessionLifecycle(owner)
    owner.session_lifecycle = lifecycle
    lifecycle.workspace_root_for_conversation = lambda *_: owner.root
    lifecycle._sandbox_capability_scope = lifecycle._current_sandbox_capability_scope()
    lifecycle._sandbox_capability_payload = {"backend_available": True}
    owner.runtime_capability_snapshot = lambda **kwargs: WebSocketSession.runtime_capability_snapshot(owner, **kwargs)
    owner.runtime_capabilities_payload = lambda **kwargs: WebSocketSession.runtime_capabilities_payload(owner, **kwargs)
    loop_thread = threading.get_ident()
    entered, release = threading.Event(), threading.Event()
    reads = []
    captured = AppConfig(llm=LLMSettings(api_key=""))

    def load(*, cwd, resolve_credentials):
        assert threading.get_ident() != loop_thread
        assert cwd == tmp_path and resolve_credentials is False
        reads.append(cwd)
        entered.set()
        assert release.wait(5)
        return captured

    def skills(root, *, config_stack):
        assert threading.get_ident() != loop_thread
        assert root == tmp_path and config_stack is captured.config_layer_stack
        return SimpleNamespace(list_all=lambda: [{"name": "owned-skill"}])

    def commands(root, *, resolve_active_workspace):
        assert threading.get_ident() != loop_thread
        assert root == tmp_path and resolve_active_workspace is False
        return [{"name": "owned-command"}]

    owner.skill_manager = SimpleNamespace(snapshot=Mock(side_effect=skills))
    monkeypatch.setattr("backend.config.load_config", load)
    monkeypatch.setattr("backend.commands.catalog.get_enabled_composer_command_catalog", commands)
    task = asyncio.create_task(lifecycle.send_runtime_capabilities(source="test", include_catalogs=include_catalogs))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not task.done()
        if change == "workspace":
            owner.root = tmp_path / "other"
        elif change == "config":
            owner.config = AppConfig(llm=LLMSettings(api_key=""))
        elif change == "permission":
            owner.permission_context = PermissionContext(mode="confirm")
        elif change == "shutdown":
            lifecycle.is_shutting_down = True
    finally:
        release.set()
        await asyncio.wait_for(task, 5)
    assert reads == [tmp_path]
    if change == "none":
        payload = owner.send_payload.call_args.args[0]
        assert payload["conversation_id"] == "reader" and payload["workspace_root"] == str(tmp_path)
        if include_catalogs:
            assert payload["capabilities"]["skills"] == [{"name": "owned-skill"}]
            assert payload["capabilities"]["composer_commands"] == [{"name": "owned-command"}]
        else:
            assert "skills" not in payload["capabilities"]
    else:
        owner.send_payload.assert_not_awaited()
    assert owner.skill_manager.snapshot.call_count == int(include_catalogs)


@pytest.mark.asyncio
async def test_model_default_and_both_registry_projections_reuse_the_captured_config(tmp_path, monkeypatch):
    from dataclasses import replace
    from backend.api import _state
    from backend.tests.test_user_input_presentation_chain import session_for

    session, record, adapter = session_for(tmp_path, monkeypatch)
    captured = replace(session.config, llm=replace(session.config.llm, model="project-default", reasoning_effort="low"))
    captured_configs = []
    def build(*args, config, **kwargs):
        captured_configs.append(config)
        return ToolRegistry()
    def forbidden(**kwargs):
        raise AssertionError("A capability projection reread an already captured configuration")
    monkeypatch.setattr(_state, "bootstrap", SimpleNamespace(create_tool_registry=build))
    monkeypatch.setattr(session, "_build_conversation_tool_registry", build)
    monkeypatch.setattr(session, "_mcp_manager_for_workspace", lambda _: None)
    session._mcp_registry_version_snapshot = -1
    monkeypatch.setattr("backend.config.load_config", forbidden)
    monkeypatch.setattr("backend.ws.agent_runner.load_config", forbidden)
    try:
        snapshot = session.runtime_capability_snapshot(config_snapshot=captured, include_catalogs=False)
        assert "error" not in snapshot
        assert snapshot["provider_capabilities"]["model"] == session.selected_model
        assert snapshot["provider_capabilities"]["default_model_selection"]["model"] == "project-default"
        assert len(captured_configs) == 2
        assert all(config.config_layer_stack is captured.config_layer_stack for config in captured_configs)
        assert session.llm is adapter
    finally:
        await session.session_lifecycle.shutdown(reason="captured-capability-config")
