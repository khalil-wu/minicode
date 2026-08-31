"""Shell command execution tool.

Foreground and background commands share the same shell and sandbox policy.
On Windows, workspace-sandbox commands run in PowerShell inside the configured
Linux container; bypass or approved escalated commands run in host
PowerShell. Callers can select an explicit host shell only outside the sandbox.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from fnmatch import fnmatchcase
import logging
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.artifact.store import ArtifactStore
from backend.agent.tool_result_persistence import persist_tool_result
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.sandbox.runner import cleanup_captured_output, read_captured_output
from backend.terminal.shell_commands import (
    normalize_windows_shell_command,
)
from backend.tools.base import (
    TOOL_SIDE_EFFECT_DESTRUCTIVE,
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_WORKSPACE,
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
    truncate_text_tail,
)

from backend.tools.command_support import (
    _as_bool,
    _coerce_timeout,
    _command_matches_excluded,
    _command_matches_patterns,
    _command_side_effect_kind,
    _host_shell_command,
    _looks_like_sandbox_denial,
    _model_shell_description,
    _validated_env,
    _windows_command_portability_hint,
    _workspace_sandbox_policy,
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT_SECONDS,
)
from backend.tools.path_resolution import _is_bypass_mode

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext
    from backend.terminal.manager import BackgroundCommandManager


logger = logging.getLogger(__name__)


class RunCommandTool(BaseTool):
    """Execute shell commands."""

    name = "run_command"
    result_kind = "command"
    activity_kind = "commandExecution"
    display_label = "Run"
    description = (
        "Execute shell commands for builds, installs, git, processes, scripts, package managers, and system operations. "
        "Do not use for file create/edit/read/search/list when dedicated workspace tools fit. "
        "For long-running servers or watchers, set run_in_background. "
        "Never skip hooks or run destructive git commands unless the user explicitly requests it."
    )
    permission = PermissionLevel.CONFIRM

    def is_capability_available(self, context=None) -> bool:
        return context is None or context.mode != "plan"
    # Pi-style tail truncation and durable full-output persistence are handled
    # by this tool, so the generic result layer must not truncate it again.
    max_result_chars = None
    mutates_workspace = True
    mutates_external_state = True  # shells can write files, commit, install, send
    # Worst-case schema/runtime metadata. Concrete calls are classified by
    # get_side_effect_kind below.
    side_effect_kind = TOOL_SIDE_EFFECT_EXTERNAL
    streams_output = True
    timeout_seconds = None
    workspace_path_fields = ("cwd",)

    def resolve_timeout(self, args: dict[str, Any]) -> float | None:
        """Foreground commands get the default watchdog; background commands none."""
        if _as_bool(args.get("run_in_background", False)):
            return None
        timeout = _coerce_timeout(args.get("timeout"))
        return timeout if timeout is not None else DEFAULT_TIMEOUT

    def model_description(self) -> str:
        return _model_shell_description()

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "cwd": {"type": "string"},
                    "env": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Non-secret environment variables injected into the command process.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Run under a persistent background command id; use for servers, watchers, "
                            "and long-lived processes. Inspect or stop only that owned process tree with "
                            "monitor(action='status'|'write_stdin'|'cancel', command_id=...)."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "description": "Optional foreground timeout in seconds; defaults to 120, capped at 600.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short label for a background command.",
                    },
                    "with_escalated_permissions": {
                        "type": "boolean",
                        "description": "Request execution outside the workspace sandbox; explicit user approval is required unless already in bypass mode.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Concise reason the sandbox cannot perform this command, shown in the approval request.",
                    },
                },
                "required": ["command"],
            },
        )

    def streamed_input_preview(
        self,
        args: dict[str, Any],
        context: Any | None = None,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            key: args[key]
            for key in ("command", "cwd", "description")
            if key in args and isinstance(args[key], str)
        }

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        # Shell text is not a trustworthy capability declaration. Use the
        # dedicated typed read tools when an operation must be auto-permitted.
        return False

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        payload = args or {}
        command = str(payload.get("command") or "").strip()
        if not command:
            return "Missing command argument"
        try:
            _coerce_timeout(payload.get("timeout"))
        except ValueError as exc:
            return str(exc)
        # Dangerous-command policy is evaluated at the canonical permission
        # boundary.  Validation must not reject before the user approval flow
        # can produce exact request evidence.
        return ""

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        return _command_side_effect_kind(args)

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        return False

    def check_permission(self, args=None, context=None):
        """Force human approval whenever the model requests sandbox escalation.

        Codex pattern: a model-initiated request to run outside the sandbox is
        never silently granted. When ``with_escalated_permissions`` is set we
        require a CONFIRM gate so the user sees the justification and approves
        before the command runs in bypass execution. Otherwise defer to the
        centralized policy (which may auto-allow read-only commands).
        """
        command = str((args or {}).get("command") or "").strip()
        if command:
            from backend.permissions.checker import check_catastrophic_command

            allowed, _reason = check_catastrophic_command(command)
            if not allowed:
                # Dangerous-pattern checks ask rather than deny: the user may
                # approve a blocked-but-legitimate command.
                return PermissionLevel.CONFIRM
        if args and _as_bool(args.get("with_escalated_permissions", False)):
            if getattr(context, "allow_unsandboxed_commands", True) is False:
                return PermissionLevel.ALWAYS_DENY
            # context here is a PermissionContext (mode on it directly). In bypass
            # mode the command already runs unsandboxed, so escalation is moot;
            # in every other mode the escalation must be confirmed by the user.
            mode = getattr(context, "mode", None)
            if mode != "bypass":
                return PermissionLevel.CONFIRM
        excluded_patterns = tuple(
            getattr(context, "sandbox_excluded_commands", ()) or ()
        )
        if command and _command_matches_patterns(command, excluded_patterns):
            return (
                PermissionLevel.AUTO
                if getattr(context, "mode", None) == "bypass"
                else PermissionLevel.CONFIRM
            )
        if (
            command
            and getattr(context, "sandbox_auto_allow_commands", False) is True
            and getattr(context, "mode", None) != "bypass"
        ):
            return PermissionLevel.AUTO
        return None

    def __init__(
        self,
        artifact_store: ArtifactStore,
        background_manager: BackgroundCommandManager | None = None,
    ) -> None:
        self._artifact_store = artifact_store
        self._background_manager = background_manager

    def _resolve_background_manager(self, context: ToolExecutionContext | None) -> BackgroundCommandManager | None:
        context_manager = getattr(context, "background_manager", None) if context else None
        return context_manager or self._background_manager

    def get_schema(self) -> ToolSchema:
        """Host-facing alias retained for direct callers; the model never sees it."""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory. Defaults to the active workspace root.",
                    },
                    "env": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Non-secret environment variables injected into the command process. Prefer this over inline shell assignments.",
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "description": "Optional foreground timeout in seconds; defaults to 120, capped at 600.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Run under a persistent command id; use for dev servers, watchers, or background "
                            "services. Inspect or stop only that owned process tree with "
                            "monitor(action='status'|'write_stdin'|'cancel', command_id=...)."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional label for the background command.",
                    },
                    "with_escalated_permissions": {
                        "type": "boolean",
                        "description": "Request bypass execution after a sandbox block; user approval is required. Provide justification.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Concise reason shown to the user when with_escalated_permissions is true.",
                    },
                },
                "required": ["command"],
            },
        )

    def get_execution_schema(self) -> ToolSchema:
        return self.model_schema()

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="shell.execute",
            toolset="default",
            exposure="core",
            required_args=("command",),
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        command = args.get("command", "")
        cwd = args.get("cwd")
        env_overrides, env_error = _validated_env(args.get("env"))
        run_in_background = _as_bool(args.get("run_in_background", False))
        try:
            timeout = _coerce_timeout(args.get("timeout"))
        except ValueError as exc:
            return self._error_result(str(exc))
        if timeout is None and not run_in_background:
            timeout = DEFAULT_TIMEOUT
        escalated = _as_bool(args.get("with_escalated_permissions", False))
        description = args.get("description", "")

        if not command:
            return self._error_result("Missing command parameter")
        if env_error:
            return self._error_result(env_error)
        permission = getattr(context, "permission", None)
        if (
            escalated
            and getattr(permission, "allow_unsandboxed_commands", True) is False
        ):
            return self._error_result(
                "Managed sandbox policy does not allow commands to run outside the sandbox."
            )

        from backend.permissions.checker import check_catastrophic_command

        allowed, reason = check_catastrophic_command(command)
        if not allowed:
            from backend.agent.final_tool_request import canonical_tool_request_digest

            request_digest = canonical_tool_request_digest(self.name, args)
            approved = (
                getattr(context, "metadata", {}).get(
                    "_approved_request_digests", set()
                )
                if context
                else set()
            )
            if request_digest not in approved:
                return self._error_result(
                    "This command requires explicit approval before execution. "
                    f"{reason}"
                )

        # Catastrophic-command enforcement lives in check_permission as a
        # CONFIRM gate; a redundant hard error here would block commands the
        # user just approved.

        try:
            effective_cwd = self._resolve_cwd(
                cwd,
                context,
                allow_workspace_escape=escalated,
            )
        except ValueError as exc:
            return self._error_result(str(exc))

        background_manager = self._resolve_background_manager(context)
        base_policy = _workspace_sandbox_policy(
            Path(getattr(context, "workspace_root", None) or effective_cwd or Path.cwd()).expanduser().resolve(),
            context,
            timeout=timeout,
            env_overrides=env_overrides,
        )
        excluded = _command_matches_excluded(command, base_policy)
        if (escalated or excluded) and not base_policy.allow_unsandboxed_commands:
            return self._error_result(
                "Managed sandbox policy does not allow this command to run outside the sandbox."
            )

        if run_in_background:
            return await self._execute_background(
                command,
                effective_cwd,
                timeout,
                description,
                background_manager,
                context,
                escalated=escalated or excluded,
                env_overrides=env_overrides,
            )

        return await self._execute_foreground(
            command,
            effective_cwd,
            timeout,
            context,
            escalated=escalated or excluded,
            env_overrides=env_overrides,
        )

    def _resolve_cwd(
        self,
        cwd: str | None,
        context: ToolExecutionContext | None,
        *,
        allow_workspace_escape: bool = False,
    ) -> str | None:
        workspace_root: Path | None = None
        if context and getattr(context, "workspace_root", None):
            workspace_root = Path(context.workspace_root).expanduser().resolve()

        if cwd:
            raw_cwd = str(cwd).strip()
            if not raw_cwd:
                return str(workspace_root) if workspace_root else None
            cwd_path = Path(raw_cwd).expanduser()
            resolved = cwd_path.resolve() if cwd_path.is_absolute() else (
                (workspace_root / cwd_path).resolve() if workspace_root else cwd_path.resolve()
            )
            if workspace_root and not (allow_workspace_escape or _is_bypass_mode(context)):
                try:
                    resolved.relative_to(workspace_root)
                except ValueError as exc:
                    raise ValueError(f"cwd must stay inside workspace: {workspace_root}") from exc
            return str(resolved)

        return str(workspace_root) if workspace_root else None

    async def _execute_background(
        self,
        command: str,
        cwd: str | None,
        timeout: float | None,
        description: str,
        background_manager: BackgroundCommandManager | None,
        context: ToolExecutionContext | None,
        *,
        escalated: bool = False,
        env_overrides: dict[str, str] | None = None,
    ) -> ToolResult:
        """Start a background command and immediately return its command id."""
        del timeout  # Foreground wait limits end at background handoff.
        if background_manager is None:
            return self._error_result(
                "Background command execution is unavailable. Restart the backend session "
                "or run long-lived commands in an external terminal."
            )

        workspace = (
            Path(context.workspace_root).expanduser().resolve()
            if context is not None and getattr(context, "workspace_root", None)
            else Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        )
        # Claude Code clears the foreground timeout when a command becomes a
        # background task. The session-owned manager remains responsible for
        # cancellation, process-tree cleanup, output bounds, and completion.
        background_timeout_ms = 0
        base_policy = _workspace_sandbox_policy(
            workspace,
            context,
            timeout=0,
            env_overrides=env_overrides,
        )
        policy = (
            base_policy.escalated_preserving_denied_reads()
            if _is_bypass_mode(context) or escalated
            else base_policy
        )
        capability = SandboxRunner(policy).capability(cwd=cwd)
        if not capability.available:
            return self._error_result(
                f"The required sandbox is unavailable, so the background command was not started: {capability.reason}"
            )

        try:
            shell_command = _host_shell_command(command, cwd=cwd)
            background_kwargs: dict[str, Any] = {
                # Preserve the model/user command for UI, persistence, and
                # diagnostics while the manager executes the prepared shell
                # form. Encoded PowerShell must never replace the public text.
                "command": command,
                "effective_command": shell_command,
                "cwd": cwd,
                "timeout_ms": background_timeout_ms,
                "description": description or command[:60],
                "sandbox_policy": policy,
            }
            conversation_id = str(getattr(context, "conversation_id", "") or "").strip()
            if conversation_id:
                background_kwargs["conversation_id"] = conversation_id
            context_metadata = getattr(context, "metadata", None)
            if isinstance(context_metadata, dict):
                parent_run_id = str(context_metadata.get("run_id") or "").strip()
                if parent_run_id:
                    background_kwargs["parent_run_id"] = parent_run_id
            context_task_id = str(getattr(context, "task_id", "") or "").strip()
            if context_task_id:
                background_kwargs["task_id"] = context_task_id
            bg_cmd = await background_manager.run_background(
                **background_kwargs,
            )
        except RuntimeError as exc:
            return self._error_result(str(exc))

        sandbox_label = (
            "bypass execution"
            if capability.backend == "full-access"
            else "OS sandbox"
        )
        return ToolResult(
            content=(
                f"Background command started (ID: {bg_cmd.command_id})\n"
                f"Command: {command[:100]}\n"
                f"Working directory: {bg_cmd.cwd}\n"
                f"Sandbox: {sandbox_label}; background execution uses the same policy as foreground commands.\n"
                f"Use monitor(action='status', command_id='{bg_cmd.command_id}') to inspect live output and status.\n"
                f"Use monitor(action='write_stdin', command_id='{bg_cmd.command_id}', chars='...') to send exact input.\n"
                f"Use monitor(action='cancel', command_id='{bg_cmd.command_id}') to stop this exact owned process tree.\n"
                "Do not stop it with process-name matching or a broad system process command."
            ),
            display_summary=f"Started in background: {self.display_label}",
            status="success",
        )

    async def _execute_foreground(
        self,
        command: str,
        cwd: str | None,
        timeout: float | None,
        context: Any,
        *,
        escalated: bool = False,
        env_overrides: dict[str, str] | None = None,
    ) -> ToolResult:
        """Execute a foreground shell command via SandboxRunner.

        When ``escalated`` is set (and the call passed the escalation approval
        gate), the command runs with bypass execution + network, mirroring the
        "retry outside the sandbox" path. Otherwise it runs under the normal
        workspace-write, no-network policy.
        """
        workspace = (
            Path(context.workspace_root).expanduser().resolve()
            if context is not None and getattr(context, "workspace_root", None)
            else Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        )
        base_policy = _workspace_sandbox_policy(
            workspace,
            context,
            timeout=timeout,
            env_overrides=env_overrides,
        )
        policy = (
            base_policy.escalated_preserving_denied_reads()
            if _is_bypass_mode(context) or escalated
            else base_policy
        )
        sandbox_active = not policy.disable_os_sandbox
        runner = SandboxRunner(policy)

        cancel_event = getattr(context, "cancel_event", None) if context else None
        stream_cb = getattr(context, "stream_callback", None) if context else None

        shell_command = _host_shell_command(command, cwd=cwd)
        result = await runner.run(
            command,
            cwd=cwd,
            cancel_event=cancel_event,
            stream_callback=stream_cb,
            host_command=shell_command,
            preserve_full_output=True,
        )

        stdout = read_captured_output(result.stdout_path, result.stdout)
        stderr = read_captured_output(result.stderr_path, result.stderr)
        captured_paths = (result.stdout_path, result.stderr_path)

        if result.sandbox_unavailable and sandbox_active:
            # The managed sandbox is required by policy but cannot be enforced.
            # The model may retry with the existing explicit escalation fields,
            # which are guarded by a fresh user approval in check_permission().
            cleanup_captured_output(*captured_paths)
            retry_hint = (
                " Retry with with_escalated_permissions=true and a concise justification; "
            "the user must explicitly approve bypass execution."
                if policy.allow_unsandboxed_commands
                else " Managed policy forbids unsandboxed fallback."
            )
            return self._error_result(
                "The workspace sandbox is unavailable on this host, so the command was not run."
                f"{retry_hint}"
            )

        exit_code = result.exit_code

        # A shell command can change workspace files (sed -i, codegen, git
        # checkout, ...). Its affected paths are unknowable without parsing
        # shell syntax, so invalidate every derived workspace view. This keeps
        # MiniCode's caches consistent with CC/Pi, which rescan the filesystem
        # for each search instead of retaining a stale file-tree index.
        try:
            from backend.tools.file_tools_common import invalidate_workspace_file_caches

            invalidate_workspace_file_caches(file_tree_changed=True, clear_file_state=True)
        except Exception:
            logger.warning(
                "Failed to invalidate workspace file caches after shell command",
                exc_info=True,
            )

        output = stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}" if output else stderr

        if result.cancelled:
            status = "Command execution was cancelled/interrupted"
            is_failed_exit = True
            result_status = "cancelled"
        elif result.timed_out:
            status = f"Command timed out after {timeout:g} seconds"
            is_failed_exit = True
            result_status = "timeout"
        else:
            status = f"Exit code: {exit_code}"
            is_failed_exit = exit_code != 0
            if is_failed_exit:
                status += " (failed)"
            result_status = "failed" if is_failed_exit else None

        # Codex escalate-on-failure: if a sandboxed command failed in a way that
        # looks caused by the sandbox (network blocked, permission denied), tell
        # the model it may retry once with escalated permissions (user-approved).
        # Only when we actually ran sandboxed and the model hasn't already
        # escalated — never advertise escalation after it was granted.
        escalation_hint = ""
        if (
            is_failed_exit
            and not result.cancelled
            and not result.timed_out
            and sandbox_active
            and not escalated
            and policy.allow_unsandboxed_commands
            and _looks_like_sandbox_denial(stderr, exit_code)
        ):
            escalation_hint = (
                "\n\n[sandbox] This command ran in a restricted sandbox (no network, "
                "writes limited to the workspace) and the failure looks sandbox-related. "
                "If it needs network or access outside the workspace, retry the SAME command "
                "with with_escalated_permissions=true and a one-line justification; the user "
                "will be asked to approve. Do not escalate for ordinary command errors."
            )
        if escalation_hint:
            status = f"{status}{escalation_hint}"

        portability_hint = _windows_command_portability_hint(
            command,
            stderr,
            exit_code,
        )
        if portability_hint:
            status = f"{status}\n\n{portability_hint}"

        truncation = truncate_text_tail(output)
        if not truncation.truncated:
            cleanup_captured_output(*captured_paths)
            return ToolResult(
                content=f"{status}\n\n{output}" if output else status,
                is_error=is_failed_exit,
                status=result_status,
            )

        artifact_id = ""
        full_output_reference = ""
        try:
            artifact_id = self._artifact_store.save(
                content=output,
                source=f"run_command({command})",
                type="command_output",
            )
        except (OSError, ValueError):
            tool_call_id = str(
                getattr(context, "tool_call_id", "") or ""
            ).strip() or "run_command"
            persisted = persist_tool_result(
                output,
                tool_call_id,
                "run_command",
                force=True,
                conversation_id=str(getattr(context, "conversation_id", "") or ""),
                workspace_root=getattr(context, "workspace_root", None),
            )
            if persisted is not None:
                full_output_reference = persisted.filepath

        if artifact_id or full_output_reference:
            cleanup_captured_output(*captured_paths)
        else:
            raw_references = [path for path in captured_paths if path]
            full_output_reference = ", ".join(raw_references)

        start_line = max(1, truncation.total_lines - truncation.output_lines + 1)
        if truncation.last_line_partial:
            truncation_notice = (
                f"Showing the last {truncation.output_bytes} bytes of line {truncation.total_lines} "
                f"({MAX_TOOL_RESULT_BYTES} byte limit)."
            )
        elif truncation.truncated_by == "lines":
            truncation_notice = (
                f"Showing lines {start_line}-{truncation.total_lines} of {truncation.total_lines} "
                f"({MAX_TOOL_RESULT_LINES} line limit)."
            )
        else:
            truncation_notice = (
                f"Showing lines {start_line}-{truncation.total_lines} of {truncation.total_lines} "
                f"({MAX_TOOL_RESULT_BYTES} byte limit)."
            )
        if full_output_reference:
            truncation_notice += f" Full output: {full_output_reference}"

        return ToolResult(
            content=f"{status}\n\n[{truncation_notice}]",
            artifact_id=artifact_id or None,
            artifact_preview=truncation.content,
            is_error=is_failed_exit,
            status=result_status,
        )
