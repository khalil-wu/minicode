from __future__ import annotations

import base64
import os
import shlex
import sys
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.permissions.context import ToolExecutionContext
from backend.tools.base import (
    TOOL_SIDE_EFFECT_WORKSPACE,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
)
from backend.tools.command_tool import RunCommandTool
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
    mutates_workspace = True
    side_effect_kind = TOOL_SIDE_EFFECT_WORKSPACE
    idempotent = False
    result_kind = "exec"
    activity_kind = "exec"
    display_label = "REPL"
    max_result_chars = None

    _MAX_CODE_CHARS = 50_000

    def __init__(self, command_tool: RunCommandTool | None = None) -> None:
        # Reuse the exact command/sandbox/streaming owner. A REPL is only a
        # convenience schema over one isolated Python invocation.
        self._command_tool = command_tool or RunCommandTool(ArtifactStore())

    def get_spec(self) -> ToolSpec:
        return ToolSpec(
            name=self.name,
            capability="code.repl",
            toolset="core",
            exposure="deferred",
            required_args=("code",),
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
                        "description": "Optional execution timeout in seconds; otherwise the enclosing turn/command policy applies.",
                    },
                    "with_escalated_permissions": {
                        "type": "boolean",
                        "description": "Retry outside the sandbox after explicit user approval.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Reason full-access execution is required.",
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
        if "timeout_seconds" in payload and payload.get("timeout_seconds") is not None:
            try:
                timeout_seconds = float(payload["timeout_seconds"])
            except (TypeError, ValueError):
                return "timeout_seconds must be a number"
            if timeout_seconds <= 0:
                return "timeout_seconds must be greater than zero"
        return ""

    def check_permission(self, args=None, context=None):
        return self._command_tool.check_permission(args=args, context=context)

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        validation = self.validate_input(args)
        if validation:
            return self._error_result(validation)

        payload = base64.b64encode(str(args.get("code") or "").encode("utf-8")).decode("ascii")
        bootstrap = f"import base64;exec(compile(base64.b64decode('{payload}'),'<repl>','exec'))"
        executable = f'"{sys.executable}"' if os.name == "nt" else shlex.quote(sys.executable)
        snippet = f'"{bootstrap}"' if os.name == "nt" else shlex.quote(bootstrap)
        command_args = {
            "command": f"{executable} -I -c {snippet}",
            "with_escalated_permissions": bool(args.get("with_escalated_permissions", False)),
            "justification": str(args.get("justification") or ""),
        }
        if args.get("timeout_seconds") is not None:
            command_args["timeout"] = float(args["timeout_seconds"])
        result = await self._command_tool.execute(command_args, context)
        result.result_kind = self.result_kind
        result.display_summary = "REPL failed" if result.is_error else "REPL completed"
        return result
