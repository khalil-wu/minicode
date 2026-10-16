from __future__ import annotations

import asyncio
import base64
import errno
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidURI

from backend.environments import EnvironmentManager, ExecutorClosed, ExecutorDisconnected, ExecutorEventsLagged, ExecutorProtocolError, ExecutorRpcError, load_environment_snapshot
from backend.environments.process import ExecutorProcess, ProcessRead
from backend.environments.client import _retryable

PEER = Path(__file__).with_name("_environments_stdio_peer.py")


@pytest.mark.parametrize("error,expected", [
    (FileNotFoundError("missing launcher"), False), (PermissionError("denied launcher"), False),
    (ConnectionRefusedError(errno.ECONNREFUSED, "refused peer"), True),
    (TimeoutError("connection timed out"), True), (ExecutorProtocolError("malformed initialize"), False),
    (ExecutorRpcError(-32010, "session already attached"), True), (ExecutorRpcError(-32000, "rejected"), False),
])
def test_recovery_uses_source_error_types_instead_of_retrying_any_os_error(error, expected):
    assert _retryable(error) is expected


def configure(tmp_path, entries, *, default="test", include_local=False):
    def value_literal(value):
        if isinstance(value, dict):
            return "{ " + ", ".join(f"{json.dumps(key)} = {value_literal(item)}" for key, item in value.items()) + " }"
        return json.dumps(value)
    text = f"default = {json.dumps(default)}\ninclude_local = {str(include_local).lower()}\n"
    for entry in entries:
        text += "\n[[environments]]\n"
        for key, value in entry.items():
            text += f"{key} = {value_literal(value)}\n"
    (tmp_path / "environments.toml").write_text(text, encoding="utf-8")


@pytest.mark.parametrize("value,expected", [(None, (True, "local")), ("none", (False, None)),
    (" NONE ", (False, None)), ("ws://127.0.0.1:1", (False, "remote"))])
def test_missing_file_only_uses_source_default_provider(tmp_path, value, expected):
    snapshot = load_environment_snapshot(tmp_path, environ={} if value is None else {"CODEX_EXEC_SERVER_URL": value})
    assert (snapshot.include_local, snapshot.default_environment_id) == expected


def test_toml_snapshot_scope_required_skills_and_relative_launcher_cwd(tmp_path):
    (tmp_path / "environments.toml").write_text('''default="none"
include_local=true
[[environments]]
id="build"
program="python"
args=["-u"]
cwd="workspace"
[environments.env]
LANG="C.UTF-8"
[environments.skills]
required=["exact-skill-name"]
''')
    snapshot = load_environment_snapshot(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "ws://ignored"})
    assert snapshot.default_environment_id is None and snapshot.include_local
    spec = snapshot.environments[0]
    assert spec.cwd == tmp_path / "workspace"
    assert spec.required_skills == ("exact-skill-name",) and spec.env == {"LANG": "C.UTF-8"}


@pytest.mark.parametrize("entry", [
    {"id": "local", "program": "python"}, {"id": "none", "program": "python"},
    {"id": "bad space", "program": "python"}, {"id": "test", "program": "python", "url": "ws://localhost:1"},
    {"id": "test", "url": "ws://localhost:1", "args": []},
    {"id": "test", "program": "python", "auth_bearer_token": "private"},
    {"id": "test", "url": "ws://example.com:1", "auth_bearer_token": "private"},
    {"id": "test", "program": "python", "invented": True},
])
def test_source_configuration_rejects_invalid_entries(tmp_path, entry):
    configure(tmp_path, [entry])
    with pytest.raises(ExecutorProtocolError):
        load_environment_snapshot(tmp_path)


def test_invalid_present_file_does_not_fall_back_to_environment(tmp_path):
    (tmp_path / "environments.toml").write_text("broken=[")
    with pytest.raises(ValueError):
        load_environment_snapshot(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "ws://localhost:1"})


@pytest.mark.asyncio
async def test_default_provider_invalid_url_is_a_real_connection_error_without_local_fallback(tmp_path):
    manager = EnvironmentManager(tmp_path, environ={"CODEX_EXEC_SERVER_URL": "https://invalid-executor"})
    environment = manager.get_environment("remote")
    with pytest.raises(InvalidURI):
        await environment.info()
    assert (await manager.status("remote"))["status"] == "disconnected"
    assert manager.get_environment("local") is None
    await manager.close()


