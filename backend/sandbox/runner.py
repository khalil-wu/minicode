"""Sandbox runner — executes commands under OS-level isolation."""
from __future__ import annotations

import asyncio
import codecs
from contextlib import suppress
import json
import locale
import os
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
from backend.runtime_env import sanitized_subprocess_env
from backend.sandbox.policy import SandboxPolicy
from backend.sandbox.result import SandboxResult
from backend.subprocesses import communicate, spawn_exec, spawn_shell, terminate_process_tree
from backend.tools.base import (
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    truncate_text_tail,
)

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
_codex_windows_sandbox_cache: tuple[float, str, str] | None = None


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
    reason: str = ""


class SandboxUnavailableError(RuntimeError):
    """Raised before process creation when the requested policy is unenforceable."""


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
            self._ensure_file()
        if self._file is not None:
            self._file.flush()
            self._file.close()
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


def _codex_windows_sandbox() -> tuple[str, str]:
    """Resolve the official Codex Windows sandbox executable.

    Packaged desktop builds provide the binary through @openai/codex. Local
    development may use that same dependency, an explicit override, or the
    installed Codex CLI. The short cache avoids filesystem/path probing for
    every command.
    """
    global _codex_windows_sandbox_cache
    if sys.platform != "win32":
        return "", "Codex Windows sandbox is only available on Windows"
    requested = str(os.environ.get("MINICODE_CODEX_SANDBOX_EXE", "") or "").strip()
    now = time.monotonic()
    cached = _codex_windows_sandbox_cache
    if cached is not None and cached[1] == requested and now - cached[0] < 30.0:
        return cached[2], "" if cached[2] else "Official Codex Windows sandbox executable is unavailable"

    candidates: list[str] = []
    if requested:
        candidates.append(requested)
    project_root = Path(__file__).resolve().parents[2]
    vendor_suffix = Path(
        "@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )
    candidates.append(str(project_root / "desktop" / "node_modules" / vendor_suffix))
    app_data = str(os.environ.get("APPDATA", "") or "").strip()
    if app_data:
        npm_modules = Path(app_data) / "npm" / "node_modules"
        candidates.extend((
            str(npm_modules / vendor_suffix),
            str(npm_modules / "@openai" / "codex" / "node_modules" / vendor_suffix),
        ))
    for name in ("codex.exe", "codex"):
        resolved = shutil.which(name)
        if resolved:
            candidates.append(resolved)
    executable = next(
        (str(Path(candidate).expanduser().resolve()) for candidate in candidates if Path(candidate).is_file()),
        "",
    )
    _codex_windows_sandbox_cache = (now, requested, executable)
    if executable:
        return executable, ""
    return "", "Install or package @openai/codex to enable the native Windows sandbox"


def _codex_sandbox_state(policy: SandboxPolicy, cwd: str | Path | None) -> str:
    """Serialize MiniCode's policy into Codex's official sandbox-state wire format."""
    workspace = (
        policy.workspace_root.expanduser().resolve()
        if policy.workspace_root is not None
        else Path(cwd or os.getcwd()).expanduser().resolve()
    )
    effective_cwd = Path(cwd or workspace).expanduser().resolve()
    if sys.platform == "win32":
        workspace = _windows_short_path(workspace)
        effective_cwd = _windows_short_path(effective_cwd)
    entries: list[dict[str, Any]] = [
        {
            # Codex's workspace profile intentionally keeps the host readable
            # while ACL capability SIDs constrain writes to explicit roots.
            "path": {"type": "special", "value": {"kind": "root"}},
            "access": "read",
        },
        {
            "path": {"type": "path", "path": str(workspace)},
            "access": "read",
        },
        {
            "path": {"type": "special", "value": {"kind": "tmpdir"}},
            "access": "write",
        },
    ]
    seen: set[tuple[str, str]] = {(str(workspace).casefold(), "read")}
    for access, roots in (("read", policy.readable_roots), ("write", policy.writable_roots)):
        for root in roots:
            resolved = root.expanduser().resolve()
            if sys.platform == "win32":
                resolved = _windows_short_path(resolved)
            key = (str(resolved).casefold(), access)
            if key in seen:
                continue
            seen.add(key)
            entries.append({
                "path": {"type": "path", "path": str(resolved)},
                "access": access,
                "missing_path_behavior": "skip",
            })
    state = {
        "permissionProfile": {
            "type": "managed",
            "file_system": {"type": "restricted", "entries": entries},
            "network": "enabled" if policy.allow_network else "restricted",
        },
        "codexLinuxSandboxExe": None,
        "sandboxCwd": effective_cwd.as_uri(),
        "useLegacyLandlock": False,
    }
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def _windows_short_path(path: Path) -> Path:
    """Ask Windows for the native 8.3 spelling without implementing path rules."""
    if sys.platform != "win32":
        return path
    import ctypes  # noqa: PLC0415

    kernel32 = ctypes.windll.kernel32
    source = str(path)
    required = kernel32.GetShortPathNameW(source, None, 0)
    if not required:
        return path
    buffer = ctypes.create_unicode_buffer(required + 1)
    length = kernel32.GetShortPathNameW(source, buffer, len(buffer))
    return Path(buffer.value) if length else path


def _codex_cwd_startup_failure(stderr: str, cwd: str | Path | None) -> bool:
    """Recognize Codex's restricted-token failure to enter the requested cwd."""
    if not cwd or not stderr or "UnauthorizedAccessException" not in stderr:
        return False
    try:
        cwd_text = str(_windows_short_path(Path(cwd).expanduser().resolve())).casefold()
    except (OSError, ValueError):
        cwd_text = str(cwd).casefold()
    return cwd_text in stderr.casefold()


def _codex_windows_command(
    executable: str,
    policy: SandboxPolicy,
    command: str,
    *,
    cwd: str | Path | None,
) -> list[str]:
    state = _codex_sandbox_state(policy, cwd)
    command_argv = _windows_command_line_to_argv(command)
    # Codex's restricted Windows token cannot start a separately installed
    # pwsh.exe on all hosts (CreateProcessAsUserW 1312). The encoded command
    # contract is shared with inbox PowerShell, which the official sandbox can
    # launch reliably.
    if Path(command_argv[0]).name.casefold() in {"pwsh", "pwsh.exe"}:
        command_argv[0] = "powershell.exe"
    return [
        executable,
        "sandbox",
        "--sandbox-state-json",
        state,
        *(["--sandbox-state-disable-network"] if not policy.allow_network else []),
        "-c",
        "shell_environment_policy.inherit=all",
        *command_argv,
    ]


def _windows_command_line_to_argv(command: str) -> list[str]:
    """Use the Windows system parser for a command line generated by list2cmdline."""
    if sys.platform != "win32":
        return [command]
    import ctypes  # noqa: PLC0415

    argc = ctypes.c_int()
    shell32 = ctypes.windll.shell32
    shell32.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell32.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv_ptr = shell32.CommandLineToArgvW(command, ctypes.byref(argc))
    if not argv_ptr:
        raise SandboxUnavailableError("Windows could not parse the sandbox command line")
    try:
        argv = [argv_ptr[index] for index in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv_ptr)
    if not argv:
        raise SandboxUnavailableError("Sandbox command must not be empty")
    return argv



class SandboxRunner:
    """Execute shell commands within a SandboxPolicy.

    Isolation strategy by platform:
      - Linux: Bubblewrap filesystem namespace plus optional network namespace
      - macOS: sandbox-exec with a generated Seatbelt profile
      - Windows: Codex's restricted-token/ACL/WFP sandbox, with a container
        fallback when the official helper is unavailable

    Full-access policies still use process groups for reliable tree cleanup,
    but process grouping is never reported as a security boundary.
    """

    def __init__(self, policy: SandboxPolicy) -> None:
        self._policy = policy
        self._container_engine = ""
        self._container_cidfile: Path | None = None
        self._container_name = ""

    def capability(self) -> SandboxCapability:
        """Report the isolation that will actually be enforced.

        A process sandbox must enforce both the declared filesystem boundary
        and, when requested, network isolation. Process groups and command
        string validation are not filesystem sandboxes.
        """
        if self._policy.disable_os_sandbox:
            return SandboxCapability(
                available=True,
                backend="full-access",
                filesystem_isolated=False,
                network_isolated=False,
            )
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            return SandboxCapability(
                available=True,
                backend="seatbelt",
                filesystem_isolated=True,
                network_isolated=not self._policy.allow_network,
            )
        if sys.platform == "linux" and shutil.which("bwrap"):
            return SandboxCapability(
                available=True,
                backend="bubblewrap",
                filesystem_isolated=True,
                network_isolated=not self._policy.allow_network,
            )
        codex_windows_executable, codex_windows_reason = _codex_windows_sandbox()
        container_engine, container_image, container_reason = _container_runtime()
        # Codex's non-elevated Windows restricted-token backend enforces the
        # filesystem profile, but reliable network denial requires its
        # elevated WFP/proxy setup. The standalone `codex sandbox` helper does
        # not prove that setup is active (and direct curl succeeds on affected
        # hosts), so never advertise network isolation from that path. Prefer
        # a container for restricted-network policies and otherwise fail
        # closed, matching Codex's unsupported-policy guard.
        if (
            sys.platform == "win32"
            and codex_windows_executable
            and self._policy.allow_network
        ):
            return SandboxCapability(
                available=True,
                backend="codex-windows-sandbox",
                filesystem_isolated=True,
                network_isolated=False,
            )
        if container_engine and container_image:
            return SandboxCapability(
                available=True,
                backend=container_engine,
                filesystem_isolated=True,
                network_isolated=not self._policy.allow_network,
            )
        if sys.platform == "win32":
            if codex_windows_executable and not self._policy.allow_network:
                codex_windows_reason = (
                    "Codex restricted-token sandbox cannot guarantee network "
                    "isolation without the elevated WFP/proxy backend"
                )
            reason = "; ".join(
                part for part in (codex_windows_reason, container_reason) if part
            ) or "No enforceable Windows sandbox backend is available"
        elif sys.platform == "linux":
            reason = "Bubblewrap (bwrap) is required to enforce the workspace filesystem boundary"
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
        capability = self.capability()
        if not capability.available:
            raise SandboxUnavailableError(capability.reason)
        return self._wrap_command(
            command,
            capability,
            cwd=cwd,
            host_command=host_command,
        ), capability

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
        capability = self.capability()
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
            return await spawn_exec(*wrapped, **spawn_kwargs)
        return await spawn_shell(wrapped, **spawn_kwargs)

    async def terminate(self, process: asyncio.subprocess.Process) -> None:
        """Terminate an owned interactive process and release sandbox state."""
        await self._kill_tree(process)

    def map_path_to_sandbox(self, path: str | Path) -> str:
        """Map a workspace path to the path visible to the selected backend."""
        resolved = Path(path).expanduser().resolve()
        capability = self.capability()
        if capability.backend not in {"docker", "podman"}:
            return str(resolved)
        workspace = self._policy.workspace_root
        if workspace is None:
            raise SandboxUnavailableError("Container sandbox requires a workspace root")
        try:
            relative = resolved.relative_to(workspace.expanduser().resolve())
        except ValueError as exc:
            raise SandboxUnavailableError("Path is outside the sandbox workspace") from exc
        return str(PurePosixPath("/workspace", *relative.parts))

    def map_path_from_sandbox(self, path: str) -> str:
        """Map a backend-visible workspace path back to its host location."""
        capability = self.capability()
        if capability.backend not in {"docker", "podman"}:
            return path
        workspace = self._policy.workspace_root
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
        cancel_event: asyncio.Event | None = None,
        stream_callback: Callable[..., Awaitable[None]] | None = None,
        host_command: str = "",
        process_started_callback: Callable[[int], Awaitable[None] | None] | None = None,
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
        cancel_task: asyncio.Task[None] | None = None
        completion_task: asyncio.Future[tuple[int, int, int]] | None = None
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

        try:
            spawn_kwargs = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.PIPE,
                "cwd": str(cwd) if cwd else None,
                "env": env,
            }
            if isinstance(wrapped_command, list):
                proc = await spawn_exec(*wrapped_command, **spawn_kwargs)
            else:
                proc = await spawn_shell(wrapped_command, **spawn_kwargs)
            if process_started_callback is not None:
                try:
                    started_result = process_started_callback(proc.pid)
                    if asyncio.iscoroutine(started_result):
                        await started_result
                except Exception:
                    pass

            if cancel_event:
                async def _wait_cancel() -> None:
                    await cancel_event.wait()
                    await self._kill_tree(proc)
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

            try:
                stdout_task = asyncio.create_task(
                    _read_stream(proc.stdout, stdout_capture, stream_name="stdout")
                )
                stderr_task = asyncio.create_task(
                    _read_stream(proc.stderr, stderr_capture, stream_name="stderr")
                )
                completion_task = asyncio.gather(
                    stdout_task,
                    stderr_task,
                    proc.wait(),
                )
                if self._policy.timeout is not None and self._policy.timeout > 0:
                    stdout_total, stderr_total, _ = await asyncio.wait_for(
                        asyncio.shield(completion_task),
                        timeout=self._policy.timeout,
                    )
                else:
                    stdout_total, stderr_total, _ = await asyncio.shield(completion_task)
            except asyncio.TimeoutError:
                await self._kill_tree(proc)
                try:
                    stdout_total, stderr_total, _ = await asyncio.wait_for(
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
                )

            stderr_text = stderr_capture.snapshot()
            if (
                capability.backend == "codex-windows-sandbox"
                and proc.returncode not in (None, 0)
                and _codex_cwd_startup_failure(stderr_text, cwd)
            ):
                return SandboxResult(
                    stdout=stdout_capture.snapshot(),
                    stderr=f"Sandbox unavailable: Codex restricted token could not enter cwd {cwd}: {stderr_text}",
                    exit_code=126,
                    sandbox_unavailable=True,
                    stdout_path=stdout_capture.path,
                    stderr_path=stderr_capture.path,
                    stdout_total_bytes=stdout_total,
                    stderr_total_bytes=stderr_total,
                )

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
            if proc is not None:
                await asyncio.shield(self._kill_tree(proc))
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
            )
        except FileNotFoundError:
            return SandboxResult(stdout="", stderr=f"Command not found: {command}", exit_code=127)
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

    def _build_env(self) -> dict[str, str]:
        env = sanitized_subprocess_env()
        # Nudge child processes toward UTF-8 so their output decodes cleanly.
        # PYTHONUTF8/PYTHONIOENCODING cover Python children; PYTHONUNBUFFERED
        # keeps streamed output prompt. Native Windows tools that ignore these
        # are handled by the OEM-codepage fallback in _decode_command_bytes.
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.update(self._policy.env_overrides)
        return env

    def _wrap_command(
        self,
        command: str,
        capability: SandboxCapability,
        *,
        cwd: str | Path | None = None,
        host_command: str = "",
    ) -> str | list[str]:
        if self._policy.disable_os_sandbox:
            return host_command or command

        if capability.backend == "bubblewrap":
            return _bubblewrap_command(command, self._policy)

        if capability.backend == "seatbelt":
            profile = _seatbelt_profile(self._policy)
            return f"sandbox-exec -p {_shell_quote(profile)} -- sh -c {_shell_quote(command)}"

        if capability.backend in {"docker", "podman"}:
            return self._container_command(command, capability.backend, cwd=cwd)

        if capability.backend == "codex-windows-sandbox":
            executable, reason = _codex_windows_sandbox()
            if not executable:
                raise SandboxUnavailableError(reason)
            return _codex_windows_command(
                executable,
                self._policy,
                host_command or command,
                cwd=cwd,
            )

        raise SandboxUnavailableError(
            capability.reason or f"Unsupported sandbox backend: {capability.backend}"
        )

    async def _kill_tree(self, proc: asyncio.subprocess.Process) -> None:
        # The sandbox owns container cleanup, while the host child still uses
        # the shared process-group lifecycle used by every other execution path.
        if proc.returncode is None:
            await terminate_process_tree(proc)
        await self._cleanup_container(force=True)

    def _container_command(
        self,
        command: str,
        engine: str,
        *,
        cwd: str | Path | None,
    ) -> str:
        _runtime, image, reason = _container_runtime()
        if not image:
            raise SandboxUnavailableError(reason or "MiniCode sandbox image is not configured")
        writable_roots = tuple(root.expanduser().resolve() for root in self._policy.writable_roots)
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
        for index, root in enumerate(self._policy.readable_roots):
            resolved = root.expanduser().resolve()
            path_mappings.extend(
                (
                    (str(resolved), f"/readable/{index}"),
                    (str(resolved).replace("\\", "/"), f"/readable/{index}"),
                )
            )
        container_command = command
        for host_path, container_path in sorted(
            set(path_mappings), key=lambda item: len(item[0]), reverse=True
        ):
            if host_path:
                container_command = container_command.replace(host_path, container_path)
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
            "--tmpfs=/tmp:rw,nosuid,nodev,size=512m",
            f"--volume={workspace_root}:/workspace:{'rw' if workspace_is_writable else 'ro'}",
            f"--workdir={container_cwd}",
            "--env=HOME=/tmp",
            "--env=PYTHONUTF8=1",
            "--env=PYTHONUNBUFFERED=1",
        ]
        if os.name != "nt" and hasattr(os, "getuid") and hasattr(os, "getgid"):
            args.append(f"--user={os.getuid()}:{os.getgid()}")
        if not self._policy.allow_network:
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
        for index, root in enumerate(self._policy.readable_roots):
            args.append(f"--volume={root.expanduser().resolve()}:/readable/{index}:ro")
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
        if os.name == "nt":
            return subprocess.list2cmdline(args)
        return " ".join(_shell_quote(part) for part in args)

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


