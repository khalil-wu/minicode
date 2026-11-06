from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.runner import SandboxRunner


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_exits", [True, False])
async def test_command_owns_descendants_after_parent_exit_and_timeout(tmp_path: Path, monkeypatch, parent_exits: bool):
    pid_file = tmp_path / "child.pid"
    child_code = f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    parent_code = (
        "import subprocess,sys,time; "
        f"child=subprocess.Popen([sys.executable,'-c',{child_code!r}]); "
        "print('parent-ready', flush=True); "
        + ("time.sleep(.2)" if parent_exits else "time.sleep(30)")
    )
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=1.5))
    monkeypatch.setattr("backend.sandbox.runner._IO_DRAIN_TIMEOUT_SECONDS", .1)
    started = time.monotonic()
    try:
        result = await runner.run("", argv=[sys.executable, "-c", parent_code], cwd=tmp_path)
        assert pid_file.exists(), result
        assert "parent-ready" in result.stdout
        assert result.timed_out is (not parent_exits), result
        assert result.cleanup_pending is False, result
        if parent_exits:
            assert result.exit_code == 0
            assert time.monotonic() - started < 1.5
        if os.name == "nt" or not parent_exits:
            child = int(pid_file.read_text())
            assert not psutil.pid_exists(child), f"owned child {child} survived completed cleanup"
    finally:
        if pid_file.exists():
            try:
                process = psutil.Process(int(pid_file.read_text()))
                process.kill()
                await asyncio.to_thread(process.wait, 3)
            except psutil.NoSuchProcess:
                pass
        await runner.cleanup()


def _orphan_tree_code(pid_file: Path) -> str:
    """Root stays alive; its intermediate child exits after starting a grandchild.

    The grandchild has no stdio pipes and no living parent link to the root,
    so only process ownership (not pipe EOF or a parent/child walk) finds it.
    """
    grandchild = f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    intermediate = (
        "import subprocess,sys; "
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}], stdin=subprocess.DEVNULL, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"
    )
    return "\n".join((
        "import subprocess, sys, time",
        "from pathlib import Path",
        f"subprocess.run([sys.executable, '-c', {intermediate!r}])",
        f"while not Path({str(pid_file)!r}).exists():",
        "    time.sleep(.02)",
        "print('orphan-ready', flush=True)",
        "time.sleep(30)",
    ))


async def _wait_for(path: Path, timeout: float = 10.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(path.read_text())
        except (OSError, ValueError):
            await asyncio.sleep(.02)
    raise AssertionError(f"{path} was never written")


async def _kill_leftover(pid_file: Path) -> None:
    try:
        process = psutil.Process(int(pid_file.read_text()))
        process.kill()
        await asyncio.to_thread(process.wait, 3)
    except (OSError, ValueError, psutil.NoSuchProcess):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["timeout", "cancel_event", "task_cancel"])
async def test_stopped_command_reaps_grandchild_whose_parent_already_exited(tmp_path: Path, stop: str):
    pid_file = tmp_path / "grandchild.pid"
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=4.0 if stop == "timeout" else 30.0))
    cancel_event = asyncio.Event()
    task = asyncio.create_task(runner.run("", argv=[sys.executable, "-c", _orphan_tree_code(pid_file)],
        cwd=tmp_path, cancel_event=cancel_event))
    try:
        grandchild = await _wait_for(pid_file)
        await asyncio.sleep(.3)
        assert psutil.pid_exists(grandchild)
        if stop == "cancel_event":
            cancel_event.set()
        elif stop == "task_cancel":
            task.cancel()
        result = await asyncio.wait_for(task, 20)
        assert result.timed_out is (stop == "timeout"), result
        assert result.cancelled is (stop != "timeout"), result
        assert result.cleanup_pending is False, result
        assert not psutil.pid_exists(grandchild), f"grandchild {grandchild} survived a reported-complete cleanup"
        assert runner.process is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await _kill_leftover(pid_file)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_stdout_reader_failure_propagates_and_releases_the_tree(tmp_path: Path, monkeypatch):
    import backend.sandbox.runner as runner_module

    pid_file = tmp_path / "grandchild.pid"
    original_append = runner_module._OutputCapture.append

    def failing_append(self, chunk):
        if b"orphan-ready" in chunk:
            raise RuntimeError("capture disk full")
        return original_append(self, chunk)

    monkeypatch.setattr(runner_module._OutputCapture, "append", failing_append)
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=30.0))
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="capture disk full") as raised:
            await runner.run("", argv=[sys.executable, "-c", _orphan_tree_code(pid_file)], cwd=tmp_path)
        assert time.monotonic() - started < 15, "reader failure waited for process exit"
        assert getattr(raised.value, "cleanup_pending", False) is False
        assert not psutil.pid_exists(int(pid_file.read_text()))
        assert runner.process is None
    finally:
        await _kill_leftover(pid_file)
        await runner.cleanup()


