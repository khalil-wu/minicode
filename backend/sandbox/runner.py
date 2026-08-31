"""Sandbox runner — executes commands under OS-level isolation."""
from __future__ import annotations

import asyncio
import codecs
from contextlib import suppress
from fnmatch import fnmatchcase
import json
import locale
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, BinaryIO, Callable

from backend.config import DATA_ROOT
from backend.runtime_env import shell_subprocess_env
from backend.sandbox.policy import (
    FileSystemAccessMode,
    FileSystemPath,
    FileSystemSpecialPath,
    ResolvedSandboxPolicy,
    SandboxEnforcement,
    SandboxPolicy,
)
from backend.sandbox.result import SandboxResult
from backend.subprocesses import communicate, spawn_exec, spawn_shell, terminate_process_tree
from backend.tools.base import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    truncate_text_tail,
)

logger = logging.getLogger(__name__)

# Compatibility alias for callers that imported the former runner-local cap.
# The actual contract is Pi's shared 2000-line/50-KiB tool-output boundary.
MAX_OUTPUT_LENGTH = MAX_TOOL_RESULT_BYTES
_CAPTURED_OUTPUT_BYTES = MAX_TOOL_RESULT_BYTES * 2
_LEGACY_MARKER_BUDGET = 160
_LEGACY_CAPTURED_BYTES = MAX_TOOL_RESULT_BYTES - _LEGACY_MARKER_BUDGET
_LEGACY_CAPTURED_HEAD_BYTES = _LEGACY_CAPTURED_BYTES // 2
_LEGACY_CAPTURED_TAIL_BYTES = _LEGACY_CAPTURED_BYTES - _LEGACY_CAPTURED_HEAD_BYTES
_CONTAINER_RUNTIME_CACHE_TTL_SECONDS = 10.0
_container_runtime_cache: tuple[float, str, str, str, str] | None = None
_bubblewrap_probe_cache: tuple[float, bool, str] | None = None


def _append_bounded_output(sink: bytearray, chunk: bytes) -> None:
    """Compatibility helper for callers of the former runner API."""

    sink.extend(chunk)
    if len(sink) <= _CAPTURED_OUTPUT_BYTES:
        return
    retained = bytes(sink)
    sink[:] = retained[:_LEGACY_CAPTURED_HEAD_BYTES] + retained[-_LEGACY_CAPTURED_TAIL_BYTES:]


def _decode_bounded_output(sink: bytearray, total_bytes: int) -> str:
    if total_bytes <= MAX_TOOL_RESULT_BYTES:
        return _decode_command_bytes(bytes(sink))
    retained = bytes(sink)
    return (
        f"{_decode_command_bytes(retained[:_LEGACY_CAPTURED_HEAD_BYTES])}\n\n"
        f"[... {total_bytes - len(retained)} bytes truncated; showing beginning and end ...]\n\n"
        f"{_decode_command_bytes(retained[-_LEGACY_CAPTURED_TAIL_BYTES:])}"
    )


@dataclass(frozen=True, slots=True)
class SandboxCapability:
    """Effective isolation available for one sandbox policy on this host."""

    available: bool
    backend: str
    filesystem_isolated: bool
    network_isolated: bool
    deny_read_isolated: bool = False
    protected_paths_isolated: bool = False
    reason: str = ""


class SandboxUnavailableError(RuntimeError):
    """Raised before process creation when the requested policy is unenforceable."""


@dataclass(frozen=True, slots=True)
class _SyntheticMountTarget:
    """Host mount target created only for sandbox namespace construction.

    This mirrors Codex's SyntheticMountTarget lifecycle: cleanup removes only
    the exact empty inode created by this runner, after the child confirms that
    its mount namespace is ready.
    """

    path: Path
    is_directory: bool
    device: int
    inode: int

    def remove_if_owned(self) -> None:
        try:
            stat = self.path.stat(follow_symlinks=False)
        except OSError:
            return
        if stat.st_dev != self.device or stat.st_ino != self.inode:
            return
        try:
            if self.is_directory:
                self.path.rmdir()
            elif stat.st_size == 0:
                self.path.unlink()
        except OSError:
            return


def _preferred_oem_encoding() -> str:
    """Best-effort name of the encoding native console tools emit on this host.

    On Windows this is the OEM/ANSI codepage (e.g. cp936 on zh-CN), which is what
    git/cmd/dir write when they have not been told to use UTF-8. Elsewhere it is
    the locale preferred encoding. Used as a fallback when bytes are not valid
    UTF-8 so non-ASCII output is not replaced with U+FFFD garbage.
    """
    if sys.platform == "win32":
        try:
            import ctypes  # noqa: PLC0415

            cp = ctypes.windll.kernel32.GetOEMCP()
            if cp:
                return f"cp{cp}"
        except Exception:
            pass
    try:
        return locale.getpreferredencoding(False) or "utf-8"
    except Exception:
        return "utf-8"


def _decode_command_bytes(data: bytes) -> str:
    """Decode finished command output, tolerating non-UTF-8 native tool output.

    Tries strict UTF-8 first (the common case once children are nudged toward
    UTF-8), then the host OEM/locale encoding for legacy native tools, and only
    then falls back to lossy UTF-8 so we never raise.
    """
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        fallback = _preferred_oem_encoding()
        if fallback.lower() not in ("utf-8", "utf8"):
            try:
                return data.decode(fallback)
            except (UnicodeDecodeError, LookupError):
                pass
        return data.decode("utf-8", errors="replace")


class _OutputCapture:
    """Bounded streaming capture ported from Pi's OutputAccumulator design."""

    def __init__(self, *, preserve_full_output: bool, prefix: str) -> None:
        self._preserve_full_output = preserve_full_output
        self._prefix = prefix
        self._tail = bytearray()
        self._raw_chunks: list[bytes] = []
        self._file: BinaryIO | None = None
        self._path: Path | None = None
        self.total_bytes = 0
        self._completed_lines = 0
        self._has_open_line = False

    @property
    def total_lines(self) -> int:
        return self._completed_lines + (1 if self._has_open_line else 0)

    @property
    def path(self) -> str:
        return str(self._path) if self._path is not None else ""

    def append(self, data: bytes) -> None:
        if not data:
            return
        self.total_bytes += len(data)
        self._completed_lines += data.count(b"\n")
        self._has_open_line = not data.endswith(b"\n")
        _append_bounded_output(self._tail, data)

        if self._file is not None:
            self._file.write(data)
            return
        if self._preserve_full_output:
            self._raw_chunks.append(data)
            if self.total_bytes > MAX_TOOL_RESULT_BYTES or self.total_lines > MAX_TOOL_RESULT_LINES:
                self._ensure_file()

    def finish(self) -> None:
        if (
            self._preserve_full_output
            and (self.total_bytes > MAX_TOOL_RESULT_BYTES or self.total_lines > MAX_TOOL_RESULT_LINES)
        ):
            try:
                self._ensure_file()
            except OSError:
                logger.warning("Sandbox output capture could not create full-output file", exc_info=True)
        if self._file is not None:
            try:
                for chunk in self._raw_chunks:
                    self._file.write(chunk)
                self._raw_chunks.clear()
                self._file.flush()
                os.fsync(self._file.fileno())
                self._file.close()
            except OSError:
                # The command result remains authoritative even if the optional
                # full-output persistence fails. Keep the bounded snapshot and
                # report persistence through the existing empty path contract.
                logger.warning(
                    "Sandbox output capture could not persist full output",
                    exc_info=True,
                )
                with suppress(OSError):
                    self._file.close()
                self._path = None
            finally:
                self._file = None

    def snapshot(self) -> str:
        raw_tail = bytes(self._tail)
        if self.total_bytes > len(raw_tail):
            newline = raw_tail.find(b"\n")
            if newline >= 0:
                raw_tail = raw_tail[newline + 1 :]
        return truncate_text_tail(_decode_command_bytes(raw_tail)).content

    def _ensure_file(self) -> None:
        if self._path is not None:
            return
        output_dir = DATA_ROOT / "tool-results"
        output_dir.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w+b",
            prefix=f"{self._prefix}-",
            suffix=".log",
            dir=output_dir,
            delete=False,
        )
        self._path = Path(handle.name)
        self._file = handle
        for chunk in self._raw_chunks:
            handle.write(chunk)
        self._raw_chunks.clear()


