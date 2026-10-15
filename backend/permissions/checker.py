"""
权限检查器（DESIGN.md §8.3）。

四级权限模型：
  - AUTO:        自动执行（read_file 等只读工具）
  - CONFIRM:     展示参数，用户确认（run_command 等）
  - DIFF_REVIEW: 展示 diff，用户审批（write_file / edit_file）
  - ALWAYS_DENY: 永远拒绝

支持路径白名单/黑名单和通配符匹配。
"""

from __future__ import annotations

import fnmatch
import inspect
import logging
import re
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from functools import lru_cache
from pathspec.gitignore import GitIgnoreSpec

from backend.config import PermissionSettings
from backend.permissions.context import PermissionContext, PermissionDecision
from backend.permissions.approval_policy import ApprovalFlow, ApprovalPolicy, policy_for_mode, rejected_approval_reason
from backend.permissions.network import assess_network_url
from backend.permissions.rules import PermissionRuleMatcher
from backend.security.sensitive_files import (
    DANGEROUS_FILES,
    DANGEROUS_DIRECTORIES,
)
from backend.tools.base import PermissionLevel, WORKSPACE_PATH_SCHEMA_FIELDS
from backend.tools.path_resolution import windows_path_safety_reason

if TYPE_CHECKING:
    from backend.tools.base import BaseTool


_ACCEPTS_TOOL_CACHE: WeakKeyDictionary[Any, dict[str, tuple[Any, bool]]] = WeakKeyDictionary()

logger = logging.getLogger(__name__)
_WORKSPACE_ROOT_UNSET = object()

# Windows and macOS resolve paths case-insensitively, so a denylist entry must
# match regardless of the casing the model happens to use.
_FILESYSTEM_IS_CASE_INSENSITIVE = sys.platform in {"win32", "darwin"}
@lru_cache(maxsize=128)
def _compiled_path_deny_spec(patterns: tuple[str, ...]) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(patterns)


def _tool_pattern_matches(tool_name: str, pattern: str, policy_aliases: tuple[str, ...] = ()) -> bool:
    """Match ordinary globs plus an MCP server-level rule.

    `mcp__server` is a deliberate server boundary, not a literal tool named
    server: it covers `mcp__server__*` without making similarly named servers
    match by raw prefix.
    """
    from backend.permissions.patterns import tool_policy_pattern_matches

    return any(tool_policy_pattern_matches(name, pattern) for name in (tool_name, *policy_aliases))


def _callable_accepts_tool_parameter(owner: Any, method_name: str, callable_obj: Any) -> bool:
    cache_token = getattr(callable_obj, "__func__", callable_obj)
    try:
        cached_methods = _ACCEPTS_TOOL_CACHE.get(owner)
    except TypeError:
        cached_methods = None
    if cached_methods is not None:
        cached = cached_methods.get(method_name)
        if cached is not None and cached[0] is cache_token:
            return cached[1]

    try:
        accepts_tool = "tool" in inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        accepts_tool = True

    try:
        if cached_methods is None:
            cached_methods = {}
            _ACCEPTS_TOOL_CACHE[owner] = cached_methods
        cached_methods[method_name] = (cache_token, accepts_tool)
    except TypeError:
        pass

    return accepts_tool



# ── Catastrophic command blocklist ──────────────────────────────────────────

_CATASTROPHIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?/+\s*$", re.I), "recursive delete of root filesystem"),
    (re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?/\*", re.I), "recursive delete of root filesystem"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/\s*$", re.I), "recursive delete of root filesystem"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+(?:~|\$HOME|\$\{HOME\}|%USERPROFILE%)(?:[\\/][^/\s]*)?\s*$", re.I), "recursive delete of home directory"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/+(?:Users|home)(?:/[^/\s]+)?/?\s*$", re.I), "recursive delete of system directory"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/+(?:etc|usr|var|bin|sbin|System|Library)(?:/|\s*$)", re.I), "recursive delete of system directory"),
    (re.compile(r"\bmkfs\b", re.I), "filesystem format"),
    (re.compile(r"\bdd\b.*\bof\s*=\s*/(?:dev|etc|boot|proc|sys|System|Library|Users|home)(?:/|\s*$)", re.I), "raw system-file write"),
    (re.compile(r"\bfind\b[^\n;&|]*(?:^|\s)-delete(?:\s|$)", re.I), "find delete operation"),
    # `-exec` may hand the delete to a shell wrapper (`-exec sh -c "rm -rf /"`),
    # so scan the whole -exec argument list within this shell segment instead of
    # requiring the delete command to be the first token after -exec.
    (re.compile(r"\bfind\b[^\n;&|]*-exec(?:dir)?\s[^\n;&|]*\b(?:rm|del|erase|rmdir|rd|unlink|shred)\b", re.I), "find recursive delete operation"),
    (re.compile(r":\(\)\s*\{.*\|.*&", re.I), "fork bomb"),
    (re.compile(r">\s*/dev/sd", re.I), "raw disk overwrite"),
    (re.compile(r"curl\b.*\|\s*(ba)?sh", re.I), "pipe remote script to shell"),
    (re.compile(r"wget\b.*\|\s*(ba)?sh", re.I), "pipe remote script to shell"),
    (re.compile(r"Remove-Item\s.*-Recurse.*[A-Z]:\\\s*$", re.I), "recursive delete of drive root"),
    (re.compile(r"Remove-Item\s.*[A-Z]:\\\s*.*-Recurse", re.I), "recursive delete of drive root"),
    (re.compile(r"\bRemove-Item\b(?=[^\n]*-Recurse)(?=[^\n]*(?:\$HOME|\$\{HOME\}|%USERPROFILE%|~))", re.I), "recursive delete of home directory"),
    (re.compile(r"\bRemove-Item\b(?=[^\n]*-Recurse)(?=[^\n]*[A-Z]:\\Users\\[^\\\s]+\\?\s*$)", re.I), "recursive delete of system directory"),
    (re.compile(r"\bformat\s+[A-Z]:", re.I), "drive format"),
    (re.compile(r"del\s+/[sS].*\\Windows", re.I), "delete Windows system directory"),
    (
        re.compile(r"\bdel\s+/[sS](?:\s+/[qQ])?\s+[A-Z]:\\\s*$", re.I),
        "recursive delete of drive root",
    ),
    (re.compile(r"rd\s+/[sS]\s+/[qQ]\s+[A-Z]:\\$", re.I), "recursive delete of drive root"),
    (re.compile(r"\b(?:rd|rmdir)\b\s+/[sS]\s+/[qQ]\s+[A-Z]:\\Users\\[^\\\s]+\\?\s*$", re.I), "recursive delete of system directory"),
    # Git operations that discard durable local state. These patterns are
    # anchored to one shell segment; compound commands are split below.
    (re.compile(r"^\s*git\s+reset\s+--hard\b", re.I), "destructive git reset"),
    (
        re.compile(
            r"^\s*git\s+clean\b"
            r"(?![^\n;&|]*(?:\s(?:-n|--dry-run)(?:\s|$)))"
            r"(?=[^\n;&|]*(?:\s-[A-Za-z]*f[A-Za-z]*\b|\s--force(?:\s|$)))",
            re.I,
        ),
        "destructive git clean",
    ),
    (
        re.compile(r"^\s*git\s+(?:checkout|restore)\s+(?:--\s+)?\.\s*$", re.I),
        "destructive git working-tree restore",
    ),
    (re.compile(r"^\s*git\s+stash\s+(?:drop|clear)\b", re.I), "destructive git stash removal"),
    (
        re.compile(
            r"^\s*git\s+branch\s+(?:-D\b|--delete\s+--force\b|--force\s+--delete\b)",
            re.I,
        ),
        "destructive git branch deletion",
    ),
    (
        re.compile(r"^\s*git\s+push\b[^\n]*(?:--force(?:-with-lease)?|-f)\b", re.I),
        "destructive forced git push",
    ),
    (re.compile(r"^\s*git\s+commit\b[^\n]*--amend\b", re.I), "destructive git amend"),
    (re.compile(r"^\s*kubectl\s+delete\b", re.I), "destructive Kubernetes delete"),
    (re.compile(r"^\s*terraform\s+destroy\b", re.I), "destructive Terraform destroy"),
    (re.compile(r"^\s*(?:drop|truncate)\s+(?:table|database|schema)\b", re.I), "destructive database operation"),
    # Background process ownership follows Codex/Pi: MiniCode can terminate the
    # exact process tree registered under a command id, but shell-wide matching
    # can kill the backend, editor, or unrelated user work. Exact PID forms such
    # as Stop-Process -Id 123 and taskkill /PID 123 remain available behind the
    # ordinary run_command approval boundary.
    (
        re.compile(r"\bstop-process\b(?=[^\n;&|]*(?:[-\u2013\u2014\u2212](?:name|n)\b))", re.I),
        "process-name termination is not scoped to an owned background command",
    ),
    (
        re.compile(r"\bget-process\b[^\n;]*\|[^\n;]*\bstop-process\b", re.I),
        "pipeline termination can kill processes outside the owned background command",
    ),
    (
        re.compile(
            r"\b(?:foreach-object|foreach|%)\b[^\n;|]*(?:[-\u2013\u2014\u2212](?:membername|m)\s+)?kill\b",
            re.I,
        ),
        "ForEach-Object method termination is not scoped to an owned background command",
    ),
    (
        re.compile(r"\bget-process\b[^\n;]*\|[^\n;]*\.kill\s*\(", re.I),
        "process pipeline Kill() is not scoped to an owned background command",
    ),
    (
        re.compile(r"\btaskkill(?:\.exe)?\b(?=[^\n;&|]*/im\b)", re.I),
        "taskkill image-name termination is not scoped to an owned background command",
    ),
    (re.compile(r"\bpkill\b", re.I), "pkill is not scoped to an owned background command"),
    (re.compile(r"\bkillall\b", re.I), "killall is not scoped to an owned background command"),
    (
        re.compile(
            r"(?:\bwin32_process\b|\bwmic\b[^\n;]*\bprocess\b|\binvoke-(?:wmi|cim)method\b)"
            r"[^\n;]*\bterminate\b",
            re.I,
        ),
        "WMI/CIM process termination is not scoped to an owned background command",
    ),
    # Environment-variable exfiltration via /proc (parser-differential defense in
    # depth; path validation may not cover a bare `cat`). No legitimate dev use.
    (re.compile(r"/proc/[^/\s]+/environ", re.I), "read of process environment (secret exfiltration)"),
    (
        re.compile(r"\bzmodload\b(?=[^\n;&|]*\bzsh/(?:system|net/socket|files|zftp)\b)", re.I),
        "load of a Zsh module that exposes raw system, network, or filesystem access",
    ),
]


