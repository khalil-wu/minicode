"""Shell command execution tool.

Foreground and background commands share the same shell and sandbox policy.
On Windows, workspace-sandbox commands run in PowerShell inside the configured
Linux container; full-access or approved escalated commands run in host
PowerShell. Callers can select an explicit host shell only outside the sandbox.
"""

from __future__ import annotations

import base64
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
    TOOL_SIDE_EFFECT_NONE,
    TOOL_SIDE_EFFECT_WORKSPACE,
    MAX_TOOL_RESULT_BYTES,
    MAX_TOOL_RESULT_LINES,
    BaseTool,
    PermissionLevel,
    ToolResult,
    ToolSchema,
    truncate_text_tail,
)

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext
    from backend.terminal.manager import BackgroundCommandManager

# Pi's Bash contract has no default timeout. Explicit deadlines are bounded
# only by the maximum supported Node timer value used upstream.
MAX_TIMEOUT_MS = 2_147_483_647
MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000
# Compatibility names retained for integrations that imported the old
# constants; unlike the previous implementation, the default is intentionally
# unbounded and the maximum is the upstream timer limit.
DEFAULT_TIMEOUT: float | None = None
MAX_TIMEOUT = MAX_TIMEOUT_SECONDS

_WINDOWS_EXPLICIT_SHELL_RE = re.compile(
    r"^\s*(?:"
    r"powershell(?:\.exe)?|pwsh(?:\.exe)?"
    r"|cmd(?:\.exe)?\s*/[ck]"
    r"|bash(?:\.exe)?(?:\s|$)|sh(?:\.exe)?(?:\s|$)|wsl(?:\.exe)?(?:\s|$)"
    r")",
    re.IGNORECASE,
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _coerce_timeout(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("timeout must be a finite positive number of seconds")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a finite positive number of seconds") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise ValueError("timeout must be a finite positive number of seconds")
    if parsed > MAX_TIMEOUT_SECONDS:
        raise ValueError(f"timeout must not exceed {MAX_TIMEOUT_SECONDS:g} seconds")
    return parsed


def _validated_env(value: Any) -> tuple[dict[str, str], str]:
    # Some OpenAI-compatible providers serialize an omitted optional object as
    # an empty string. Treat only that empty sentinel as absent; non-empty
    # strings and all other invalid shapes remain rejected.
    if value is None or value == "":
        return {}, ""
    if not isinstance(value, dict):
        return {}, "env must be an object of string values"
    resolved: dict[str, str] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "")
        if not _ENV_NAME_RE.fullmatch(key):
            return {}, f"Invalid environment variable name: {key[:128]}"
        if not isinstance(raw_value, str):
            return {}, f"Environment variable {key} must be a string"
        if "\x00" in raw_value:
            return {}, f"Environment variable {key} contains a null byte"
        if len(raw_value) > 32_768:
            return {}, f"Environment variable {key} exceeds 32768 characters"
        resolved[key] = raw_value
    return resolved, ""


def _is_bypass_mode(context: Any = None) -> bool:
    permission = getattr(context, "permission", None)
    return getattr(permission, "mode", None) == "bypass"


def _sandbox_writable_roots(workspace: Path, context: Any = None) -> tuple[Path, ...]:
    """Resolve the structured path allowlist into enforceable write mounts.

    Shell text is intentionally not parsed.  The workspace is mounted read-only
    and only these concrete roots are over-mounted read-write by the OS sandbox.
    Wildcard rules contribute existing matches; a trailing ``/**`` contributes
    its concrete directory prefix.  Rules that cannot be represented without
    broadening access simply contribute no writable mount (fail closed).
    """

    permission = getattr(context, "permission", None)
    constraints = getattr(permission, "filesystem_constraints", {}) or {}
    raw_allowlist = constraints.get("allowlist")
    if raw_allowlist is None:
        checker = getattr(context, "permission_checker", None)
        snapshot = checker.policy_snapshot() if checker is not None else {}
        raw_allowlist = snapshot.get("path_allowlist", ["."])

    rules = [str(item or "").strip() for item in (raw_allowlist or [])]
    if not rules:
        return (workspace,)

    resolved: list[Path] = []
    seen: set[Path] = set()

    def _add(candidate: Path) -> None:
        path = candidate.expanduser().resolve()
        try:
            path.relative_to(workspace)
        except ValueError:
            return
        if not path.exists() or path in seen:
            return
        seen.add(path)
        resolved.append(path)

    for raw_rule in rules:
        normalized = raw_rule.replace("\\", "/")
        while normalized.startswith("./"):
            normalized = normalized[2:]
        normalized = normalized.rstrip("/")
        if normalized in {"", "."}:
            return (workspace,)

        wildcard_at = min(
            (normalized.find(token) for token in ("*", "?", "[") if token in normalized),
            default=-1,
        )
        if wildcard_at < 0:
            _add(workspace / normalized)
            continue

        if normalized.endswith("/**"):
            prefix = normalized[:-3].rstrip("/")
            if prefix and not any(token in prefix for token in ("*", "?", "[")):
                _add(workspace / prefix)
        try:
            matches = workspace.glob(normalized)
        except (OSError, ValueError):
            continue
        for match in matches:
            _add(match)

    return tuple(resolved)


def _workspace_sandbox_policy(
    workspace: Path,
    context: Any,
    *,
    timeout: float | None,
    env_overrides: dict[str, str] | None = None,
) -> SandboxPolicy:
    return SandboxPolicy(
        workspace_root=workspace,
        writable_roots=_sandbox_writable_roots(workspace, context),
        allow_network=bool(getattr(context, "allow_network", False)),
        env_overrides=dict(env_overrides or {}),
        timeout=timeout,
    )


def _windows_powershell_executable() -> str:
    """Prefer PowerShell 7 while retaining a stock-Windows fallback."""

    return "pwsh.exe" if shutil.which("pwsh.exe") else "powershell.exe"


def _windows_powershell_shell_command(
    command: str,
    *,
    cwd: str | Path | None = None,
) -> str:
    location = ""
    if cwd:
        escaped_cwd = str(Path(cwd).expanduser().resolve()).replace("'", "''")
        location = f"Set-Location -LiteralPath '{escaped_cwd}'; "
    prelude = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$ProgressPreference = 'SilentlyContinue'; "
        f"{location}"
        "$global:LASTEXITCODE = $null; "
    )
    # PowerShell can return 1 for a successful native command when the command
    # redirects stderr into stdout (``2>&1``). Preserve the native process exit
    # code explicitly so test runners that report progress on stderr aren't
    # presented to the agent as failures.
    epilogue = (
        "; $minicodeCommandSucceeded = $?; "
        "$minicodeNativeExit = $global:LASTEXITCODE; "
        "if ($null -ne $minicodeNativeExit) { exit $minicodeNativeExit } "
        "elseif ($minicodeCommandSucceeded) { exit 0 } else { exit 1 }"
    )
    script = f"{prelude}{command}{epilogue}"
    # -EncodedCommand avoids cmd.exe corrupting nested quotes, pipes, dollar
    # expressions, or non-ASCII text when SandboxRunner launches its command
    # string through the host shell.
    encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.list2cmdline(
        [
            _windows_powershell_executable(),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded_script,
        ]
    )


