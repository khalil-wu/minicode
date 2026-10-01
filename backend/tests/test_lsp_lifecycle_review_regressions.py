"""Finite launch-ownership verification; not business-source audit coverage."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.lsp import client as lsp
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxUnavailableError
from backend.subprocesses import spawn_exec, terminate_process_tree


class _FailedReadyRunner:
    """Actual OS child; only failed readiness/first cleanup verdict are seams."""

    def __init__(self, workspace: Path, cleanup_complete: bool) -> None:
        self._policy = SandboxPolicy(workspace_root=workspace)
        self.cleanup_complete = cleanup_complete
        self.cleanup_calls = 0
        self.process: asyncio.subprocess.Process | None = None
        self.spawned_process: asyncio.subprocess.Process | None = None
        self.setup_owned = False

    def capability(self) -> SimpleNamespace:
        return SimpleNamespace(available=True)

    async def spawn_interactive(self, *args, **kwargs):
        self.process = await spawn_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self.spawned_process = self.process
        self.setup_owned = True
        error = SandboxUnavailableError("finite failed namespace readiness")
        error.cleanup_pending = True
        raise error

    async def cleanup(self) -> bool:
        self.cleanup_calls += 1
        if not self.cleanup_complete:
            return False
        if self.process is not None and not await terminate_process_tree(self.process):
            return False
        self.process = None
        self.setup_owned = False
        return True


@pytest.mark.parametrize("cleanup_complete", [False, True])
def test_failed_launch_runner_owner_survives_until_true_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_complete: bool,
) -> None:
    async def scenario() -> None:
        manager = lsp.LSPManager()
        runner = _FailedReadyRunner(tmp_path, cleanup_complete)
        monkeypatch.setattr(lsp, "_resolve_server_executable", lambda *args: sys.executable)
        monkeypatch.setattr(lsp, "_lsp_sandbox_runner", lambda *args: runner)
        try:
            result = await manager.get_client("sample.py", str(tmp_path))
            assert result is None
            assert runner.cleanup_calls == 1
            if cleanup_complete:
                assert manager._clients == {}
                assert runner.process is None
                assert not runner.setup_owned
                assert runner.spawned_process.returncode is not None
            else:
                owned_client, = manager._clients.values()
                assert owned_client._sandbox_runner is runner
                assert owned_client._process is None
                assert runner.process.returncode is None
                assert runner.setup_owned
                with pytest.raises(RuntimeError, match="remain owned"):
                    await manager.shutdown_all()
                assert list(manager._clients.values()) == [owned_client]
                assert runner.cleanup_calls == 2
                runner.cleanup_complete = True
                await manager.shutdown_all()
                assert manager._clients == {}
                assert runner.process is None
                assert not runner.setup_owned
                assert runner.cleanup_calls == 3
                assert runner.spawned_process.returncode is not None
        finally:
            if runner.spawned_process is not None:
                assert await terminate_process_tree(runner.spawned_process)

    asyncio.run(scenario())
