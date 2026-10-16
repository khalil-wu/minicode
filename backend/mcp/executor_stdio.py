"""MCP SDK byte streams for an executor-managed stdio server."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import anyio
from mcp import types
from mcp.shared.message import SessionMessage

from backend.environments import ExecutorEventsLagged, ExecutorProtocolError

logger = logging.getLogger(__name__)
MAX_STDOUT_LINE_BYTES = 8 * 1024 * 1024
MAX_STDERR_LINE_BYTES = 1024 * 1024


class _Lines:
    def __init__(self, maximum: int):
        self.maximum = maximum
        self.buffer = bytearray()
        self.pending = 0

    def feed(self, chunk: bytes) -> None:
        for index, segment in enumerate(chunk.split(b"\n")):
            if index:
                self.buffer.extend(b"\n")
                self.pending = 0
            if self.pending + len(segment) > self.maximum:
                raise ExecutorProtocolError(f"remote MCP line exceeds {self.maximum} bytes")
            self.buffer.extend(segment)
            self.pending += len(segment)

    def take(self, *, partial: bool = False) -> bytes | None:
        at = self.buffer.find(b"\n")
        if at >= 0:
            line = bytes(self.buffer[:at])
            del self.buffer[:at + 1]
            return line.removesuffix(b"\r")
        if partial and self.buffer:
            line = bytes(self.buffer)
            self.buffer.clear()
            self.pending = 0
            return line.removesuffix(b"\r")
        return None


class _ProcessReader:
    def __init__(self, process, program: str):
        self.process = process
        self.program = program
        self.events = process.subscribe_events()
        self.stdout = _Lines(MAX_STDOUT_LINE_BYTES)
        self.stderr = _Lines(MAX_STDERR_LINE_BYTES)
        self.last_seq = 0
        self.closed = False

    def output(self, seq: int, stream: str, chunk: bytes) -> None:
        if seq <= self.last_seq:
            return
        self.last_seq = seq
        if stream in {"stdout", "pty"}:
            self.stdout.feed(chunk)
        elif stream == "stderr":
            self.stderr.feed(chunk)
            while (line := self.stderr.take()) is not None:
                logger.info("MCP server stderr (%s): %s", self.program, line.decode("utf-8", errors="replace"))

    async def recover(self) -> None:
        response = await self.process.read(after_seq=self.last_seq, max_bytes=None, wait_ms=0)
        for chunk in response.chunks:
            if chunk.seq > self.last_seq + 1:
                raise ExecutorProtocolError(
                    f"remote MCP server output stream lost process events: expected sequence {self.last_seq + 1}, received {chunk.seq}")
            self.output(chunk.seq, chunk.stream, chunk.chunk)
        next_output_seq = response.next_seq - int(response.exited) - int(response.closed)
        if next_output_seq > self.last_seq + 1:
            raise ExecutorProtocolError(
                f"remote MCP server output stream lost process events: expected sequence {self.last_seq + 1}, received {next_output_seq}")
        self.last_seq = max(self.last_seq, response.next_seq - 1)
        if response.failure is not None:
            logger.warning("Remote MCP server process failed (%s): %s", self.program, response.failure)
        self.closed = response.closed or response.failure is not None

    async def messages(self):
        while True:
            line = self.stdout.take(partial=self.closed)
            if line is not None:
                try:
                    message = types.JSONRPCMessage.model_validate_json(line)
                except ValueError as error:
                    logger.exception("Failed to parse remote MCP server message (%s)", self.program)
                    yield error
                    continue
                yield SessionMessage(message)
                continue
            if self.closed:
                tail = self.stderr.take(partial=True)
                if tail is not None:
                    logger.info("MCP server stderr (%s): %s", self.program, tail.decode("utf-8", errors="replace"))
                return
            try:
                event = await self.events.recv()
            except ExecutorEventsLagged:
                await self.recover()
                continue
            kind = event["method"]
            if kind == "process/output":
                self.output(event["seq"], event["stream"], event["chunk"])
            elif kind in {"process/exited", "process/closed"}:
                self.last_seq = max(self.last_seq, event["seq"])
                self.closed = kind == "process/closed"
            elif kind == "process/failed":
                logger.warning("Remote MCP server process failed (%s): %s", self.program, event["error"])
                self.closed = True


@asynccontextmanager
async def executor_stdio_client(lease, argv: list[str], cwd_uri: str, *, env: dict[str, str], env_policy: dict,
        sandbox: dict | None = None, process_options: dict | None = None):
    """Placement is explicit; the existing MCP SDK still owns every protocol call."""
    sdk_lease = lease.clone()
    process = None
    read_writer, read_stream = anyio.create_memory_object_stream[SessionMessage | Exception](0)
    write_stream, write_reader = anyio.create_memory_object_stream[SessionMessage](0)
    try:
        process = await sdk_lease.environment.process.start(argv, cwd_uri, sandbox=sandbox, env=env,
            env_policy=env_policy, tty=False, pipe_stdin=True, **(process_options or {}))
        reader = _ProcessReader(process, argv[0])

        async def receive():
            async with read_writer:
                async for message in reader.messages():
                    await read_writer.send(message)

        async def send():
            async with write_reader:
                async for message in write_reader:
                    payload = message.message.model_dump_json(by_alias=True, exclude_none=True)
                    status = await process.write((payload + "\n").encode("utf-8"))
                    if status != "accepted":
                        raise ExecutorProtocolError(f"remote MCP process stdin write was {status}")

        async with anyio.create_task_group() as group:
            group.start_soon(receive)
            group.start_soon(send)
            try:
                yield read_stream, write_stream
            finally:
                group.cancel_scope.cancel()
    finally:
        with anyio.CancelScope(shield=True):
            try:
                if process is not None:
                    await process.aclose()
            finally:
                await read_stream.aclose()
                await write_stream.aclose()
                await read_writer.aclose()
                await write_reader.aclose()
                await sdk_lease.aclose()
