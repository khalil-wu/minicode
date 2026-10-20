"""MiniCode background-command lifecycle and recovery owner."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import signal
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Coroutine

from backend.agent.execution_lifecycle import ExecutionLifecycle
from backend.async_cleanup import (
    CANCELLATION_DRAIN_TIMEOUT_SECONDS,
    cancel_and_drain_receipt,
)
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.terminal.task_output import (
    DurableTaskOutput,
    cleanup_task_output_owner,
    delete_task_output,
    format_task_output,
)
from backend.terminal.task_persistence import (
    PersistedTaskState,
    cleanup_orphaned_tasks,
)
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.terminal.stdin_authority import TerminalInputApproval, TerminalLaunchAuthority, StdinAuthorityError
from backend.permissions.environment_command import EnvironmentCommandPolicy
from backend.terminal.executor_runner import ExecutorCommandProcess, ExecutorCommandRunner, command_capability, command_runner
from backend.tools.base import MAX_TOOL_RESULT_BYTES, truncate_text_tail

logger = logging.getLogger(__name__)

MAX_BACKGROUND_COMMANDS = 10
# cc LocalShellTask.tsx: check output growth every 5s and notify once when the
# output has not grown for 45s and the tail looks like an interactive prompt.
STALL_CHECK_INTERVAL_SECONDS = 5.0
STALL_THRESHOLD_SECONDS = 45.0
_STALL_TAIL_CHARS = 1024
_STALL_PROMPT_PATTERNS = (
    r"\(y/n\)",
    r"\[y/n\]",
    r"\(yes/no\)",
    r"\b(?:Do you|Would you|Shall I|Are you sure|Ready to)\b.*\?\s*$",
    r"Press (?:any key|Enter)",
    r"Continue\?",
    r"Overwrite\?",
)
_STALL_PROMPT_RE = [re.compile(pattern, re.IGNORECASE) for pattern in _STALL_PROMPT_PATTERNS]


def _stall_tail_looks_like_prompt(output: str) -> bool:
    tail = output[-_STALL_TAIL_CHARS:].rstrip()
    last_line = tail.splitlines()[-1] if tail else ""

    return any(pattern.search(last_line) for pattern in _STALL_PROMPT_RE)
MAX_BACKGROUND_LIVE_TAIL = MAX_TOOL_RESULT_BYTES


@dataclass
class BackgroundCommand:
    """后台命令状态。"""
    command_id: str
    command: str
    description: str = ""
    cwd: str = ""
    status: str = "running"  # running | completed | failed | cancelled
    output: str = ""
    exit_code: int | None = None
    started_at: float = 0.0
    completed_at: float | None = None
    timeout_ms: int = 0
    conversation_id: str = ""
    owner_task_id: str = ""
    parent_run_id: str = ""
    sandbox_backend: str = "full-access"
    output_path: str = ""
    output_bytes: int = 0
    output_chars: int = 0
    output_file_redundant: bool = False
    effective_command: str = field(default="", repr=False)
    argv: list[str] = field(default_factory=list, repr=False)
    tty: bool = False
    terminal_authority: TerminalLaunchAuthority | None = field(default=None, repr=False)
    original_tool_call_id: str = ""
    original_tool_args: dict[str, Any] = field(default_factory=dict, repr=False)
    completion_hook_claimed: bool = False
    command_completed: bool = False
    sandbox_policy: SandboxPolicy | None = field(default=None, repr=False)
    runner: SandboxRunner | None = field(default=None, repr=False)
    lifecycle: ExecutionLifecycle | None = field(default=None, repr=False)
    task_output: DurableTaskOutput | None = field(default=None, repr=False)
    output_write_error: str = field(default="", repr=False)
    pid: int | None = field(default=None, repr=False)
    process_start_time: float | None = field(default=None, repr=False)
    cleanup_pending: bool = False
    cleanup_reason: str = ""
    cleanup_requested_at: float | None = None
    cleanup_completed_at: float | None = None
    cleanup_error: dict[str, Any] = field(default_factory=dict, repr=False)
    backgrounded: bool = True
    output_changed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    stream_callback: Callable[[str], Any] | None = field(default=None, repr=False)
    environment_id: str = "local"
    executor_remote: bool = False
    executor_process_id: str = ""
    executor_session_id: str = ""
    network_audit: list[dict] = field(default_factory=list, repr=False)
    startup_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def append_live_output(self, piece: str) -> None:
        """Persist one streamed chunk and keep only a bounded in-memory tail."""
        self.output_changed.set()
        if not piece:
            return
        if self.task_output is not None and not self.output_write_error:
            try:
                self.task_output.append(piece)
                self.output_bytes = self.task_output.bytes_written
                self.output_chars = self.task_output.characters_written
            except OSError as exc:
                self.output_write_error = str(exc)
        combined = f"{self.output}{piece}"
        self.output = truncate_text_tail(
            combined,
            max_bytes=MAX_BACKGROUND_LIVE_TAIL,
        ).content

    def close_task_output(self) -> None:
        if self.task_output is None:
            return
        try:
            self.task_output.close()
            self.output_bytes = self.task_output.bytes_written
            self.output_chars = self.task_output.characters_written
        except OSError as exc:
            self.output_write_error = self.output_write_error or str(exc)

    def _lifecycle(self) -> ExecutionLifecycle:
        if self.lifecycle is None:
            self.lifecycle = ExecutionLifecycle(
                run_id=self.command_id,
                task_id=self.command_id,
                kind="background_command",
                phase="running",
                status=self.status,
                started_at=int(self.started_at * 1000),
                updated_at=int(self.started_at * 1000),
            )
        return self.lifecycle

    def transition(
        self,
        *,
        phase: str,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        self._lifecycle().transition(
            phase=phase,
            status=status,
            result=result,
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "command_id": self.command_id,
            "command": self.command,
            "description": self.description,
            "cwd": self.cwd,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "conversation_id": self.conversation_id,
            "owner_task_id": self.owner_task_id,
            "parent_run_id": self.parent_run_id,
            "sandbox_backend": self.sandbox_backend,
            "tty": self.tty,
            "output_path": self.output_path,
            "output_bytes": self.output_bytes,
            "output_length": self.output_chars,
            "output_file_redundant": self.output_file_redundant,
            "cleanup_pending": self.cleanup_pending,
            "cleanup_reason": self.cleanup_reason,
            "cleanup_requested_at": self.cleanup_requested_at,
            "cleanup_completed_at": self.cleanup_completed_at,
            "cleanup_error": dict(self.cleanup_error),
            "environment_id": self.environment_id,
            "network_policy_decisions": list(self.network_audit),
        }
        payload.update(self._lifecycle().to_payload())
        return payload


class BackgroundCommandManager:
    """
    管理后台执行的命令。

    特性：
      - 命令在后台异步执行
      - 命令完成时通过回调推送通知
      - 支持取消
      - 输出截断保护
      - 最大并发限制
    """

    def __init__(
        self,
        on_completed: Callable[[BackgroundCommand], Coroutine[Any, Any, None]] | None = None,
        on_started: Callable[[BackgroundCommand], Coroutine[Any, Any, None]] | None = None,
        max_commands: int = MAX_BACKGROUND_COMMANDS,
        session_id: str | None = None,
    ) -> None:
        self._commands: dict[str, BackgroundCommand] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._stdin_locks: dict[str, asyncio.Lock] = {}
        self._completion_notified: set[str] = set()
        self._on_completed = on_completed
        self._on_started = on_started
        self._on_stalled = None
        self._max_commands = max_commands
        self._session_id = session_id or ""

    def cleanup_orphaned_tasks_on_startup(self) -> list[PersistedTaskState]:
        """Reconcile persisted commands whose owning MiniCode process exited.

        The returned records are reloaded after the durable terminal update,
        so callers project the committed status and cleanup receipt rather
        than the stale pre-recovery record.
        """
        if not self._session_id:
            return []
        return cleanup_orphaned_tasks(self._session_id)

    async def run_background(
        self,
        command: str,
        cwd: str | None = None,
        timeout_ms: int = 0,
        description: str = "",
        conversation_id: str = "",
        task_id: str = "",
        parent_run_id: str = "",
        sandbox_policy: SandboxPolicy | None = None,
        effective_command: str = "",
        backgrounded: bool = True,
        stream_callback: Callable[[str], Any] | None = None,
        argv: list[str] | None = None,
        tty: bool = False,
        terminal_authority: TerminalLaunchAuthority | None = None,
        original_tool_call_id: str = "",
        original_tool_args: dict[str, Any] | None = None,
    ) -> BackgroundCommand:
        """在后台执行命令，立即返回 BackgroundCommand。"""
        owner = str(conversation_id or "").strip()
        if not owner:
            raise RuntimeError(
                "Background commands require a conversation owner; the command was not started."
            )
        if not self._session_id:
            raise RuntimeError(
                "Background commands require a session owner; the command was not started."
            )
        # 清理已完成的命令
        self._cleanup_completed()

        running = [c for c in self._commands.values() if c.status == "running" or c.cleanup_pending]
        if len(running) >= self._max_commands:
            raise RuntimeError(
                f"后台命令数已达上限（{self._max_commands}）。"
                "请等待现有命令完成或取消后重试。"
            )

        display_command = command
        if sandbox_policy is None:
            raise RuntimeError(
                "Background commands require an explicit sandbox policy; the command was not started."
            )
        capability = command_capability(sandbox_policy, cwd=cwd)
        if not capability.available:
            raise RuntimeError(
                f"Sandbox unavailable: {capability.reason}. The background command was not started."
            )
        sandbox_backend = capability.backend
        timeout_seconds = (
            max(1, int((timeout_ms + 999) / 1000))
            if timeout_ms > 0
            else 0
        )
        sandbox_policy = replace(
            sandbox_policy,
            timeout=timeout_seconds,
        )

        command_id = f"bg_{uuid.uuid4().hex[:8]}"
        try:
            task_output = DurableTaskOutput(
                session_id=self._session_id,
                conversation_id=owner,
                task_id=command_id,
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Background output file could not be created; the command was not started: {exc}"
            ) from exc

        bg_cmd = BackgroundCommand(
            command_id=command_id,
            command=display_command,
            backgrounded=backgrounded,
            stream_callback=stream_callback,
            description=description or display_command[:60],
            cwd=cwd or os.getcwd(),
            status="running",
            started_at=time.time(),
            timeout_ms=timeout_ms,
            conversation_id=owner,
            owner_task_id=str(task_id or ""),
            parent_run_id=str(parent_run_id or ""),
            sandbox_backend=sandbox_backend,
            output_path=str(task_output.path),
            effective_command=effective_command or command,
            argv=list(argv or []),
            tty=tty,
            terminal_authority=terminal_authority,
            original_tool_call_id=original_tool_call_id,
            original_tool_args=deepcopy(original_tool_args or {"command": display_command}),
            sandbox_policy=sandbox_policy,
            environment_id=sandbox_policy.scope.environment_id if isinstance(sandbox_policy, EnvironmentCommandPolicy) else "local",
            executor_remote=isinstance(sandbox_policy, EnvironmentCommandPolicy) and sandbox_policy.attachment.is_remote,
            executor_process_id=command_id if isinstance(sandbox_policy, EnvironmentCommandPolicy) else "",
            task_output=task_output,
            lifecycle=ExecutionLifecycle(
                run_id=command_id,
                task_id=str(task_id or command_id),
                parent_run_id=str(parent_run_id or ""),
                kind="background_command",
                phase="queued",
                status="running",
                started_at=int(time.time() * 1000),
                updated_at=int(time.time() * 1000),
            ),
        )
        # Durable ownership is admission, not eventual bookkeeping. No task is
        # allowed to reach the event loop until its owner can be recovered.
        from backend.terminal.task_persistence import save_task

        try:
            save_task(
                session_id=self._session_id,
                task_id=command_id,
                command=display_command,
                description=bg_cmd.description,
                cwd=bg_cmd.cwd,
                pid=None,
                started_at=bg_cmd.started_at,
                timeout_ms=timeout_ms,
                status="running",
                conversation_id=owner,
                owner_task_id=bg_cmd.owner_task_id,
                parent_run_id=bg_cmd.parent_run_id,
                environment_id=bg_cmd.environment_id, executor_remote=bg_cmd.executor_remote,
                executor_process_id=bg_cmd.executor_process_id,
            )
        except Exception as exc:
            bg_cmd.close_task_output()
            try:
                delete_task_output(bg_cmd.output_path)
            except OSError:
                logger.debug("Failed to remove rejected background output", exc_info=True)
            raise RuntimeError(
                "Background command owner could not be persisted; the command was not started."
            ) from exc

        self._commands[command_id] = bg_cmd
        try:
            if isinstance(sandbox_policy, EnvironmentCommandPolicy):
                bg_cmd.runner = command_runner(sandbox_policy)
            task = asyncio.create_task(self._execute(bg_cmd))
        except Exception:
            if bg_cmd.runner is not None:
                await bg_cmd.runner.cleanup()
            self._commands.pop(command_id, None)
            from backend.terminal.task_persistence import delete_task

            delete_task(self._session_id, command_id)
            bg_cmd.close_task_output()
            delete_task_output(bg_cmd.output_path)
            raise
        self._tasks[command_id] = task

        logger.info("Background command started: %s (id: %s)", display_command[:60], command_id)
        return bg_cmd

    async def _notify_completed_once(self, bg_cmd: BackgroundCommand) -> None:
        """Deliver exactly one terminal callback, even across cancel races."""

        bg_cmd.output_changed.set()
        if not bg_cmd.backgrounded:
            return
        if bg_cmd.command_id in self._completion_notified:
            return
        self._completion_notified.add(bg_cmd.command_id)
        if self._on_completed:
            try:
                await self._on_completed(bg_cmd)
            except Exception as exc:
                # This callback is the only emitter of background.completed. A
                # debug-level swallow left the UI showing the task as running
                # forever.
                logger.error(
                    "Background completion callback failed for %s; the client was "
                    "never told the command finished: %s",
                    bg_cmd.command_id,
                    exc,
                    exc_info=True,
                )

    def _start_stall_watchdog(self, bg_cmd: BackgroundCommand) -> Callable[[], None]:
        """Watch one background command for an interactive-prompt stall.

        Every 5s the output size is compared; once it has not grown for 45s
        and the tail looks like an interactive prompt, a one-shot stall
        notice fires so the model can kill the task and re-run with piped
        input instead of hanging forever.
        """
        notified = False
        last_size = 0
        last_growth = time.monotonic()

        async def _watch() -> None:
            nonlocal notified, last_size, last_growth
            while not notified:
                await asyncio.sleep(STALL_CHECK_INTERVAL_SECONDS)
                if bg_cmd.status != "running":
                    return
                if not bg_cmd.backgrounded:
                    continue
                size = bg_cmd.output_bytes
                if size > last_size:
                    last_size = size
                    last_growth = time.monotonic()
                    continue
                if time.monotonic() - last_growth < STALL_THRESHOLD_SECONDS:
                    continue
                if not _stall_tail_looks_like_prompt(bg_cmd.output):
                    # Merely slow (long builds); check again 45s out.
                    last_growth = time.monotonic()
                    continue
                notified = True
                tail = bg_cmd.output[-_STALL_TAIL_CHARS:].rstrip()
                logger.warning(
                    "Background command %s appears blocked on an interactive prompt: %s",
                    bg_cmd.command_id,
                    bg_cmd.command,
                )
                callback = getattr(self, "_on_stalled", None)
                if callback is not None:
                    try:
                        await callback(bg_cmd, tail)
                    except Exception as exc:
                        logger.debug("Background stall callback failed: %s", exc)
                return

        task = asyncio.create_task(_watch())

        def _stop() -> None:
            nonlocal notified
            notified = True
            if not task.done():
                task.cancel()

        return _stop

    async def _execute(self, bg_cmd: BackgroundCommand) -> None:
        """Run one background command and settle its lifecycle record."""
        # Reason the process tree's exit could not be observed. Empty means the
        # teardown is proven and the cleanup receipt may be closed.
        unproven_cleanup = ""
        try:
            policy = bg_cmd.sandbox_policy
            if policy is None:
                raise RuntimeError("Background command has no sandbox policy")
            runner = bg_cmd.runner if bg_cmd.runner is not None else command_runner(policy)
            bg_cmd.runner = runner
            host_command = (None if isinstance(policy, EnvironmentCommandPolicy) else
                normalize_windows_shell_command(bg_cmd.effective_command or bg_cmd.command))

            async def _executor_started(process_id: str) -> None:
                from backend.terminal.task_persistence import save_task

                bg_cmd.executor_process_id = process_id
                bg_cmd.executor_session_id = runner.handle.client.session_id if bg_cmd.executor_remote else ""
                bg_cmd.transition(phase="running", status="running")
                bg_cmd.startup_ready.set()
                save_task(session_id=self._session_id, task_id=bg_cmd.command_id,
                    command=bg_cmd.command, description=bg_cmd.description, cwd=bg_cmd.cwd, pid=None,
                    started_at=bg_cmd.started_at, timeout_ms=bg_cmd.timeout_ms, status="running",
                    conversation_id=bg_cmd.conversation_id, owner_task_id=bg_cmd.owner_task_id,
                    parent_run_id=bg_cmd.parent_run_id, environment_id=bg_cmd.environment_id,
                    executor_remote=bg_cmd.executor_remote, executor_process_id=process_id,
                    executor_session_id=bg_cmd.executor_session_id)
                if self._on_started and bg_cmd.backgrounded:
                    await self._on_started(bg_cmd)

            async def _process_started(pid: int) -> None:
                from backend.terminal.task_persistence import get_process_start_time

                bg_cmd.pid = pid
                bg_cmd.startup_ready.set()
                bg_cmd.process_start_time = get_process_start_time(pid)
                bg_cmd.transition(phase="running", status="running")
                # Update persisted task only after the OS process exists.
                if self._session_id and pid:
                    from backend.terminal.task_persistence import save_task
                    save_task(
                        session_id=self._session_id,
                        task_id=bg_cmd.command_id,
                        command=bg_cmd.command,
                        description=bg_cmd.description,
                        cwd=bg_cmd.cwd,
                        pid=pid,
                        started_at=bg_cmd.started_at,
                        timeout_ms=bg_cmd.timeout_ms,
                        status="running",
                        conversation_id=bg_cmd.conversation_id,
                        owner_task_id=bg_cmd.owner_task_id,
                        parent_run_id=bg_cmd.parent_run_id,
                        environment_id=bg_cmd.environment_id, executor_remote=bg_cmd.executor_remote,
                        executor_process_id=bg_cmd.executor_process_id, executor_session_id=bg_cmd.executor_session_id,
                        process_start_time=bg_cmd.process_start_time,
                        **runner.container_ownership,
                    )
                if self._on_started and bg_cmd.backgrounded:
                    try:
                        await self._on_started(bg_cmd)
                    except Exception as exc:
                        logger.debug("Background start callback failed: %s", exc)

            async def _process_ready(process: asyncio.subprocess.Process) -> None:
                self._processes[bg_cmd.command_id] = process

            async def _stream_output(piece: str, _stream_name: str = "stdout") -> None:
                # Keep the owned task record current so the Monitor can serve
                # both polling snapshots and continuous output streaming.
                bg_cmd.append_live_output(piece)
                if bg_cmd.stream_callback is not None:
                    value = bg_cmd.stream_callback(piece)
                    if inspect.isawaitable(value):
                        await value

            stall_stop = self._start_stall_watchdog(bg_cmd)
            try:
                result = await runner.run(
                    bg_cmd.command,
                    cwd=bg_cmd.cwd,
                    host_command=host_command,
                    argv=bg_cmd.argv or None,
                    tty=bg_cmd.tty,
                    process_started_callback=_process_started,
                    process_ready_callback=_process_ready,
                    keep_stdin_open=True,
                    stream_callback=_stream_output,
                    **({"executor_started_callback": _executor_started, "process_id": bg_cmd.executor_process_id}
                       if isinstance(runner, ExecutorCommandRunner) else {}),
                )
            finally:
                stall_stop()

            if result.cleanup_pending:
                unproven_cleanup = result.cleanup_reason or "process_cleanup_pending"
            bg_cmd.network_audit = list(result.network_audit)
            if bg_cmd.output_write_error:
                raise RuntimeError(
                    f"Background output could not be persisted: {bg_cmd.output_write_error}"
                )

            if result.cancelled:
                bg_cmd.status = "cancelled"
                bg_cmd.exit_code = result.exit_code
                bg_cmd.completed_at = time.time()
                bg_cmd.transition(phase="cancelled", status="cancelled")
                return
            if result.sandbox_unavailable:
                bg_cmd.status = "failed"
                bg_cmd.append_live_output(result.stderr)
                bg_cmd.exit_code = 126
                bg_cmd.completed_at = time.time()
                bg_cmd.transition(
                    phase="completed",
                    status="failed",
                    error={"kind": "sandbox_unavailable", "message": result.stderr},
                )
                return
            if result.timed_out:
                bg_cmd.status = "failed"
                timeout_message = f"命令执行超时（{bg_cmd.timeout_ms / 1000.0:.0f}秒）"
                if bg_cmd.output:
                    bg_cmd.append_live_output(f"\n{timeout_message}")
                else:
                    bg_cmd.append_live_output(timeout_message)
                bg_cmd.exit_code = result.exit_code
                bg_cmd.completed_at = time.time()
                bg_cmd.transition(
                    phase="completed",
                    status="failed",
                    error={"kind": "timeout", "message": timeout_message},
                )
                return

            if result.execution_error:
                bg_cmd.status = "failed"
                bg_cmd.exit_code = result.exit_code
                bg_cmd.completed_at = time.time()
                if bg_cmd.output_chars == 0:
                    if result.stdout:
                        bg_cmd.append_live_output(result.stdout)
                    if result.stderr:
                        bg_cmd.append_live_output(result.stderr)
                bg_cmd.transition(phase="completed", status="failed",
                                  error={"kind": "runtime_error", "message": result.execution_error})
                return

            # The stream callback is the source of truth.  A startup failure can
            # return a final snapshot without ever opening a stream, so retain
            # that diagnostic only when no chunk was delivered.
            if bg_cmd.output_chars == 0:
                if result.stdout:
                    bg_cmd.append_live_output(result.stdout)
                if result.stderr:
                    bg_cmd.append_live_output(result.stderr)
            bg_cmd.exit_code = result.exit_code
            bg_cmd.status = "completed" if result.exit_code == 0 else "failed"
            bg_cmd.completed_at = time.time()
            bg_cmd.transition(
                phase="completed",
                status=bg_cmd.status,
                result={
                    "exit_code": bg_cmd.exit_code,
                    "output_length": bg_cmd.output_chars,
                    "output_bytes": bg_cmd.output_bytes,
                    "output_path": bg_cmd.output_path,
                },
                error=(
                    {"kind": "nonzero_exit", "exit_code": bg_cmd.exit_code}
                    if bg_cmd.status == "failed"
                    else {}
                ),
            )
            bg_cmd.command_completed = True

        except asyncio.CancelledError:
            bg_cmd.status = "cancelled"
            bg_cmd.completed_at = time.time()
            bg_cmd.transition(phase="cancelled", status="cancelled")
            if bg_cmd.pid is not None:
                # The coroutine was cancelled outside SandboxRunner's own
                # termination path, so nothing proved the process tree exited.
                unproven_cleanup = "cancelled_without_process_exit_proof"
        except Exception as exc:
            if isinstance(runner, ExecutorCommandRunner):
                bg_cmd.network_audit = list(runner.network_audit)
            if getattr(exc, "cleanup_pending", False):
                unproven_cleanup = str(exc.cleanup_reason)
            bg_cmd.status = "failed"
            error_message = f"执行错误: {exc}"
            if bg_cmd.output:
                bg_cmd.append_live_output(f"\n{error_message}")
            else:
                bg_cmd.append_live_output(error_message)
            bg_cmd.exit_code = (bg_cmd.runner.handle.exit_code if bg_cmd.runner.handle is not None else None
                ) if isinstance(bg_cmd.runner, ExecutorCommandRunner) else -1
            bg_cmd.completed_at = time.time()
            bg_cmd.transition(
                phase="completed",
                status="failed",
                error={"kind": "runtime_error", "message": str(exc)},
            )
            logger.error("Background command %s failed: %s", bg_cmd.command_id, exc)

        finally:
            bg_cmd.startup_ready.set()
            # Only a proven process exit closes the cleanup receipt. When the
            # tree's exit could not be observed the record stays pending so the
            # owner (or the next process's reconciliation) can still reap it.
            bg_cmd.stream_callback = None
            if unproven_cleanup:
                bg_cmd.cleanup_pending = True
                bg_cmd.cleanup_reason = unproven_cleanup
                bg_cmd.cleanup_requested_at = bg_cmd.cleanup_requested_at or time.time()
                bg_cmd.cleanup_completed_at = None
            elif bg_cmd.cleanup_requested_at is not None:
                bg_cmd.cleanup_pending = False
                bg_cmd.cleanup_completed_at = time.time()
            bg_cmd.close_task_output()
            if bg_cmd.output_write_error and bg_cmd.status != "cancelled":
                bg_cmd.status = "failed"
                bg_cmd.command_completed = False
                bg_cmd.exit_code = -1
                bg_cmd.transition(
                    phase="completed",
                    status="failed",
                    error={
                        "kind": "output_persistence_error",
                        "message": bg_cmd.output_write_error,
                    },
                )
            # A background shell may create, delete, rename, or rewrite files
            # after the run_command tool has already returned.  Invalidate
            # shared workspace views at the terminal boundary so the next
            # read/search observes the completed command rather than a stale
            # cache entry.  This is deliberately broad because shell syntax
            # is not safely parsed here.
            from backend.tools.file_tools_common import invalidate_workspace_file_caches

            # Admission captured the actual write authority, including extra
            # writable roots and any full-disk-write grant.
            if not isinstance(bg_cmd.sandbox_policy, EnvironmentCommandPolicy):
                invalidate_workspace_file_caches(file_tree_changed=True, clear_file_state=True,
                    mutation_policy=bg_cmd.sandbox_policy)
            await self._settle_cleanup(bg_cmd)
            await self._notify_completed_once(bg_cmd)

    async def _settle_cleanup(self, bg_cmd: BackgroundCommand) -> None:
        if isinstance(bg_cmd.runner, ExecutorCommandRunner):
            bg_cmd.network_audit = list(bg_cmd.runner.network_audit)
        # A surviving process tree keeps its durable owner record: the UI
        # may show a terminal status, but the reaper still needs the PID
        # identity. Commit that evidence before the notification so a crash
        # in between cannot lose it.
        if self._session_id and bg_cmd.cleanup_pending:
            try:
                from backend.terminal.task_persistence import save_task

                save_task(
                    session_id=self._session_id,
                    task_id=bg_cmd.command_id,
                    command=bg_cmd.command,
                    description=bg_cmd.description,
                    cwd=bg_cmd.cwd,
                    pid=bg_cmd.pid,
                    started_at=bg_cmd.started_at,
                    timeout_ms=bg_cmd.timeout_ms,
                    status="interrupted",
                    conversation_id=bg_cmd.conversation_id,
                    owner_task_id=bg_cmd.owner_task_id,
                    parent_run_id=bg_cmd.parent_run_id,
                    environment_id=bg_cmd.environment_id, executor_remote=bg_cmd.executor_remote,
                    executor_process_id=bg_cmd.executor_process_id, executor_session_id=bg_cmd.executor_session_id,
                    network_audit=bg_cmd.network_audit,
                    process_start_time=bg_cmd.process_start_time,
                    cleanup_pending=True,
                    cleanup_reason=bg_cmd.cleanup_reason,
                    cleanup_requested_at=bg_cmd.cleanup_requested_at,
                    **(bg_cmd.runner.container_ownership if bg_cmd.runner is not None else {}),
                )
            except Exception as exc:
                bg_cmd.cleanup_error = {
                    "kind": "owner_persistence_failed",
                    "message": str(exc),
                }
                bg_cmd.transition(
                    phase="cleanup_pending",
                    status="interrupted",
                    error=dict(bg_cmd.cleanup_error),
                )
                logger.exception(
                    "Unreaped background command %s could not be durably recorded",
                    bg_cmd.command_id,
                )
        # The durable record is the recovery handle for a live process. Drop
        # it only once this command owns no process that could outlive it.
        # Settle this before the completion projection so persistence
        # failures are part of the same canonical terminal evidence.
        if self._session_id and not bg_cmd.cleanup_pending:
            try:
                from backend.terminal.task_persistence import delete_task
                delete_task(self._session_id, bg_cmd.command_id)
            except Exception as exc:
                bg_cmd.cleanup_pending = True
                bg_cmd.cleanup_reason = "owner_record_delete_failed"
                bg_cmd.cleanup_requested_at = bg_cmd.cleanup_requested_at or time.time()
                bg_cmd.cleanup_completed_at = None
                bg_cmd.cleanup_error = {
                    "kind": "owner_record_delete_failed",
                    "message": str(exc),
                }
                bg_cmd.transition(
                    phase="cleanup_pending",
                    status=bg_cmd.status,
                    error=dict(bg_cmd.cleanup_error),
                )
                logger.error(
                    "Task persistence delete failed for %s; retaining in-memory evidence: %s",
                    bg_cmd.command_id,
                    exc,
                    exc_info=True,
                )
        if not bg_cmd.cleanup_pending:
            self._processes.pop(bg_cmd.command_id, None)
            self._stdin_locks.pop(bg_cmd.command_id, None)
            bg_cmd.runner = None

    async def retain_foreground_cleanup(
        self, *, runner: SandboxRunner, command: str, cwd: str,
        conversation_id: str, task_id: str, parent_run_id: str, reason: str,
    ) -> BackgroundCommand:
        """Keep a foreground command's unfinished cleanup in the existing owner."""
        from backend.terminal.task_persistence import get_process_start_time

        process = runner.process
        command_id = f"bg_{uuid.uuid4().hex[:8]}"
        now = time.time()
        bg_cmd = BackgroundCommand(
            command_id=command_id, command=command, cwd=cwd,
            conversation_id=conversation_id, owner_task_id=task_id,
            parent_run_id=parent_run_id, status="interrupted",
            started_at=now, completed_at=now, runner=runner,
            pid=process.pid if process is not None else None,
            process_start_time=get_process_start_time(process.pid) if process is not None else None,
            cleanup_pending=True, cleanup_reason=reason, cleanup_requested_at=now,
            environment_id=runner.policy.scope.environment_id if isinstance(runner, ExecutorCommandRunner) else "local",
            executor_remote=isinstance(runner, ExecutorCommandRunner) and runner.policy.attachment.is_remote,
            executor_process_id=runner.handle.process_id if isinstance(runner, ExecutorCommandRunner) and runner.handle is not None else "",
            executor_session_id=runner.handle.client.session_id if isinstance(runner, ExecutorCommandRunner) and runner.policy.attachment.is_remote and runner.handle is not None else "",
        )
        self._commands[command_id] = bg_cmd
        if process is not None:
            self._processes[command_id] = process
        await self._settle_cleanup(bg_cmd)
        return bg_cmd

    def get_status(self, command_id: str, *, conversation_id: str) -> BackgroundCommand | None:
        owner = str(conversation_id or "").strip()
        command = self._commands.get(command_id)
        if not owner or command is None or command.conversation_id != owner:
            return None
        return command

    async def wait_for_startup(self, command_id: str, *, conversation_id: str) -> BackgroundCommand:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            raise KeyError(command_id)
        try:
            await command.startup_ready.wait()
        except asyncio.CancelledError:
            await self.detach(command_id, conversation_id=conversation_id)
            raise
        return command

    async def wait(
        self, command_id: str, *, conversation_id: str, wait_ms: int,
        cursor: int | None = None, cancel_event: asyncio.Event | None = None,
    ) -> BackgroundCommand:
        """Wait on owned output/completion without cancelling the process on timeout."""
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            raise KeyError(command_id)
        lock = self._stdin_locks.setdefault(command_id, asyncio.Lock())
        async with lock:
            if self.get_status(command_id, conversation_id=conversation_id) is not command:
                raise StdinAuthorityError("the terminal instance changed while this interaction was pending; submit it again")
            return await self._wait_for_output(command, wait_ms=wait_ms, cursor=cursor, cancel_event=cancel_event)

    async def _wait_for_output(self, command: BackgroundCommand, *, wait_ms: int,
                               cursor: int | None, cancel_event: asyncio.Event | None) -> BackgroundCommand:
        deadline = asyncio.get_running_loop().time() + wait_ms / 1000
        while command.status == "running":
            command.output_changed.clear()
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            if cursor is not None and command.output_bytes > cursor:
                break
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            changed = asyncio.create_task(command.output_changed.wait())
            cancelled = asyncio.create_task(cancel_event.wait()) if cancel_event is not None else None
            waiters = {changed, *([cancelled] if cancelled is not None else [])}
            try:
                done, _ = await asyncio.wait(waiters, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
                if cancelled is not None and cancelled in done:
                    raise asyncio.CancelledError
                if not done:
                    break
            finally:
                for waiter in waiters:
                    waiter.cancel()
                await asyncio.gather(*waiters, return_exceptions=True)
        return command

    async def detach(self, command_id: str, *, conversation_id: str) -> None:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            raise KeyError(command_id)
        if command.status == "running" and not command.backgrounded:
            command.backgrounded = True
            command.stream_callback = None
            if command.pid is not None and self._on_started is not None:
                await self._on_started(command)

    def get_output_chunk(
        self, command_id: str, *, conversation_id: str, cursor: int, max_chars: int,
    ) -> tuple[str, int, bool, str] | None:
        from backend.terminal.task_output import read_task_output_chunk

        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            return None
        text, next_cursor, more = read_task_output_chunk(command.output_path, cursor, max_chars)
        return text, next_cursor, more, command.output_path

    def get_output_preview(
        self, command_id: str, *, conversation_id: str, max_chars: int,
    ) -> tuple[str, int, bool, int] | None:
        from backend.terminal.task_output import read_task_output_preview

        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            return None
        return read_task_output_preview(command.output_path, max_chars)

    def get_output(self, command_id: str, *, conversation_id: str) -> str | None:
        cmd = self.get_status(command_id, conversation_id=conversation_id)
        return cmd.output if cmd else None

    def get_output_snapshot(
        self,
        command_id: str,
        *,
        conversation_id: str,
        max_chars: int,
    ) -> tuple[str, bool, str] | None:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            return None
        if command.output_path:
            try:
                content, truncated = format_task_output(
                    command.output_path,
                    max_chars,
                )
                return content, truncated, command.output_path
            except OSError as exc:
                logger.debug(
                    "Background output read failed for %s: %s",
                    command.command_id,
                    exc,
                )
        output = command.output
        clipped = len(output) > max_chars
        truncated = clipped or command.output_chars > len(output)
        return (output[-max_chars:] if clipped else output), truncated, ""

    async def cancel(self, command_id: str, *, conversation_id: str) -> bool:
        owner = str(conversation_id or "").strip()
        command = self._commands.get(command_id)
        if not owner or command is None or command.conversation_id != owner:
            return False
        return await self._cancel_unscoped(command_id)

    async def write_stdin(
        self,
        command_id: str,
        chars: str,
        *,
        conversation_id: str,
        close_stdin: bool = False,
        context: Any = None,
        authorize_input: Callable[[TerminalInputApproval], Any] | None = None,
        wait_ms: int = 0,
        cursor: int | None = None,
    ) -> int:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            raise KeyError(command_id)
        interacts = bool(chars) or close_stdin
        interrupt = not command.tty and chars == "\x03"
        if interacts and command.status != "running":
            raise RuntimeError(
                f"Background command {command_id} is {command.status}; stdin is closed."
            )
        process = self._processes.get(command_id)
        if close_stdin and isinstance(process, ExecutorCommandProcess):
            raise StdinAuthorityError("The executor process protocol does not support closing stdin independently; no input was sent.")
        if context is not None and isinstance(process, ExecutorCommandProcess):
            from backend.agent.environment_selection import select_turn_environment

            snapshot = context.run_context.turn_environments if context.run_context is not None else None
            if snapshot is not None:
                selected = select_turn_environment(snapshot, command.environment_id)
            elif context.turn_environment is not None and context.turn_environment.selection.environment_id == command.environment_id:
                selected = context.turn_environment
            else:
                raise StdinAuthorityError("cannot access the terminal's original environment; attach it before retrying")
            context = replace(context, turn_environment=selected, environment_id=command.environment_id,
                permission=replace(context.permission, environment_scope=selected.permission_scope))
        if interacts and (process is None or process.returncode is not None or (not interrupt and process.stdin is None)):
            raise RuntimeError(
                f"Background command {command_id} has no writable stdin channel."
            )
        payload = chars.encode("utf-8")
        lock = self._stdin_locks.setdefault(command_id, asyncio.Lock())
        async with lock:
            if interacts:
                self._verify_interaction_instance(command_id, command, process, conversation_id, interrupt=interrupt)
            elif self.get_status(command_id, conversation_id=conversation_id) is not command:
                raise StdinAuthorityError("the terminal instance changed while this interaction was pending; submit it again")
            if context is not None and context.cancel_event is not None and context.cancel_event.is_set():
                raise asyncio.CancelledError
            if chars and context is not None and command.terminal_authority is not None:
                from backend.tools.command_support import current_terminal_policy, stdin_approval_enabled

                if stdin_approval_enabled(context) and not (not command.tty and chars == "\x03"):
                    authority = command.terminal_authority
                    current_policy = current_terminal_policy(context)
                    intent = authority.review_requirement(current_policy, environment_id=context.environment_id)
                    strict = context.permission.strict_auto_review or context.permission_grants.strict_auto_review_enabled(context.turn_id)
                    if intent != "use_default" or strict:
                        action = TerminalInputApproval(command.original_tool_call_id, context.tool_call_id,
                            authority.environment_id, command_id,
                            process.process_id if isinstance(process, ExecutorCommandProcess) else process.pid, chars, command.cwd,
                            command.tty, intent, authority.additional_permissions, authority.approval_reason(intent))
                        action.verify_reviewable()
                        if authorize_input is None:
                            raise StdinAuthorityError("terminal input requires approval but no approval channel is available")
                        await authorize_input(action)
                        self._verify_interaction_instance(command_id, command, process, conversation_id, interrupt=interrupt)
            if context is not None and context.cancel_event is not None and context.cancel_event.is_set():
                raise asyncio.CancelledError
            try:
                if interrupt:
                    if isinstance(process, ExecutorCommandProcess):
                        await process.interrupt()
                    elif os.name == "nt":
                        reaped = await command.runner._kill_tree(process)
                        if not reaped:
                            command.cleanup_pending = True
                            command.cleanup_reason = "process_tree_survived_interrupt"
                            command.cleanup_requested_at = time.time()
                            raise StdinAuthorityError("terminal input interrupt could not confirm the owned process tree exited")
                    else:
                        os.killpg(process.pid, signal.SIGINT)
                elif payload:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    if command.tty:
                        await asyncio.sleep(0.1)
                if close_stdin:
                    process.stdin.close()
                    await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise RuntimeError(
                    f"Background command {command_id} closed stdin before the write completed."
                ) from exc
            await self._wait_for_output(command, wait_ms=wait_ms, cursor=cursor,
                                        cancel_event=context.cancel_event if context is not None else None)
        return len(payload)

    def _verify_interaction_instance(self, command_id, command, process, conversation_id, *, interrupt=False):
        if self.get_status(command_id, conversation_id=conversation_id) is not command or self._processes.get(command_id) is not process:
            raise StdinAuthorityError("the terminal instance changed while this interaction was pending; submit it again")
        if command.status != "running" or process.returncode is not None or (not interrupt and process.stdin is None):
            raise StdinAuthorityError(f"Background command {command_id} is {command.status}; stdin is closed.")

    def terminal_hook_payload(self, command_id: str, *, conversation_id: str, output: str) -> dict[str, Any]:
        command = self.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            raise KeyError(command_id)
        payload = {"command_id": command_id, "ready": False}
        if not command.command_completed or command.status == "running" or command.cleanup_pending or command.completion_hook_claimed or not command.original_tool_call_id:
            return payload
        command.completion_hook_claimed = True
        return {**payload, "ready": True, "tool_name": "Bash", "tool_use_id": command.original_tool_call_id,
                "tool_input": {"command": command.command}, "output": output}

    async def _cancel_unscoped(self, command_id: str) -> bool:
        task = self._tasks.get(command_id)
        cmd = self._commands.get(command_id)
        # The process can finish before its terminal notification is delivered.
        # Cancelling that notifier must not reopen its proven terminal state.
        if cmd is not None and cmd.status != "running" and not cmd.cleanup_pending:
            return False
        if task and not task.done():
            if cmd:
                cmd.cleanup_pending = True
                cmd.cleanup_reason = "cancel_requested"
                cmd.cleanup_requested_at = time.time()
                cmd.cleanup_completed_at = None
                cmd.transition(phase="cancelling", status="running")
                if self._session_id:
                    try:
                        from backend.terminal.task_persistence import save_task

                        save_task(
                            session_id=self._session_id,
                            task_id=cmd.command_id,
                            command=cmd.command,
                            description=cmd.description,
                            cwd=cmd.cwd,
                            pid=cmd.pid,
                            started_at=cmd.started_at,
                            timeout_ms=cmd.timeout_ms,
                            status="running",
                            conversation_id=cmd.conversation_id,
                            owner_task_id=cmd.owner_task_id,
                            parent_run_id=cmd.parent_run_id,
                            environment_id=cmd.environment_id, executor_remote=cmd.executor_remote,
                            executor_process_id=cmd.executor_process_id, executor_session_id=cmd.executor_session_id,
                            network_audit=cmd.network_audit,
                            process_start_time=cmd.process_start_time,
                            cleanup_pending=True,
                            cleanup_reason=cmd.cleanup_reason,
                            cleanup_requested_at=cmd.cleanup_requested_at,
                            **(cmd.runner.container_ownership if cmd.runner is not None else {}),
                        )
                    except Exception as exc:
                        logger.debug("Background cleanup intent persistence failed: %s", exc)
            receipt = await cancel_and_drain_receipt(
                [task],
                timeout=CANCELLATION_DRAIN_TIMEOUT_SECONDS,
                label=f"background command {command_id}",
            )
            if cmd and not receipt.completed:
                cmd.cleanup_reason = "process_cleanup_pending"
            elif cmd:
                # A worker cancelled before its first step never creates a
                # runner or process and therefore has no coroutine finalizer.
                if cmd.status == "running" and (cmd.runner is None or isinstance(cmd.runner, ExecutorCommandRunner) and cmd.runner.handle is None):
                    if cmd.runner is not None:
                        await cmd.runner.cleanup()
                    cmd.status = "cancelled"
                    cmd.completed_at = time.time()
                    cmd.transition(phase="cancelled", status="cancelled")
                    cmd.cleanup_pending = False
                    cmd.cleanup_completed_at = cmd.cleanup_completed_at or time.time()
                    await self._settle_cleanup(cmd)
                await self._notify_completed_once(cmd)
            return True
        if cmd is not None and cmd.cleanup_pending:
            cmd.cleanup_requested_at = time.time()
            completed = await cmd.runner.cleanup() if cmd.runner is not None else True
            cmd.cleanup_pending = not completed
            cmd.cleanup_reason = "" if completed else "command_resource_cleanup_pending"
            cmd.cleanup_completed_at = time.time() if completed else None
            cmd.transition(phase="completed" if completed else "cleanup_pending", status=cmd.status)
            await self._settle_cleanup(cmd)
            return True
        return False

    async def destroy_for_conversation(self, conversation_id: str) -> int:
        """Cancel, drain, and forget every background command for one chat."""
        owner = str(conversation_id or "").strip()
        if not owner:
            return 0
        command_ids = [
            command_id
            for command_id, command in self._commands.items()
            if command.conversation_id == owner
        ]
        if command_ids:
            await asyncio.gather(
                *(self._cancel_unscoped(command_id) for command_id in command_ids),
                return_exceptions=False,
            )
        removed_ids: list[str] = []
        for command_id in command_ids:
            command = self._commands.get(command_id)
            task = self._tasks.get(command_id)
            if command is None or command.cleanup_pending or (task is not None and not task.done()):
                continue
            if command is not None and command.output_path:
                try:
                    delete_task_output(command.output_path)
                except OSError as exc:
                    command.cleanup_pending = True
                    command.cleanup_reason = "output_cleanup_failed"
                    command.cleanup_requested_at = command.cleanup_requested_at or time.time()
                    command.cleanup_completed_at = None
                    command.cleanup_error = {
                        "kind": "output_cleanup_failed",
                        "message": str(exc),
                    }
                    command.transition(
                        phase="cleanup_pending",
                        status=command.status,
                        error=dict(command.cleanup_error),
                    )
                    continue
            self._tasks.pop(command_id, None)
            self._commands.pop(command_id, None)
            self._completion_notified.discard(command_id)
            removed_ids.append(command_id)
        if self._session_id and len(removed_ids) == len(command_ids):
            try:
                cleanup_task_output_owner(self._session_id, owner)
            except (OSError, ValueError) as exc:
                logger.debug(
                    "Background owner output cleanup failed for %s: %s",
                    owner,
                    exc,
                )
        pending_ids = [
            command_id for command_id in command_ids if command_id not in set(removed_ids)
        ]
        if pending_ids:
            # Raise like the sibling teardown APIs (TerminalSessionManager
            # .destroy_sessions_for_conversation, preview _stop_preview_processes).
            # Returning a short count let conversation.delete read "stopped" and
            # go on to remove the conversation and its worktree while a process
            # whose exit was never proven may still be writing into it.
            raise RuntimeError(
                "Background commands could not be proven stopped: "
                + ", ".join(sorted(pending_ids))
            )
        return len(removed_ids)

    def list_commands(
        self,
        include_completed: bool = False,
        *,
        conversation_id: str,
    ) -> list[dict[str, Any]]:
        owner = str(conversation_id or "").strip()
        if not owner:
            return []
        commands = []
        for cmd in self._commands.values():
            if cmd.conversation_id != owner:
                continue
            if not include_completed and cmd.status not in {"running"}:
                continue
            commands.append(cmd.to_dict())
        return commands

    def _cleanup_completed(self) -> None:
        """Drop settled commands after 30 minutes, keeping unproven ones.

        ``cleanup_pending`` means the process tree's exit was never proven, so
        the record is the only reaping handle left. Its siblings
        (``destroy_for_conversation``, ``shutdown``) already refuse to
        deregister those; dropping them here silently discarded the evidence and
        deleted the output file a reaper would need.
        """
        now = time.time()
        stale = [
            cid
            for cid, cmd in self._commands.items()
            if cmd.status != "running"
            and not cmd.cleanup_pending
            and cmd.completed_at
            and (now - cmd.completed_at) > 1800
        ]
        for cid in stale:
            command = self._commands.get(cid)
            if command is not None and command.output_path:
                try:
                    delete_task_output(command.output_path)
                except OSError as exc:
                    command.cleanup_pending = True
                    command.cleanup_reason = "output_cleanup_failed"
                    command.cleanup_requested_at = command.cleanup_requested_at or time.time()
                    command.cleanup_completed_at = None
                    command.cleanup_error = {
                        "kind": "output_cleanup_failed",
                        "message": str(exc),
                    }
                    command.transition(
                        phase="cleanup_pending",
                        status=command.status,
                        error=dict(command.cleanup_error),
                    )
                    logger.error(
                        "Background output cleanup failed for %s; retaining recovery record: %s",
                        cid,
                        exc,
                        exc_info=True,
                    )
                    continue
            self._commands.pop(cid, None)
            self._tasks.pop(cid, None)
            self._completion_notified.discard(cid)

    async def shutdown(self) -> None:
        """Request cancellation while retaining every unsettled resource owner."""
        await asyncio.gather(
            *(self._cancel_unscoped(command_id) for command_id in list(self._tasks)),
            return_exceptions=True,
        )
        settled = [
            command_id
            for command_id, task in self._tasks.items()
            if task.done()
            and not bool(getattr(self._commands.get(command_id), "cleanup_pending", False))
        ]
        for command_id in settled:
            command = self._commands.get(command_id)
            if command is not None and command.output_path:
                try:
                    delete_task_output(command.output_path)
                except OSError as exc:
                    if command is not None:
                        command.cleanup_pending = True
                        command.cleanup_reason = "output_cleanup_failed"
                        command.cleanup_requested_at = command.cleanup_requested_at or time.time()
                        command.cleanup_completed_at = None
                        command.cleanup_error = {
                            "kind": "output_cleanup_failed",
                            "message": str(exc),
                        }
                        command.transition(
                            phase="cleanup_pending",
                            status=command.status,
                            error=dict(command.cleanup_error),
                        )
                    logger.error(
                        "Background output cleanup failed for %s; retaining recovery record: %s",
                        command_id,
                        exc,
                        exc_info=True,
                    )
                    continue
            self._tasks.pop(command_id, None)
            self._commands.pop(command_id, None)
            self._completion_notified.discard(command_id)
