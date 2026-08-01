"""
后台命令管理器（参考 Claude Code 的 run_in_background 功能）。

核心类：
  - BackgroundCommand: 后台命令状态数据类
  - BackgroundCommandManager: 管理后台执行的命令，完成时推送通知
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Coroutine

from backend.agent.execution_lifecycle import ExecutionLifecycle
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.tools.base import MAX_TOOL_RESULT_BYTES, truncate_text_tail

logger = logging.getLogger(__name__)

MAX_BACKGROUND_COMMANDS = 10
MAX_BACKGROUND_OUTPUT = MAX_TOOL_RESULT_BYTES


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
    sandbox_backend: str = "full-access"
    effective_command: str = field(default="", repr=False)
    sandbox_policy: SandboxPolicy | None = field(default=None, repr=False)
    lifecycle: ExecutionLifecycle | None = field(default=None, repr=False)

    def append_live_output(self, piece: str) -> None:
        """Keep a bounded live tail for Monitor while the process is running."""
        if not piece:
            return
        combined = f"{self.output}{piece}"
        self.output = truncate_text_tail(combined, max_bytes=MAX_BACKGROUND_OUTPUT).content

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
            "sandbox_backend": self.sandbox_backend,
            "output_length": len(self.output),
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
        self._completion_notified: set[str] = set()
        self._on_completed = on_completed
        self._on_started = on_started
        self._max_commands = max_commands
        self._session_id = session_id or ""

    def cleanup_orphaned_tasks_on_startup(self) -> list[dict[str, Any]]:
        """
        Scan persisted tasks on session startup. Return list of orphaned tasks
        (process dead but state says running) for caller to emit notifications.
        """
        if not self._session_id:
            return []

        from backend.terminal.task_persistence import cleanup_orphaned_tasks
        try:
            orphaned = cleanup_orphaned_tasks(self._session_id)
            return [
                {
                    "task_id": task.task_id,
                    "command": task.command,
                    "description": task.description,
                    "pid": task.pid,
                    "started_at": task.started_at,
                    "status": "interrupted",
                    "reason": "background_owner_exited",
                }
                for task in orphaned
            ]
        except Exception as exc:
            logger.debug(f"Orphaned task cleanup failed: {exc}")
            return []

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
    ) -> BackgroundCommand:
        """在后台执行命令，立即返回 BackgroundCommand。"""
        owner = str(conversation_id or "").strip()
        if not owner:
            raise RuntimeError(
                "Background commands require a conversation owner; the command was not started."
            )
        # 清理已完成的命令
        self._cleanup_completed()

        running = [c for c in self._commands.values() if c.status == "running"]
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
        runner = SandboxRunner(sandbox_policy)
        capability = runner.capability()
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
        bg_cmd = BackgroundCommand(
            command_id=command_id,
            command=display_command,
            description=description or display_command[:60],
            cwd=cwd or os.getcwd(),
            status="running",
            started_at=time.time(),
            timeout_ms=timeout_ms,
            conversation_id=owner,
            sandbox_backend=sandbox_backend,
            effective_command=effective_command or command,
            sandbox_policy=sandbox_policy,
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
        self._commands[command_id] = bg_cmd

        task = asyncio.create_task(self._execute(bg_cmd))
        self._tasks[command_id] = task

        # Persist task state for cross-session recovery
        if self._session_id:
            from backend.terminal.task_persistence import save_task
            try:
                save_task(
                    session_id=self._session_id,
                    task_id=command_id,
                    command=display_command,
                    description=bg_cmd.description,
                    cwd=bg_cmd.cwd,
                    pid=None,  # Will be set after process starts
                    started_at=bg_cmd.started_at,
                    timeout_ms=timeout_ms,
                    status="running",
                )
            except Exception as exc:
                logger.debug(f"Task persistence save failed: {exc}")

        logger.info("Background command started: %s (id: %s)", display_command[:60], command_id)
        return bg_cmd

    async def _notify_completed_once(self, bg_cmd: BackgroundCommand) -> None:
        """Deliver exactly one terminal callback, even across cancel races."""

        if bg_cmd.command_id in self._completion_notified:
            return
        self._completion_notified.add(bg_cmd.command_id)
        if self._on_completed:
            try:
                await self._on_completed(bg_cmd)
            except Exception as exc:
                logger.debug("Background completion callback failed: %s", exc)

    async def _execute(self, bg_cmd: BackgroundCommand) -> None:
        """执行后台命令。"""
        try:
            timeout_seconds = (
                max(1, int((bg_cmd.timeout_ms + 999) / 1000))
                if bg_cmd.timeout_ms > 0
                else 0
            )
            policy = bg_cmd.sandbox_policy
            if policy is None:
                raise RuntimeError("Background command has no sandbox policy")
            runner = SandboxRunner(policy)
            host_command = normalize_windows_shell_command(
                bg_cmd.effective_command or bg_cmd.command
            )

            async def _process_started(pid: int) -> None:
                bg_cmd.transition(phase="running", status="running")
                # Update persisted task only after the OS process exists.
                if self._session_id and pid:
                    from backend.terminal.task_persistence import save_task
                    try:
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
                        )
                    except Exception as exc:
                        logger.debug(f"Task persistence PID update failed: {exc}")
                if self._on_started:
                    try:
                        await self._on_started(bg_cmd)
                    except Exception as exc:
                        logger.debug("Background start callback failed: %s", exc)

            async def _stream_output(piece: str, _stream_name: str = "stdout") -> None:
                # Codex exposes running exec-session output through polling and
                # Claude Code writes background shell output continuously. Keep
                # the same owned task record current so Monitor can do both.
                bg_cmd.append_live_output(piece)

            result = await runner.run(
                bg_cmd.command,
                cwd=bg_cmd.cwd,
                host_command=host_command,
                process_started_callback=_process_started,
                stream_callback=_stream_output,
            )

            if result.cancelled:
                bg_cmd.status = "cancelled"
                bg_cmd.exit_code = -1
                bg_cmd.completed_at = time.time()
                bg_cmd.transition(phase="cancelled", status="cancelled")
                return
            if result.sandbox_unavailable:
                bg_cmd.status = "failed"
                bg_cmd.output = result.stderr
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
                bg_cmd.output = f"命令执行超时（{bg_cmd.timeout_ms / 1000.0:.0f}秒）"
                bg_cmd.exit_code = -1
                bg_cmd.completed_at = time.time()
                bg_cmd.transition(
                    phase="completed",
                    status="failed",
                    error={"kind": "timeout", "message": bg_cmd.output},
                )
                return

            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}" if output else result.stderr

            output = truncate_text_tail(output, max_bytes=MAX_BACKGROUND_OUTPUT).content

            bg_cmd.output = output
            bg_cmd.exit_code = result.exit_code
            bg_cmd.status = "completed" if result.exit_code == 0 else "failed"
            bg_cmd.completed_at = time.time()
            bg_cmd.transition(
                phase="completed",
                status=bg_cmd.status,
                result={"exit_code": bg_cmd.exit_code, "output_length": len(bg_cmd.output)},
                error=(
                    {"kind": "nonzero_exit", "exit_code": bg_cmd.exit_code}
                    if bg_cmd.status == "failed"
                    else {}
                ),
            )

        except asyncio.CancelledError:
            bg_cmd.status = "cancelled"
            bg_cmd.completed_at = time.time()
            bg_cmd.transition(phase="cancelled", status="cancelled")
        except Exception as exc:
            bg_cmd.status = "failed"
            bg_cmd.output = f"执行错误: {exc}"
            bg_cmd.exit_code = -1
            bg_cmd.completed_at = time.time()
            bg_cmd.transition(
                phase="completed",
                status="failed",
                error={"kind": "runtime_error", "message": str(exc)},
            )
            logger.error("Background command %s failed: %s", bg_cmd.command_id, exc)

        finally:
            # Every terminal state (success, failure, timeout, or cancellation)
            # must update the UI; previously cancellation silently disappeared.
            await self._notify_completed_once(bg_cmd)

            # Delete persisted task state on completion
            if self._session_id:
                try:
                    from backend.terminal.task_persistence import delete_task
                    delete_task(self._session_id, bg_cmd.command_id)
                except Exception as exc:
                    logger.debug(f"Task persistence delete failed: {exc}")

    def get_status(self, command_id: str, *, conversation_id: str) -> BackgroundCommand | None:
        owner = str(conversation_id or "").strip()
        command = self._commands.get(command_id)
        if not owner or command is None or command.conversation_id != owner:
            return None
        return command

    def get_output(self, command_id: str, *, conversation_id: str) -> str | None:
        cmd = self.get_status(command_id, conversation_id=conversation_id)
        return cmd.output if cmd else None

    async def cancel(self, command_id: str, *, conversation_id: str) -> bool:
        owner = str(conversation_id or "").strip()
        command = self._commands.get(command_id)
        if not owner or command is None or command.conversation_id != owner:
            return False
        return await self._cancel_unscoped(command_id)

    async def _cancel_unscoped(self, command_id: str) -> bool:
        task = self._tasks.get(command_id)
        if task and not task.done():
            task.cancel()
            cmd = self._commands.get(command_id)
            if cmd:
                cmd.status = "cancelled"
                cmd.completed_at = time.time()
                cmd.transition(phase="cancelling", status="cancelled")
                try:
                    # SandboxRunner owns the bounded platform-specific process
                    # teardown. Do not impose a shorter outer timeout that can
                    # strand its Windows Proactor readers and kill-tree task.
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await self._notify_completed_once(cmd)
            return True
        return False

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
        """清理超过 30 分钟的已完成命令。"""
        now = time.time()
        stale = [
            cid
            for cid, cmd in self._commands.items()
            if cmd.status != "running" and cmd.completed_at and (now - cmd.completed_at) > 1800
        ]
        for cid in stale:
            self._commands.pop(cid, None)
            self._tasks.pop(cid, None)
            self._completion_notified.discard(cid)

    async def shutdown(self) -> None:
        """取消所有运行中的后台命令。"""
        await asyncio.gather(
            *(self._cancel_unscoped(command_id) for command_id in list(self._tasks)),
            return_exceptions=True,
        )
        self._tasks.clear()
        self._commands.clear()