def _host_shell_command(command: str, *, cwd: str | Path | None = None) -> str:
    if sys.platform == "win32":
        command = normalize_windows_shell_command(command)
    if sys.platform == "win32" and not _WINDOWS_EXPLICIT_SHELL_RE.match(command):
        return _windows_powershell_shell_command(command, cwd=cwd)
    return command


def _model_shell_description() -> str:
    if sys.platform == "win32":
        host_shell = "PowerShell 7" if shutil.which("pwsh.exe") else "Windows PowerShell 5.1"
        return (
            "Execute a shell command. In normal workspace modes it runs in PowerShell 7 inside the "
            "MiniCode Linux sandbox container; use workspace-relative paths and cross-platform tools. "
            f"In full-access or approved escalated mode it runs in host {host_shell}. "
            "Use the cwd and env arguments instead of shell cd/env setup, and do not assume && is available."
        )
    return "Execute a shell command for builds, tests, installs, git, processes, and scripts."


# Stderr signatures that typically indicate the OS sandbox (network namespace /
# seatbelt / fs restriction) blocked the command rather than a logic error in
# the command itself. Used to decide whether to offer Codex-style escalation.
_SANDBOX_DENIAL_PATTERNS = (
    "network is unreachable",
    "no route to host",
    "read-only file system",
    "enetunreach",
    "sandbox-exec: deny",
    "sandbox: deny",
    "bwrap:",
)

