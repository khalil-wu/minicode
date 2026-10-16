from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import os
import sys

import pytest
from websockets.asyncio.server import serve

from backend.environments import (
    EnvironmentManager, ExecutorClosed, ExecutorProtocolError, HttpHeader, HttpRequestParams,
)
from backend.environments.http import handle_http_body_delta


def _config(tmp_path, spec):
    def literal(value):
        if isinstance(value, dict):
            return "{ " + ", ".join(f"{json.dumps(key)} = {literal(item)}" for key, item in value.items()) + " }"
        return json.dumps(value)
    (tmp_path / "environments.toml").write_text('default="remote"\ninclude_local=false\n[[environments]]\n'
        + "\n".join(f"{key}={literal(value)}" for key, value in {"id": "remote", **spec}.items()), encoding="utf-8")


async def _reply(socket, message, *, status=200, body=b"", headers=()):
    await socket.send(json.dumps({"id": message["id"], "result": {
        "status": status, "headers": list(headers), "bodyBase64": base64.b64encode(body).decode("ascii"),
    }}))


async def _delta(socket, request_id, seq, chunk=b"", *, done=False, error=None):
    await socket.send(json.dumps({"method": "http/request/bodyDelta", "params": {
        "requestId": request_id, "seq": seq, "deltaBase64": base64.b64encode(chunk).decode("ascii"),
        "done": done, "error": error,
    }}))


@asynccontextmanager
async def _environment(tmp_path, http_handler, *, env_capability=True):
    requests, tasks = [], []

    async def peer(socket):
        async for raw in socket:
            message = json.loads(raw)
            requests.append(message)
            method = message["method"]
            if method == "initialized":
                continue
            if method == "initialize":
                result = {"sessionId": "http-session", "environmentInfo": {"providerId": "owned-http-peer",
                          "capabilities": {"httpHeaderEnvVars": env_capability}}}
            elif method == "http/request":
                tasks.append(asyncio.create_task(http_handler(socket, message)))
                continue
            elif method == "environment/info":
                result = {"providerId": "owned-http-peer", "capabilities": {"httpHeaderEnvVars": env_capability}}
            else:
                result = {"status": "ready"}
            await socket.send(json.dumps({"id": message["id"], "result": result}))

    async with serve(peer, "127.0.0.1", 0) as server:
        _config(tmp_path, {"url": f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"})
        manager = EnvironmentManager(tmp_path)
        try:
            yield manager, manager.get_environment("remote"), requests
        finally:
            await manager.close()
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            failures = [result for result in results if isinstance(result, Exception)]
            if failures:
                raise ExceptionGroup("owned HTTP peer failed", failures)


@pytest.mark.asyncio
async def test_buffered_executor_http_preserves_bytes_duplicate_headers_and_source_fields(tmp_path):
    async def handler(socket, message):
        params = message["params"]
        assert params == {"method": "POST", "url": "https://remote.invalid/mcp", "headers": [
            {"name": "x-multi", "value": "one"}, {"name": "x-multi", "value": "two"}],
            "bodyBase64": base64.b64encode(b"\x00\xffrequest").decode(), "redirectPolicy": "stop",
            "requestId": "buffered-owner", "streamResponse": False}
        await _reply(socket, message, status=201, body=b"\x00\xffresponse", headers=[
            {"name": "x-answer", "value": "a"}, {"name": "x-answer", "value": "b"}])

    async with _environment(tmp_path, handler) as (_manager, environment, _requests):
        response = await environment.http.http_request(HttpRequestParams("POST", "https://remote.invalid/mcp",
            headers=(HttpHeader("x-multi", "one"), HttpHeader("x-multi", "two")), body=b"\x00\xffrequest",
            redirect_policy="stop", request_id="buffered-owner"))
        assert response.status == 201 and response.body == b"\x00\xffresponse"
        assert response.headers == (HttpHeader("x-answer", "a"), HttpHeader("x-answer", "b"))


@pytest.mark.asyncio
async def test_body_before_headers_is_routed_and_stream_ids_never_reuse_caller_identity(tmp_path):
    ids = []

    async def handler(socket, message):
        request_id = message["params"]["requestId"]
        ids.append(request_id)
        await _delta(socket, request_id, 1, b"before-headers")
        await _reply(socket, message)
        if len(ids) == 2:
            await _delta(socket, ids[0], 9, b"late-abandoned-data", done=True)
        await _delta(socket, request_id, 2, b"tail", done=True)

    async with _environment(tmp_path, handler) as (manager, environment, _requests):
        for _index in range(2):
            response, stream = await environment.http.http_request_stream(HttpRequestParams("GET", "https://remote.invalid/sse", request_id="caller-reused"))
            assert response.body == b""
            assert await stream.recv() == b"before-headers"
            assert await stream.recv() == b"tail"
            if _index == 1:
                # The final nonempty done chunk already established EOF.
                await manager.close()
            assert await stream.recv() is None
        assert ids == ["http-1", "http-2"]
        assert not environment.client.http_streams and environment.client.http_queued_bytes == 0
        await manager.close()
        assert await stream.recv() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["headers", "body"])
async def test_cancel_removes_only_original_route_without_cancel_rpc_or_post_replay(tmp_path, phase):
    admitted, release = asyncio.Event(), asyncio.Event()

    async def handler(socket, message):
        admitted.set()
        if phase == "body":
            await _reply(socket, message)
        await release.wait()
        if phase == "headers":
            await _reply(socket, message)
        await _delta(socket, message["params"]["requestId"], 1, b"late", done=True)

    async with _environment(tmp_path, handler) as (_manager, environment, requests):
        pending = asyncio.create_task(environment.http.http_request_stream(HttpRequestParams("POST", "https://remote.invalid/mcp")))
        await asyncio.wait_for(admitted.wait(), 5)
        if phase == "headers":
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            _response, stream = await pending
            read = asyncio.create_task(stream.recv())
            await asyncio.sleep(0)
            read.cancel()
            with pytest.raises(asyncio.CancelledError):
                await read
        assert not environment.client.http_streams and environment.client.http_queued_bytes == 0
        assert environment.borrowers == 0
        release.set()
        # Keep the owned peer alive until its late reply is actually sent.
        await asyncio.sleep(0)
        assert sum(request["method"] == "http/request" for request in requests) == 1
        assert not any("cancel" in request["method"] for request in requests)


@pytest.mark.asyncio
async def test_disconnect_fails_original_http_body_and_recovers_connection_without_reposting(tmp_path):
    release = asyncio.Event()

    async def handler(socket, message):
        await _reply(socket, message)
        await _delta(socket, message["params"]["requestId"], 1, b"prefix")
        await release.wait()
        await socket.close()

    async with _environment(tmp_path, handler) as (_manager, environment, requests):
        _response, stream = await environment.http.http_request_stream(HttpRequestParams("POST", "https://remote.invalid/mcp"))
        assert await stream.recv() == b"prefix"
        release.set()
        with pytest.raises(ExecutorProtocolError):
            await asyncio.wait_for(stream.recv(), 5)
        await asyncio.wait_for(environment.info(), 5)
        assert sum(request["method"] == "http/request" for request in requests) == 1
        assert not environment.client.http_streams and environment.client.http_queued_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["sequence", "remote_error"])
