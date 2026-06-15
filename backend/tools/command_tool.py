"""Shell command execution tool.

Foreground commands run through SandboxRunner. Background commands return a
command id immediately and are explicitly marked as unsandboxed until the
background manager is moved behind the same sandbox abstraction.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.artifact.store import ArtifactStore
from backend.sandbox import SandboxPolicy, SandboxRunner
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext
    from backend.terminal.manager import BackgroundCommandManager

COMMAND_OUTPUT_TOKEN_LIMIT = 500
MAX_OUTPUT_LENGTH = 20_000
DEFAULT_TIMEOUT = 120
MAX_TIMEOUT = 600

_WINDOWS_START_B_PATTERNS = (
    re.compile(r"^\s*(?:cmd(?:\.exe)?\s*/c\s+)?start\s+/b\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:cmd(?:\.exe)?\s*/c\s+)?start\s+\"[^\"]*\"\s+/b\s+(.+?)\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:cmd(?:\.exe)?\s*/c\s+)?start\s+/b\s+\"[^\"]*\"\s+(.+?)\s*$", re.IGNORECASE),
)

_LONG_RUNNING_COMMAND_PATTERNS = (
    (re.compile(r"\b(?:npm|pnpm|yarn)\s+(?:run\s+)?(?:dev|start|serve)\b", re.IGNORECASE), "JavaScript dev server"),
    (re.compile(r"\b(?:vite|next|nuxt|astro)\s+(?:dev|start|preview)\b", re.IGNORECASE), "frontend dev server"),
    (re.compile(r"\b(?:uvicorn|hypercorn|gunicorn)\b", re.IGNORECASE), "web server"),
    (re.compile(r"\bflask\s+run\b", re.IGNORECASE), "Flask server"),
    (re.compile(r"\bstreamlit\s+run\b", re.IGNORECASE), "Streamlit server"),
    (re.compile(r"\bpython(?:\d+(?:\.\d+)?)?(?:\.exe)?\s+-m\s+http\.server\b", re.IGNORECASE), "HTTP server"),
    (re.compile(r"\bpython(?:\d+(?:\.\d+)?)?(?:\.exe)?\s+[^&|;]*manage\.py\s+runserver\b", re.IGNORECASE), "Django server"),
    (re.compile(r"\bpython(?:\d+(?:\.\d+)?)?\s+[^&|;]*server\.py\b", re.IGNORECASE), "Python server script"),
    (re.compile(r"\b(?:http-server|live-server)\b", re.IGNORECASE), "static file server"),
)

_COMMAND_PATH_TOKEN_RE = re.compile(
    r"""(?P<path>
        [A-Za-z]:[\\/][^\s"'<>|&;,)]+
        |\.[\\/][^\s"'<>|&;,)]+
        |\.\.[\\/][^\s"'<>|&;,)]+
    )""",
    re.VERBOSE,
)

_POWERSHELL_CMDLETS = frozenset(
    {
        "Add-Content",
        "Clear-Content",
        "Copy-Item",
        "ForEach-Object",
        "Get-ChildItem",
        "Get-Content",
        "Get-Location",
        "Get-Process",
        "Get-Service",
        "Measure-Object",
        "Move-Item",
        "New-Item",
        "Out-File",
        "Remove-Item",
        "Select-Object",
        "Select-String",
        "Set-Content",
        "Set-Location",
        "Sort-Object",
        "Test-Path",
        "Where-Object",
        "Write-Error",
        "Write-Output",
    }
)
_POWERSHELL_CMDLETS_LOWER = frozenset(cmdlet.lower() for cmdlet in _POWERSHELL_CMDLETS)

_POWERSHELL_EXPLICIT_SHELL_RE = re.compile(r"^\s*(?:powershell(?:\.exe)?|pwsh(?:\.exe)?|cmd(?:\.exe)?\s*/c)\b", re.IGNORECASE)
_POWERSHELL_TOKEN_RE = re.compile(r"(?:^|[|;&(]\s*)(?:&\s*)?(?P<name>[A-Za-z][A-Za-z0-9]+-[A-Za-z][A-Za-z0-9]+)\b")


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coerce_timeout(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = DEFAULT_TIMEOUT
    return max(1, min(parsed, MAX_TIMEOUT))


def _is_bypass_mode(context: Any = None) -> bool:
    permission = getattr(context, "permission", None)
    return getattr(permission, "mode", None) == "bypass"


def _unwrap_windows_start_background(command: str) -> str | None:
    for pattern in _WINDOWS_START_B_PATTERNS:
        match = pattern.match(command)
        if match:
            return match.group(1).strip()
    return None


def _looks_like_powershell_command(command: str) -> bool:
    stripped = (command or "").strip()
    if not stripped or _POWERSHELL_EXPLICIT_SHELL_RE.match(stripped):
        return False
    return any(
        match.group("name").lower() in _POWERSHELL_CMDLETS_LOWER
        for match in _POWERSHELL_TOKEN_RE.finditer(stripped)
    )


def _windows_powershell_shell_command(command: str) -> str:
    prelude = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    )
    return subprocess.list2cmdline(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"{prelude}{command}",
        ]
    )


def _host_shell_command(command: str) -> str:
    if sys.platform == "win32":
        command = normalize_windows_shell_command(command)
    if sys.platform == "win32" and _looks_like_powershell_command(command):
        return _windows_powershell_shell_command(command)
    return command


def _long_running_command_reason(command: str) -> str | None:
    stripped = command.strip()
    if not stripped:
        return None
    for pattern, reason in _LONG_RUNNING_COMMAND_PATTERNS:
        if pattern.search(stripped):
            return reason
    return None


def _command_path_allowlist_violation(
    command: str,
    *,
    workspace_root: Path | None,
    permission_checker: Any,
    permission_context: Any,
) -> str:
    if workspace_root is None or permission_checker is None:
        return ""
    if getattr(permission_context, "mode", None) == "bypass":
        return ""

    try:
        workspace = workspace_root.expanduser().resolve()
    except OSError:
        return ""

    checked: set[str] = set()
    for match in _COMMAND_PATH_TOKEN_RE.finditer(command or ""):
        raw_path = match.group("path").rstrip(".,")
        if not raw_path or raw_path in checked:
            continue
        checked.add(raw_path)
        candidate = Path(raw_path).expanduser()
        resolved = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
        try:
            relative = resolved.relative_to(workspace).as_posix()
        except ValueError:
            continue
        path_for_check = relative or "."
        is_allowed = permission_checker.is_path_allowed(path_for_check, context=permission_context)
        if not is_allowed:
            return (
                "Blocked run_command because the command references a workspace path outside "
                f"the allowed workspace paths: {raw_path}. Use write_file/edit_file within an "
                "allowed path, change the active workspace/permission rules, or switch to Full access if trusted."
            )
    return ""


class RunCommandTool(BaseTool):
    """Execute shell commands."""

    name = "run_command"
    description = (
        "Execute shell commands for builds, installs, git, processes, scripts, and system operations.\n\n"
        "Do NOT use run_command to create files — use write_file instead.\n"
        "Do NOT use run_command to edit files — use edit_file instead.\n"
        "Do NOT use run_command to read files — use read_file instead.\n"
        "Do NOT use run_command to search files — use grep_files or glob_files instead.\n"
        "Do NOT use run_command to list directories — use list_files instead.\n\n"
        "Reserve run_command for: builds, installs, git operations, process management, scripts, "
        "package managers, and anything that genuinely needs a shell.\n"
        "For long-running servers, use run_in_background mode."
    )
    permission = PermissionLevel.CONFIRM
    # Self-bounds stdout/stderr (MAX_OUTPUT_LENGTH) and artifacts large output.
    max_result_chars = None
    mutates_external_state = True  # shells can write files, commit, install, send
    timeout_seconds = float(DEFAULT_TIMEOUT)

    def is_read_only(self, args: dict[str, Any] | None = None) -> bool:
        """Classify this shell invocation as read-only when it only reads state.

        Mirrors CC's BashTool.isReadOnly: ``ls``/``git status``/``cat`` and other
        allowlisted commands without redirection or chaining are side-effect free.
        """
        from backend.permissions.checker import is_read_only_command

        if not args:
            return False
        return is_read_only_command(str(args.get("command", "")))

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
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute. Use absolute paths or paths relative to the workspace root.",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the command. Defaults to the active workspace root.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": f"Timeout in seconds. Default {DEFAULT_TIMEOUT}, max {MAX_TIMEOUT}.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Run in the background without waiting for output. "
                            "Use only for long-lived commands like dev servers, watchers, or background services."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional description for background command notifications.",
                    },
                },
                "required": ["command"],
            },
        )

    def get_spec(self):
        from backend.agent.harness.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="shell.execute",
            toolset="default",
            exposure="core",
            required_args=("command",),
            arg_roles={"command": "generated_content"},
            repair_policy={"command": "needs_model_generation"},
            empty_args_policy="repair_or_block",
            blocked_guidance=(
                "Missing command. Generate a concrete shell command first; "
                "for the current directory use pwd or PowerShell Get-Location."
            ),
        )

    async def execute(
        self,
        args: dict[str, Any],
        context: ToolExecutionContext | None = None,
    ) -> ToolResult:
        command = args.get("command", "")
        cwd = args.get("cwd")
        timeout = _coerce_timeout(args.get("timeout", DEFAULT_TIMEOUT))
        run_in_background = _as_bool(args.get("run_in_background", False))
        description = args.get("description", "")

        if not command:
            return self._error_result("Missing command parameter")

        from backend.permissions.checker import PermissionChecker, check_catastrophic_command

        allowed, reason = check_catastrophic_command(command)
        if not allowed:
            return self._error_result(reason)

        checker = PermissionChecker.instance() if hasattr(PermissionChecker, "instance") else None
        if checker is None and context and hasattr(context, "permission_checker"):
            checker = context.permission_checker
        if checker:
            allowed, reason = checker.validate_command(command)
            if not allowed:
                return self._error_result(reason)

        try:
            effective_cwd = self._resolve_cwd(cwd, context)
        except ValueError as exc:
            return self._error_result(str(exc))

        path_violation = _command_path_allowlist_violation(
            command,
            workspace_root=getattr(context, "workspace_root", None) if context else None,
            permission_checker=checker,
            permission_context=getattr(context, "permission", None) if context else None,
        )
        if path_violation:
            return self._error_result(path_violation)

        background_manager = self._resolve_background_manager(context)

        unwrapped_background_command = _unwrap_windows_start_background(command)
        if unwrapped_background_command:
            if background_manager is None:
                return self._error_result(
                    "Detected a Windows start /B background command, but no background "
                    "command manager is available for this session. Restart the backend "
                    "session or start the service in an external terminal."
                )
            command = unwrapped_background_command
            run_in_background = True
            if not description:
                description = "Windows start /B command"

        long_running_reason = _long_running_command_reason(command)
        if long_running_reason and not run_in_background:
            if background_manager is not None:
                run_in_background = True
                if not description:
                    description = long_running_reason
            else:
                return self._error_result(
                    f"Detected a likely long-running service command ({long_running_reason}). "
                    "It cannot run in the foreground because it would keep the agent busy. "
                    "Use run_in_background=true or start it in an external terminal."
                )

        if run_in_background:
            return await self._execute_background(
                command,
                effective_cwd,
                timeout,
                description,
                background_manager,
                auto_background=bool(long_running_reason or unwrapped_background_command),
            )

        return await self._execute_foreground(command, effective_cwd, timeout, context)

    def _resolve_cwd(self, cwd: str | None, context: ToolExecutionContext | None) -> str | None:
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
            if workspace_root and not _is_bypass_mode(context):
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
        timeout: int,
        description: str,
        background_manager: BackgroundCommandManager | None,
        *,
        auto_background: bool = False,
    ) -> ToolResult:
        """Start a background command and immediately return its command id."""
        if background_manager is None:
            return self._error_result(
                "Background command execution is unavailable. Restart the backend session "
                "or run long-lived commands in an external terminal."
            )

        try:
            shell_command = _host_shell_command(command)
            bg_cmd = await background_manager.run_background(
                command=shell_command,
                cwd=cwd,
                timeout_ms=timeout * 1000,
                description=description or command[:60],
            )
        except RuntimeError as exc:
            return self._error_result(str(exc))

        prefix = "Detected a long-running command and started it in the background.\n" if auto_background else ""
        return self._success_result(
            f"{prefix}Background command started (ID: {bg_cmd.command_id})\n"
            f"Command: {command[:100]}\n"
            f"Working directory: {bg_cmd.cwd}\n"
            "Sandbox: foreground commands use SandboxRunner; background commands are currently unsandboxed "
            "but still use sanitized env and workspace cwd validation.\n"
            "You will receive a notification when it completes."
        )

    async def _execute_foreground(
        self,
        command: str,
        cwd: str | None,
        timeout: int,
        context: Any,
    ) -> ToolResult:
        """Execute a foreground shell command via SandboxRunner."""
        workspace = Path(cwd) if cwd else Path.cwd()
        if _is_bypass_mode(context):
            policy = SandboxPolicy.danger_full_access(timeout=timeout)
        else:
            allow_network = getattr(context, "allow_network", False) if context else False
            policy = SandboxPolicy(
                writable_roots=(workspace,),
                allow_network=allow_network,
                timeout=timeout,
            )
        runner = SandboxRunner(policy)

        cancel_event = getattr(context, "cancel_event", None) if context else None
        stream_cb = getattr(context, "stream_callback", None) if context else None

        shell_command = _host_shell_command(command)
        result = await runner.run(
            shell_command,
            cwd=cwd,
            cancel_event=cancel_event,
            stream_callback=stream_cb,
        )

        if result.cancelled:
            return ToolResult(content="Command execution was cancelled/interrupted", is_error=True)

        if result.timed_out:
            return self._error_result(
                f"Command timed out after {timeout} seconds. Use run_in_background=true for long-running commands."
            )

        stdout = result.stdout
        stderr = result.stderr
        exit_code = result.exit_code

        if len(stdout) > MAX_OUTPUT_LENGTH:
            stdout = stdout[:MAX_OUTPUT_LENGTH] + f"\n\n[stdout truncated: showing first {MAX_OUTPUT_LENGTH} chars]"
        if len(stderr) > MAX_OUTPUT_LENGTH:
            stderr = stderr[:MAX_OUTPUT_LENGTH] + f"\n\n[stderr truncated: showing first {MAX_OUTPUT_LENGTH} chars]"

        output = stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}" if output else stderr

        status = f"Exit code: {exit_code}"
        if exit_code != 0:
            status += " (failed)"

        estimated_tokens = len(output) // 4
        is_failed_exit = exit_code != 0
        result_status = "failed" if is_failed_exit else None
        if estimated_tokens <= COMMAND_OUTPUT_TOKEN_LIMIT:
            return ToolResult(
                content=f"{status}\n\n{output}" if output else status,
                is_error=is_failed_exit,
                status=result_status,
            )

        artifact_id = self._artifact_store.save(
            content=output,
            source=f"run_command({command})",
            type="command_output",
        )
        preview = self._artifact_store.get_preview(artifact_id, lines=10)

        return ToolResult(
            content=f"{status} (output about {estimated_tokens} tokens)",
            artifact_id=artifact_id,
            artifact_preview=preview,
            is_error=is_failed_exit,
            status=result_status,
        )
