"""Command tool helpers shared by RunCommandTool and its control plane.

Extracted from ``backend/tools/command_tool.py`` so shell policy, sandbox
policy derivation and Windows portability helpers are independent of the tool
class that executes them.
"""

from __future__ import annotations

from backend.sandbox import (
    SandboxPolicy,
)
from backend.terminal.shell_commands import normalize_windows_shell_command
from backend.tools.base import (
    TOOL_SIDE_EFFECT_DESTRUCTIVE,
    TOOL_SIDE_EFFECT_EXTERNAL,
    TOOL_SIDE_EFFECT_WORKSPACE,
)
from dataclasses import replace
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any
import base64
import math
import os
import re
import shutil
import subprocess
import sys


DEFAULT_TIMEOUT: float = 120.0

MAX_TIMEOUT_MS = 600_000

MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000
DEFAULT_COMMAND_YIELD_MS = 10_000
MAX_COMMAND_YIELD_MS = 60_000


def command_wait_ms(value: Any, *, default: int = DEFAULT_COMMAND_YIELD_MS) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value) or not 0 <= value <= MAX_COMMAND_YIELD_MS:
        raise ValueError(f"yield_time_ms must be an integer between 0 and {MAX_COMMAND_YIELD_MS}")
    return int(value)


def command_sandbox_recovery_hint(output: str, exit_code: int, *, sandbox_active: bool, escalated: bool, allow_unsandboxed: bool) -> str:
    if sandbox_active and not escalated and allow_unsandboxed and _looks_like_sandbox_denial(output, exit_code):
        return (
            "\n\n[sandbox] This command ran in a restricted sandbox and the failure looks sandbox-related. "
            "If it needs network or access outside the workspace, retry the SAME command with "
            'sandbox_permissions="require_escalated" and a one-line justification; the user will be asked '
            "to approve. Do not escalate for ordinary command errors."
        )
    return ""

_DANGEROUS_ENV_EXACT = frozenset(
    {
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "BASH_ENV",
        "ENV",
        "IFS",
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "NODE_OPTIONS",
        "NODE_PATH",
        "GIT_SSH_COMMAND",
        "GIT_SSH",
        "SSH_ASKPASS",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "PROMPT_COMMAND",
        "PS4",
        "RUBYOPT",
        "PERL5OPT",
    }
)

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")

_WINDOWS_EXPLICIT_SHELL_RE = re.compile(
    r"^\s*(?:"
    r"powershell(?:\.exe)?|pwsh(?:\.exe)?"
    r"|cmd(?:\.exe)?\s*/[ck]"
    r"|bash(?:\.exe)?(?:\s|$)|sh(?:\.exe)?(?:\s|$)|wsl(?:\.exe)?(?:\s|$)"
    r")",
    re.IGNORECASE,
)


def _is_dangerous_env_name(name: str) -> bool:
    upper = str(name or "").upper()
    return (
        upper in _DANGEROUS_ENV_EXACT
        or upper.startswith("LD_")
        or upper.startswith("DYLD_")
        or upper.startswith("GIT_CONFIG_")
    )

def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
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
        if _is_dangerous_env_name(key):
            return {}, f"Environment variable {key} is not allowed for command overrides"
        if not isinstance(raw_value, str):
            return {}, f"Environment variable {key} must be a string"
        if "\x00" in raw_value:
            return {}, f"Environment variable {key} contains a null byte"
        if len(raw_value) > 32_768:
            return {}, f"Environment variable {key} exceeds 32768 characters"
        resolved[key] = raw_value
    return resolved, ""


def _command_matches_excluded(command: str, policy: SandboxPolicy) -> bool:
    return _command_matches_patterns(command, policy.excluded_commands)