async def test_actual_stream_sequence_and_terminal_errors_are_exposed(tmp_path, failure):
    async def handler(socket, message):
        await _reply(socket, message)
        await _delta(socket, message["params"]["requestId"], 2 if failure == "sequence" else 1,
                     done=True, error="executor read failed" if failure == "remote_error" else None)

    async with _environment(tmp_path, handler) as (_manager, environment, _requests):
        _response, stream = await environment.http.http_request_stream(HttpRequestParams("GET", "https://remote.invalid/sse"))
        with pytest.raises(ExecutorProtocolError):
            await stream.recv()
        assert stream.closed and environment.client.http_queued_bytes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["frames", "bytes"])
async def test_source_frame_and_shared_byte_budget_fail_stream_without_blocking_rpc_reader(tmp_path, limit):
    async def handler(socket, message):
        await _reply(socket, message)

    async with _environment(tmp_path, handler) as (_manager, environment, _requests):
        _response, first = await environment.http.http_request_stream(HttpRequestParams("GET", "https://remote.invalid/a"))
        _response, second = await environment.http.http_request_stream(HttpRequestParams("GET", "https://remote.invalid/b"))
        rpc = environment.client.connection
        chunk = b"" if limit == "frames" else b"x" * (1024 * 1024)
        encoded = base64.b64encode(chunk).decode()
        count = 257 if limit == "frames" else 17
        for index in range(count):
            target = first if limit == "frames" or index < 8 else second
            seq = index + 1 if target is first else index - 7
            handle_http_body_delta(environment.client, rpc, {"requestId": target.request_id, "seq": seq, "deltaBase64": encoded})
        failed = first if limit == "frames" else second
        with pytest.raises(ExecutorProtocolError):
            while await failed.recv() is not None:
                pass
        assert (await environment.status())["status"] == "ready"
        await first.aclose()
        await second.aclose()
        assert environment.client.http_queued_bytes == 0


