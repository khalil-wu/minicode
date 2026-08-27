from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
import subprocess
from contextlib import suppress
from typing import Any

import psutil

logger = logging.getLogger(__name__)


class SubprocessOutputLimitError(RuntimeError):
    """A subprocess stream exceeded its in-memory transport boundary.

    ``captured`` contains exactly the complete bytes retained before the cap.
    Callers must not treat it as a complete command result.  Search-style
    callers get a fail-closed exception instead of silently returning partial
    output.
    """

    # Proof-of-exit evidence for the tree that was killed on overflow; see
    # ``record_unproven_cleanup``.
    cleanup_pending: bool = False
    cleanup_reason: str = ""

    def __init__(self, *, stream_name: str, limit_bytes: int, captured: bytes):
        self.stream_name = stream_name
        self.limit_bytes = limit_bytes
        self.captured = captured
        super().__init__(
            f"{self.stream_name} exceeded the {self.limit_bytes}-byte "
            "subprocess output limit"
        )


class SubprocessTimeoutError(asyncio.TimeoutError):
    """A bounded subprocess wait timed out and carries the teardown verdict.

    Subclasses :class:`asyncio.TimeoutError` so existing ``except TimeoutError``
    callers keep working while callers that own recovery can read
    ``cleanup_pending``.
    """

    cleanup_pending: bool = False
    cleanup_reason: str = ""


def record_unproven_cleanup(
    exc: BaseException,
    *,
    reaped: bool,
    proc: asyncio.subprocess.Process | subprocess.Popen[Any],
) -> None:
    """Attach ``terminate_process_tree``'s verdict to a propagating exception.

    ``False`` from :func:`terminate_process_tree` means the tree may still be
    running, so the caller owns an unfinished cleanup.  Dropping that bool at
    the wrapper boundary left callers unable to distinguish "tree reaped" from
    "tree still running"; the evidence now travels with the exception the same
    way ``terminal/session.py`` and ``preview/launcher.py`` keep it for their
    direct calls.
    """

    pid = getattr(proc, "pid", None)
    exc.cleanup_pending = not reaped  # type: ignore[attr-defined]
    exc.cleanup_reason = (  # type: ignore[attr-defined]
        "" if reaped else f"subprocess_tree_survived_kill:pid={pid}"
    )


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
) -> bool:
    """Terminate the process tree and report whether its exit is proven.

    ``False`` means the tree may still be running: callers own an unfinished
    cleanup and must keep their recovery evidence instead of reporting a
    completed teardown.
    """
    pid = getattr(proc, "pid", None)
    tree_reaped = True
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
            def kill_windows_tree() -> bool:
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
                        return True
                except (OSError, subprocess.TimeoutExpired):
                    pass

                # Use a platform-native process-tree fallback after taskkill
                # cannot reap the owner, retaining support for stripped-down
                # hosts and transient taskkill failures.
                try:
                    parent = psutil.Process(pid)
                except psutil.NoSuchProcess:
                    return True
                descendants = parent.children(recursive=True)
                for process in [*reversed(descendants), parent]:
                    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                        process.kill()
                _, alive = psutil.wait_procs([*descendants, parent], timeout=3.0)
                return not alive

            tree_reaped = await asyncio.to_thread(kill_windows_tree)
        else:
            # Pi and Codex give cooperative processes a bounded grace period
            # before forcing the process group down. This lets git, databases,
            # and build tools flush their own transactional state.
            os.killpg(pid, signal.SIGTERM)
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.05)
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # The group still exists but is not ours to observe.
                    tree_reaped = False
                    break
            else:
                os.killpg(pid, signal.SIGKILL)
                await asyncio.sleep(0.05)
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    tree_reaped = False
    except ProcessLookupError:
        # The group is already gone; only the direct child still needs reaping.
        pass
    except (PermissionError, OSError):
        tree_reaped = False
        returncode = (
            proc.returncode
            if isinstance(proc, asyncio.subprocess.Process)
            else proc.poll()
            if hasattr(proc, "poll")
            else getattr(proc, "returncode", None)
        )
        if returncode is None:
            with suppress(ProcessLookupError):
                proc.kill()
    child_reaped = False
    with suppress(Exception):
        if isinstance(proc, asyncio.subprocess.Process) or inspect.iscoroutinefunction(proc.wait):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        else:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=2.0)
        child_reaped = True
    if not child_reaped:
        returncode = (
            proc.returncode
            if isinstance(proc, asyncio.subprocess.Process)
            else proc.poll()
            if hasattr(proc, "poll")
            else getattr(proc, "returncode", None)
        )
        child_reaped = returncode is not None
    if not (tree_reaped and child_reaped):
        # Logged here so an unfinished teardown leaves evidence even for callers
        # whose own error path already reported a timeout or cancellation.
        logger.warning(
            "Process tree (pid %s) could not be proven terminated "
            "(tree_reaped=%s child_reaped=%s)",
            pid,
            tree_reaped,
            child_reaped,
        )
    return tree_reaped and child_reaped


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
        reaped = await asyncio.shield(terminate_process_tree(proc))
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(operation), timeout=2.0)
        if not operation.done():
            operation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation
        timed_out = SubprocessTimeoutError()
        record_unproven_cleanup(timed_out, reaped=reaped, proc=proc)
        raise timed_out
    except asyncio.CancelledError as exc:
        reaped = await asyncio.shield(terminate_process_tree(proc))
        if not operation.done():
            operation.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(operation, timeout=2.0)
        record_unproven_cleanup(exc, reaped=reaped, proc=proc)
        raise


