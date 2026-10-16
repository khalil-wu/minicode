"""Real same-process PTY sizes through the original native control owner."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import threading

import httpx
import psutil
import pytest

from backend.subprocesses import terminate_process_tree
from backend.terminal.pty_process import spawn_pty
from backend.tests.test_desktop_remote_host import _daemon, _native


def _size_probe(workspace: Path) -> tuple[Path, Path]:
    result = workspace / "actual-console-size.json"
    script = workspace / "console_size_probe.py"
    script.write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        f"target=Path({str(result)!r})\n"
        "counter=0\n"
        "def report():\n"
        " global counter\n counter+=1\n size=os.get_terminal_size()\n"
        " payload={'pid':os.getpid(),'cols':size.columns,'rows':size.lines,'query':counter,"
        "'stdin_is_tty':sys.stdin.isatty(),'stdout_is_tty':sys.stdout.isatty()}\n"
        " pending=target.with_suffix('.next')\n pending.write_text(json.dumps(payload),encoding='utf-8')\n pending.replace(target)\n"
        " print('ACTUAL_CONSOLE_SIZE:'+json.dumps(payload),flush=True)\n"
        "report()\n"
        "for line in sys.stdin:\n"
        " if line.strip()=='exit': break\n"
        " if line.strip()=='size': report()\n",
        encoding="utf-8",
    )
    return script, result


async def _size(result: Path, query: int) -> dict:
    async with asyncio.timeout(15):
        while True:
            if result.exists():
                actual = json.loads(result.read_text(encoding="utf-8"))
                if actual["query"] == query:
                    return actual
            await asyncio.sleep(.02)


@pytest.mark.asyncio
async def test_same_native_pty_process_resizes_and_preserves_native_error_reply(tmp_path, record_testsuite_property):
    script, result = _size_probe(tmp_path)
    process = await spawn_pty([sys.executable, "-u", str(script)], cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"}, resizable=True)
    try:
        first = await _size(result, 1)
        assert first["stdin_is_tty"] and first["stdout_is_tty"]
        original_pid = process.pid
        assert await process.resize(101, 31) is True
        process.stdin.write(b"size\n")
        await process.stdin.drain()
        second = await _size(result, 2)
        assert second == {**first, "cols": 101, "rows": 31, "query": 2}
        if os.name == "nt":
            with pytest.raises(RuntimeError, match="failed to resize"):
                # Source converts u16 columns to the native signed COORD.
                # -1 is rejected by ResizePseudoConsole on this exact handle.
                await process.resize(65535, 31)
        assert await process.resize(132, 44) is True
        process.stdin.write(b"size\n")
        await process.stdin.drain()
        third = await _size(result, 3)
        assert third == {**first, "cols": 132, "rows": 44, "query": 3}
        assert process.pid == original_pid and process.returncode is None
        process.stdin.write(b"exit\n")
        await process.stdin.drain()
        await asyncio.wait_for(process.wait(), 15)
        assert process.returncode == 0
        assert not psutil.pid_exists(first["pid"])
        record_testsuite_property("direct_native_pty_sizes", json.dumps({"wrapper_pid": original_pid,
            "queries": [first, second, third], "exit_code": process.returncode}))
    finally:
        assert await terminate_process_tree(process)


@pytest.mark.skipif(os.name != "nt", reason="Windows original ConPTY control pipe")
@pytest.mark.asyncio
async def test_repeated_resize_cancellation_drains_original_reply_before_next_request(tmp_path):
    script, result = _size_probe(tmp_path)
    process = await spawn_pty([sys.executable, "-u", str(script)], cwd=tmp_path,
        env={**os.environ, "PYTHONUTF8": "1"}, resizable=True)
    entered, release = threading.Event(), threading.Event()
    reader = process._control._response_read

    class GatedReader:
        calls = 0

        def readline(self):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                assert release.wait(15)
            return reader.readline()

        def close(self):
            reader.close()

    gate = GatedReader()
    process._control._response_read = gate
    first_resize = second_resize = None
    try:
        original = await _size(result, 1)
        first_resize = asyncio.create_task(process.resize(101, 31))
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 15), 16)
        first_resize.cancel()
        await asyncio.sleep(0)
        first_resize.cancel()
        await asyncio.sleep(0)
        second_resize = asyncio.create_task(process.resize(132, 44))
        done, _ = await asyncio.wait({first_resize, second_resize}, timeout=.05)
        assert not done and gate.calls == 1
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first_resize, 15)
        assert await asyncio.wait_for(second_resize, 15) is True
        assert gate.calls == 2
        process.stdin.write(b"size\n")
        await process.stdin.drain()
        assert await _size(result, 2) == {**original, "cols": 132, "rows": 44, "query": 2}
        process.stdin.write(b"exit\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.wait(), 15) == 0
    finally:
        release.set()
        await asyncio.gather(*(task for task in (first_resize, second_resize) if task is not None), return_exceptions=True)
        assert await terminate_process_tree(process)


@pytest.mark.asyncio
async def test_remote_resize_uses_original_pty_after_detach_and_releases_only_its_window(tmp_path, record_testsuite_property):
    workspace = tmp_path / "resize-workspace"
    workspace.mkdir()
    script, result = _size_probe(workspace)
    async with _daemon(tmp_path) as (attachment, attach, windows, _state):
        owner = {"window_instance_id": "resize-owner", "window_owner_id": "primary", "window_binding_token": secrets.token_hex(16)}
        foreign = {"window_instance_id": "resize-foreign", "window_owner_id": "secondary", "window_binding_token": secrets.token_hex(16)}
        windows[owner["window_instance_id"]], windows[foreign["window_instance_id"]] = owner, foreign
        ready = attachment.ready
        conversation = "native-resize-conversation"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ready['backend_port']}",
            headers={"x-minicode-token": ready["runtime_token"]}, timeout=20) as client:
            assert (await _native(client, owner, "workspace:trust", [str(workspace)])).status_code == 200
            spawned = await _native(client, owner, "pty:spawn", [str(workspace), conversation])
            assert spawned.status_code == 200, spawned.text
            original = spawned.json()
            session = original["session_id"]
            command = (f"& '{sys.executable.replace(chr(39), chr(39)*2)}' -u '{str(script).replace(chr(39), chr(39)*2)}'\r"
                if os.name == "nt" else f"'{sys.executable}' -u '{script}'\n")
            assert (await _native(client, owner, "pty:write", [session, command, conversation])).status_code == 200
            first = await _size(result, 1)
            assert first["stdin_is_tty"] and first["stdout_is_tty"]
            resized = await _native(client, owner, "pty:resize", [session, 101, 31, conversation])
            assert resized.status_code == 200 and resized.json() is True, resized.text
            assert (await _native(client, owner, "pty:write", [session, "size\n", conversation])).status_code == 200
            second = await _size(result, 2)
            assert second == {**first, "cols": 101, "rows": 31, "query": 2}
            await attachment.detach()
            resumed = await attach()
            assert resumed.ready["generation"] == ready["generation"] and resumed.ready["backend_port"] == ready["backend_port"]
            snapshot = await _native(client, owner, "pty:snapshot", [session, 100000, conversation])
            assert snapshot.status_code == 200 and snapshot.json()["pid"] == original["pid"] and snapshot.json()["is_alive"], snapshot.text
            assert (await _native(client, foreign, "pty:resize", [session, 80, 24, conversation])).status_code == 403
            assert (await _native(client, owner, "pty:resize", [session, 80, 24, "wrong-conversation"])).status_code == 403
            for cols, rows in [(0, 31), (101, 0), (3.5, 31), ("101", 31), (101, 70000)]:
                invalid = await _native(client, owner, "pty:resize", [session, cols, rows, conversation])
                assert invalid.status_code == 422, invalid.text
            resized = await _native(client, owner, "pty:resize", [session, 132, 44, conversation])
            assert resized.status_code == 200 and resized.json() is True, resized.text
            assert (await _native(client, owner, "pty:write", [session, "size\n", conversation])).status_code == 200
            third = await _size(result, 3)
            assert third == {**first, "cols": 132, "rows": 44, "query": 3}
            other = await _native(client, foreign, "pty:spawn", [str(workspace), "foreign-resize-conversation"])
            assert other.status_code == 200, other.text
            released = await _native(client, owner, "window:release", [], headers={"x-minicode-desktop-lifecycle-token": ready["bridge_token"]})
            assert released.status_code == 200, released.text
            assert not psutil.pid_exists(first["pid"])
            assert (await _native(client, owner, "pty:list", [conversation])).json() == []
            assert len((await _native(client, foreign, "pty:list", ["foreign-resize-conversation"])).json()) == 1
            record_testsuite_property("remote_native_pty_sizes", json.dumps({"wrapper_pid": original["pid"], "session_id": session,
                "generation_preserved": True, "queries": [first, second, third], "owner_released": True, "foreign_owner_retained": True}))
