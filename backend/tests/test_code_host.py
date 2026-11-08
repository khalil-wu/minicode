from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import psutil
import pytest

from backend.agent.code_host import CodeHostDisconnected, CodeHostError, CodeHostSession, _Connection
from backend.agent.code_host_protocol import MAX_FRAME_BYTES, encode_frame, read_frame


async def start(vm, code="text(42)", storage=None):
    return await vm.step("start", {"code": code, "tools": [], "storage": storage or {}})


@pytest.mark.asyncio
async def test_one_lazy_process_serves_concurrent_isolated_sessions_and_closes_with_last_owner():
    first, second = CodeHostSession(), CodeHostSession()
    assert first.host is second.host and first.host._connection is None
    left, right = first.cell(), second.cell()
    try:
        a, b = await asyncio.gather(start(left, "globalThis.privateValue=7; text(load('owner'));", {"owner": "left"}),
                                  start(right, "text(typeof privateValue); text(load('owner'));", {"owner": "right"}))
        connection = first.host._connection
        assert connection.process.pid != os.getpid()
        assert connection is second.host._connection
        assert [item["text"] for item in a["output"]] == ["left"]
        assert [item["text"] for item in b["output"]] == ["undefined", "right"]
        await left.aclose()
        await first.aclose()
        assert connection.alive
        again = second.cell()
        try:
            assert (await start(again))["output"][0]["text"] == "42"
            assert again._binding[0] is connection
        finally:
            await again.aclose()
        await right.aclose()
        await second.aclose()
        assert connection.process.returncode == 0
        assert not first.host.sessions
    finally:
        await first.aclose()
        await second.aclose()


@pytest.mark.asyncio
async def test_busy_cell_cancellation_does_not_interrupt_another_isolate():
    session = CodeHostSession()
    busy, healthy = session.cell(), session.cell()
    await busy._prepare()
    task = asyncio.create_task(start(busy, "while(true){}"))
    try:
        result = await asyncio.wait_for(start(healthy, "text('other isolate completed')"), 5)
        assert result["output"][0]["text"] == "other isolate completed"
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(busy.aclose(), 3)
        assert session.host._connection.alive
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await healthy.aclose()
        await session.aclose()


@pytest.mark.asyncio
async def test_host_crash_fails_all_bound_cells_and_only_new_cells_can_reconnect():
    session = CodeHostSession()
    left, right = session.cell(), session.cell()
    try:
        await asyncio.gather(start(left, "await new Promise(()=>{});"), start(right, "await new Promise(()=>{});"))
        connection, old_session_id = left._binding
        failures = [asyncio.create_task(vm.wait_failure()) for vm in (left, right)]
        connection.process.kill()
        results = await asyncio.gather(*failures, return_exceptions=True)
        assert all(isinstance(result, CodeHostDisconnected) for result in results)
        with pytest.raises(CodeHostDisconnected):
            await left.step("deliver", [])
        await asyncio.gather(left.aclose(), right.aclose())
        fresh = session.cell()
        try:
            assert (await start(fresh))["output"][0]["text"] == "42"
            assert fresh._binding[0].process.pid != connection.process.pid
            assert fresh._binding[1] != old_session_id
            assert connection.process.returncode is not None
        finally:
            await fresh.aclose()
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_unresponsive_host_timeout_reaps_process_and_fails_other_live_cells(monkeypatch):
    import backend.agent.code_host as module

    session = CodeHostSession()
    left, right = session.cell(), session.cell()
    try:
        await asyncio.gather(start(left, "await new Promise(()=>{});"), start(right, "await new Promise(()=>{});"))
        connection = session.host._connection
        lost = asyncio.create_task(right.wait_failure())
        process = psutil.Process(connection.process.pid)
        process.suspend()
        monkeypatch.setattr(module, "HOST_REQUEST_TIMEOUT_SECONDS", .1)
        with pytest.raises(CodeHostDisconnected, match="timed out"):
            await asyncio.wait_for(left.step("deliver", []), 8)
        with pytest.raises(CodeHostDisconnected):
            await lost
        assert connection.process.returncode is not None
        assert not process.is_running()
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_cancelled_start_and_close_wait_for_the_actual_spawn_owner(monkeypatch):
    import backend.agent.code_host as module

    entered, release = asyncio.Event(), asyncio.Event()
    original = module._Connection.start
    spawned = []
    async def held():
        entered.set()
        await release.wait()
        connection = await original()
        spawned.append(connection)
        return connection
    monkeypatch.setattr(module._Connection, "start", held)
    session = CodeHostSession()
    vm = session.cell()
    running = asyncio.create_task(start(vm))
    await entered.wait()
    running.cancel()
    closing = asyncio.create_task(session.aclose())
    await asyncio.sleep(0)
    assert not running.done() and not closing.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 5)
    await asyncio.wait_for(closing, 5)
    assert len(spawned) == 1 and spawned[0].process.returncode == 0
    assert not session.host.sessions


@pytest.mark.asyncio
async def test_cancelled_close_retains_the_host_until_remote_shutdown_finishes(monkeypatch):
    session = CodeHostSession()
    vm = session.cell()
    await start(vm)
    connection = session.host._connection
    entered, release = asyncio.Event(), asyncio.Event()
    original = connection.request
    async def held(method, **payload):
        if method == "shutdown":
            entered.set()
            await release.wait()
        return await original(method, **payload)
    monkeypatch.setattr(connection, "request", held)
    closing = asyncio.create_task(session.aclose())
    await entered.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and connection.alive
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, 5)
    assert connection.process.returncode == 0
    await session.aclose()