def _command_matches_patterns(command: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    from backend.permissions.checker import _split_shell_compound

    segments = _split_shell_compound(command)
    for segment in segments:
        candidate = str(segment or "").strip()
        for pattern in patterns:
            if pattern.endswith(":*"):
                prefix = pattern[:-2].strip()
                if candidate == prefix or candidate.startswith(f"{prefix} "):
                    return True
            elif any(token in pattern for token in ("*", "?", "[")):
                if fnmatchcase(candidate, pattern):
                    return True
            elif candidate == pattern or candidate.startswith(f"{pattern} "):
                return True
    return False


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
    snapshot = getattr(context, "sandbox_policy", None)
    if isinstance(snapshot, SandboxPolicy):
        return replace(
            snapshot,
            env_overrides={**snapshot.env_overrides, **dict(env_overrides or {})},
            timeout=timeout,
        )
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
    from backend.runtime_env import powershell_script

    script = powershell_script(command, cwd=str(Path(cwd).expanduser().resolve()) if cwd else None)
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
            # Without this, a redirected stderr carries CLIXML-serialized error
            # records (``#< CLIXML <Objs ...``) instead of the message text.
            "-OutputFormat",
            "Text",
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


def command_login_allowed(workspace: Path | None) -> bool:
    from backend.config import load_config_layer_stack

    stack = load_config_layer_stack(cwd=workspace)
    return stack.requirements.value_for("allow_login_shell", stack.effective_config().get("allow_login_shell", True))


def stdin_approval_enabled(context: Any) -> bool:
    from backend.config import load_config_layer_stack
    from backend.feature_flags import load_feature_flags

    stack = load_config_layer_stack(cwd=context.workspace_root)
    flags = load_feature_flags(stack.effective_config(), managed_requirements=stack.requirements.feature_requirements)
    return flags.enabled("write_stdin_approval", default=True)


def stdin_wait_ms(value: Any, *, chars: str, context: Any) -> int:
    if value is None:
        wait_ms = 250
    else:
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**64 - 1:
            raise ValueError("yield_time_ms must be a non-negative integer")
        wait_ms = value
    if chars:
        return min(max(250, wait_ms), 30_000)
    from backend.config import load_config_layer_stack

    stack = load_config_layer_stack(cwd=context.workspace_root)
    maximum = max(5000, stack.effective_config().get("background_terminal_max_timeout", 300_000))
    return min(max(5000, wait_ms), maximum)


def current_terminal_policy(context: Any) -> SandboxPolicy:
    if context.turn_environment is not None:
        from backend.permissions.environment_command import EnvironmentCommandPolicy
        from backend.permissions.grants import current_granted_permissions

        scope = context.permission.environment_scope.with_additional_permissions(current_granted_permissions(context))
        return EnvironmentCommandPolicy(scope, context.turn_environment,
            context.turn_environment.config.shell_environment_policy, {}, None, context.permission.allow_unsandboxed_commands)
    from backend.sandbox.policy import sandbox_policy_for_permission_context

    run_context = context.run_context
    provider = run_context.permission_context_provider if run_context is not None else None
    if provider is not None:
        permission = provider()
        policy = sandbox_policy_for_permission_context(context.workspace_root, permission)
    else:
        permission = context.permission
        policy = _workspace_sandbox_policy(Path(context.workspace_root), context, timeout=None)
    return policy.escalated_preserving_denied_reads() if permission.mode == "bypass" else policy


def command_shell_argv(
    command: str,
    *,
    shell: str | None = None,
    login: bool | None = None,
    allow_login_shell: bool = True,
    native_windows_sandbox: bool = False,
    wrap_powershell: bool = True,
) -> list[str]:
    """Codex shell_detect + Shell::derive_exec_args, before sandbox wrapping.

    The Source uses the model path to select a shell type, then discovers its
    executable; an unknown/unavailable type selects its ultimate cmd/sh shell.
    """
    if login is True and not allow_login_shell:
        raise ValueError("login shell is disabled by config; omit login or set it to false.")
    use_login = allow_login_shell if login is None else login
    names = {"powershell": "powershell", "pwsh": "powershell", "bash": "bash", "zsh": "zsh", "sh": "sh", "cmd": "cmd"}
    selected = names.get(Path(shell).stem) if shell is not None else None

    def discover(kind: str) -> str | None:
        if kind == "powershell":
            programs = [shutil.which("pwsh"), shutil.which("powershell")]
            if sys.platform == "win32":
                programs[1:1] = [str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "PowerShell/7/pwsh.exe")]
                programs.append(str(Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"))
            else:
                programs[1:1] = ["/usr/local/bin/pwsh"]
            return next((program for program in programs if program and Path(program).is_file()
                         and (not native_windows_sandbox or (Path(program).suffix.lower() == ".exe" and "/windowsapps/" not in program.replace("\\", "/").lower()))), None)
        paths = {"bash": ("/bin/bash", "/usr/bin/bash"), "zsh": ("/bin/zsh",), "sh": ("/bin/sh",), "cmd": ()}
        return next((program for program in (shutil.which(kind), *paths[kind]) if program and Path(program).is_file()), None)

    if shell is None:
        if sys.platform == "win32":
            choices = ["powershell"]
        else:
            user_shell = os.environ.get("SHELL", "")
            preferred = names.get(Path(user_shell).stem) if user_shell else None
            choices = [preferred, *( ["zsh", "bash"] if sys.platform == "darwin" else ["bash", "zsh"])]
        chosen = next(((kind, program) for kind in choices if kind and (program := discover(kind))), None)
    else:
        program = discover(selected) if selected is not None else None
        chosen = (selected, program) if program is not None else None
    if chosen is None:
        chosen = ("cmd", "cmd.exe") if sys.platform == "win32" else ("sh", "/bin/sh")
    kind, program = chosen
    if kind == "powershell":
        from backend.runtime_env import powershell_script

        # Retain MiniCode's existing UTF-8/native-exit adapter; the selected
        # shell and startup-profile flags still follow Source derive_exec_args.
        return [program, *([] if use_login else ["-NoProfile"]), "-Command", powershell_script(command) if wrap_powershell else command]
    if kind == "cmd":
        return [program, "/c", command]
    return [program, "-lc" if use_login else "-c", command]


def _model_shell_description(*, windows: bool | None = None) -> str:
    if (windows if windows is not None else sys.platform == "win32"):
        host_shell = ("PowerShell" if windows is not None else
            "PowerShell 7" if shutil.which("pwsh.exe") else "Windows PowerShell 5.1")
        return (
            f"Execute a shell command. The default Windows shell is {host_shell}; use shell to select a shell type. "
            "The sandbox applies to the selected shell. Use workspace-relative paths and cross-platform tools. "
            "Use the cwd and env arguments instead of shell cd/env setup, and do not assume && is available. "
            "For the default PowerShell, do not use POSIX-only head/tail/grep/sed/cat commands or inline "
            "NAME=value command assignments; use Get-Content/Select-String, dedicated workspace tools, "
            "and the structured env field. If a POSIX command is not recognized, retry with its PowerShell "
            "equivalent instead of repeating it. "
            # Codex shell_spec windows_shell_guidance: destructive-operation
            # rules belong in the tool description the model reads, not only
            # in user-facing prompt context.
            "Windows safety rules: do not compose destructive filesystem commands across shells; "
            "enumerate and delete/move within one shell end-to-end, preferring native cmdlets "
            "(Remove-Item/Move-Item) with -LiteralPath over string-built commands. Before any "
            "recursive delete or move, verify the resolved absolute target paths stay inside the "
            "workspace or the explicitly named target directory; never recurse against an "
            "unverified computed path. Launch background helpers with "
            "Start-Process -WindowStyle Hidden unless the user explicitly asked for a visible window."
        )
    return (
        "Execute a shell command for builds, tests, installs, git, processes, and scripts "
        "within the workspace sandbox."
    )


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
        r"\bgit\s+(?:reset\s+--hard|branch\s+-D)\b",
        # `git clean -fdx` bundles its flags, so a trailing \b after the `f`
        # could never match. Look ahead for any force flag in the segment.
        r"\bgit\s+clean\b(?=[^\n;&|]*(?:\s-[a-z]*f|\s--force))",
        r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)\b",
        r"\bgit\s+(?:checkout|restore)\s+(?:--\s+)?\.\s*$",
        r"\bgit\s+stash\s+(?:drop|clear)\b",
        r"\bgit\s+commit\b[^\n;&|]*--amend\b",
        r"\b(?:kubectl\s+delete|terraform\s+destroy)\b",
        r"\b(?:drop|truncate)\s+(?:table|database|schema)\b",
        # PowerShell's Format-* cmdlets are read-only output formatting. Only
        # classify an independent `format` command as destructive.
        r"\bmkfs(?:\.[a-z0-9]+)?\b",
        r"(?:^|[;&|]\s*)format(?:\s|$)",
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
    """Classify shell execution conservatively.

    Literal argv extraction can prove a command is destructive or external;
    it cannot prove one is read-only, because expanded words are dropped. So a
    command string never earns the read-only scheduling or permission fast
    path. Dedicated read/search/list tools remain available for that work.
    """
    payload = args or {}
    command = str(payload.get("command") or "").strip()
    if not command:
        return TOOL_SIDE_EFFECT_EXTERNAL

    from backend.permissions import argv_rules
    from backend.permissions.checker import (
        check_catastrophic_command,
        literal_command_parses,
        protected_write_command_reason,
    )
    from backend.permissions.powershell_ast import command_readings

    readings = command_readings(command)
    allowed, _reason = check_catastrophic_command(command)
    # A shell write to a protected path (.minicode/**, .git/**, settings.json, …)
    # is classified destructive so it always requires confirmation, never a
    # silent workspace-write. The dedicated file tools already block these
    # paths; this keeps the shell path from being an unconfirmed bypass.
    if (
        not allowed
        or protected_write_command_reason(command)
        or any(pattern.search(reading) for reading in readings for pattern in _DESTRUCTIVE_COMMAND_PATTERNS)
    ):
        return TOOL_SIDE_EFFECT_DESTRUCTIVE
    # The string patterns above only see top-level text. Resolve the literal
    # argv of every command the script runs so a delete inside a brace group,
    # loop body, pipeline tail, or quoted spelling is classified the same way.
    literal_commands = [
        argv for parsed in literal_command_parses(command) for argv in parsed.commands
    ]
    if any(argv_rules.destructive_reason(argv) for argv in literal_commands):
        return TOOL_SIDE_EFFECT_DESTRUCTIVE
    if _as_bool(payload.get("with_escalated_permissions", False)):
        return TOOL_SIDE_EFFECT_EXTERNAL
    if any(pattern.search(reading) for reading in readings for pattern in _EXTERNAL_COMMAND_PATTERNS):
        return TOOL_SIDE_EFFECT_EXTERNAL
    if any(argv_rules.is_external(argv) for argv in literal_commands):
        return TOOL_SIDE_EFFECT_EXTERNAL
    # Do not infer safety from shell text. This keeps commands out of plan
    # mode, speculative execution, concurrent read batches, and result cache.
    return TOOL_SIDE_EFFECT_WORKSPACE


def _looks_like_sandbox_denial(stderr: str, exit_code: int) -> bool:
    """Heuristic: did the sandbox (not the command's own logic) cause this failure?

    Only accept signatures tied to an OS/network sandbox. Generic application
    errors such as EACCES, connection refused, or "operation not permitted"
    must not turn the bypass gate into routine error recovery.
    """
    if exit_code == 0:
        return False
    text = (stderr or "").lower()
    if not text:
        return False
    return any(sig in text for sig in _SANDBOX_DENIAL_PATTERNS)


_WINDOWS_POSIX_COMMANDS = {
    "head",
    "tail",
    "grep",
    "sed",
    "awk",
    "cat",
    "export",
}
_WINDOWS_INLINE_ENV_RE = re.compile(
    r"(?:^|[;&|]\s*)([A-Za-z_][A-Za-z0-9_]*)=(?!=)"
)


def _windows_command_portability_hint(
    command: str,
    stderr: str,
    exit_code: int | None,
) -> str:
    """Explain the safe, model-actionable fix for POSIX-on-Windows errors."""

    if sys.platform != "win32" or exit_code in (None, 0):
        return ""
    error_text = str(stderr or "").lower()
    if not error_text:
        return ""

    inline_env = bool(_WINDOWS_INLINE_ENV_RE.search(str(command or "")))
    command_names = {
        match.group(1).lower()
        for match in re.finditer(
            r"(?<![A-Za-z0-9_.-])([A-Za-z][A-Za-z0-9_.-]*)(?=\s|$)",
            str(command or ""),
        )
    }
    posix_names = sorted(command_names & _WINDOWS_POSIX_COMMANDS)
    not_recognized = (
        "not recognized" in error_text
        or "commandnotfoundexception" in error_text
        or "is not recognized as the name" in error_text
    )
    if not not_recognized and not inline_env:
        return ""

    if inline_env:
        return (
            "[windows-portability] This command used a POSIX inline environment assignment. "
            "Use cwd and `python -m ...` for Python module resolution. For ordinary variables, "
            "use the structured env object (for example env={\"TASK_MODE\":\"audit\"}); "
            "do not repeat `NAME=value command`."
        )
    if posix_names:
        equivalents = {
            "head": "Get-Content -Head N",
            "tail": "Get-Content -Tail N",
            "grep": "Select-String or grep_files",
            "sed": "apply_patch or a Python edit script",
            "awk": "a Python script",
            "cat": "Get-Content -Raw",
            "export": "run_command's structured env object",
        }
        rendered = ", ".join(
            f"{name} -> {equivalents[name]}" for name in posix_names
        )
        return (
            "[windows-portability] This command used POSIX utilities that are unavailable in the "
            f"Windows PowerShell shell ({rendered}). Retry once with the PowerShell/tool equivalent; "
            "do not repeat the failed POSIX command."
        )
    return (
        "[windows-portability] The command was not recognized by the Windows PowerShell shell. "
        "Check the executable name and retry with a PowerShell-compatible command; use cwd/env "
        "fields instead of shell setup."
    )

