"""Native PTYs on the selected desktop host, with original window ownership."""
from __future__ import annotations

import asyncio
import codecs
from dataclasses import dataclass
import os
import time
from uuid import uuid4

from backend.runtime_env import default_user_shell, shell_subprocess_env
from backend.subprocesses import terminate_process_tree
from backend.terminal.pty_process import spawn_pty
from backend.terminal.session import TerminalSession
from fastapi import HTTPException


@dataclass
class _Terminal:
    window_id: str
    record: TerminalSession
    process: object
    reader_task: asyncio.Task | None = None


class NativeTerminals:
    def __init__(self, emit):
        self.emit = emit
        self.sessions: dict[str, _Terminal] = {}
        self.spawn_lock = asyncio.Lock()

    def owned(self, session_id: str, conversation_id: str, window_id: str) -> _Terminal:
        terminal = self.sessions.get(session_id)
        if terminal is None:
            raise HTTPException(404, "remote terminal session is unavailable")
        if terminal.window_id != window_id or terminal.record.conversation_id != conversation_id:
            raise HTTPException(403, "remote terminal belongs to another conversation or window")
        return terminal

    async def spawn(self, cwd: str, conversation_id: str, window_id: str) -> dict:
        async with self.spawn_lock:
            if sum(item.record.is_alive for item in self.sessions.values()) >= 8:
                raise RuntimeError("remote desktop has reached its eight live terminal limit")
            session_id = f"pty-{uuid4().hex}"
            selected = default_user_shell()
            shell = selected["path"]
            if os.name != "nt":
                argv = [shell, "-i"]
            elif selected["name"] == "powershell":
                from backend.terminal.session import _windows_powershell_init_command

                argv = [shell, "-NoLogo", "-NoProfile", "-NoExit", "-Command", _windows_powershell_init_command()]
            else:
                argv = [shell, "/Q", "/K", "chcp 65001 >nul"]
            process = await spawn_pty(argv, cwd=cwd,
                env=shell_subprocess_env(extra={"TERM": "xterm-256color"}, vault_scope=None))
            record = TerminalSession(session_id, cwd=cwd, conversation_id=conversation_id)
            record.update_external_metadata(cwd=cwd, shell=shell, pid=process.pid)
            terminal = _Terminal(window_id, record, process)
            self.sessions[session_id] = terminal
            terminal.reader_task = asyncio.create_task(self._read(terminal), name=f"remote-{session_id}")
            return record.snapshot()

    async def _read(self, terminal: _Terminal) -> None:
        record, process = terminal.record, terminal.process

        async def append(data: str) -> None:
            if not data:
                return
            start = record.snapshot(max_chars=0)["output_end_cursor"]
            record.append_external_output(data)
            end = record.snapshot(max_chars=0)["output_end_cursor"]
            await self.emit(terminal.window_id, "minicode:pty:data", {
                "sessionId": record.session_id, "conversationId": record.conversation_id,
                "data": data, "startCursor": start, "endCursor": end,
            })

        async def pump(stream):
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            while data := await stream.read(64 * 1024):
                await append(decoder.decode(data))
            await append(decoder.decode(b"", final=True))

        async with asyncio.TaskGroup() as group:
            group.create_task(pump(process.stdout))
            if process.stderr is not None:
                group.create_task(pump(process.stderr))
        code = await process.wait()
        record.update_external_metadata(is_alive=False, exit_code=code, exited_at=time.time() * 1000)
        await self.emit(terminal.window_id, "minicode:pty:exit", {
            "sessionId": record.session_id, "conversationId": record.conversation_id, "exitCode": code,
            "exitSignal": None, "exitedAt": record.info.exited_at,
        })

    async def kill(self, terminal: _Terminal, *, preserve: bool = False) -> bool:
        if terminal.record.is_alive:
            reaped = await terminate_process_tree(terminal.process)
            if not reaped:
                raise RuntimeError(f"remote terminal process tree has not exited: {terminal.process.pid}")
        await terminal.reader_task
        if not preserve:
            del self.sessions[terminal.record.session_id]
        return True

    async def request(self, action: str, args: list, window_id: str):
        if action == "spawn":
            cwd, conversation_id = args
            return await self.spawn(cwd, conversation_id, window_id)
        if action in {"list", "killConversation"}:
            conversation_id = args[0]
            terminals = [item for item in self.sessions.values()
                if item.window_id == window_id and item.record.conversation_id == conversation_id]
            if action == "list":
                return [item.record.snapshot(max_chars=8000) for item in terminals]
            preserve = args[1]
            results = await asyncio.gather(*(self.kill(item, preserve=preserve) for item in terminals))
            return sum(results)
        conversation_index = {"write": 2, "resize": 3, "snapshot": 2}.get(action, 1)
        terminal = self.owned(args[0], args[conversation_index], window_id)
        if action == "write":
            terminal.process.stdin.write(args[1].encode("utf-8"))
            await terminal.process.stdin.drain()
            return True
        if action == "resize":
            if os.name == "nt":
                raise HTTPException(409, "the remote Windows PTY runtime does not expose a resize channel")
            import fcntl
            import struct
            import termios

            cols, rows = args[1:3]
            pipe = terminal.process.stdin.get_extra_info("pipe")
            fcntl.ioctl(pipe.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
            return True
        if action == "snapshot":
            return terminal.record.snapshot(max_chars=args[1])
        if action == "clear":
            terminal.record.clear_output()
            return {"cleared": True, "outputCursor": terminal.record.snapshot(max_chars=0)["output_end_cursor"]}
        if action == "ackExit":
            if terminal.record.is_alive:
                return False
            del self.sessions[args[0]]
            return True
        if action == "kill":
            return await self.kill(terminal)
        if action == "restart":
            cwd = terminal.record.info.cwd
            conversation_id = terminal.record.conversation_id
            await self.kill(terminal)
            return await self.spawn(cwd, conversation_id, window_id)
        raise ValueError(f"unknown remote terminal operation: {action}")

    async def release_window(self, window_id: str) -> None:
        await asyncio.gather(*(self.kill(item) for item in tuple(self.sessions.values()) if item.window_id == window_id))

    async def close(self) -> None:
        await asyncio.gather(*(self.kill(item) for item in tuple(self.sessions.values())))
