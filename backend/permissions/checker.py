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
from pathlib import Path
from typing import Any

from backend.config import PermissionSettings
from backend.tools.base import PermissionLevel


class PermissionChecker:
    """
    工具调用权限检查。

    职责：
    1. 根据工具名 → 判定权限级别
    2. 根据路径参数 → 检查白名单/黑名单
    """

    def __init__(self, settings: PermissionSettings) -> None:
        self._settings = settings

    def check(self, tool_name: str, args: dict[str, Any] | None = None) -> PermissionLevel:
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

        # 2. 检查是否需要 diff review
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

    def is_path_allowed(self, file_path: str) -> bool:
        """
        检查路径是否在允许范围内。

        规则：
        1. 路径不能匹配 denylist 中的任何模式
        2. 如果 allowlist 非空，路径必须匹配其中至少一个

        Returns:
            True 表示允许访问
        """
        # 检查黑名单
        for deny_pattern in self._settings.path_denylist:
            if fnmatch.fnmatch(file_path, deny_pattern):
                return False
            # 也检查文件名
            if fnmatch.fnmatch(Path(file_path).name, deny_pattern):
                return False

        # 检查白名单（空白名单 = 不限制）
        if not self._settings.path_allowlist:
            return True

        for allow_pattern in self._settings.path_allowlist:
            if file_path.startswith(allow_pattern):
                return True
            if fnmatch.fnmatch(file_path, allow_pattern + "/*"):
                return True
            if fnmatch.fnmatch(file_path, allow_pattern + "/**"):
                return True

        return False

    def get_denial_reason(self, tool_name: str, args: dict[str, Any] | None = None) -> str | None:
        """
        获取拒绝执行的原因（如果有）。

        用于向 Agent 提供可操作的反馈信息。
        """
        level = self.check(tool_name, args)

        if level == PermissionLevel.ALWAYS_DENY:
            return f"工具 '{tool_name}' 被管理员禁止使用。"

        # 路径检查
        if args:
            file_path = args.get("file_path") or args.get("directory") or args.get("path")
            if file_path and not self.is_path_allowed(file_path):
                return (
                    f"路径 '{file_path}' 不在允许范围内。"
                    f"允许的路径: {self._settings.path_allowlist}。"
                    f"禁止的路径模式: {self._settings.path_denylist}。"
                )

        return None

    @staticmethod
    def _matches(tool_name: str, patterns: list[str]) -> bool:
        """检查工具名是否匹配模式列表中的任何一个。"""
        for pattern in patterns:
            if fnmatch.fnmatch(tool_name, pattern):
                return True
        return False
