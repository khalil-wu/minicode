"""Owned stdio host for V8 isolates. No workspace or tool implementations are loaded."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading

# -I excludes cwd, user site packages and PYTHON* injection. The installed
# backend (or this checkout) supplies the host's own modules explicitly.
if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.agent.code_execution_vm import CodeVM
from backend.agent.code_host_protocol import PROTOCOL_VERSION, encode_frame, frame_length
from backend.async_cleanup import to_thread_cancel_safe


class Host:
    def __init__(self):
        self.sessions: dict[str, dict[str, CodeVM]] = {}
        self.seen_sessions: set[str] = set()
        self.requests: dict[int, tuple[dict, asyncio.Task]] = {}
        self.output_lock = asyncio.Lock()
        self.disconnected = False

    async def send(self, message: dict) -> None:
        if self.disconnected:
            return
        frame = encode_frame(message)
        def write():
            sys.stdout.buffer.write(frame)
            sys.stdout.buffer.flush()
        async with self.output_lock:
            await to_thread_cancel_safe(write)

    async def close_cells(self, session_id: str, cell_id: str | None = None) -> None:
        cells = self.sessions[session_id]
        selected = {cell_id: cells.pop(cell_id)} if cell_id is not None else self.sessions.pop(session_id)
        tasks = [task for request, task in self.requests.values()
                 if request.get("session") == session_id and request.get("cell") in selected
                 and request["method"] == "step" and task is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for vm in selected.values():
            vm.close()

    async def handle(self, request: dict) -> None:
        request_id = request["id"]
        try:
            session_id = request["session"]
            match request["method"]:
                case "open":
                    if session_id in self.seen_sessions:
                        raise ValueError("Code host session ID has already been used")
                    self.seen_sessions.add(session_id)
                    self.sessions[session_id] = {}
                    result = None
                case "create":
                    cells = self.sessions[session_id]
                    cell_id = request["cell"]
                    if cell_id in cells:
                        raise ValueError("Code cell has already started")
                    if sum(map(len, self.sessions.values())) >= 128:
                        raise ValueError("Code host has too many active cells")
                    cells[cell_id] = CodeVM()
                    result = None
                case "step":
                    result = await self.sessions[session_id][request["cell"]].step(request["operation"], request["payload"])
                case "close":
                    await self.close_cells(session_id, request["cell"])
                    result = None
                case "shutdown":
                    await self.close_cells(session_id)
                    result = None
                case _:
                    raise ValueError(f"Unknown code host method: {request['method']}")
            response = {"id": request_id, "result": result}
        except asyncio.CancelledError:
            response = {"id": request_id, "error": "Code host request cancelled"}
        except Exception as error:
            response = {"id": request_id, "error": f"{type(error).__name__}: {error}"}
        try:
            await self.send(response)
        finally:
            self.requests.pop(request_id)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        incoming: asyncio.Queue = asyncio.Queue()

        def read_input():
            try:
                while header := sys.stdin.buffer.read(4):
                    length = frame_length(header)
                    payload = sys.stdin.buffer.read(length)
                    if len(payload) != length:
                        raise EOFError("Truncated code host frame")
                    loop.call_soon_threadsafe(incoming.put_nowait, json.loads(payload))
            except Exception as error:
                loop.call_soon_threadsafe(incoming.put_nowait, error)
            finally:
                loop.call_soon_threadsafe(incoming.put_nowait, None)

        threading.Thread(target=read_input, name="code-host-input", daemon=True).start()
        hello = await incoming.get()
        if hello != {"hello": PROTOCOL_VERSION}:
            raise ValueError("Unsupported code host handshake")
        await self.send({"ready": PROTOCOL_VERSION, "capabilities": ["cancel", "session_isolation"]})
        try:
            while (message := await incoming.get()) is not None:
                if isinstance(message, Exception):
                    raise message
                if "cancel" in message:
                    pending = self.requests.get(message["cancel"])
                    if pending is not None and pending[0]["method"] == "step":
                        pending[1].cancel()
                else:
                    request_id = message["id"]
                    if request_id in self.requests:
                        raise ValueError("Duplicate code host request ID")
                    if len(self.requests) >= 256:
                        await self.send({"id": request_id, "error": "Code host has too many in-flight requests"})
                        continue
                    task = asyncio.create_task(self.handle(message), name=f"code-host:{request_id}")
                    self.requests[request_id] = (message, task)
                    def finished(done):
                        if not done.cancelled() and (error := done.exception()) is not None:
                            incoming.put_nowait(error)
                    task.add_done_callback(finished)
                    # Enter the request before a queued cancellation can stop
                    # it; even cancelled V8 operations must send their receipt.
                    await asyncio.sleep(0)
        finally:
            self.disconnected = True
            tasks = [task for _, task in self.requests.values()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for cells in self.sessions.values():
                for vm in cells.values():
                    vm.close()
            self.sessions.clear()


if __name__ == "__main__":
    asyncio.run(Host().run())
