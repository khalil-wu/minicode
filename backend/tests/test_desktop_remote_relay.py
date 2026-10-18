"""Real loopback TCP consumers of the desktop host framing protocol."""
from __future__ import annotations

import asyncio
import base64
from contextlib import AsyncExitStack, asynccontextmanager
import json
import socket
import struct
import traceback

import pytest

from backend.desktop_remote.relay import Relay, STREAM_WINDOW_BYTES, read_frame
from backend.desktop_remote.host import DesktopHost


async def _send(writer, message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    writer.write(struct.pack("!I", len(body)) + body)
    await writer.drain()


async def _send_together(writer, messages):
    payloads = [json.dumps(message, ensure_ascii=False).encode("utf-8") for message in messages]
    writer.write(b"".join(struct.pack("!I", len(body)) + body for body in payloads))
    await writer.drain()


@asynccontextmanager
async def _relay(backend_port):
    attached = asyncio.get_running_loop().create_future()
    ended = asyncio.Event()
    errors = []

    async def handler(reader, writer):
        relay = Relay(reader, writer, backend_port=backend_port, on_closed=lambda: None)
        attached.set_result(relay)
        try:
            await relay.run({"protocol": 1})
        except Exception as error:
            errors.append(error)
        finally:
            ended.set()

    async with await asyncio.start_server(handler, "127.0.0.1", 0) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.sockets[0].getsockname()[1])
        relay = await attached
        ready = await read_frame(reader)
        assert ready["stream_window_bytes"] == STREAM_WINDOW_BYTES and ready["stream_chunk_bytes"] == 65536
        try:
            yield relay, reader, writer, ended, errors
        finally:
            writer.close()
            await writer.wait_closed()
            async with asyncio.timeout(5):
                await ended.wait()


@asynccontextmanager
async def _echo_backend(*, first_output=b"", half_close_first=False):
    accepted = asyncio.Queue()
    count = 0

    async def echo(reader, writer):
        nonlocal count
        first = count == 0
        count += 1
        accepted.put_nowait(None)
        if first and first_output:
            writer.write(first_output)
            await writer.drain()
        if first and half_close_first:
            writer.write_eof()
            await writer.drain()
        body = await reader.read()
        if body:
            writer.write(body)
            await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(echo, "127.0.0.1", 0) as server:
        yield server.sockets[0].getsockname()[1], accepted


@pytest.mark.asyncio
async def test_real_tcp_half_closes_drain_all_bytes_and_release_every_stream():
    expected = {}
    async def echo(reader, writer):
        body = await reader.read()
        writer.write(body[::-1])
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(echo, "127.0.0.1", 0) as server:
        async with _relay(server.sockets[0].getsockname()[1]) as (relay, reader, writer, _ended, errors):
            for index in range(30):
                stream_id = index * 2 + 1
                body = (f"原字节😀:{index}:" * 100).encode("utf-8")
                expected[stream_id] = body[::-1]
                await _send(writer, {"type": "open", "id": stream_id, "target": "backend"})
                await _send(writer, {"type": "data", "id": stream_id, "data": base64.b64encode(body).decode()})
                await _send(writer, {"type": "end", "id": stream_id})
            received, closed = {}, set()
            async with asyncio.timeout(5):
                while len(closed) != len(expected):
                    frame = await read_frame(reader)
                    if frame["type"] == "data":
                        data = base64.b64decode(frame["data"])
                        received.setdefault(frame["id"], bytearray()).extend(data)
                        await _send(writer, {"type": "credit", "id": frame["id"], "bytes": len(data)})
                    elif frame["type"] == "close":
                        closed.add(frame["id"])
            assert received == expected and not relay.streams and not errors


