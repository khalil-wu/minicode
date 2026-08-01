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
import re
import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from weakref import WeakKeyDictionary

from pathspec.gitignore import GitIgnoreSpec

from backend.config import PermissionSettings
from backend.permissions.context import PermissionContext, PermissionDecision
from backend.permissions.network import assess_network_url
from backend.permissions.rules import PermissionRuleMatcher, SandboxValidator
from backend.security.sensitive_files import (
    PROTECTED_WRITE_FILE_NAMES,
    PROTECTED_WRITE_PATH_PARTS,
)
from backend.tools.base import PermissionLevel, WORKSPACE_PATH_SCHEMA_FIELDS

if TYPE_CHECKING:
    from backend.tools.base import BaseTool


_ACCEPTS_TOOL_CACHE: WeakKeyDictionary[Any, dict[str, tuple[Any, bool]]] = WeakKeyDictionary()

# Windows and macOS resolve paths case-insensitively, so a denylist entry must
# match regardless of the casing the model happens to use.
_FILESYSTEM_IS_CASE_INSENSITIVE = sys.platform in {"win32", "darwin"}
_LEGACY_TOOL_NAME_ALIASES = {
    "terminal.exec": "run_command",
    "terminal_exec": "run_command",
}


def _tool_pattern_matches(tool_name: str, pattern: str) -> bool:
    """Match ordinary globs plus an MCP server-level rule.

    `mcp__server` is a deliberate server boundary, not a literal tool named
    server: it covers `mcp__server__*` without making similarly named servers
    match by raw prefix.
    """
    normalized_pattern = str(pattern or "").strip()
    normalized_pattern = _LEGACY_TOOL_NAME_ALIASES.get(
        normalized_pattern.casefold(),
        normalized_pattern,
    )
    tool_name = _LEGACY_TOOL_NAME_ALIASES.get(tool_name.casefold(), tool_name)
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
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/+(?:Users|home)/[^/\s]+/?\s*$", re.I), "recursive delete of system directory"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/+(?:etc|usr|var|bin|sbin|System|Library)(?:/|\s*$)", re.I), "recursive delete of system directory"),
    (re.compile(r"\bmkfs\b", re.I), "filesystem format"),
    (re.compile(r"\bdd\b.*\bof\s*=\s*/dev/", re.I), "raw disk write"),
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
    # Environment-variable exfiltration via /proc (parser-differential defense in
    # depth; path validation may not cover a bare `cat`). No legitimate dev use.
    (re.compile(r"/proc/[^/\s]+/environ", re.I), "read of process environment (secret exfiltration)"),
    (
        re.compile(r"\bzmodload\b(?=[^\n;&|]*\bzsh/(?:system|net/socket|files|zftp)\b)", re.I),
        "load of a Zsh module that exposes raw system, network, or filesystem access",
    ),
]


# ── Parser-differential / command-injection risk signals ─────────────────────
# Mirrors CC bashSecurity.ts COMMAND_SUBSTITUTION_PATTERNS. These are RISK
# SIGNALS, not outright blocks: a command carrying one is never silently
# auto-classified as read-only (so it always requires confirmation), but common
# legitimate uses like `echo $(date)` remain runnable after the user approves.
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
# stop a shell command from writing protected files (.claude/**, .git/**,
# settings.json, .mcp.json, …) that the file tools (write_file/edit_file/
# apply_patch) already block. cc blocks the same class of shell writes
# (bashPermissions cd+redirect / cd+mv into .claude/settings.json); this is the
# proportionate equivalent reusing the existing segment splitter — the OS
# sandbox + CONFIRM remain the primary boundary, this closes the file-tool vs
# shell asymmetry for the protected set.
_SHELL_WRITE_REDIRECT_RE = re.compile(r">>?\s*([^\s;&|]+)")
_SHELL_WRITE_COMMAND_RE = re.compile(
    r"\b(?:mv|cp|tee|install|ln)\b\s+(.+)", re.IGNORECASE
)


