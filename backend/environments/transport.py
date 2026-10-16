"""One owned stdio child or WebSocket, with a single RPC reader above it."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import signal
import subprocess
from dataclasses import dataclass

from websockets.asyncio.client import connect
from websockets.uri import parse_uri

from backend.runtime_env import shell_subprocess_env

from .config import EnvironmentSpec

MAX_MESSAGE_BYTES = 64 * 1024 * 1024  # Source connection.rs stdio/WS ceiling.
STDIO_TERMINATION_GRACE_SECONDS = 2
NON_INHERITABLE_ENV_VARS = {
    "CODEX_EXEC_SERVER_NOISE_AUTH_TOKEN", "NODE_REPL_AUTH_TOKEN", "CODEX_GUARDIAN_DECISIONS_API_KEY",
    "OPENAI_FEDERATION_RULE_ID", "OPENAI_IDENTITY_TOKEN_FILE", "OPENAI_WORKLOAD_IDENTITY_CONTEXT",
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TransportCloseReceipt:
    kind: str
    exit_code: int | None = None
    forced: bool = False
    stderr_tail: bytes = b""


class StdioTransport:
    def __init__(self, child: asyncio.subprocess.Process):
        self.child = child
        self.stderr_tail = b""
        self.stderr_task = asyncio.create_task(self._drain_stderr(), name="executor-stderr")
        self.close_task: asyncio.Task[TransportCloseReceipt] | None = None

    async def _drain_stderr(self) -> None:
        while chunk := await self.child.stderr.read(8192):
            self.stderr_tail = (self.stderr_tail + chunk)[-8192:]
            logger.debug("exec-server stderr: %s", chunk.decode("utf-8", errors="replace"))

    async def recv(self) -> str:
        data = await self.child.stdout.readline()
        if not data:
            raise EOFError("exec-server stdio closed")
        return data.decode("utf-8")

    async def send(self, data: str) -> None:
        self.child.stdin.write(data.encode("utf-8") + b"\n")
        await self.child.stdin.drain()

    async def _terminate_tree(self, *, force: bool) -> None:
        if self.child.returncode is not None:
            return
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/pid", str(self.child.pid), "/t", "/f",
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
        else:
            os.killpg(self.child.pid, signal.SIGKILL if force else signal.SIGTERM)

    async def _close(self) -> TransportCloseReceipt:
        self.child.stdin.close()
        forced = False
        try:
            await asyncio.wait_for(asyncio.shield(self.child.wait()), STDIO_TERMINATION_GRACE_SECONDS)
        except TimeoutError:
            forced = True
            await self._terminate_tree(force=False)
            try:
                await asyncio.wait_for(asyncio.shield(self.child.wait()), STDIO_TERMINATION_GRACE_SECONDS)
            except TimeoutError:
                await self._terminate_tree(force=True)
                await self.child.wait()
        await self.stderr_task
        return TransportCloseReceipt("stdio", self.child.returncode, forced, self.stderr_tail)

    async def close(self) -> TransportCloseReceipt:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="executor-stdio-close")
        return await asyncio.shield(self.close_task)


class WebSocketTransport:
    def __init__(self, websocket):
        self.websocket = websocket

    async def recv(self) -> str:
        data = await self.websocket.recv()
        return data.decode("utf-8") if isinstance(data, bytes) else data

    async def send(self, data: str) -> None:
        await self.websocket.send(data)

    async def close(self) -> TransportCloseReceipt:
        await self.websocket.close()
        await self.websocket.wait_closed()
        return TransportCloseReceipt("websocket")


async def open_transport(spec: EnvironmentSpec) -> StdioTransport | WebSocketTransport:
    if spec.program is not None:
        inherited = shell_subprocess_env(extra=spec.env, allow_process_control_overrides=True, vault_scope=None)
        child_env = {key: value for key, value in inherited.items()
                     if key.upper() not in NON_INHERITABLE_ENV_VARS}
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        child = await asyncio.create_subprocess_exec(
            spec.program, *spec.args, env=child_env, cwd=spec.cwd,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=MAX_MESSAGE_BYTES + 1, **options,
        )
        return StdioTransport(child)
    host = parse_uri(spec.url).host
    loopback = host.lower() == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    headers = {"Authorization": f"Bearer {spec.auth_bearer_token}"} if spec.auth_bearer_token is not None else {}
    websocket = await connect(spec.url, additional_headers=headers, open_timeout=spec.connect_timeout_sec,
                              max_size=MAX_MESSAGE_BYTES, ping_interval=30,
                              **({"proxy": None} if loopback else {}))
    return WebSocketTransport(websocket)
