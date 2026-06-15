"""
Git 工具集（参考 Claude Code 的 Git 集成）。

提供常用 Git 操作：
  - git_status: 查看工作区状态
  - git_diff: 查看文件差异
  - git_log: 查看提交历史
  - git_commit: 创建提交
  - git_branch: 分支管理
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

logger = logging.getLogger(__name__)


def _workspace_root(context: Any, fallback: Path) -> Path:
    if context and getattr(context, "workspace_root", None):
        return Path(context.workspace_root).resolve()
    return fallback.resolve()


def _resolve_work_dir(root: Path, path_value: Any) -> Path:
    raw = str(path_value or ".").strip() or "."
    path = Path(raw)
    if path.is_absolute():
        return path.resolve()
    return (root / path).resolve()


class GitStatusTool(BaseTool):
    """
    查看 Git 工作区状态。

    显示修改、新增、删除的文件。
    权限: AUTO（只读操作）
    """

    name = "git_status"
    read_only = True
    description = (
        "Show the Git working tree status: modified, added, deleted, and untracked files. "
        "Use this before commits to review what changed, or at the start of a task to understand the current state. "
        "Returns a structured list of changed files with their status (M/A/D/??). "
        "For actual diffs, use git_diff instead."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root or Path.cwd()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "工作目录（可选，默认工作区根目录）",
                    },
                },
                "required": [],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        path_str = args.get("path", ".")
        root = _workspace_root(context, self._workspace_root)
        work_dir = _resolve_work_dir(root, path_str)

        if not work_dir.exists():
            return self._error_result(f"路径不存在: {path_str} (workspace: {root})")

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--short",
                "--branch",
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                if "not a git repository" in error_msg.lower():
                    return self._success_result(f"{path_str} 不是 Git 仓库；跳过 Git 状态检查。")
                return self._error_result(f"Git 命令失败: {error_msg}")

            output = stdout.decode().strip()
            if not output:
                output = "工作区干净，没有未提交的更改"

            return self._success_result(output)

        except FileNotFoundError:
            return self._error_result("Git 未安装或不在 PATH 中")
        except Exception as e:
            logger.error(f"Git status 执行失败: {e}", exc_info=True)
            return self._error_result(f"执行失败: {e}")


class GitDiffTool(BaseTool):
    """
    查看 Git 文件差异。（只读）

    显示工作区或暂存区的文件变更。
    权限: AUTO（只读操作）
    """

    name = "git_diff"
    read_only = True
    description = (
        "Show the actual code changes (diff) in the working tree or for a specific file. "
        "Use this to review what you've changed before committing, or to understand a teammate's changes. "
        "For staged changes, set staged=true. For a specific file, pass file_path. "
        "Use git_status first for a quick overview, then git_diff for details."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root or Path.cwd()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径（可选，默认所有文件）",
                    },
                    "staged": {
                        "type": "boolean",
                        "description": "是否查看暂存区（默认 false，查看工作区）",
                        "default": False,
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "上下文行数（默认 3）",
                        "default": 3,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        file_path = args.get("file_path")
        staged = args.get("staged", False)
        context_lines = args.get("context_lines", 3)
        root = _workspace_root(context, self._workspace_root)

        cmd = ["git", "diff", f"--unified={context_lines}"]
        if staged:
            cmd.append("--staged")

        if file_path:
            cmd.append(file_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                if "not a git repository" in error_msg.lower():
                    return self._success_result("当前工作区不是 Git 仓库；没有 Git diff。")
                return self._error_result(f"Git 命令失败: {stderr.decode()}")

            output = stdout.decode().strip()
            if not output:
                area = "暂存区" if staged else "工作区"
                output = f"{area}没有变更"

            return self._success_result(output)

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")


class GitLogTool(BaseTool):
    """
    查看 Git 提交历史。（只读）

    显示最近的提交记录。
    权限: AUTO（只读操作）
    """

    name = "git_log"
    read_only = True
    description = (
        "Show Git commit history with messages, authors, and timestamps. "
        "Use this to understand recent changes, find when a bug was introduced, or review a file's evolution. "
        "Defaults to 10 recent commits. Pass file_path to see history for a specific file. "
        "Use git_diff for the actual code changes; git_log shows the commit metadata only."
    )
    permission = PermissionLevel.AUTO

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root or Path.cwd()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "显示的提交数量（默认 10）",
                        "default": 10,
                    },
                    "file_path": {
                        "type": "string",
                        "description": "文件路径（可选，查看特定文件的历史）",
                    },
                    "oneline": {
                        "type": "boolean",
                        "description": "是否使用简洁格式（默认 false）",
                        "default": False,
                    },
                },
                "required": [],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        limit = args.get("limit", 10)
        file_path = args.get("file_path")
        oneline = args.get("oneline", False)
        root = _workspace_root(context, self._workspace_root)

        cmd = ["git", "log", f"-{limit}"]

        if oneline:
            cmd.append("--oneline")
        else:
            cmd.extend(["--pretty=format:%h - %an, %ar : %s"])

        if file_path:
            cmd.extend(["--", file_path])

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                if "not a git repository" in error_msg.lower():
                    return self._success_result("当前工作区不是 Git 仓库；没有 Git 提交历史。")
                return self._error_result(f"Git 命令失败: {stderr.decode()}")

            output = stdout.decode().strip()
            if not output:
                output = "没有提交历史"

            return self._success_result(output)

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")


class GitCommitTool(BaseTool):
    """
    创建 Git 提交。

    将暂存区的更改提交到仓库。
    权限: CONFIRM（需要用户确认）
    """

    name = "git_commit"
    mutates_external_state = True
    description = (
        "Create a Git commit with the staged changes. "
        "Before committing: (1) run git_status to review changes, (2) run git_diff to verify the code, "
        "(3) use run_command('git add ...') to stage files. "
        "Write concise commit messages in conventional format (e.g., 'feat: add login page', 'fix: handle null pointer'). "
        "Do NOT commit without reviewing the diff first."
    )
    permission = PermissionLevel.CONFIRM

    def __init__(self, workspace_root: Path | None = None):
        self._workspace_root = workspace_root or Path.cwd()

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "提交信息",
                    },
                    "add_all": {
                        "type": "boolean",
                        "description": "是否自动添加所有修改的文件（git add -A）",
                        "default": False,
                    },
                },
                "required": ["message"],
            },
        )

    async def execute(self, args: dict[str, Any], context: Any = None) -> ToolResult:
        message = args.get("message", "")
        add_all = args.get("add_all", False)
        root = _workspace_root(context, self._workspace_root)

        if not message:
            return self._error_result("缺少提交信息")

        try:
            # 如果需要，先添加所有文件
            if add_all:
                add_proc = await asyncio.create_subprocess_exec(
                    "git",
                    "add",
                    "-A",
                    cwd=str(root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await add_proc.communicate()

            # 创建提交
            proc = await asyncio.create_subprocess_exec(
                "git",
                "commit",
                "-m",
                message,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                error_msg = stderr.decode().strip()
                if "nothing to commit" in error_msg.lower():
                    return self._error_result("没有需要提交的更改")
                return self._error_result(f"提交失败: {error_msg}")

            output = stdout.decode().strip()

            return self._success_result(f"提交成功:\n{output}")

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")