@pytest.mark.asyncio
async def test_real_slow_stream_credit_does_not_block_ping_or_reverse_bridge():
    payload = b"a" * (STREAM_WINDOW_BYTES * 4)
    release = asyncio.Event()
    async def output(reader, writer):
        writer.write(payload)
        await writer.drain()
        await release.wait()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(output, "127.0.0.1", 0) as source:
        async with _relay(source.sockets[0].getsockname()[1]) as (relay, reader, writer, _ended, errors):
            await _send(writer, {"type": "open", "id": 1, "target": "backend"})
            count = 0
            while count < STREAM_WINDOW_BYTES:
                frame = await read_frame(reader)
                assert frame["type"] == "data"
                count += len(base64.b64decode(frame["data"]))
            await _send(writer, {"type": "ping", "id": "while-credit-exhausted"})
            async with asyncio.timeout(2):
                assert await read_frame(reader) == {"type": "pong", "id": "while-credit-exhausted"}
            assert relay.streams[1].send_credit == 0

            bridge_done = asyncio.Event()
            async def reverse(reader, output_writer):
                await relay.open_bridge(reader, output_writer)
                bridge_done.set()

            async with await asyncio.start_server(reverse, "127.0.0.1", 0) as bridge:
                bridge_reader, bridge_writer = await asyncio.open_connection("127.0.0.1", bridge.sockets[0].getsockname()[1])
                await bridge_done.wait()
                frame = await read_frame(reader)
                assert frame == {"type": "open", "id": 2, "target": "bridge"}
                bridge_writer.write(b"actual reverse request")
                await bridge_writer.drain()
                frame = await read_frame(reader)
                assert base64.b64decode(frame["data"]) == b"actual reverse request" and frame["id"] == 2
                await _send(writer, {"type": "data", "id": 2, "data": base64.b64encode(b"actual reverse reply").decode()})
                async with asyncio.timeout(2):
                    assert await bridge_reader.readexactly(len(b"actual reverse reply")) == b"actual reverse reply"
                await _send(writer, {"type": "close", "id": 2})
                bridge_writer.close()
                await bridge_writer.wait_closed()
            await _send(writer, {"type": "close", "id": 1})
            release.set()
            async with asyncio.timeout(2):
                while relay.streams:
                    await asyncio.sleep(0)
            assert not errors


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,reason", [(b"\x00\x00", "header was truncated"), (struct.pack("!I", 10) + b"{", "body was truncated")])
async def test_actual_truncated_frame_is_a_protocol_error_and_releases_attachment(payload, reason):
    async with _relay(1) as (relay, reader, writer, ended, errors):
        writer.write(payload)
        await writer.drain()
        writer.write_eof()
        async with asyncio.timeout(2):
            await ended.wait()
        assert relay.closed and not relay.streams and len(errors) == 1
        assert reason in "".join(traceback.format_exception(errors[0]))


@pytest.mark.asyncio
async def test_actual_ipv6_preview_keeps_its_original_loopback_address_family():
    peers = []
    async def echo(reader, writer):
        peers.append(writer.get_extra_info("sockname")[0])
        writer.write(await reader.read())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async with await asyncio.start_server(echo, "::1", 0) as server:
        async with _relay(1) as (relay, reader, writer, _ended, errors):
            body = "实际 IPv6 preview 😀".encode()
            await _send(writer, {"type": "open", "id": 1, "target": "preview", "host": "::1", "port": server.sockets[0].getsockname()[1]})
            await _send(writer, {"type": "data", "id": 1, "data": base64.b64encode(body).decode()})
            await _send(writer, {"type": "end", "id": 1})
            received = bytearray()
            async with asyncio.timeout(3):
                while True:
                    frame = await read_frame(reader)
                    if frame["type"] == "data":
                        received.extend(base64.b64decode(frame["data"]))
                    elif frame["type"] == "close":
                        break
            assert received == body and peers == ["::1"] and not relay.streams and not errors


@pytest.mark.asyncio
async def test_actual_preview_ingress_rejects_a_non_loopback_target_without_opening_it():
    async with _relay(1) as (relay, _reader, writer, ended, errors):
        await _send(writer, {"type": "open", "id": 1, "target": "preview", "host": "192.0.2.1", "port": 80})
        async with asyncio.timeout(2):
            await ended.wait()
        assert relay.closed and not relay.streams and len(errors) == 1
        assert "allowed loopback host" in "".join(traceback.format_exception(errors[0]))


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["backend", "bridge"])
async def test_data_end_close_in_one_write_preserves_final_bytes_and_tcp_eof(direction, caplog):
    received = asyncio.get_running_loop().create_future()
    accepted = asyncio.Event()

    async def consume(reader, writer):
        accepted.set()
        received.set_result(await reader.read())
        writer.close()
        await writer.wait_closed()

    async with AsyncExitStack() as stack:
        destination = await stack.enter_async_context(await asyncio.start_server(consume, "127.0.0.1", 0))
        relay, reader, writer, ended, errors = await stack.enter_async_context(_relay(destination.sockets[0].getsockname()[1]))
        if direction == "backend":
            stream_id = 1
            await _send(writer, {"type": "open", "id": stream_id, "target": "backend"})
            await accepted.wait()
            downstream = received
        else:
            async def reverse(bridge_reader, bridge_writer):
                await relay.open_bridge(bridge_reader, bridge_writer)
            bridge = await stack.enter_async_context(await asyncio.start_server(reverse, "127.0.0.1", 0))
            bridge_reader, bridge_writer = await asyncio.open_connection("127.0.0.1", bridge.sockets[0].getsockname()[1])
            frame = await read_frame(reader)
            assert frame == {"type": "open", "id": 2, "target": "bridge"}
            stream_id = 2
            downstream = asyncio.create_task(bridge_reader.read())
        stream = relay.streams[stream_id]
        await stream.opened.wait()
        owned_socket = stream.writer.get_extra_info("socket")
        payload = ("final queued bytes 中文 😀\n" * 128).encode()
        try:
            await _send_together(writer, [{"type": "data", "id": stream_id, "data": base64.b64encode(payload).decode()},
                {"type": "end", "id": stream_id}, {"type": "close", "id": stream_id}])
            async with asyncio.timeout(3):
                assert await downstream == payload
                results = await asyncio.gather(*stream.tasks, return_exceptions=True)
            assert all(result is None or isinstance(result, asyncio.CancelledError) for result in results)
            assert stream.write_ended and stream.closed and stream_id not in relay.streams
            assert owned_socket.fileno() == -1 and stream.writer.transport._sock is None
            await _send(writer, {"type": "ping", "id": "after-graceful-close"})
            while (frame := await read_frame(reader))["type"] == "credit":
                assert frame["id"] == stream_id
            assert frame == {"type": "pong", "id": "after-graceful-close"}
            assert not relay.closed and not ended.is_set() and not errors
            assert not any("Exception in callback" in record.getMessage() for record in caplog.records)
        finally:
            if direction == "bridge":
                bridge_writer.close()
                await bridge_writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", ["drain", "detach", "physical-error"])
