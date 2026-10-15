from __future__ import annotations

import asyncio
from copy import deepcopy
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
from backend.tools.command_support import command_wait_ms, stdin_wait_ms
from backend.terminal.stdin_authority import StdinAuthorityError, TerminalInputApproval


class MonitorTool(BaseTool):
    """Inspect, write to, or cancel owned background commands."""

    name = "monitor"
    result_kind = "terminal"
    activity_kind = "commandExecution"
    display_label = "Monitor command"
    description = (
        "Inspect, wait for, write to, or cancel owned commands returned by run_command. "
        "Use action=status to read live output, action=write_stdin to send exact characters to the owned "
        "process, and action=cancel to stop the exact owned process tree. "
        "Pass cursor=the last next_cursor to read only new output, and yield_time_ms to wait for output or completion. "
        "Do not use shell process-name matching to clean up a background command."
    )
    permission = PermissionLevel.AUTO
    # Status/list is the default operation; cancellation is classified per call
    # by is_read_only/get_side_effect_kind, as with the browser action tool.
    read_only = True
    max_result_chars = MAX_TOOL_RESULT_CHARS

    def check_permission(self, args=None, context=None):
        # Source reviews an existing terminal's retained authority at the actual
        # write, under its process lock. Static deny/ask still wins in checker.
        if (args or {}).get("action", "status") == "write_stdin":
            return PermissionLevel.AUTO
        return None

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
                    "yield_time_ms": {
                        "type": "integer", "minimum": 0,
                        "description": "Wait for completion or new output after cursor. Status defaults to 0; write_stdin defaults to 250, and empty stdin polls wait at least 5000. Waiting does not stop the command.",
                    },
                    "cursor": {
                        "type": "integer", "minimum": 0,
                        "description": "Byte cursor returned as next_cursor by the previous result. Use 0 for output from the beginning; omit for the latest tail.",
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
                    "output_mode": {
                        "type": "string", "enum": ["tail", "head_tail"],
                        "description": "Without cursor: tail shows recent output (default); head_tail shows both the beginning and end. With cursor: read a sequential page.",
                    },
                },
            },
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        result = await self._execute(args, context)
        result.runtime_metadata.setdefault("terminal_hook", {"ready": False})
        return result

    async def _execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None,
    ) -> ToolResult:
        manager = getattr(context, "background_manager", None) if context else None
        if manager is None:
            return self._error_result("No background command manager is available in this session.")
        conversation_id = context.command_scope_id
        if not conversation_id:
            return self._error_result("No session or conversation owner is available for command inspection.")

        action = str(args.get("action") or "status").strip().lower()
        command_id = str(args.get("command_id") or "").strip()
        try:
            wait_ms = (stdin_wait_ms(args.get("yield_time_ms"), chars=args.get("chars", ""), context=context)
                       if action == "write_stdin" else command_wait_ms(args.get("yield_time_ms"), default=0))
        except ValueError as exc:
            return self._error_result(str(exc))
        cursor = args.get("cursor")
        if cursor is not None and (isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0):
            return self._error_result("cursor must be a non-negative byte offset returned by this command")
        if (wait_ms or cursor is not None) and not command_id:
            return self._error_result("command_id is required when waiting for command output")
        if action == "write_stdin":
            return await self._write_stdin(
                manager,
                command_id,
                str(args.get("chars") or ""),
                conversation_id=conversation_id,
                close_stdin=bool(args.get("close_stdin", False)),
                context=context,
                approval_args=deepcopy(args),
                wait_ms=wait_ms,
                cursor=cursor,
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
            if wait_ms:
                try:
                    await manager.wait(command_id, conversation_id=conversation_id, wait_ms=wait_ms, cursor=cursor, cancel_event=getattr(context, "cancel_event", None))
                except KeyError:
                    return self._error_result(f"Background command '{command_id}' was not found.")
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
        presentation = ["Background commands:"]
        for item in commands:
            presentation.append(f"- {item.get('status')} · {item.get('command') or ''}\ncwd: {item.get('cwd') or ''}\nexit: {item.get('exit_code')}")
        return ToolResult(content="\n".join(lines), content_preview="\n".join(presentation))

    async def _write_stdin(
        self,
        manager: Any,
        command_id: str,
        chars: str,
        *,
        conversation_id: str,
        close_stdin: bool,
        context: ToolExecutionContext,
        approval_args: dict[str, Any],
        wait_ms: int,
        cursor: int | None,
    ) -> ToolResult:
        if not command_id:
            return self._error_result("command_id is required when action is write_stdin.")
        try:
            written = await manager.write_stdin(
                command_id,
                chars,
                conversation_id=conversation_id,
                close_stdin=close_stdin,
                context=context,
                authorize_input=lambda action: self._approve_terminal_input(action, context, approval_args),
                wait_ms=wait_ms,
                cursor=cursor,
            )
        except KeyError:
            return self._error_result(f"Background command '{command_id}' was not found.")
        except RuntimeError as exc:
            return self._error_result(str(exc))
        result = self._command_snapshot(manager, command_id, approval_args, conversation_id=conversation_id)
        result.runtime_metadata.update(stdin_bytes_written=written, stdin_closed=close_stdin)
        return result

    async def _approve_terminal_input(self, action: TerminalInputApproval, context: ToolExecutionContext, args: dict[str, Any]) -> None:
        from backend.agent.tool_batch_execution import _await_approval_within_turn_deadline
        from backend.agent.tool_execution import await_preflight
        from backend.llm.base import ToolCallEvent
        from backend.permissions.approval_policy import rejected_approval_reason

        strict = context.permission.strict_auto_review or context.permission_grants.strict_auto_review_enabled(context.turn_id)
        if action.sandbox_permissions != "use_default" and not (strict and context.permission.approval_policy == "never"):
            reason = rejected_approval_reason(context.permission, "sandbox_approval")
            if reason is not None:
                raise StdinAuthorityError(reason)

        hook_manager = context.run_context.hook_manager if context.run_context is not None else None
        if hook_manager is not None:
            hook = await await_preflight(hook_manager.run_permission_request(
                "write_stdin", action.permission_hook_input(), reason=action.reason,
                permission_level="confirm", tool_call_id=context.tool_call_id,
                session_id=context.session_id, permission_mode=context.permission.mode),
                deadline=context.deadline_monotonic, cancel_event=context.cancel_event,
                run_context=context.run_context, llm=context.llm)
            if hook.updated_input is not None:
                raise StdinAuthorityError("the permission hook proposed different terminal input; submit it as a new tool request")
            if hook.blocked or hook.permission_decision == "deny":
                raise StdinAuthorityError(hook.message or hook.feedback or "terminal input was rejected by the permission hook")
            if hook.permission_decision == "allow":
                return
        service = context.run_context.guardian_review_service if context.run_context is not None else None
        if service is not None:
            from backend.agent.guardian.action import GuardianAction

            receipt = await service.decide(GuardianAction.from_terminal_input(action, context), context,
                strict_auto_review=context.permission.strict_auto_review or context.permission_grants.strict_auto_review_enabled(context.turn_id),
                approval_reason=action.reason)
            if receipt is not None and not receipt.require_user:
                if receipt.status == "aborted":
                    raise asyncio.CancelledError
                if receipt.status != "approved":
                    raise StdinAuthorityError(receipt.rationale or "terminal input was not approved")
                return
        if context.permission.approval_policy == "never":
            raise StdinAuthorityError("approval policy is never; terminal input requires permissions that cannot be requested")
        if context.approval_handler is None or context.emit_event is None:
            raise StdinAuthorityError("terminal input requires approval but no approval channel is available")
        if context.cancel_event is not None and context.cancel_event.is_set():
            raise asyncio.CancelledError
        await context.emit_event("approval_request", {
            "subtype": "write_stdin", "tool_call_id": context.tool_call_id, "tool_name": self.name,
            "args": deepcopy(args), "request_digest": context.authorization_digest,
            "policy_identity": context.authorization_policy_identity,
            "stdin_action": action.to_payload(),
        })
        call = ToolCallEvent(id=context.tool_call_id, name=self.name, arguments=deepcopy(args))
        response = await _await_approval_within_turn_deadline(context.approval_handler, call, context,
            expected_digest=context.authorization_digest)
        if response.get("action") != "approve" or response.get("updated_input") is not None or response.get("updatedInput") is not None:
            raise StdinAuthorityError(response.get("guidance") or "terminal input was not approved")
        if response.get("scope") not in {None, "call"} or response.get("remember"):
            raise StdinAuthorityError("terminal input approvals apply only to this interaction")

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
            user_prefix = "Cancellation requested; process cleanup is still pending."
        elif cancelled:
            prefix = f"Cancelled owned background command {command_id}."
            user_prefix = "Background command cancelled."
        else:
            prefix = (
                f"Background command {command_id} was already {previous_status or command.status}; "
                "no process was terminated."
            )
            user_prefix = f"Command was already {previous_status or command.status}; no process was terminated."
        snapshot.content = f"{prefix}\n\n{snapshot.content}"
        snapshot.content_preview = f"{user_prefix}\n\n{snapshot.content_preview or ''}"
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
        next_cursor = None
        end_cursor = None
        more = False
        if args.get("cursor") is not None:
            try:
                chunk = manager.get_output_chunk(command_id, conversation_id=conversation_id, cursor=args["cursor"], max_chars=max_chars)
            except (ValueError, OSError) as exc:
                return self._error_result(str(exc))
            if chunk is None:
                return self._error_result(f"Background command '{command_id}' was not found.")
            output, next_cursor, more, output_path = chunk
            snapshot = (output, False, output_path)
        elif args.get("output_mode") == "head_tail":
            preview = manager.get_output_preview(command_id, conversation_id=conversation_id, max_chars=max_chars)
            output, next_cursor, more, end_cursor = preview
            snapshot = (output, more, command.output_path)
        else:
            snapshot = manager.get_output_snapshot(
                command_id, conversation_id=conversation_id, max_chars=max_chars,
            )
        if snapshot is None:
            return self._error_result(f"Background command '{command_id}' was not found.")
        output, truncated, output_path = snapshot
        header = (
            f"Background command {command.command_id} ({command.status})\n"
            f"command: {command.command[:160]}{'…' if len(command.command) > 160 else ''}\n"
            f"cwd: {command.cwd}\n"
            f"exit_code: {command.exit_code}\n"
            f"started_at: {command.started_at}\n"
            f"completed_at: {command.completed_at}\n"
            f"output_bytes: {int(getattr(command, 'output_bytes', 0) or 0)}\n"
        )
        if truncated and not output_path:
            header += f"[showing last {len(output)} chars]\n"
        if next_cursor is not None:
            header += f"next_cursor: {next_cursor}\nmore_output: {str(more).lower()}\n"
        if end_cursor is not None:
            header += f"end_cursor: {end_cursor}\n"
        if more or truncated:
            if end_cursor is not None:
                header += "Head/tail preview: next_cursor reads the omitted middle; end_cursor follows future output. "
            elif next_cursor is not None:
                header += "Output page: continue with next_cursor, or use cursor=0 to revisit the beginning. "
            else:
                header += "Tail preview: use cursor=0 to read the stored output from the beginning. "
            header += "Do not rerun the command to retrieve diagnostics.\n"
        cleanup_receipt = {}
        if bool(getattr(command, "cleanup_pending", False)):
            cleanup_receipt = {
                "resource_kind": "command", "resource_id": command.command_id,
                "reason": str(getattr(command, "cleanup_reason", "") or ""),
                "requested": True, "acknowledged": True, "completed": False, "pending": 1,
            }
            header += "Process cleanup is still pending; use action='cancel' on this command id to retry cleanup.\n"
        body = wrap_untrusted_content(output, "monitor") if output else "<no output captured yet>"
        is_execution_error = command.status == "failed" and not command.command_completed
        status = (
            "pending"
            if bool(getattr(command, "cleanup_pending", False))
            else "failed"
            if is_execution_error
            else "success"
        )
        result = ToolResult(
            content=f"{header}\n{body}",
            content_preview=(
                f"Process: {command.status}\ncommand: {command.command}\ncwd: {command.cwd}\nexit_code: {command.exit_code}\n"
                + ("Process cleanup is still pending.\n" if cleanup_receipt else "")
                + ("[Output is incomplete; more output is retained.]\n" if more or truncated else "")
                + (output or "<no output captured yet>")
            ),
            is_error=is_execution_error,
            status=status,
            result_kind="terminal",
            display_summary=f"Background command {command.command_id}: {command.status}",
            runtime_metadata={"command_id": command.command_id, "next_cursor": next_cursor, "more_output": more,
                              "end_cursor": end_cursor,
                              "stdout_preview": output, "stderr_preview": "",
                              "process_status": command.status, "exit_code": command.exit_code},
            cleanup_receipt=cleanup_receipt,
        )
        result.runtime_metadata["terminal_hook"] = manager.terminal_hook_payload(command_id,
            conversation_id=conversation_id, output=output)
        return result
