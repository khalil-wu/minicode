"""Actual local executor services under MiniCode's existing OS sandbox owner."""
from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import signal as os_signal
import secrets
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

from backend.environments.errors import ExecutorClosed, ExecutorProtocolError
from backend.environments.events import ProcessEventLog
from backend.environments.process import ProcessOutputChunk, ProcessRead
from backend.network.policy import NetworkExecutionPolicy
from backend.sandbox import SandboxRunner
from backend.sandbox.runner import SandboxUnavailableError
from backend.runtime_env import default_user_shell
from backend.terminal.pty_process import spawn_pty
from backend.workspace.executor_local_fs import LocalExecutorFileSystem
from backend.workspace.executor_local_policy import local_path, local_sandbox_policy
from backend.workspace.executor_paths import ExecutorPath


_SOURCE_DENIAL_TEXT = ("operation not permitted", "permission denied", "read-only file system",
    "seccomp", "sandbox", "landlock", "failed to write file")


def _native_uri(path: str | Path) -> str:
    return ExecutorPath.from_native(str(path), convention="windows" if os.name == "nt" else "posix").uri


def _source_sandbox_type(backend: str) -> str | None:
    return {"full-access": "none", "external-sandbox": "none", "seatbelt": "macosSeatbelt",
        "bubblewrap": "linuxSeccomp", "linux-managed-proxy": "linuxSeccomp", "windows-restricted-token": "windowsRestrictedToken",
        "windows-elevated-wfp": "windowsRestrictedToken"}.get(backend)


