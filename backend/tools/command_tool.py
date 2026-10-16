"""Shell command execution tool.

Foreground and background commands share the same shell and sandbox policy.
Shell/login and PTY selection travel through the same sandbox/process owner
as plain pipe commands. The public command stays separate from execution argv.
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from dataclasses import replace
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.artifact.store import ArtifactStore
from backend.async_cleanup import to_thread_cancel_safe
from backend.terminal.task_output import preview_text_output
from backend.tools.output_limits import TASK_OUTPUT_DEFAULT_CHARS, TASK_OUTPUT_MAX_CHARS
from backend.agent.tool_result_persistence import persist_tool_result
from backend.sandbox import SandboxPolicy, SandboxResult, SandboxRunner
from backend.sandbox.runner import cleanup_captured_output, read_captured_output
from backend.tools.base import (
    TOOL_SIDE_EFFECT_EXTERNAL,
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
    _model_shell_description,
    _validated_env,
    _windows_command_portability_hint,
    _workspace_sandbox_policy,
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT_SECONDS,
    command_wait_ms,
    command_login_allowed,
    command_shell_argv,
    command_sandbox_recovery_hint,
    MAX_COMMAND_YIELD_MS,
)
from backend.tools.path_resolution import _is_bypass_mode
from backend.permissions.grants import current_granted_permissions, sandbox_policy_with_grants, permission_profile_allows_path

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext
    from backend.terminal.manager import BackgroundCommandManager


logger = logging.getLogger(__name__)


def _with_network_approval_context(policy, context):
    from backend.permissions.environment_command import EnvironmentCommandPolicy

    if not isinstance(policy, EnvironmentCommandPolicy) or policy.network_policy is None:
        return policy

    def register(execution_id, cancel_execution, apply_amendment):
        async def persist_amendment(*, host, approved_host, protocol, action, reason, on_policy_applied):
            runtime = policy.network_policy.runtime
            normalized = await to_thread_cancel_safe(runtime.invoke_sync, "network/normalizeHost", {"host": host})
            approved = await to_thread_cancel_safe(runtime.invoke_sync, "network/normalizeHost", {"host": approved_host})
            if normalized["host"] != approved["host"]:
                raise ValueError("Network policy amendment does not match the reviewed host")
            await apply_amendment(host=host, protocol=protocol, action=action)
            on_policy_applied()
            await context.run_context.exec_policy_owner.persist_network_amendment(runtime,
                host=host, protocol=protocol, action=action)

        return context.run_context.network_approval_service.register(context.final_tool_request, context,
            execution_id=execution_id, cancel_execution=cancel_execution, persist_amendment=persist_amendment)

    return replace(policy, network_approval_factory=register)


class RunCommandTool(BaseTool):
    """Execute shell commands."""

    requires_execution_environment = True

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
        return True
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
        """The process owner enforces timeout; the tool waits by yield/deadline."""
        # A second tool timer can cancel the wait before the process owner has
        # recorded timeout/cleanup. SandboxRunner still bounds legacy calls.
        return None

    def model_description(self, *, windows: bool | None = None) -> str:
        return _model_shell_description(windows=windows) + (
            " Managed commands wait up to yield_time_ms (default 10000 ms), then return a live command id. "
            "A running result is not an exit: use monitor with that id to wait/read output/send stdin/cancel. "
            "Continue independent work while it runs, but wait for completion before work that depends on its effects. "
            "Use next_cursor for remaining captured output, or end_cursor to follow future output. Do not restart a command just because it yielded."
            " Set max_chars to control the returned output length; the initial view keeps both the beginning and end. "
            "The entire command output is retained, even when the preview is short. "
            " Run tests directly: shell pipes to tail/grep can discard failures and replace the test exit code. "
            "Use monitor with cursor=0 to reread captured output instead of rerunning tests to change the display."
        )

    def model_description_for_policy(self, policy) -> str:
        return self.model_description(windows=policy.windows_shell_guidance)

    def model_schema_for_policy(self, policy) -> ToolSchema:
        schema = super().model_schema_for_policy(policy)
        if policy.environment_count is None:
            return schema
        properties = {**schema.parameters["properties"]}
        properties["command"] = {**properties["command"], "description": "Shell command in the selected execution environment's shell syntax."}
        if policy.environment_login_parameter is False:
            properties.pop("login")
        return replace(schema, parameters={**schema.parameters, "properties": properties})

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command in the host's shell syntax (PowerShell on Windows)."},
                    "shell": {"type": "string", "description": "Shell binary to launch. Defaults to the user's default shell."},
                    "login": {"type": "boolean", "description": "True uses shell startup profiles; false disables them. Defaults to true when allowed by config."},
                    "tty": {"type": "boolean", "description": "True allocates a PTY for the command; false or omitted uses plain pipes."},
                    "max_chars": {
                        "type": "integer", "minimum": 1, "maximum": TASK_OUTPUT_MAX_CHARS,
                        "description": f"Output preview character budget, default {TASK_OUTPUT_DEFAULT_CHARS}. Preserves the beginning and end; full output remains readable with monitor. Use this instead of shell tail/grep filters.",
                    },
                    "yield_time_ms": {
                        "type": "integer", "minimum": 0, "maximum": MAX_COMMAND_YIELD_MS,
                        "description": "How long to wait before returning a live command id (default 10000 ms). Continue with monitor; this does not stop the process.",
                    },
                    "cwd": {"type": "string", "description": "Working directory; defaults to the active workspace."},
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
                        "description": "Optional runtime limit for non-background commands, in seconds (max 600). Explicit background mode detaches this limit; yield_time_ms only limits waiting.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short label for a background command.",
                    },
                    "sandbox_permissions": {"type": "string", "enum": ["use_default", "require_escalated", "with_additional_permissions"],
                        "description": "Use the configured sandbox, request escalation, or request sandboxed additional permissions for this command."},
                    "additional_permissions": {"type": "object", "description": "Specific network or file_system permissions requested for this command when sandbox_permissions is with_additional_permissions."},
                    "justification": {
                        "type": "string",
                        "description": "Concise reason the sandbox cannot perform this command, shown in the approval request.",
                    },
                    "prefix_rule": {"type": "array", "items": {"type": "string"},
                        "description": "Reusable command-token prefix proposed for approval. The host verifies it against every parsed command."},
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
        return False

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        return TOOL_SIDE_EFFECT_EXTERNAL

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        return False

    def check_permission(self, args=None, context=None):
        """The final native requirement owns shell approval routing."""
        native = context.captured_exec_policy if context is not None else None
        if native is not None:
            return {"skip": PermissionLevel.AUTO, "needs_approval": PermissionLevel.CONFIRM,
                "forbidden": PermissionLevel.ALWAYS_DENY}[native.kind]
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
                    "shell": {"type": "string", "description": "Shell binary to launch. Defaults to the user's default shell."},
                    "login": {"type": "boolean", "description": "True loads shell startup profiles; false disables them."},
                    "tty": {"type": "boolean", "description": "True allocates a PTY; false or omitted uses plain pipes."},
                    "yield_time_ms": {
                        "type": "integer", "minimum": 0, "maximum": MAX_COMMAND_YIELD_MS,
                        "description": "Wait up to this many milliseconds, then return the owned command id if it is still running. Default 10000; use monitor to continue.",
                    },
                    "max_chars": {
                        "type": "integer", "minimum": 1, "maximum": TASK_OUTPUT_MAX_CHARS,
                        "description": f"Output preview character budget, default {TASK_OUTPUT_DEFAULT_CHARS}. Full output is retained; this does not change the command or exit code.",
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
                        "description": "Optional runtime limit for non-background commands, in seconds (max 600). Explicit background mode detaches this limit; yield_time_ms only limits waiting.",
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
                    "sandbox_permissions": {"type": "string", "enum": ["use_default", "require_escalated", "with_additional_permissions"],
                        "description": "Use the configured sandbox, request escalation, or request sandboxed additional permissions."},
                    "additional_permissions": {"type": "object", "description": "Requested network and file_system permissions for this command."},
                    "prefix_rule": {"type": "array", "items": {"type": "string"}, "description": "Proposed reusable command-token approval prefix."},
                    "justification": {
                        "type": "string",
                        "description": "Concise reason shown to the user when sandbox_permissions is require_escalated.",
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

    def capture_exec_policy(self, request, context: ToolExecutionContext, permission):
        from backend.permissions.exec_policy import FrozenExecCommand
        from backend.runtime_env import powershell_script
        args = request.argument_values()
        captured_context = replace(context, permission=permission)
        env, error = _validated_env(args.get("env"))
        if error:
            raise ValueError(error)
        timeout = _coerce_timeout(args.get("timeout"))
        environment = context.turn_environment
        if environment is not None:
            from backend.permissions.environment_command import EnvironmentCommandPolicy
            from backend.workspace.executor_paths import ExecutorPath

            scope = permission.environment_scope
            cwd_path = scope.resolve_path(args.get("cwd") or scope.cwd_uri)
            roots = tuple(ExecutorPath.parse(root, platform_os=scope.platform_os) for root in scope.workspace_root_uris)
            policy = EnvironmentCommandPolicy(replace(scope, cwd_uri=cwd_path.uri), environment,
                environment.config.shell_environment_policy, env, timeout, permission.allow_unsandboxed_commands)
        else:
            cwd = self._resolve_cwd(args.get("cwd"), context, allow_workspace_escape=True)
            workspace = Path(context.workspace_root).resolve()
            cwd_path, roots = Path(cwd), (workspace,)
            policy = _workspace_sandbox_policy(workspace, captured_context, timeout=timeout, env_overrides=env)
        policy = sandbox_policy_with_grants(policy, captured_context)
        from backend.permissions.approval_policy import rejected_approval_reason
        from backend.permissions.grants import normalize_permission_profile, intersect_permission_profiles
        requested_mode = args.get("sandbox_permissions", "use_default")
        requested_profile = args.get("additional_permissions")
        wanted = (normalize_permission_profile(requested_profile, cwd=cwd_path, workspace_roots=roots)
            if requested_profile is not None else None)
        granted = current_granted_permissions(captured_context)
        preapproved = (wanted is not None and granted is not None and intersect_permission_profiles(granted, wanted,
            cwd=cwd_path, workspace_roots=roots,
            **({"temporary_directories": tuple(ExecutorPath.parse(path) for path in policy.scope.temporary_directory_uris or ())}
                if environment is not None else {})) == wanted)
        sandbox_permissions = "use_default" if requested_mode == "with_additional_permissions" and preapproved else requested_mode
        if sandbox_permissions != "use_default" and not preapproved and rejected_approval_reason(permission, "sandbox_approval") is not None:
            raise ValueError(f"approval policy is {permission.approval_policy}; reject command — you cannot ask for escalated permissions if the approval policy is {permission.approval_policy}")
        if (not preapproved and not context.model_execution.config.feature_flags.enabled("exec_permission_approvals", default=False)
                and (requested_mode == "with_additional_permissions" or requested_profile is not None)):
            raise ValueError("additional permissions are disabled; enable `features.exec_permission_approvals` before using `with_additional_permissions`")
        if requested_mode == "with_additional_permissions":
            if not preapproved and permission.approval_policy != "on-request":
                raise ValueError(f"approval policy is {permission.approval_policy}; reject command — you cannot request additional permissions unless the approval policy is OnRequest")
            if wanted is None:
                raise ValueError("missing `additional_permissions`; provide at least one of `network` or `file_system` when using `with_additional_permissions`")
            if wanted.is_empty:
                raise ValueError("`additional_permissions` must include at least one requested permission in `network` or `file_system`")
            policy = policy.with_additional_permissions(wanted)
        elif requested_profile is not None:
            raise ValueError("`additional_permissions` requires `sandbox_permissions` set to `with_additional_permissions`")
        if environment is not None and context.run_context.network_step is not None:
            network_policy = context.run_context.network_step.policy_for(environment)
            policy = replace(policy, network_policy=network_policy.for_scope(policy.scope) if network_policy is not None else None)
        if environment is not None and environment.is_remote:
            from backend.permissions.environment_command import executor_shell_argv

            raw = executor_shell_argv(environment, str(args["command"]), shell=args.get("shell"), login=args.get("login"),
                fallback_shell=context.run_context.user_shell)
        else:
            raw = command_shell_argv(str(args["command"]), shell=args.get("shell"), login=args.get("login"),
                allow_login_shell=environment.config.allow_login_shell if environment is not None else context.run_context.exec_policy_owner.allow_login_shell,
                native_windows_sandbox=sys.platform == "win32" and not policy.disable_os_sandbox,
                wrap_powershell=False)
        argv = list(raw)
        import ntpath
        if ntpath.splitext(ntpath.basename(raw[0]))[0].lower() in {"pwsh", "powershell"}:
            argv[-1] = powershell_script(raw[-1])
        capture = FrozenExecCommand.capture(request, argv=argv, policy_argv=raw, cwd=cwd_path,
            configured_shell=raw[0], permission=permission, profile=policy.permission_profile,
            resolved_profile=None if environment is not None else policy.resolve(cwd=cwd_path),
            environment_scope=policy.scope if environment is not None else None,
            windows_sandbox_level=(policy.scope.windows_sandbox_level if environment is not None
                else "elevated" if sys.platform == "win32" and not policy.disable_os_sandbox else "disabled"),
            sandbox_permissions=sandbox_permissions,
            prefix_rule=args.get("prefix_rule"), environment_id=context.environment_id, policy=policy,
            additional_permissions_present=(wanted is not None or (not granted.is_empty and requested_mode != "require_escalated")))
        return context.run_context.exec_policy_owner.command_requirement(capture, model=context.model_execution,
            environment_requirements=environment.config.exec_policy if environment is not None else None)

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
            yield_ms = command_wait_ms(args.get("yield_time_ms"))
        except ValueError as exc:
            return self._error_result(str(exc))
        if timeout is None and not run_in_background:
            timeout = DEFAULT_TIMEOUT
        escalated = args.get("sandbox_permissions") == "require_escalated"
        description = args.get("description", "")
        shell = args.get("shell")
        login = args.get("login")
        tty = args.get("tty", False)

        if not command:
            return self._error_result("Missing command parameter")
        if env_error:
            return self._error_result(env_error)
        native = context.exec_policy_decision if context is not None else None
        if native is None:
            return self._error_result("The command has no frozen native execution-policy request.")
        if context.authorization_digest != native.command.execution_digest:
            return self._error_result("The command execution identity differs from its approved request.")
        from backend.agent.final_tool_request import canonical_tool_request_digest
        if canonical_tool_request_digest(self.policy_identity, args) != native.command.tool_request_digest:
            return self._error_result("The command arguments differ from the frozen execution request.")
        approved = context.authorization_digest in context.metadata.get("_approved_request_digests", set())
        if native.kind == "forbidden":
            return self._error_result(native.reason)
        if native.kind == "needs_approval" and not approved:
            return self._error_result("This captured command requires approval before execution.")
        override = native.first_attempt_override(already_approved=approved)
        escalated = override != "no_override"
        effective_cwd = str(native.command.cwd)
        base_policy = native.command.policy
        if escalated and not base_policy.allow_unsandboxed_commands:
            return self._error_result("Managed sandbox policy does not allow this command to run outside the sandbox.")
        background_manager = self._resolve_background_manager(context)

        if run_in_background:
            return await self._execute_background(
                command,
                effective_cwd,
                timeout,
                description,
                background_manager,
                context,
                escalated=escalated,
                env_overrides=env_overrides,
                shell=shell, login=login, tty=tty,
                original_tool_args=deepcopy(args),
            )

        if background_manager is not None and context is not None and context.command_scope_id:
            return await self._execute_background(
                command, effective_cwd, timeout if args.get("timeout") is not None else None, description, background_manager, context,
                escalated=escalated, env_overrides=env_overrides, wait_ms=yield_ms,
                max_chars=args.get("max_chars", TASK_OUTPUT_DEFAULT_CHARS),
                shell=shell, login=login, tty=tty,
                original_tool_args=deepcopy(args),
            )
        if "yield_time_ms" in args:
            return self._error_result("This session has no command manager for yielding a live process. Use the managed desktop session or omit yield_time_ms for a bounded foreground call.")

        return await self._execute_foreground(
            command,
            effective_cwd,
            timeout,
            context,
            escalated=escalated,
            env_overrides=env_overrides,
            max_chars=args.get("max_chars"),
            shell=shell, login=login, tty=tty,
        )

    def _resolve_cwd(
        self,
        cwd: str | None,
        context: ToolExecutionContext | None,
        *,
        allow_workspace_escape: bool = False,
    ) -> str | None:
        if context is not None and context.turn_environment is not None:
            scope = context.permission.environment_scope
            return scope.resolve_path(cwd or scope.cwd_uri).uri
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
                    if context is not None and permission_profile_allows_path(
                        current_granted_permissions(context), resolved,
                        cwd=workspace_root, workspace_root=workspace_root,
                    ):
                        return str(resolved)
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
        wait_ms: int | None = None,
        max_chars: int = TASK_OUTPUT_DEFAULT_CHARS,
        shell: str | None = None,
        login: bool | None = None,
        tty: bool = False,
        original_tool_args: dict[str, Any] | None = None,
    ) -> ToolResult:
        """Start once under the process owner; wait or hand back its live handle."""
        if background_manager is None:
            return self._error_result(
                "Background command execution is unavailable. Restart the backend session "
                "or run long-lived commands in an external terminal."
            )

        native = context.exec_policy_decision if context is not None else None
        workspace = None if native is not None else (
            Path(context.workspace_root).expanduser().resolve()
            if context is not None and getattr(context, "workspace_root", None)
            else Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        )
        # Claude Code clears the foreground timeout when a command becomes a
        # background task. The session-owned manager remains responsible for
        # cancellation, process-tree cleanup, output bounds, and completion.
        background_timeout_ms = int(timeout * 1000) if wait_ms is not None and timeout is not None else 0
        if native is not None:
            base_policy = replace(native.command.policy, timeout=timeout if wait_ms is not None and timeout is not None else 0)
        else:
            base_policy = _workspace_sandbox_policy(
                workspace,
                context,
                timeout=timeout if wait_ms is not None and timeout is not None else 0,
                env_overrides=env_overrides,
            )
        baseline_policy = base_policy.escalated_preserving_denied_reads() if native is None and _is_bypass_mode(context) else base_policy
        if native is None:
            base_policy = sandbox_policy_with_grants(base_policy, context)
        policy = (
            base_policy.escalated_preserving_denied_reads()
            if escalated or (native is None and _is_bypass_mode(context))
            else base_policy
        )
        policy = _with_network_approval_context(policy, context)
        from backend.terminal.executor_runner import command_capability

        capability = command_capability(policy, cwd=cwd)
        if not capability.available:
            return self._error_result(
                f"The required sandbox is unavailable, so the background command was not started: {capability.reason}"
            )

        try:
            if native is not None:
                argv = list(native.command.argv)
            else:
                argv = command_shell_argv(
                    command, shell=shell, login=login,
                    allow_login_shell=command_login_allowed(workspace),
                    native_windows_sandbox=capability.backend == "windows-elevated-wfp",
                )
            background_kwargs: dict[str, Any] = {
                # Preserve the model/user command for UI, persistence, and
                # diagnostics while the manager executes the prepared shell
                # form. Encoded PowerShell must never replace the public text.
                "command": command,
                "argv": argv,
                "tty": tty,
                "cwd": cwd,
                "timeout_ms": background_timeout_ms,
                "description": description or command[:60],
                "sandbox_policy": policy,
            }
            if context is not None:
                from backend.terminal.stdin_authority import TerminalLaunchAuthority

                override = escalated and not _is_bypass_mode(context)
                background_kwargs.update(
                    terminal_authority=TerminalLaunchAuthority.capture(
                        baseline_policy=baseline_policy, effective_policy=policy,
                        launch_permissions="require_escalated" if override and policy.disable_os_sandbox else "use_default",
                        filesystem_escalated=override and not policy.disable_os_sandbox,
                        additional_permissions=current_granted_permissions(context), internal_permissions=None,
                        environment_id=context.environment_id,
                    ),
                    original_tool_call_id=context.tool_call_id,
                    original_tool_args=deepcopy(original_tool_args or {}),
                )
            conversation_id = context.command_scope_id if context is not None else ""
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
                **({"backgrounded": False, "stream_callback": getattr(context, "stream_callback", None)}
                   if wait_ms is not None else {}),
            )
            from backend.permissions.environment_command import EnvironmentCommandPolicy

            if isinstance(policy, EnvironmentCommandPolicy):
                await background_manager.wait_for_startup(bg_cmd.command_id, conversation_id=conversation_id)
        except (RuntimeError, ValueError) as exc:
            return self._error_result(str(exc))

        if wait_ms is not None:
            from backend.tools.monitor_tool import MonitorTool

            try:
                # The registry owns cancellation of this tool task. A second
                # token waiter can start cleanup before that cancellation and
                # have the same cleanup interrupted by the registry.
                await background_manager.wait(
                    bg_cmd.command_id, conversation_id=conversation_id, wait_ms=wait_ms,
                )
                await background_manager.detach(bg_cmd.command_id, conversation_id=conversation_id)
            except asyncio.CancelledError:
                await background_manager.cancel(bg_cmd.command_id, conversation_id=conversation_id)
                raise
            result = MonitorTool()._command_snapshot(
                background_manager, bg_cmd.command_id, {"output_mode": "head_tail", "max_chars": max_chars}, conversation_id=conversation_id,
            )
            result.result_kind = "command"
            if bg_cmd.lifecycle.error.get("kind") == "timeout":
                result.status = "timeout"
            elif bg_cmd.status == "cancelled":
                result.status = "cancelled"
            result.display_summary = (
                f"Command running: {bg_cmd.command_id}"
                if bg_cmd.status == "running" else f"Exit code: {bg_cmd.exit_code}"
            )
            result.runtime_metadata.update(command_id=bg_cmd.command_id, process_status=bg_cmd.status, exit_code=bg_cmd.exit_code)
            if bg_cmd.status == "running":
                result.content += (
                    f"\nThe process is still running; do not restart it. Continue with monitor(action='status', "
                    f"command_id='{bg_cmd.command_id}', cursor=<next_cursor>, yield_time_ms=10000). "
                    "Use action='write_stdin' for input or action='cancel' to stop this process."
                )
            elif bg_cmd.exit_code is not None:
                result.content = f"Exit code: {bg_cmd.exit_code}\n" + result.content
                if bg_cmd.status == "failed" and not bg_cmd.cleanup_pending and bg_cmd.lifecycle.error.get("kind") != "timeout":
                    result.content += command_sandbox_recovery_hint(
                        bg_cmd.output, bg_cmd.exit_code,
                        sandbox_active=capability.filesystem_isolated or capability.network_isolated,
                        escalated=escalated, allow_unsandboxed=policy.allow_unsandboxed_commands,
                    )
                    hint = (_windows_command_portability_hint(command, bg_cmd.output, bg_cmd.exit_code)
                            if Path(argv[0]).stem in {"pwsh", "powershell"} else "")
                    if hint:
                        result.content += "\n\n" + hint
            return result

        if bg_cmd.status != "running":
            from backend.tools.monitor_tool import MonitorTool

            result = MonitorTool()._command_snapshot(background_manager, bg_cmd.command_id,
                {"output_mode": "head_tail", "max_chars": max_chars}, conversation_id=conversation_id)
            result.runtime_metadata.update(command_id=bg_cmd.command_id, process_status=bg_cmd.status, exit_code=bg_cmd.exit_code)
            return result

        sandbox_label = (
            "executor permissions" if isinstance(policy, EnvironmentCommandPolicy) else "bypass execution"
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
            runtime_metadata={"command_id": bg_cmd.command_id, "next_cursor": 0, "stdout_preview": "", "stderr_preview": "",
                              "terminal_hook": {"command_id": bg_cmd.command_id, "ready": False}},
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
        max_chars: int | None = None,
        shell: str | None = None,
        login: bool | None = None,
        tty: bool = False,
    ) -> ToolResult:
        """Execute a foreground shell command via SandboxRunner.

        When ``escalated`` is set (and the call passed the escalation approval
        gate), the command runs with bypass execution + network, mirroring the
        "retry outside the sandbox" path. Otherwise it runs under the normal
        workspace-write, no-network policy.
        """
        native = context.exec_policy_decision if context is not None else None
        workspace = None if native is not None else (
            Path(context.workspace_root).expanduser().resolve()
            if context is not None and getattr(context, "workspace_root", None)
            else Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        )
        if native is not None:
            base_policy = replace(native.command.policy, timeout=timeout)
        else:
            base_policy = _workspace_sandbox_policy(
                workspace,
                context,
                timeout=timeout,
                env_overrides=env_overrides,
            )
        if native is None:
            base_policy = sandbox_policy_with_grants(base_policy, context)
        policy = (
            base_policy.escalated_preserving_denied_reads()
            if escalated or (native is None and _is_bypass_mode(context))
            else base_policy
        )
        policy = _with_network_approval_context(policy, context)
        from backend.terminal.executor_runner import command_runner

        runner = command_runner(policy)

        cancel_event = getattr(context, "cancel_event", None) if context else None
        stream_cb = getattr(context, "stream_callback", None) if context else None

        try:
            if native is not None:
                argv = list(native.command.argv)
            else:
                argv = command_shell_argv(
                    command, shell=shell, login=login,
                    allow_login_shell=command_login_allowed(workspace),
                    native_windows_sandbox=runner.capability(cwd=cwd).backend == "windows-elevated-wfp",
                )
        except ValueError as exc:
            return self._error_result(str(exc))
        try:
            result = await runner.run(
                command,
                cwd=cwd,
                cancel_event=cancel_event,
                stream_callback=stream_cb,
                argv=argv,
                tty=tty,
                preserve_full_output=True,
            )
        except Exception as exc:
            if getattr(exc, "cleanup_pending", False):
                exc.cleanup_receipt = await self._retain_cleanup(
                    runner, command, cwd, context, str(exc.cleanup_reason),
                )
            raise
        cleanup_receipt = (
            await self._retain_cleanup(runner, command, cwd, context, result.cleanup_reason)
            if result.cleanup_pending else {}
        )

        return await to_thread_cancel_safe(
            self._foreground_result, result, command, timeout, context, policy,
            escalated, cleanup_receipt, max_chars, Path(argv[0]).stem in {"pwsh", "powershell"},
        )

    def _foreground_result(
        self,
        result: SandboxResult,
        command: str,
        timeout: float | None,
        context: Any,
        policy: SandboxPolicy,
        escalated: bool,
        cleanup_receipt: dict[str, Any],
        max_chars: int | None,
        powershell: bool = True,
    ) -> ToolResult:
        """Read, retain and project the completed command's captured output."""
        sandbox_active = not policy.disable_os_sandbox
        stdout = read_captured_output(result.stdout_path, result.stdout)
        stderr = read_captured_output(result.stderr_path, result.stderr)
        captured_paths = (result.stdout_path, result.stderr_path)

        if result.sandbox_unavailable and sandbox_active:
            # The managed sandbox is required by policy but cannot be enforced.
            # The model may retry with the existing explicit escalation fields,
            # which are guarded by a fresh user approval in check_permission().
            cleanup_captured_output(*captured_paths)
            retry_hint = (
                ' Retry with sandbox_permissions="require_escalated" and a concise justification; '
            "the user must explicitly approve bypass execution."
                if policy.allow_unsandboxed_commands
                else " Managed policy forbids unsandboxed fallback."
            )
            tool_result = self._error_result(
                "The workspace sandbox is unavailable on this host, so the command was not run."
                f"{retry_hint}"
            )
            tool_result.cleanup_receipt = cleanup_receipt
            return tool_result

        exit_code = result.exit_code

        # A shell command can change workspace files (sed -i, codegen, git
        # checkout, ...). Its affected paths are unknowable without parsing
        # shell syntax, so invalidate derived views of this command's workspace.
        from backend.tools.file_tools_common import invalidate_workspace_file_caches

        from backend.permissions.environment_command import EnvironmentCommandPolicy

        if not isinstance(policy, EnvironmentCommandPolicy):
            invalidate_workspace_file_caches(file_tree_changed=True, clear_file_state=True, mutation_policy=policy)

        output = stdout + stderr

        if result.cancelled:
            status = "Command execution was cancelled/interrupted"
            is_failed_exit = True
            result_status = "cancelled"
        elif result.timed_out:
            status = f"Command timed out after {timeout:g} seconds"
            is_failed_exit = True
            result_status = "timeout"
        elif result.execution_error:
            status = f"Command execution error: {result.execution_error}"
            is_failed_exit = True
            result_status = "failed"
        else:
            status = f"Exit code: {exit_code}"
            is_failed_exit = exit_code != 0
            result_status = "success"

        # Codex escalate-on-failure: if a sandboxed command failed in a way that
        # looks caused by the sandbox (network blocked, permission denied), tell
        # the model it may retry once with escalated permissions (user-approved).
        # Only when we actually ran sandboxed and the model hasn't already
        # escalated — never advertise escalation after it was granted.
        escalation_hint = (
            command_sandbox_recovery_hint(
                stderr, exit_code, sandbox_active=sandbox_active,
                escalated=escalated, allow_unsandboxed=policy.allow_unsandboxed_commands,
            ) if is_failed_exit and not result.cancelled and not result.timed_out and not result.execution_error else ""
        )
        if escalation_hint:
            status = f"{status}{escalation_hint}"

        portability_hint = _windows_command_portability_hint(
            command,
            stderr,
            exit_code,
        ) if powershell else ""
        if portability_hint:
            status = f"{status}\n\n{portability_hint}"

        if cleanup_receipt:
            recovery_id = cleanup_receipt["resource_id"]
            status += f"\nResource cleanup is still pending. Recovery command ID: {recovery_id}."
            if not cleanup_receipt.get("manual_recovery_required"):
                status += f" Use monitor(action='cancel', command_id='{recovery_id}') to retry cleanup."

        truncation = truncate_text_tail(output)
        if not truncation.truncated and (max_chars is None or len(output) <= max_chars):
            cleanup_captured_output(*captured_paths)
            return ToolResult(
                content=f"{status}\n\n{output}" if output else status,
                is_error=result.cancelled or result.timed_out or bool(result.execution_error),
                status=result_status,
                error_kind="execution_error" if result.execution_error else None,
                runtime_metadata={"stdout_preview": stdout, "stderr_preview": stderr, "exit_code": exit_code, "hook_output": output,
                    "execution_error": result.execution_error, "network_policy_decisions": list(result.network_audit),
                    "command_completed": not (result.cancelled or result.timed_out or result.execution_error),
                    "process_status": result_status if result.cancelled or result.timed_out else "failed" if is_failed_exit else "completed"},
                cleanup_receipt=cleanup_receipt,
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
        preview = truncation.content
        if max_chars is not None:
            preview = preview_text_output(output, max_chars)
            truncation_notice = f"Output view limited to {max_chars} characters plus omission marker; full output retained."
        if full_output_reference:
            truncation_notice += f" Full output: {full_output_reference}"

        return ToolResult(
            content=f"{status}\n\n[{truncation_notice}]",
            runtime_metadata={"stdout_preview": preview, "stderr_preview": "", "exit_code": exit_code, "hook_output": preview,
                "execution_error": result.execution_error, "network_policy_decisions": list(result.network_audit),
                "command_completed": not (result.cancelled or result.timed_out or result.execution_error),
                "process_status": result_status if result.cancelled or result.timed_out else "failed" if is_failed_exit else "completed"},
            artifact_id=artifact_id or None,
            artifact_preview=preview,
            is_error=result.cancelled or result.timed_out or bool(result.execution_error),
            status=result_status,
            error_kind="execution_error" if result.execution_error else None,
            cleanup_receipt=cleanup_receipt,
        )

    async def _retain_cleanup(self, runner, command, cwd, context, reason) -> dict[str, Any]:
        manager = self._resolve_background_manager(context)
        owner = context.command_scope_id if context is not None else ""
        process = runner.process
        from backend.terminal.executor_runner import ExecutorCommandProcess

        receipt = {
            "resource_kind": "command",
            "resource_id": (process.process_id if isinstance(process, ExecutorCommandProcess)
                else str(process.pid) if process is not None else runner._container_name),
            "reason": reason,
            "requested": True,
            "acknowledged": True,
            "completed": False,
            "pending": 1,
        }
        if manager is not None and owner:
            metadata = getattr(context, "metadata", None) or {}
            retained = await manager.retain_foreground_cleanup(
                runner=runner, command=command, cwd=cwd or "",
                conversation_id=owner, task_id=str(getattr(context, "task_id", "") or ""),
                parent_run_id=str(metadata.get("run_id") or ""), reason=reason,
            )
            receipt["resource_id"] = retained.command_id
        else:
            receipt["manual_recovery_required"] = True
        return receipt
