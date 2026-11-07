import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.agent.context import ContextBuilder
from backend.config import AppConfig, LLMSettings
from backend.extensions.capability_source import LoadedLifecycleCapability
from backend.extensions.capability_source import ExtensionCapabilitySource
from backend.extensions.loader import clear_extension_cache
from backend.tests.test_agent_runner_done_fallback import _Session
from backend.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership", ["unchanged", "running", "retired"])
async def test_command_history_worker_keeps_loop_responsive_and_respects_live_binding(tmp_path, monkeypatch, ownership):
    host = _Session(tmp_path, [])
    conversation = host.conversation_repo.get_conversation("conv_runnerdone")
    conversation.context_snapshot = {"history": [{"role": "user", "content": f"history {index}"} for index in range(1000)]}
    runner = SimpleNamespace(active=True)
    state = host._extension_runtime_state(conversation.id)
    state["runtime"] = runner
    state["host_actions_bound"] = True
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    load = ContextBuilder.load_snapshot
    bind = Mock()

    def held_load(builder, snapshot):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        load(builder, snapshot)

    monkeypatch.setattr(ContextBuilder, "load_snapshot", held_load)
    monkeypatch.setattr(host, "_bind_lifecycle_runtime_host_actions", bind)
    task = asyncio.create_task(host._bind_lifecycle_runtime_command_snapshot(runner,
        conversation=conversation, tool_registry=ToolRegistry(), config=AppConfig(llm=LLMSettings(api_key="test-key"))))
    active = None
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert not task.done()
        if ownership == "running":
            active = asyncio.create_task(asyncio.Event().wait())
            host.run_manager.run_tasks[conversation.id] = active
        elif ownership == "retired":
            runner.active = False
        release.set()
        await asyncio.wait_for(task, 5)
        if ownership == "unchanged":
            assert bind.call_count == 1
            assert len(bind.call_args.kwargs["run_context_builder"]._history) == 1000
            assert bind.call_args.kwargs["run_context_builder"]._history[-1].content == "history 999"
        else:
            bind.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if active is not None:
            active.cancel()
            await asyncio.gather(active, return_exceptions=True)
            host.run_manager.run_tasks.pop(conversation.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
async def test_unpublished_command_snapshot_is_drained_and_discarded(tmp_path, monkeypatch, stop):
    clear_extension_cache()
    monkeypatch.setenv("MINICODE_CONFIG_DIR", str(tmp_path / "home"))
    monkeypatch.setattr("backend.ws.agent_runner.is_workspace_trusted", lambda root: True)
    host = _Session(tmp_path, [])
    registry = ToolRegistry()
    entered, release = threading.Event(), threading.Event()
    load = ContextBuilder.load_snapshot
    discarded = []
    discard = LoadedLifecycleCapability.discard

    def held_load(builder, snapshot):
        entered.set()
        assert release.wait(5)
        load(builder, snapshot)

    def observe_discard(capability, reason):
        discarded.append(capability)
        discard(capability, reason)

    monkeypatch.setattr(ContextBuilder, "load_snapshot", held_load)
    monkeypatch.setattr(LoadedLifecycleCapability, "discard", observe_discard)
    task = asyncio.create_task(host._ensure_lifecycle_runtime(
        conversation_id="conv_runnerdone", workspace_root=tmp_path, tool_registry=registry))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if stop == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done(), "Cancellation must still own the running history worker"
        else:
            await host._shutdown_lifecycle_runtimes("shutdown while preparing command history")
        release.set()
        if stop == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
        assert len(discarded) == 1
        assert not discarded[0].runtime.active
        assert not host._extension_runtime_states.get("conv_runnerdone", {}).get("runtime")
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host._shutdown_lifecycle_runtimes("test complete")
        clear_extension_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "shutdown", "continue"])
async def test_slow_capability_fingerprint_yields_before_loading_or_publishing_runtime(tmp_path, monkeypatch, stop):
    host = _Session(tmp_path, [])
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    loaded = []

    def fingerprint(source, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5), "Capability discovery blocked input reception"
        return "captured-content"

    class ReachedLoad(Exception):
        pass

    async def load(source, **kwargs):
        loaded.append(source)
        raise ReachedLoad()

    monkeypatch.setattr("backend.ws.agent_runner.is_workspace_trusted", lambda root: True)
    monkeypatch.setattr(ExtensionCapabilitySource, "fingerprint", fingerprint)
    monkeypatch.setattr(ExtensionCapabilitySource, "load", load)
    task = asyncio.create_task(host._ensure_lifecycle_runtime(
        conversation_id="conv_runnerdone", workspace_root=tmp_path, tool_registry=ToolRegistry()))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        if stop == "cancel":
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        elif stop == "shutdown":
            await host._shutdown_lifecycle_runtimes("during fingerprint")
        release.set()
        if stop == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            assert await task is None
        assert bool(loaded) is (stop == "continue")
        assert host._extension_runtime_states.get("conv_runnerdone", {}).get("runtime") is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await host._shutdown_lifecycle_runtimes("test complete")
