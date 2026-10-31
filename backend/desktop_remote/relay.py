"""Independent, backpressured TCP streams over one desktop attachment."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import json
import logging
import struct
from collections.abc import Callable


MAX_FRAME_BYTES = 1024 * 1024
STREAM_CHUNK_BYTES = 64 * 1024
STREAM_WINDOW_BYTES = 256 * 1024
logger = logging.getLogger(__name__)


async def read_frame(reader: asyncio.StreamReader) -> dict:
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError as error:
        if error.partial:
            raise ValueError("remote host frame header was truncated") from error
        raise
    size, = struct.unpack("!I", header)
    if size > MAX_FRAME_BYTES:
        raise ValueError("remote host frame exceeds 1 MiB")
    try:
        body = await reader.readexactly(size)
    except asyncio.IncompleteReadError as error:
        raise ValueError("remote host frame body was truncated") from error
    return json.loads(body)


@dataclass
class _Stream:
    id: int
    writer: asyncio.StreamWriter | None = None
    incoming: asyncio.Queue = field(default_factory=asyncio.Queue)
    opened: asyncio.Event = field(default_factory=asyncio.Event)
    credit_changed: asyncio.Event = field(default_factory=asyncio.Event)
    tasks: set[asyncio.Task] = field(default_factory=set)
    send_credit: int = STREAM_WINDOW_BYTES
    receive_credit: int = STREAM_WINDOW_BYTES
    read_ended: bool = False
    write_ended: bool = False
    peer_ended: bool = False
    peer_closed: bool = False
    closed: bool = False


class Relay:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 *, backend_port: int, on_closed: Callable[[], None]):
        self.reader, self.writer = reader, writer
        self.backend_port = backend_port
        self.on_closed = on_closed
        self.next_id = 2
        self.last_client_id = -1
        self.streams: dict[int, _Stream] = {}
        self.write_lock = asyncio.Lock()
        self.closed = False
        self._close_task: asyncio.Task | None = None
        self.group: asyncio.TaskGroup | None = None

    async def send(self, value: dict) -> None:
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("remote host frame exceeds 1 MiB")
        async with self.write_lock:
            self.writer.write(struct.pack("!I", len(data)) + data)
            await self.writer.drain()

    def _task(self, stream: _Stream, operation, label: str) -> None:
        stream.tasks.add(self.group.create_task(operation, name=f"desktop-host-{stream.id}-{label}"))

    async def _pump(self, stream: _Stream, reader: asyncio.StreamReader) -> None:
        while True:
            while not stream.send_credit:
                stream.credit_changed.clear()
                await stream.credit_changed.wait()
            try:
                data = await reader.read(min(STREAM_CHUNK_BYTES, stream.send_credit))
            except OSError as error:
                await self._stream_error(stream, error)
                return
            if not data:
                break
            stream.send_credit -= len(data)
            await self.send({"type": "data", "id": stream.id, "data": base64.b64encode(data).decode("ascii")})
        await self.send({"type": "end", "id": stream.id})
        stream.read_ended = True
        await self._finish_stream(stream)

    async def _write(self, stream: _Stream) -> None:
        await stream.opened.wait()
        writer = stream.writer
        while True:
            data = await stream.incoming.get()
            try:
                if data is None:
                    writer.write_eof()
                else:
                    writer.write(data)
                await writer.drain()
            except OSError as error:
                await self._stream_error(stream, error)
                return
            if data is None:
                break
            stream.receive_credit += len(data)
            await self.send({"type": "credit", "id": stream.id, "bytes": len(data)})
        stream.write_ended = True
        await self._finish_stream(stream)

    def _new_stream(self, stream_id: int) -> _Stream:
        stream = _Stream(stream_id)
        self.streams[stream_id] = stream
        self._task(stream, self._write(stream), "write")
        return stream

    def _attach(self, stream: _Stream, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream.writer = writer
        stream.opened.set()
        self._task(stream, self._pump(stream, reader), "read")

    async def _connect(self, stream: _Stream, host: str, port: int) -> None:
        try:
            reader, writer = await asyncio.open_connection(host, port)
        except OSError as error:
            await self._stream_error(stream, error)
            return
        self._attach(stream, reader, writer)

    async def open_bridge(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.closed:
            writer.close()
            await writer.wait_closed()
            return
        stream_id = self.next_id
        self.next_id += 2
        stream = self._new_stream(stream_id)
        self._attach(stream, reader, writer)
        await self.send({"type": "open", "id": stream_id, "target": "bridge"})

    async def _stream_error(self, stream: _Stream, error: OSError) -> None:
        if stream.closed or self.closed:
            return
        await self._report_stream_error(stream, error)
        await self._close_stream(stream.id, reported_error=error)

    async def _report_stream_error(self, stream: _Stream, error: OSError) -> None:
        logger.warning("Remote desktop TCP stream %s failed", stream.id, exc_info=error)
        await self.send({"type": "error", "id": stream.id, "error": str(error)})

    async def _finish_stream(self, stream: _Stream) -> None:
        if stream.write_ended and (stream.read_ended or stream.peer_closed):
            await self._close_stream(stream.id, notify=not stream.peer_closed, graceful=True)

    async def _close_stream(self, stream_id: int, *, notify: bool = False, graceful: bool = False, reported_error: OSError | None = None) -> None:
        stream = self.streams.get(stream_id)
        if stream is None:
            return
        if stream.closed:
            if not graceful and stream.writer is not None:
                stream.writer.transport.abort()
            return
        stream.closed = True
        try:
            current = asyncio.current_task()
            tasks = tuple(task for task in stream.tasks if task is not current and not task.done())
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if stream.writer is not None:
                if graceful:
                    stream.writer.close()
                else:
                    stream.writer.transport.abort()
                try:
                    await stream.writer.wait_closed()
                except OSError as error:
                    # The close waiter reports connection_lost's socket error;
                    # retain new notifications within the original stream.
                    if error is not reported_error:
                        await self._report_stream_error(stream, error)
                    return
            if notify:
                await self.send({"type": "close", "id": stream_id})
        except asyncio.CancelledError:
            if stream.writer is not None:
                stream.writer.transport.abort()
            raise
        finally:
            self.streams.pop(stream_id)

    async def run(self, ready: dict | None = None) -> None:
        async with asyncio.TaskGroup() as group:
            self.group = group
            try:
                if ready is not None:
                    await self.send({"type": "ready", **ready, "stream_window_bytes": STREAM_WINDOW_BYTES,
                        "stream_chunk_bytes": STREAM_CHUNK_BYTES})
                while True:
                    frame = await read_frame(self.reader)
                    kind = frame["type"]
                    if kind == "ping":
                        await self.send({"type": "pong", "id": frame["id"]})
                        continue
                    stream_id = frame["id"]
                    if kind == "open":
                        if type(stream_id) is not int or stream_id % 2 != 1 or stream_id <= self.last_client_id:
                            raise ValueError("remote desktop stream id is not a new client stream")
                        self.last_client_id = stream_id
                        target = frame["target"]
                        port = self.backend_port if target == "backend" else frame["port"] if target == "preview" else None
                        host = "127.0.0.1" if target == "backend" else frame["host"] if target == "preview" else None
                        if type(port) is not int or not 1 <= port <= 65535:
                            raise ValueError("remote desktop stream target is not an owned loopback port")
                        if host not in {"127.0.0.1", "localhost", "::1"}:
                            raise ValueError("remote desktop preview target must retain an allowed loopback host")
                        stream = self._new_stream(stream_id)
                        self._task(stream, self._connect(stream, host, port), "connect")
                        continue
                    stream = self.streams.get(stream_id)
                    if stream is None:
                        continue
                    if kind == "data":
                        data = base64.b64decode(frame["data"], validate=True)
                        if stream.peer_ended or len(data) > stream.receive_credit:
                            raise ValueError("remote desktop stream exceeded its receive credit or sent data after EOF")
                        stream.receive_credit -= len(data)
                        stream.incoming.put_nowait(data)
                    elif kind == "credit":
                        credit = frame["bytes"]
                        if type(credit) is not int or not 0 < credit <= STREAM_WINDOW_BYTES - stream.send_credit:
                            raise ValueError("remote desktop stream returned invalid send credit")
                        stream.send_credit += credit
                        stream.credit_changed.set()
                    elif kind == "end":
                        stream.peer_ended = True
                        stream.incoming.put_nowait(None)
                    elif kind == "close":
                        stream.peer_closed = True
                        if stream.peer_ended:
                            # The writer owns the queued data and EOF before
                            # this graceful close, even if frames were coalesced.
                            if stream.write_ended:
                                self._task(stream, self._finish_stream(stream), "close")
                        else:
                            await self._close_stream(stream_id)
                    elif kind == "error":
                        await self._close_stream(stream_id)
                    else:
                        raise ValueError(f"unknown remote desktop frame: {kind}")
            except asyncio.IncompleteReadError:
                pass
            finally:
                await self.aclose()

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(), name="remote-relay-close")
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        self.closed = True
        try:
            await asyncio.gather(*(self._close_stream(stream_id) for stream_id in tuple(self.streams)))
        finally:
            self.writer.close()
            await self.writer.wait_closed()
        self.on_closed()
