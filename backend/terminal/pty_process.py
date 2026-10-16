"""PTY transport under the command owner's existing sandbox and lifecycle."""
from __future__ import annotations

import asyncio
import errno
import json
import os
import struct
import sys
from contextlib import ExitStack
from pathlib import Path

from backend.subprocesses import spawn_exec, spawn_shell


class _PtyReadProtocol(asyncio.StreamReaderProtocol):
    def connection_lost(self, exc):
        # A Unix PTY reports EIO when its last slave closes; this is terminal EOF.
        super().connection_lost(None if isinstance(exc, OSError) and exc.errno == errno.EIO else exc)


class _PtyWriteProtocol(asyncio.streams.FlowControlMixin):
    def __init__(self, loop):
        super().__init__(loop=loop)
        self.closed = loop.create_future()

    def connection_lost(self, exc):
        super().connection_lost(exc)
        if exc is None:
            self.closed.set_result(None)
        else:
            self.closed.set_exception(exc)

    def _get_close_waiter(self, _stream):
        return self.closed


class PtyProcess:
    def __init__(self, process, stdout, stdin):
        self._process = process
        self.stdout = stdout
        self.stdin = stdin
        self.stderr = None

    @property
    def pid(self):
        return self._process.pid

    @property
    def returncode(self):
        return self._process.returncode

    async def wait(self):
        exit_code = await self._process.wait()
        self.stdin.close()
        return exit_code

    def poll(self):
        return self.returncode

    def kill(self):
        self._process.kill()

    def terminate(self):
        self._process.terminate()

    async def resize(self, cols: int, rows: int):
        import fcntl
        import termios

        pipe = self.stdin.get_extra_info("pipe")
        fcntl.ioctl(pipe.fileno(), termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        return True


class _WindowsPtyControl:
    def __init__(self, owner: ExitStack):
        import msvcrt
        import subprocess

        request_read, request_write = os.pipe()
        self._request_read = owner.enter_context(os.fdopen(request_read, "rb", buffering=0))
        self._request_write = owner.enter_context(os.fdopen(request_write, "wb", buffering=0))
        response_read, response_write = os.pipe()
        self._response_read = owner.enter_context(os.fdopen(response_read, "rb"))
        self._response_write = owner.enter_context(os.fdopen(response_write, "wb", buffering=0))
        os.set_inheritable(request_read, True)
        os.set_inheritable(response_write, True)
        self.handles = (msvcrt.get_osfhandle(request_read), msvcrt.get_osfhandle(response_write))
        self.startupinfo = subprocess.STARTUPINFO(lpAttributeList={"handle_list": list(self.handles)})
        self._lock = asyncio.Lock()

    def release_child_ends(self):
        self._request_read.close()
        self._response_write.close()

    def _resize(self, cols: int, rows: int):
        self._request_write.write(struct.pack("!HH", cols, rows))
        response = self._response_read.readline()
        if not response:
            raise EOFError("the original Windows PTY resize channel closed")
        result = json.loads(response)
        if "error" in result:
            raise RuntimeError(result["error"])
        return result["resized"]

    async def resize(self, cols: int, rows: int):
        from backend.async_cleanup import to_thread_cancel_safe

        async with self._lock:
            return await to_thread_cancel_safe(self._resize, cols, rows)

    async def close(self):
        async with self._lock:
            self._request_write.close()
            self._response_read.close()


class _WindowsPtyProcess(PtyProcess):
    def __init__(self, process, control):
        super().__init__(process, process.stdout, process.stdin)
        self.stderr = process.stderr
        self._control = control

    async def resize(self, cols: int, rows: int):
        return await self._control.resize(cols, rows)

    async def wait(self):
        exit_code = await super().wait()
        await self._control.close()
        return exit_code


def _attach_controlling_terminal():
    import fcntl
    import termios

    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def spawn_pty(command: str | list[str], *, cwd: str | Path | None, env: dict[str, str],
        pass_fds: tuple[int, ...] = (), on_exit=None, creationflags: int | None = None, on_created=None,
        resizable: bool = False):
    if sys.platform == "win32":
        from backend.sandbox.windows_native import pty_command

        argv = command if isinstance(command, list) else [env.get("COMSPEC", "cmd.exe"), "/c", command]
        # Source codex-utils-pty owns ConPTY and its Job Object. The Python owner
        # still owns the wrapper process, captured output and exact tree cancel.
        kwargs = {"creationflags": creationflags} if creationflags is not None else {}
        if resizable:
            with ExitStack() as owner:
                control = _WindowsPtyControl(owner)
                process = await spawn_exec(*pty_command(argv, cwd=cwd, control_handles=control.handles),
                    cwd=str(cwd) if cwd else None, env=env, on_exit=on_exit,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    startupinfo=control.startupinfo, **kwargs)
                control.release_child_ends()
                if on_created is not None:
                    on_created(process)
                owner.pop_all()
                return _WindowsPtyProcess(process, control)
        process = await spawn_exec(*pty_command(argv, cwd=cwd), cwd=str(cwd) if cwd else None,
                                env=env, on_exit=on_exit, stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs)
        if on_created is not None:
            on_created(process)
        return process

    import fcntl
    import struct
    import termios

    master, slave = os.openpty()
    read_transport = None
    write_transport = None
    handed_off = False
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        kwargs = {"cwd": str(cwd) if cwd else None, "env": env, "stdin": slave, "stdout": slave,
                  "stderr": slave, "preexec_fn": _attach_controlling_terminal, "on_exit": on_exit}
        if pass_fds:
            kwargs["pass_fds"] = pass_fds
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        read_transport, _ = await loop.connect_read_pipe(lambda: _PtyReadProtocol(reader), os.fdopen(os.dup(master), "rb", 0))
        write_protocol = _PtyWriteProtocol(loop)
        write_transport, _ = await loop.connect_write_pipe(lambda: write_protocol, os.fdopen(os.dup(master), "wb", 0))
        process = (await spawn_exec(*command, **kwargs) if isinstance(command, list)
                   else await spawn_shell(command, **kwargs))
        handed_off = True
        return PtyProcess(process, reader, asyncio.StreamWriter(write_transport, write_protocol, reader, loop))
    finally:
        os.close(slave)
        os.close(master)
        if not handed_off:
            if read_transport is not None:
                read_transport.close()
            if write_transport is not None:
                write_transport.close()
