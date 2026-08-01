from __future__ import annotations

import asyncio
import inspect
import os
import signal
import subprocess
from contextlib import suppress
from typing import Any

import psutil


def process_group_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {
            "creationflags": (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        }
    return {"start_new_session": True}


async def spawn_exec(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
    kwargs.update(process_group_kwargs())
    return await asyncio.create_subprocess_exec(*args, **kwargs)


async def spawn_shell(command: str, **kwargs: Any) -> asyncio.subprocess.Process:
    kwargs.update(process_group_kwargs())
    return await asyncio.create_subprocess_shell(command, **kwargs)


async def terminate_process_tree(
    proc: asyncio.subprocess.Process | subprocess.Popen[Any],
) -> None:
    pid = getattr(proc, "pid", None)
    try:
        if pid is None:
            # Test doubles and threaded fallbacks may expose only the small
            # Popen-like surface.  Preserve cancellation semantics by using
            # their direct-child kill hook when no process-tree identity is
            # available; real spawned processes always take the tree/group
            # path below.
            returncode = (
                proc.returncode
                if isinstance(proc, asyncio.subprocess.Process)
                else proc.poll()
                if hasattr(proc, "poll")
                else getattr(proc, "returncode", None)
            )
            if returncode is None:
                with suppress(Exception):
                    proc.kill()
        elif os.name == "nt":
            def kill_windows_tree() -> None:
                taskkill_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                try:
                    completed = subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5.0,
                        check=False,
                        creationflags=taskkill_flags,
                    )
                    if completed.returncode == 0:
                        return
                except (OSError, subprocess.TimeoutExpired):
                    pass

                # Match Codex's taskkill-based Windows MCP cleanup, while
                # retaining the existing in-process fallback for stripped-down
                # hosts or transient taskkill failures.
                try:
                    parent = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    return
                descendants = parent.children(recursive=True)
                for process in [*reversed(descendants), parent]:
                    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        process.kill()
                psutil.wait_procs([*descendants, parent], timeout=3.0)

            await asyncio.to_thread(kill_windows_tree)
        else:
            # spawn_exec/spawn_shell create a new session whose process-group
            # id is the leader pid. Address that group directly: looking up the
            # leader first fails if it exited while a descendant still holds a
            # stdout/stderr pipe open.
            os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        returncode = (
            proc.returncode
            if isinstance(proc, asyncio.subprocess.Process)
            else proc.poll()
        )
        if returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
    with suppress(Exception):
        if isinstance(proc, asyncio.subprocess.Process) or inspect.iscoroutinefunction(proc.wait):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        else:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=2.0)


async def communicate(
    proc: asyncio.subprocess.Process,
    input_data: bytes | None = None,
    *,
    timeout: float | None = None,
) -> tuple[bytes, bytes]:
    operation = asyncio.create_task(
        proc.communicate() if input_data is None else proc.communicate(input_data)
    )
    try:
        if timeout is None:
            return await operation
        done, _ = await asyncio.wait({operation}, timeout=max(0.0, timeout))
        if operation in done:
            return operation.result()
        await asyncio.shield(terminate_process_tree(proc))
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(operation), timeout=2.0)
        if not operation.done():
            operation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation
        raise asyncio.TimeoutError
    except asyncio.CancelledError:
        await asyncio.shield(terminate_process_tree(proc))
        if not operation.done():
            operation.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(operation, timeout=2.0)
        raise
