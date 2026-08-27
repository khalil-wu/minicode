"""Bounded, recoverable output storage for MiniCode command hooks.

* stdout and stderr share an 8 MiB in-memory boundary;
* once that boundary is crossed, the complete output is moved to a
  session-owned file and subsequent chunks stream to that file;
* the disk file has a 5 GiB hard guard;
* the inline result becomes a short tail plus the readable full-output path.

The model-context limit remains a separate concern in ``hooks.reducer``.  In
particular, this module must never use the small context limit as a transport
limit: a command can produce diagnostics that should remain recoverable even
when only a compact projection is sent to the model.
"""

from __future__ import annotations

import asyncio
import codecs
import hashlib
import os
import re
import tempfile
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, TypeVar

from backend.agent.tool_result_persistence import TOOL_RESULT_DATA_DIR


DEFAULT_MAX_MEMORY_BYTES = 8 * 1024 * 1024
MAX_HOOK_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
MAX_HOOK_OUTPUT_BYTES_DISPLAY = "5GB"

_RECENT_LINE_COUNT = 1_000
_INLINE_TAIL_LINE_COUNT = 5
_MAX_PARTIAL_LINE_CHARS = 4_096
_STREAM_CHUNK_BYTES = 64 * 1024
_DISK_CAP_NOTICE = (
    f"\n[output truncated: exceeded {MAX_HOOK_OUTPUT_BYTES_DISPLAY} disk cap]\n"
).encode("utf-8")
_IOResult = TypeVar("_IOResult")


class HookOutputCaptureError(RuntimeError):
    """The host could not preserve a hook's streamed output safely."""


