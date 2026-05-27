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
import re
from pathlib import Path
from typing import Any

from backend.config import PermissionSettings
from backend.permissions.context import PermissionContext
from backend.permissions.rules import PermissionRuleMatcher, SandboxValidator
from backend.tools.base import PermissionLevel


# ── Catastrophic command blocklist ──────────────────────────────────────────

_CATASTROPHIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?/\s*$", re.I), "recursive delete of root filesystem"),
    (re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?/\*", re.I), "recursive delete of root filesystem"),
    (re.compile(r"\bmkfs\b", re.I), "filesystem format"),
    (re.compile(r"\bdd\b.*\bof\s*=\s*/dev/", re.I), "raw disk write"),
    (re.compile(r":\(\)\s*\{.*\|.*&", re.I), "fork bomb"),
    (re.compile(r">\s*/dev/sd", re.I), "raw disk overwrite"),
    (re.compile(r"curl\b.*\|\s*(ba)?sh", re.I), "pipe remote script to shell"),
    (re.compile(r"wget\b.*\|\s*(ba)?sh", re.I), "pipe remote script to shell"),
    (re.compile(r"Remove-Item\s.*-Recurse.*[A-Z]:\\\s*$", re.I), "recursive delete of drive root"),
    (re.compile(r"Remove-Item\s.*[A-Z]:\\\s*.*-Recurse", re.I), "recursive delete of drive root"),
    (re.compile(r"\bformat\s+[A-Z]:", re.I), "drive format"),
    (re.compile(r"del\s+/[sS].*\\Windows", re.I), "delete Windows system directory"),
    (re.compile(r"rd\s+/[sS]\s+/[qQ]\s+[A-Z]:\\$", re.I), "recursive delete of drive root"),
]


def _check_catastrophic_command(command: str) -> tuple[bool, str]:
    stripped = command.strip()
    for pattern, description in _CATASTROPHIC_PATTERNS:
        if pattern.search(stripped):
            return False, f"命令被安全策略拦截: {description}"
    return True, ""

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
        source: str = "runtime",
    ) -> PermissionContext:
        return PermissionContext(
            mode=mode if mode in {"default", "plan", "confirm", "bypass", "auto", "accept_edits"} else "default",
            session_overrides=dict(session_overrides or {}),
            tool_deny_rules=list(tool_deny_rules or []),
            filesystem_constraints=dict(filesystem_constraints or {}),
            source=source,
        )

    def check(
        self,
        tool_name: str,
        args: dict[str, Any] | None = None,
        *,
        context: PermissionContext | None = None,
    ) -> PermissionLevel:
        """
        判定工具调用的权限级别。

        Args:
            tool_name: 工具名称（支持 mcp__server__tool 格式）
            args: 工具参数（用于路径检查）

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

            # Compute mode-level permission first
            mode_level: PermissionLevel | None = None
            if context.mode == "bypass":
                mode_level = PermissionLevel.AUTO
            elif context.mode == "auto":
                if self._matches(tool_name, _WRITE_TOOL_PATTERNS):
                    mode_level = PermissionLevel.DIFF_REVIEW
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
        raw_path = str(file_path).replace("\\", "/").strip()
        normalized_path = raw_path
        path_obj = Path(file_path).expanduser()
        if path_obj.is_absolute():
            try:
                normalized_path = path_obj.resolve().relative_to(self._workspace_root).as_posix()
            except ValueError:
                normalized_path = raw_path
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
    ) -> str | None:
        """
        获取拒绝执行的原因（如果有）。

        用于向 Agent 提供可操作的反馈信息。
        """
        level = self.check(tool_name, args, context=context)

        if level == PermissionLevel.ALWAYS_DENY:
            return f"工具 '{tool_name}' 被管理员禁止使用。"

        # Path checks only apply to first-party local filesystem tools. Other
        # tools may legitimately use fields named "path" for remote resources.
        if args:
            if self._matches(tool_name, _LOCAL_FILE_TOOL_PATTERNS):
                file_path = args.get("file_path") or args.get("directory") or args.get("path")
                if file_path and not self.is_path_allowed(file_path, context=context):
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
