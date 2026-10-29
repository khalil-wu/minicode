"""Real Local executor transport, process trees and captured OS permissions."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import psutil
import pytest

from backend.environments.errors import ExecutorClosed, ExecutorProtocolError
from backend.workspace.executor_local import LocalExecutionEnvironmentBackend, LocalProcessBackend
from backend.workspace.executor_local_policy import local_sandbox_policy
from backend.sandbox.runner import SandboxRunner, SandboxUnavailableError


async def collect(process):
    chunks = []
    after = 0
    async with asyncio.timeout(20):
        while True:
            result = await process.read(after_seq=after, wait_ms=1000)
            chunks.extend(result.chunks)
            after = result.next_seq - 1
            if result.closed:
                return chunks, result


def python(*args):
    return [sys.executable, "-I", "-S", "-u", *args]


@pytest.mark.asyncio
async def test_local_info_reports_real_host_and_only_implemented_capabilities(tmp_path):
    backend = LocalExecutionEnvironmentBackend()
    try:
        info = await backend.info()
        assert info["cwd"] == Path.cwd().as_uri()
        assert info["userHomeDir"] == Path.home().as_uri()
        assert Path(info["shell"]["path"]).is_file()
        assert info["platformOs"] == ("windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else sys.platform)
        assert info["capabilities"]["sandboxedFileStreaming"]
        assert not any(info["capabilities"][key] for key in ("windowsMxc", "networkProxyLaunch", "shellSnapshotV2", "capabilityDiscoveryV2"))
        await backend.filesystem.write_file((tmp_path / "owned").as_uri(), b"owned", sandbox=None)
    finally:
        await backend.close()
    with pytest.raises(ExecutorClosed):
        await backend.info()


@pytest.mark.asyncio
async def test_real_process_preserves_argv_binary_stdout_stderr_and_exit_events(tmp_path):
    backend = LocalProcessBackend()
    try:
        process = await backend.start(python("-c", "import os,sys; os.write(1,sys.argv[1].encode()); os.write(2,b'\\x00\\xffstderr'); sys.exit(7)", "中文 & literal $()"), tmp_path.as_uri(), sandbox=None)
        chunks, result = await collect(process)
        assert b"".join(chunk.chunk for chunk in chunks if chunk.stream == "stdout") == "中文 & literal $()".encode()
        assert b"".join(chunk.chunk for chunk in chunks if chunk.stream == "stderr") == b"\x00\xffstderr"
        assert result.exited and result.exit_code == 7 and not result.failure and not result.sandbox_denied
        receiver = process.subscribe_events()
        events = []
        while not events or events[-1]["method"] != "process/closed":
            events.append(await receiver.recv())
        assert events[-2]["method"] == "process/exited" or any(event["method"] == "process/exited" for event in events)
        assert [event["seq"] for event in events] == sorted(event["seq"] for event in events)
        assert await process.terminate() is False
    finally:
        await backend.close()
    assert not backend.processes


@pytest.mark.asyncio
async def test_process_observer_subscribes_before_fast_output_and_failure_prevents_launch(tmp_path):
    backend = LocalProcessBackend()
    receivers = []
    try:
        process = await backend.start(python("-c", "print('FIRST_FAST_OUTPUT',flush=True)"), tmp_path.as_uri(),
            sandbox=None, process_observer=lambda process: receivers.append(process.subscribe_events()))
        events = []
        async with asyncio.timeout(10):
            while not events or events[-1]["method"] != "process/closed":
                events.append(await receivers[0].recv())
        assert b"FIRST_FAST_OUTPUT" in b"".join(event["chunk"] for event in events if event["method"] == "process/output")
        assert any(event["method"] == "process/exited" and event["exitCode"] == 0 for event in events)
        await process.aclose()
        marker = tmp_path / "must-not-launch"

        refused = []
        def reject(_process):
            refused.append(_process)
            raise RuntimeError("owned observer rejected before spawn")

        with pytest.raises(RuntimeError, match="before spawn"):
            await backend.start(python("-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
                tmp_path.as_uri(), sandbox=None, process_observer=reject)
        assert not marker.exists() and not backend.processes
        assert refused[0].closed and not refused[0].accepted and refused[0].watch_task is None
        assert (await refused[0].aclose())["closed"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_missing_executable_closes_original_unaccepted_handle_with_start_failure(tmp_path):
    backend = LocalProcessBackend()
    captured = []
    try:
        with pytest.raises(OSError):
            await backend.start([str(tmp_path / "not-an-executable")], tmp_path.as_uri(), sandbox=None,
                process_observer=captured.append)
        [handle] = captured
        receipt = await handle.aclose()
        assert receipt["closed"] and receipt["failure"]
        assert handle.closed and not handle.accepted and handle.watch_task is None
        assert handle.process is None and not backend.processes
        receiver = handle.subscribe_events()
        assert (await receiver.recv())["method"] == "process/closed"
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("tty", [False, True])
async def test_rejected_readiness_after_actual_spawn_closes_original_handle_and_child(tmp_path, monkeypatch, tty):
    backend = LocalProcessBackend()
    captured, children = [], []
    async def reject_ready(runner, process, **kwargs):
        children.append(process)
        raise SandboxUnavailableError("owned actual-child readiness rejection")
    monkeypatch.setattr(SandboxRunner, "_await_sandbox_ready", reject_ready)
    try:
        with pytest.raises(SandboxUnavailableError, match="actual-child readiness rejection"):
            await backend.start(python("-c", "import time;time.sleep(60)"), tmp_path.as_uri(), sandbox=None,
                tty=tty, process_observer=captured.append)
        [handle], [child] = captured, children
        receipt = await handle.aclose()
        assert receipt["closed"] and "readiness rejection" in receipt["failure"]
        assert not handle.accepted and handle.watch_task is None and handle.closed
        assert child.returncode is not None and not psutil.pid_exists(child.pid)
        assert not backend.processes
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_unacknowledged_rejected_start_cleanup_does_not_report_closed(tmp_path, monkeypatch):
    backend = LocalProcessBackend()
    captured = []
    async def unsettled_cleanup(runner):
        return False
    monkeypatch.setattr(SandboxRunner, "cleanup", unsettled_cleanup)
    try:
        with pytest.raises(ExceptionGroup, match="startup and cleanup failed"):
            await backend.start([str(tmp_path / "not-an-executable")], tmp_path.as_uri(), sandbox=None,
                process_observer=captured.append)
        [handle] = captured
        assert not handle.closed and not handle.accepted and handle.watch_task is None
        with pytest.raises(ExceptionGroup, match="did not close"):
            await handle.aclose()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_real_pipe_write_deduplication_and_captured_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_LOCAL_PROCESS_CAPTURE", "inherited")
    backend = LocalProcessBackend()
    try:
        process = await backend.start(python("-c", "import os,sys; print(os.getenv('MINICODE_LOCAL_PROCESS_CAPTURE'),os.getenv('MINICODE_LOCAL_PROCESS_EXPLICIT'),flush=True); print(sys.stdin.buffer.readline().hex(),flush=True)"),
            tmp_path.as_uri(), sandbox=None, pipe_stdin=True,
            env={"MINICODE_LOCAL_PROCESS_EXPLICIT": "explicit"}, env_policy={"inherit": "all"})
        assert await process.write(b"\x00\xff\n", write_id="same-write") == "accepted"
        assert await process.write(b"must-not-duplicate\n", write_id="same-write") == "accepted"
        chunks, result = await collect(process)
        assert b"".join(chunk.chunk for chunk in chunks).replace(b"\r\n", b"\n") == b"inherited explicit\n00ff0a\n"
        assert result.exit_code == 0
        assert await process.write(b"retry", write_id="same-write") == "accepted"
        assert await process.write(b"new", write_id="new-write") == "stdinClosed"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_real_exit_precedes_inherited_pipe_close_and_owner_kills_original_descendant(tmp_path):
    backend = LocalProcessBackend()
    code = "import subprocess,sys; p=subprocess.Popen([sys.executable,'-I','-S','-u','-c','import time; time.sleep(60)']); print(p.pid,flush=True)"
    process = await backend.start(python("-c", code), tmp_path.as_uri(), sandbox=None)
    descendant = None
    after = 0
    try:
        async with asyncio.timeout(15):
            while True:
                result = await process.read(after_seq=after, wait_ms=1000)
                after = result.next_seq - 1
                output = b"".join(chunk.chunk for chunk in result.chunks)
                if output:
                    descendant = psutil.Process(int(output.strip()))
                if result.exited:
                    break
        assert result.exit_code == 0 and not result.closed
        assert descendant is not None and descendant.is_running()
        await process.aclose()
        await asyncio.to_thread(descendant.wait, timeout=5)
        assert not descendant.is_running()
        assert process.closed and not backend.processes
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_closed_parent_with_redirected_child_still_releases_original_tree(tmp_path):
    backend = LocalProcessBackend()
    pidfile = tmp_path / "child-pid"
    code = "import pathlib,subprocess,sys; p=subprocess.Popen([sys.executable,'-I','-S','-c','import time; time.sleep(60)'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); pathlib.Path(sys.argv[1]).write_text(str(p.pid))"
    try:
        process = await backend.start(python("-c", code, str(pidfile)), tmp_path.as_uri(), sandbox=None)
        _, result = await collect(process)
        child = psutil.Process(int(pidfile.read_text()))
        assert result.closed and child.is_running()
        await process.aclose()
        await asyncio.to_thread(child.wait, timeout=5)
        assert not child.is_running()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_cancelled_start_retains_actual_process_until_owner_cleanup(tmp_path, monkeypatch):
    backend = LocalProcessBackend()
    original = SandboxRunner.spawn_interactive
    arrived, release = asyncio.Event(), asyncio.Event()
    spawned = []

    async def paused(runner, *args, **kwargs):
        process = await original(runner, *args, **kwargs)
        spawned.append(process)
        arrived.set()
        await release.wait()
        return process

    monkeypatch.setattr(SandboxRunner, "spawn_interactive", paused)
    starting = asyncio.create_task(backend.start(python("-c", "import time; time.sleep(60)"), tmp_path.as_uri(), sandbox=None))
    await arrived.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert backend.processes and spawned[0].returncode is None
    release.set()
    await backend.close()
    assert spawned[0].returncode is not None and not psutil.pid_exists(spawned[0].pid)
    assert not backend.processes


@pytest.mark.asyncio
async def test_real_pty_stream_and_owner_close(tmp_path):
    backend = LocalProcessBackend()
    try:
        process = await backend.start(python("-c", "print('pty-visible',flush=True)"), tmp_path.as_uri(), sandbox=None, tty=True)
        chunks, result = await collect(process)
        assert result.exit_code == 0 and b"pty-visible" in b"".join(chunk.chunk for chunk in chunks)
        assert all(chunk.stream in {"pty", "stderr"} for chunk in chunks)
        assert any(chunk.stream == "pty" for chunk in chunks)
    finally:
        await backend.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process ownership")
@pytest.mark.asyncio
@pytest.mark.parametrize("tty", [False, True])
async def test_windows_child_runs_inside_original_job_without_thread_enumeration(tmp_path, monkeypatch, tty):
    def enumerate_threads(_process):
        raise AssertionError("Starting a captured child must not enumerate system threads")

    monkeypatch.setattr(psutil.Process, "threads", enumerate_threads)
    code = """import ctypes
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.GetCurrentProcess.restype = wintypes.HANDLE
kernel.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
kernel.IsProcessInJob.restype = wintypes.BOOL
contained = wintypes.BOOL()
assert kernel.IsProcessInJob(kernel.GetCurrentProcess(), None, ctypes.byref(contained))
assert contained.value
print('contained-before-first-output', flush=True)
"""
    backend = LocalProcessBackend()
    try:
        process = await backend.start(python("-c", code), tmp_path.as_uri(), sandbox=None, tty=tty)
        chunks, result = await collect(process)
        assert result.exit_code == 0
        assert b"contained-before-first-output" in b"".join(chunk.chunk for chunk in chunks)
        await process.aclose()
        assert process.group_owner.closed and not backend.processes
    finally:
        await backend.close()


@pytest.mark.skipif(os.name != "nt", reason="Windows suspended process ownership")
@pytest.mark.asyncio
async def test_windows_resume_rejection_never_runs_payload_and_reaps_original_child(tmp_path, monkeypatch):
    import ctypes

    load_library = ctypes.WinDLL

    def rejected_resume(_handle):
        return -1073741790  # STATUS_ACCESS_DENIED

    def library(name, *args, **kwargs):
        loaded = load_library(name, *args, **kwargs)
        if name == "ntdll":
            loaded.NtResumeProcess = rejected_resume
        return loaded

    monkeypatch.setattr(ctypes, "WinDLL", library)
    backend = LocalProcessBackend()
    captured = []
    marker = tmp_path / "unresumed-payload"
    try:
        with pytest.raises(OSError) as rejected:
            await backend.start(python("-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"),
                tmp_path.as_uri(), sandbox=None, process_observer=captured.append)
        assert rejected.value.winerror == 5
        [process] = captured
        assert process.group_owner.pid is not None
        assert process.closed and process.group_owner.closed and not backend.processes
        assert not psutil.pid_exists(process.group_owner.pid)
        assert not marker.exists()
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_actual_sandbox_captured_scope_denial_and_no_controller_write(tmp_path):
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    target, denied = workspace / "allowed", outside / "denied"
    sandbox = {"permissions": {"type": "managed", "file_system": {"type": "restricted", "entries": [
        {"path": {"type": "special", "value": {"kind": "root"}}, "access": "read"},
        {"path": {"type": "path", "path": workspace.as_uri()}, "access": "write"}]}, "network": "restricted"},
        "policyContext": {"cwd": workspace.as_uri(), "workspaceRoots": [workspace.as_uri()]},
        "windowsSandboxLevel": "elevated", "useLegacyLandlock": False}
    capability = SandboxRunner(local_sandbox_policy(sandbox)).capability(cwd=workspace)
    if not capability.available or not capability.filesystem_isolated:
        pytest.skip(f"actual local sandbox unavailable: {capability.reason}")
    backend = LocalProcessBackend()
    try:
        process = await backend.start(python("-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('allowed'); pathlib.Path(sys.argv[2]).write_text('denied')", str(target), str(denied)),
            workspace.as_uri(), sandbox=sandbox)
        sandbox["permissions"]["file_system"]["entries"].clear()
        _, result = await collect(process)
        assert target.read_text() == "allowed" and not denied.exists()
        assert result.exit_code != 0 and result.sandbox_denied
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_source_unsupported_options_do_not_start_a_process(tmp_path):
    backend = LocalProcessBackend()
    try:
        for kwargs in ({"shell_snapshot": {}}, {"enforce_managed_network": True}, {"network_proxy": {}}):
            with pytest.raises(ExecutorProtocolError):
                await backend.start(python("-c", "raise SystemExit(99)"), tmp_path.as_uri(), sandbox=None, **kwargs)
        assert not backend.processes
        with pytest.raises(OSError):
            await backend.start([str(tmp_path / "missing-executable")], tmp_path.as_uri(), sandbox=None)
        assert not backend.processes
    finally:
        await backend.close()
