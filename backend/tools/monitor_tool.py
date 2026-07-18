from __future__ import annotations

from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.untrusted import wrap_untrusted_content


class MonitorTool(BaseTool):
    """Inspect background commands started with run_command(run_in_background=true)."""

    name = "monitor"
    description = (
        "Inspect background commands started by run_command with run_in_background=true. "
        "Use this to check long-running dev servers, watchers, tests, or builds without polling with sleep."
    )
    permission = PermissionLevel.AUTO
    read_only = True
    max_result_chars = None

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command_id": {
                        "type": "string",
                        "description": "Background command id returned by run_command. Omit to list background commands.",
                    },
                    "include_completed": {
                        "type": "boolean",
                        "description": "When listing, include completed/failed/cancelled commands. Default false.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum recent output characters to return for one command. Default 20000.",
                    },
                },
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        manager = getattr(context, "background_manager", None) if context else None
        if manager is None:
            return self._error_result("No background command manager is available in this session.")

        command_id = str(args.get("command_id") or "").strip()
        if command_id:
            return self._command_snapshot(manager, command_id, args)

        include_completed = bool(args.get("include_completed", False))
        commands = manager.list_commands(include_completed=include_completed)
        if not commands:
            return self._success_result("No background commands are currently running.")

        lines = ["Background commands:"]
        for item in commands:
            started_at = item.get("started_at")
            output_length = int(item.get("output_length") or 0)
            lines.append(
                f"- {item.get('command_id')}: {item.get('status')} "
                f"exit={item.get('exit_code')} output={output_length} chars "
                f"cwd={item.get('cwd') or ''} command={item.get('command') or ''} "
                f"started_at={started_at}"
            )
        return self._success_result("\n".join(lines))

    def _command_snapshot(self, manager: Any, command_id: str, args: dict[str, Any]) -> ToolResult:
        command = manager.get_status(command_id)
        if command is None:
            return self._error_result(f"Background command '{command_id}' was not found.")
        try:
            max_chars = int(args.get("max_chars") or 20_000)
        except (TypeError, ValueError):
            max_chars = 20_000
        output = str(getattr(command, "output", "") or "")
        truncated = max_chars > 0 and len(output) > max_chars
        if truncated:
            output = output[-max_chars:]
        header = (
            f"Background command {command.command_id} ({command.status})\n"
            f"command: {command.command}\n"
            f"cwd: {command.cwd}\n"
            f"exit_code: {command.exit_code}\n"
            f"started_at: {command.started_at}\n"
            f"completed_at: {command.completed_at}\n"
        )
        if truncated:
            header += f"[showing last {len(output)} chars]\n"
        body = wrap_untrusted_content(output, "monitor") if output else "<no output captured yet>"
        status = "failed" if command.status == "failed" else "success"
        return ToolResult(
            content=f"{header}\n{body}",
            is_error=False,
            status=status,
            result_kind="terminal",
            display_summary=f"Background command {command.command_id}: {command.status}",
        )
