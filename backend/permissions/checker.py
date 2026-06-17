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
from pathlib import Path
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from backend.config import PermissionSettings
from backend.permissions.context import PermissionContext
from backend.permissions.network import assess_network_url
from backend.permissions.rules import PermissionRuleMatcher, SandboxValidator
from backend.tools.base import PermissionLevel

if TYPE_CHECKING:
    from backend.tools.base import BaseTool


_ACCEPTS_TOOL_CACHE: WeakKeyDictionary[Any, dict[str, tuple[Any, bool]]] = WeakKeyDictionary()


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
    (re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?/\s*$", re.I), "recursive delete of root filesystem"),
    (re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?/\*", re.I), "recursive delete of root filesystem"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/\s*$", re.I), "recursive delete of root filesystem"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+(?:~|\$HOME|\$\{HOME\}|%USERPROFILE%)(?:[\\/]\*)?\s*$", re.I), "recursive delete of home directory"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/(?:Users|home)/[^/\s]+/?\s*$", re.I), "recursive delete of system directory"),
    (re.compile(r"\brm\b(?:\s+--?[^\s]+)*\s+/(?:etc|usr|var|bin|sbin|System|Library)(?:/|\s*$)", re.I), "recursive delete of system directory"),
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
]


def _check_catastrophic_command(command: str) -> tuple[bool, str]:
    stripped = command.strip()
    for pattern, description in _CATASTROPHIC_PATTERNS:
        if pattern.search(stripped):
            return False, f"命令被安全策略拦截: {description}"
    return True, ""


def check_catastrophic_command(command: str) -> tuple[bool, str]:
    """Public wrapper for the static catastrophic shell-command blocklist."""
    return _check_catastrophic_command(command)


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

def _is_workspace_root_file(normalized_path: str) -> bool:
    return bool(normalized_path) and "/" not in normalized_path and "\\" not in normalized_path


def normalize_permission_mode_token(mode: str | None) -> str:
    normalized = str(mode or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "ask": "confirm",
        "ask_permissions": "confirm",
        "full_access": "bypass",
        "danger_full_access": "bypass",
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

_PLAN_MODE_AUTO_ALLOW = [
    "read_*",
    "list_*",
    "grep_*",
    "glob_*",
    "recall_*",
    "tool_search",
    "go_to_definition",
    "find_references",
    "git_status",
    "git_diff",
    # Network reads allowed — plan mode is "no local mutations", not "no I/O".
    # web_fetch/web_search are read-only from the workspace perspective.
    "web_fetch",
    "web_search",
    "mcp__websearch__fetch_page",
    "mcp__websearch__search",
    "ask_user",
    "read_artifact",
    "task",
]

_PLAN_MODE_DENY = [
    "write_*",
    "edit_*",
    "run_*",
    "save_*",
    "remember_*",
    "terminal_*",
    "load_skill",
    "unload_skill",
]

_AUTO_MODE_ALLOW = [
    *_PLAN_MODE_AUTO_ALLOW,
    "todo_write",
    "task",
    "list_skills",
    "workspace_*",
    "preview.detect",
    "preview.verify",
]

_WRITE_TOOL_PATTERNS = [
    "write_*",
    "edit_*",
    "save_*",
]

_CONFIRM_TOOL_PATTERNS = [
    "run_*",
    "terminal_*",
    "remember_*",
    "load_skill",
    "unload_skill",
    "git_commit",
    "git_push",
    "git_stage_*",
    "git_unstage_*",
    "worktree_*",
    "mcp__*",
]

_READ_ONLY_NETWORK_TOOL_PATTERNS = [
    "web_fetch",
    "web_search",
    "mcp__websearch__fetch_page",
    "mcp__websearch__search",
]

_NETWORK_URL_TOOL_PATTERNS = [
    "web_fetch",
    "browser_*",
    "cdp_*",
    "preview.navigate",
    "preview.verify",
    "mcp__*",
    "mcp__websearch__fetch_page",
    "mcp__*__fetch_page",
]

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
) -> bool:
    if context is not None and context.mode == "bypass":
        return False
    if not _matches_network_url_tool(tool_name):
        return False
    for target in _extract_network_targets(args):
        if not assess_network_url(target, resolve_dns=False).allowed:
            return True
    return False