# ── Parser-differential / command-injection risk signals ─────────────────────
# Superset of CC bashSecurity.ts COMMAND_SUBSTITUTION_PATTERNS: the common
# substitution shapes mirror CC, while `$'`, `$IFS`, `print -P`, and `setopt`
# are MiniCode additions (CC handles backticks in a separate unescaped-check
# pass). These are RISK SIGNALS, not outright blocks: a command carrying one
# is never silently auto-classified as read-only (so it always requires
# confirmation), but common legitimate uses like `echo $(date)` remain
# runnable after the user approves.
_INJECTION_RISK_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\$\("), "$() command substitution"),
    (re.compile(r"`"), "backtick command substitution"),
    (re.compile(r"\$\{"), "${} parameter expansion"),
    (re.compile(r"<\("), "process substitution <()"),
    (re.compile(r">\("), "process substitution >()"),
    (re.compile(r"\$'"), "ANSI-C quoting ($'...') can hide characters"),
    (re.compile(r"\$IFS"), "IFS variable can bypass argument parsing"),
    (
        re.compile(r"\bprint\b(?=[^\n;&|]*\s-[A-Za-z]*P[A-Za-z]*(?:\s|$))"),
        "Zsh prompt expansion (print -P)",
    ),
    (
        re.compile(r"\bsetopt\b(?=[^\n;&|]*\b(?:prompt_?subst|glob_?subst)\b)", re.I),
        "Zsh dynamic substitution option",
    ),
)

# A command substitution is a nested shell payload. Inspecting it with the
# same catastrophic blocklist keeps a wrapper from hiding a destructive action.
_COMMAND_SUBSTITUTION_RE = re.compile(
    r"\$\(([^()]*)\)|`([^`]*)`|<\(([^()]*)\)|>\(([^()]*)\)",
    re.DOTALL,
)


def command_injection_risk(command: str) -> str:
    """Return a reason string when a command carries a parser-differential /
    injection risk signal, else "". Used to withhold read-only auto-allow."""
    for pattern, description in _INJECTION_RISK_PATTERNS:
        if pattern.search(command or ""):
            return description
    return ""


_POSIX_SHELL_WRAPPERS = {"bash", "sh", "zsh", "dash", "ksh"}
_POWERSHELL_WRAPPERS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_CMD_WRAPPERS = {"cmd", "cmd.exe"}

_DESTRUCTIVE_COMPOUND_SEGMENTS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"^\s*rm\b(?=[^\n]*(?:^|\s)-[^\s]*[rR])", re.I),
        "recursive delete hidden inside a compound command",
    ),
    (
        re.compile(r"^\s*(?:Remove-Item|del|erase|rd|rmdir)\b(?=[^\n]*(?:-Recurse|/[sS]))", re.I),
        "recursive delete hidden inside a compound command",
    ),
    (
        re.compile(r"^\s*git\s+(?:clean\b(?=[^\n]*\s-[^\s]*[fdx])|reset\s+--hard\b)", re.I),
        "destructive git operation hidden inside a compound command",
    ),
    (
        re.compile(r"^\s*find\b[^\n]*(?:-delete|-exec(?:dir)?\s+(?:rm|del|erase|rmdir|rd)\b)", re.I),
        "destructive find operation hidden inside a compound command",
    ),
    (
        re.compile(
            r"^\s*git\s+(?:reset\s+--hard\b|clean\b|"
            r"(?:checkout|restore)\s+(?:--\s+)?\.\s*$|"
            r"stash\s+(?:drop|clear)\b|"
            r"branch\s+(?:-D\b|--delete\s+--force\b|--force\s+--delete\b)|"
            r"push\b[^\n]*(?:--force(?:-with-lease)?|-f)\b|"
            r"commit\b[^\n]*--amend\b)",
            re.I,
        ),
        "destructive git operation hidden inside a compound command",
    ),
    (
        re.compile(r"^\s*(?:kubectl\s+delete|terraform\s+destroy)\b", re.I),
        "destructive external operation hidden inside a compound command",
    ),
    (
        re.compile(r"^\s*(?:drop|truncate)\s+(?:table|database|schema)\b", re.I),
        "destructive database operation hidden inside a compound command",
    ),
)


def _split_shell_compound(command: str) -> list[str]:
    """Split top-level shell chains without interpreting quoted separators."""
    segments: list[str] = []
    current: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            current.append(char)
            escaped = False
            index += 1
            continue
        # Backslash is not an escape character in PowerShell or cmd. Treat it
        # as one only inside a quoted token, where suppressing quote parsing is
        # required. This keeps `C:\\; Remove-Item ...` from hiding a real
        # top-level separator behind the drive-root backslash.
        if char == "\\" and quote and quote != "'":
            current.append(char)
            escaped = True
            index += 1
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            current.append(char)
            index += 1
            continue
        separator_length = 0
        if not quote:
            if command.startswith(("&&", "||"), index):
                separator_length = 2
            elif char in {";", "\n"}:
                separator_length = 1
        if separator_length:
            segment = "".join(current).strip()
            if segment:
                segments.append(segment)
            current.clear()
            index += separator_length
            continue
        current.append(char)
        index += 1
    segment = "".join(current).strip()
    if segment:
        segments.append(segment)
    return segments


def _destructive_compound_reason(command: str) -> str:
    segments = _split_shell_compound(command)
    if len(segments) < 2:
        return ""
    for segment in segments:
        for pattern, reason in _DESTRUCTIVE_COMPOUND_SEGMENTS:
            if pattern.search(segment):
                return reason
    return ""


# Tokens that, followed by a path, denote a shell WRITE to that path. Used to
# stop a shell command from writing protected files (.env, *.pem, secrets/**,
# settings.json, .minicode/**, .git/**, …) that the file tools
# (write_file/edit_file/apply_patch) already block even under bypass. This
# closes the file-tool vs shell asymmetry for the protected set; the OS sandbox
# and CONFIRM remain the primary boundary.
#
# Known limitation: only writes this matcher can see are covered. Shell *reads*
# of a protected path are not pattern-matched — any program can read any file,
# so a string matcher would give false assurance. Read confinement belongs to
# the sandbox filesystem policy, which denies these paths by profile.
_SHELL_WRITE_REDIRECT_RE = re.compile(r">>?\s*([^\s;&|]+)")
_SHELL_WRITE_COMMAND_RE = re.compile(
    r"\b(?:mv|cp|tee|install|ln)\b\s+(.+)", re.IGNORECASE
)
_DD_OUTPUT_RE = re.compile(r"\bof\s*=\s*([^\s;&|]+)", re.IGNORECASE)


def _path_is_protected_write(raw_path: str) -> bool:
    cleaned = raw_path.strip().strip("'\"").replace("\\", "/").strip()
    if not cleaned:
        return False
    name = cleaned.rsplit("/", 1)[-1].lower()
    if name in DANGEROUS_FILES:
        return True
    parts = [segment for segment in cleaned.split("/") if segment]
    if any(part.lower() in DANGEROUS_DIRECTORIES for part in parts):
        return True
    # Same secret/repo floor the file tools enforce. Consulting it here instead
    # of keeping a second, shorter list is what makes the two paths agree: the
    # short list omitted .env, *.pem, secrets/** and settings.json, so a shell
    # redirect could overwrite a file write_file refuses even under bypass.
    return _matches_secret_repo_floor(cleaned)


def _protected_write_reason(command: str) -> str:
    for segment in _split_shell_compound(command):
        dd_match = _DD_OUTPUT_RE.search(segment)
        if dd_match and _path_is_protected_write(dd_match.group(1)):
            return "shell write to a protected path (dd output)"
        for match in _SHELL_WRITE_REDIRECT_RE.finditer(segment):
            if _path_is_protected_write(match.group(1)):
                return "shell write to a protected path (redirection)"
        command_match = _SHELL_WRITE_COMMAND_RE.search(segment)
        if command_match:
            for token in command_match.group(1).split():
                if token.startswith("-"):
                    continue
                if _path_is_protected_write(token):
                    return "shell write to a protected path (file command)"
    return ""


def _shell_wrapper_payload(command: str) -> tuple[str | None, str]:
    """Return a shell wrapper's command payload, or an unsafe parse reason."""
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        lowered = command.lower()
        if any(name in lowered for name in (*_POSIX_SHELL_WRAPPERS, *_POWERSHELL_WRAPPERS, *_CMD_WRAPPERS)):
            return None, f"malformed shell wrapper: {exc}"
        return None, ""
    if not argv:
        return None, ""
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
    option_index = -1
    if executable in _POSIX_SHELL_WRAPPERS:
        from backend.permissions.shell_ast import posix_shell_payload_index

        payload_index = posix_shell_payload_index(argv)
        if payload_index is None:
            return None, ""
        if payload_index >= len(argv):
            return None, "shell wrapper is missing its command payload"
        return argv[payload_index], ""
    elif executable in _POWERSHELL_WRAPPERS:
        option_index = next(
            (
                index
                for index, value in enumerate(argv[1:], start=1)
                if value.lower() in {"-command", "-c"}
            ),
            -1,
        )
    elif executable in _CMD_WRAPPERS:
        option_index = next(
            (index for index, value in enumerate(argv[1:], start=1) if value.lower() in {"/c", "/k"}),
            -1,
        )
    if option_index < 0:
        return None, ""
    if option_index + 1 >= len(argv):
        return None, "shell wrapper is missing its command payload"
    return " ".join(argv[option_index + 1 :]), ""


def _normalize_for_catastrophic_match(command: str) -> str:
    """Normalize a command for the string-level catastrophic patterns.

    Drops shell quotes (replacing them with spaces so ``rm -rf "/"`` collapses
    to ``rm -rf /``). This is the floor used when a script cannot be parsed;
    parseable scripts are also judged on their resolved argv.
    """
    no_quotes = command.replace('"', " ").replace("'", " ")
    return re.sub(r"\s+", " ", no_quotes).strip()