def read_captured_output(path: str, fallback: str = "") -> str:
    """Read a preserved command stream using the runner's host decoding rules."""

    if not path:
        return fallback
    try:
        return _decode_command_bytes(Path(path).read_bytes())
    except OSError:
        return fallback


def cleanup_captured_output(*paths: str) -> None:
    """Remove runner-owned temporary stream files after durable persistence."""

    for raw_path in paths:
        if not raw_path:
            continue
        with suppress(OSError):
            Path(raw_path).unlink()


def _container_runtime() -> tuple[str, str, str]:
    """Resolve a ready local container runtime and prebuilt sandbox image.

    Image pulls are deliberately disabled by the generated command, so this
    preflight cannot turn a no-network tool call into an implicit daemon-side
    download. The short cache avoids a CLI round-trip for every agent command.
    """
    global _container_runtime_cache
    requested = str(os.environ.get("MINICODE_SANDBOX_RUNTIME", "") or "").strip().lower()
    image = str(
        os.environ.get("MINICODE_SANDBOX_IMAGE", "minicode-agent-sandbox:latest")
        or ""
    ).strip()
    cache_key = f"{requested}\0{image}"
    now = time.monotonic()
    cached = _container_runtime_cache
    if cached is not None and cached[1] == cache_key and now - cached[0] < _CONTAINER_RUNTIME_CACHE_TTL_SECONDS:
        return cached[2], cached[3], cached[4]

    candidates = (requested,) if requested else ("docker", "podman")
    reasons: list[str] = []
    if not image:
        result = ("", "", "MINICODE_SANDBOX_IMAGE is empty")
        _container_runtime_cache = (now, cache_key, *result)
        return result
    for engine in candidates:
        if engine not in {"docker", "podman"}:
            reasons.append(f"Unsupported container runtime: {engine}")
            continue
        executable = shutil.which(engine)
        if not executable:
            reasons.append(f"{engine} is not installed")
            continue
        inspect_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "timeout": 4.0,
            "check": False,
        }
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if os.name == "nt" and no_window:
            inspect_kwargs["creationflags"] = no_window
        try:
            inspected = subprocess.run(
                [executable, "image", "inspect", image],
                **inspect_kwargs,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            reasons.append(f"{engine} is unavailable: {exc}")
            continue
        if inspected.returncode != 0:
            reasons.append(
                f"{engine} image {image!r} is unavailable; build backend/sandbox/Dockerfile first"
            )
            continue
        result = (engine, image, "")
        _container_runtime_cache = (now, cache_key, *result)
        return result

    result = ("", image, "; ".join(reasons) or "No supported container runtime is available")
    _container_runtime_cache = (now, cache_key, *result)
    return result


def _bubblewrap_capability() -> tuple[bool, str]:
    """Probe the user-namespace operation that the generated wrapper needs."""

    global _bubblewrap_probe_cache
    if sys.platform != "linux":
        return False, "Bubblewrap is only available on Linux"
    now = time.monotonic()
    cached = _bubblewrap_probe_cache
    if cached is not None and now - cached[0] < 30.0:
        return cached[1], cached[2]
    executable = shutil.which("bwrap")
    if not executable:
        result = (False, "Bubblewrap (bwrap) is not installed")
        _bubblewrap_probe_cache = (now, *result)
        return result
    try:
        probe = subprocess.run(
            [
                executable,
                "--unshare-user",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--perms",
                "000",
                "--tmpfs",
                "/tmp",
                "--remount-ro",
                "/tmp",
                "--",
                "true",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=3.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = (False, f"Bubblewrap probe failed: {exc}")
    else:
        detail = _decode_command_bytes(probe.stderr).strip()
        result = (
            probe.returncode == 0,
            "" if probe.returncode == 0 else f"Bubblewrap user namespace is unavailable: {detail or probe.returncode}",
        )
    _bubblewrap_probe_cache = (now, *result)
    return result


class SandboxRunner:
    """Execute shell commands within a SandboxPolicy.

    Isolation strategy by platform:
      - Linux: Bubblewrap filesystem namespace plus optional network namespace
      - macOS: sandbox-exec with a generated Seatbelt profile
      - Windows: MiniCode-owned container capability when available; otherwise
        fail closed before process creation

    Full-access policies still use process groups for reliable tree cleanup,
    but process grouping is never reported as a security boundary.
    """

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy
        self._container_engine = ""
        self._container_cidfile: Path | None = None
        self._container_name = ""
        self._synthetic_mount_targets: list[_SyntheticMountTarget] = []
        self._synthetic_mount_overrides: dict[str, Path] = {}
        self._sandbox_ready_file: Path | None = None

    def capability(self, *, cwd: str | Path | None = None) -> SandboxCapability:
        """Report the isolation that will actually be enforced.

        A process sandbox must enforce both the declared filesystem boundary
        and, when requested, network isolation. Process groups and command
        string validation are not filesystem sandboxes.
        """
        resolved = self._policy.resolve(cwd=cwd)
        if resolved.enforcement is SandboxEnforcement.DISABLED:
            return SandboxCapability(
                available=True,
                backend="full-access",
                filesystem_isolated=False,
                network_isolated=False,
            )
        if resolved.enforcement is SandboxEnforcement.EXTERNAL:
            # Codex treats ExternalSandbox as an already-established boundary:
            # MiniCode must not layer or second-guess a platform sandbox here.
            return SandboxCapability(
                available=True,
                backend="external-sandbox",
                filesystem_isolated=False,
                network_isolated=False,
                deny_read_isolated=False,
                protected_paths_isolated=False,
            )
        if (
            sys.platform == "darwin"
            and shutil.which("sandbox-exec")
            and _seatbelt_policy_supported(resolved)
        ):
            return SandboxCapability(
                available=True,
                backend="seatbelt",
                filesystem_isolated=True,
                network_isolated=not resolved.allow_network,
                deny_read_isolated=True,
                protected_paths_isolated=_protected_paths_fully_isolated(resolved),
            )
        bwrap_available, bwrap_reason = _bubblewrap_capability()
        policy_preflight_error = _sandbox_policy_preflight_error(resolved)
        if bwrap_available and not policy_preflight_error:
            return SandboxCapability(
                available=True,
                backend="bubblewrap",
                filesystem_isolated=True,
                network_isolated=not resolved.allow_network,
                deny_read_isolated=True,
                protected_paths_isolated=_protected_paths_fully_isolated(resolved),
            )
        container_engine, container_image, container_reason = _container_runtime()
        container_can_represent = not _has_filesystem_root_write(resolved) and (
            not resolved.root_read_baseline or sys.platform == "win32"
        )
        if (
            container_engine
            and container_image
            and container_can_represent
            and not policy_preflight_error
        ):
            return SandboxCapability(
                available=True,
                backend=container_engine,
                filesystem_isolated=True,
                network_isolated=not resolved.allow_network,
                deny_read_isolated=True,
                protected_paths_isolated=_protected_paths_fully_isolated(resolved),
            )
        if sys.platform == "win32":
            reason = "; ".join(
                part
                for part in (
                    policy_preflight_error,
                    container_reason,
                )
                if part
            ) or "No enforceable Windows sandbox backend is available"
        elif sys.platform == "linux":
            reason = policy_preflight_error or bwrap_reason or "Bubblewrap is required to enforce the workspace filesystem boundary"
        else:
            reason = f"No supported OS sandbox backend is available for {sys.platform}"
        return SandboxCapability(
            available=False,
            backend="unavailable",
            filesystem_isolated=False,
            network_isolated=False,
            reason=reason,
        )

    def prepare_command(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        host_command: str = "",
    ) -> tuple[str | list[str], SandboxCapability]:
        """Return an enforceable command wrapper or fail before process creation."""
        self._cleanup_sandbox_setup_state()
        capability = self.capability(cwd=cwd)
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        try:
            wrapped = self._wrap_command(
                command,
                capability,
                cwd=cwd,
                host_command=host_command,
            )
        except Exception:
            self._cleanup_sandbox_setup_state()
            raise
        return wrapped, capability

    async def spawn_interactive(
        self,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        container_argv: list[str] | None = None,
        stdin: Any = asyncio.subprocess.PIPE,
        stdout: Any = asyncio.subprocess.PIPE,
        stderr: Any = asyncio.subprocess.PIPE,
    ) -> asyncio.subprocess.Process:
        """Start a long-lived stdio process behind the same sandbox boundary.

        Interactive services such as language servers need ownership of their
        stdin/stdout streams, so they cannot use ``run``.  This entry point
        deliberately shares command wrapping, environment construction and
        process-tree cleanup with normal sandboxed commands.
        """
        if not argv or not str(argv[0]).strip():
            raise ValueError("Sandbox process argv must not be empty")
        capability = self.capability(cwd=cwd)
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)

        effective_argv = (
            list(container_argv)
            if capability.backend in {"docker", "podman"} and container_argv
            else list(argv)
        )
        command = (
            subprocess.list2cmdline(effective_argv)
            if os.name == "nt"
            else shlex.join(effective_argv)
        )
        host_command = (
            subprocess.list2cmdline(argv)
            if os.name == "nt"
            else shlex.join(argv)
        )
        wrapped = self._wrap_command(
            command,
            capability,
            cwd=cwd,
            host_command=host_command,
        )
        spawn_kwargs = {
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
            "cwd": str(cwd) if cwd else None,
            "env": self._build_env(),
        }
        if isinstance(wrapped, list):
            process = await spawn_exec(*wrapped, **spawn_kwargs)
        else:
            process = await spawn_shell(wrapped, **spawn_kwargs)
        await self._await_sandbox_ready(process)
        return process

    async def spawn_shell_interactive(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        stdin: Any = asyncio.subprocess.PIPE,
        stdout: Any = asyncio.subprocess.PIPE,
        stderr: Any = asyncio.subprocess.PIPE,
    ) -> asyncio.subprocess.Process:
        """Start a long-lived shell command behind the declared sandbox."""
        if not str(command or "").strip():
            raise ValueError("Sandbox shell command must not be empty")
        wrapped, _ = self.prepare_command(command, cwd=cwd, host_command=command)
        spawn_kwargs = {
            "stdin": stdin,
            "stdout": stdout,
            "stderr": stderr,
            "cwd": str(cwd) if cwd else None,
            "env": self._build_env(),
        }
        if isinstance(wrapped, list):
            process = await spawn_exec(*wrapped, **spawn_kwargs)
        else:
            process = await spawn_shell(wrapped, **spawn_kwargs)
        await self._await_sandbox_ready(process)
        return process

    async def terminate(self, process: asyncio.subprocess.Process) -> bool:
        """Terminate an owned interactive process and release sandbox state.

        Returns whether the process tree's exit was observed.
        """
        return await self._kill_tree(process)

    def map_path_to_sandbox(self, path: str | Path) -> str:
        """Map a workspace path to the path visible to the selected backend."""
        resolved = Path(path).expanduser().resolve()
        workspace = self._policy.workspace_root
        capability = self.capability(cwd=workspace)
        if capability.backend not in {"docker", "podman"}:
            return str(resolved)
        if workspace is None:
            raise SandboxUnavailableError("Container sandbox requires a workspace root")
        try:
            relative = resolved.relative_to(workspace.expanduser().resolve())
        except ValueError as exc:
            raise SandboxUnavailableError("Path is outside the sandbox workspace") from exc
        return str(PurePosixPath("/workspace", *relative.parts))

    def map_path_from_sandbox(self, path: str) -> str:
        """Map a backend-visible workspace path back to its host location."""
        workspace = self._policy.workspace_root
        capability = self.capability(cwd=workspace)
        if capability.backend not in {"docker", "podman"}:
            return path
        if workspace is None:
            return path
        candidate = PurePosixPath(path.replace("\\", "/"))
        try:
            relative = candidate.relative_to(PurePosixPath("/workspace"))
        except ValueError:
            return path
        return str(workspace.expanduser().resolve().joinpath(*relative.parts))

    async def run(
        self,
        command: str,
        *,
        cwd: str | Path | None = None,
        stdin_data: bytes | None = None,
        keep_stdin_open: bool = False,
        cancel_event: asyncio.Event | None = None,
        stream_callback: Callable[..., Awaitable[None]] | None = None,
        host_command: str = "",
        process_started_callback: Callable[[int], Awaitable[None] | None] | None = None,
        process_ready_callback: Callable[
            [asyncio.subprocess.Process], Awaitable[None] | None
        ]
        | None = None,
        preserve_full_output: bool = False,
    ) -> SandboxResult:
        env = self._build_env()
        try:
            wrapped_command, capability = self.prepare_command(
                command,
                cwd=cwd,
                host_command=host_command,
            )
        except SandboxUnavailableError as exc:
            return SandboxResult(
                stdout="",
                stderr=f"Sandbox unavailable: {exc}",
                exit_code=126,
                sandbox_unavailable=True,
            )
        proc: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[int] | None = None
        stderr_task: asyncio.Task[int] | None = None
        stdin_task: asyncio.Task[None] | None = None
        cancel_task: asyncio.Task[None] | None = None
        completion_task: asyncio.Future[tuple[None, int, int, int]] | None = None
        stdout_capture = _OutputCapture(
            preserve_full_output=preserve_full_output,
            prefix="minicode-stdout",
        )
        stderr_capture = _OutputCapture(
            preserve_full_output=preserve_full_output,
            prefix="minicode-stderr",
        )
        stdout_total = 0
        stderr_total = 0
        stream_totals = {"stdout": 0, "stderr": 0}
        cancel_tree_reaped = True

        try:
            spawn_kwargs = {
                "stdin": (
                    asyncio.subprocess.PIPE
                    if stdin_data is not None or keep_stdin_open
                    else None
                ),
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(cwd) if cwd else None,
                "env": env,
            }
            if isinstance(wrapped_command, list):
                proc = await spawn_exec(*wrapped_command, **spawn_kwargs)
            else:
                proc = await spawn_shell(wrapped_command, **spawn_kwargs)
            await self._await_sandbox_ready(proc)
            if process_ready_callback is not None:
                try:
                    ready_result = process_ready_callback(proc)
                    if asyncio.iscoroutine(ready_result):
                        await ready_result
                except Exception:
                    await self._kill_tree(proc)
                    raise
            if process_started_callback is not None:
                try:
                    started_result = process_started_callback(proc.pid)
                    if asyncio.iscoroutine(started_result):
                        await started_result
                except Exception:
                    # A started process without a durable owner cannot be
                    # recovered or safely cancelled after restart. Tear down
                    # the exact process tree before exposing the failure.
                    await self._kill_tree(proc)
                    raise

            if cancel_event:
                async def _wait_cancel() -> None:
                    nonlocal cancel_tree_reaped
                    await cancel_event.wait()
                    cancel_tree_reaped = await self._kill_tree(proc)
                cancel_task = asyncio.create_task(_wait_cancel())

            async def _forward_stream(piece: str, stream_name: str) -> None:
                if not stream_callback or not piece:
                    return
                try:
                    await stream_callback(piece, stream_name)
                except TypeError:
                    try:
                        await stream_callback(piece)
                    except Exception:
                        pass
                except Exception:
                    pass

            async def _read_stream(
                stream: Any, capture: _OutputCapture, *, stream_name: str
            ) -> int:
                # Accumulate raw bytes and cap on byte length; the final decode
                # handles UTF-8/native fallback. For live forwarding we use an
                # incremental UTF-8 decoder so multi-byte characters split across
                # read() boundaries are not corrupted mid-stream.
                total = 0
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                while stream is not None:
                    chunk = await stream.read(4096)
                    if not chunk:
                        break
                    capture.append(chunk)
                    total += len(chunk)
                    stream_totals[stream_name] = total
                    if stream_callback:
                        piece = decoder.decode(chunk)
                        await _forward_stream(piece, stream_name)
                if stream_callback:
                    tail = decoder.decode(b"", final=True)
                    await _forward_stream(tail, stream_name)
                return total

            async def _write_stdin() -> None:
                if proc is None or proc.stdin is None:
                    return
                if stdin_data is None and keep_stdin_open:
                    return
                try:
                    if stdin_data:
                        proc.stdin.write(stdin_data)
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    if not keep_stdin_open:
                        proc.stdin.close()

            try:
                stdin_task = asyncio.create_task(_write_stdin())
                stdout_task = asyncio.create_task(
                    _read_stream(proc.stdout, stdout_capture, stream_name="stdout")
                )
                stderr_task = asyncio.create_task(
                    _read_stream(proc.stderr, stderr_capture, stream_name="stderr")
                )
                completion_task = asyncio.gather(
                    stdin_task,
                    stdout_task,
                    stderr_task,
                    proc.wait(),
                )
                if self._policy.timeout is not None and self._policy.timeout > 0:
                    _, stdout_total, stderr_total, _ = await asyncio.wait_for(
                        asyncio.shield(completion_task),
                        timeout=self._policy.timeout,
                    )
                else:
                    _, stdout_total, stderr_total, _ = await asyncio.shield(completion_task)
            except asyncio.TimeoutError:
                tree_reaped = await self._kill_tree(proc)
                try:
                    _, stdout_total, stderr_total, _ = await asyncio.wait_for(
                        asyncio.shield(completion_task),
                        timeout=3.0,
                    )
                except (asyncio.TimeoutError, ProcessLookupError):
                    if completion_task is not None and not completion_task.done():
                        completion_task.cancel()
                        await asyncio.gather(completion_task, return_exceptions=True)
                stdout_capture.finish()
                stderr_capture.finish()
                return SandboxResult(
                    stdout=stdout_capture.snapshot(),
                    stderr=stderr_capture.snapshot(),
                    exit_code=-1,
                    timed_out=True,
                    stdout_path=stdout_capture.path,
                    stderr_path=stderr_capture.path,
                    stdout_total_bytes=stream_totals["stdout"],
                    stderr_total_bytes=stream_totals["stderr"],
                    cleanup_pending=not tree_reaped,
                    cleanup_reason=(
                        "" if tree_reaped else "process_tree_survived_timeout_kill"
                    ),
                )

            stdout_capture.finish()
            stderr_capture.finish()
            if cancel_event and cancel_event.is_set():
                return SandboxResult(
                    stdout=stdout_capture.snapshot(),
                    stderr=stderr_capture.snapshot(),
                    exit_code=-1,
                    cancelled=True,
                    stdout_path=stdout_capture.path,
                    stderr_path=stderr_capture.path,
                    stdout_total_bytes=stdout_total,
                    stderr_total_bytes=stderr_total,
                    cleanup_pending=not cancel_tree_reaped,
                    cleanup_reason=(
                        ""
                        if cancel_tree_reaped
                        else "process_tree_survived_cancellation_kill"
                    ),
                )

            stderr_text = stderr_capture.snapshot()

            return SandboxResult(
                stdout=stdout_capture.snapshot(),
                stderr=stderr_text,
                exit_code=proc.returncode or 0,
                stdout_path=stdout_capture.path,
                stderr_path=stderr_capture.path,
                stdout_total_bytes=stdout_total,
                stderr_total_bytes=stderr_total,
            )

        except asyncio.CancelledError:
            # Cancellation must terminate the whole process tree and drain
            # reader tasks before the coroutine leaves; otherwise Windows
            # Proactor transports can survive the agent turn and leak output.
            tree_reaped = True
            if proc is not None:
                tree_reaped = await asyncio.shield(self._kill_tree(proc))
            if completion_task is not None and not completion_task.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(completion_task),
                        timeout=3.0,
                    )
                except (asyncio.TimeoutError, ProcessLookupError):
                    if not completion_task.done():
                        completion_task.cancel()
                        await asyncio.gather(completion_task, return_exceptions=True)
            stdout_capture.finish()
            stderr_capture.finish()
            return SandboxResult(
                stdout=stdout_capture.snapshot(),
                stderr=stderr_capture.snapshot(),
                exit_code=-1,
                cancelled=True,
                stdout_path=stdout_capture.path,
                stderr_path=stderr_capture.path,
                stdout_total_bytes=stream_totals["stdout"],
                stderr_total_bytes=stream_totals["stderr"],
                cleanup_pending=not tree_reaped,
                cleanup_reason=(
                    "" if tree_reaped else "process_tree_survived_cancellation_kill"
                ),
            )
        except FileNotFoundError:
            return SandboxResult(stdout="", stderr=f"Command not found: {command}", exit_code=127)
        except SandboxUnavailableError as exc:
            return SandboxResult(
                stdout="",
                stderr=f"Sandbox unavailable: {exc}",
                exit_code=126,
                sandbox_unavailable=True,
            )
        except OSError as exc:
            return SandboxResult(stdout="", stderr=str(exc), exit_code=1)
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
            if completion_task is not None and not completion_task.done():
                completion_task.cancel()
            pending = [
                task
                for task in (cancel_task, completion_task)
                if task is not None
            ]
            if pending:
                await asyncio.gather(
                    *pending,
                    return_exceptions=True,
                )
            stdout_capture.finish()
            stderr_capture.finish()
            # A cancelled Proactor StreamReader may retain its pipe transport
            # even after the process exits. Close that private transport only
            # as a final Windows cleanup fallback; normal EOF/drain paths above
            # have already completed first.
            for stream in (
                getattr(proc, "stdout", None) if proc is not None else None,
                getattr(proc, "stderr", None) if proc is not None else None,
            ):
                transport = getattr(stream, "_transport", None)
                if transport is not None:
                    with suppress(Exception):
                        transport.close()
            await self._cleanup_container()
            self._cleanup_sandbox_setup_state()

    def _build_env(self) -> dict[str, str]:
        # Per-launch command overrides are applied after the configured policy,
        # matching Codex command/exec. The helper also makes runtime-owned
        # identity variables non-restorable.
        env = shell_subprocess_env(
            self._policy.shell_environment_policy,
            self._policy.env_overrides,
        )
        # Nudge child processes toward UTF-8 so their output decodes cleanly.
        # PYTHONUTF8/PYTHONIOENCODING cover Python children; PYTHONUNBUFFERED
        # keeps streamed output prompt. Native Windows tools that ignore these
        # are handled by the OEM-codepage fallback in _decode_command_bytes.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        return env

    def _wrap_command(
        self,
        command: str,
        capability: SandboxCapability,
        *,
        cwd: str | Path | None = None,
        host_command: str = "",
    ) -> str | list[str]:
        resolved = self._policy.resolve(cwd=cwd)
        if resolved.enforcement in {
            SandboxEnforcement.DISABLED,
            SandboxEnforcement.EXTERNAL,
        }:
            return host_command or command

        if capability.backend == "bubblewrap":
            ready_path = self._prepare_synthetic_mount_targets(resolved)
            return _bubblewrap_command(
                command,
                resolved,
                ready_path=ready_path,
                mount_path_overrides=self._synthetic_mount_overrides,
            )

        if capability.backend == "seatbelt":
            profile = _seatbelt_profile(resolved)
            return f"sandbox-exec -p {_shell_quote(profile)} -- sh -c {_shell_quote(command)}"

        if capability.backend in {"docker", "podman"}:
            self._prepare_synthetic_mount_targets(resolved)
            return self._container_command(
                command,
                capability.backend,
                cwd=cwd,
                resolved=resolved,
            )

        raise SandboxUnavailableError(
            capability.reason or f"Unsupported sandbox backend: {capability.backend}"
        )

    def _prepare_synthetic_mount_targets(
        self,
        resolved: ResolvedSandboxPolicy,
    ) -> Path | None:
        targets: dict[str, tuple[Path, bool]] = {}
        mount_overrides: dict[str, Path] = {}
        protected_names = {
            name.casefold()
            for root in resolved.writable_roots
            for name in root.protected_metadata_names
        }
        for path, access in _resolved_path_events(resolved):
            if access is FileSystemAccessMode.WRITE:
                continue
            writable_symlink = _first_writable_symlink_component(path, resolved)
            if writable_symlink is not None:
                raise SandboxUnavailableError(
                    f"Cannot enforce sandbox protection for {path} because it crosses "
                    f"writable symlink {writable_symlink}"
                )
            if path.exists():
                continue
            missing = _first_missing_component(path)
            if missing is None or not _policy_path_is_writable(resolved, missing.parent):
                continue
            mount_overrides[os.path.normcase(str(path.expanduser().absolute()))] = missing
            is_directory = (
                missing.name.casefold() in protected_names
                and _protected_metadata_target_is_directory(missing.name)
            )
            targets[os.path.normcase(str(missing))] = (missing, is_directory)
        for writable in resolved.writable_roots:
            for name in writable.protected_metadata_names:
                protected = writable.root / name
                if protected.exists() or not _policy_path_is_writable(resolved, writable.root):
                    continue
                targets.setdefault(
                    os.path.normcase(str(protected)),
                    (protected, _protected_metadata_target_is_directory(name)),
                )

        created: list[_SyntheticMountTarget] = []
        try:
            for path, is_directory in targets.values():
                if path.exists():
                    continue
                if not path.parent.is_dir():
                    raise SandboxUnavailableError(
                        f"Sandbox cannot protect missing path because its parent is unavailable: {path}"
                    )
                try:
                    if is_directory:
                        path.mkdir()
                    else:
                        path.touch(exist_ok=False)
                except FileExistsError:
                    continue
                stat = path.stat(follow_symlinks=False)
                created.append(
                    _SyntheticMountTarget(
                        path=path,
                        is_directory=is_directory,
                        device=stat.st_dev,
                        inode=stat.st_ino,
                    )
                )
        except Exception:
            for target in reversed(created):
                target.remove_if_owned()
            raise

        self._synthetic_mount_overrides = mount_overrides
        if not created:
            return None
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix="minicode-sandbox-ready-",
            delete=False,
        )
        handle.close()
        self._synthetic_mount_targets = created
        self._sandbox_ready_file = Path(handle.name)
        return self._sandbox_ready_file

    async def _await_sandbox_ready(self, process: asyncio.subprocess.Process) -> None:
        ready_file = self._sandbox_ready_file
        if ready_file is None:
            return
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                if ready_file.read_text(encoding="utf-8") == "ready":
                    self._cleanup_sandbox_setup_state()
                    return
            except OSError:
                pass
            if process.returncode is not None:
                break
            await asyncio.sleep(0.01)
        self._cleanup_sandbox_setup_state()
        if process.returncode is None:
            await self._kill_tree(process)
        raise SandboxUnavailableError(
            "Sandbox namespace did not confirm protected-path setup before command execution"
        )

    def _cleanup_sandbox_setup_state(self) -> None:
        targets = self._synthetic_mount_targets
        self._synthetic_mount_overrides = {}
        ready_file = self._sandbox_ready_file
        self._synthetic_mount_targets = []
        self._sandbox_ready_file = None
        for target in reversed(targets):
            target.remove_if_owned()
        if ready_file is not None:
            with suppress(OSError):
                ready_file.unlink()

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> bool:
        # The sandbox owns container cleanup, while the host child still uses
        # the shared process-group lifecycle used by every other execution path.
        reaped = True
        if proc.returncode is None:
            reaped = await terminate_process_tree(proc)
        await self._cleanup_container(force=True)
        self._cleanup_sandbox_setup_state()
        return reaped

    def _container_command(
        self,
        command: str,
        engine: str,
        *,
        cwd: str | Path | None,
        resolved: ResolvedSandboxPolicy,
    ) -> list[str]:
        _runtime, image, reason = _container_runtime()
        if not image:
            raise SandboxUnavailableError(reason or "MiniCode sandbox image is not configured")
        writable_roots = tuple(root.root for root in resolved.writable_roots)
        readable_roots = tuple(resolved.readable_roots)
        workspace_root = (
            self._policy.workspace_root.expanduser().resolve()
            if self._policy.workspace_root is not None
            else writable_roots[0]
            if writable_roots
            else None
        )
        if workspace_root is None:
            raise SandboxUnavailableError("Container sandbox requires a workspace root")
        effective_cwd = Path(cwd).expanduser().resolve() if cwd else workspace_root
        try:
            relative_cwd = effective_cwd.relative_to(workspace_root)
        except ValueError as exc:
            raise SandboxUnavailableError(
                f"Command cwd must stay inside the sandbox workspace: {workspace_root}"
            ) from exc

        container_token = uuid4().hex
        container_name = f"minicode-sandbox-{container_token}"
        cidfile = Path(tempfile.gettempdir()) / f"{container_name}.cid"
        self._container_engine = engine
        self._container_cidfile = cidfile
        self._container_name = container_name
        container_cwd = "/workspace"
        if relative_cwd.parts:
            container_cwd += "/" + "/".join(relative_cwd.parts)

        workspace_is_writable = any(root == workspace_root for root in writable_roots)
        # Commands originate from the Windows host and commonly contain
        # absolute workspace paths (for example, a tool-generated `touch
        # C:\\repo\\file`). A Linux container only knows its bind-mount
        # targets, so translate known policy paths before invoking pwsh/sh.
        # This is deliberately limited to declared roots; arbitrary host paths
        # stay unmapped and therefore remain inaccessible inside the sandbox.
        path_mappings: list[tuple[str, str]] = [
            (str(workspace_root), "/workspace"),
            (str(workspace_root).replace("\\", "/"), "/workspace"),
        ]
        host_filesystem_roots: tuple[tuple[Path, str], ...] = ()
        if resolved.root_read_baseline and sys.platform == "win32":
            host_filesystem_roots = tuple(
                (root, f"/host-roots/{root.drive[:1].upper()}")
                for root in _windows_filesystem_roots()
            )
            for host_root, target in host_filesystem_roots:
                path_mappings.extend(
                    (
                        (str(host_root), f"{target}/"),
                        (str(host_root).replace("\\", "/"), f"{target}/"),
                    )
                )
        writable_index = 1
        for root in writable_roots:
            if root == workspace_root:
                continue
            try:
                relative_root = root.relative_to(workspace_root)
            except ValueError:
                target = f"/writable/{writable_index}"
                writable_index += 1
            else:
                target = "/workspace"
                if relative_root.parts:
                    target += "/" + "/".join(relative_root.parts)
            path_mappings.extend(
                (
                    (str(root), target),
                    (str(root).replace("\\", "/"), target),
                )
            )
        for index, root in enumerate(readable_roots):
            readable = root.expanduser().resolve()
            target, _needs_mount = _container_readable_target(
                readable,
                index,
                resolved,
                workspace_root,
            )
            path_mappings.extend(
                (
                    (str(readable), target),
                    (str(readable).replace("\\", "/"), target),
                )
            )
        container_command = command
        for host_path, container_path in sorted(
            set(path_mappings), key=lambda item: len(item[0]), reverse=True
        ):
            if host_path:
                container_command = container_command.replace(host_path, container_path)
        ready_file = self._sandbox_ready_file
        if ready_file is not None:
            if sys.platform == "win32":
                container_command = (
                    "Set-Content -LiteralPath '/minicode-control-ready' "
                    "-NoNewline -Value 'ready'; "
                    f"{container_command}"
                )
            else:
                container_command = (
                    "printf ready > /minicode-control-ready; "
                    f"{container_command}"
                )
        args = [
            engine,
            "run",
            "--rm",
            "--init",
            "--pull=never",
            f"--name={container_name}",
            f"--cidfile={cidfile}",
            "--label=com.minicode.sandbox=true",
            f"--label=com.minicode.owner_pid={os.getpid()}",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--pids-limit=512",
            (
                "--tmpfs=/tmp:rw,nosuid,nodev,size=512m"
                if _policy_path_is_writable(resolved, Path(tempfile.gettempdir()))
                else "--tmpfs=/tmp:ro,nosuid,nodev,mode=0555,size=4k"
            ),
            f"--volume={workspace_root}:/workspace:{'rw' if workspace_is_writable else 'ro'}",
            f"--workdir={container_cwd}",
            "--env=HOME=/tmp",
            "--env=PYTHONUTF8=1",
            "--env=PYTHONUNBUFFERED=1",
        ]
        for host_root, target in host_filesystem_roots:
            args.append(f"--volume={host_root}:{target}:ro")
        if ready_file is not None:
            args.append(f"--volume={ready_file}:/minicode-control-ready:rw")
        if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            args.append(f"--user={os.getuid()}:{os.getgid()}")
        if not resolved.allow_network:
            args.append("--network=none")
        writable_index = 1
        for root in writable_roots:
            # The workspace mount above already carries rw when the workspace
            # itself is writable. Adding the same target a second time makes
            # Docker reject the container before the command starts.
            if root == workspace_root:
                continue
            try:
                relative_root = root.relative_to(workspace_root)
            except ValueError:
                target = f"/writable/{writable_index}"
                writable_index += 1
            else:
                target = "/workspace"
                if relative_root.parts:
                    target += "/" + "/".join(relative_root.parts)
            args.append(f"--volume={root}:{target}:rw")
        for index, root in enumerate(readable_roots):
            readable = root.expanduser().resolve()
            target, needs_mount = _container_readable_target(
                readable,
                index,
                resolved,
                workspace_root,
            )
            if needs_mount:
                args.append(f"--volume={readable}:{target}:ro")
        args.extend(
            _container_policy_masks(
                resolved,
                workspace_root,
                mount_path_overrides=self._synthetic_mount_overrides,
            )
        )
        for key, value in self._policy.env_overrides.items():
            if key:
                args.append(f"--env={key}={value}")
        if sys.platform == "win32":
            args.extend(
                (
                    image,
                    "pwsh",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    container_command,
                )
            )
        else:
            args.extend((image, "/bin/sh", "-lc", container_command))
        # The container CLI is an executable with a structured argv contract.
        # Keeping that structure makes spawn_exec launch it directly, so the
        # host shell never interprets command text, mount paths, or env values.
        # The final pwsh/sh argument is still interpreted inside the container.
        return args

    async def _cleanup_container(self, *, force: bool = False) -> None:
        cidfile = self._container_cidfile
        engine = self._container_engine
        container_name = self._container_name
        self._container_cidfile = None
        self._container_engine = ""
        self._container_name = ""
        if cidfile is None and not container_name:
            return
        container_id = ""
        if cidfile is not None:
            with suppress(OSError):
                container_id = cidfile.read_text(encoding="utf-8").strip()
        container_ref = container_name or container_id
        if force and engine and container_ref:
            with suppress(Exception):
                cleanup = await spawn_exec(
                    engine,
                    "rm",
                    "--force",
                    container_ref,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await communicate(cleanup, timeout=5.0)
        if cidfile is not None:
            with suppress(OSError):
                cidfile.unlink()


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _container_policy_masks(
    resolved: ResolvedSandboxPolicy,
    workspace_root: Path,
    *,
    mount_path_overrides: dict[str, Path] | None = None,
) -> list[str]:
    """Translate layered filesystem entries into nested container mounts.

    Docker and Podman apply nested mounts independently of their parent mount,
    so the same broad-to-specific order used by Codex's bubblewrap backend can
    reopen a narrow read/write path after masking a broader denied directory.
    """

    args: list[str] = []
    events = _resolved_path_events(resolved)
    for path, access in events:
        ancestors = [
            ancestor_access
            for ancestor, ancestor_access in events
            if path != ancestor and path.is_relative_to(ancestor)
        ]
        if access is FileSystemAccessMode.WRITE and not any(
            ancestor_access is not FileSystemAccessMode.WRITE
            for ancestor_access in ancestors
        ):
            continue
        if access is FileSystemAccessMode.READ and not any(
            ancestor_access is FileSystemAccessMode.WRITE
            for ancestor_access in ancestors
        ):
            continue
        source = _mount_event_path(path, access, mount_path_overrides)
        targets = _container_targets_for_path(source, resolved, workspace_root)
        # Paths outside every declared bind are already absent from the
        # container namespace; adding a host mount would broaden access.
        for target in targets:
            if access is FileSystemAccessMode.WRITE:
                if source.exists():
                    args.append(f"--volume={source}:{target}:rw")
                continue
            if access is FileSystemAccessMode.READ:
                if source.is_dir():
                    args.append(f"--volume={source}:{target}:ro")
                elif source.exists():
                    args.append(f"--volume={source}:{target}:ro")
                continue
            if source.is_file():
                args.append(f"--volume=/dev/null:{target}:ro")
            else:
                args.append(f"--tmpfs={target}:ro,nosuid,nodev,noexec,mode=000,size=4k")
    return args


def _container_targets_for_path(
    path: Path,
    resolved: ResolvedSandboxPolicy,
    workspace_root: Path,
) -> tuple[str, ...]:
    candidate = path.expanduser().absolute()
    targets: list[str] = []
    relative = _relative_path(candidate, workspace_root)
    if relative is not None:
        targets.append(_container_join("/workspace", relative))

    writable_index = 1
    for writable in resolved.writable_roots:
        root = writable.root.expanduser().absolute()
        if root == workspace_root or _relative_path(root, workspace_root) is not None:
            continue
        relative = _relative_path(candidate, root)
        if relative is not None:
            targets.append(_container_join(f"/writable/{writable_index}", relative))
        writable_index += 1

    for index, root in enumerate(resolved.readable_roots):
        readable = root.expanduser().absolute()
        relative = _relative_path(candidate, readable)
        if relative is not None:
            readable_target, _needs_mount = _container_readable_target(
                readable,
                index,
                resolved,
                workspace_root,
            )
            targets.append(_container_join(readable_target, relative))
    if resolved.root_read_baseline and sys.platform == "win32":
        for host_root in _windows_filesystem_roots():
            relative = _relative_path(candidate, host_root)
            if relative is not None:
                targets.append(
                    _container_join(
                        f"/host-roots/{host_root.drive[:1].upper()}",
                        relative,
                    )
                )
    return tuple(dict.fromkeys(targets))


def _container_readable_target(
    readable: Path,
    index: int,
    resolved: ResolvedSandboxPolicy,
    workspace_root: Path,
) -> tuple[str, bool]:
    relative = _relative_path(readable, workspace_root)
    if relative is not None:
        return _container_join("/workspace", relative), False

    writable_index = 1
    for writable in resolved.writable_roots:
        root = writable.root.expanduser().absolute()
        if root == workspace_root or _relative_path(root, workspace_root) is not None:
            continue
        relative = _relative_path(readable, root)
        if relative is not None:
            return _container_join(f"/writable/{writable_index}", relative), False
        writable_index += 1
    return f"/readable/{index}", True


def _windows_filesystem_roots() -> tuple[Path, ...]:
    if sys.platform != "win32":
        return ()
    try:
        import ctypes  # noqa: PLC0415

        mask = int(ctypes.windll.kernel32.GetLogicalDrives())
    except Exception:
        mask = 0
    roots = [
        Path(f"{chr(ord('A') + index)}:\\")
        for index in range(26)
        if mask & (1 << index)
    ]
    if not roots:
        anchor = Path.cwd().anchor
        if anchor:
            roots.append(Path(anchor))
    return tuple(root.resolve() for root in roots if root.exists())


def _relative_path(path: Path, root: Path) -> Path | None:
    try:
        return path.relative_to(root)
    except ValueError:
        return None


def _container_join(root: str, relative: Path) -> str:
    if not relative.parts:
        return root
    return f"{root}/{'/'.join(relative.parts)}"


def _bubblewrap_command(
    command: str,
    policy: ResolvedSandboxPolicy | SandboxPolicy,
    *,
    ready_path: Path | None = None,
    mount_path_overrides: dict[str, Path] | None = None,
) -> str:
    """Build Codex-style layered mounts for a canonical filesystem policy."""

    resolved = _resolved_policy(policy)
    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-user",
        "--unshare-uts",
        "--unshare-ipc",
    ]
    if not resolved.allow_network:
        args.append("--unshare-net")

    if resolved.root_read_baseline:
        args.extend(("--ro-bind", "/", "/"))
    else:
        args.extend(("--tmpfs", "/"))
        if resolved.include_platform_defaults:
            for raw_path in (
                "/usr",
                "/bin",
                "/sbin",
                "/lib",
                "/lib64",
                "/etc",
                "/nix/store",
                "/run/current-system/sw",
            ):
                if Path(raw_path).exists():
                    args.extend(("--ro-bind", raw_path, raw_path))

    # Recreate a minimal runtime after the root baseline. Later, increasingly
    # specific path mounts implement Codex's "most specific entry wins" rule.
    args.extend(("--proc", "/proc", "--dev", "/dev"))
    denied_roots = [
        path.expanduser().absolute()
        for path, access in _resolved_path_events(resolved)
        if access is FileSystemAccessMode.DENY
    ]
    for path, access in _resolved_path_events(resolved):
        _append_bwrap_path_event(
            args,
            _mount_event_path(path, access, mount_path_overrides),
            access,
            denied_roots=denied_roots,
        )

    if ready_path is not None:
        args.extend(("--bind", str(ready_path), "/dev/minicode-control-ready"))
        command = "printf ready > /dev/minicode-control-ready; " + command

    args.extend(("--setenv", "HOME", "/tmp", "--", "sh", "-c", command))
    return " ".join(_shell_quote(part) for part in args)


