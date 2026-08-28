"""Command tool helpers shared by RunCommandTool and its control plane.

Extracted from ``backend/tools/command_tool.py`` so shell policy, sandbox
policy derivation and Windows portability helpers are independent of the tool
class that executes them.
"""

from __future__ import annotations

import logging

from backend.sandbox import (
    SandboxPolicy,
    SandboxRunner,
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
import re
import shutil
import subprocess
import sys


logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT: float = 120.0

MAX_TIMEOUT_MS = 600_000

MAX_TIMEOUT_SECONDS = MAX_TIMEOUT_MS / 1000

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


def _is_bypass_mode(context: Any = None) -> bool:
    policy = getattr(context, "sandbox_policy", None)
    if isinstance(policy, SandboxPolicy):
        return policy.disable_os_sandbox
    permission = getattr(context, "permission", None)
    return getattr(permission, "mode", None) == "bypass"


def _command_matches_excluded(command: str, policy: SandboxPolicy) -> bool:
    return _command_matches_patterns(command, policy.excluded_commands)


def _command_matches_patterns(command: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return False
    try:
        from backend.permissions.checker import _split_shell_compound

        segments = _split_shell_compound(command)
    except Exception:
        segments = [command]
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
    location = ""
    if cwd:
        escaped_cwd = str(Path(cwd).expanduser().resolve()).replace("'", "''")
        location = f"Set-Location -LiteralPath '{escaped_cwd}'; "
    prelude = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        # PowerShell 7 colours error records with ANSI escapes; the model reads
        # the raw bytes, so render plain text. $PSStyle is absent on 5.1.
        "if ($null -ne $PSStyle) { $PSStyle.OutputRendering = 'PlainText' }; "
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


def _model_shell_description() -> str:
    if sys.platform == "win32":
        host_shell = "PowerShell 7" if shutil.which("pwsh.exe") else "Windows PowerShell 5.1"
        return (
            "Execute a shell command. On Windows, the command language is PowerShell in both normal "
            "workspace-sandbox and bypass modes; the sandbox changes permissions/network, not the "
            "shell language. Use workspace-relative paths and cross-platform tools. "
            f"In bypass or approved escalated mode it runs in host {host_shell}. "
            "Use the cwd and env arguments instead of shell cd/env setup, and do not assume && is available. "
            "On Windows, do not use POSIX-only head/tail/grep/sed/cat commands or inline "
            "NAME=value command assignments; use Get-Content/Select-String, dedicated workspace tools, "
            "and the structured env field. If a POSIX command is not recognized, retry with its PowerShell "
            "equivalent instead of repeating it."
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
        r"\bgit\s+(?:reset\s+--hard|branch\s+-D)\b",
        # `git clean -fdx` bundles its flags, so a trailing \b after the `f`
        # could never match. Look ahead for any force flag in the segment.
        r"\bgit\s+clean\b(?=[^\n;&|]*(?:\s-[a-z]*f|\s--force))",
        r"\bgit\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)\b",
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
    # A shell write to a protected path (.minicode/**, .git/**, settings.json, …)
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
            "Retry with run_command's structured env object (for example env={\"PYTHONPATH\":\"..\"}) "
            "and leave the command as `python ...`; do not repeat `NAME=value command`."
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