class HookTaskOutput:
    """Own stdout/stderr capture for one command hook process."""

    def __init__(
        self,
        *,
        scope_id: str,
        task_id: str,
        max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
        max_disk_bytes: int = MAX_HOOK_OUTPUT_BYTES,
    ) -> None:
        self.scope_id = str(scope_id or "startup").strip() or "startup"
        self.task_id = _safe_task_id(task_id)
        self.max_memory_bytes = max(0, int(max_memory_bytes))
        self.max_disk_bytes = max(0, int(max_disk_bytes))

        self._stdout = bytearray()
        self._stderr = bytearray()
        self._path: Path | None = None
        self._file: BinaryIO | None = None
        self._disk_bytes = 0
        self._disk_capped = False
        self._finished = False
        self._lock = asyncio.Lock()

        self._recent_lines: deque[str] = deque(maxlen=_RECENT_LINE_COUNT)
        self._stdout_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stderr_decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._stdout_fragment = ""
        self._stderr_fragment = ""
        self.total_bytes = 0

    @property
    def path(self) -> str:
        return str(self._path) if self._path is not None else ""

    @property
    def spilled_to_disk(self) -> bool:
        return self._path is not None

    @property
    def disk_capped(self) -> bool:
        return self._disk_capped

    async def write_stdout(self, data: bytes) -> None:
        await self._write(data, is_stderr=False)

    async def write_stderr(self, data: bytes) -> None:
        await self._write(data, is_stderr=True)

    async def _write(self, data: bytes, *, is_stderr: bool) -> None:
        if not data:
            return
        payload = bytes(data)
        async with self._lock:
            if self._finished:
                raise HookOutputCaptureError("hook output arrived after capture was finalized")
            self.total_bytes += len(payload)
            self._remember_recent(payload, is_stderr=is_stderr)

            if self._file is not None:
                await self._append_disk(payload, is_stderr=is_stderr)
                return

            buffered = len(self._stdout) + len(self._stderr)
            if buffered + len(payload) > self.max_memory_bytes:
                await self._spill_to_disk()
                await self._append_disk(payload, is_stderr=is_stderr)
                return

            target = self._stderr if is_stderr else self._stdout
            target.extend(payload)

    def _remember_recent(self, data: bytes, *, is_stderr: bool) -> None:
        decoder = self._stderr_decoder if is_stderr else self._stdout_decoder
        fragment = self._stderr_fragment if is_stderr else self._stdout_fragment
        text = fragment + decoder.decode(data, final=False)
        lines = text.split("\n")
        fragment = lines.pop() if lines else ""
        if len(fragment) > _MAX_PARTIAL_LINE_CHARS:
            fragment = fragment[-_MAX_PARTIAL_LINE_CHARS:]
        prefix = "[stderr] " if is_stderr else ""
        for line in lines:
            clean = line.rstrip("\r")
            if clean.strip():
                self._recent_lines.append(prefix + clean)
        if is_stderr:
            self._stderr_fragment = fragment
        else:
            self._stdout_fragment = fragment

    async def _spill_to_disk(self) -> None:
        if self._file is not None:
            return
        path: Path | None = None
        handle: BinaryIO | None = None
        try:
            directory = _scope_output_directory(self.scope_id)
            path, handle = await _open_hook_output_file(
                directory,
                prefix=f"{self.task_id}-",
                suffix=".output",
            )
            # Flush the pre-overflow buffers as one operation.  Keeping the
            # bytearrays intact until that write succeeds prevents a failed
            # spill from silently discarding the only complete copy.
            initial = bytes(self._stdout)
            if self._stderr:
                initial += b"[stderr] " + bytes(self._stderr)
            disk_bytes = 0
            disk_capped = False
            if initial:
                if len(initial) > self.max_disk_bytes:
                    await _run_blocking(_write_all, handle, _DISK_CAP_NOTICE)
                    disk_bytes = len(_DISK_CAP_NOTICE)
                    disk_capped = True
                else:
                    await _run_blocking(_write_all, handle, initial)
                    disk_bytes = len(initial)

            self._path = path
            self._file = handle
            self._disk_bytes = disk_bytes
            self._disk_capped = disk_capped
            self._stdout.clear()
            self._stderr.clear()
        except asyncio.CancelledError:
            if handle is not None and path is not None:
                await _discard_unpublished_output(handle, path)
            raise
        except Exception as exc:
            if handle is not None and path is not None:
                await _discard_unpublished_output(handle, path)
            raise HookOutputCaptureError(
                f"failed to create hook output file under {TOOL_RESULT_DATA_DIR}: {exc}"
            ) from exc

    async def _append_disk(self, data: bytes, *, is_stderr: bool) -> None:
        payload = b"[stderr] " + data if is_stderr else data
        await self._append_raw_disk(payload)

    async def _append_raw_disk(self, payload: bytes) -> None:
        if not payload or self._disk_capped:
            return
        handle = self._file
        if handle is None:
            raise HookOutputCaptureError("hook output file is not open")
        try:
            if self._disk_bytes + len(payload) > self.max_disk_bytes:
                await _run_blocking(_write_all, handle, _DISK_CAP_NOTICE)
                self._disk_bytes += len(_DISK_CAP_NOTICE)
                self._disk_capped = True
                return
            await _run_blocking(_write_all, handle, payload)
            self._disk_bytes += len(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise HookOutputCaptureError(
                f"failed to append hook output to {self.path or '<unknown>'}: {exc}"
            ) from exc

    async def finish(self) -> None:
        async with self._lock:
            if self._finished:
                return
            self._finished = True
            self._flush_recent_fragment(is_stderr=False)
            self._flush_recent_fragment(is_stderr=True)
            handle = self._file
            self._file = None
            if handle is None:
                return
            try:
                await _run_blocking(handle.flush)
                await _run_blocking(handle.close)
            except asyncio.CancelledError:
                with suppress(asyncio.CancelledError, Exception):
                    await _run_blocking(handle.close)
                raise
            except Exception as exc:
                with suppress(asyncio.CancelledError, Exception):
                    await _run_blocking(handle.close)
                raise HookOutputCaptureError(
                    f"failed to finalize hook output file {self.path}: {exc}"
                ) from exc

    def _flush_recent_fragment(self, *, is_stderr: bool) -> None:
        decoder = self._stderr_decoder if is_stderr else self._stdout_decoder
        fragment = self._stderr_fragment if is_stderr else self._stdout_fragment
        fragment = (fragment + decoder.decode(b"", final=True)).rstrip("\r")
        if fragment.strip():
            prefix = "[stderr] " if is_stderr else ""
            self._recent_lines.append(prefix + fragment[-_MAX_PARTIAL_LINE_CHARS:])
        if is_stderr:
            self._stderr_fragment = ""
        else:
            self._stdout_fragment = ""

    def stdout_text(self) -> str:
        if self._path is None:
            return bytes(self._stdout).decode("utf-8", errors="replace")
        tail = "\n".join(list(self._recent_lines)[-_INLINE_TAIL_LINE_COUNT:])
        size_kib = (self.total_bytes + 512) // 1024
        notice = (
            f"Output truncated ({size_kib}KB total). "
            f"Full output saved to: {self._path}"
        )
        if self._disk_capped:
            notice += f"\n[output truncated: exceeded {MAX_HOOK_OUTPUT_BYTES_DISPLAY} disk cap]"
        return f"{tail}\n{notice}" if tail else notice

    def stderr_text(self) -> str:
        if self._path is not None:
            return ""
        return bytes(self._stderr).decode("utf-8", errors="replace")


async def drain_hook_process_output(
    proc: asyncio.subprocess.Process,
    input_data: bytes,
    *,
    capture: HookTaskOutput,
) -> None:
    """Drain stdin/stdout/stderr concurrently with backpressure."""

    stdout_task = asyncio.create_task(
        _drain_stream(proc.stdout, capture.write_stdout),
        name=f"hook-output:{capture.task_id}:stdout",
    )
    stderr_task = asyncio.create_task(
        _drain_stream(proc.stderr, capture.write_stderr),
        name=f"hook-output:{capture.task_id}:stderr",
    )
    stdin_task = asyncio.create_task(
        _write_stdin(proc, input_data),
        name=f"hook-output:{capture.task_id}:stdin",
    )
    wait_task = asyncio.create_task(
        proc.wait(),
        name=f"hook-output:{capture.task_id}:wait",
    )
    tasks = (stdout_task, stderr_task, stdin_task, wait_task)
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _drain_stream(
    stream: asyncio.StreamReader | None,
    sink: Callable[[bytes], Awaitable[None]],
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            return
        await sink(chunk)


async def _write_stdin(proc: asyncio.subprocess.Process, input_data: bytes) -> None:
    stdin = proc.stdin
    if stdin is None:
        return
    try:
        if input_data:
            stdin.write(input_data)
            await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        # ``Process.communicate`` treats an early-closing child the same way.
        pass
    finally:
        with suppress(Exception):
            stdin.close()
        with suppress(Exception):
            await stdin.wait_closed()


async def _run_blocking(
    operation: Callable[..., _IOResult],
    /,
    *args: object,
    **kwargs: object,
) -> _IOResult:
    """Finish an already-dispatched file operation before propagating cancel."""

    task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A second Task.cancel() must not abandon an already-dispatched file
        # operation. The output store owns the file until the write or close
        # has converged; retain that ownership, then propagate cancellation.
        with suppress(Exception):
            await _await_task_despite_cancellation(task)
        raise


async def _await_task_despite_cancellation(
    task: asyncio.Task[_IOResult],
) -> _IOResult:
    """Wait for an uncancellable worker while absorbing repeated cancels."""

    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            continue
    return task.result()


async def _open_hook_output_file(
    directory: Path,
    *,
    prefix: str,
    suffix: str,
) -> tuple[Path, BinaryIO]:
    """Create one exclusive output file without leaking it on cancellation.

    ``asyncio.to_thread`` cannot be interrupted after dispatch.  If the caller
    is cancelled while ``mkstemp`` is running, wait for that operation and
    explicitly close/unlink the unpublished file before propagating cancel.
    """

    task = asyncio.create_task(
        asyncio.to_thread(
            _create_hook_output_file,
            directory,
            prefix,
            suffix,
        )
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            path, handle = await _await_task_despite_cancellation(task)
        except Exception:
            pass
        else:
            await _discard_unpublished_output(handle, path)
        raise


async def _discard_unpublished_output(handle: BinaryIO, path: Path) -> None:
    with suppress(asyncio.CancelledError, Exception):
        await _run_blocking(_close_and_unlink_output_file, handle, path)


def _create_hook_output_file(
    directory: Path,
    prefix: str,
    suffix: str,
) -> tuple[Path, BinaryIO]:
    directory.mkdir(parents=True, exist_ok=True)
    fd = -1
    raw_path = ""
    handle: BinaryIO | None = None
    try:
        fd, raw_path = tempfile.mkstemp(
            prefix=prefix,
            suffix=suffix,
            dir=str(directory),
        )
        handle = os.fdopen(fd, "ab", buffering=0)
        fd = -1
        return Path(raw_path).resolve(), handle
    except BaseException:
        if handle is not None:
            with suppress(Exception):
                handle.close()
        elif fd >= 0:
            with suppress(OSError):
                os.close(fd)
        if raw_path:
            with suppress(OSError):
                Path(raw_path).unlink()
        raise


def _close_and_unlink_output_file(handle: BinaryIO, path: Path) -> None:
    try:
        handle.close()
    finally:
        with suppress(OSError):
            path.unlink()


def _write_all(handle: BinaryIO, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = handle.write(remaining)
        if written is None or written <= 0:
            raise OSError("short write while preserving hook output")
        remaining = remaining[written:]


def _scope_output_directory(scope_id: str) -> Path:
    raw = str(scope_id or "startup").strip() or "startup"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.")[:32] or "session"
    digest = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    return TOOL_RESULT_DATA_DIR / "hooks" / f"{slug}-{digest}"


def _safe_task_id(task_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id or "hook")).strip("-.")
    return (value[:48] or "hook").lower()