class LocalExecutorProcess:
    def __init__(self, owner, process_id: str, sandbox: dict | None, runner: SandboxRunner,
            *, tty: bool, pipe_stdin: bool):
        self.owner = owner
        self.process_id = process_id
        self.sandbox = deepcopy(sandbox)
        self.runner = runner
        self.tty, self.pipe_stdin = tty, pipe_stdin
        self.process = None
        self.start_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        self.retention_task: asyncio.Task | None = None
        self.close_task: asyncio.Task | None = None
        self.events = ProcessEventLog()
        self.default_receiver = None
        self.output = deque()
        self.retained_bytes = 0
        self.next_seq = 1
        self.exited = self.closed = self.accepted = self.released = False
        self.exit_code: int | None = None
        self.failure: Exception | None = None
        self.sandbox_denied = False
        self.sandbox_type: str | None = None
        self.sandbox_backend = ""
        self.network_lease = None
        self.network_audit: list[dict] = []
        self.group_owner = None
        self.changed = asyncio.Event()
        self.exit_observed = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.write_ids = set()
        self.write_order = deque()

    def _publish(self, method: str, **payload) -> None:
        seq = self.next_seq
        self.next_seq += 1
        event = {"method": method, "processId": self.process_id, "seq": seq, **payload}
        if method == "process/output":
            chunk = ProcessOutputChunk(seq, payload["stream"], payload["chunk"])
            self.output.append(chunk)
            self.retained_bytes += len(chunk.chunk)
            while self.retained_bytes > 1024 * 1024 or len(self.output) > 50_000:
                self.retained_bytes -= len(self.output.popleft().chunk)
        self.events.publish(event)
        self.changed.set()

    async def _pump(self, stream, name: str) -> None:
        while chunk := await stream.read(64 * 1024):
            self._publish("process/output", stream=name, chunk=chunk)

    async def _watch(self) -> None:
        streams = [(self.process.stdout, "pty" if self.tty else "stdout")]
        if self.process.stderr is not None:
            streams.append((self.process.stderr, "stderr"))
        pumps = [asyncio.create_task(self._pump(stream, name), name=f"local-process-{name}-{self.process_id}")
            for stream, name in streams]
        exit_waiter = asyncio.create_task(self.exit_observed.wait(), name=f"local-process-exit-{self.process_id}")
        try:
            pending = {*pumps, exit_waiter}
            while exit_waiter in pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            code = self.process.returncode
            # codex-utils-pty::exit_code_from_status exposes Unix signals as
            # 128 + signal, rather than Python's negative return code.
            self.exit_code = 128 - code if os.name != "nt" and code < 0 else code
            self.exited = True
            if self.sandbox_type != "none":
                await asyncio.wait(pumps, timeout=0.02)
            if self.sandbox_type != "none" and self.exit_code != 0:
                text = b"".join(chunk.chunk for chunk in self.output).decode("utf-8", errors="replace").lower()
                self.sandbox_denied = any(word in text for word in _SOURCE_DENIAL_TEXT) or (
                    self.sandbox_type == "linuxSeccomp" and self.exit_code == 128 + getattr(os_signal, "SIGSYS", -1000))
            self._publish("process/exited", exitCode=self.exit_code, sandboxDenied=self.sandbox_denied)
            await asyncio.gather(*pumps)
            await self.process.wait()
        except Exception as error:
            self.failure = error
            self.events.publish({"method": "process/failed", "error": str(error)})
            self.changed.set()
            await self._terminate_owned_tree()
            await asyncio.gather(*pumps, return_exceptions=True)
            await self.process.wait()
            raise
        finally:
            exit_waiter.cancel()
            await asyncio.gather(exit_waiter, return_exceptions=True)
            if self.process.stdin is not None:
                self.process.stdin.close()
                try:
                    await self.process.stdin.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The exited child has already closed its pipe.
            if self.process.returncode is not None and all(task.done() for task in pumps):
                self.closed = True
                self._publish("process/closed")
                self.retention_task = asyncio.create_task(self._expire(), name=f"local-process-retention-{self.process_id}")

    async def _expire(self) -> None:
        await asyncio.sleep(30)
        if self.owner.processes.get(self.process_id) is self:
            task = asyncio.create_task(self.aclose(), name=f"expired-local-process-{self.process_id}")
            self.owner.cleanup_tasks.add(task)

    async def _start(self, argv: list[str], cwd: str, arg0: str | None) -> None:
        native_cwd = local_path(cwd)
        capability = self.runner.capability(cwd=native_cwd)
        self.sandbox_backend = capability.backend
        self.sandbox_type = _source_sandbox_type(capability.backend)
        executable = list(argv)
        if arg0 is not None:
            executable = [sys.executable, "-I", "-S", str(Path(__file__).with_name("executor_process_shim.py")),
                json.dumps(argv, ensure_ascii=True), arg0]
        flags, created = None, None
        if os.name == "nt":
            import win32process
            from backend.workspace.executor_process_owner import WindowsProcessJob

            self.group_owner = WindowsProcessJob()
            flags = win32process.CREATE_SUSPENDED | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            created = self.group_owner.attach_suspended
        if self.tty:
            command = subprocess.list2cmdline(executable) if os.name == "nt" else shlex.join(executable)
            wrapped, _capability = self.runner.prepare_command(command, cwd=native_cwd,
                host_command=command, argv=executable, tty=True)
            pass_fds = (self.runner._bubblewrap_mount_targets.preserved_fds
                if self.runner._bubblewrap_mount_targets is not None else ())
            try:
                self.process = await spawn_pty(wrapped, cwd=native_cwd, env=self.runner._build_env(), pass_fds=pass_fds,
                    on_exit=self.exit_observed.set, creationflags=flags, on_created=created)
                self.runner.process = self.process
                await self.runner._await_sandbox_ready(self.process, capture_startup_output=True)
            except BaseException:
                await self.runner.cleanup()
                raise
        else:
            self.process = await self.runner.spawn_interactive(executable, cwd=native_cwd,
                stdin=asyncio.subprocess.PIPE if self.pipe_stdin else asyncio.subprocess.DEVNULL,
                creationflags=flags, on_created=created, on_exit=self.exit_observed.set)
        self.accepted = True
        self.watch_task = asyncio.create_task(self._watch(), name=f"local-process-watch-{self.process_id}")

    async def read(self, *, after_seq: int | None = None, max_bytes: int | None = None,
            wait_ms: int | None = None) -> ProcessRead:
        after = after_seq if after_seq is not None else 0
        deadline = time.monotonic() + (wait_ms or 0) / 1000
        while True:
            if self.owner.processes.get(self.process_id) is not self:
                raise ExecutorProtocolError(f"unknown process id {self.process_id}")
            chunks, size, next_seq = [], 0, self.next_seq
            for chunk in self.output:
                if chunk.seq <= after:
                    continue
                if chunks and max_bytes is not None and size + len(chunk.chunk) > max_bytes:
                    break
                chunks.append(chunk)
                size += len(chunk.chunk)
                next_seq = chunk.seq + 1
                if max_bytes is not None and size >= max_bytes:
                    break
            if max_bytes is None:
                next_seq = self.next_seq
            if chunks or self.closed or self.failure is not None or self.exited and after < next_seq - 1 or time.monotonic() >= deadline:
                return ProcessRead(tuple(chunks), next_seq, self.exited, self.exit_code, self.closed,
                    str(self.failure) if self.failure is not None else None, self.sandbox_denied)
            self.changed.clear()
            try:
                async with asyncio.timeout(max(0, deadline - time.monotonic())):
                    await self.changed.wait()
            except TimeoutError:
                continue

    def subscribe_events(self):
        return self.events.subscribe()

    async def next_event(self) -> dict:
        if self.default_receiver is None:
            self.default_receiver = self.subscribe_events()
        return await self.default_receiver.recv()

    async def write(self, chunk: bytes, *, write_id: str | None = None) -> str:
        identity = write_id if write_id is not None else uuid4().hex
        if not identity:
            raise ValueError("writeId must not be empty")
        if self.owner.processes.get(self.process_id) is not self:
            return "unknownProcess"
        if not self.accepted:
            return "starting"
        if not (self.tty or self.pipe_stdin):
            return "stdinClosed"
        async with self.write_lock:
            if identity in self.write_ids:
                return "accepted"
            if self.process.stdin is None or self.process.stdin.is_closing():
                return "stdinClosed"
            self.process.stdin.write(chunk)
            self.write_ids.add(identity)
            self.write_order.append(identity)
            while len(self.write_order) > 4096:
                self.write_ids.remove(self.write_order.popleft())
            await self.process.stdin.drain()
        return "accepted"

    async def signal(self, signal: str = "interrupt") -> None:
        if signal != "interrupt":
            raise ValueError(f"Unknown process signal: {signal}")
        if not self.accepted or self.exited:
            return
        if self.tty:
            await self.write(b"\x03")
        elif os.name == "nt":
            self.process.send_signal(os_signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(self.process.pid, os_signal.SIGINT)

    async def _terminate_owned_tree(self) -> None:
        if self.group_owner is not None:
            await self.group_owner.terminate()
        elif self.process is not None and os.name != "nt":
            try:
                os.killpg(self.process.pid, os_signal.SIGKILL)
            except ProcessLookupError:
                pass  # The captured group already has no processes.
        elif self.process is not None and not await self.runner.terminate(self.process):
            raise RuntimeError(f"local process tree {self.process_id} did not exit")

    async def terminate(self) -> bool:
        if not self.accepted or self.exited:
            return False
        running = True
        await self._terminate_owned_tree()
        return running

    async def _close(self) -> dict:
        errors = []
        try:
            if self.start_task is not None:
                results = await asyncio.gather(asyncio.shield(self.start_task), return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        self.failure = result
            try:
                # Even a closed parent can have descendants with redirected
                # stdio. Release the original tree, not a fresh PID lookup.
                await self._terminate_owned_tree()
            except Exception as error:
                errors.append(error)
            if self.watch_task is not None:
                await asyncio.gather(asyncio.shield(self.watch_task), return_exceptions=True)
            if self.group_owner is not None:
                try:
                    await self.group_owner.aclose()
                except Exception as error:
                    errors.append(error)
            if self.retention_task is not None:
                self.retention_task.cancel()
                await asyncio.gather(self.retention_task, return_exceptions=True)
            try:
                if not await self.runner.cleanup():
                    raise RuntimeError(f"local process sandbox {self.process_id} did not close")
            except Exception as error:
                errors.append(error)
            if self.network_lease is not None:
                try:
                    await self.network_lease.flush_audit()
                    await self.network_lease.aclose()
                except Exception as error:
                    errors.append(error)
            if errors:
                raise ExceptionGroup(f"local process {self.process_id} did not close", errors)
            return {"process_id": self.process_id, "closed": self.closed, "exit_code": self.exit_code,
                "failure": str(self.failure) if self.failure is not None else None}
        finally:
            self.released = True
            if self.owner.processes.get(self.process_id) is self:
                self.owner.processes.pop(self.process_id)

    async def aclose(self) -> dict:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name=f"local-process-close-{self.process_id}")
        return await asyncio.shield(self.close_task)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        await self.aclose()


class LocalProcessBackend:
    def __init__(self):
        self.processes: dict[str, LocalExecutorProcess] = {}
        self.cleanup_tasks: set[asyncio.Task] = set()
        self.closed = False

    async def start(self, argv: list[str], cwd: str, *, sandbox: dict | None,
            env: dict[str, str] | None = None, tty: bool = False, pipe_stdin: bool = False,
            arg0: str | None = None, process_id: str | None = None, env_policy: dict | None = None,
            metadata: dict | None = None, shell_snapshot: dict | None = None,
            enforce_managed_network: bool = False, managed_network: dict | None = None,
            network_proxy: dict | None = None, process_observer=None,
            managed_network_policy: NetworkExecutionPolicy | None = None, network_decider=None,
            network_observer=None) -> LocalExecutorProcess:
        if self.closed:
            raise ExecutorClosed("local execution environment is closed")
        if not argv:
            raise ValueError("process argv must not be empty")
        if shell_snapshot is not None:
            raise ExecutorProtocolError("local executor shell snapshots are not supported")
        if managed_network is not None or network_proxy is not None or enforce_managed_network:
            raise ExecutorProtocolError("local managed network launch requires its captured native execution policy")
        policy = local_sandbox_policy(sandbox, env=env, env_policy=env_policy)
        if managed_network_policy is not None:
            policy = replace(policy, enforce_managed_network=True)
            if sys.platform == "linux":
                policy = replace(policy, linux_sandbox_executable=managed_network_policy.runtime.binary)
        runner = SandboxRunner(policy)
        identity = process_id or uuid4().hex
        if identity in self.processes:
            raise ExecutorProtocolError("process handle is already registered in this executor session")
        process = LocalExecutorProcess(self, identity, sandbox, runner, tty=tty, pipe_stdin=pipe_stdin)
        self.processes[identity] = process
        try:
            if managed_network_policy is not None:
                capability = runner.capability(cwd=local_path(cwd))
                if not capability.available:
                    raise SandboxUnavailableError(capability.reason)
                process.network_lease = await managed_network_policy.prepare_local(dict(policy.captured_env),
                    environment_id=managed_network_policy.environment_id, execution_id=identity,
                    attribution_token=secrets.token_hex(32), decider=network_decider, observer=network_observer)
                process.network_audit = process.network_lease.network_audit
                if sys.platform == "win32":
                    from backend.sandbox.windows_native import capture_local_network_prerequisite

                    prerequisite = await asyncio.to_thread(capture_local_network_prerequisite, process.network_lease)
                    status = await asyncio.to_thread(prerequisite.status)
                    if not status["ready"]:
                        raise SandboxUnavailableError(status["reason"])
                runner._policy = replace(policy, captured_env=process.network_lease.env,
                    managed_network=process.network_lease.sandbox_context,
                    network_proxy_restricting_sid=process.network_lease.restricting_sid)
            if process_observer is not None:
                process_observer(process)
        except BaseException:
            await process.aclose()
            raise
        process.start_task = asyncio.create_task(process._start(argv, cwd, arg0), name=f"local-process-start-{identity}")
        try:
            await asyncio.shield(process.start_task)
            return process
        except asyncio.CancelledError:
            task = asyncio.create_task(process.aclose(), name=f"cancelled-local-process-start-{identity}")
            self.cleanup_tasks.add(task)
            raise
        except Exception as error:
            try:
                await process.aclose()
            except Exception as close_error:
                raise ExceptionGroup("local process startup and cleanup failed", [error, close_error]) from error
            raise

    async def close(self) -> None:
        self.closed = True
        results = await asyncio.gather(*(process.aclose() for process in tuple(self.processes.values())),
            *tuple(self.cleanup_tasks), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("local process resources did not close", errors)


class LocalExecutionEnvironmentBackend:
    def __init__(self):
        self.filesystem = LocalExecutorFileSystem()
        self.process = LocalProcessBackend()
        self.close_task: asyncio.Task | None = None

    async def info(self) -> dict:
        if self.process.closed:
            raise ExecutorClosed("local execution environment is closed")
        shell = default_user_shell()
        temporary = []
        for key in (("TEMP", "TMP") if os.name == "nt" else ("TMPDIR",)):
            raw = os.environ.get(key)
            if raw:
                path = Path(raw)
                if not path.is_absolute() and os.name == "nt":
                    continue
                value = _native_uri(path if path.is_absolute() else Path.cwd() / path)
                if value not in temporary:
                    temporary.append(value)
        return {"shell": shell, "executorVersion": "0.0.0",
            "cwd": _native_uri(Path.cwd()), "userHomeDir": _native_uri(Path.home()),
            "platformOs": "windows" if os.name == "nt" else "macos" if sys.platform == "darwin" else sys.platform,
            "prependPathDirs": [], "temporaryDirectories": temporary, "tempDir": _native_uri(tempfile.gettempdir()),
            "capabilities": {"networkProxyLaunch": False, "capabilityDiscoverySandbox": False,
                "capabilityDiscoveryV2": False, "environmentConfigRead": False, "httpHeaderEnvVars": False,
                "sandboxedFileStreaming": True, "fileWriteStreaming": True, "shellSnapshotV2": False,
                "windowsMxc": False, "linuxRootWritePreservesDevices": False,
                "linuxApprovedRootWritePreservesRestrictions": False}}

    async def _close(self) -> None:
        results = await asyncio.gather(self.process.close(), self.filesystem.close(), return_exceptions=True)
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise ExceptionGroup("local execution environment did not close", errors)

    async def close(self) -> None:
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._close(), name="local-environment-close")
        await asyncio.shield(self.close_task)

    aclose = close