@pytest.mark.asyncio
async def test_host_uses_isolated_python_and_scrubs_provider_credentials(monkeypatch, tmp_path):
    import backend.agent.code_host as module

    monkeypatch.setenv("OPENAI_API_KEY", "private-model-token")
    monkeypatch.setenv("MINICODE_DESKTOP_TOKEN", "private-desktop-token")
    (tmp_path / "sitecustomize.py").write_text("raise RuntimeError('Injected Python startup')", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    original = module.spawn_exec
    captured = []
    async def spawn(*args, **kwargs):
        captured.append((args, kwargs["env"]))
        return await original(*args, **kwargs)
    monkeypatch.setattr(module, "spawn_exec", spawn)
    session = CodeHostSession()
    try:
        result = await start(session.cell(), "text([typeof process,typeof require,typeof fetch,typeof console]);")
        assert json.loads(result["output"][0]["text"]) == ["undefined"] * 4
        args, environment = captured[0]
        assert args[0] == sys.executable and args[1] == "-I"
        inherited_secrets = {"OPENAI_API_KEY", "MINICODE_DESKTOP_TOKEN"} & environment.keys()
        assert not inherited_secrets
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_process_cell_limit_releases_capacity_when_the_actual_cell_closes():
    session = CodeHostSession()
    cells = [session.cell() for _ in range(129)]
    try:
        for vm in cells[:128]:
            await vm._prepare()
        with pytest.raises(CodeHostError, match="too many active cells"):
            await cells[128]._prepare()
        await cells[0].aclose()
        await cells[128]._prepare()
        assert (await start(cells[128]))["output"][0]["text"] == "42"
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_new_session_can_connect_while_the_previous_last_owner_is_finishing_close(monkeypatch):
    old = CodeHostSession()
    await start(old.cell())
    connection = old.host._connection
    closed, release = asyncio.Event(), asyncio.Event()
    original = connection.aclose
    calls = 0
    async def held_close():
        nonlocal calls
        calls += 1
        first = calls == 1
        await original()
        if first:
            closed.set()
            await release.wait()
    monkeypatch.setattr(connection, "aclose", held_close)
    closing = asyncio.create_task(old.aclose())
    await closed.wait()
    new = CodeHostSession()
    try:
        assert (await start(new.cell()))["output"][0]["text"] == "42"
        replacement = new.host._connection
        assert replacement is not connection
        release.set()
        await closing
        assert new.host._connection is replacement and replacement.alive
        await new.aclose()
        assert replacement.process.returncode == 0
    finally:
        release.set()
        await closing
        await new.aclose()


@pytest.mark.asyncio
async def test_length_prefix_handles_fragmented_utf8_and_rejects_oversized_peer_frames():
    reader = asyncio.StreamReader()
    message = {"code": "text('你好 🌍')"}
    encoded = encode_frame(message)
    async def feed():
        for offset in range(0, len(encoded), 3):
            reader.feed_data(encoded[offset:offset + 3])
            await asyncio.sleep(0)
    writer = asyncio.create_task(feed())
    assert await read_frame(reader) == message
    await writer
    reader.feed_data(struct.pack("<I", MAX_FRAME_BYTES + 1))
    with pytest.raises(ValueError, match="exceeds"):
        await read_frame(reader)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["{'ready': 999}", "None"])
async def test_failed_handshake_reaps_the_spawned_child(tmp_path, monkeypatch, reply):
    import backend.agent.code_host as module

    peer = tmp_path / "peer.py"
    peer.write_text("import sys,struct,json,time\nheader=sys.stdin.buffer.read(4)\n"
        "sys.stdin.buffer.read(struct.unpack('<I',header)[0])\n"
        f"reply={reply}\n"
        "if reply is not None:\n data=json.dumps(reply).encode();sys.stdout.buffer.write(struct.pack('<I',len(data))+data);sys.stdout.buffer.flush();time.sleep(60)\n",
        encoding="utf-8")
    original = module.spawn_exec
    children = []
    async def spawn(*args, **kwargs):
        child = await original(sys.executable, "-I", str(peer), **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(module, "spawn_exec", spawn)
    with pytest.raises((CodeHostError, asyncio.IncompleteReadError)):
        await _Connection.start()
    assert len(children) == 1 and children[0].returncode is not None


def test_parent_exit_closes_its_busy_host_without_an_orphan(tmp_path):
    parent = tmp_path / "parent.py"
    parent.write_text("import asyncio,sys\n"
        f"sys.path.insert(0,{str(Path(__file__).resolve().parents[2])!r})\n"
        "from backend.agent.code_host import CodeHostSession\n"
        "async def main():\n s=CodeHostSession();vm=s.cell();await vm._prepare()\n"
        " print(s.host._connection.process.pid,flush=True)\n"
        " await vm.step('start',{'code':'while(true){}','tools':[],'storage':{}})\n"
        "asyncio.run(main())\n", encoding="utf-8")
    process = subprocess.Popen([sys.executable, "-I", str(parent)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    child = None
    try:
        child = psutil.Process(int(process.stdout.readline()))
        process.kill()
        process.communicate(timeout=10)
        child.wait(timeout=10)
        assert not child.is_running()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if child is not None and child.is_running():
            child.kill()
            child.wait(timeout=5)


def test_production_code_cells_do_not_load_v8_in_the_controller():
    script = """
import asyncio,sys
from backend.agent.code_execution_store import CodeExecutionStore
async def main():
    store=CodeExecutionStore()
    try:
        vm=store.create_vm()
        result=await vm.step('start',{'code':'text(123)','tools':[],'storage':{}})
        assert result['output'][0]['text']=='123'
        assert not any(name=='py_mini_racer' or name.startswith('py_mini_racer.') for name in sys.modules)
    finally:
        await store.aclose()
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