@pytest.mark.asyncio
async def test_explicit_local_is_registered_and_never_remote_failure_fallback(tmp_path):
    configure(tmp_path, [{"id": "test", "program": "nonexistent-executor-for-test"}], include_local=True)
    local = SimpleNamespace(info=AsyncMock(return_value={"actual": "host"}), close=AsyncMock(), filesystem=object(), process=object())
    manager = EnvironmentManager(tmp_path, local_backend=local)
    assert (await manager.status("local"))["status"] == "ready"
    assert (await manager.status("test"))["status"] == "pending"
    assert (await manager.status("unknown"))["status"] == "unknown"
    with pytest.raises(OSError):
        await manager.get_environment("test").info()
    assert (await manager.status("test"))["status"] == "disconnected"
    assert manager.default_environment_id == "test" and local.info.await_count == 0
    await manager.close()


@pytest.mark.asyncio
async def test_stdio_actual_fs_process_stdin_output_and_normal_owned_close(tmp_path):
    configure(tmp_path, [{"id": "test", "program": sys.executable, "args": [str(PEER)], "cwd": str(tmp_path)}])
    manager = EnvironmentManager(tmp_path)
    environment = manager.get_environment("test")
    assert (await environment.status())["status"] == "pending" and environment.client.connection is None
    info = await environment.info()
    assert info["providerId"] == "owned-stdio-test-peer"
    assert (await environment.status())["status"] == "ready"
    target = (tmp_path / "读写文件.bin").as_uri()
    await environment.filesystem.write_file(target, b"first\x00\xff", sandbox=None)
    assert await environment.filesystem.read_file(target, sandbox=None) == b"first\x00\xff"
    with pytest.raises(TypeError):
        await environment.filesystem.read_file(target)
    with pytest.raises(ExecutorRpcError, match="sandbox denied"):
        await environment.filesystem.write_file(target, b"forbidden", sandbox={"deny": True})
    assert await environment.filesystem.read_file(target, sandbox=None) == b"first\x00\xff"
    async with await environment.filesystem.open(target, sandbox=None, mode="replace") as handle:
        await handle.write_block(0, b"stream-content")
    stream = await environment.filesystem.open(target, sandbox=None)
    assert b"".join([chunk async for chunk in stream.chunks()]) == b"stream-content"
    async with await environment.process.start([sys.executable, "-u", "-c", "print(input());print('err',file=__import__('sys').stderr)"],
                                               tmp_path.as_uri(), sandbox=None, pipe_stdin=True) as process:
        assert await process.write(b"from-original-session\n") == "accepted"
        events = []
        async with asyncio.timeout(10):
            while True:
                event = await process.next_event()
                events.append(event)
                if event["method"] == "process/closed":
                    break
        assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
        assert b"from-original-session" in b"".join(event["chunk"] for event in events if event["method"] == "process/output")
        assert events[-2]["method"] == "process/exited" and events[-1]["method"] == "process/closed"
        assert process.exit_code == 0
    child = environment.client.connection.transport.child
    receipts = await manager.close()
    assert child.returncode == 0 and receipts[0][-1].exit_code == 0 and not receipts[0][-1].forced
    assert not environment.client.connection.pending


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process", "file"])
async def test_websocket_multiplex_owner_replacement_and_cancelled_resource_cleanup(tmp_path, kind):
    requests = []
    start_seen, release_start, terminated = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_cleanup = asyncio.Event()
    connections = []

    async def peer(socket):
        connections.append(socket)
        tasks = []

        async def answer(message):
            requests.append(message)
            method, params = message["method"], message.get("params")
            if method == "initialize":
                result = {"sessionId": "ws-original-session", "environmentInfo": {"capabilities": {}}}
            elif method == "initialized":
                return
            elif method == "environment/info":
                result = {"providerId": "ws-peer"}
            elif method == "environment/status":
                result = {"status": "ready"}
            elif method in {"process/start", "fs/open"}:
                start_seen.set()
                await release_start.wait()
                result = {"processId": params["processId"], "sandboxType": "none"} if kind == "process" else {"handleId": params["handleId"]}
            elif method in {"process/terminate", "fs/close"}:
                terminated.set()
                await release_cleanup.wait()
                result = {"running": True} if kind == "process" else {}
            elif method == "process/read":
                result = {"chunks": [], "nextSeq": 3, "exited": True, "exitCode": -1, "closed": True, "failure": None}
            else:
                await socket.send(json.dumps({"id": message["id"], "error": {"code": -32004, "message": "missing"}}))
                return
            await socket.send(json.dumps({"id": message["id"], "result": result}))

        async for raw in socket:
            tasks.append(asyncio.create_task(answer(json.loads(raw))))
        await asyncio.gather(*tasks)

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        configure(tmp_path, [{"id": "test", "url": url, "auth_bearer_token": "owned-fixture-token"}])
        manager = EnvironmentManager(tmp_path)
        environment = manager.get_environment("test")
        await environment.info()
        assert (await environment.status())["status"] == "ready"
        start = asyncio.create_task(environment.process.start(["ignored"], "file:///remote/path", sandbox=None)
            if kind == "process" else environment.filesystem.open("file:///remote/path", sandbox=None))
        await asyncio.wait_for(start_seen.wait(), 5)
        assert (await environment.info())["providerId"] == "ws-peer"  # start reply remains pending
        start.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start
        replacement = manager.upsert({"id": "test", "url": url})
        await replacement.info()
        assert environment is not replacement and environment.client is not replacement.client
        release_start.set()
        await asyncio.wait_for(terminated.wait(), 5)
        close = asyncio.create_task(manager.close())
        async with asyncio.timeout(5):
            while not environment.client.closed:
                await asyncio.sleep(0)
        release_cleanup.set()
        await asyncio.wait_for(close, 5)
        assert not environment.client.processes and not environment.client.files
        assert all("jsonrpc" not in message for message in requests)
        assert len([request for request in requests if request["method"] == ("process/start" if kind == "process" else "fs/open")]) == 1
        assert await manager.close() == close.result()


