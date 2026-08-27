from __future__ import annotations

from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.tools.base import (
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_NONE,
    MAX_TOOL_RESULT_CHARS,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
)
from backend.tools.output_limits import (
    TASK_OUTPUT_DEFAULT_CHARS,
    TASK_OUTPUT_MAX_CHARS,
)
from backend.tools.untrusted import wrap_untrusted_content


class MonitorTool(BaseTool):
    """Inspect, write to, or cancel owned background commands."""

    name = "monitor"
    result_kind = "terminal"
    activity_kind = "commandExecution"
    display_label = "Monitor command"
    description = (
        "Inspect, write to, or cancel background commands started by run_command with run_in_background=true. "
        "Use action=status to read live output, action=write_stdin to send exact characters to the owned "
        "process, and action=cancel to stop the exact owned process tree. "
        "Do not use shell process-name matching to clean up a background command."
    )
    permission = PermissionLevel.AUTO
    # Status/list is the default operation; cancellation is classified per call
    # by is_read_only/get_side_effect_kind, as with the browser action tool.
    read_only = True
    max_result_chars = MAX_TOOL_RESULT_CHARS

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        return str((args or {}).get("action") or "status").strip().lower() == "status"

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        return TOOL_SIDE_EFFECT_NONE if self.is_read_only(args) else TOOL_SIDE_EFFECT_EXTERNAL

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        action = str((args or {}).get("action") or "status").strip().lower()
        return action != "write_stdin"

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        payload = args or {}
        action = str(payload.get("action") or "status").strip().lower()
        if action in {"cancel", "write_stdin"} and not str(
            payload.get("command_id") or ""
        ).strip():
            return f"command_id is required when action is {action}"
        if action == "write_stdin":
            chars = payload.get("chars")
            if not isinstance(chars, str):
                return "chars must be a string when action is write_stdin"
            if not chars and not bool(payload.get("close_stdin", False)):
                return "chars must not be empty unless close_stdin is true"
        return ""

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "write_stdin", "cancel"],
                        "description": "Inspect status/output (default), write exact characters to stdin, or cancel the exact owned background command.",
                    },
                    "command_id": {
                        "type": "string",
                        "description": "Background command id returned by run_command. Required for write_stdin and cancel; omit for a status listing.",
                    },
                    "chars": {
                        "type": "string",
                        "description": "Exact UTF-8 characters to write. Include a newline explicitly when the process expects Enter.",
                    },
                    "close_stdin": {
                        "type": "boolean",
                        "description": "Close the command's stdin after writing. Default false.",
                    },
                    "include_completed": {
                        "type": "boolean",
                        "description": "When listing, include completed/failed/cancelled commands. Default false.",
                    },
                    "max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": TASK_OUTPUT_MAX_CHARS,
                        "description": f"Maximum recent output characters to return for one command. Default {TASK_OUTPUT_DEFAULT_CHARS}.",
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
        conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
        if not conversation_id:
            return self._error_result("No conversation owner is available for background command inspection.")

        action = str(args.get("action") or "status").strip().lower()
        command_id = str(args.get("command_id") or "").strip()
        if action == "write_stdin":
            return await self._write_stdin(
                manager,
                command_id,
                str(args.get("chars") or ""),
                conversation_id=conversation_id,
                close_stdin=bool(args.get("close_stdin", False)),
            )
        if action == "cancel":
            if not command_id:
                return self._error_result("command_id is required when action is cancel.")
            return await self._cancel_command(
                manager,
                command_id,
                args,
                conversation_id=conversation_id,
            )
        if command_id:
            return self._command_snapshot(manager, command_id, args, conversation_id=conversation_id)

        include_completed = bool(args.get("include_completed", False))
        commands = manager.list_commands(
            include_completed=include_completed,
            conversation_id=conversation_id,
        )
        if not commands:
            return self._success_result("No background commands are currently running.")

        lines = ["Background commands:"]
        for item in commands:
            started_at = item.get("started_at")
            output_length = int(item.get("output_length") or 0)
            output_bytes = int(item.get("output_bytes") or 0)
            lines.append(
                f"- {item.get('command_id')}: {item.get('status')} "
                f"exit={item.get('exit_code')} output={output_length} chars/{output_bytes} bytes "
                f"cwd={item.get('cwd') or ''} command={item.get('command') or ''} "
                f"started_at={started_at}"
            )
        return self._success_result("\n".join(lines))

    async def _write_stdin(
        self,
        manager: Any,
        command_id: str,
        chars: str,
        *,
        conversation_id: str,
        close_stdin: bool,
    ) -> ToolResult:
        if not command_id:
            return self._error_result("command_id is required when action is write_stdin.")
        try:
            written = await manager.write_stdin(
                command_id,
                chars,
                conversation_id=conversation_id,
                close_stdin=close_stdin,
            )
        except KeyError:
            return self._error_result(f"Background command '{command_id}' was not found.")
        except RuntimeError as exc:
            return self._error_result(str(exc))
        suffix = " and closed stdin" if close_stdin else ""
        return self._success_result(
            f"Wrote {written} UTF-8 bytes to background command {command_id}{suffix}."
        )

    async def _cancel_command(
        self,
        manager: Any,
        command_id: str,
        args: dict[str, Any],
        *,
        conversation_id: str,
    ) -> ToolResult:
        command = manager.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            return self._error_result(f"Background command '{command_id}' was not found.")

        previous_status = str(getattr(command, "status", "") or "")
        cancelled = await manager.cancel(command_id, conversation_id=conversation_id)
        command = manager.get_status(command_id, conversation_id=conversation_id)
        if command is None:  # Defensive: managers normally retain the terminal record.
            if cancelled:
                return self._success_result(f"Cancelled background command {command_id}.")
            return self._error_result(f"Background command '{command_id}' was not found.")

        snapshot = self._command_snapshot(
            manager,
            command_id,
            args,
            conversation_id=conversation_id,
        )
        cleanup_pending = bool(getattr(command, "cleanup_pending", False))
        if cancelled and cleanup_pending:
            prefix = (
                f"Cancellation requested for owned background command {command_id}; "
                "process cleanup is still pending."
            )
        elif cancelled:
            prefix = f"Cancelled owned background command {command_id}."
        else:
            prefix = (
                f"Background command {command_id} was already {previous_status or command.status}; "
                "no process was terminated."
            )
        snapshot.content = f"{prefix}\n\n{snapshot.content}"
        snapshot.status = (
            "pending"
            if cleanup_pending
            else str(command.status or ("cancelled" if cancelled else "completed"))
        )
        snapshot.display_summary = f"Background command {command_id}: {command.status}"
        return snapshot

    def _command_snapshot(
        self,
        manager: Any,
        command_id: str,
        args: dict[str, Any],
        *,
        conversation_id: str,
    ) -> ToolResult:
        command = manager.get_status(command_id, conversation_id=conversation_id)
        if command is None:
            return self._error_result(f"Background command '{command_id}' was not found.")
        try:
            max_chars = int(args.get("max_chars") or TASK_OUTPUT_DEFAULT_CHARS)
        except (TypeError, ValueError):
            max_chars = TASK_OUTPUT_DEFAULT_CHARS
        max_chars = max(1, min(max_chars, TASK_OUTPUT_MAX_CHARS))
        snapshot = manager.get_output_snapshot(
            command_id,
            conversation_id=conversation_id,
            max_chars=max_chars,
        )
        if snapshot is None:
            return self._error_result(f"Background command '{command_id}' was not found.")
        output, truncated, output_path = snapshot
        header = (
            f"Background command {command.command_id} ({command.status})\n"
            f"command: {command.command}\n"
            f"cwd: {command.cwd}\n"
            f"exit_code: {command.exit_code}\n"
            f"started_at: {command.started_at}\n"
            f"completed_at: {command.completed_at}\n"
            f"output_bytes: {int(getattr(command, 'output_bytes', 0) or 0)}\n"
        )
        if truncated and not output_path:
            header += f"[showing last {len(output)} chars]\n"
        body = wrap_untrusted_content(output, "monitor") if output else "<no output captured yet>"
        status = (
            "pending"
            if bool(getattr(command, "cleanup_pending", False))
            else "failed"
            if command.status == "failed"
            else "success"
        )
        return ToolResult(
            content=f"{header}\n{body}",
            is_error=False,
            status=status,
            result_kind="terminal",
            display_summary=f"Background command {command.command_id}: {command.status}",
        )