def _bubblewrap_command(command: str, policy: SandboxPolicy) -> str:
    """Build a minimal Linux filesystem namespace around the workspace.

    Host home directories are intentionally absent. Standard runtime paths are
    mounted read-only, writable roots are mounted read-write, and /tmp is an
    isolated tmpfs. This mirrors the workspace-write contract instead of merely
    changing the child process group.
    """
    args = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--unshare-pid",
        "--unshare-uts",
        "--unshare-ipc",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
    ]
    if not policy.allow_network:
        args.append("--unshare-net")

    for raw_path in ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/nix"):
        path = Path(raw_path)
        if path.exists():
            args.extend(("--ro-bind", raw_path, raw_path))

    workspace_root = (
        policy.workspace_root.expanduser().resolve()
        if policy.workspace_root is not None
        else None
    )
    writable_roots = tuple(root.expanduser().resolve() for root in policy.writable_roots)
    readable_roots = tuple(root.expanduser().resolve() for root in policy.readable_roots)
    if workspace_root is not None:
        args.extend(("--ro-bind", str(workspace_root), str(workspace_root)))
    for root in readable_roots:
        if workspace_root is not None and root == workspace_root:
            continue
        args.extend(("--ro-bind", str(root), str(root)))
    for root in writable_roots:
        args.extend(("--bind", str(root), str(root)))

    args.extend(("--setenv", "HOME", "/tmp", "--", "sh", "-c", command))
    return " ".join(_shell_quote(part) for part in args)


def _seatbelt_profile(policy: SandboxPolicy) -> str:
    def literal(path: Path) -> str:
        return str(path).replace("\\", "\\\\").replace('"', '\\"')

    rules = ["(version 1)", "(deny default)"]
    rules.append("(allow process-exec)")
    rules.append("(allow process-fork)")
    rules.append("(allow sysctl-read)")
    rules.append("(allow mach-lookup)")
    # Read access to standard system paths
    rules.append('(allow file-read* (subpath "/usr"))')
    rules.append('(allow file-read* (subpath "/System"))')
    rules.append('(allow file-read* (subpath "/Library"))')
    rules.append('(allow file-read* (subpath "/private/tmp"))')
    rules.append('(allow file-read* (subpath "/dev"))')
    if policy.workspace_root is not None:
        rules.append(f'(allow file-read* (subpath "{literal(policy.workspace_root)}"))')
    for root in policy.writable_roots:
        rules.append(f'(allow file-read* file-write* (subpath "{literal(root)}"))')
    for root in policy.readable_roots:
        rules.append(f'(allow file-read* (subpath "{literal(root)}"))')
    if policy.allow_network:
        rules.append("(allow network*)")
    return "\n".join(rules)