@pytest.mark.asyncio
async def test_ws_resumes_same_session_recovers_ordered_events_and_never_replays_mutations(tmp_path):
    requests, headers = [], []
    process_id = None
    connected = 0

    async def peer(socket):
        nonlocal process_id, connected
        connected += 1
        number = connected
        headers.append(socket.request.headers.get("Authorization"))
        async for raw in socket:
            message = json.loads(raw)
            requests.append(message)
            method, params = message["method"], message.get("params")
            if method == "initialized":
                continue
            if method == "initialize":
                assert params["resumeSessionId"] == (None if number == 1 else "same-session")
                result = {"sessionId": "same-session", "environmentInfo": {"capabilities": {}}}
            elif method == "environment/info":
                result = {"providerId": "recovery-peer"}
            elif method == "process/start":
                process_id = params["processId"]
                result = {"processId": process_id, "sandboxType": "none"}
            elif method == "fs/writeFile":
                assert params["path"] == "file:///remote/unchanged%20path.txt" and params["sandbox"] == {"policy": "original"}
                await socket.close()
                continue
            elif method == "process/read":
                result = {"chunks": [{"seq": 1, "stream": "stdout", "chunk": base64.b64encode(b"a").decode()},
                                     {"seq": 2, "stream": "stderr", "chunk": base64.b64encode(b"b").decode()},
                                     {"seq": 4, "stream": "stdout", "chunk": base64.b64encode(b"tail").decode()}],
                          "nextSeq": 6, "exited": True, "exitCode": 0, "closed": True, "failure": None}
            else:
                result = {"status": "ready"}
            await socket.send(json.dumps({"id": message["id"], "result": result}))
            if method == "process/start":
                for seq, stream, chunk in ((2, "stderr", b"b"), (1, "stdout", b"a")):
                    await socket.send(json.dumps({"method": "process/output", "params": {
                        "processId": process_id, "seq": seq, "stream": stream, "chunk": base64.b64encode(chunk).decode()}}))

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        configure(tmp_path, [{"id": "test", "url": url, "auth_bearer_token": "same-fixture-token"}])
        manager = EnvironmentManager(tmp_path)
        environment = manager.get_environment("test")
        process = await environment.process.start(["peer-process"], "file:///remote", sandbox=None)
        with pytest.raises(ExecutorDisconnected):
            await environment.filesystem.write_file("file:///remote/unchanged%20path.txt", b"once", sandbox={"policy": "original"})
        await asyncio.wait_for(environment.info(), 5)
        events = []
        async with asyncio.timeout(5):
            while True:
                event = await process.next_event()
                events.append(event)
                if event["method"] == "process/closed":
                    break
        assert [event["seq"] for event in events] == [1, 2, 3, 4, 5]
        assert events[2]["method"] == "process/exited" and events[3]["chunk"] == b"tail"
        assert headers == ["Bearer same-fixture-token", "Bearer same-fixture-token"]
        assert sum(request["method"] == "process/start" for request in requests) == 1
        assert sum(request["method"] == "fs/writeFile" for request in requests) == 1
        await manager.close()


