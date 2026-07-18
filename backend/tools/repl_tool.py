from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import (
    TOOL_SIDE_EFFECT_WORKSPACE,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
    truncate_tool_result,
)
from backend.tools.contracts import ToolSpec


class ReplTool(BaseTool):
    """Run a short, one-shot Python snippet with bounded output."""

    name = "repl"
    description = (
        "Execute a short Python snippet in a fresh, non-persistent interpreter. "
        "Use for quick calculations or inspecting installed Python packages. "
        "This is not a long-running shell or notebook kernel."
    )
    permission = PermissionLevel.CONFIRM
    timeout_seconds = 35.0
    mutates_workspace = True
    side_effect_kind = TOOL_SIDE_EFFECT_WORKSPACE
    idempotent = False
    result_kind = "exec"
    activity_kind = "exec"
    display_label = "REPL"
    max_result_chars = None

    _MAX_CODE_CHARS = 50_000
    _MAX_TIMEOUT_SECONDS = 30.0
    _MAX_OUTPUT_CHARS = 12_000

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="code.repl",
            toolset="core",
            exposure="deferred",
            required_args=("code",),
            arg_roles={"code": "generated_content", "language": "control", "timeout_seconds": "control"},
            repair_policy={"code": "needs_model_generation", "language": "runtime_control"},
            empty_args_policy="block",
            blocked_guidance="Missing code. Provide a short Python snippet to execute.",
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["python"],
                        "description": "Only python is supported.",
                    },
                    "code": {
                        "type": "string",
                        "description": "Python code to execute once in a fresh interpreter.",
                    },
                    "timeout_seconds": {
                        "type": "number",
                        "minimum": 0.1,
                        "maximum": self._MAX_TIMEOUT_SECONDS,
                        "description": "Execution timeout in seconds. Defaults to 10, max 30.",
                    },
                },
                "required": ["code"],
            },
        )

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        payload = args or {}
        language = str(payload.get("language") or "python").strip().lower()
        if language != "python":
            return "repl currently supports only language='python'"
        code = payload.get("code")
        if not isinstance(code, str) or not code.strip():
            return "Missing code. Provide a short Python snippet."
        if len(code) > self._MAX_CODE_CHARS:
            return f"code is too large for repl (max {self._MAX_CODE_CHARS} chars)"
        try:
            timeout_seconds = float(payload.get("timeout_seconds", 10.0))
        except (TypeError, ValueError):
            return "timeout_seconds must be a number"
        if timeout_seconds <= 0 or timeout_seconds > self._MAX_TIMEOUT_SECONDS:
            return f"timeout_seconds must be > 0 and <= {self._MAX_TIMEOUT_SECONDS:g}"
        return ""

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        validation = self.validate_input(args)
        if validation:
            return self._error_result(validation)

        code = str(args.get("code") or "")
        timeout_seconds = float(args.get("timeout_seconds", 10.0))
        cwd = Path(context.workspace_root).resolve() if context and context.workspace_root else Path.cwd()
        started = time.perf_counter()

        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-",
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return self._error_result(f"failed to start Python interpreter: {exc}")

        communicate_task = asyncio.create_task(proc.communicate(code.encode("utf-8")))
        cancel_task: asyncio.Task[bool] | None = None
        cancel_event = getattr(context, "cancel_event", None) if context else None
        if cancel_event is not None:
            cancel_task = asyncio.create_task(cancel_event.wait())

        try:
            waiters: set[asyncio.Task[Any]] = {communicate_task}
            if cancel_task is not None:
                waiters.add(cancel_task)
            done, _pending = await asyncio.wait(
                waiters,
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if communicate_task in done:
                stdout, stderr = communicate_task.result()
            else:
                reason = "cancelled" if cancel_task is not None and cancel_task in done else "timed out"
                proc.kill()
                stdout, stderr = await communicate_task
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                output = self._format_output(
                    proc.returncode,
                    stdout,
                    stderr,
                    extra=f"Execution {reason} after {elapsed_ms / 1000:.1f}s.",
                )
                return ToolResult(
                    content=truncate_tool_result(output, self._MAX_OUTPUT_CHARS),
                    is_error=True,
                    status="cancelled" if reason == "cancelled" else "timeout",
                    result_kind=self.result_kind,
                    display_summary=f"REPL {reason}",
                    duration_ms=elapsed_ms,
                )
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        output = self._format_output(proc.returncode, stdout, stderr)
        exit_code = proc.returncode if proc.returncode is not None else -1
        return ToolResult(
            content=truncate_tool_result(output, self._MAX_OUTPUT_CHARS),
            is_error=exit_code != 0,
            status="success" if exit_code == 0 else "failed",
            result_kind=self.result_kind,
            display_summary=f"REPL exited {exit_code}",
            duration_ms=elapsed_ms,
        )

    @staticmethod
    def _decode(data: bytes) -> str:
        return data.decode("utf-8", errors="replace").rstrip()

    def _format_output(self, exit_code: int | None, stdout: bytes, stderr: bytes, *, extra: str = "") -> str:
        parts = [f"Python repl exit_code={exit_code if exit_code is not None else 'unknown'}"]
        if extra:
            parts.append(extra)
        out = self._decode(stdout)
        err = self._decode(stderr)
        if out:
            parts.append("--- stdout ---")
            parts.append(out)
        if err:
            parts.append("--- stderr ---")
            parts.append(err)
        if not out and not err:
            parts.append("(no output)")
        return "\n".join(parts)
