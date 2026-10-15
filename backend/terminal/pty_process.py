"""PTY transport under the command owner's existing sandbox and lifecycle."""
from __future__ import annotations

import asyncio
import errno
import os
import sys
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


def _attach_controlling_terminal():
    import fcntl
    import termios

    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


async def spawn_pty(command: str | list[str], *, cwd: str | Path | None, env: dict[str, str], pass_fds: tuple[int, ...] = ()):
    if sys.platform == "win32":
        from backend.sandbox.windows_native import pty_command

        argv = command if isinstance(command, list) else [env.get("COMSPEC", "cmd.exe"), "/c", command]
        # Source codex-utils-pty owns ConPTY and its Job Object. The Python owner
        # still owns the wrapper process, captured output and exact tree cancel.
        return await spawn_exec(*pty_command(argv, cwd=cwd), cwd=str(cwd) if cwd else None,
                                env=env, stdin=asyncio.subprocess.PIPE,
                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

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
                  "stderr": slave, "preexec_fn": _attach_controlling_terminal}
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