@pytest.mark.asyncio
async def test_startup_failure_is_observed_without_status_restarting_stdio(tmp_path):
    configure(tmp_path, [{"id": "test", "program": sys.executable,
                          "args": ["-c", "print('not-json', flush=True)"]}])
    manager = EnvironmentManager(tmp_path)
    environment = manager.get_environment("test")
    with pytest.raises(ExecutorProtocolError):
        await environment.info()
    task = environment.client.connect_task
    assert (await environment.status())["status"] == "disconnected"
    assert environment.client.connect_task is task
    await manager.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["process", "file"])
async def test_host_close_releases_unconfirmed_start_or_open_on_original_connection(tmp_path, kind):
    admitted, cleaned = asyncio.Event(), asyncio.Event()
    starts, cleanups, reads = [], [], []

    async def peer(socket):
        async for raw in socket:
            message = json.loads(raw)
            method, params = message["method"], message.get("params")
            if method == "initialized":
                continue
            if method == "initialize":
                result = {"sessionId": "closing-session", "environmentInfo": {"capabilities": {}}}
            elif method in {"process/start", "fs/open"}:
                starts.append(params)
                admitted.set()
                continue  # the ordinary response is deliberately never sent
            elif method in {"process/terminate", "fs/close"}:
                cleanups.append(params)
                cleaned.set()
                result = {"running": False} if kind == "process" else {}
            elif method == "process/read":
                reads.append(params)
                result = {"chunks": [], "nextSeq": 3, "exited": True, "exitCode": -1, "closed": True, "failure": None}
            else:
                result = {"providerId": "closing-peer"}
            await socket.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        configure(tmp_path, [{"id": "test", "url": url}])
        manager = EnvironmentManager(tmp_path)
        environment = manager.get_environment("test")
        operation = asyncio.create_task(environment.process.start(["peer"], "file:///remote", sandbox=None)
            if kind == "process" else environment.filesystem.open("file:///remote/file", sandbox=None))
        await asyncio.wait_for(admitted.wait(), 5)
        await asyncio.wait_for(manager.close(), 5)
        with pytest.raises(ExecutorClosed):
            await operation
        assert cleaned.is_set()
        key = "processId" if kind == "process" else "handleId"
        assert len(starts) == 1 and cleanups == [{key: starts[0][key]}]
        if kind == "process":
            assert reads and all(read["processId"] == starts[0]["processId"] for read in reads)
        assert not environment.client.connection.pending


@pytest.mark.asyncio
async def test_registry_replacement_keeps_original_thread_borrower_then_closes_last_owner(tmp_path):
    configure(tmp_path, [{"id": "test", "program": sys.executable, "args": [str(PEER)], "cwd": str(tmp_path)}])
    manager = EnvironmentManager(tmp_path)
    lease = manager.borrow("test")
    old = lease.environment
    await old.info()
    old_child = old.client.connection.transport.child
    replacement = manager.upsert({"id": "test", "program": sys.executable, "args": [str(PEER)], "cwd": str(tmp_path)})
    assert replacement is not old and not old.closed
    assert (await old.status())["status"] == "ready"
    await lease.aclose()
    assert old.closed and old_child.returncode == 0
    await replacement.info()
    replaced_again = manager.upsert({"id": "test", "program": sys.executable, "args": [str(PEER)], "cwd": str(tmp_path)})
    await replacement.close_task
    assert replacement.closed and replacement.client.connection.transport.child.returncode == 0
    assert replaced_again.client.connection is None  # stdio remains lazy
    await manager.close()


@pytest.mark.asyncio
async def test_sdk_lease_and_original_process_file_handles_outlive_the_thread_lease(tmp_path):
    configure(tmp_path, [{"id": "test", "program": sys.executable, "args": [str(PEER)], "cwd": str(tmp_path)}])
    manager = EnvironmentManager(tmp_path)
    thread_lease = manager.borrow("test")
    sdk_lease = thread_lease.clone()
    original = sdk_lease.environment
    target = (tmp_path / "retained-file").as_uri()
    await original.filesystem.write_file(target, b"original SDK owner", sandbox=None)
    file = await original.filesystem.open(target, sandbox=None)
    process = await original.process.start([sys.executable, "-u", "-c", "print(input())"],
                                           tmp_path.as_uri(), sandbox=None, pipe_stdin=True)
    old_child = original.client.connection.transport.child
    replacement = manager.upsert({"id": "test", "program": sys.executable, "args": [str(PEER)]})
    await thread_lease.aclose()
    assert not original.closed and sdk_lease.environment is original
    await sdk_lease.aclose()
    assert not original.closed and old_child.returncode is None
    await process.write(b"original-process\n")
    async with asyncio.timeout(5):
        while (await process.next_event())["method"] != "process/closed":
            pass
    await process.aclose()
    assert not original.closed  # the original file handle still borrows it
    assert (await file.read_block(0))[0] == b"original SDK owner"
    await file.aclose()
    await original.close_task
    assert original.closed and old_child.returncode == 0
    assert replacement.client.connection is None
    await manager.close()


