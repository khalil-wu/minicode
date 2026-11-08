from __future__ import annotations

import asyncio
import subprocess
import sys
import threading

import pytest

from backend.agent.code_execution_vm import CodeVM


def test_loading_the_tool_runtime_does_not_load_v8_or_open_an_isolate():
    result = subprocess.run([sys.executable, "-c", """
import sys
from backend.agent.code_execution import CodeExecutionRuntime
from backend.agent.code_execution_vm import CodeVM
assert not any(name == 'py_mini_racer' or name.startswith('py_mini_racer.') for name in sys.modules)
vm = CodeVM()
vm.close()
assert not any(name == 'py_mini_racer' or name.startswith('py_mini_racer.') for name in sys.modules)
"""], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_closed_unstarted_cell_does_not_create_a_new_isolate():
    vm = CodeVM()
    vm.close()
    with pytest.raises(RuntimeError, match="closed"):
        await vm.step("start", {"code": "text('unreachable')", "tools": [], "storage": {}})


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_native_initialization_yields_and_cancelled_start_closes_actual_context(monkeypatch, cancel):
    import backend.agent.code_execution_vm as module
    entered, release = threading.Event(), threading.Event()
    loop_thread = threading.get_ident()
    runtime = module._runtime
    created = []

    def held():
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(4)
        constructor = runtime()
        def create():
            assert threading.get_ident() == loop_thread
            context = constructor()
            created.append(context)
            return context
        return create

    monkeypatch.setattr(module, "_runtime", held)
    vm = CodeVM()

    async def run():
        try:
            return await vm.step("start", {"code": "text('real V8 output')", "tools": [], "storage": {}})
        finally:
            vm.close()

    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        assert not task.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
    finally:
        release.set()
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["output"][0]["text"] == "real V8 output"
    assert vm.context is None and len(created) == (0 if cancel else 1)
    if created:
        with pytest.raises(Exception):
            created[0].eval("1+1")