def _path_is_protected_write(raw_path: str) -> bool:
    cleaned = raw_path.strip().strip("'\"").replace("\\", "/").strip()
    if not cleaned:
        return False
    name = cleaned.rsplit("/", 1)[-1].lower()
    if name in PROTECTED_WRITE_FILE_NAMES:
        return True
    parts = [segment for segment in cleaned.split("/") if segment]
    return any(part.lower() in PROTECTED_WRITE_PATH_PARTS for part in parts)


def _protected_write_reason(command: str) -> str:
    for segment in _split_shell_compound(command):
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
    evaluate = getattr(checker, "evaluate", None)
    if callable(evaluate):
        return evaluate(tool_name, args, context=context, tool=tool)
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
    )

def normalize_permission_mode_token(mode: str | None) -> str:
    normalized = str(mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ask": "confirm",
        "ask_permissions": "confirm",
        "bypass_permissions": "bypass",
        "full_access": "bypass",
        "fullaccess": "bypass",
        "danger_full_access": "bypass",
        "dangerfullaccess": "bypass",
        "acceptedits": "accept_edits",
    }
    candidate = aliases.get(normalized, normalized)
    if candidate in {"default", "plan", "confirm", "bypass", "auto", "accept_edits"}:
        return candidate
    return "default"


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


def _bypass_denylist(configured: list[str]) -> list[str]:
    """Keep built-in secret/repo guards in bypass, skip custom workspace policy.

    The built-in guards are unconditional: bypass waives the user's own workspace
    policy, not the secret/repo floor. Previously a single missing default turned
    this into an empty list, dropping .env/.git/secrets protection entirely —
    cc's equivalent safety check is likewise immune to bypass.
    """
    del configured
    return list(_DEFAULT_PATH_DENYLIST)


