from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from contextlib import suppress

import psutil
import pytest

from backend.subprocesses import (
    SubprocessTimeoutError,
    communicate,
    process_group_kwargs,
    spawn_exec,
    terminate_process_tree,
)


def _wait_until_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not psutil.pid_exists(pid):
            return True
        try:
            process = psutil.Process(pid)
            if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.NoSuchProcess:
            return True
        time.sleep(0.02)
    return False


def test_communicate_timeout_kills_the_complete_process_tree() -> None:
    async def scenario() -> tuple[int, int]:
        child_script = "import time; time.sleep(60)"
        parent_script = (
            "import json, subprocess, sys, time; "
            "child=subprocess.Popen([sys.executable, '-c', sys.argv[1]]); "
            "print(json.dumps({'child_pid': child.pid}), flush=True); "
            "time.sleep(60)"
        )
        proc = await spawn_exec(
            sys.executable,
            "-c",
            parent_script,
            child_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdout is not None
        child_line = await asyncio.wait_for(proc.stdout.readline(), timeout=3.0)
        child_pid = int(json.loads(child_line)["child_pid"])

        with pytest.raises(asyncio.TimeoutError):
            await communicate(proc, timeout=0.05)
        return proc.pid, child_pid

    parent_pid, child_pid = asyncio.run(scenario())
    assert _wait_until_gone(parent_pid)
    assert _wait_until_gone(child_pid)


def test_communicate_timeout_reports_the_proof_of_exit_verdict() -> None:
    """The wrapper must not discard ``terminate_process_tree``'s bool.

    ``False`` means the tree may still be running, so the caller owns an
    unfinished cleanup; the verdict travels on the raised exception.
    """

    async def scenario() -> SubprocessTimeoutError:
        proc = await spawn_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        with pytest.raises(asyncio.TimeoutError) as excinfo:
            await communicate(proc, timeout=0.05)
        return excinfo.value  # type: ignore[return-value]

    error = asyncio.run(scenario())
    assert isinstance(error, SubprocessTimeoutError)
    assert error.cleanup_pending is False
    assert error.cleanup_reason == ""


def test_communicate_reports_unproven_cleanup_when_the_tree_survives(monkeypatch) -> None:
    async def scenario() -> SubprocessTimeoutError:
        import backend.subprocesses as subprocesses

        async def never_reaped(_proc):
            return False

        monkeypatch.setattr(subprocesses, "terminate_process_tree", never_reaped)
        proc = await spawn_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            with pytest.raises(asyncio.TimeoutError) as excinfo:
                await communicate(proc, timeout=0.05)
            return excinfo.value  # type: ignore[return-value]
        finally:
            proc.kill()
            with suppress(ProcessLookupError):
                await proc.wait()

    error = asyncio.run(scenario())
    assert error.cleanup_pending is True
    assert "subprocess_tree_survived_kill" in error.cleanup_reason


def test_terminate_process_tree_supports_threaded_subprocess_fallbacks() -> None:
    child_script = "import time; time.sleep(60)"
    parent_script = (
        "import json, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', sys.argv[1]]); "
        "print(json.dumps({'child_pid': child.pid}), flush=True); "
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_script, child_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **process_group_kwargs(),
    )
    assert proc.stdout is not None
    child_pid = int(json.loads(proc.stdout.readline())["child_pid"])

    asyncio.run(terminate_process_tree(proc))

    assert _wait_until_gone(proc.pid)
    assert _wait_until_gone(child_pid)
