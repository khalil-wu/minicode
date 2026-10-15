"""Real auxiliary task leases must settle before their model generation closes."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from backend.llm.model_runtime import ModelRuntime
from backend.memory.generation import MemoryGenerationCoordinator, schedule_memory_startup
from backend.tests.test_ws_cold_connection import _connection
from backend.ws.agent_runner import _lease_session_llm_for_task
from backend.ws.manager import WebSocketManager


@asynccontextmanager
async def auxiliary(tmp_path, monkeypatch, *, resist_cancel=False):
    manager = WebSocketManager()
    session, _ = await manager.connect(**_connection(tmp_path, "auxiliary-owner"))
    adapter = session.llm
    session._llm_adapter_cache[("auxiliary",)] = adapter
    model_runtime = ModelRuntime(provider_configs={})
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observations = []

    async def memory_worker(self, *, current_conversation_id, startup_sandbox_mode=None, startup_subagent_header=None):
        observations.append(("worker-start", adapter.close_calls, model_runtime.active))
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            if resist_cancel:
                await release.wait()
            observations.append(("worker-cancelled", adapter.close_calls, model_runtime.active))
            raise
        observations.append(("worker-finished", adapter.close_calls, model_runtime.active))

    monkeypatch.setattr(MemoryGenerationCoordinator, "run_startup", memory_worker)
    worker = schedule_memory_startup(repository=session.conversation_repo, llm=adapter,
        workspace_root=tmp_path, current_conversation_id="conv_auxiliary")
    _lease_session_llm_for_task(session, adapter, worker)
    await entered.wait()
    shutdown_runtimes = session._shutdown_lifecycle_runtimes

    async def retire_models(reason):
        assert worker.done()
        observations.append(("model-runtime-retire", adapter.close_calls, model_runtime.active))
        model_runtime.retire()
        await shutdown_runtimes(reason)

    monkeypatch.setattr(session, "_shutdown_lifecycle_runtimes", retire_models)
    sleep = asyncio.sleep

    async def unit_grace(delay, *args, **kwargs):
        return await sleep(0 if delay == 30.0 else delay, *args, **kwargs)

    monkeypatch.setattr("backend.ws.manager.asyncio.sleep", unit_grace)
    case = dict(manager=manager, session=session, adapter=adapter, model=model_runtime,
        worker=worker, cancelled=cancelled, release=release, observations=observations)
    try:
        yield case
    finally:
        release.set()
        await asyncio.gather(worker, return_exceptions=True)
        await manager.shutdown(reason="auxiliary-test-cleanup")


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_full_shutdown_drains_actual_memory_task_before_model_runtime_and_adapter(tmp_path, monkeypatch):
    async with auxiliary(tmp_path, monkeypatch, resist_cancel=True) as c:
        closing = asyncio.create_task(c["manager"].shutdown_execution_host(c["session"].session_id))
        await c["cancelled"].wait()
        assert not c["worker"].done() and not closing.done()
        assert c["adapter"].close_calls == 0 and c["model"].active
        c["release"].set()
        assert await closing
        assert c["worker"].cancelled()
        assert c["observations"] == [("worker-start", 0, True), ("worker-cancelled", 0, True), ("model-runtime-retire", 0, True)]
        assert c["adapter"].close_calls == 1 and not c["model"].active
        assert c["manager"].execution_contexts.retained_hosts() == ()


@pytest.mark.asyncio
async def test_pending_memory_cleanup_keeps_original_model_runtime_adapter_and_host(tmp_path, monkeypatch):
    async with auxiliary(tmp_path, monkeypatch, resist_cancel=True) as c:
        monkeypatch.setattr("backend.ws.session_lifecycle.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.03)
        with pytest.raises(RuntimeError, match="model borrower"):
            await c["manager"].shutdown_execution_host(c["session"].session_id)
        assert c["adapter"].close_calls == 0 and c["model"].active
        assert not c["worker"].done()
        assert c["manager"].execution_host_for_session(c["session"].session_id) is c["session"]
        assert c["session"].session_lifecycle.llm_borrower_tasks() == {c["worker"]}
        c["release"].set()
        await asyncio.gather(c["worker"], return_exceptions=True)
        assert await c["manager"].shutdown_execution_host(c["session"].session_id)
        assert c["adapter"].close_calls == 1 and not c["model"].active


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [False, True])
async def test_transport_retirement_preserves_independent_memory_without_foreground_state(tmp_path, monkeypatch, reconnect):
    async with auxiliary(tmp_path, monkeypatch) as c:
        manager, session, worker = c["manager"], c["session"], c["worker"]
        outbox = session.event_outbox
        manager.disconnect(session.session_id, connection_generation=session.connection_generation)
        await manager._disconnect_tasks[session.session_id]
        assert not worker.done() and worker.cancelling() == 0
        assert c["adapter"].close_calls == 0 and c["model"].active
        assert manager.execution_host_for_session(session.session_id) is session
        assert manager.execution_contexts.contexts() == ()
        assert not session.run_manager.has_active_run()
        assert session.runtime_snapshot(include_capabilities=False)["active_task_id"] is None
        assert session.session_id not in manager._execution_host_shutdown_tasks
        if reconnect:
            restored, generation = await manager.connect(**_connection(tmp_path / "reconnect", session.session_id))
            assert restored is session and generation == 2 and restored.event_outbox is outbox
        c["release"].set()
        await worker
        if reconnect:
            assert manager.get_session(session.session_id) is session
            assert c["adapter"].close_calls == 0
        else:
            await until(lambda: manager.execution_contexts.retained_host(session.session_id) is None)
            assert c["adapter"].close_calls == 1 and not c["model"].active


@pytest.mark.asyncio
async def test_pending_home_command_blocks_resource_close_before_any_conversation_exists(tmp_path, monkeypatch):
    manager = WebSocketManager()
    session, _ = await manager.connect(**_connection(tmp_path, "home-command-owner"))
    cancelled, release = asyncio.Event(), asyncio.Event()

    async def preparing_home():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            raise

    task = asyncio.create_task(preparing_home())
    session.command_dispatcher.track_execution_command_task(task)
    await asyncio.sleep(0)
    monkeypatch.setattr("backend.ws.session_lifecycle.CANCELLATION_DRAIN_TIMEOUT_SECONDS", 0.03)
    try:
        with pytest.raises(RuntimeError, match="command task"):
            await manager.shutdown_execution_host(session.session_id)
        assert cancelled.is_set() and not task.done()
        assert session.llm.close_calls == 0
        assert manager.execution_contexts.contexts() == ()
        assert manager.execution_host_for_session(session.session_id) is session
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert await manager.shutdown_execution_host(session.session_id)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await manager.shutdown()
