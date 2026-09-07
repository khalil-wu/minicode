from __future__ import annotations

import asyncio
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

import pytest

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.preview import launcher
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.subprocesses import spawn_exec, spawn_shell


def test_natural_cleanup_failure_stays_owned_until_an_explicit_retry(monkeypatch, tmp_path: Path) -> None:
    async def scenario():
        cleanup = AsyncMock(side_effect=[False, True])
        launched = launcher.PreviewLaunchProcess(
            id="cleanup-retry",
            config=launcher.PreviewLaunchConfig("web", "fixture", str(tmp_path), 43130, "http://127.0.0.1:43130"),
            process=SimpleNamespace(pid=1234, returncode=0, stdout=None, stderr=None),
            session_id="session-cleanup", conversation_id="conversation-cleanup", workspace_root=str(tmp_path),
            _sandbox_runner=SimpleNamespace(terminate=cleanup),
        )
        launched._exit_event.set()
        monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
        broadcast = AsyncMock()
        launched._monitor_task = asyncio.create_task(launcher._monitor_process(launched, broadcast))

        await launched._monitor_task

        assert launcher.all_running_preview_processes() == [launched]
        assert launcher._RUNNING[launched.id] is launched
        assert launched.status == "unhealthy"
        assert launched.to_dict()["cleanup_pending"] is True
        assert broadcast.await_args.args[0]["type"] == "preview.server.unhealthy"
        assert broadcast.await_args.args[0]["cleanup_pending"] is True
        assert not launcher.preview_url_is_owned(
            launched.effective_url, session_id=launched.session_id,
            conversation_id=launched.conversation_id, workspace_root=tmp_path,
        )

        stopped = await launcher.stop_preview_launch(
            session_id=launched.session_id, conversation_id=launched.conversation_id, workspace_root=tmp_path,
        )

        assert stopped == [launched]
        assert cleanup.await_count == 2
        assert launched.status == "exited"
        assert not launched.cleanup_pending
        assert launched.cleanup_reason == ""
        assert launcher._RUNNING == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("reaped", [False, True], ids=["unfinished", "released"])
def test_restart_waits_for_the_previous_instance_cleanup(monkeypatch, tmp_path: Path, reaped: bool) -> None:
    async def scenario():
        cleanup_entered = asyncio.Event()
        release_cleanup = asyncio.Event()
        spawned = []

        class Runner:
            def __init__(self, policy):
                self.policy = policy

            async def spawn_shell_interactive(self, command, **kwargs):
                process = SimpleNamespace(pid=1234 + len(spawned), returncode=None, stdout=None, stderr=None)
                spawned.append((process, kwargs["on_exit"]))
                return process

            async def terminate(self, process):
                if process is spawned[0][0]:
                    cleanup_entered.set()
                    await release_cleanup.wait()
                    return reaped
                process.returncode = 0
                return True

        monkeypatch.setattr(launcher, "SandboxRunner", Runner)
        monkeypatch.setattr(launcher, "_RUNNING", {})
        config = launcher.PreviewLaunchConfig("web", "fixture", str(tmp_path))
        options = {"session_id": "session-restart", "conversation_id": "conversation-restart", "workspace_root": tmp_path}
        original = await launcher._start_preview_config(config, **options)
        original.process.returncode = 0
        spawned[0][1]()
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
        restart = asyncio.create_task(launcher._start_preview_config(config, **options))
        try:
            await asyncio.sleep(0)
            assert not restart.done()
            assert len(spawned) == 1
            assert launcher.all_running_preview_processes() == [original]
            release_cleanup.set()
            if reaped:
                replacement = await restart
                assert len(spawned) == 2
                assert replacement is not original
                assert replacement.id == original.id
                assert launcher._RUNNING[replacement.id] is replacement
                await launcher.stop_all_preview_launches()
                assert replacement._monitor_task.done()
            else:
                with pytest.raises(RuntimeError, match="could not be proven stopped"):
                    await restart
                assert len(spawned) == 1
                assert launcher._RUNNING[original.id] is original
                assert original.cleanup_pending
        finally:
            release_cleanup.set()
            await asyncio.gather(restart, return_exceptions=True)
            launcher._RUNNING.clear()

    asyncio.run(scenario())


