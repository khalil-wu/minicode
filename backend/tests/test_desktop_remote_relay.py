"""Real loopback TCP consumers of the desktop host framing protocol."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
import json
import struct
import traceback

import pytest

from backend.desktop_remote.relay import Relay, STREAM_WINDOW_BYTES, read_frame


async def _send(writer, message):
    body = json.dumps(message, ensure_ascii=False).encode("utf-8")
    writer.write(struct.pack("!I", len(body)) + body)
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