async def test_queued_graceful_close_does_not_block_other_streams_and_detach_owns_pending_drain(completion, caplog, monkeypatch):
    release = asyncio.Event()
    accepted = asyncio.Event()
    received = asyncio.get_running_loop().create_future()
    sink_closed = asyncio.Event()

    async def blocked_sink(reader, writer):
        writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        writer.transport.pause_reading()
        accepted.set()
        try:
            await release.wait()
            writer.transport.resume_reading()
            received.set_result(await reader.read())
        finally:
            writer.close()
            await writer.wait_closed()
            sink_closed.set()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    async with await asyncio.start_server(blocked_sink, sock=listener) as destination:
        async with _echo_backend() as (healthy_port, _accepted):
            async with _relay(destination.sockets[0].getsockname()[1]) as (relay, reader, writer, ended, errors):
                await _send(writer, {"type": "open", "id": 1, "target": "backend"})
                await accepted.wait()
                stream = relay.streams[1]
                await stream.opened.wait()
                stream.writer.get_extra_info("socket").setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                payload = b"queued bytes before peer close\n" * 8000
                messages = [{"type": "data", "id": 1, "data": base64.b64encode(payload[offset:offset + 65536]).decode()}
                    for offset in range(0, len(payload), 65536)]
                try:
                    await _send_together(writer, [*messages, {"type": "end", "id": 1}, {"type": "close", "id": 1}])
                    async with asyncio.timeout(3):
                        while 1 in relay.streams and stream.writer.transport.get_write_buffer_size() == 0:
                            await asyncio.sleep(0)
                    assert 1 in relay.streams
                    assert stream.writer.transport.get_write_buffer_size() > 0
                    await _send(writer, {"type": "open", "id": 3, "target": "preview", "host": "127.0.0.1", "port": healthy_port})
                    body = "other stream while drain is blocked 😀".encode()
                    await _send_together(writer, [{"type": "data", "id": 3, "data": base64.b64encode(body).decode()}, {"type": "end", "id": 3}])
                    echoed = bytearray()
                    async with asyncio.timeout(3):
                        while True:
                            frame = await read_frame(reader)
                            assert frame["type"] != "error"
                            if frame["id"] == 1:
                                assert frame["type"] == "credit"
                            elif frame["type"] == "data":
                                echoed.extend(base64.b64decode(frame["data"]))
                            elif frame["type"] == "close":
                                break
                    assert echoed == body and 1 in relay.streams
                    await _send(writer, {"type": "ping", "id": "while-drain-pending"})
                    assert await read_frame(reader) == {"type": "pong", "id": "while-drain-pending"}
                    if completion == "drain":
                        release.set()
                        async with asyncio.timeout(3):
                            assert await received == payload
                            await asyncio.gather(*stream.tasks, return_exceptions=True)
                        assert stream.write_ended and stream.closed and 1 not in relay.streams
                        assert not relay.closed and not ended.is_set()
                    elif completion == "detach":
                        writer.close()
                        await writer.wait_closed()
                        async with asyncio.timeout(3):
                            await ended.wait()
                        assert relay.closed and not relay.streams
                        assert all(task.done() for task in stream.tasks)
                    else:
                        failure = OSError(121, "physical relay writer failed while another stream was closing")
                        send = relay.send

                        async def fail_physical_send(frame):
                            if frame["type"] == "data" and frame["id"] == 5:
                                raise failure
                            await send(frame)

                        monkeypatch.setattr(relay, "send", fail_physical_send)
                        await _send(writer, {"type": "open", "id": 5, "target": "preview", "host": "127.0.0.1", "port": healthy_port})
                        await _send_together(writer, [{"type": "data", "id": 5, "data": base64.b64encode(b"physical write failure trigger").decode()}, {"type": "end", "id": 5}])
                        async with asyncio.timeout(3):
                            await ended.wait()
                        assert relay.closed and not relay.streams
                        assert len(errors) == 1 and errors[0].exceptions == (failure,)
                    assert stream.writer.transport._sock is None
                    if completion != "physical-error":
                        assert not errors
                    assert not any("Exception in callback" in record.getMessage() for record in caplog.records)
                finally:
                    release.set()
                    async with asyncio.timeout(3):
                        await sink_closed.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["read", "write", "eof-close", "write-and-close"])