@pytest.mark.asyncio
async def test_waiting_for_a_slow_command_does_not_spin_on_finished_readers(tmp_path: Path, monkeypatch):
    import backend.sandbox.runner as runner_module

    real_wait = asyncio.wait
    calls = {"count": 0}

    async def counting_wait(*args, **kwargs):
        calls["count"] += 1
        return await real_wait(*args, **kwargs)

    # stdin_task finishes at once (no stdin data); stdout hits EOF early.
    code = "import os,sys,time; print('x', flush=True); os.close(1); os.close(2); time.sleep(1.5)"
    monkeypatch.setattr(runner_module.asyncio, "wait", counting_wait)
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=30.0))
    try:
        result = await runner.run("", argv=[sys.executable, "-c", code], cwd=tmp_path)
    finally:
        monkeypatch.setattr(runner_module.asyncio, "wait", real_wait)
        await runner.cleanup()
    assert result.exit_code == 0 and "x" in result.stdout, result
    assert calls["count"] < 20, f"asyncio.wait called {calls['count']} times during a 1.5s command"


windows_only = pytest.mark.skipif(os.name != "nt", reason="Windows Job ownership")


@windows_only
@pytest.mark.asyncio
async def test_job_attach_failure_kills_the_suspended_process(tmp_path: Path, monkeypatch):
    from backend.workspace.executor_process_owner import WindowsProcessJob

    spawned: list[int] = []

    def failing_attach(self, process):
        spawned.append(process.pid)
        raise PermissionError("assign denied")

    monkeypatch.setattr(WindowsProcessJob, "attach_suspended", failing_attach)
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=30.0))
    result = await runner.run("", argv=[sys.executable, "-c", "print('never runs')"], cwd=tmp_path)
    assert "Windows job" in result.execution_error and "assign denied" in result.execution_error
    assert result.exit_code == 1 and result.stdout == ""
    assert result.cleanup_pending is False, result
    assert spawned and not psutil.pid_exists(spawned[0])
    assert runner.process is None and runner._process_job is None


@windows_only
@pytest.mark.asyncio
async def test_unproven_job_close_keeps_pending_evidence_until_cleanup(tmp_path: Path, monkeypatch):
    from backend.workspace.executor_process_owner import WindowsProcessJob

    original_aclose = WindowsProcessJob.aclose
    failures = {"left": 1}

    async def flaky_aclose(self):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("job still has live descendants")
        await original_aclose(self)

    monkeypatch.setattr(WindowsProcessJob, "aclose", flaky_aclose)
    pid_file = tmp_path / "child.pid"
    child = f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    parent = "\n".join((
        "import subprocess, sys, time",
        "from pathlib import Path",
        (f"subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.DEVNULL, "
         "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"),
        f"while not Path({str(pid_file)!r}).exists():",
        "    time.sleep(.02)",
    ))
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=30.0))
    try:
        result = await runner.run("", argv=[sys.executable, "-c", parent], cwd=tmp_path)
        assert result.exit_code == 0
        assert result.cleanup_pending is True and result.cleanup_reason == "process_job_survived_completion"
        assert runner._process_job is not None
        assert psutil.pid_exists(int(pid_file.read_text()))
        assert await runner.cleanup() is True
        assert runner._process_job is None
        assert not psutil.pid_exists(int(pid_file.read_text()))
    finally:
        await _kill_leftover(pid_file)
        await runner.cleanup()


@windows_only
@pytest.mark.asyncio
async def test_closed_job_whose_exit_proof_failed_is_rechecked_by_cleanup(tmp_path: Path, monkeypatch):
    real_wait_procs = psutil.wait_procs
    unproven = {"left": 1}

    def flaky_wait_procs(procs, timeout=None):
        if unproven["left"]:
            unproven["left"] -= 1
            return [], list(procs)
        return real_wait_procs(procs, timeout)

    # _close_windows_job imports psutil locally, so patch the module attribute.
    monkeypatch.setattr(psutil, "wait_procs", flaky_wait_procs)
    pid_file = tmp_path / "child.pid"
    child = f"from pathlib import Path; import os,time; Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    parent = "\n".join((
        "import subprocess, sys, time",
        "from pathlib import Path",
        (f"subprocess.Popen([sys.executable, '-c', {child!r}], stdin=subprocess.DEVNULL, "
         "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)"),
        f"while not Path({str(pid_file)!r}).exists():",
        "    time.sleep(.02)",
    ))
    runner = SandboxRunner(SandboxPolicy.bypass(timeout=30.0))
    try:
        result = await runner.run("", argv=[sys.executable, "-c", parent], cwd=tmp_path)
        assert result.cleanup_pending is True and result.cleanup_reason == "process_job_survived_completion"
        job = runner._process_job
        assert job is not None and job.closed, "handle is closed; only the exit proof is missing"
        assert await runner.cleanup() is True
        assert runner._process_job is None and runner.process is None
        assert not psutil.pid_exists(int(pid_file.read_text()))
    finally:
        await _kill_leftover(pid_file)
        await runner.cleanup()