def _check_catastrophic_command(command: str, *, _depth: int = 0) -> tuple[bool, str]:
    stripped = command.strip()
    # Match against both the raw form and the quote/variable-normalized form so
    # bypasses like ``rm -rf "/"`` or ``rm -rf $HOME/`` are caught.
    candidates = (stripped, _normalize_for_catastrophic_match(stripped))
    for candidate in candidates:
        for pattern, description in _CATASTROPHIC_PATTERNS:
            if pattern.search(candidate):
                return False, f"命令被安全策略拦截: {description}"
    compound_reason = _destructive_compound_reason(stripped)
    if compound_reason:
        return False, f"命令被安全策略拦截: {compound_reason}"
    literal_reason = _literal_catastrophic_reason(stripped)
    if literal_reason:
        return False, f"命令被安全策略拦截: {literal_reason}"
    if _depth >= 4:
        return False, "命令被安全策略拦截: shell wrapper nesting is too deep"
    for match in _COMMAND_SUBSTITUTION_RE.finditer(stripped):
        nested = next(
            (group for group in match.groups() if group is not None),
            "",
        ).strip()
        if not nested:
            continue
        nested_allowed, nested_reason = _check_catastrophic_command(
            nested,
            _depth=_depth + 1,
        )
        if not nested_allowed:
            return nested_allowed, nested_reason
    payload, parse_error = _shell_wrapper_payload(stripped)
    if parse_error:
        return False, f"命令被安全策略拦截: {parse_error}"
    if payload is not None:
        return _check_catastrophic_command(payload, _depth=_depth + 1)
    return True, ""


def _literal_catastrophic_reason(command: str) -> str:
    """Judge the commands a script really runs, not its surface text.

    Brace groups, loop bodies, pipeline tails, quoted spellings and wrapper
    payloads all resolve to plain argv here, so a rule sees ``rm -rf /``
    whether it was written directly or hidden behind shell composition.
    Unparseable scripts return "" and stay on the string-level floor above.
    """
    from backend.permissions import argv_rules

    for parsed in literal_command_parses(command):
        for argv in parsed.commands:
            reason = argv_rules.catastrophic_reason(argv)
            if reason:
                return reason
        if parsed.compound:
            for argv in parsed.commands:
                reason = argv_rules.compound_destructive_reason(argv)
                if reason:
                    return reason
    return ""


def literal_command_parses(command: str) -> list[Any]:
    """Parse *command* with every grammar the host may run it under.

    On Windows the command tool hands the script to PowerShell, but models
    frequently write POSIX-style scripts that PowerShell aliases accept
    (``rm -rf``), so both readings are judged. A grammar that cannot parse
    the script contributes nothing; the string-level rules stay the floor.
    """
    from backend.permissions import powershell_ast, shell_ast

    parses = []
    posix = shell_ast.parse_literal_commands(command)
    if posix is not None:
        parses.append(posix)
    if sys.platform == "win32" or _looks_like_powershell(command):
        windows = powershell_ast.parse_literal_commands(powershell_ast.powershell_equivalent(command))
        if windows is not None:
            parses.append(windows)
    return parses


_POWERSHELL_HINT = re.compile(
    r"(?:^|[\s;|&(])(?:[A-Z][a-z]+-[A-Z][A-Za-z]+|(?i:pwsh|powershell|remove-item|ri|rd|rmdir|del|erase|spps|iex))\b|\$env:|\$_|-Recurse\b|[A-Za-z]:[\\/]",
)


def _looks_like_powershell(command: str) -> bool:
    return _POWERSHELL_HINT.search(command) is not None


def check_catastrophic_command(command: str) -> tuple[bool, str]:
    """Public wrapper for the static catastrophic shell-command blocklist."""
    from backend.permissions.powershell_ast import command_readings

    for reading in command_readings(command):
        allowed, reason = _check_catastrophic_command(reading)
        if not allowed:
            return allowed, reason
    return True, ""


# PowerShell cmdlets (and their aliases) that create, overwrite, move or delete
# the item named by their path arguments.
_POWERSHELL_WRITE_COMMANDS = frozenset({
    "set-content", "add-content", "clear-content", "out-file", "tee-object",
    "copy-item", "move-item", "new-item", "rename-item", "set-item", "remove-item",
    "sc", "ac", "clc", "tee", "cp", "cpi", "copy", "mv", "mi", "move", "ni",
    "ren", "rni", "si", "rm", "ri", "del", "erase", "rd", "rmdir",
})
# Parameters whose value is data, not a filesystem target.
_POWERSHELL_NON_PATH_PARAMETERS = frozenset({
    "-value", "-inputobject", "-encoding", "-itemtype", "-stream", "-filter",
    "-include", "-exclude", "-credential", "-delimiter", "-width",
})


def _powershell_protected_write_reason(command: str) -> str:
    from backend.permissions import powershell_ast
    from backend.permissions.shell_ast import DYNAMIC_WORD

    if sys.platform != "win32" and not _looks_like_powershell(command):
        return ""
    parsed = powershell_ast.parse_literal_commands(powershell_ast.powershell_equivalent(command))
    if parsed is None:
        return ""
    for argv in parsed.commands:
        if not argv or argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower() not in _POWERSHELL_WRITE_COMMANDS:
            continue
        skip_value = False
        for token in argv[1:]:
            if skip_value:
                skip_value = False
                continue
            if token == DYNAMIC_WORD:
                continue
            name, separator, bound = token.partition(":") if token.startswith("-") else ("", "", token)
            if name.lower() in _POWERSHELL_NON_PATH_PARAMETERS:
                skip_value = not separator
                continue
            # ``-Path:.env`` binds the value in the same token.
            if bound and _path_is_protected_write(bound):
                return "shell write to a protected path (PowerShell cmdlet)"
    return ""


def protected_write_command_reason(command: str, *, _depth: int = 0) -> str:
    """Return a reason when a shell command writes to a protected path.

    Unwraps shell wrappers (bash -c "…") like the catastrophic check so a write
    hidden inside a wrapper is still seen. Empty string means no protected-path
    write was detected.
    """
    stripped = command.strip()
    from backend.permissions.powershell_ast import command_readings

    for reading in command_readings(stripped):
        reason = _protected_write_reason(reading)
        if reason:
            return reason
    reason = _powershell_protected_write_reason(stripped)
    if reason:
        return reason
    if _depth >= 4:
        return ""
    payload, parse_error = _shell_wrapper_payload(stripped)
    if parse_error or payload is None:
        return ""
    return protected_write_command_reason(payload, _depth=_depth + 1)


def check_permission_level(
    checker: Any,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    context: Any = None,
    tool: Any = None,
) -> "PermissionLevel":
    """Call ``checker.check`` forwarding ``tool`` only when it is accepted.

    Permission checkers are sometimes duck-typed (test doubles, external
    integrations) with the older ``check(tool_name, args, context)`` signature.
    Probe for the ``tool`` parameter so production call sites can pass the tool
    instance without breaking those implementations.
    """
    accepts_tool = _callable_accepts_tool_parameter(checker, "check", checker.check)
    if accepts_tool:
        return checker.check(tool_name, args, context=context, tool=tool)
    return checker.check(tool_name, args, context=context)


def check_denial_reason(
    checker: Any,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    context: Any = None,
    tool: Any = None,
) -> str | None:
    """``get_denial_reason`` counterpart of :func:`check_permission_level`."""
    get_reason = getattr(checker, "get_denial_reason", None)
    if get_reason is None:
        return None
    accepts_tool = _callable_accepts_tool_parameter(checker, "get_denial_reason", get_reason)
    if accepts_tool:
        return get_reason(tool_name, args, context=context, tool=tool)
    return get_reason(tool_name, args, context=context)


def evaluate_permission_decision(
    checker: Any,
    tool_name: str,
    args: dict[str, Any] | None = None,
    *,
    context: PermissionContext | None = None,
    tool: Any = None,
) -> PermissionDecision:
    from backend.agent.final_tool_request import canonical_tool_request_digest

    request_digest = canonical_tool_request_digest(tool_name, args or {})
    evaluate = getattr(checker, "evaluate", None)
    if callable(evaluate):
        decision = evaluate(tool_name, args, context=context, tool=tool)
        if getattr(decision, "request_digest", "") == request_digest:
            return decision
        return replace(decision, request_digest=request_digest)
    level = check_permission_level(checker, tool_name, args, context=context, tool=tool)
    denial = check_denial_reason(checker, tool_name, args, context=context, tool=tool) or ""
    decision = "deny" if denial or level == PermissionLevel.ALWAYS_DENY else "allow" if level == PermissionLevel.AUTO else "ask"
    return PermissionDecision(
        permission_level=level,
        decision=decision,
        capability_allowed=True,
        capability_reason="External checker did not report a separate capability boundary.",
        approval_policy={
            PermissionLevel.AUTO: "auto",
            PermissionLevel.CONFIRM: "confirm",
            PermissionLevel.DIFF_REVIEW: "diff_review",
            PermissionLevel.ALWAYS_DENY: "deny",
        }[level],
        matched_rule_source="external_checker",
        matched_rule=denial or "legacy",
        risk="medium" if level in {PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW} else "low",
        scope={"workspace_scope": context.workspace_scope if context is not None else "project", "boundary": "general"},
        expiry="call",
        request_digest=request_digest,
    )


PERMISSION_MODES: frozenset[str] = frozenset(
    {"plan", "confirm", "auto", "bypass"}
)