def test_stream_eof_does_not_end_a_live_preview(monkeypatch, tmp_path: Path) -> None:
    async def scenario():
        cleanup = AsyncMock(return_value=True)
        launched = launcher.PreviewLaunchProcess(
            id="early-eof", config=launcher.PreviewLaunchConfig("web", "fixture", str(tmp_path)),
            process=SimpleNamespace(pid=1234, returncode=None, stdout=None, stderr=None),
            _sandbox_runner=SimpleNamespace(terminate=cleanup),
        )
        monkeypatch.setattr(launcher, "_RUNNING", {launched.id: launched})
        launched._monitor_task = asyncio.create_task(launcher._monitor_process(launched, None))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cleanup.assert_not_awaited()
        assert not launched._monitor_task.done()
        launched.process.returncode = 0
        launched._exit_event.set()
        await asyncio.wait_for(launched._monitor_task, timeout=1)
        cleanup.assert_awaited_once()
        assert launcher._RUNNING == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("shell", [False, True], ids=["exec", "shell"])
def test_spawn_exit_callback_preserves_standard_process_streams(shell: bool) -> None:
    async def scenario():
        exited = asyncio.Event()
        callbacks = []

        def on_exit():
            callbacks.append(True)
            exited.set()

        arguments = [sys.executable, "-c", "print('exit-callback-fixture', flush=True)"]
        options = {"stdout": asyncio.subprocess.PIPE, "stderr": asyncio.subprocess.PIPE, "on_exit": on_exit}
        if shell:
            import shlex
            command = subprocess.list2cmdline(arguments) if os.name == "nt" else shlex.join(arguments)
            process = await spawn_shell(command, **options)
        else:
            process = await spawn_exec(*arguments, **options)
        await asyncio.wait_for(exited.wait(), timeout=5)
        stdout, stderr = await process.communicate()
        assert process.returncode == 0
        assert stdout.strip() == b"exit-callback-fixture"
        assert stderr == b""
        assert callbacks == [True]
        assert process.stdin is None

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "linux", reason="The isolated descendant fixture uses Linux subreaper ownership")
@pytest.mark.parametrize("stop_requested", [False, True], ids=["natural", "stop-during-cleanup"])
def test_root_exit_with_inherited_pipes_keeps_cleanup_ownership(tmp_path: Path, stop_requested: bool) -> None:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path), str(int(stop_requested))],
        capture_output=True, text=True, timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result["http_before"] == "PREVIEW_CHILD_FIXTURE"
    assert result["root_exit_code"] == 0
    assert result["snapshot_during_cleanup"] == ["inherited-preview"]
    assert result["registered_during_cleanup"] is True
    assert result["owned_url_during_cleanup"] is False
    assert result["child_exit_code"] == -15
    assert result["http_after"] == "unreachable"
    assert result["monitor_done"] is True
    assert result["cleanup_pending"] is False
    assert result["registry_after"] == []
    assert result["pending_fixture_tasks"] == []
    assert result["stopped_ids"] == (["inherited-preview"] if stop_requested else [])
    if not stop_requested:
        assert result["events"][-1]["type"] == "preview.launch.stopped"


