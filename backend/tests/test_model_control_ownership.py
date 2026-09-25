from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.run_context import RunContext
from backend.agent.context import ContextBuilder
from backend.conversations.repository import ConversationRepository
from backend.services.misc_command_service import is_conversation_effort_command
from backend.tests.test_model_execution_ownership import setup, execute
from backend.llm.base import ToolCallEvent
from backend.ws.command_handlers import SessionCommandHandlersMixin
from backend.ws.handlers.misc import handle_llm_config_set, handle_model_command
from backend.ws.manager import WebSocketManager
from backend.ws.run_manager import SessionRunManager


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))


class ControlHost(SessionCommandHandlersMixin):
    def __init__(self, fixture, repo, tmp_path, active_id):
        self.session_id = ""
        self.ws_manager = None
        self.conversation_repo = repo
        self.active_conversation_id = active_id
        self.config = fixture.config
        self.context_builder = ContextBuilder(llm=fixture.model, token_budget=fixture.config.token_budget)
        self.provider = "custom"
        self.selected_model = "model-a"
        self.available_models = ["model-a", "model-b"]
        self.models_source = "configured"
        self._model_override_active = False
        self._provider_override_active = False
        self._last_llm_state_payload = None
        self._resolve_llm_provider = lambda *_: "custom"
        self._resolve_available_models = lambda *_: self.available_models
        self._resolve_models_source = lambda *_: "configured"
        self.catalog = fixture.owner.model_execution.model_runtime
        self._model_runtime_for_conversation = lambda _: self.catalog
        self.session_lifecycle = SimpleNamespace(
            workspace_root_for_conversation=lambda *_: tmp_path,
            schedule_task_runtime_update=Mock(), send_runtime_capabilities=AsyncMock(),
        )
        self.send_payload = AsyncMock(return_value=True)
        self.send_event = AsyncMock()
        self.emit_command_result = AsyncMock()
        self._report_model_catalog_error = AsyncMock()
        self.run_manager = SessionRunManager(self)
        self.run_manager.watch_conversation_notifications = Mock()


def controls(fixture, tmp_path, monkeypatch):
    repo = ConversationRepository(tmp_path / "conversations")
    for identity in ("conv_model_control", "conv_other_control"):
        repo.create_conversation(conversation_id=identity, workspace_root=str(tmp_path),
            model_selection={"provider": "custom", "model": "model-a", "reasoning_effort": "low"})
    catalog = fixture.owner.model_execution.model_runtime
    catalog.refresh = Mock()
    catalog.get_registered_provider_config = lambda _: None
    catalog.provider_payload = lambda provider, model: {
        "wire_api": "chat", "provider_id": provider, "reasoning_effort_levels": ["low", "high"],
        "context_window": catalog.get_model(provider, model).context_window,
    }
    monkeypatch.setattr("backend.config.load_config", lambda **_: fixture.config)
    host = ControlHost(fixture, repo, tmp_path, "conv_model_control")
    host.run_manager.register(conversation_id="conv_model_control", task=asyncio.current_task(),
        task_id="model-run", cancel_event=asyncio.Event(), active_conversation_id="conv_model_control", run_context=fixture.owner)
    return host, repo


@pytest.mark.asyncio
async def test_footer_changes_live_request_after_current_tools_and_persists_only_task(tmp_path, monkeypatch):
    fixture, host, repo = None, None, None
    requests = 0

    async def behavior(model, messages):
        nonlocal requests
        requests += 1
        if requests == 1:
            await handle_model_command(host, {"model": "model-b", "conversation_id": "conv_model_control"})
        elif requests == 2:
            await handle_llm_config_set(host, {"provider": "unrelated-default", "reasoning_effort": "high",
                "source": "frontend.footer", "conversation_id": "conv_model_control"})
        if requests <= 3:
            return ToolCallEvent(id=f"inspect-{requests}", name="inspect_model", arguments={})
        return None

    fixture = setup(tmp_path, monkeypatch, behavior)
    host, repo = controls(fixture, tmp_path, monkeypatch)
    global_update = AsyncMock(side_effect=AssertionError("Task effort must not change global provider settings"))
    monkeypatch.setattr("backend.services.llm_config_service.apply_llm_config_update", global_update)
    try:
        await execute(fixture, tmp_path, conversation_id="conv_model_control")
        assert fixture.inspector.observations == [
            ("model-a", "model-a", "low", 96000),
            ("model-b", "model-b", "low", 32768),
            ("model-b", "model-b", "high", 32768),
        ]
        assert repo.get_conversation_summary("conv_model_control").model_selection == {"provider": "custom", "model": "model-b", "reasoning_effort": "high"}
        assert repo.get_conversation_summary("conv_other_control").model_selection["reasoning_effort"] == "low"
        assert fixture.config.llm.reasoning_effort == "low"
        assert all(call.args[0]["conversation_id"] == "conv_model_control" for call in host.send_payload.call_args_list)
        global_update.assert_not_called()
    finally:
        await host.run_manager.shutdown_notification_wakes()