def normalize_permission_mode_token(mode: str | None) -> str:
    """Resolve one permission-mode token, or reject it.

    An unrecognized token is a contract error, not a request for the default
    mode: silently downgrading it would replace the user's explicit permission
    mode with a different one.
    """
    normalized = str(mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not normalized:
        raise ValueError("Permission mode is required")
    if normalized not in PERMISSION_MODES:
        raise ValueError(f"Unsupported permission mode: {mode!r}")
    return normalized


_PERMISSION_MODE_AUTHORITY: dict[str, int] = {
    # Delegation may narrow authority but cannot widen it.
    "plan": 0,
    "confirm": 1,
    "auto": 2,
    "bypass": 3,
}


def permission_mode_authority(mode: str | None) -> int:
    """Rank one permission mode by the authority it grants without asking."""
    return _PERMISSION_MODE_AUTHORITY[normalize_permission_mode_token(mode)]


def clamp_permission_mode(requested: str | None, ceiling: str | None) -> str:
    """Return ``requested`` only when it is no wider than ``ceiling``.

    Delegation may narrow authority and must never widen it. Without this a
    child could request a mode its parent does not hold — e.g. a teammate
    spawned from a read-only Plan-mode turn asking for ``bypass`` and getting
    an unsandboxed, never-prompting context.
    """
    ceiling_mode = normalize_permission_mode_token(ceiling)
    if requested is None or not str(requested).strip():
        return ceiling_mode
    requested_mode = normalize_permission_mode_token(requested)
    if permission_mode_authority(requested_mode) > permission_mode_authority(ceiling_mode):
        return ceiling_mode
    return requested_mode


_PERMISSION_RESTRICTIVENESS: dict[PermissionLevel, int] = {
    PermissionLevel.AUTO: 0,
    PermissionLevel.CONFIRM: 1,
    PermissionLevel.DIFF_REVIEW: 2,
    PermissionLevel.ALWAYS_DENY: 3,
}

def _has_undeclared_path_argument(tool: "BaseTool", args: dict[str, Any] | None) -> bool:
    """Detect a supplied filesystem path that lacks a tool-owned extractor."""
    payload = args or {}
    if not payload:
        return False
    try:
        schema = tool.get_schema().parameters
    except Exception:
        return False
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return False
    try:
        extracted = {
            str(value).strip()
            for value in tool.get_workspace_paths(payload)
            if str(value or "").strip() not in {"", "."}
        }
    except Exception:
        extracted = set()
    for field in WORKSPACE_PATH_SCHEMA_FIELDS.intersection(properties):
        value = payload.get(field)
        supplied = (
            [value]
            if isinstance(value, str)
            else [item for item in value if isinstance(item, str)]
            if isinstance(value, (list, tuple))
            else []
        )
        if any(item.strip() not in {"", "."} and item.strip() not in extracted for item in supplied):
            return True
    return False

_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)
_URL_LIKE_ARG_KEYS = frozenset(
    {
        "url",
        "uri",
        "href",
        "link",
        "endpoint",
        "origin",
        "baseurl",
        "targeturl",
        "sourceurl",
        "requesturl",
        "navigationurl",
    }
)
_HOST_LIKE_ARG_KEYS = frozenset({"host", "hostname", "domain", "server", "address"})
_MAX_NETWORK_TARGETS_PER_CALL = 32
def _network_target_requires_confirmation(
    tool_name: str,
    args: dict[str, Any] | None,
    context: PermissionContext | None,
    tool: "BaseTool | None" = None,
) -> bool:
    if context is not None and context.mode == "bypass":
        return False
    if tool is not None:
        if not bool(getattr(tool, "open_world", False)):
            return False
    elif not tool_name.startswith("mcp__"):
        return False
    for target in _extract_network_targets(args):
        if not assess_network_url(target, resolve_dns=False).allowed:
            return True
    return False


def _extract_network_targets(args: dict[str, Any] | None) -> list[str]:
    if not isinstance(args, dict):
        return []

    targets: list[str] = []
    seen: set[str] = set()

    def visit(value: Any, key: str = "", depth: int = 0) -> None:
        if depth > 6 or len(targets) >= _MAX_NETWORK_TARGETS_PER_CALL:
            return
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key), depth + 1)
            return
        if isinstance(value, (list, tuple)):
            for item in value:
                visit(item, key, depth + 1)
            return
        if not isinstance(value, str):
            return

        target = _normalize_network_arg_candidate(key, value)
        if target and target not in seen:
            seen.add(target)
            targets.append(target)

    visit(args)
    return targets


def _normalize_network_arg_candidate(key: str, value: str) -> str | None:
    raw = value.strip().strip("<>")
    if not raw:
        return None

    normalized_key = re.sub(r"[^a-z0-9]", "", key.lower())
    singular_key = normalized_key[:-1] if normalized_key.endswith("s") else normalized_key
    url_like = (
        normalized_key in _URL_LIKE_ARG_KEYS
        or singular_key in _URL_LIKE_ARG_KEYS
        or normalized_key.endswith("url")
        or normalized_key.endswith("urls")
        or normalized_key.endswith("uri")
        or normalized_key.endswith("uris")
    )
    host_like = (
        normalized_key in _HOST_LIKE_ARG_KEYS
        or singular_key in _HOST_LIKE_ARG_KEYS
        or normalized_key.endswith("host")
        or normalized_key.endswith("hosts")
    )
    if not (url_like or host_like):
        return None

    if raw.startswith("//"):
        return f"http:{raw}"
    if _URL_SCHEME_RE.match(raw):
        return raw
    if _looks_like_plain_network_target(raw):
        return f"http://{raw}"
    return None


def _looks_like_plain_network_target(value: str) -> bool:
    if value.startswith(("/", ".", "#")):
        return False
    if "\\" in value or any(ch.isspace() for ch in value):
        return False
    host = value.split("/", 1)[0].split("?", 1)[0].strip("[]").lower()
    if not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if ":" in host:
        return True
    return "." in host

_DEFAULT_PATH_DENYLIST = tuple(PermissionSettings().path_denylist)


@lru_cache(maxsize=1)
def _secret_repo_floor_specs() -> tuple[GitIgnoreSpec, GitIgnoreSpec | None]:
    """Compile the built-in secret/repo floor once, plus a case-folded pass."""
    patterns = [
        pattern.replace("\\", "/").strip()
        for pattern in _DEFAULT_PATH_DENYLIST
        if str(pattern or "").strip()
    ]
    folded = (
        GitIgnoreSpec.from_lines(pattern.lower() for pattern in patterns)
        if _FILESYSTEM_IS_CASE_INSENSITIVE
        else None
    )
    return GitIgnoreSpec.from_lines(patterns), folded


def _granted_path_allows_operation(context: PermissionContext, path: str, operation: str) -> bool:
    from backend.permissions.grants import permission_profile_allows_path

    return permission_profile_allows_path(
        context.additional_permissions, path,
        cwd=Path(context.workspace_root), workspace_root=context.workspace_root,
        write=operation == "write",
    )


def _matches_secret_repo_floor(workspace_relative_path: str) -> bool:
    """True when a path is inside the bypass-immune secret/repo floor.

    Shares the exact patterns is_path_allowed enforces for the file tools, so
    the shell write guard and the file tools cannot disagree about what is
    protected. Negations (``!.env.example``) are honoured by the same spec.
    """
    candidate = workspace_relative_path.replace("\\", "/").strip().lstrip("/")
    if not candidate:
        return False
    spec, folded_spec = _secret_repo_floor_specs()
    if spec.match_file(candidate):
        return True
    return bool(folded_spec is not None and folded_spec.match_file(candidate.lower()))


def _bypass_denylist(host_constraints: list[str]) -> list[str]:
    """Keep built-in guards and host constraints in bypass, drop local settings.

    Bypass waives the user's own workspace policy (``permissions.path_denylist``
    in settings.json), never the built-in secret/repo floor and never a
    constraint the host injected into the live permission context — managed
    ``permissions.filesystem.deny_read`` arrives that way, and the sandbox layer
    already folds it into its hard denies, so discarding it here made the two
    layers disagree about the same administrator policy.

    The built-in floor is appended last so a host pattern cannot negate it.
    """
    return list(
        dict.fromkeys(
            [
                *(
                    str(pattern).strip()
                    for pattern in host_constraints
                    if str(pattern or "").strip()
                ),
                *_DEFAULT_PATH_DENYLIST,
            ]
        )
    )


