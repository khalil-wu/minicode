from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner


@pytest.mark.parametrize("interactive", [False, True])
def test_sandbox_interactive_spawn_failure_removes_prepared_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interactive: bool
) -> None:
    runner = SandboxRunner(SandboxPolicy.bypass())
    temporary = tmp_path / "sandbox-setup"

    def wrap(*args, **kwargs) -> str:
        temporary.mkdir()
        runner._low_integrity_temp_dir = temporary
        return "launch"

    async def failed_spawn(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(runner, "_wrap_command", wrap)
    monkeypatch.setattr("backend.sandbox.runner.spawn_shell", failed_spawn)

    async def scenario() -> None:
        with pytest.raises(OSError, match="spawn failed"):
            if interactive:
                await runner.spawn_interactive(["server"])
            else:
                await runner.spawn_shell_interactive("server")

    asyncio.run(scenario())
    assert not temporary.exists()
    assert runner.process is None


@pytest.mark.parametrize("interactive", [False, True])
def test_sandbox_interactive_cancellation_reaps_spawned_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, interactive: bool
) -> None:
    runner = SandboxRunner(SandboxPolicy.bypass())
    temporary = tmp_path / "sandbox-setup"
    process = SimpleNamespace(pid=1234, returncode=None)
    ready = asyncio.Event()
    killed: list[object] = []

    def wrap(*args, **kwargs) -> str:
        temporary.mkdir()
        runner._low_integrity_temp_dir = temporary
        return "launch"

    async def spawn(*args, **kwargs):
        return process

    async def await_ready(_process) -> None:
        ready.set()
        await asyncio.Event().wait()

    async def kill_tree(_process) -> bool:
        killed.append(_process)
        runner.process = None
        runner._cleanup_sandbox_setup_state()
        return True

    monkeypatch.setattr(runner, "_wrap_command", wrap)
    monkeypatch.setattr(runner, "_await_sandbox_ready", await_ready)
    monkeypatch.setattr(runner, "_kill_tree", kill_tree)
    monkeypatch.setattr("backend.sandbox.runner.spawn_shell", spawn)

    async def scenario() -> None:
        call = asyncio.create_task(
            runner.spawn_interactive(["server"])
            if interactive
            else runner.spawn_shell_interactive("server")
        )
        await ready.wait()
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call

    asyncio.run(scenario())
    assert killed == [process]
    assert runner.process is None
    assert not temporary.exists()