@pytest.mark.asyncio
async def test_header_env_var_gate_never_resolves_host_environment_or_sends_unsupported_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_OWNED_EXECUTOR_HTTP_TOKEN", "host-must-not-be-read")
    async def handler(socket, message):
        raise AssertionError("unsupported env header request was sent")
    async with _environment(tmp_path, handler, env_capability=False) as (_manager, environment, requests):
        with pytest.raises(ExecutorProtocolError, match="httpHeaderEnvVars"):
            await environment.http.http_request(HttpRequestParams("GET", "https://remote.invalid", headers=(
                HttpHeader("authorization", "Bearer ", "MINICODE_OWNED_EXECUTOR_HTTP_TOKEN"),)))
        assert not any(request["method"] == "http/request" for request in requests)


@pytest.mark.asyncio
async def test_http_stream_retains_old_environment_until_sdk_close_and_host_close_wakes_body(tmp_path):
    async def handler(socket, message):
        await _reply(socket, message)
    async with _environment(tmp_path, handler) as (manager, original, _requests):
        lease = manager.borrow("remote")
        _response, stream = await original.http.http_request_stream(HttpRequestParams("GET", "https://remote.invalid/sse"))
        manager.upsert({"id": "remote", "program": sys.executable, "args": ["-c", "pass"]})
        await lease.aclose()
        assert not original.closed
        await stream.aclose()
        await original.close_task
        assert original.closed
    async with _environment(tmp_path, handler) as (manager, environment, _requests):
        _response, stream = await environment.http.http_request_stream(HttpRequestParams("GET", "https://remote.invalid/sse"))
        waiting = asyncio.create_task(stream.recv())
        await asyncio.wait_for(manager.close(), 5)
        with pytest.raises(ExecutorClosed):
            await waiting
        assert environment.borrowers == 0 and not environment.client.http_streams


@pytest.mark.asyncio
@pytest.mark.skipif(not os.environ.get("MINICODE_SOURCE_EXECUTOR"), reason="requires owned Source exec-server peer")
async def test_source_native_executor_http_buffered_stream_and_remote_header_env(tmp_path, monkeypatch):
    records = []

    async def endpoint(reader, writer):
        header = await reader.readuntil(b"\r\n\r\n")
        lines = header.decode("ascii").split("\r\n")
        length = next((int(line.split(":", 1)[1]) for line in lines if line.lower().startswith("content-length:")), 0)
        body = await reader.readexactly(length)
        records.append((lines, body))
        if "/stream " in lines[0]:
            writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
                         b"4\r\none\x00\r\n4\r\ntwo\xff\r\n0\r\n\r\n")
        else:
            writer.write(b"HTTP/1.1 201 Created\r\nX-Repeated: a\r\nX-Repeated: b\r\nContent-Length: 3\r\nConnection: close\r\n\r\n\x00\xffR")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    monkeypatch.setenv("MINICODE_OWNED_EXECUTOR_HTTP_TOKEN", "host-must-not-be-used")
    child_home = tmp_path / "owned-http-codex-home"
    child_home.mkdir()
    _config(tmp_path, {"program": os.environ["MINICODE_SOURCE_EXECUTOR"],
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"], "cwd": str(tmp_path),
        "env": {"CODEX_HOME": str(child_home), "MINICODE_OWNED_EXECUTOR_HTTP_TOKEN": "executor-owned-value"}})
    async with await asyncio.start_server(endpoint, "127.0.0.1", 0) as server:
        url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        manager = EnvironmentManager(tmp_path)
        environment = manager.get_environment("remote")
        try:
            response = await environment.http.http_request(HttpRequestParams("POST", url + "/buffer", body=b"\x00\xffP",
                headers=(HttpHeader("authorization", "Bearer ", "MINICODE_OWNED_EXECUTOR_HTTP_TOKEN"),)))
            assert response.status == 201 and response.body == b"\x00\xffR"
            assert [header.value for header in response.headers if header.name.lower() == "x-repeated"] == ["a", "b"]
            response, stream = await environment.http.http_request_stream(HttpRequestParams("GET", url + "/stream"))
            assert response.status == 200 and b"".join([chunk async for chunk in stream]) == b"one\x00two\xff"
        finally:
            receipts = await manager.close()
    assert len(records) == 2 and records[0][1] == b"\x00\xffP"
    assert "authorization: Bearer executor-owned-value" in records[0][0]
    assert not any("host-must-not-be-used" in line for lines, _body in records for line in lines)
    assert receipts[0][-1].exit_code == 0 and not receipts[0][-1].forced