class PermissionChecker:
    """
    工具调用权限检查。

    职责：
    1. 根据工具名 → 判定权限级别
    2. 根据路径参数 → 检查白名单/黑名单
    """

    def __init__(
        self,
        settings: PermissionSettings,
        workspace_root: Path | str | None | object = _WORKSPACE_ROOT_UNSET,
    ) -> None:
        self._settings = settings
        if workspace_root is _WORKSPACE_ROOT_UNSET:
            self._workspace_root: Path | None = Path.cwd().resolve()
        elif workspace_root is None:
            self._workspace_root = None
        else:
            self._workspace_root = Path(workspace_root).expanduser().resolve()
        self._rule_matcher = (
            PermissionRuleMatcher(self._workspace_root)
            if self._workspace_root is not None
            else None
        )
        # Pre-parse content rules (Tool(content) syntax) once.
        from backend.permissions.content_rules import parse_content_rules

        self._content_allow = parse_content_rules(list(getattr(settings, "content_allow_rules", [])))
        self._content_ask = parse_content_rules(list(getattr(settings, "content_ask_rules", [])))
        self._content_deny = parse_content_rules(list(getattr(settings, "content_deny_rules", [])))

    def with_workspace_root(
        self,
        workspace_root: Path | str | None | object = _WORKSPACE_ROOT_UNSET,
    ) -> "PermissionChecker":
        if workspace_root is _WORKSPACE_ROOT_UNSET:
            return self
        return PermissionChecker(
            self._settings,
            None if workspace_root is None else Path(workspace_root),
        )

    def capability_available(
        self,
        tool_name: str,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> tuple[bool, str]:
        """Check static capability boundaries without inventing invocation args."""
        policy_name = tool.policy_identity if tool is not None else tool_name
        policy_aliases = (*tool.policy_aliases, *getattr(tool, "policy_deny_aliases", ())) if tool is not None else ()
        matched = self._first_match(policy_name, self._settings.always_deny, policy_aliases)
        if matched:
            return False, f"Tool '{tool_name}' is disabled by the static permission policy ({matched})."
        if context is not None:
            matched = self._first_match(policy_name, context.tool_deny_rules, policy_aliases)
            if matched:
                return False, f"Tool '{tool_name}' is disabled for this session ({matched})."
        if tool is not None:
            declared = getattr(tool, "permission", PermissionLevel.AUTO)
            if declared == PermissionLevel.ALWAYS_DENY:
                return False, f"Tool '{tool_name}' declares an unavailable capability."
            if not tool.is_capability_available(context):
                return False, f"Tool '{tool_name}' is unavailable in the current permission context."
        return True, ""

    def _content_rule_decision(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        policy_aliases: tuple[str, ...] = (),
        deny_aliases: tuple[str, ...] = (),
    ) -> tuple[PermissionLevel, str] | None:
        """Evaluate parsed Tool(content) rules with deny > ask > allow precedence."""
        from backend.permissions.content_rules import rule_matches_call

        restriction_aliases = (*policy_aliases, *deny_aliases)
        for rule in self._content_deny:
            if rule_matches_call(rule, tool_name, args, effect="deny", policy_aliases=restriction_aliases):
                return PermissionLevel.ALWAYS_DENY, rule.tool_glob
        for rule in self._content_ask:
            if rule_matches_call(rule, tool_name, args, effect="ask", policy_aliases=restriction_aliases):
                return PermissionLevel.CONFIRM, rule.tool_glob
        for rule in self._content_allow:
            if rule_matches_call(rule, tool_name, args, effect="allow", policy_aliases=policy_aliases):
                return PermissionLevel.AUTO, rule.tool_glob
        return None

    def build_context(
        self,
        *,
        mode: str = "confirm",
        session_overrides: dict[str, PermissionLevel] | None = None,
        command_prompt_allow_rules: list[str] | tuple[str, ...] | None = None,
        tool_deny_rules: list[str] | None = None,
        filesystem_constraints: dict[str, list[str]] | None = None,
        workspace_scope: str = "project",
        source: str = "runtime",
        pre_plan_mode: str | None = None,
        approval_policy: Any = "",
        granular_approval: dict[str, bool] | None = None,
        sandbox_mode: str = "",
        requirements_source: str = "",
    ) -> PermissionContext:
        normalized_mode = normalize_permission_mode_token(mode)
        # Explicit turn authority retains the complete Source policy. Builtin
        # Full access overrides config with Never; other modes retain config.
        if approval_policy == "granular":
            resolved = ApprovalPolicy.parse({"granular": granular_approval})
        elif approval_policy not in (None, ""):
            resolved = ApprovalPolicy.parse(approval_policy)
        else:
            resolved = policy_for_mode(normalized_mode, self._settings.approval_policy)
        return PermissionContext(
            mode=normalized_mode,  # type: ignore[arg-type]
            session_overrides=dict(session_overrides or {}),
            command_prompt_allow_rules=tuple(
                dict.fromkeys(
                    prompt
                    for prompt in (
                        str(item or "").strip()
                        for item in (command_prompt_allow_rules or ())
                    )
                    if prompt
                )
            ),
            tool_deny_rules=list(tool_deny_rules or []),
            filesystem_constraints=dict(filesystem_constraints or {}),
            workspace_scope=workspace_scope if workspace_scope in {"computer", "project", "worktree"} else "project",
            source=source,
            pre_plan_mode=(
                normalize_permission_mode_token(pre_plan_mode)
                if str(pre_plan_mode or "").strip()
                and normalize_permission_mode_token(pre_plan_mode) != "plan"
                else None
            ),
            approval_policy=resolved.kind,
            granular_approval=resolved.granular.to_dict() if resolved.granular is not None else None,
            sandbox_mode=sandbox_mode,
            requirements_source=requirements_source,
        )

    def check(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> PermissionLevel:
        """
        判定工具调用的权限级别。

        Args:
            tool_name: 工具名称（支持 mcp__server__tool 格式）
            args: 工具参数（用于路径检查）
            tool: 工具实例。提供时，先咨询工具自有的 check_permission /
                  is_read_only（对应 CC 的 checkPermissions / isReadOnly），
                  再回退到集中式策略。

        Returns:
            PermissionLevel
        """
        return self._evaluate_level(tool_name, args, context=context, tool=tool)[0]

    def _evaluate_level(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> tuple[PermissionLevel, str, str]:
        if tool_name == "run_command" and context is not None and context.captured_exec_policy is not None:
            allowed, reason = self.capability_available(tool_name, context=context, tool=tool)
            if not allowed:
                return PermissionLevel.ALWAYS_DENY, "capability_boundary", reason
            native = context.captured_exec_policy
            if native.kind == "forbidden":
                return PermissionLevel.ALWAYS_DENY, "exec_policy", native.reason or "blocked by policy"
            if context.strict_auto_review:
                return PermissionLevel.CONFIRM, "tool", "strict_auto_review"
            return (PermissionLevel.AUTO if native.kind == "skip" else PermissionLevel.CONFIRM,
                "exec_policy", native.reason or native.kind)
        result = self._evaluate_policy_level(tool_name, args, context=context, tool=tool)
        if result[0] in {PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW} and context is not None and context.approval_policy in {"never", "granular"} and result[1:] != ("tool", "strict_auto_review"):
            flow = self._approval_flow(tool, result[1], result[2])
            reason = rejected_approval_reason(context, flow)
            if reason is not None:
                source = "managed_requirements" if context.requirements_source else "approval_policy"
                return PermissionLevel.ALWAYS_DENY, source, reason
        return result

    def _approval_flow(self, tool: Any, source: str, rule: str) -> ApprovalFlow:
        # The admitted MCP producer creates ApprovalAction::McpToolCall. It
        # is independent of the granular sandbox/rule/elicitation switches.
        if tool is not None:
            spec = tool.get_spec()
            if spec is not None and spec.capability == "mcp":
                return "mcp_tool_call"
        if source == "content_rule" or source == "session_override":
            return "rules"
        if source == "static_policy":
            for field in ("require_confirm", "require_diff_review"):
                if rule in getattr(self._settings, field) and field not in self._settings.builtin_approval_routing:
                    return "rules"
        return "sandbox_approval"

    def _evaluate_policy_level(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> tuple[PermissionLevel, str, str]:
        capability_floor = (PermissionLevel.AUTO, "", "")
        policy_name = tool.policy_identity if tool is not None else tool_name
        policy_aliases = tool.policy_aliases if tool is not None else ()
        deny_aliases = (*policy_aliases, *getattr(tool, "policy_deny_aliases", ())) if tool is not None else ()

        def raise_floor(
            level: PermissionLevel,
            source: str,
            rule: str,
        ) -> None:
            nonlocal capability_floor
            if _PERMISSION_RESTRICTIVENESS[level] > _PERMISSION_RESTRICTIVENESS[capability_floor[0]]:
                capability_floor = (level, source, rule)

        def apply_floor(
            level: PermissionLevel,
            source: str,
            rule: str,
        ) -> tuple[PermissionLevel, str, str]:
            if _PERMISSION_RESTRICTIVENESS[capability_floor[0]] > _PERMISSION_RESTRICTIVENESS[level]:
                return capability_floor
            return level, source, rule

        # 1. 检查是否在 always_deny
        matched = self._first_match(policy_name, self._settings.always_deny, deny_aliases)
        if matched:
            return PermissionLevel.ALWAYS_DENY, "static_policy", matched

        policy_override: tuple[PermissionLevel, str, str] | None = None
        if context is not None:
            matched = self._first_match(policy_name, context.tool_deny_rules, deny_aliases)
            if matched:
                return PermissionLevel.ALWAYS_DENY, "context_deny", matched

            override = self._resolve_override_match(policy_name, context.session_overrides, policy_aliases, deny_aliases=deny_aliases)
            if override is not None:
                pattern, level = override
                if level == PermissionLevel.ALWAYS_DENY:
                    return level, "session_override", pattern
                policy_override = (level, "session_override", pattern)

        # Content-level rules (Tool(content) syntax). A deny rule wins in every
        # mode (safety); an allow rule forces AUTO except in plan mode (plan is
        # strict read-only regardless of allow rules).
        content_match = self._content_rule_decision(policy_name, args, policy_aliases, deny_aliases)
        content_decision = content_match[0] if content_match is not None else None
        if content_decision == PermissionLevel.ALWAYS_DENY:
            return PermissionLevel.ALWAYS_DENY, "content_rule", "content_deny"
        if policy_override is None and content_decision in {
            PermissionLevel.AUTO,
            PermissionLevel.CONFIRM,
        }:
            policy_override = (
                content_decision,
                "content_rule",
                "content_allow"
                if content_decision == PermissionLevel.AUTO
                else "content_ask",
            )

        tool_level: PermissionLevel | None = None
        tool_decision_is_explicit = False
        if tool is not None:
            tool_level, tool_decision_is_explicit = self._tool_permission_decision(
                tool,
                args,
                context,
            )

        # Static policy is a restriction layer, not a fallback used only when
        # a UI mode is absent. Apply it through the existing capability floor
        # before confirm/auto/accept-edits choose their default behavior.
        static_auto: tuple[PermissionLevel, str, str] | None = None
        static_floor: list[tuple[PermissionLevel, str, str]] = []
        explicit_static_floor: list[tuple[PermissionLevel, str, str]] = []
        granted_tool_auto = (
            context is not None
            and tool_name in {"run_command", "apply_patch"}
            and context.additional_permissions is not None
            and not context.additional_permissions.is_empty
            and tool_decision_is_explicit and tool_level == PermissionLevel.AUTO
        )
        for field_name, patterns, level in (
            ("require_diff_review", self._settings.require_diff_review, PermissionLevel.DIFF_REVIEW),
            ("require_confirm", self._settings.require_confirm, PermissionLevel.CONFIRM),
        ):
            if granted_tool_auto and field_name in self._settings.builtin_approval_routing:
                continue
            static_floor.extend(
                (level, "static_policy", pattern) for pattern in patterns
                if _tool_pattern_matches(policy_name, pattern, deny_aliases)
            )
            if field_name not in self._settings.builtin_approval_routing:
                explicit_static_floor.extend(
                    (level, "static_policy", pattern) for pattern in patterns
                    if _tool_pattern_matches(policy_name, pattern, deny_aliases)
                )
        if granted_tool_auto:
            # A path/network grant preapproves authority, never a user's
            # explicit tool rule. Builtin routing was removed above; these
            # remaining restrictions must reach the invocation-specific AUTO.
            for floor in static_floor:
                raise_floor(*floor)
        auto_matches = [pattern for pattern in self._settings.auto_allow if _tool_pattern_matches(policy_name, pattern, policy_aliases)]
        exact_names = {policy_name, *policy_aliases}
        matched = next((pattern for pattern in auto_matches if pattern in exact_names), auto_matches[0] if auto_matches else "")
        if matched:
            static_auto = (PermissionLevel.AUTO, "static_policy", matched)

        ordinary_exec = (
            context is not None and tool is not None and tool_name == "run_command"
            and (args or {}).get("with_escalated_permissions") is not True
            and tool.get_side_effect_kind(args) != "destructive"
        )
        strict_review = context is not None and context.strict_auto_review and tool is not None and (
            ordinary_exec or tool_name == "apply_patch"
        )
        granted_tool_strict = (
            strict_review and context.additional_permissions is not None and not context.additional_permissions.is_empty
            and tool_decision_is_explicit and tool_level == PermissionLevel.CONFIRM
        )
        never_regular_exec = ordinary_exec and context.approval_policy == "never" and not strict_review

        # cc's COMMAND_SUBSTITUTION_PATTERNS make commands carrying injection
        # signals 'ask' instead of silently auto-classified read-only.
        injection_reason = ""
        if args is not None and not strict_review and not (context is not None and (
            context.mode == "bypass" or context.approval_policy == "never"
        )) and (
            fnmatch.fnmatch(tool_name, "run_command") or tool_name.startswith("terminal_")
        ):
            shell_command = str(args.get("command") or args.get("cmd") or "")
            injection_reason = command_injection_risk(shell_command)
            if injection_reason:
                static_auto = None
                raise_floor(PermissionLevel.CONFIRM, "injection_risk", injection_reason)

        if (
            tool_name == "run_command"
            and args is not None
            and str(args.get("command") or "").strip()
            and context is not None
            and context.mode != "bypass"
        ):
            from backend.sandbox.policy import SandboxEnforcement, sandbox_policy_for_permission_context
            from backend.sandbox.runner import SandboxRunner

            workspace = Path(context.workspace_root or self._workspace_root or Path.cwd()).resolve()
            policy = sandbox_policy_for_permission_context(workspace, context)
            if context.additional_permissions is not None:
                policy = policy.with_additional_permissions(context.additional_permissions)
            resolved = policy.resolve(cwd=workspace)
            if resolved.enforcement is SandboxEnforcement.MANAGED and not resolved.allow_network:
                capability = SandboxRunner(policy).capability(cwd=workspace)
                if capability.available and not capability.network_isolated:
                    raise_floor(PermissionLevel.CONFIRM, "sandbox_capability", "network_not_isolated")

        if context is not None and context.mode == "plan":
            if tool is None or tool_level is None:
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:deny"
            if tool_level == PermissionLevel.ALWAYS_DENY:
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:tool_deny"
            try:
                side_effect_kind = str(tool.get_side_effect_kind(args) or "").strip().lower()
            except Exception:
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:metadata_error"
            if side_effect_kind == "none":
                if tool_name.startswith("mcp__"):
                    return PermissionLevel.ALWAYS_DENY, "mode", "plan:untrusted_mcp"
                plan_rule = "plan:auto"
            elif (
                tool_name in {"edit_file", "write_file"}
                and tool_decision_is_explicit
                and tool_level == PermissionLevel.AUTO
            ):
                # File tools grant only the current plan file. Other writes
                # remain unavailable even when an approval rule matches.
                plan_rule = "plan:plan_file"
            else:
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:side_effect"

            # Plan constrains side effects; it must also retain the same
            # explicit asks as ordinary routing (including ExitPlanMode).
            if tool_decision_is_explicit:
                raise_floor(tool_level, "tool", f"{tool_name}.check_permission")
            if policy_override is not None:
                return apply_floor(*policy_override)
            if not (tool_decision_is_explicit and tool_level == PermissionLevel.AUTO):
                for floor in static_floor:
                    raise_floor(*floor)
            return apply_floor(PermissionLevel.AUTO, "mode", plan_rule)

        from backend.mcp.registry import MCPToolProxy

        if isinstance(tool, MCPToolProxy):
            # Source owns MCP approval through its captured server policy and
            # three-state annotations. Local filesystem/external capability
            # defaults must not add another MCP approval producer.
            for floor in explicit_static_floor:
                raise_floor(*floor)
            if policy_override is not None and policy_override[0] in {PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW}:
                raise_floor(*policy_override)
            if context is not None and context.strict_auto_review:
                return apply_floor(PermissionLevel.CONFIRM, "tool", "strict_auto_review")
            if tool._approval_mode == "auto":
                if policy_override is not None and policy_override[0] == PermissionLevel.AUTO:
                    return apply_floor(*policy_override)
                if static_auto is not None:
                    return apply_floor(*static_auto)
            return apply_floor(tool_level, "tool", f"{tool_name}.check_permission")

        # Bypass is MiniCode's explicit unattended/full-access mode.  It
        # removes approval routing for ordinary workspace and external work;
        # otherwise DIFF_REVIEW/CONFIRM would be converted into a denial by
        # approval_policy="never", hiding the capability from the provider and
        # making the user's selected mode unusable.  Hard policy denies and
        # tool-owned refusals have already returned above.  Path containment
        # and sensitive-file checks remain authoritative in evaluate().
        if context is not None and (context.mode == "bypass" or strict_review or never_regular_exec):
            for floor in explicit_static_floor:
                raise_floor(*floor)
            if policy_override is not None and policy_override[0] in {PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW}:
                raise_floor(*policy_override)
        if strict_review:
            raise_floor(PermissionLevel.CONFIRM, "tool", "strict_auto_review")
        if context is not None and context.mode == "bypass":
            if tool_level == PermissionLevel.ALWAYS_DENY:
                return PermissionLevel.ALWAYS_DENY, "tool", f"{tool_name}.check_permission"
            if tool is not None:
                try:
                    side_effect_kind = str(tool.get_side_effect_kind(args) or "").strip().lower()
                except Exception:
                    return PermissionLevel.ALWAYS_DENY, "tool", f"{tool_name}.side_effect_metadata"
                # Non-MCP destructive tool declarations retain their local
                # capability boundary. The captured Source MCP producer
                # has already resolved above.
                if bool(getattr(tool, "destructive", False)) or side_effect_kind == "destructive":
                    return PermissionLevel.CONFIRM, "capability_boundary", "bypass:destructive"
            return apply_floor(PermissionLevel.AUTO, "mode", "bypass:auto")

        # CC asks whenever a filesystem-shaped tool call has no getPath seam.
        # A forgotten declaration must not silently inherit AUTO in MiniCode.
        if (
            tool is not None
            and not (context is not None and context.mode == "bypass")
            and _has_undeclared_path_argument(tool, args)
        ):
            raise_floor(
                PermissionLevel.CONFIRM,
                "capability_boundary",
                f"{tool_name}.undeclared_path",
            )

        if (
            not (context is not None and context.mode == "bypass")
            and _network_target_requires_confirmation(tool_name, args, context, tool)
        ):
            raise_floor(PermissionLevel.CONFIRM, "capability_boundary", "network_target")

        # Tool-owned checks define an invocation-specific capability floor.
        # A content allow or remembered ordinary approval cannot lower it.
        if tool is not None:
            if (
                tool_level is not None
                and tool_decision_is_explicit
                and (
                    context is None
                    or context.mode != "bypass"
                    or tool_level in {
                        PermissionLevel.ALWAYS_DENY,
                        PermissionLevel.DIFF_REVIEW,
                    }
                )
            ):
                raise_floor(tool_level, "tool", f"{tool_name}.check_permission")
            # An explicit allow is a decision about this exact capability, so it
            # clears the metadata-derived external/open-world floor: the tool's
            # own check_permission returning AUTO (sandbox exclusions,
            # autoAllowCommandsIfSandboxed), an exact Tool(content) rule, or a scoped
            # session override ("don't ask again for this tool"). A broad session
            # mode never clears it, and neither does a coarse static auto_allow
            # tool-name glob — embedding hosts synthesize those themselves, so
            # they are not evidence of a user decision about this capability.
            # An injection-risk signal also keeps the floor: that asymmetry is
            # the point.
            explicit_capability_allow = not injection_reason and (
                never_regular_exec or strict_review
                or
                (tool_decision_is_explicit and tool_level == PermissionLevel.AUTO)
                or (
                    policy_override is not None
                    and policy_override[0] == PermissionLevel.AUTO
                    and policy_override[1] in {"content_rule", "session_override"}
                )
            )
            metadata_floor = self._tool_capability_floor(
                tool,
                args,
                context,
                explicit_allow=explicit_capability_allow,
                permissions_preapproved=granted_tool_auto or granted_tool_strict,
            )
            if metadata_floor is not None:
                raise_floor(
                    metadata_floor,
                    "tool_capability",
                    f"{tool_name}.runtime_metadata",
                )

        # Source reviews ordinary actions under an existing strict turn grant
        # through Guardian. Configured rule prompts and stronger capability
        # floors retain their original producer, so Never/granular still gate
        # those actual requests. Builtin routing cannot overwrite the sentinel.
        if strict_review:
            return capability_floor
        if never_regular_exec:
            # default_exec_approval_requirement(Never) skips ordinary prompts;
            # the command continues to execute within its actual sandbox.
            return apply_floor(PermissionLevel.AUTO, "mode", "never:auto")

        if policy_override is not None:
            return apply_floor(*policy_override)

        # A tool-owned AUTO describes this exact invocation (a sandbox-excluded
        # command, autoAllowCommandsIfSandboxed, a read-only browser action), so it
        # is more specific than a static require_confirm glob and is returned
        # before the static routing layer. apply_floor still keeps every
        # capability floor accumulated above (destructive metadata, MCP
        # extension boundary, undeclared path, network target, diff review).
        if (
            tool_decision_is_explicit
            and tool_level == PermissionLevel.AUTO
            and not injection_reason
        ):
            return apply_floor(tool_level, "tool", f"{tool_name}.check_permission")

        # Static rules are the default routing layer. Explicit content/session
        # allows above are more specific; capability boundaries still win.
        for floor in static_floor:
            raise_floor(*floor)

        # A user-authored static floor that only TIES with the routing decision
        # below must still win the attribution: a pre-tool hook allow may skip
        # ordinary mode/tool prompts, but it must never skip an explicit
        # settings ask rule (cc's resolveHookPermissionDecision invariant).
        static_ask_floor = (
            max(static_floor, key=lambda floor: _PERMISSION_RESTRICTIVENESS[floor[0]])
            if static_floor
            else None
        )

        def apply_static_tie(
            level: PermissionLevel,
            source: str,
            rule: str,
        ) -> tuple[PermissionLevel, str, str]:
            if (
                static_ask_floor is not None
                and source not in {"static_policy", "injection_risk", "content_rule"}
                and _PERMISSION_RESTRICTIVENESS[level]
                == _PERMISSION_RESTRICTIVENESS[static_ask_floor[0]]
            ):
                return static_ask_floor
            return level, source, rule

        if static_auto is not None:
            return apply_static_tie(*apply_floor(*static_auto))

        if context is not None:
            mode_level: PermissionLevel | None = None
            if context.mode == "auto":
                mode_level = tool_level or PermissionLevel.CONFIRM
            elif context.mode == "confirm":
                mode_level = tool_level or PermissionLevel.CONFIRM

            if mode_level is not None:
                return apply_static_tie(
                    *apply_floor(mode_level, "mode", f"{context.mode}:{mode_level.value}")
                )

        if tool_level is not None:
            return apply_static_tie(*apply_floor(tool_level, "tool", f"{tool_name}.permission"))

        return apply_static_tie(
            *apply_floor(PermissionLevel.CONFIRM, "default", "confirm")
        )

    def _is_owner_allowed_tool_result_path(
        self,
        file_path: str | Path,
        *,
        context: PermissionContext | None = None,
    ) -> bool:
        from backend.agent.tool_result_persistence import is_tool_result_path

        workspace_root = getattr(context, "workspace_root", None) if context is not None else None
        return is_tool_result_path(
            file_path,
            conversation_id=str(getattr(context, "conversation_id", "") or ""),
            workspace_root=workspace_root or self._workspace_root,
        )

    def validate_file_operation(
        self,
        file_path: str,
        operation: str,
        content: str | None = None,
        *,
        context: PermissionContext | None = None,
        allow_workspace_escape: bool = False,
    ) -> tuple[bool, str]:
        if operation not in {"read", "write", "execute"}:
            return False, f"Unsupported file operation: {operation}"
        effective_root = (
            getattr(context, "workspace_root", None)
            if context is not None and getattr(context, "workspace_root", None) is not None
            else self._workspace_root
        )
        path = Path(file_path).expanduser()
        if not path.is_absolute() and effective_root is not None:
            effective_root = Path(effective_root).expanduser().resolve()
            path = effective_root / path
        if operation == "write":
            from backend.security.sensitive_files import is_protected_write_path

            if is_protected_write_path(
                path,
                state_roots=context.protected_state_roots if context is not None else (),
            ):
                return False, f"Refusing to modify protected application or repository state: {file_path}"
        if context is not None:
            from backend.agent.plans import is_current_plan_file

            if operation in {"read", "write"} and is_current_plan_file(path, context):
                return True, ""
        if operation == "read":
            if self._is_owner_allowed_tool_result_path(path, context=context):
                return True, ""
            constraints = context.filesystem_constraints if context is not None else {}
            for raw_root in constraints.get("readable_roots", []):
                try:
                    path.resolve().relative_to(Path(str(raw_root)).expanduser().resolve())
                    return True, ""
                except (OSError, ValueError):
                    continue
        if effective_root is None:
            return False, "This operation requires an open workspace."
        effective_root = Path(effective_root).expanduser().resolve()
        matcher = (
            self._rule_matcher
            if self._workspace_root == effective_root and self._rule_matcher is not None
            else PermissionRuleMatcher(effective_root)
        )
        return matcher.check_file_access(
            str(path.resolve()), operation,
            allow_workspace_escape=(
                allow_workspace_escape or (context is not None and context.mode == "bypass")
            ),
        )

    def evaluate(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> PermissionDecision:
        level, rule_source, matched_rule = self._evaluate_level(
            tool_name,
            args,
            context=context,
            tool=tool,
        )
        capability_denial = self._capability_denial_reason(
            tool_name,
            args,
            context=context,
            tool=tool,
        )
        decision = (
            "deny"
            if capability_denial or level == PermissionLevel.ALWAYS_DENY
            else "allow"
            if level == PermissionLevel.AUTO
            else "ask"
        )
        approval_policy = {
            PermissionLevel.AUTO: "auto",
            PermissionLevel.CONFIRM: "confirm",
            PermissionLevel.DIFF_REVIEW: "diff_review",
            PermissionLevel.ALWAYS_DENY: "deny",
        }[level]
        scope = self._decision_scope(tool_name, args, context=context, tool=tool)
        from backend.agent.final_tool_request import canonical_tool_request_digest

        return PermissionDecision(
            permission_level=level,
            decision=decision,
            capability_allowed=not bool(capability_denial),
            capability_reason=capability_denial or "Capability boundary allows this tool call.",
            approval_policy=approval_policy,
            matched_rule_source=rule_source,
            matched_rule=matched_rule,
            risk=self._decision_risk(level, tool),
            scope=scope,
            expiry="session" if rule_source == "session_override" else "call" if rule_source == "tool" else "policy",
            request_digest=canonical_tool_request_digest(tool.policy_identity if tool is not None else tool_name, args or {}),
        )

    def validate_command(self, command: str) -> tuple[bool, str]:
        return _check_catastrophic_command(command)

    def _is_allowed_workspace_root_path(
        self,
        file_path: str,
        *,
        context: PermissionContext | None = None,
    ) -> bool:
        effective_root = (
            getattr(context, "workspace_root", None)
            if context is not None and getattr(context, "workspace_root", None) is not None
            else self._workspace_root
        )
        if effective_root is None:
            return False
        effective_root = Path(effective_root).expanduser().resolve()
        path_obj = Path(file_path).expanduser()
        resolved_path = path_obj if path_obj.is_absolute() else effective_root / path_obj
        try:
            normalized_path = resolved_path.resolve().relative_to(effective_root).as_posix()
        except ValueError:
            return False
        denylist = list(self._settings.path_denylist)
        if context is not None and "denylist" in context.filesystem_constraints:
            denylist = list(context.filesystem_constraints["denylist"])
        raw_path = str(file_path).replace("\\", "/").strip()
        for deny_pattern in denylist:
            normalized_deny = deny_pattern.replace("\\", "/").strip()
            while normalized_deny.startswith("./"):
                normalized_deny = normalized_deny[2:]
            if fnmatch.fnmatch(raw_path, deny_pattern) or fnmatch.fnmatch(normalized_path, normalized_deny):
                return False
            if fnmatch.fnmatch(Path(file_path).name, deny_pattern):
                return False
        return normalized_path in {"", "."} and resolved_path.exists()

    def is_path_allowed(self, file_path: str, *, context: PermissionContext | None = None, include_granted_permissions: bool = False) -> bool:
        """检查路径是否在允许范围内，denylist 优先于 allowlist。"""
        return self.prepare_path_check(context=context, include_granted_permissions=include_granted_permissions)(file_path)

    def prepare_path_check(self, *, context: PermissionContext | None = None, include_granted_permissions: bool = False) -> Callable[[str], bool]:
        """Prepare one operation's policy; each candidate is resolved afresh.

        Search operations check thousands of files against the same roots and
        rules. Resolve those roots once without caching any path decision.
        """
        effective_root = (
            getattr(context, "workspace_root", None)
            if context is not None and getattr(context, "workspace_root", None) is not None
            else self._workspace_root
        )
        resolved_root = (
            Path(effective_root).expanduser().resolve()
            if effective_root is not None
            else None
        )
        path_denylist = list(self._settings.path_denylist)
        path_allowlist = list(self._settings.path_allowlist)
        readable_roots: list[Path] = []
        if context is not None:
            for raw_root in context.filesystem_constraints.get("readable_roots", []):
                try:
                    readable_roots.append(Path(str(raw_root)).expanduser().resolve())
                except (OSError, ValueError):
                    continue
            host_denylist: list[str] = []
            if "denylist" in context.filesystem_constraints:
                host_denylist = list(context.filesystem_constraints["denylist"])
                # A host/managed constraint narrows the surface; it does not
                # replace the built-in secret/repo floor. Appending the floor
                # last also keeps a host pattern from negating it.
                path_denylist = [*host_denylist, *_DEFAULT_PATH_DENYLIST]
            if "allowlist" in context.filesystem_constraints:
                path_allowlist = list(context.filesystem_constraints["allowlist"])
            if context.mode == "bypass":
                path_denylist = _bypass_denylist(host_denylist)

        deny_patterns = tuple(
            pattern.replace("\\", "/").strip()
            for pattern in path_denylist
            if str(pattern or "").strip()
        )
        deny_spec = _compiled_path_deny_spec(deny_patterns)
        # Match case-insensitively on NTFS/APFS without changing gitignore's
        # interpretation or the order of negation rules.
        folded_spec = (
            _compiled_path_deny_spec(tuple(pattern.lower() for pattern in deny_patterns))
            if _FILESYSTEM_IS_CASE_INSENSITIVE else None
        )
        bypass = context is not None and context.mode == "bypass"
        allow_patterns: list[str] = []
        for allow_pattern in path_allowlist:
            normalized = allow_pattern.replace("\\", "/").strip().rstrip("/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            allow_patterns.append(normalized)

        def is_allowed(file_path: str) -> bool:
            if windows_path_safety_reason(file_path):
                return False
            path_obj = Path(file_path).expanduser()
            resolved_path = (
                path_obj if path_obj.is_absolute() or resolved_root is None
                else resolved_root / path_obj
            ).resolve()
            declared_readable = False
            declared_grant = False
            if include_granted_permissions and context is not None and resolved_root is not None:
                from backend.permissions.grants import permission_profile_allows_path

                declared_grant = permission_profile_allows_path(
                    context.additional_permissions, resolved_path,
                    cwd=resolved_root, workspace_root=resolved_root,
                )
            for readable_root in readable_roots:
                try:
                    resolved_path.relative_to(readable_root)
                    declared_readable = True
                    break
                except ValueError:
                    continue
            if resolved_root is None and not declared_readable and not declared_grant:
                return False
            if resolved_root is not None:
                try:
                    normalized_path = resolved_path.relative_to(resolved_root).as_posix()
                except ValueError:
                    normalized_path = str(file_path).replace("\\", "/").strip()
            else:
                normalized_path = resolved_path.as_posix()
            if normalized_path == ".":
                normalized_path = ""
            raw_path = str(file_path).replace("\\", "/").strip()
            while normalized_path.startswith("./"):
                normalized_path = normalized_path[2:]
            candidate = normalized_path or raw_path.lstrip("/")
            if deny_spec.match_file(candidate):
                return False
            if folded_spec is not None and folded_spec.match_file(candidate.lower()):
                return False
            if bypass or declared_readable or declared_grant or not allow_patterns:
                return True
            for normalized in allow_patterns:
                if normalized in {"", "."}:
                    return True
                if normalized_path == normalized or normalized_path.startswith(normalized + "/"):
                    return True
                if fnmatch.fnmatch(normalized_path, normalized + "/*"):
                    return True
                if fnmatch.fnmatch(normalized_path, normalized + "/**"):
                    return True
            return False

        return is_allowed

    def get_denial_reason(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> str | None:
        """
        获取拒绝执行的原因（如果有）。

        用于向 Agent 提供可操作的反馈信息。
        """
        decision = self.evaluate(tool_name, args, context=context, tool=tool)
        if decision.permission_level == PermissionLevel.ALWAYS_DENY:
            return f"工具 '{tool_name}' 被管理员禁止使用。"
        return None if decision.capability_allowed else decision.capability_reason

    def _capability_denial_reason(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        *,
        context: PermissionContext | None = None,
        tool: "BaseTool | None" = None,
    ) -> str:
        if tool_name == "run_command" and context is not None and context.captured_exec_policy is not None:
            # Actual shell paths are enforced by the captured Source sandbox,
            # rather than guessing a command's writes from its cwd argument.
            return ""
        # Filesystem tools own the declaration of path-bearing arguments. The
        # checker applies one policy without maintaining a parallel tool-name
        # taxonomy.
        if args and tool is not None:
            try:
                workspace_paths = list(tool.get_workspace_paths(args))
            except Exception:
                # A failed extraction means the invocation's paths are unknown,
                # never that there are none to check.  Treating it as the latter
                # skipped the denylist and the DANGEROUS_* guards below, so a
                # tool whose extractor raised could read .env.  Every other
                # failure branch in this checker fails closed; so does this one.
                logger.warning(
                    "Failed to extract workspace paths for %s; denying at the capability boundary",
                    tool_name,
                    exc_info=True,
                )
                return (
                    f"无法确定工具 '{tool_name}' 访问的路径，出于安全已拒绝执行。"
                    "请修正该工具的路径声明后重试。"
                )
            for file_path in workspace_paths:
                if str(file_path or "").strip() in {"", "."}:
                    continue
                tool_result_read = False
                if bool(getattr(tool, "allow_tool_result_path", False)):
                    tool_result_read = self._is_owner_allowed_tool_result_path(
                        str(file_path),
                        context=context,
                    )
                root_path_allowed = bool(
                    getattr(tool, "allow_workspace_root_path", False)
                    and self._is_allowed_workspace_root_path(str(file_path), context=context)
                )
                plan_path_allowed = False
                if context is not None:
                    from backend.agent.plans import is_current_plan_file

                    plan_path_allowed = is_current_plan_file(str(file_path), context)
                if (
                    not tool_result_read
                    and not self.is_path_allowed(str(file_path), context=context, include_granted_permissions=tool_name in {"run_command", "apply_patch"})
                    and not root_path_allowed
                    and not plan_path_allowed
                ):
                    return (
                        f"路径 '{file_path}' 不在允许范围内。"
                        f"允许的路径: {self._settings.path_allowlist}。"
                        f"禁止的路径模式: {self._settings.path_denylist}。"
                    )

                if not tool_result_read:
                    try:
                        operation = (
                            "write"
                            if tool.get_side_effect_kind(args) in {"workspace", "destructive"}
                            else "read"
                        )
                    except Exception:
                        operation = "read"
                    allowed, reason = self.validate_file_operation(
                        str(file_path),
                        operation,
                        context=context,
                        allow_workspace_escape=(
                            (tool_name == "run_command"
                             and args.get("with_escalated_permissions") is True
                             and (context is None or context.allow_unsandboxed_commands))
                            or (tool_name in {"run_command", "apply_patch"}
                                and context is not None
                                and context.workspace_root is not None
                                and _granted_path_allows_operation(context, str(file_path), operation))
                        ),
                    )
                    if not allowed:
                        return reason
        return ""

    def _decision_scope(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
        *,
        context: PermissionContext | None,
        tool: "BaseTool | None",
    ) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "workspace_scope": context.workspace_scope if context is not None else "project",
            "source": context.source if context is not None else "settings",
        }
        if context is not None and context.requirements_source:
            scope["requirements_source"] = context.requirements_source
        raw_args = args or {}
        path = raw_args.get("file_path") or raw_args.get("directory") or raw_args.get("path")
        url = raw_args.get("url") or raw_args.get("uri") or raw_args.get("href")
        if path:
            scope["boundary"] = "filesystem"
            scope["target"] = str(path)
        elif url:
            scope["boundary"] = "network"
            scope["target"] = urlparse(str(url)).hostname or str(url)
        elif tool is not None and bool(getattr(tool, "open_world", False)):
            scope["boundary"] = "network"
        elif tool is not None and bool(getattr(tool, "mutates_workspace", False)):
            scope["boundary"] = "filesystem"
        elif tool is not None:
            try:
                side_effect_kind = tool.get_side_effect_kind(args)
            except Exception:
                side_effect_kind = ""
            scope["boundary"] = "system" if side_effect_kind in {"external", "destructive"} else "general"
        else:
            scope["boundary"] = "general"
        return scope

    @staticmethod
    def _decision_risk(level: PermissionLevel, tool: "BaseTool | None") -> str:
        if tool is not None and bool(getattr(tool, "destructive", False)):
            return "critical"
        if level == PermissionLevel.ALWAYS_DENY:
            return "high"
        if tool is not None and (
            bool(getattr(tool, "open_world", False))
            or bool(getattr(tool, "mutates_external_state", False))
        ):
            return "high"
        if level in {PermissionLevel.CONFIRM, PermissionLevel.DIFF_REVIEW} or (
            tool is not None and bool(getattr(tool, "mutates_workspace", False))
        ):
            return "medium"
        return "low"

    def policy_snapshot(self) -> dict[str, list[str]]:
        """Return the current baseline permission policy from static settings."""
        return {
            "auto_allow": list(self._settings.auto_allow),
            "require_confirm": list(self._settings.require_confirm),
            "require_diff_review": list(self._settings.require_diff_review),
            "always_deny": list(self._settings.always_deny),
            "path_allowlist": list(self._settings.path_allowlist),
            "path_denylist": list(self._settings.path_denylist),
        }

    @staticmethod
    def _tool_permission_decision(
        tool: "BaseTool",
        args: dict[str, Any] | None,
        context: PermissionContext | None,
    ) -> tuple[PermissionLevel, bool]:
        """Return the invocation policy and whether a tool explicitly raised it."""
        declared = getattr(tool, "permission", PermissionLevel.AUTO)
        if not isinstance(declared, PermissionLevel):
            declared = PermissionLevel.CONFIRM
        try:
            decided = tool.check_permission(args, context)
            if decided is not None:
                return decided, True
            if tool.is_read_only(args):
                return PermissionLevel.AUTO, False
        except Exception:  # pragma: no cover - metadata errors must fail closed
            # A broken metadata hook is not evidence that the invocation is
            # read-only. Preserve stricter declarations and otherwise require
            # an explicit confirmation instead of silently granting AUTO.
            if _PERMISSION_RESTRICTIVENESS[declared] > _PERMISSION_RESTRICTIVENESS[PermissionLevel.CONFIRM]:
                return declared, False
            return PermissionLevel.CONFIRM, False
        return declared, False

    @staticmethod
    def _tool_invocation_is_read_only(
        tool: "BaseTool",
        args: dict[str, Any] | None,
    ) -> bool:
        """Return True when a tool explicitly classifies this call as read-only."""
        try:
            return bool(tool.is_read_only(args))
        except Exception:  # pragma: no cover - defensive; failures never widen access
            return False

    @staticmethod
    def _tool_capability_floor(
        tool: "BaseTool",
        args: dict[str, Any] | None,
        context: PermissionContext | None,
        *,
        explicit_allow: bool = False,
        permissions_preapproved: bool = False,
    ) -> PermissionLevel | None:
        """Translate tool metadata into an approval floor that rules cannot lower.

        ``permission`` is normally a policy default and may be reduced by an
        exact content rule or a scoped session approval.  Three declarations
        are different because they describe the capability being exercised:

        * diff-review tools must still present their diff outside the explicit
          workspace-write modes;
        * destructive invocations always require a human confirmation;
        * open-world tools whose own metadata does not classify the invocation
          as an ordinary read require confirmation.

        ``explicit_allow`` marks the third case as already decided by the user
        for this exact call (tool-owned AUTO, exact content rule, scoped session
        override).  Destructive and diff-review floors stay in force because
        they describe harm, not merely reach.

        Bypass and plan modes are resolved before this helper.  Metadata or
        classification failures fail closed only to the tool's declared
        permission; they never silently turn a mutating call into ``AUTO``.
        """
        declared = getattr(tool, "permission", PermissionLevel.AUTO)
        if not isinstance(declared, PermissionLevel):
            declared = PermissionLevel.AUTO

        if declared == PermissionLevel.ALWAYS_DENY:
            return PermissionLevel.ALWAYS_DENY

        from backend.mcp.registry import MCPToolProxy

        if isinstance(tool, MCPToolProxy):
            # MCP risk metadata is handled by Source's approval-mode decision,
            # including Writes/Approve and real-profile Never preapproval.
            return None

        mode = getattr(context, "mode", "confirm") if context is not None else "confirm"
        if declared == PermissionLevel.DIFF_REVIEW and mode != "auto" and not permissions_preapproved:
            return PermissionLevel.DIFF_REVIEW

        try:
            side_effect_kind = str(tool.get_side_effect_kind(args) or "").strip().lower()
        except Exception:  # pragma: no cover - defensive metadata fallback
            logger.warning(
                "Failed to determine runtime side effects for %s; retaining declared permission floor",
                getattr(tool, "name", type(tool).__name__),
                exc_info=True,
            )
            side_effect_kind = ""
        destructive = bool(getattr(tool, "destructive", False)) or side_effect_kind == "destructive"
        if destructive:
            return (
                declared
                if _PERMISSION_RESTRICTIVENESS[declared]
                > _PERMISSION_RESTRICTIVENESS[PermissionLevel.CONFIRM]
                else PermissionLevel.CONFIRM
            )

        external_capability = bool(
            getattr(tool, "open_world", False)
            or getattr(tool, "mutates_external_state", False)
        )
        if external_capability and not explicit_allow:
            try:
                invocation_is_read_only = bool(tool.is_read_only(args))
            except Exception:  # pragma: no cover - unknown external calls require review
                invocation_is_read_only = False
            if not invocation_is_read_only:
                return (
                    declared
                    if _PERMISSION_RESTRICTIVENESS[declared]
                    > _PERMISSION_RESTRICTIVENESS[PermissionLevel.CONFIRM]
                    else PermissionLevel.CONFIRM
                )

        return None

    @staticmethod
    def _resolve_override(
        tool_name: str, overrides: dict[str, PermissionLevel], policy_aliases: tuple[str, ...] = ()
    ) -> PermissionLevel | None:
        match = PermissionChecker._resolve_override_match(tool_name, overrides, policy_aliases)
        return match[1] if match is not None else None

    @staticmethod
    def _resolve_override_match(
        tool_name: str,
        overrides: dict[str, PermissionLevel],
        policy_aliases: tuple[str, ...] = (),
        *,
        deny_aliases: tuple[str, ...] = (),
    ) -> tuple[str, PermissionLevel] | None:
        matches = [
            (pattern, level) for pattern, level in overrides.items()
            if _tool_pattern_matches(
                tool_name, pattern,
                (*policy_aliases, *deny_aliases) if level != PermissionLevel.AUTO else policy_aliases,
            )
        ]
        # A later approval for the new wire name cannot erase a deny attached
        # to the same raw tool/server identity before it was renamed.
        exact_names = {tool_name, *policy_aliases}
        return next(
            (match for match in matches if match[1] == PermissionLevel.ALWAYS_DENY),
            next((match for match in matches if match[0] in exact_names), matches[0] if matches else None),
        )

    @staticmethod
    def _first_match(tool_name: str, patterns: list[str], policy_aliases: tuple[str, ...] = ()) -> str:
        return next((pattern for pattern in patterns if _tool_pattern_matches(tool_name, pattern, policy_aliases)), "")