async def _inherited_http_fixture(workspace: Path, stop_requested: bool) -> dict:
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("Could not enable subreaper for this isolated fixture")
    child_pid_path = workspace / "http-child.pid"
    http_code = (
        "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
        "class Handler(BaseHTTPRequestHandler):\n"
        " def do_GET(self):\n"
        "  self.send_response(200); self.end_headers(); self.wfile.write(b'PREVIEW_CHILD_FIXTURE')\n"
        " def log_message(self, *args): pass\n"
        "server = HTTPServer(('127.0.0.1', 0), Handler)\n"
        "print(f'http://127.0.0.1:{server.server_port}', flush=True)\n"
        "server.serve_forever()\n"
    )
    root_code = (
        "import subprocess, sys; from pathlib import Path; "
        f"child=subprocess.Popen([sys.executable, '-u', '-c', {http_code!r}]); "
        f"Path({str(child_pid_path)!r}).write_text(str(child.pid)); sys.stdin.readline()"
    )
    exited = asyncio.Event()
    process = await spawn_exec(
        sys.executable, "-u", "-c", root_code, on_exit=exited.set,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    runner = SandboxRunner(SandboxPolicy(workspace_root=workspace))
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    original_terminate = runner.terminate

    async def terminate(child):
        cleanup_entered.set()
        await release_cleanup.wait()
        return await original_terminate(child)

    runner.terminate = terminate
    launched = launcher.PreviewLaunchProcess(
        id="inherited-preview", config=launcher.PreviewLaunchConfig("web", "isolated fixture", str(workspace)),
        process=process, _sandbox_runner=runner, _exit_event=exited,
        session_id="session-inherited", conversation_id="conversation-inherited", workspace_root=str(workspace),
    )
    events = []

    async def broadcast(event):
        events.append(event)

    launcher._RUNNING[launched.id] = launched
    launched._monitor_task = asyncio.create_task(launcher._monitor_process(launched, broadcast))
    reaper = None
    stop_task = None
    opener = build_opener(ProxyHandler({}))
    try:
        await asyncio.wait_for(launched.ready_event.wait(), timeout=5)
        with opener.open(launched.effective_url, timeout=2) as response:
            http_before = response.read().decode()
        child_pid = int(child_pid_path.read_text())
        process.stdin.write(b"exit\n")
        await process.stdin.drain()
        await asyncio.wait_for(exited.wait(), timeout=5)
        reaper = asyncio.create_task(asyncio.to_thread(os.waitpid, child_pid, 0))
        await asyncio.wait_for(cleanup_entered.wait(), timeout=5)
        snapshot = launcher.all_running_preview_processes()
        registered = launcher._RUNNING.get(launched.id) is launched
        owned_url = launcher.preview_url_is_owned(
            launched.effective_url, session_id=launched.session_id,
            conversation_id=launched.conversation_id, workspace_root=workspace,
        )
        if stop_requested:
            stop_task = asyncio.create_task(launcher.stop_all_preview_launches())
            await asyncio.sleep(0)
        release_cleanup.set()
        await asyncio.wait_for(launched._monitor_task, timeout=5)
        stopped = await stop_task if stop_task else []
        child_pid, wait_status = await asyncio.wait_for(reaper, timeout=5)
        try:
            with opener.open(launched.effective_url, timeout=0.5) as response:
                http_after = response.read().decode()
        except URLError:
            http_after = "unreachable"
        return {
            "boundary": "Real Linux root and HTTP descendant, production spawn/monitor/registry/runner termination; controlled cleanup gate, no application sandbox launch",
            "http_before": http_before, "http_after": http_after, "root_exit_code": process.returncode,
            "child_exit_code": os.waitstatus_to_exitcode(wait_status),
            "snapshot_during_cleanup": [item.id for item in snapshot],
            "registered_during_cleanup": registered, "owned_url_during_cleanup": owned_url,
            "monitor_done": launched._monitor_task.done(), "cleanup_pending": launched.cleanup_pending,
            "registry_after": list(launcher._RUNNING), "stopped_ids": [item.id for item in stopped],
            "pending_fixture_tasks": [task.get_coro().__qualname__ for task in asyncio.all_tasks() if task is not asyncio.current_task()],
            "events": events,
        }
    finally:
        release_cleanup.set()
        if not launched._monitor_task.done():
            launched._stop_event.set()
        await original_terminate(process)
        await asyncio.gather(launched._monitor_task, *([stop_task] if stop_task else []), return_exceptions=True)
        if reaper is not None:
            await reaper
        elif child_pid_path.exists():
            await asyncio.to_thread(os.waitpid, int(child_pid_path.read_text()), 0)
        launcher._RUNNING.clear()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(_inherited_http_fixture(Path(sys.argv[1]), bool(int(sys.argv[2])))), indent=2))