async def test_socket_failure_keeps_other_tcp_streams_ping_and_host_binding_alive(origin, monkeypatch, tmp_path, caplog):
    output = b"x" * STREAM_WINDOW_BYTES if origin in {"write", "write-and-close"} else b""
    failure = OSError(121, "original socket failure")
    close_failure = OSError(121, "original socket failure") if origin == "write-and-close" else failure
    owner = DesktopHost(tmp_path / "daemon", tmp_path / "state")
    async with _echo_backend(first_output=output, half_close_first=origin == "eof-close") as (port, accepted):
        async with _relay(port) as (relay, reader, writer, ended, errors):
            owner.relay = relay
            relay.on_closed = lambda: owner._detached(relay)
            await _send(writer, {"type": "open", "id": 1, "target": "backend"})
            await accepted.get()
            failed = relay.streams[1]
            await failed.opened.wait()
            if output:
                received = 0
                while received < len(output):
                    frame = await read_frame(reader)
                    assert frame["type"] == "data" and frame["id"] == 1
                    received += len(base64.b64decode(frame["data"]))
                assert failed.send_credit == 0
            if origin == "eof-close":
                assert await read_frame(reader) == {"type": "end", "id": 1}
                assert failed.read_ended
            await _send(writer, {"type": "open", "id": 3, "target": "backend"})
            await accepted.get()
            await relay.streams[3].opened.wait()

            socket_writer = failed.writer
            protocol = socket_writer._protocol
            socket_reader = protocol._stream_reader
            transport = socket_writer.transport
            if origin == "eof-close":
                close = socket_writer.close

                def close_with_socket_notification():
                    transport._force_close(failure)
                    close()

                monkeypatch.setattr(socket_writer, "close", close_with_socket_notification)
                await _send(writer, {"type": "end", "id": 1})
            elif origin == "write-and-close":
                def fail_write(_data):
                    transport._force_close(close_failure)
                    raise failure

                monkeypatch.setattr(socket_writer, "write", fail_write)
                await _send(writer, {"type": "data", "id": 1, "data": base64.b64encode(b"write trigger").decode()})
            else:
                # The Proactor/Selector transport invokes the real
                # StreamReaderProtocol.connection_lost with this exact object.
                transport._force_close(failure)
                await asyncio.sleep(0)
                assert socket_reader.exception() is failure
                assert protocol._get_close_waiter(socket_writer).exception() is failure
                if origin == "write":
                    await _send(writer, {"type": "data", "id": 1, "data": base64.b64encode(b"write trigger").decode()})

            expected_errors = [failure, close_failure] if origin == "write-and-close" else [failure]
            async with asyncio.timeout(3):
                for error in expected_errors:
                    assert await read_frame(reader) == {"type": "error", "id": 1, "error": str(error)}
                results = await asyncio.gather(*failed.tasks, return_exceptions=True)
            assert all(result is None or isinstance(result, asyncio.CancelledError) for result in results)
            assert failed.closed and 1 not in relay.streams
            assert not relay.closed and not ended.is_set() and owner.relay is relay and not errors
            records = [record for record in caplog.records if record.name == "backend.desktop_remote.relay"]
            assert [record.exc_info[1] for record in records] == expected_errors
            assert all("stream 1" in record.getMessage() for record in records)

            body = "healthy stream after socket failure 😀".encode()
            await _send(writer, {"type": "data", "id": 3, "data": base64.b64encode(body).decode()})
            await _send(writer, {"type": "end", "id": 3})
            received = bytearray()
            async with asyncio.timeout(3):
                while True:
                    frame = await read_frame(reader)
                    assert frame["id"] == 3 and frame["type"] != "error"
                    if frame["type"] == "data":
                        received.extend(base64.b64decode(frame["data"]))
                    elif frame["type"] == "close":
                        break
            assert received == body and 3 not in relay.streams
            await _send(writer, {"type": "ping", "id": "attachment-still-owned"})
            assert await read_frame(reader) == {"type": "pong", "id": "attachment-still-owned"}
            await owner.emit_native("actual-window-binding", "workspace:changed", {"scope": "after-stream-error"})
            assert await read_frame(reader) == {"type": "event", "windowInstanceId": "actual-window-binding",
                "channel": "workspace:changed", "payload": {"scope": "after-stream-error"}}
            assert owner.relay is relay and not errors
        assert owner.relay is None


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["close", "non-io-close-notification"])
async def test_actual_cleanup_execution_or_non_io_notification_is_not_hidden(stage, monkeypatch):
    original_error = OSError(121, "reported socket failure")
    cleanup_error = original_error if stage == "close" else RuntimeError("non-I/O teardown failure")
    async with _echo_backend() as (port, accepted):
        async with _relay(port) as (relay, reader, writer, ended, errors):
            await _send(writer, {"type": "open", "id": 1, "target": "backend"})
            await accepted.get()
            stream = relay.streams[1]
            await stream.opened.wait()
            socket_writer = stream.writer
            close, wait_closed = socket_writer.close, socket_writer.wait_closed
            abort = socket_writer.transport.abort

            def fail_cleanup():
                if stage == "close":
                    raise cleanup_error
                socket_writer.transport._force_close(cleanup_error)
                close()

            monkeypatch.setattr(socket_writer.transport, "abort", fail_cleanup)
            try:
                relay._task(stream, relay._stream_error(stream, original_error), "reported-failure")
                assert await read_frame(reader) == {"type": "error", "id": 1, "error": str(original_error)}
                async with asyncio.timeout(3):
                    await ended.wait()
                assert relay.closed and not relay.streams and len(errors) == 1
                assert errors[0].exceptions == (cleanup_error,)
            finally:
                monkeypatch.setattr(socket_writer.transport, "abort", abort)
                close()
                if stage == "close":
                    await wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_type", ["data", "credit"])
async def test_physical_relay_send_failure_is_not_reclassified_as_a_stream_socket_error(frame_type, monkeypatch, caplog):
    failure = OSError(121, "physical relay writer failure")
    async with _echo_backend() as (port, accepted):
        async with _relay(port) as (relay, _reader, writer, ended, errors):
            sent = []
            send = relay.send

            async def fail_physical_send(frame):
                sent.append(frame)
                if frame["type"] == frame_type:
                    raise failure
                await send(frame)

            monkeypatch.setattr(relay, "send", fail_physical_send)
            await _send(writer, {"type": "open", "id": 1, "target": "backend"})
            await accepted.get()
            await relay.streams[1].opened.wait()
            await _send(writer, {"type": "data", "id": 1, "data": base64.b64encode(b"physical send failure trigger").decode()})
            await _send(writer, {"type": "end", "id": 1})
            async with asyncio.timeout(3):
                await ended.wait()
            assert relay.closed and not relay.streams and len(errors) == 1
            assert errors[0].exceptions == (failure,)
            assert not any(frame["type"] == "error" for frame in sent)
            assert not any(record.name == "backend.desktop_remote.relay" for record in caplog.records)