async def _read_bounded_stream(
    stream: asyncio.StreamReader | None,
    *,
    stream_name: str,
    limit_bytes: int,
) -> bytes:
    """Drain one child stream without ever retaining more than ``limit_bytes``.

    Reading stdout and stderr in separate tasks is important: a child that
    fills either pipe must not deadlock while the other pipe is being drained.
    The extra byte requested at the boundary lets us distinguish an exact-cap
    result from output that continued past the cap.
    """

    if stream is None:
        return b""
    cap = max(0, int(limit_bytes))
    chunks: list[bytes] = []
    retained = 0
    while True:
        remaining = cap - retained
        chunk = await stream.read(min(64 * 1024, remaining + 1))
        if not chunk:
            return b"".join(chunks)
        if len(chunk) > remaining:
            if remaining > 0:
                chunks.append(chunk[:remaining])
            raise SubprocessOutputLimitError(
                stream_name=stream_name,
                limit_bytes=cap,
                captured=b"".join(chunks),
            )
        chunks.append(chunk)
        retained += len(chunk)


async def _close_stdin(
    proc: asyncio.subprocess.Process,
    input_data: bytes | None,
) -> None:
    stdin = proc.stdin
    if stdin is None:
        return
    try:
        if input_data:
            stdin.write(input_data)
            await stdin.drain()
    finally:
        with suppress(Exception):
            stdin.close()
        with suppress(Exception):
            await stdin.wait_closed()


async def _bounded_communicate_operation(
    proc: asyncio.subprocess.Process,
    input_data: bytes | None,
    *,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> tuple[bytes, bytes]:
    stdout_task = asyncio.create_task(
        _read_bounded_stream(
            proc.stdout,
            stream_name="stdout",
            limit_bytes=stdout_limit_bytes,
        )
    )
    stderr_task = asyncio.create_task(
        _read_bounded_stream(
            proc.stderr,
            stream_name="stderr",
            limit_bytes=stderr_limit_bytes,
        )
    )
    stdin_task = asyncio.create_task(_close_stdin(proc, input_data))
    wait_task = asyncio.create_task(proc.wait())
    tasks = (stdout_task, stderr_task, stdin_task, wait_task)
    try:
        stdout, stderr, _stdin_done, _returncode = await asyncio.gather(*tasks)
        return stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.gather(*tasks, return_exceptions=True)


async def communicate_bounded(
    proc: asyncio.subprocess.Process,
    input_data: bytes | None = None,
    *,
    timeout: float | None = None,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
) -> tuple[bytes, bytes]:
    """Communicate with a child using independent bounded stdout/stderr caps.

    The process is terminated as a tree on timeout, cancellation, or output
    overflow.  Overflow is deliberately surfaced as
    :class:`SubprocessOutputLimitError`; callers must not present the retained
    prefix as a successful or complete result.
    """

    operation = asyncio.create_task(
        _bounded_communicate_operation(
            proc,
            input_data,
            stdout_limit_bytes=stdout_limit_bytes,
            stderr_limit_bytes=stderr_limit_bytes,
        )
    )
    try:
        if timeout is None:
            return await operation
        done, _ = await asyncio.wait({operation}, timeout=max(0.0, timeout))
        if operation in done:
            return operation.result()
        reaped = await asyncio.shield(terminate_process_tree(proc))
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(operation), timeout=2.0)
        if not operation.done():
            operation.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await operation
        timed_out = SubprocessTimeoutError()
        record_unproven_cleanup(timed_out, reaped=reaped, proc=proc)
        raise timed_out
    except SubprocessOutputLimitError as exc:
        reaped = await asyncio.shield(terminate_process_tree(proc))
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(operation), timeout=2.0)
        record_unproven_cleanup(exc, reaped=reaped, proc=proc)
        raise
    except asyncio.CancelledError as exc:
        reaped = await asyncio.shield(terminate_process_tree(proc))
        if not operation.done():
            operation.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await asyncio.wait_for(operation, timeout=2.0)
        record_unproven_cleanup(exc, reaped=reaped, proc=proc)
        raise