def _matches_network_url_tool(tool_name: str) -> bool:
    return any(fnmatch.fnmatch(tool_name, pattern) for pattern in _NETWORK_URL_TOOL_PATTERNS)


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

# Shell commands that only read state — safe to auto-allow without confirmation
# (ClaudeCode BashTool.isReadOnly pattern).
_READ_ONLY_SHELL_COMMANDS = frozenset({
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "tree",
    "echo", "which", "whoami", "date", "uname", "hostname", "env", "printenv",
    "find", "grep", "rg", "fd", "diff", "test", "df", "du", "ps", "top",
    "dir", "type", "where", "whereis",
})
# Git/gh/docker subcommands that only read
_READ_ONLY_SUBCOMMANDS = {
    "git": frozenset({"status", "diff", "log", "show", "branch", "remote",
                      "rev-parse", "describe", "blame", "shortlog", "tag",
                      "config", "ls-files", "ls-remote", "for-each-ref"}),
    "gh": frozenset({"pr", "issue", "repo", "api", "run", "release"}),  # mostly read; sub-subcommand may write but rare
    "docker": frozenset({"ps", "images", "logs", "inspect", "version", "info"}),
    "npm": frozenset({"list", "ls", "view", "outdated"}),
    "pip": frozenset({"list", "show", "freeze"}),
    "kubectl": frozenset({"get", "describe", "logs"}),
}


# Shell metacharacters that enable redirection, chaining, or substitution.
# A command containing any of these is no longer a single trivially-read-only
# invocation, so we refuse to auto-classify it (CC rejects the same way before
# its flag-parsing layer runs).
_SHELL_CONTROL_TOKENS = (">", "<", "|", "&", ";", "$(", "${", "`", "\n")


def is_read_only_command(command: str) -> bool:
    """Return True when a shell command only reads state (no side effects).

    Conservative by design: anything with redirection, piping, chaining, or
    command substitution is rejected, as is any command not on the allowlist.
    A false negative just means an extra confirmation prompt; a false positive
    would auto-run a writing command, so we always err toward False.
    """
    cmd = (command or "").strip()
    if not cmd or any(tok in cmd for tok in _SHELL_CONTROL_TOKENS):
        return False
    parts = cmd.split()
    head = parts[0].lower().rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    if head in _READ_ONLY_SHELL_COMMANDS:
        return True
    sub = _READ_ONLY_SUBCOMMANDS.get(head)
    if sub is not None and len(parts) >= 2:
        return parts[1].lower() in sub
    return False


_LOCAL_FILE_TOOL_PATTERNS = [
    "read_file",
    "write_file",
    "edit_file",
    "list_files",
    "grep_files",
    "glob_files",
    "fuzzy_search",
    "go_to_definition",
    "find_references",
]