# Invocation-level scheduling policy. These expressions do not authorize or
# block commands; the permission checker and OS sandbox remain authoritative.
# They only tell retry, cache, checkpoint, and concurrency machinery what this
# concrete command can affect.
_DESTRUCTIVE_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:^|[;&|]\s*)rm\b(?=[^\n]*(?:-[^\s]*[rf]|--recursive|--force))",
        r"(?:^|[;&|]\s*)remove-item\b(?=[^\n]*(?:-recurse|-force))",
        r"(?:^|[;&|]\s*)(?:del|erase|rmdir|rd)\b",
        r"\bgit\s+(?:reset\s+--hard|clean\s+-[^\s]*f|branch\s+-D)\b",
        r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)\b",
        r"\b(?:mkfs(?:\.[a-z0-9]+)?|format)\b",
        r"\bdd\b[^\n]*\bof\s*=",
    )
)
_EXTERNAL_COMMAND_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"(?:^|[;&|]\s*)git\s+(?:push|fetch|pull|clone|remote\s+(?:add|remove|set-url))\b",
        r"(?:^|[;&|]\s*)gh\s+(?:api|pr|issue|release|repo|workflow|run)\b",
        r"(?:^|[;&|]\s*)(?:curl|wget|invoke-webrequest|invoke-restmethod)\b",
        r"(?:^|[;&|]\s*)(?:ssh|scp|sftp|rsync)\b",
        r"(?:^|[;&|]\s*)(?:docker|podman|kubectl|helm|terraform)\b",
        r"(?:^|[;&|]\s*)(?:npm|pnpm|yarn|bun)\s+(?:install|add|remove|publish)\b",
        r"(?:^|[;&|]\s*)(?:pip|pip\d+|uv\s+pip|conda|mamba|micromamba)\s+(?:install|uninstall|remove|create)\b",
    )
)


def _command_side_effect_kind(args: dict[str, Any] | None) -> str:
    """Classify shell execution conservatively without parsing shell syntax.

    Codex and Claude Code either run commands inside a real sandbox or use a
    complete command parser. MiniCode has neither parser on this path, so a
    command string never earns the read-only scheduling or permission fast
    path. Dedicated read/search/list tools remain available for that work.
    """
    payload = args or {}
    command = str(payload.get("command") or "").strip()
    if not command:
        return TOOL_SIDE_EFFECT_EXTERNAL

    from backend.permissions.checker import (
        check_catastrophic_command,
        protected_write_command_reason,
    )

    allowed, _reason = check_catastrophic_command(command)
    # A shell write to a protected path (.claude/**, .git/**, settings.json, …)
    # is classified destructive so it always requires confirmation, never a
    # silent workspace-write. The dedicated file tools already block these
    # paths; this keeps the shell path from being an unconfirmed bypass.
    if (
        not allowed
        or protected_write_command_reason(command)
        or any(pattern.search(command) for pattern in _DESTRUCTIVE_COMMAND_PATTERNS)
    ):
        return TOOL_SIDE_EFFECT_DESTRUCTIVE
    if _as_bool(payload.get("with_escalated_permissions", False)):
        return TOOL_SIDE_EFFECT_EXTERNAL
    if any(pattern.search(command) for pattern in _EXTERNAL_COMMAND_PATTERNS):
        return TOOL_SIDE_EFFECT_EXTERNAL
    # Do not infer safety from shell text. This keeps commands out of plan
    # mode, speculative execution, concurrent read batches, and result cache.
    return TOOL_SIDE_EFFECT_WORKSPACE