def _seatbelt_profile(
    policy: ResolvedSandboxPolicy | SandboxPolicy,
) -> str:
    resolved = _resolved_policy(policy)

    def literal(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    def filters_for(path: Path, *, directory: bool | None = None) -> str:
        escaped = literal(path)
        if directory is False or (directory is None and path.is_file()):
            return f'(literal "{escaped}")'
        # Seatbelt's `subpath` excludes the directory inode itself. Pair it
        # with `literal` so rename/unlink/replace cannot bypass a directory
        # carveout, matching Codex's macOS profile generation.
        return f'(literal "{escaped}") (subpath "{escaped}")'

    rules = ["(version 1)", "(deny default)"]
    rules.append("(allow process-exec)")
    rules.append("(allow process-fork)")
    rules.append("(allow sysctl-read)")
    rules.append("(allow mach-lookup)")
    if resolved.root_read_baseline:
        rules.append("(allow file-read*)")
    elif resolved.include_platform_defaults:
        for raw_path in ("/usr", "/bin", "/sbin", "/System", "/Library", "/private/tmp", "/dev"):
            path = Path(raw_path)
            if path.exists():
                rules.append(f"(allow file-read* {filters_for(path)})")
    if resolved.root_write_baseline:
        rules.append("(allow file-write*)")
    for root in resolved.readable_roots:
        rules.append(f"(allow file-read* {filters_for(root)})")
    for root in resolved.writable_roots:
        rules.append(f"(allow file-read* file-write* {filters_for(root.root)})")
        for readonly in root.read_only_subpaths:
            rules.append(f"(deny file-write* {filters_for(readonly)})")
        for name in root.protected_metadata_names:
            protected = root.root / name
            if protected not in root.read_only_subpaths:
                rules.append(
                    f"(deny file-write* {filters_for(protected, directory=_protected_metadata_target_is_directory(name))})"
                )
    for root in resolved.unreadable_roots:
        rules.append(
            f"(deny file-read* file-read-metadata file-write* {filters_for(root)})"
        )
    for pattern in resolved.unreadable_globs:
        patterns = {pattern}
        canonical = _canonicalized_glob_static_prefix(pattern)
        if canonical:
            patterns.add(canonical)
        for candidate in sorted(patterns):
            regex = _seatbelt_regex_for_unreadable_glob(candidate).replace('"', '\\"')
            rules.append(f'(deny file-read* (regex #"{regex}"))')
            rules.append(f'(deny file-write-unlink (regex #"{regex}"))')
    if resolved.allow_network:
        rules.append("(allow network*)")
    return "\n".join(rules)


def _canonicalized_glob_static_prefix(pattern: str) -> str | None:
    wildcard_at = min(
        (pattern.find(token) for token in ("*", "?", "[", "]") if token in pattern),
        default=-1,
    )
    if wildcard_at < 0:
        return None
    static_prefix = pattern[:wildcard_at]
    prefix_end = len(static_prefix) - 1 if static_prefix.endswith("/") else static_prefix.rfind("/")
    if prefix_end <= 0:
        return None
    root = Path(pattern[:prefix_end]).resolve(strict=False)
    normalized = f"{root}{pattern[prefix_end:]}"
    return normalized if normalized != pattern else None


def _seatbelt_regex_for_unreadable_glob(pattern: str) -> str:
    regex = "^"
    saw_glob = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            saw_glob = True
            if index + 1 < len(pattern) and pattern[index + 1] == "*":
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    regex += "(.*/)?"
                    index += 1
                else:
                    regex += ".*"
                continue
            regex += "[^/]*"
        elif char == "?":
            saw_glob = True
            regex += "[^/]"
        elif char == "[":
            saw_glob = True
            class_end = pattern.find("]", index + 1)
            if class_end < 0:
                regex += r"\["
            else:
                class_content = pattern[index + 1 : class_end]
                regex += "["
                if class_content.startswith("!"):
                    regex += "^"
                    class_content = class_content[1:]
                elif class_content.startswith("^"):
                    regex += r"\^"
                    class_content = class_content[1:]
                regex += class_content.replace("\\", r"\\") + "]"
                index = class_end
        elif char == "]":
            saw_glob = True
            regex += r"\]"
        else:
            regex += re.escape(char)
        index += 1
    if not saw_glob:
        regex += "(/.*)?"
    return regex + "$"


def _resolved_policy(
    policy: ResolvedSandboxPolicy | SandboxPolicy,
) -> ResolvedSandboxPolicy:
    return policy if isinstance(policy, ResolvedSandboxPolicy) else policy.resolve()


def _resolved_path_events(
    resolved: ResolvedSandboxPolicy,
) -> list[tuple[Path, FileSystemAccessMode]]:
    """Layer concrete paths from broad to narrow using Codex precedence."""

    priority = {
        FileSystemAccessMode.READ: 0,
        FileSystemAccessMode.WRITE: 1,
        FileSystemAccessMode.DENY: 2,
    }
    events: dict[str, tuple[Path, FileSystemAccessMode]] = {}

    def add(path: Path, access: FileSystemAccessMode) -> None:
        candidate = path.expanduser().absolute()
        key = os.path.normcase(str(candidate))
        previous = events.get(key)
        if previous is None or priority[access] > priority[previous[1]]:
            events[key] = (candidate, access)

    for path in resolved.readable_roots:
        add(path, FileSystemAccessMode.READ)
    for writable in resolved.writable_roots:
        add(writable.root, FileSystemAccessMode.WRITE)
        for path in writable.read_only_subpaths:
            add(path, FileSystemAccessMode.READ)
        for name in writable.protected_metadata_names:
            add(writable.root / name, FileSystemAccessMode.READ)
    for path in resolved.unreadable_roots:
        add(path, FileSystemAccessMode.DENY)
    # Glob rules remain available to the direct permission checker. At the OS
    # boundary, masking the static prefix is intentionally broader and prevents
    # a post-start file creation from escaping a startup-only glob expansion.
    for pattern in resolved.unreadable_globs:
        for path in _expand_unreadable_glob(pattern, resolved.glob_scan_max_depth):
            add(path, FileSystemAccessMode.DENY)
    return sorted(
        events.values(),
        key=lambda item: (len(item[0].parts), priority[item[1]], os.path.normcase(str(item[0]))),
    )


def _effective_mount_event_path(
    path: Path,
    access: FileSystemAccessMode,
) -> Path:
    candidate = path.expanduser().absolute()
    if access is FileSystemAccessMode.WRITE or candidate.exists():
        return candidate
    return _first_missing_component(candidate) or candidate


def _mount_event_path(
    path: Path,
    access: FileSystemAccessMode,
    overrides: dict[str, Path] | None,
) -> Path:
    candidate = path.expanduser().absolute()
    if overrides:
        override = overrides.get(os.path.normcase(str(candidate)))
        if override is not None:
            return override
    return _effective_mount_event_path(candidate, access)


def _policy_path_is_writable(
    resolved: ResolvedSandboxPolicy,
    path: Path,
) -> bool:
    return resolved.resolve_access(path) is FileSystemAccessMode.WRITE


def _has_filesystem_root_write(resolved: ResolvedSandboxPolicy) -> bool:
    return resolved.root_write_baseline or any(
        writable.root == Path(writable.root.anchor or os.sep)
        for writable in resolved.writable_roots
    )


def _protected_paths_fully_isolated(resolved: ResolvedSandboxPolicy) -> bool:
    for writable in resolved.writable_roots:
        for readonly in writable.read_only_subpaths:
            if resolved.resolve_access(readonly) is FileSystemAccessMode.WRITE:
                return False
    return True


def _protected_metadata_target_is_directory(name: str) -> bool:
    return name.casefold() in {".git", ".minicode"}


def _sandbox_policy_preflight_error(resolved: ResolvedSandboxPolicy) -> str:
    try:
        events = _resolved_path_events(resolved)
    except SandboxUnavailableError as exc:
        return str(exc)
    for path, access in events:
        if access is FileSystemAccessMode.WRITE:
            continue
        writable_symlink = _first_writable_symlink_component(path, resolved)
        if writable_symlink is not None:
            return (
                f"Cannot enforce sandbox protection for {path} because it crosses "
                f"writable symlink {writable_symlink}"
            )
    return ""


def _first_writable_symlink_component(
    path: Path,
    resolved: ResolvedSandboxPolicy,
) -> Path | None:
    candidate = path.expanduser().absolute()
    current = Path(candidate.anchor or os.sep)
    try:
        relative_parts = candidate.relative_to(current).parts
    except ValueError:
        relative_parts = candidate.parts
        current = Path()
    for part in relative_parts:
        current = current / part
        try:
            is_symlink = current.is_symlink()
        except OSError:
            break
        if is_symlink and any(
            current.is_relative_to(writable.root)
            and not any(
                current.is_relative_to(readonly)
                for readonly in writable.read_only_subpaths
            )
            for writable in resolved.writable_roots
        ):
            return current
        if not current.exists() and not is_symlink:
            break
    return None


def _expand_unreadable_glob(pattern: str, max_depth: int | None) -> tuple[Path, ...]:
    if max_depth == 0:
        return ()
    search_root, relative_pattern = _split_unreadable_glob(pattern)
    if not search_root.exists():
        return ()
    if not search_root.is_dir():
        raise SandboxUnavailableError(
            f"Deny-read glob search root is not a directory: {search_root}"
        )

    rg = shutil.which("rg")
    if rg:
        args = [rg, "--files", "--hidden", "--no-ignore", "--null"]
        if max_depth is not None:
            args.extend(("--max-depth", str(max_depth)))
        args.extend(("--glob", relative_pattern, "--", str(search_root)))
        try:
            completed = subprocess.run(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=15.0,
            )
        except OSError as exc:
            if isinstance(exc, FileNotFoundError):
                raw_matches = _walk_unreadable_glob(
                    search_root,
                    relative_pattern,
                    max_depth,
                )
            else:
                raise SandboxUnavailableError(
                    f"Failed to scan deny-read glob {pattern!r}: {exc}"
                ) from exc
        except subprocess.TimeoutExpired as exc:
            raise SandboxUnavailableError(
                f"Deny-read glob scan timed out for {pattern!r}"
            ) from exc
        else:
            if completed.returncode == 1 and not completed.stderr:
                raw_matches = []
            elif completed.returncode != 0:
                detail = _decode_command_bytes(completed.stderr).strip()
                raise SandboxUnavailableError(
                    f"Ripgrep deny-read scan failed for {search_root}: "
                    f"{detail or completed.returncode}"
                )
            else:
                raw_matches = []
                for raw in completed.stdout.split(b"\0"):
                    if not raw:
                        continue
                    decoded = os.fsdecode(raw)
                    candidate = Path(decoded)
                    raw_matches.append(
                        candidate.absolute()
                        if candidate.is_absolute()
                        else (search_root / candidate).absolute()
                    )
    else:
        raw_matches = _walk_unreadable_glob(search_root, relative_pattern, max_depth)

    matches: list[Path] = []
    seen: set[str] = set()
    for match in raw_matches:
        candidates = [match]
        if match.is_symlink():
            try:
                candidates.append(match.resolve(strict=True))
            except OSError as exc:
                raise SandboxUnavailableError(
                    f"Failed to resolve deny-read symlink match {match}: {exc}"
                ) from exc
        for candidate in candidates:
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            matches.append(candidate)
            if len(matches) > 8192:
                raise SandboxUnavailableError(
                    f"Deny-read glob matched more than 8192 paths: {pattern}"
                )
    return tuple(matches)


def _split_unreadable_glob(pattern: str) -> tuple[Path, str]:
    absolute_pattern = str(Path(pattern).expanduser().absolute())
    wildcard_at = min(
        (absolute_pattern.find(token) for token in ("*", "?", "[", "]") if token in absolute_pattern),
        default=-1,
    )
    if wildcard_at < 0:
        raise SandboxUnavailableError(f"Deny-read glob has no wildcard: {pattern}")
    static_prefix = absolute_pattern[:wildcard_at]
    separator = max(static_prefix.rfind("/"), static_prefix.rfind("\\"))
    if separator < 0:
        raise SandboxUnavailableError(
            f"Deny-read glob has no bounded search root: {pattern}"
        )
    search_root_raw = static_prefix[:separator] or Path(absolute_pattern).anchor or os.sep
    search_root = Path(search_root_raw).absolute()
    if search_root == Path(search_root.anchor or os.sep):
        raise SandboxUnavailableError(
            f"Root-level deny-read glob is too broad to enforce safely: {pattern}"
        )
    relative_pattern = absolute_pattern[separator + 1 :].replace("\\", "/")
    if not relative_pattern:
        raise SandboxUnavailableError(f"Deny-read glob is empty below {search_root}")
    return search_root, relative_pattern


def _walk_unreadable_glob(
    search_root: Path,
    pattern: str,
    max_depth: int | None,
) -> list[Path]:
    matches: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise SandboxUnavailableError(
                f"Failed to scan deny-read glob directory {directory}: {exc}"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                is_symlink = entry.is_symlink()
                is_file = entry.is_file(follow_symlinks=False)
                is_directory = entry.is_dir(follow_symlinks=False)
            except OSError as exc:
                raise SandboxUnavailableError(
                    f"Failed to inspect deny-read glob entry {path}: {exc}"
                ) from exc
            relative = path.relative_to(search_root)
            if (is_file or is_symlink) and _component_glob_match(
                pattern.split("/"),
                list(relative.parts),
            ):
                matches.append(path.absolute())
                if len(matches) > 8192:
                    raise SandboxUnavailableError(
                        f"Deny-read glob matched more than 8192 paths below {search_root}"
                    )
            if is_directory and (max_depth is None or depth < max_depth):
                walk(path, depth + 1)

    walk(search_root, 1)
    return matches


def _component_glob_match(pattern: list[str], path: list[str]) -> bool:
    if not pattern:
        return not path
    head = pattern[0]
    if head == "**":
        return _component_glob_match(pattern[1:], path) or bool(
            path and _component_glob_match(pattern, path[1:])
        )
    return bool(
        path
        and fnmatchcase(path[0], head)
        and _component_glob_match(pattern[1:], path[1:])
    )


def _first_missing_component(path: Path) -> Path | None:
    candidate = path.expanduser().absolute()
    missing: Path | None = None
    for part in (candidate, *candidate.parents):
        if part.exists():
            break
        missing = part
    return missing


def _append_bwrap_mount_target_dir_args(
    args: list[str],
    mount_target: Path,
    anchor: Path,
) -> None:
    """Recreate missing mount target parents under a masked ancestor.

    codex bwrap.rs append_mount_target_parent_dir_args: after a denied root is
    frozen with a 000-perms tmpfs, each intermediate directory between the
    masking anchor and the writable descendant is recreated with ``--dir`` so
    the subsequent ``--bind`` has a namespace target to land on.
    """
    mount_target_dir = mount_target if mount_target.is_dir() else mount_target.parent
    intermediate: list[Path] = []
    for part in (mount_target_dir, *mount_target_dir.parents):
        if part == anchor:
            break
        intermediate.append(part)
    for part in reversed(intermediate):
        args.extend(("--dir", str(part)))


def _append_bwrap_path_event(
    args: list[str],
    path: Path,
    access: FileSystemAccessMode,
    *,
    denied_roots: list[Path] | None = None,
) -> None:
    candidate = path.expanduser().absolute()
    if access is FileSystemAccessMode.WRITE:
        if candidate.exists():
            masking_anchor = None
            for denied in denied_roots or ():
                if denied != candidate and candidate.is_relative_to(denied):
                    if masking_anchor is None or denied.is_relative_to(masking_anchor):
                        masking_anchor = denied
            if masking_anchor is not None:
                _append_bwrap_mount_target_dir_args(args, candidate, masking_anchor)
            args.extend(("--bind", str(candidate), str(candidate)))
        return
    if access is FileSystemAccessMode.READ:
        if candidate.exists():
            args.extend(("--ro-bind", str(candidate), str(candidate)))
            return
        missing = _first_missing_component(candidate)
        if missing is not None:
            args.extend(("--perms", "555", "--tmpfs", str(missing), "--remount-ro", str(missing)))
        return

    if not candidate.exists():
        candidate = _first_missing_component(candidate) or candidate
    if candidate.is_file():
        args.extend(("--ro-bind", "/dev/null", str(candidate)))
    else:
        args.extend(("--perms", "000", "--tmpfs", str(candidate), "--remount-ro", str(candidate)))


def _seatbelt_policy_supported(resolved: ResolvedSandboxPolicy) -> bool:
    """Seatbelt deny rules cannot reopen a more-specific nested allow."""

    for denied in resolved.unreadable_roots:
        if any(
            path != denied and path.is_relative_to(denied)
            for path in (
                *resolved.readable_roots,
                *(root.root for root in resolved.writable_roots),
            )
        ):
            return False
    for writable in resolved.writable_roots:
        if any(
            other.root != writable.root and other.root.is_relative_to(readonly)
            for readonly in writable.read_only_subpaths
            for other in resolved.writable_roots
        ):
            return False
    return True
