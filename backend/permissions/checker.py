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
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from functools import lru_cache
from pathspec.gitignore import GitIgnoreSpec

from backend.config import PermissionSettings
from backend.permissions.context import PermissionContext, PermissionDecision
from backend.permissions.network import assess_network_url
from backend.permissions.rules import PermissionRuleMatcher, SandboxValidator
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
def _tool_pattern_matches(tool_name: str, pattern: str) -> bool:
    """Match ordinary globs plus an MCP server-level rule.

    `mcp__server` is a deliberate server boundary, not a literal tool named
    server: it covers `mcp__server__*` without making similarly named servers
    match by raw prefix.
    """
    normalized_pattern = str(pattern or "").strip()
    if fnmatch.fnmatch(tool_name, normalized_pattern):
        return True
    if normalized_pattern.startswith("mcp__") and "*" not in normalized_pattern:
        return tool_name.startswith(normalized_pattern + "__")
    return False


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
    (re.compile(r"rd\s+/[sS]\s+/[qQ]\s+[A-Z]:\\$", re.I), "recursive delete of drive root"),
    (re.compile(r"\b(?:rd|rmdir)\b\s+/[sS]\s+/[qQ]\s+[A-Z]:\\Users\\[^\\\s]+\\?\s*$", re.I), "recursive delete of system directory"),
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
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    option_index = -1
    if executable in _POSIX_SHELL_WRAPPERS:
        option_index = next(
            (index for index, value in enumerate(argv[1:], start=1) if value == "-c"),
            -1,
        )
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
    """Normalize a command for catastrophic-pattern matching.

    Claude Code tokenizes the command into argv (``bashParser``) before testing
    dangerous patterns, so quoted spellings collapse to their real targets. This
    approximates that by dropping shell quotes (replacing them with spaces so
    ``rm -rf "/"`` collapses to ``rm -rf /``). Home-variable trailing-slash
    variants are handled by the patterns themselves.
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
    if _depth >= 4:
        return False, "命令被安全策略拦截: shell wrapper nesting is too deep"
    payload, parse_error = _shell_wrapper_payload(stripped)
    if parse_error:
        return False, f"命令被安全策略拦截: {parse_error}"
    if payload is not None:
        return _check_catastrophic_command(payload, _depth=_depth + 1)
    return True, ""


def check_catastrophic_command(command: str) -> tuple[bool, str]:
    """Public wrapper for the static catastrophic shell-command blocklist."""
    return _check_catastrophic_command(command)


def protected_write_command_reason(command: str, *, _depth: int = 0) -> str:
    """Return a reason when a shell command writes to a protected path.

    Unwraps shell wrappers (bash -c "…") like the catastrophic check so a write
    hidden inside a wrapper is still seen. Empty string means no protected-path
    write was detected.
    """
    stripped = command.strip()
    reason = _protected_write_reason(stripped)
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
        self._sandbox = (
            SandboxValidator(self._workspace_root)
            if self._workspace_root is not None
            else None
        )
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
        matched = self._first_match(tool_name, self._settings.always_deny)
        if matched:
            return False, f"Tool '{tool_name}' is disabled by the static permission policy ({matched})."
        if context is not None:
            matched = self._first_match(tool_name, context.tool_deny_rules)
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
    ) -> PermissionLevel | None:
        """Evaluate parsed Tool(content) rules with deny > ask > allow precedence."""
        from backend.permissions.content_rules import rule_matches_call

        for rule in self._content_deny:
            if rule_matches_call(rule, tool_name, args, effect="deny"):
                return PermissionLevel.ALWAYS_DENY
        for rule in self._content_ask:
            if rule_matches_call(rule, tool_name, args, effect="ask"):
                return PermissionLevel.CONFIRM
        for rule in self._content_allow:
            if rule_matches_call(rule, tool_name, args, effect="allow"):
                return PermissionLevel.AUTO
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
        approval_policy: str = "",
        sandbox_mode: str = "",
        requirements_source: str = "",
    ) -> PermissionContext:
        normalized_mode = normalize_permission_mode_token(mode)
        # The mode already implies an approval policy (bypass never
        # prompt; plan and the rest ask on request). Derive it from the one
        # canonical mapping so the authorizing chokepoint and visible surface agree.
        from backend.config_requirements import permission_mode_requirements

        resolved_approval_policy = str(approval_policy or "").strip() or (
            permission_mode_requirements(normalized_mode)[0]
        )
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
            approval_policy=resolved_approval_policy,
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
        capability_floor = (PermissionLevel.AUTO, "", "")

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
        matched = self._first_match(tool_name, self._settings.always_deny)
        if matched:
            return PermissionLevel.ALWAYS_DENY, "static_policy", matched

        policy_override: tuple[PermissionLevel, str, str] | None = None
        if context is not None:
            matched = self._first_match(tool_name, context.tool_deny_rules)
            if matched:
                return PermissionLevel.ALWAYS_DENY, "context_deny", matched

            override = self._resolve_override_match(tool_name, context.session_overrides)
            if override is not None:
                pattern, level = override
                if level == PermissionLevel.ALWAYS_DENY:
                    return level, "session_override", pattern
                policy_override = (level, "session_override", pattern)

        # Content-level rules (Tool(content) syntax). A deny rule wins in every
        # mode (safety); an allow rule forces AUTO except in plan mode (plan is
        # strict read-only regardless of allow rules).
        content_decision = self._content_rule_decision(tool_name, args)
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
        matched = self._first_match(tool_name, self._settings.require_diff_review)
        if matched:
            static_floor.append((PermissionLevel.DIFF_REVIEW, "static_policy", matched))
        matched = self._first_match(tool_name, self._settings.require_confirm)
        if matched:
            static_floor.append((PermissionLevel.CONFIRM, "static_policy", matched))
        matched = self._first_match(tool_name, self._settings.auto_allow)
        if matched:
            static_auto = (PermissionLevel.AUTO, "static_policy", matched)

        # cc's COMMAND_SUBSTITUTION_PATTERNS make commands carrying injection
        # signals 'ask' instead of silently auto-classified read-only.
        injection_reason = ""
        if args is not None and (
            fnmatch.fnmatch(tool_name, "run_command") or tool_name.startswith("terminal_")
        ):
            shell_command = str(args.get("command") or args.get("cmd") or "")
            injection_reason = command_injection_risk(shell_command)
            if injection_reason:
                static_auto = None
                static_floor.append((PermissionLevel.CONFIRM, "injection_risk", injection_reason))

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
                # Plan mode may strengthen permissions, but it must never
                # weaken a tool-owned interactive floor.  ExitPlanMode is
                # side-effect-free in the workspace taxonomy yet still
                # requires explicit user approval.
                if tool_decision_is_explicit and tool_level in {
                    PermissionLevel.CONFIRM,
                    PermissionLevel.DIFF_REVIEW,
                }:
                    return tool_level, "mode", f"plan:{tool_level.value}"
                return PermissionLevel.AUTO, "mode", "plan:auto"

            # Claude delegates the one Plan-mode write exception to the file
            # tools' path-aware permission seam.  Their explicit AUTO is only
            # returned for the exact session plan path; all other workspace,
            # external, and destructive side effects remain denied.  In
            # particular, a command tool's own CONFIRM declaration is not a
            # Plan-mode exception and must not be downgraded to approval.
            if (
                tool_name in {"edit_file", "write_file"}
                and tool_decision_is_explicit
                and tool_level == PermissionLevel.AUTO
            ):
                return PermissionLevel.AUTO, "mode", "plan:plan_file"
            if side_effect_kind != "none":
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:side_effect"

        # Bypass is MiniCode's explicit unattended/full-access mode.  It
        # removes approval routing for ordinary workspace and external work;
        # otherwise DIFF_REVIEW/CONFIRM would be converted into a denial by
        # approval_policy="never", hiding the capability from the provider and
        # making the user's selected mode unusable.  Hard policy denies and
        # tool-owned refusals have already returned above.  Path containment
        # and sensitive-file checks remain authoritative in evaluate().
        if context is not None and context.mode == "bypass":
            if tool_level == PermissionLevel.ALWAYS_DENY:
                return PermissionLevel.ALWAYS_DENY, "tool", f"{tool_name}.check_permission"
            if tool is not None:
                try:
                    side_effect_kind = str(tool.get_side_effect_kind(args) or "").strip().lower()
                except Exception:
                    return PermissionLevel.ALWAYS_DENY, "tool", f"{tool_name}.side_effect_metadata"
                # Bypass skips ordinary tool-default prompts, as in Claude
                # Code's bypassPermissions branch.  Concrete destructive
                # classification remains a human-confirmation boundary. MCP
                # extensions keep the same boundary for open-world/external
                # calls: bypass removes the generic MCP prompt, but it does
                # not turn an untrusted remote capability into a local AUTO
                # operation. A plain read-only MCP-shaped test tool with no
                # external metadata remains eligible for bypass AUTO.
                if bool(getattr(tool, "destructive", False)) or side_effect_kind == "destructive":
                    return PermissionLevel.CONFIRM, "capability_boundary", "bypass:destructive"
                if (
                    tool_name.startswith("mcp__")
                    and (
                        bool(getattr(tool, "open_world", False))
                        or side_effect_kind == "external"
                    )
                ):
                    return PermissionLevel.CONFIRM, "capability_boundary", "bypass:mcp_external"
            return PermissionLevel.AUTO, "mode", "bypass:auto"

        # A user-authored/session-scoped MCP allow is the only way an extension
        # may skip confirmation. Its own readOnlyHint is never sufficient.
        if (
            tool_name.startswith("mcp__")
            and policy_override is not None
            and policy_override[0] == PermissionLevel.AUTO
            and not bool(getattr(tool, "destructive", False))
            and not bool(getattr(tool, "open_world", False))
        ):
            return policy_override

        # MCP declarations are untrusted extensions in ordinary modes. Their
        # claimed read-only metadata may shape the UI but cannot waive an
        # explicit confirmation. Claude's bypassPermissions branch happens
        # after the tool's own deny/safety hooks and therefore does not invent
        # a second generic MCP confirmation here.
        if tool_name.startswith("mcp__") and not (
            context is not None and context.mode == "bypass"
        ):
            raise_floor(PermissionLevel.CONFIRM, "capability_boundary", "mcp_extension")

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
            )
            if metadata_floor is not None:
                raise_floor(
                    metadata_floor,
                    "tool_capability",
                    f"{tool_name}.runtime_metadata",
                )

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
        sandbox = (
            self._sandbox
            if self._workspace_root == effective_root and self._sandbox is not None
            else SandboxValidator(effective_root)
        )
        return sandbox.validate_file_operation(str(path), operation, content)

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
        if (
            decision == "ask"
            and context is not None
            and context.approval_policy == "never"
            and context.mode != "bypass"
        ):
            level = PermissionLevel.ALWAYS_DENY
            decision = "deny"
            rule_source = "managed_requirements"
            matched_rule = context.requirements_source or "allowed_approval_policies=never"
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
            request_digest=canonical_tool_request_digest(tool_name, args or {}),
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

    def is_path_allowed(self, file_path: str, *, context: PermissionContext | None = None) -> bool:
        """
        检查路径是否在允许范围内。

        规则：
        1. 路径不能匹配 denylist 中的任何模式
        2. 如果 allowlist 非空，路径必须匹配其中至少一个

        Returns:
            True 表示允许访问
        """
        # 检查黑名单
        if windows_path_safety_reason(file_path):
            return False
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
        path_obj = Path(file_path).expanduser()
        resolved_path = (
            path_obj
            if path_obj.is_absolute()
            else resolved_root / path_obj
            if resolved_root is not None
            else path_obj.resolve()
        )
        declared_readable = False
        if context is not None:
            for raw_root in context.filesystem_constraints.get("readable_roots", []):
                try:
                    resolved_path.resolve().relative_to(
                        Path(str(raw_root)).expanduser().resolve()
                    )
                    declared_readable = True
                    break
                except (OSError, ValueError):
                    continue
        if resolved_root is None and not declared_readable:
            return False
        if resolved_root is not None:
            try:
                normalized_path = resolved_path.resolve().relative_to(
                    resolved_root
                ).as_posix()
            except ValueError:
                normalized_path = str(file_path).replace("\\", "/").strip()
        else:
            normalized_path = resolved_path.resolve().as_posix()
        if normalized_path == ".":
            normalized_path = ""
        raw_path = str(file_path).replace("\\", "/").strip()
        while normalized_path.startswith("./"):
            normalized_path = normalized_path[2:]
        if context is not None:
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

        deny_spec = GitIgnoreSpec.from_lines(
            pattern.replace("\\", "/").strip()
            for pattern in path_denylist
            if str(pattern or "").strip()
        )
        candidate = normalized_path or raw_path.lstrip("/")
        if deny_spec.match_file(candidate):
            return False
        if _FILESYSTEM_IS_CASE_INSENSITIVE:
            # NTFS and APFS resolve "Secrets/api.txt" and "secrets/api.txt" to
            # the same file, but gitignore matching is case-sensitive, so a
            # differently-cased path would slip past a denylist entry. cc
            # normalizes case for the same reason (filesystem.ts).
            folded_spec = GitIgnoreSpec.from_lines(
                pattern.replace("\\", "/").strip().lower()
                for pattern in path_denylist
                if str(pattern or "").strip()
            )
            if folded_spec.match_file(candidate.lower()):
                return False

        # 检查白名单（空白名单 = 不限制）
        if context is not None and context.mode == "bypass":
            return True

        if declared_readable:
            return True

        if not path_allowlist:
            return True

        for allow_pattern in path_allowlist:
            normalized = allow_pattern.replace("\\", "/").strip().rstrip("/")
            while normalized.startswith("./"):
                normalized = normalized[2:]
            if normalized in {"", "."}:
                return True
            if not normalized_path and normalized in {"", "."}:
                return True
            if normalized_path == normalized or normalized_path.startswith(normalized + "/"):
                return True
            if fnmatch.fnmatch(normalized_path, normalized + "/*"):
                return True
            if fnmatch.fnmatch(normalized_path, normalized + "/**"):
                return True

        return False

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
                    and not self.is_path_allowed(str(file_path), context=context)
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

        mode = getattr(context, "mode", "confirm") if context is not None else "confirm"
        if declared == PermissionLevel.DIFF_REVIEW and mode != "auto":
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
        tool_name: str, overrides: dict[str, PermissionLevel]
    ) -> PermissionLevel | None:
        for pattern, level in overrides.items():
            if _tool_pattern_matches(tool_name, pattern):
                return level
        return None

    @staticmethod
    def _resolve_override_match(
        tool_name: str,
        overrides: dict[str, PermissionLevel],
    ) -> tuple[str, PermissionLevel] | None:
        for pattern, level in overrides.items():
            if _tool_pattern_matches(tool_name, pattern):
                return pattern, level
        return None

    @staticmethod
    def _first_match(tool_name: str, patterns: list[str]) -> str:
        return next((pattern for pattern in patterns if _tool_pattern_matches(tool_name, pattern)), "")
