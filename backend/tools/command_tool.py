"""
命令执行工具（DESIGN.md §8.2）。

  - run_command: 执行 shell 命令。stdout ≤ 500 tokens 否则存 artifact。
                 超时控制。权限: CONFIRM
"""

from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

COMMAND_OUTPUT_TOKEN_LIMIT = 500  # 约 2000 字符
DEFAULT_TIMEOUT = 30  # 秒


class RunCommandTool(BaseTool):
    """
    执行 shell 命令。

    超时控制：默认 30 秒。
    输出控制：stdout ≤ 500 tokens 直接返回，超出存 artifact。
    权限: CONFIRM — 执行前需用户确认。
    """

    name = "run_command"
    description = (
        "执行一条 shell 命令并返回输出。"
        "默认超时 30 秒。输出过长时自动存入 artifact。"
        "示例: run_command(command='python -m pytest tests/ -v')。"
        "注意: 会在子进程中执行，支持管道和重定向。"
        "危险命令（rm -rf / 等）应由用户确认后执行。"
    )
    permission = PermissionLevel.CONFIRM

    def __init__(self, artifact_store: ArtifactStore) -> None:
        self._artifact_store = artifact_store

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的 shell 命令",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "工作目录（可选，默认当前目录）",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                    },
                },
                "required": ["command"],
            },
        )

    async def execute(self, args: dict[str, Any]) -> ToolResult:
        command = args.get("command", "")
        cwd = args.get("cwd")
        timeout = args.get("timeout", DEFAULT_TIMEOUT)

        if not command:
            return self._error_result("缺少 command 参数")

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return self._error_result(
                f"命令执行超时（{timeout}秒）。"
                "建议: 增加 timeout 参数，或拆分为更小的命令。"
            )
        except FileNotFoundError:
            return self._error_result(
                f"找不到命令或工作目录: {command}"
            )
        except OSError as exc:
            return self._error_result(f"命令执行失败: {exc}")

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        exit_code = proc.returncode

        output = ""
        if stdout:
            output += stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}" if output else stderr

        # 状态信息
        status = f"退出码: {exit_code}"
        if exit_code != 0:
            status += "（执行失败）"

        # Token 控制
        estimated_tokens = len(output) // 4
        if estimated_tokens <= COMMAND_OUTPUT_TOKEN_LIMIT:
            return self._success_result(f"{status}\n\n{output}" if output else status)

        # 大输出：存 artifact
        artifact_id = self._artifact_store.save(
            content=output,
            source=f"run_command({command})",
            type="command_output",
        )
        preview = self._artifact_store.get_preview(artifact_id, lines=10)

        return self._success_result(
            content=f"{status}（输出约 {estimated_tokens} tokens）",
            artifact_id=artifact_id,
            artifact_preview=preview,
        )
