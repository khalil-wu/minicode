"""
Git Worktree 工具（参考 Claude Code 的 worktree 支持）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.workspace.worktree import get_global_worktree_manager


class ListWorktreesTool(BaseTool):
    """列出所有 Git worktree"""

    name = "list_worktrees"
    read_only = True
    description = (
        "列出当前 Git 仓库的所有 worktree。"
        "Worktree 允许在同一个仓库中同时检出多个分支。"
    )
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {},
                "required": [],
            },
            strict=True,
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        manager = get_global_worktree_manager()

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        worktrees = manager.list_worktrees()

        if not worktrees:
            return self._success_result("没有找到 worktree")

        lines = [f"找到 {len(worktrees)} 个 worktree:\n"]

        for i, wt in enumerate(worktrees, 1):
            status = []
            if wt.is_bare:
                status.append("bare")
            if wt.is_detached:
                status.append("detached")

            status_str = f" [{', '.join(status)}]" if status else ""
            branch_info = wt.branch if wt.branch else f"detached at {wt.commit[:8]}"

            lines.append(f"{i}. {wt.path}")
            lines.append(f"   Branch: {branch_info}{status_str}")
            lines.append(f"   Commit: {wt.commit[:8]}")

        result = "\n".join(lines)
        return self._success_result(result)


class CreateWorktreeTool(BaseTool):
    """创建新的 Git worktree"""

    name = "create_worktree"
    description = (
        "创建新的 Git worktree。"
        "Worktree 允许在独立的目录中检出不同的分支，"
        "适用于并行开发、测试、代码审查等场景。"
    )
    permission = PermissionLevel.CONFIRM

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Worktree 路径（相对或绝对路径）",
                    },
                    "branch": {
                        "type": "string",
                        "description": "分支名（可选）",
                    },
                    "new_branch": {
                        "type": "boolean",
                        "description": "是否创建新分支（默认 false）",
                        "default": False,
                    },
                    "commit": {
                        "type": "string",
                        "description": "基于的提交（可选，默认为 HEAD）",
                    },
                },
                "required": ["path"],
            },
            strict=True,
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        path_str = args.get("path", "")
        branch = args.get("branch")
        new_branch = args.get("new_branch", False)
        commit = args.get("commit")

        if not path_str:
            return self._error_result("缺少 path 参数")

        manager = get_global_worktree_manager()

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        path = Path(path_str)

        success = manager.create_worktree(
            path=path,
            branch=branch,
            new_branch=new_branch,
            commit=commit,
        )

        if success:
            branch_info = f"分支 {branch}" if branch else f"提交 {commit or 'HEAD'}"
            return self._success_result(
                f"已创建 worktree: {path}\n基于: {branch_info}"
            )
        else:
            return self._error_result(f"创建 worktree 失败: {path}")


class RemoveWorktreeTool(BaseTool):
    """删除 Git worktree"""

    name = "remove_worktree"
    description = (
        "删除指定的 Git worktree。"
        "注意: 如果 worktree 中有未提交的更改，需要使用 force=true 强制删除。"
    )
    permission = PermissionLevel.CONFIRM

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Worktree 路径",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "是否强制删除（即使有未提交的更改，默认 false）",
                        "default": False,
                    },
                },
                "required": ["path"],
            },
            strict=True,
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        path_str = args.get("path", "")
        force = args.get("force", False)

        if not path_str:
            return self._error_result("缺少 path 参数")

        manager = get_global_worktree_manager()

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        path = Path(path_str)

        success = manager.remove_worktree(path=path, force=force)

        if success:
            return self._success_result(f"已删除 worktree: {path}")
        else:
            return self._error_result(
                f"删除 worktree 失败: {path}\n"
                "提示: 如果有未提交的更改，请使用 force=true"
            )