@pytest.mark.asyncio
async def test_source_bounded_event_receivers_report_lag_and_reorder_buffer_can_close_its_gap():
    process = ExecutorProcess(SimpleNamespace(), "logical-id", None)
    receiver = process.subscribe_events()
    for seq in range(2, 258):
        process.publish("process/output", {"seq": seq, "stream": "stdout", "chunk": "eA==", "processId": "logical-id"})
    process.publish("process/output", {"seq": 1, "stream": "stdout", "chunk": "eA==", "processId": "logical-id"})
    assert process.last_seq == 257 and len(process.events.history) == 256
    with pytest.raises(ExecutorEventsLagged) as error:
        await receiver.recv()
    assert error.value.lost == 1
    assert (await receiver.recv())["seq"] == 2
    overflow = ExecutorProcess(SimpleNamespace(), "overflow", None)
    with pytest.raises(ExecutorProtocolError, match="reorder buffer"):
        for seq in range(2, 259):
            overflow.publish("process/closed", {"processId": "overflow", "seq": seq})


def test_huge_recovery_gap_and_invalid_bytechunk_are_real_protocol_errors():
    process = ExecutorProcess(SimpleNamespace(), "logical-id", None)
    with pytest.raises(ExecutorProtocolError, match="sequence gap"):
        process._recover_response(ProcessRead((), 2**63, True, 0, True, None))
    with pytest.raises(ExecutorProtocolError, match="base64 chunk"):
        ProcessRead.from_wire({"chunks": [{"seq": 1, "stream": "stdout", "chunk": "not base64"}],
                               "nextSeq": 2, "exited": False, "exitCode": None, "closed": False, "failure": None})


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires explicitly configured owned Source exec-server peer")
async def test_source_executable_interop_with_new_python_client(tmp_path):
    codex_home = tmp_path / "owned-codex-home"
    codex_home.mkdir()
    configure(tmp_path, [{"id": "test", "program": os.environ["MINICODE_SOURCE_EXECUTOR"],
                          "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"],
                          "env": {"CODEX_HOME": str(codex_home)}, "cwd": str(tmp_path)}])
    manager = EnvironmentManager(tmp_path)
    environment = manager.get_environment("test")
    try:
        info = await environment.info()
        assert info["providerId"] and info["executorVersion"]
        assert (await environment.status())["status"] == "ready"
        target = (tmp_path / "source-python-client.bin").as_uri()
        await environment.filesystem.write_file(target, b"Source\x00\xffUTF8", sandbox=None)
        assert await environment.filesystem.read_file(target, sandbox=None) == b"Source\x00\xffUTF8"
        handle = await environment.filesystem.open(target, sandbox=None)
        assert b"".join([chunk async for chunk in handle.chunks()]) == b"Source\x00\xffUTF8"
        process = await environment.process.start([sys.executable, "-u", "-c", "print(input());print('source-stderr',file=__import__('sys').stderr)"],
                                                   tmp_path.as_uri(), sandbox=None, pipe_stdin=True)
        assert await process.write(b"PYTHON_SOURCE_INTEROP\n") == "accepted"
        chunks, cursor = [], None
        async with asyncio.timeout(15):
            while True:
                state = await process.read(after_seq=cursor, wait_ms=1000)
                chunks.extend(state.chunks)
                cursor = state.next_seq - 1
                if state.closed:
                    assert state.exited and state.exit_code == 0 and state.failure is None
                    break
        assert b"PYTHON_SOURCE_INTEROP" in b"".join(chunk.chunk for chunk in chunks)
        assert b"source-stderr" in b"".join(chunk.chunk for chunk in chunks)
        await process.aclose()
    finally:
        receipts = await manager.close()
    assert receipts[0][-1].kind == "stdio" and receipts[0][-1].exit_code == 0 and not receipts[0][-1].forced
