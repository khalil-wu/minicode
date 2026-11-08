from __future__ import annotations

from unittest.mock import Mock

import pytest

from backend.commands.registry import CommandRegistry
from backend.extensions.loader import clear_extension_cache
from backend.tests.test_agent_runner_done_fallback import _Session
from backend.tools.registry import ToolRegistry


def catalog_host(tmp_path):
    host = _Session(tmp_path, [])
    host.command_registry = CommandRegistry()
    host.refresh_tool_registry_if_mcp_changed = Mock()
    host._build_conversation_tool_registry = lambda *args, **kwargs: ToolRegistry()
    host._on_model_runtime_changed = Mock()
    return host


@pytest.mark.asyncio
async def test_empty_extension_generation_publishes_models_without_loading_conversation_history(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    host = catalog_host(tmp_path)
    record = host.conversation_repo.get_conversation("conv_runnerdone")
    record.context_snapshot = {"history": [{"role": "user", "content": f"history {index}"} for index in range(10000)]}
    host.conversation_repo.save_conversation(record)
    full_read = Mock(side_effect=AssertionError("An empty executable catalog has no history consumer"))
    monkeypatch.setattr(host.conversation_repo, "get_conversation", full_read)
    prewarm = Mock()
    monkeypatch.setattr("backend.ws.model_prewarm.schedule_model_prewarm", prewarm)
    try:
        runner = await host._ensure_extension_commands_for_conversation(record.id, bind_command_context=False)
        assert runner.active and runner.extensions == []
        state = host._extension_runtime_state(record.id)
        assert state["model_registry"].runtime is state["model_runtime"]
        assert not state["host_actions_bound"]
        assert host.command_registry.list_extension_slash_commands(scope_id=record.id) == []
        assert await host._ensure_extension_commands_for_conversation(record.id) is runner
        prewarm.assert_called_with(host, record.id)
        full_read.assert_not_called()
    finally:
        await host._shutdown_lifecycle_runtimes("test complete")
        host.run_manager.close_durable_queue()
        clear_extension_cache()


@pytest.mark.asyncio
async def test_real_extension_is_fully_bound_before_startup_and_removed_generation_drops_its_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("backend.ws.agent_runner.is_workspace_trusted", lambda root: True)
    host = catalog_host(tmp_path)
    record = host.conversation_repo.get_conversation("conv_runnerdone")
    record.context_snapshot = {"history": [{"role": "user", "content": "original user"},
        {"role": "assistant", "content": "original answer"}]}
    host.conversation_repo.save_conversation(record)
    extension = tmp_path / ".minicode" / "extensions" / "owned.py"
    extension.parent.mkdir(parents=True)
    extension.write_text("def extension(api):\n"
        "    api.on('session_start', lambda event, ctx: api.set_session_name('startup actions are bound'))\n"
        "    api.register_command('owned', lambda args, ctx: True)\n", encoding="utf-8")
    bind = host._bind_lifecycle_runtime_host_actions
    snapshots = []
    def record_binding(*args, **kwargs):
        snapshots.append([message.content for message in kwargs["run_context_builder"]._history])
        return bind(*args, **kwargs)
    monkeypatch.setattr(host, "_bind_lifecycle_runtime_host_actions", record_binding)
    try:
        runner = await host._ensure_lifecycle_runtime(conversation_id=record.id, workspace_root=tmp_path,
            tool_registry=ToolRegistry(), config=host.config)
        assert runner.active and not runner.errors
        assert snapshots == [["original user", "original answer"]]
        assert host.conversation_repo.get_conversation_summary(record.id).title == "startup actions are bound"
        assert host.command_registry.get_extension_slash("/owned", scope_id=record.id) is not None
        extension.unlink()
        refreshed = await host._ensure_lifecycle_runtime(conversation_id=record.id, workspace_root=tmp_path,
            tool_registry=ToolRegistry(), config=host.config, force_reload=True)
        assert refreshed is not runner and refreshed.extensions == [] and not runner.active
        assert host.command_registry.get_extension_slash("/owned", scope_id=record.id) is None
        assert snapshots == [["original user", "original answer"]]
    finally:
        await host._shutdown_lifecycle_runtimes("test complete")
        host.run_manager.close_durable_queue()
        clear_extension_cache()