class PermissionChecker:
    """
    工具调用权限检查。

    职责：
    1. 根据工具名 → 判定权限级别
    2. 根据路径参数 → 检查白名单/黑名单
    """

    def __init__(self, settings: PermissionSettings, workspace_root: Path | None = None) -> None:
        self._settings = settings
        self._workspace_root = (workspace_root or Path.cwd()).resolve()
        self._sandbox = SandboxValidator(self._workspace_root)
        self._rule_matcher = PermissionRuleMatcher(self._workspace_root)
        # Pre-parse content rules (Tool(content) syntax) once.
        from backend.permissions.content_rules import parse_content_rules

        self._content_allow = parse_content_rules(list(getattr(settings, "content_allow_rules", [])))
        self._content_deny = parse_content_rules(list(getattr(settings, "content_deny_rules", [])))

    def with_workspace_root(self, workspace_root: Path | str | None) -> "PermissionChecker":
        if workspace_root is None:
            return self
        return PermissionChecker(self._settings, Path(workspace_root))

    def _content_rule_decision(
        self,
        tool_name: str,
        args: dict[str, Any] | None,
    ) -> PermissionLevel | None:
        """Evaluate parsed Tool(content) rules. Returns ALWAYS_DENY, AUTO, or None."""
        from backend.permissions.content_rules import rule_matches_call

        for rule in self._content_deny:
            if rule_matches_call(rule, tool_name, args):
                return PermissionLevel.ALWAYS_DENY
        for rule in self._content_allow:
            if rule_matches_call(rule, tool_name, args):
                return PermissionLevel.AUTO
        return None

    def build_context(
        self,
        *,
        mode: str = "default",
        session_overrides: dict[str, PermissionLevel] | None = None,
        tool_deny_rules: list[str] | None = None,
        filesystem_constraints: dict[str, list[str]] | None = None,
        workspace_scope: str = "project",
        source: str = "runtime",
    ) -> PermissionContext:
        normalized_mode = normalize_permission_mode_token(mode)
        return PermissionContext(
            mode=normalized_mode,  # type: ignore[arg-type]
            session_overrides=dict(session_overrides or {}),
            tool_deny_rules=list(tool_deny_rules or []),
            filesystem_constraints=dict(filesystem_constraints or {}),
            workspace_scope=workspace_scope if workspace_scope in {"computer", "project", "worktree"} else "project",
            source=source,
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
        if policy_override is None and content_decision == PermissionLevel.AUTO:
            policy_override = (PermissionLevel.AUTO, "content_rule", "content_allow")

        if context is not None and context.mode == "bypass":
            return PermissionLevel.AUTO, "mode", "bypass:auto"

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

        if context is not None and context.mode == "plan":
            if tool is None or tool_level != PermissionLevel.AUTO:
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:deny"
            try:
                side_effect_kind = str(tool.get_side_effect_kind(args) or "").strip().lower()
            except Exception:
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:metadata_error"
            if side_effect_kind != "none":
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:side_effect"
            if tool_name.startswith("mcp__"):
                return PermissionLevel.ALWAYS_DENY, "mode", "plan:untrusted_mcp"
            return PermissionLevel.AUTO, "mode", "plan:auto"

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

        # MCP declarations are untrusted extensions.  Their claimed read-only
        # metadata may shape the UI but cannot waive an explicit confirmation.
        if tool_name.startswith("mcp__"):
            raise_floor(PermissionLevel.CONFIRM, "capability_boundary", "mcp_extension")

        # CC asks whenever a filesystem-shaped tool call has no getPath seam.
        # A forgotten declaration must not silently inherit AUTO in MiniCode.
        if tool is not None and _has_undeclared_path_argument(tool, args):
            raise_floor(
                PermissionLevel.CONFIRM,
                "capability_boundary",
                f"{tool_name}.undeclared_path",
            )

        if context is not None and _network_target_requires_confirmation(
            tool_name,
            args,
            context,
            tool,
        ):
            raise_floor(PermissionLevel.CONFIRM, "capability_boundary", "network_target")

        # Tool-owned checks define an invocation-specific capability floor.
        # A content allow or remembered ordinary approval cannot lower it.
        if tool is not None:
            if tool_level is not None and tool_decision_is_explicit:
                raise_floor(tool_level, "tool", f"{tool_name}.check_permission")
            metadata_floor = self._tool_capability_floor(tool, args, context)
            if metadata_floor is not None:
                raise_floor(
                    metadata_floor,
                    "tool_capability",
                    f"{tool_name}.runtime_metadata",
                )

        if policy_override is not None:
            return apply_floor(*policy_override)

        # Static rules are the default routing layer. Explicit content/session
        # allows above are more specific; capability boundaries still win.
        for floor in static_floor:
            raise_floor(*floor)

        if static_auto is not None:
            return apply_floor(*static_auto)

        if context is not None:
            mode_level: PermissionLevel | None = None
            if context.mode in {"auto", "accept_edits"}:
                mode_level = tool_level or PermissionLevel.CONFIRM
                if (
                    context.mode == "accept_edits"
                    and mode_level == PermissionLevel.DIFF_REVIEW
                    and tool is not None
                ):
                    try:
                        if tool.get_side_effect_kind(args) == "workspace":
                            mode_level = PermissionLevel.AUTO
                    except Exception:
                        pass
            elif context.mode == "confirm":
                mode_level = tool_level or PermissionLevel.CONFIRM

            if mode_level is not None:
                if (
                    context.mode == "accept_edits"
                    and mode_level == PermissionLevel.AUTO
                    and capability_floor[0] == PermissionLevel.DIFF_REVIEW
                ):
                    # This is the explicit product meaning of accept_edits:
                    # workspace edits skip the review dialog, while confirm
                    # and destructive/network floors remain effective.
                    return PermissionLevel.AUTO, "mode", "accept_edits:auto"
                return apply_floor(mode_level, "mode", f"{context.mode}:{mode_level.value}")

        if tool_level is not None:
            return apply_floor(tool_level, "tool", f"{tool_name}.permission")

        return apply_floor(PermissionLevel.CONFIRM, "default", "confirm")

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
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = self._workspace_root / path
        if operation == "read":
            from backend.agent.tool_result_persistence import is_tool_result_path

            if is_tool_result_path(path):
                return True, ""
            constraints = context.filesystem_constraints if context is not None else {}
            for raw_root in constraints.get("readable_roots", []):
                try:
                    path.resolve().relative_to(Path(str(raw_root)).expanduser().resolve())
                    return True, ""
                except (OSError, ValueError):
                    continue
        return self._sandbox.validate_file_operation(str(path), operation, content)

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
        )

    def validate_command(self, command: str) -> tuple[bool, str]:
        return _check_catastrophic_command(command)

    def _is_allowed_workspace_root_path(
        self,
        file_path: str,
        *,
        context: PermissionContext | None = None,
    ) -> bool:
        path_obj = Path(file_path).expanduser()
        resolved_path = path_obj if path_obj.is_absolute() else self._workspace_root / path_obj
        try:
            normalized_path = resolved_path.resolve().relative_to(self._workspace_root).as_posix()
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
        path_denylist = list(self._settings.path_denylist)
        path_allowlist = list(self._settings.path_allowlist)
        path_obj = Path(file_path).expanduser()
        resolved_path = path_obj if path_obj.is_absolute() else self._workspace_root / path_obj
        try:
            normalized_path = resolved_path.resolve().relative_to(self._workspace_root).as_posix()
        except ValueError:
            normalized_path = str(file_path).replace("\\", "/").strip()
        if normalized_path == ".":
            normalized_path = ""
        raw_path = str(file_path).replace("\\", "/").strip()
        while normalized_path.startswith("./"):
            normalized_path = normalized_path[2:]
        if context is not None:
            if "denylist" in context.filesystem_constraints:
                path_denylist = list(context.filesystem_constraints["denylist"])
            if "allowlist" in context.filesystem_constraints:
                path_allowlist = list(context.filesystem_constraints["allowlist"])
            if context.mode == "bypass":
                path_denylist = _bypass_denylist(path_denylist)

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
                workspace_paths = []
            for file_path in workspace_paths:
                if str(file_path or "").strip() in {"", "."}:
                    continue
                tool_result_read = False
                if bool(getattr(tool, "allow_tool_result_path", False)):
                    from backend.agent.tool_result_persistence import is_tool_result_path

                    tool_result_read = is_tool_result_path(str(file_path))
                root_path_allowed = bool(
                    getattr(tool, "allow_workspace_root_path", False)
                    and self._is_allowed_workspace_root_path(str(file_path), context=context)
                )
                if (
                    not tool_result_read
                    and not self.is_path_allowed(str(file_path), context=context)
                    and not root_path_allowed
                ):
                    return (
                        f"路径 '{file_path}' 不在允许范围内。"
                        f"允许的路径: {self._settings.path_allowlist}。"
                        f"禁止的路径模式: {self._settings.path_denylist}。"
                    )

                if not tool_result_read and not (context is not None and context.mode == "bypass"):
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
        except Exception:  # pragma: no cover - metadata errors use declared policy
            return declared, False
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

        Bypass and plan modes are resolved before this helper.  Metadata or
        classification failures fail closed only to the tool's declared
        permission; they never silently turn a mutating call into ``AUTO``.
        """
        declared = getattr(tool, "permission", PermissionLevel.AUTO)
        if not isinstance(declared, PermissionLevel):
            declared = PermissionLevel.AUTO

        if declared == PermissionLevel.ALWAYS_DENY:
            return PermissionLevel.ALWAYS_DENY

        mode = getattr(context, "mode", "default") if context is not None else "default"
        if declared == PermissionLevel.DIFF_REVIEW and mode != "accept_edits":
            return PermissionLevel.DIFF_REVIEW

        try:
            side_effect_kind = str(tool.get_side_effect_kind(args) or "").strip().lower()
        except Exception:  # pragma: no cover - defensive metadata fallback
            side_effect_kind = ""
        destructive = bool(getattr(tool, "destructive", False)) or side_effect_kind == "destructive"
        if destructive:
            return (
                declared
                if _PERMISSION_RESTRICTIVENESS[declared]
                > _PERMISSION_RESTRICTIVENESS[PermissionLevel.CONFIRM]
                else PermissionLevel.CONFIRM
            )

        if bool(getattr(tool, "open_world", False)) and declared != PermissionLevel.AUTO:
            try:
                invocation_is_read_only = bool(tool.is_read_only(args))
            except Exception:  # pragma: no cover - unknown open-world calls require review
                invocation_is_read_only = False
            if not invocation_is_read_only:
                return PermissionLevel.CONFIRM

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