def _is_bypass_local_file_tool(tool_name: str, context: PermissionContext | None) -> bool:
    return context is not None and context.mode == "bypass" and any(
        fnmatch.fnmatch(tool_name, pattern) for pattern in _LOCAL_FILE_TOOL_PATTERNS
    )


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

    def with_workspace_root(self, workspace_root: Path | str | None) -> "PermissionChecker":
        if workspace_root is None:
            return self
        return PermissionChecker(self._settings, Path(workspace_root))

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
        # 1. 检查是否在 always_deny
        if self._matches(tool_name, self._settings.always_deny):
            return PermissionLevel.ALWAYS_DENY

        if context is not None:
            if self._matches(tool_name, context.tool_deny_rules):
                return PermissionLevel.ALWAYS_DENY

            override = self._resolve_override(tool_name, context.session_overrides)
            if override is not None:
                return override

            if context.mode == "confirm" and self._matches(tool_name, _READ_ONLY_NETWORK_TOOL_PATTERNS):
                return PermissionLevel.CONFIRM
            if context.mode == "auto" and _network_target_requires_confirmation(tool_name, args, context):
                return PermissionLevel.CONFIRM

            # Tool-owned permission decision (CC's checkPermissions analogue).
            # Honored in every mode except plan/auto/bypass. Plan denies local
            # mutations outright; auto has product-level semantics below
            # (workspace edits are allowed without prompting), and bypass must
            # not be narrowed by a tool's conservative default metadata.
            if tool is not None and context.mode not in {"plan", "auto", "bypass"}:
                tool_level = self._consult_tool(tool, args, context)
                if tool_level is not None:
                    return tool_level

            # Compute mode-level permission first
            mode_level: PermissionLevel | None = None
            if context.mode == "bypass":
                mode_level = PermissionLevel.AUTO
            elif context.mode == "auto":
                if self._matches(tool_name, _WRITE_TOOL_PATTERNS):
                    mode_level = PermissionLevel.AUTO
                elif _network_target_requires_confirmation(tool_name, args, context):
                    mode_level = PermissionLevel.CONFIRM
                elif tool is not None and self._tool_invocation_is_read_only(tool, args):
                    mode_level = PermissionLevel.AUTO
                elif self._matches(tool_name, _READ_ONLY_NETWORK_TOOL_PATTERNS):
                    mode_level = PermissionLevel.AUTO
                elif self._matches(tool_name, _CONFIRM_TOOL_PATTERNS):
                    mode_level = PermissionLevel.CONFIRM
                elif self._matches(tool_name, _AUTO_MODE_ALLOW):
                    mode_level = PermissionLevel.AUTO
                else:
                    mode_level = PermissionLevel.CONFIRM
            elif context.mode == "accept_edits":
                if self._matches(tool_name, _WRITE_TOOL_PATTERNS):
                    mode_level = PermissionLevel.AUTO
                elif self._matches(tool_name, _READ_ONLY_NETWORK_TOOL_PATTERNS):
                    mode_level = PermissionLevel.AUTO
                elif self._matches(tool_name, _CONFIRM_TOOL_PATTERNS):
                    mode_level = PermissionLevel.CONFIRM
                else:
                    mode_level = PermissionLevel.AUTO
            elif context.mode == "plan":
                if self._matches(tool_name, _PLAN_MODE_DENY):
                    mode_level = PermissionLevel.ALWAYS_DENY
                elif self._matches(tool_name, _PLAN_MODE_AUTO_ALLOW):
                    mode_level = PermissionLevel.AUTO
                else:
                    mode_level = PermissionLevel.ALWAYS_DENY
            elif context.mode == "confirm":
                if self._matches(tool_name, self._settings.require_diff_review):
                    mode_level = PermissionLevel.DIFF_REVIEW
                elif self._matches(tool_name, _READ_ONLY_NETWORK_TOOL_PATTERNS):
                    mode_level = PermissionLevel.CONFIRM
                elif self._matches(tool_name, _PLAN_MODE_AUTO_ALLOW):
                    mode_level = PermissionLevel.AUTO
                else:
                    mode_level = PermissionLevel.CONFIRM

            if mode_level is not None:
                return mode_level

        # 2. 检查是否需要 diff review
        if self._matches(tool_name, _READ_ONLY_NETWORK_TOOL_PATTERNS):
            return PermissionLevel.AUTO

        if self._matches(tool_name, self._settings.require_diff_review):
            return PermissionLevel.DIFF_REVIEW

        # 3. 检查是否需要 confirm
        if self._matches(tool_name, self._settings.require_confirm):
            if tool is not None:
                tool_level = self._consult_tool(tool, args, context)
                if tool_level is not None:
                    return tool_level
            return PermissionLevel.CONFIRM

        # 4. 检查是否 auto allow
        if self._matches(tool_name, self._settings.auto_allow):
            return PermissionLevel.AUTO

        # 5. 默认：需要确认
        return PermissionLevel.CONFIRM

    def validate_file_operation(
        self,
        file_path: str,
        operation: str,
        content: str | None = None,
    ) -> tuple[bool, str]:
        if operation not in {"read", "write", "execute"}:
            return False, f"Unsupported file operation: {operation}"
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            path = self._workspace_root / path
        return self._sandbox.validate_file_operation(str(path), operation, content)

    def validate_command(self, command: str) -> tuple[bool, str]:
        return _check_catastrophic_command(command)

    def _is_allowed_workspace_root_file_read(
        self,
        tool_name: str,
        file_path: str,
        *,
        context: PermissionContext | None = None,
    ) -> bool:
        if tool_name != "read_file":
            return False
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
        return _is_workspace_root_file(normalized_path) and resolved_path.is_file()

    def _is_allowed_workspace_root_directory_discovery(
        self,
        tool_name: str,
        directory: str,
        *,
        context: PermissionContext | None = None,
    ) -> bool:
        """Allow safe top-level workspace discovery under a narrow allowlist.

        A model may pass the absolute workspace root instead of ".". That
        should behave like list_files(".") so the agent can discover the
        allowed project directories before drilling into them. Keep this narrow
        to filename/directory discovery tools; content search still needs an
        explicitly allowed subtree.
        """
        if tool_name not in {"list_files", "glob_files"}:
            return False
        path_obj = Path(directory or ".").expanduser()
        resolved_path = path_obj if path_obj.is_absolute() else self._workspace_root / path_obj
        try:
            normalized_path = resolved_path.resolve().relative_to(self._workspace_root).as_posix()
        except ValueError:
            return False
        return normalized_path in {"", "."} and resolved_path.is_dir()

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

        for deny_pattern in path_denylist:
            normalized_deny = deny_pattern.replace("\\", "/").strip()
            while normalized_deny.startswith("./"):
                normalized_deny = normalized_deny[2:]
            if fnmatch.fnmatch(raw_path, deny_pattern) or fnmatch.fnmatch(normalized_path, normalized_deny):
                return False
            # 也检查文件名
            if fnmatch.fnmatch(Path(file_path).name, deny_pattern):
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
        level = self.check(tool_name, args, context=context, tool=tool)

        if level == PermissionLevel.ALWAYS_DENY:
            return f"工具 '{tool_name}' 被管理员禁止使用。"

        # Path checks only apply to first-party local filesystem tools. Other
        # tools may legitimately use fields named "path" for remote resources.
        if args:
            if self._matches(tool_name, _LOCAL_FILE_TOOL_PATTERNS):
                if _is_bypass_local_file_tool(tool_name, context):
                    return None
                file_path = args.get("file_path") or args.get("directory") or args.get("path")
                if tool_name == "list_files" and str(file_path or "").strip() in {"", "."}:
                    file_path = None
                if (
                    file_path
                    and not self.is_path_allowed(file_path, context=context)
                    and not self._is_allowed_workspace_root_file_read(tool_name, str(file_path), context=context)
                    and not self._is_allowed_workspace_root_directory_discovery(tool_name, str(file_path), context=context)
                ):
                    return (
                        f"路径 '{file_path}' 不在允许范围内。"
                        f"允许的路径: {self._settings.path_allowlist}。"
                        f"禁止的路径模式: {self._settings.path_denylist}。"
                    )

                if file_path:
                    operation = "write" if self._matches(tool_name, _WRITE_TOOL_PATTERNS) else "read"
                    allowed, reason = self.validate_file_operation(str(file_path), operation)
                    if not allowed:
                        return reason
        return None

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
    def _consult_tool(
        tool: "BaseTool",
        args: dict[str, Any] | None,
        context: PermissionContext | None,
    ) -> PermissionLevel | None:
        """Ask a tool to decide its own permission for this invocation.

        Returns an explicit level from ``check_permission``, or ``AUTO`` when
        the tool classifies the invocation as read-only. ``None`` means defer
        to the centralized policy. Tool failures fall through to ``None`` so a
        buggy hook can never silently widen access.
        """
        try:
            decided = tool.check_permission(args, context)
            if decided is not None:
                return decided
            if tool.is_read_only(args):
                return PermissionLevel.AUTO
        except Exception:  # pragma: no cover - defensive; never widen on error
            return None
        return None

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
    def _resolve_override(
        tool_name: str, overrides: dict[str, PermissionLevel]
    ) -> PermissionLevel | None:
        for pattern, level in overrides.items():
            if fnmatch.fnmatch(tool_name, pattern):
                return level
        return None

    @staticmethod
    def _matches(tool_name: str, patterns: list[str]) -> bool:
        """检查工具名是否匹配模式列表中的任何一个。"""
        for pattern in patterns:
            if fnmatch.fnmatch(tool_name, pattern):
                return True
        return False
