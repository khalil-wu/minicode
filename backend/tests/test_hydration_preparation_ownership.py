from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from backend.agent import runtime as runtime_module
from backend.tests.test_agent_runner_done_fallback import _Session


@pytest.mark.asyncio
async def test_runtime_probe_observes_only_complete_initialization_without_waiting_for_it(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = runtime_module.AgentRuntime
    created = []
    def construct():
        entered.set()
        assert release.wait(5)
        runtime = original(metrics_file=tmp_path / "metrics.jsonl", enable_lease_heartbeat=False)
        created.append(runtime)
        return runtime
    monkeypatch.setattr(runtime_module, "_DEFAULT_RUNTIME", None)
    monkeypatch.setattr(runtime_module, "AgentRuntime", construct)
    task = asyncio.create_task(asyncio.to_thread(runtime_module.default_runtime))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert await asyncio.wait_for(asyncio.to_thread(runtime_module.default_runtime_if_initialized), .5) is None
        assert not task.done()
        release.set()
        actual = await task
        assert runtime_module.default_runtime_if_initialized() is actual
        assert runtime_module.default_runtime() is actual
    finally:
        release.set()
        await task
        for runtime in created:
            runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_reconcile_initializes_runtime_off_loop_without_reading_unrelated_swarm_lists(tmp_path, monkeypatch):
    session = _Session(tmp_path, [])
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    def runtime():
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(5)
        def list_runs(*, conversation_id, include_subagents, include_swarm):
            assert threading.get_ident() == loop_thread
            assert conversation_id == "conv_runnerdone" and include_subagents and not include_swarm
            return {"subagents": []}
        return SimpleNamespace(list_runs=list_runs)
    monkeypatch.setattr("backend.ws.agent_runner.default_runtime", runtime)
    task = asyncio.create_task(session.reconcile_persisted_ui_agent_state("conv_runnerdone"))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not task.done()
        assert not session._conversation_projection_lock("conv_runnerdone").locked()
    finally:
        release.set()
        assert (await task).id == "conv_runnerdone"