def _looks_like_sandbox_denial(stderr: str, exit_code: int) -> bool:
    """Heuristic: did the sandbox (not the command's own logic) cause this failure?

    Only accept signatures tied to an OS/network sandbox. Generic application
    errors such as EACCES, connection refused, or "operation not permitted"
    must not turn the full-access gate into routine error recovery.
    """
    if exit_code == 0:
        return False
    text = (stderr or "").lower()
    if not text:
        return False
    return any(sig in text for sig in _SANDBOX_DENIAL_PATTERNS)


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
        """Apply an outer watchdog only when the caller supplied a deadline."""
        if _as_bool(args.get("run_in_background", False)):
            return None
        return _coerce_timeout(args.get("timeout"))

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
                            "and long-lived processes, then inspect it with monitor."
                        ),
                    },
                    "timeout": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": MAX_TIMEOUT_SECONDS,
                        "description": "Optional foreground timeout in seconds; there is no default timeout.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short label for a background command.",
                    },
                    "with_escalated_permissions": {
                        "type": "boolean",
                        "description": "Request execution outside the workspace sandbox; explicit user approval is required unless already in full-access mode.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Concise reason the sandbox cannot perform this command, shown in the approval request.",
                    },
                },
                "required": ["command"],
            },
        )

    def streamed_input_preview(self, args: dict[str, Any]) -> dict[str, Any]:
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
        from backend.permissions.checker import check_catastrophic_command

        allowed, reason = check_catastrophic_command(command)
        return "" if allowed else reason

    def get_side_effect_kind(self, args: dict[str, Any] | None = None) -> str:
        return _command_side_effect_kind(args)

    def is_idempotent(self, args: dict[str, Any] | None = None) -> bool:
        return False

    def check_permission(self, args=None, context=None):
        """Force human approval whenever the model requests sandbox escalation.

        Codex pattern: a model-initiated request to run outside the sandbox is
        never silently granted. When ``with_escalated_permissions`` is set we
        require a CONFIRM gate so the user sees the justification and approves
        before the command runs with full access. Otherwise defer to the
        centralized policy (which may auto-allow read-only commands).
        """
        from backend.tools.base import PermissionLevel

        command = str((args or {}).get("command") or "").strip()
        if command:
            from backend.permissions.checker import check_catastrophic_command

            allowed, _reason = check_catastrophic_command(command)
            if not allowed:
                return PermissionLevel.ALWAYS_DENY
        if args and _as_bool(args.get("with_escalated_permissions", False)):
            # context here is a PermissionContext (mode on it directly). In bypass
            # mode the command already runs unsandboxed, so escalation is moot;
            # in every other mode the escalation must be confirmed by the user.
            mode = getattr(context, "mode", None)
            if mode != "bypass":
                return PermissionLevel.CONFIRM
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
                        "description": "Optional foreground timeout in seconds; there is no default timeout.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Run under a persistent command id; use for dev servers, watchers, or background "
                            "services, then inspect live output with monitor."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional label for the background command.",
                    },
                    "with_escalated_permissions": {
                        "type": "boolean",
                        "description": "Request full-access execution after a sandbox block; user approval is required. Provide justification.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Concise reason shown to the user when with_escalated_permissions is true.",
                    },
                },
                "required": ["command"],
            },
        )

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
        try:
            timeout = _coerce_timeout(args.get("timeout"))
        except ValueError as exc:
            return self._error_result(str(exc))
        run_in_background = _as_bool(args.get("run_in_background", False))
        escalated = _as_bool(args.get("with_escalated_permissions", False))
        description = args.get("description", "")

        if not command:
            return self._error_result("Missing command parameter")
        if env_error:
            return self._error_result(env_error)

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
            )

        return await self._execute_foreground(
            command,
            effective_cwd,
            timeout,
            context,
            escalated=escalated,
            env_overrides=env_overrides,
        )

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
        if _is_bypass_mode(context) or escalated:
            policy = SandboxPolicy.danger_full_access(
                timeout=0,
                env_overrides=env_overrides,
            )
        else:
            policy = _workspace_sandbox_policy(
                workspace,
                context,
                timeout=0,
                env_overrides=env_overrides,
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

        sandbox_label = "full access" if policy.disable_os_sandbox else "OS sandbox"
        return ToolResult(
            content=(
                f"Background command started (ID: {bg_cmd.command_id})\n"
                f"Command: {command[:100]}\n"
                f"Working directory: {bg_cmd.cwd}\n"
                f"Sandbox: {sandbox_label}; background execution uses the same policy as foreground commands.\n"
                f"Use monitor(command_id='{bg_cmd.command_id}') to inspect live output and status."
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
        gate), the command runs with full access + network, mirroring Codex's
        "retry outside the sandbox" path. Otherwise it runs under the normal
        workspace-write, no-network policy.
        """
        workspace = (
            Path(context.workspace_root).expanduser().resolve()
            if context is not None and getattr(context, "workspace_root", None)
            else Path(cwd).expanduser().resolve() if cwd else Path.cwd().resolve()
        )
        sandbox_active = True
        if _is_bypass_mode(context) or escalated:
            policy = SandboxPolicy.danger_full_access(
                timeout=timeout,
                env_overrides=env_overrides,
            )
            sandbox_active = False
        else:
            policy = _workspace_sandbox_policy(
                workspace,
                context,
                timeout=timeout,
                env_overrides=env_overrides,
            )
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
            # Codex fails closed when the requested sandbox cannot be enforced.
            # The model may retry with the existing explicit escalation fields,
            # which are guarded by a fresh user approval in check_permission().
            cleanup_captured_output(*captured_paths)
            return self._error_result(
                "The workspace sandbox is unavailable on this host, so the command was not run. "
                "Retry with with_escalated_permissions=true and a concise justification; "
                "the user must explicitly approve full-access execution."
            )

        exit_code = result.exit_code

        # A shell command can change workspace files (sed -i, codegen, git
        # checkout, ...); invalidate the grep/glob/list caches so a later search
        # never returns pre-command results. Claude Code has no such cache (it
        # runs ripgrep every call) — this keeps MiniCode's cache from going stale
        # after a command. Lazy import avoids a module-load cycle.
        try:
            from backend.tools.file_tools_common import clear_list_files_cache

            clear_list_files_cache()
        except Exception:
            pass

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
            metadata = getattr(context, "metadata", None)
            tool_call_id = str(
                metadata.get("_current_tool_call_id")
                if isinstance(metadata, dict)
                else ""
            ).strip() or "run_command"
            persisted = persist_tool_result(
                output,
                tool_call_id,
                "run_command",
                force=True,
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
