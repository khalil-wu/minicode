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
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Coroutine

from backend.runtime_env import sanitized_subprocess_env
from backend.terminal.shell_commands import normalize_windows_shell_command

logger = logging.getLogger(__name__)

MAX_BACKGROUND_COMMANDS = 10
MAX_BACKGROUND_OUTPUT = 50_000  # 后台命令最大输出字符数


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
    timeout_ms: int = 600_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command": self.command,
            "description": self.description,
            "cwd": self.cwd,
            "status": self.status,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "output_length": len(self.output),
        }


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
        max_commands: int = MAX_BACKGROUND_COMMANDS,
        session_id: str | None = None,
    ) -> None:
        self._commands: dict[str, BackgroundCommand] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._on_completed = on_completed
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
        timeout_ms: int = 600_000,
        description: str = "",
    ) -> BackgroundCommand:
        """在后台执行命令，立即返回 BackgroundCommand。"""
        # 清理已完成的命令
        self._cleanup_completed()

        running = [c for c in self._commands.values() if c.status == "running"]
        if len(running) >= self._max_commands:
            raise RuntimeError(
                f"后台命令数已达上限（{self._max_commands}）。"
                "请等待现有命令完成或取消后重试。"
            )

        command_id = f"bg_{uuid.uuid4().hex[:8]}"
        bg_cmd = BackgroundCommand(
            command_id=command_id,
            command=command,
            description=description or command[:60],
            cwd=cwd or os.getcwd(),
            status="running",
            started_at=time.time(),
            timeout_ms=timeout_ms,
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
                    command=command,
                    description=bg_cmd.description,
                    cwd=bg_cmd.cwd,
                    pid=None,  # Will be set after process starts
                    started_at=bg_cmd.started_at,
                    timeout_ms=timeout_ms,
                    status="running",
                )
            except Exception as exc:
                logger.debug(f"Task persistence save failed: {exc}")

        logger.info("Background command started: %s (id: %s)", command[:60], command_id)
        return bg_cmd

    async def _execute(self, bg_cmd: BackgroundCommand) -> None:
        """执行后台命令。"""
        proc = None
        try:
            # Normalize Windows shell commands (e.g., curl -> curl.exe to avoid PowerShell alias)
            normalized_command = normalize_windows_shell_command(bg_cmd.command)

            process_kwargs: dict[str, Any] = {}
            if os.name == "nt":
                process_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                process_kwargs["start_new_session"] = True

            proc = await asyncio.shield(asyncio.create_subprocess_shell(
                normalized_command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=bg_cmd.cwd,
                env=sanitized_subprocess_env(),
                **process_kwargs,
            ))

            # Update persisted task with PID
            if self._session_id and proc.pid:
                from backend.terminal.task_persistence import save_task
                try:
                    save_task(
                        session_id=self._session_id,
                        task_id=bg_cmd.command_id,
                        command=bg_cmd.command,
                        description=bg_cmd.description,
                        cwd=bg_cmd.cwd,
                        pid=proc.pid,
                        started_at=bg_cmd.started_at,
                        timeout_ms=bg_cmd.timeout_ms,
                        status="running",
                    )
                except Exception as exc:
                    logger.debug(f"Task persistence PID update failed: {exc}")

            timeout_sec = bg_cmd.timeout_ms / 1000.0
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                try:
                    if os.name == "nt":
                        killer = await asyncio.create_subprocess_exec(
                            "taskkill", "/PID", str(proc.pid), "/T", "/F",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await killer.communicate()
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                bg_cmd.status = "failed"
                bg_cmd.output = f"命令执行超时（{timeout_sec:.0f}秒）"
                bg_cmd.exit_code = -1
                bg_cmd.completed_at = time.time()

                if self._on_completed:
                    await self._on_completed(bg_cmd)
                return

            stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
            stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}" if output else stderr

            # 截断保护
            if len(output) > MAX_BACKGROUND_OUTPUT:
                output = output[:MAX_BACKGROUND_OUTPUT] + f"\n\n[输出已截断，共 {len(output)} 字符]"

            bg_cmd.output = output
            bg_cmd.exit_code = proc.returncode or 0
            bg_cmd.status = "completed" if bg_cmd.exit_code == 0 else "failed"
            bg_cmd.completed_at = time.time()

        except asyncio.CancelledError:
            bg_cmd.status = "cancelled"
            bg_cmd.completed_at = time.time()
            if proc is not None:
                try:
                    if os.name == "nt":
                        killer = await asyncio.create_subprocess_exec(
                            "taskkill", "/PID", str(proc.pid), "/T", "/F",
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=asyncio.subprocess.DEVNULL,
                        )
                        await killer.communicate()
                    else:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            raise
        except Exception as exc:
            bg_cmd.status = "failed"
            bg_cmd.output = f"执行错误: {exc}"
            bg_cmd.exit_code = -1
            bg_cmd.completed_at = time.time()
            logger.error("Background command %s failed: %s", bg_cmd.command_id, exc)

        # 推送完成通知
        if self._on_completed:
            try:
                await self._on_completed(bg_cmd)
            except Exception as exc:
                logger.debug("Background completion callback failed: %s", exc)

        # Delete persisted task state on completion
        if self._session_id:
            from backend.terminal.task_persistence import delete_task
            try:
                delete_task(self._session_id, bg_cmd.command_id)
            except Exception as exc:
                logger.debug(f"Task persistence delete failed: {exc}")

    def get_status(self, command_id: str) -> BackgroundCommand | None:
        return self._commands.get(command_id)

    def get_output(self, command_id: str) -> str | None:
        cmd = self._commands.get(command_id)
        return cmd.output if cmd else None

    async def cancel(self, command_id: str) -> bool:
        task = self._tasks.get(command_id)
        if task and not task.done():
            task.cancel()
            cmd = self._commands.get(command_id)
            if cmd:
                cmd.status = "cancelled"
                cmd.completed_at = time.time()
            return True
        return False

    def list_commands(self, include_completed: bool = False) -> list[dict[str, Any]]:
        commands = []
        for cmd in self._commands.values():
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

    async def shutdown(self) -> None:
        """取消所有运行中的后台命令。"""
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()
        self._commands.clear()
