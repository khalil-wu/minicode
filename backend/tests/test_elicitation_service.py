from __future__ import annotations

import asyncio
import sys
import time
import os
import shlex

import pytest

from backend.agent.elicitation import ElicitationService
from backend.sandbox import SandboxPolicy
from backend.sandbox import SandboxRunner
from backend.terminal.manager import BackgroundCommandManager
from backend.agent.code_execution_store import CodeExecutionStore
from backend.tests.test_executor_tool_admission import executor_host, batch


@pytest.mark.asyncio
async def test_external_and_internal_interactions_release_only_their_own_registration():
    service = ElicitationService()
    assert service.increment() == 1
    assert service.increment() == 2
    with service.register():
        waiting = asyncio.create_task(service.wait_until_clear())
        assert service.decrement() == 1
        assert service.decrement() == 0
        await asyncio.sleep(0)
        assert service.paused and not waiting.done()
        with pytest.raises(ValueError, match="already zero"):
            service.decrement()
    await waiting
    assert not service.paused
    service.external_count = (1 << 63) - 1
    with pytest.raises(OverflowError):
        service.increment()


@pytest.mark.asyncio
async def test_an_interaction_started_during_a_wait_suspends_only_the_remaining_budget():
    service = ElicitationService()
    process = asyncio.create_task(asyncio.Event().wait())
    waiting = asyncio.create_task(service.wait_any({process}, .15))
    try:
        await asyncio.sleep(.04)
        service.increment()
        await asyncio.sleep(.18)
        assert not waiting.done()
        released = time.monotonic()
        service.decrement()
        done, pending = await asyncio.wait_for(waiting, 1)
        assert done == set() and pending == {process}
        assert .07 <= time.monotonic() - released < .4
        assert not process.done()
    finally:
        service.reset_external()
        process.cancel()
        await asyncio.gather(waiting, process, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_real_command_finishes_while_external_interaction_holds_its_observation(tmp_path, cancel):
    service = ElicitationService()
    manager = BackgroundCommandManager(session_id="elicitation-command")
    finished = asyncio.Event()
    async def complete(command):
        finished.set()
    manager._on_completed = complete
    command = await manager.run_background("owned completion", argv=[sys.executable, "-u", "-c", "print('actual process output')"],
        cwd=str(tmp_path), conversation_id="owner", sandbox_policy=SandboxPolicy.bypass(timeout=5))
    service.increment()
    cancel_event = asyncio.Event()
    waiting = asyncio.create_task(manager.wait(command.command_id, conversation_id="owner", wait_ms=10,
        cursor=0, cancel_event=cancel_event, elicitations=service))
    try:
        await asyncio.wait_for(finished.wait(), 5)
        assert command.exit_code == 0 and not waiting.done()
        if cancel:
            cancel_event.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(waiting, 1)
            assert service.paused
        else:
            service.decrement()
            observed = await asyncio.wait_for(waiting, 1)
            assert observed is command and observed.exit_code == 0
            assert "actual process output" in manager.get_output_chunk(command.command_id,
                conversation_id="owner", cursor=0, max_chars=1000)[0]
    finally:
        service.reset_external()
        await asyncio.gather(waiting, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
async def test_explicit_process_timeout_pauses_but_process_exit_is_observed_immediately(tmp_path):
    service = ElicitationService()
    service.increment()
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=.05))
    try:
        result = await asyncio.wait_for(runner.run("real delayed completion", cwd=str(tmp_path),
            argv=[sys.executable, "-u", "-c", "import time;time.sleep(.15);print('completed while paused')"],
            elicitations=service), 5)
        assert result.exit_code == 0 and not result.timed_out
        assert "completed while paused" in result.stdout
        assert service.paused
    finally:
        service.reset_external()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_command_wait_budget_excludes_time_spent_in_an_external_interaction(tmp_path):
    service = ElicitationService()
    manager = BackgroundCommandManager(session_id="elicitation-clock")
    command = await manager.run_background("held real process", argv=[sys.executable, "-u", "-c", "import time;time.sleep(20)"],
        cwd=str(tmp_path), conversation_id="owner", sandbox_policy=SandboxPolicy.bypass(timeout=30))
    await manager.wait_for_startup(command.command_id, conversation_id="owner")
    service.increment()
    waiting = asyncio.create_task(manager.wait(command.command_id, conversation_id="owner", wait_ms=100,
        elicitations=service))
    try:
        await asyncio.sleep(.15)
        assert not waiting.done()
        released_at = time.monotonic()
        service.decrement()
        result = await asyncio.wait_for(waiting, 2)
        assert time.monotonic() - released_at >= .08
        assert result.status == "running"
    finally:
        service.reset_external()
        await asyncio.gather(waiting, return_exceptions=True)
        await manager.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
async def test_actual_executor_timeout_and_tool_observation_share_the_thread_pause(executor_host):
    host = executor_host
    store = CodeExecutionStore()
    host.context.run_context.code_store = store
    service = store.elicitations
    service.increment()
    script = host.workspace / "delayed.py"
    script.write_text("import time\ntime.sleep(1.2)\nprint('PAUSED_TIMEOUT_COMPLETED')\n", encoding="utf-8")
    command = ("& " + " ".join("'" + value.replace("'", "''") + "'" for value in (sys.executable, str(script)))
               if os.name == "nt" else shlex.join([sys.executable, str(script)]))
    task = asyncio.create_task(batch(host, "run_command", {"command": command, "timeout": 1, "yield_time_ms": 1}))
    try:
        async with asyncio.timeout(8):
            while not host.background._commands:
                await asyncio.sleep(.01)
            process, = host.background._commands.values()
            while process.status == "running":
                process.output_changed.clear()
                await process.output_changed.wait()
        process, = host.background._commands.values()
        assert process.exit_code == 0 and not task.done()
        assert "PAUSED_TIMEOUT_COMPLETED" in process.output
        service.decrement()
        _, result, _ = await asyncio.wait_for(task, 3)
        assert not result.get("is_error")
    finally:
        service.reset_external()
        await asyncio.gather(task, return_exceptions=True)
        await store.aclose()