@pytest.mark.asyncio
async def test_other_window_targets_running_owner_without_changing_visible_task(tmp_path, monkeypatch):
    fixture, executing, requesting = None, None, None
    requests = 0

    async def behavior(model, messages):
        nonlocal requests
        requests += 1
        if requests == 1:
            await handle_model_command(requesting, {"model": "model-b", "conversation_id": "conv_model_control"})
            return ToolCallEvent(id="old-model-call", name="inspect_model", arguments={})
        assert model.model_id() == "model-b"
        return None

    fixture = setup(tmp_path, monkeypatch, behavior)
    executing, repo = controls(fixture, tmp_path, monkeypatch)
    executing.active_conversation_id = "conv_other_control"
    requesting = ControlHost(fixture, repo, tmp_path, "conv_other_control")
    manager = object.__new__(WebSocketManager)
    manager._sessions = {"executing": executing, "requesting": requesting}
    executing.ws_manager = requesting.ws_manager = manager
    try:
        await execute(fixture, tmp_path, conversation_id="conv_model_control")
        assert executing.selected_model == requesting.selected_model == "model-a"
        assert repo.get_conversation_summary("conv_model_control").model_selection["model"] == "model-b"
        assert repo.get_conversation_summary("conv_other_control").model_selection["model"] == "model-a"
        assert requesting.send_payload.call_args.args[0]["conversation_id"] == "conv_other_control"
        assert fixture.owner.model_execution.llm in fixture.created
    finally:
        await executing.run_manager.shutdown_notification_wakes()
        await requesting.run_manager.shutdown_notification_wakes()


@pytest.mark.asyncio
async def test_run_cleanup_cannot_remove_newer_model_owner(tmp_path, monkeypatch):
    fixture = setup(tmp_path, monkeypatch, None)
    host, _ = controls(fixture, tmp_path, monkeypatch)
    old_task = asyncio.current_task()
    newer_task = asyncio.create_task(asyncio.sleep(30))
    cancel = asyncio.Event()
    newer = RunContext()
    # Simulate the completed old task's late cleanup after a replacement run
    # has already been registered by the manager.
    host.run_manager.run_tasks.pop("conv_model_control")
    host.run_manager.register(conversation_id="conv_model_control", task=newer_task,
        task_id="newer", cancel_event=cancel, active_conversation_id="conv_model_control", run_context=newer)
    try:
        host.run_manager.cleanup(conversation_id="conv_model_control", task=old_task, task_id="model-run", cancel_event=asyncio.Event())
        assert host.run_manager.publish_model_execution("conv_model_control", fixture.owner.model_execution) is newer_task
        assert newer.model_execution is fixture.owner.model_execution
        host.run_manager.cleanup(conversation_id="conv_model_control", task=newer_task, task_id="newer", cancel_event=cancel)
        assert host.run_manager.publish_model_execution("conv_model_control", fixture.owner.model_execution) is None
    finally:
        newer_task.cancel()
        await asyncio.gather(newer_task, return_exceptions=True)
        await host.run_manager.shutdown_notification_wakes()
        fixture.runtime.close(release_lease=True)


def test_task_effort_and_provider_configuration_use_different_lock_scopes():
    assert is_conversation_effort_command({"source": "frontend.footer", "reasoning_effort": "high"}, "task")
    assert not is_conversation_effort_command({"source": "settings.provider.save", "reasoning_effort": "high"}, "task")
    assert not is_conversation_effort_command({"source": "frontend.footer", "reasoning_effort": "high"}, None)


@pytest.mark.asyncio
async def test_effort_edit_uses_selected_capabilities_without_refreshing_auth(tmp_path, monkeypatch):
    fixture = setup(tmp_path, monkeypatch, AsyncMock(return_value=None))
    host, repo = controls(fixture, tmp_path, monkeypatch)
    oauth = AsyncMock()
    auth = AsyncMock()
    monkeypatch.setattr(host.catalog, "refresh_oauth_credentials", oauth)
    monkeypatch.setattr(host.catalog, "refresh_provider_auth", auth)
    try:
        await handle_llm_config_set(host, {"source": "frontend.footer", "reasoning_effort": "high",
                                         "conversation_id": "conv_model_control"})
        assert repo.get_conversation("conv_model_control").model_selection["reasoning_effort"] == "high"
        oauth.assert_not_awaited()
        auth.assert_not_awaited()
        await host.set_selected_model("model-b", manual_override=True)
        oauth.assert_awaited_once()
        auth.assert_awaited_once()
    finally:
        await host.run_manager.shutdown_notification_wakes()
        fixture.runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_rejected_model_selection_does_not_acknowledge_effort_as_applied():
    session = SimpleNamespace(ws_manager=None, active_conversation_id="conv_model_control",
        set_selected_model=AsyncMock(return_value=False), emit_command_result=AsyncMock())
    await handle_llm_config_set(session, {"source": "frontend.footer", "reasoning_effort": "high"})
    session.emit_command_result.assert_not_called()
