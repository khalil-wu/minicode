"""Persistent remote MiniCode host, independent of an SSH/WSL attachment."""
from __future__ import annotations

import asyncio
import hmac
import json
import os
from pathlib import Path
import platform
import secrets
import signal
import socket

from .relay import Relay


PROTOCOL_VERSION = 1


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    return listener


class DesktopHost:
    def __init__(self, daemon_dir: Path, state_root: Path):
        self.daemon_dir, self.state_root = daemon_dir, state_root
        self.runtime_token = secrets.token_hex(32)
        self.bridge_token = secrets.token_hex(32)
        self.control_token = secrets.token_hex(32)
        self.generation = secrets.token_hex(16)
        self.relay: Relay | None = None
        self.server = None
        self.stop_event = asyncio.Event()
        self.native = None

    async def _bridge(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        relay = self.relay
        if relay is None:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        await relay.open_bridge(reader, writer)

    async def emit_native(self, window_id: str, channel: str, payload: dict) -> None:
        relay = self.relay
        if relay is not None:
            await relay.send({"type": "event", "windowInstanceId": window_id, "channel": channel, "payload": payload})

    async def _control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = json.loads(await reader.readline())
        if not hmac.compare_digest(request["token"], self.control_token):
            writer.close()
            await writer.wait_closed()
            return
        if request["action"] == "stop":
            writer.write(b'{"stopping":true}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            self.stop_event.set()
            return
        if request["action"] != "relay":
            raise ValueError("unknown remote desktop control action")
        if self.relay is not None:
            # A live desktop attachment is an owner, not a lease to steal.
            writer.write(b'{"error":"desktop host already has an active attachment"}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return
        writer.write(b'{"attached":true}\n')
        await writer.drain()
        relay = Relay(reader, writer, backend_port=self.runtime["backend_port"],
                      on_closed=lambda: self._detached(relay))
        self.relay = relay
        await relay.run(self.runtime)

    def _detached(self, relay: Relay) -> None:
        if self.relay is relay:
            self.relay = None

    async def run(self) -> None:
        import uvicorn

        bridge = await asyncio.start_server(self._bridge, "127.0.0.1", 0)
        bridge_port = bridge.sockets[0].getsockname()[1]
        backend_socket = _listener()
        backend_port = backend_socket.getsockname()[1]
        bridge_endpoint = f"http://127.0.0.1:{bridge_port}"
        os.environ["MINICODE_STATE_ROOT"] = str(self.state_root)
        os.environ["MINICODE_RUNTIME_TOKEN"] = self.runtime_token
        os.environ["MINICODE_EMBEDDED_BROWSER_ENDPOINT"] = bridge_endpoint
        os.environ["MINICODE_EMBEDDED_BROWSER_TOKEN"] = self.bridge_token
        os.environ.pop("MINICODE_DESKTOP_OWNED", None)
        from backend.main import app
        from backend.version import __version__
        from .native import RemoteNative

        self.native = RemoteNative(self.emit_native, bridge_endpoint, self.bridge_token)
        app.include_router(self.native.router)
        self.runtime = {
            "protocol": PROTOCOL_VERSION, "version": __version__, "generation": self.generation,
            "backend_port": backend_port, "runtime_token": self.runtime_token,
            "bridge_endpoint": bridge_endpoint, "bridge_token": self.bridge_token,
            "platform": platform.system().lower(), "home": str(Path.home()), "state_root": str(self.state_root),
        }
        self.server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning", ws_max_size=1024 * 1024,
                                                  ws_ping_interval=None))
        backend_task = asyncio.create_task(self.server.serve(sockets=[backend_socket]), name="remote-minicode-harness")
        control = None
        state_file = self.daemon_dir / "daemon.json"
        try:
            async with asyncio.timeout(60):
                while not self.server.started:
                    if backend_task.done():
                        await backend_task
                        raise RuntimeError("remote MiniCode backend exited before readiness")
                    await asyncio.sleep(0.02)
            control = await asyncio.start_server(self._control, "127.0.0.1", 0, limit=4096)
            state = {"pid": os.getpid(), "generation": self.generation,
                     "port": control.sockets[0].getsockname()[1], "token": self.control_token}
            temporary = self.daemon_dir / f"daemon-{self.generation}.json"
            temporary.write_text(json.dumps(state), encoding="utf-8")
            temporary.chmod(0o600)
            temporary.replace(state_file)
            stop_task = asyncio.create_task(self.stop_event.wait(), name="remote-host-stop")
            try:
                await asyncio.wait((stop_task, backend_task), return_when=asyncio.FIRST_COMPLETED)
            finally:
                stop_task.cancel()
                await asyncio.gather(stop_task, return_exceptions=True)
        finally:
            if control is not None:
                control.close()
            if self.relay is not None:
                await self.relay.aclose()
            if control is not None:
                await control.wait_closed()
            bridge.close()
            await bridge.wait_closed()
            if self.native is not None:
                await self.native.close()
            self.server.should_exit = True
            await backend_task
            backend_socket.close()
            if state_file.exists() and json.loads(state_file.read_text(encoding="utf-8"))["generation"] == self.generation:
                state_file.unlink()


async def serve(daemon_dir: Path, state_root: Path) -> None:
    host = DesktopHost(daemon_dir, state_root)
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, host.stop_event.set)
    await host.run()
