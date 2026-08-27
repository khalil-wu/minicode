"""
Git 工具集。cc 不提供同构的用户级 git 工具（其 Git 能力依托 Bash、
checkpoint 与 worktree 基础设施）；本工具集为 MiniCode 自有设计，
checkpoint/worktree 相关机制另见对应模块的上游标注。

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
from backend.subprocesses import (
    SubprocessOutputLimitError,
    communicate_bounded,
    spawn_exec,
)

logger = logging.getLogger(__name__)
_GIT_TRANSPORT_LIMIT_BYTES = 20 * 1024 * 1024


async def _communicate_git(
    proc: asyncio.subprocess.Process,
) -> tuple[bytes, bytes]:
    return await communicate_bounded(
        proc,
        stdout_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
        stderr_limit_bytes=_GIT_TRANSPORT_LIMIT_BYTES,
    )


def _raise_if_cancelled(context: Any) -> None:
    cancel_event = getattr(context, "cancel_event", None) if context is not None else None
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError


def _decode_process_output(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")


def _workspace_root(context: Any, fallback: Path) -> Path:
    if context and getattr(context, "workspace_root", None):
        return Path(context.workspace_root).resolve()
    return fallback.resolve()


def _resolve_work_dir(root: Path, path_value: Any) -> Path:
    raw = str(path_value or ".").strip() or "."
    resolved = (root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
    # Confine to the workspace: an absolute or ../-escaping path must not let a
    # read-only AUTO tool inspect arbitrary repos on the host. Escapes fall back
    # to the workspace root rather than running git in an unrelated directory.
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return root_resolved
    return resolved


def _is_denied_path(context: Any, file_path: str) -> bool:
    checker = getattr(context, "permission_checker", None) if context is not None else None
    if checker is None:
        return False
    permission = getattr(context, "permission", None)
    try:
        return not checker.is_path_allowed(str(file_path), context=permission)
    except Exception:
        return False


class GitStatusTool(BaseTool):
    """
    查看 Git 工作区状态。

    显示修改、新增、删除的文件。
    权限: AUTO（只读操作）
    """

    name = "git_status"
    result_kind = "code"
    activity_kind = "workspaceSearch"
    display_label = "Git status"
    read_only = True
    description = (
        "Show the Git working tree status: modified, added, deleted, and untracked files. "
        "Use this before commits to review what changed, or at the start of a task to understand the current state. "
        "Returns a structured list of changed files with their status (M/A/D/??). "
        "For actual diffs, use git_diff instead."
    )
    permission = PermissionLevel.AUTO
    # The checker only fences path arguments a tool declares, so an undeclared
    # field leaves an AUTO read-only tool with no workspace boundary.
    workspace_path_fields = ("path",)
    allow_workspace_root_path = True

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
            proc = await spawn_exec(
                "git",
                "status",
                "--short",
                "--branch",
                cwd=str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await _communicate_git(proc)

            if proc.returncode != 0:
                error_msg = _decode_process_output(stderr).strip()
                if "not a git repository" in error_msg.lower():
                    return self._success_result(f"{path_str} 不是 Git 仓库；跳过 Git 状态检查。")
                return self._error_result(f"Git 命令失败: {error_msg}")

            output = _decode_process_output(stdout).strip()
            if not output:
                output = "工作区干净，没有未提交的更改"

            return self._success_result(output)

        except FileNotFoundError:
            return self._error_result("Git 未安装或不在 PATH 中")
        except SubprocessOutputLimitError:
            return self._error_result("Git status 输出超过 20 MB；请缩小仓库范围后重试")
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
    result_kind = "code"
    activity_kind = "workspaceSearch"
    display_label = "Git diff"
    read_only = True
    description = (
        "Show the actual code changes (diff) in the working tree or for a specific file. "
        "Use this to review what you've changed before committing, or to understand a teammate's changes. "
        "For staged changes, set staged=true. For a specific file, pass file_path. "
        "Use git_status first for a quick overview, then git_diff for details."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("file_path",)

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

        try:
            if file_path:
                if _is_denied_path(context, str(file_path)):
                    return self._error_result(
                        f"路径 '{file_path}' 属于受保护的敏感路径，git_diff 不会输出其内容。"
                    )
                # "--" terminates option parsing so a path like "--output=..." is
                # treated as a pathspec, not a git flag (which could write files).
                cmd.extend(["--", file_path])
            else:
                # A bare diff would print denylisted contents verbatim. Ask the
                # permission checker about each concretely changed path and
                # exclude only those, so a denylist entry that is re-allowed by a
                # negation rule (".env.example") is not silently hidden.
                denied_or_result = await self._denied_changed_paths(root, staged, context)
                if isinstance(denied_or_result, ToolResult):
                    return denied_or_result
                cmd.append("--")
                cmd.extend(f":(exclude,literal){path}" for path in denied_or_result)

            proc = await spawn_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await _communicate_git(proc)

            if proc.returncode != 0:
                error_msg = _decode_process_output(stderr).strip()
                if "not a git repository" in error_msg.lower():
                    return self._success_result("当前工作区不是 Git 仓库；没有 Git diff。")
                return self._error_result(f"Git 命令失败: {error_msg}")

            output = _decode_process_output(stdout).strip()
            if not output:
                area = "暂存区" if staged else "工作区"
                output = f"{area}没有变更"

            return self._success_result(output)

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except SubprocessOutputLimitError:
            return self._error_result("Git diff 输出超过 20 MB；请指定 file_path 缩小范围")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")

    async def _denied_changed_paths(
        self,
        root: Path,
        staged: bool,
        context: Any,
    ) -> list[str] | ToolResult:
        """Return changed paths the permission checker refuses, or a ToolResult.

        Listing names first keeps the exclusion argv bounded by the number of
        *denied* files rather than by the denylist's pattern count, and makes the
        checker the single authority on whether a path is readable.
        """

        cmd = ["git", "diff", "--name-only"]
        if staged:
            cmd.append("--staged")
        proc = await spawn_exec(
            *cmd,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await _communicate_git(proc)
        if proc.returncode != 0:
            error_msg = _decode_process_output(stderr).strip()
            if "not a git repository" in error_msg.lower():
                return self._success_result("当前工作区不是 Git 仓库；没有 Git diff。")
            return self._error_result(f"Git 命令失败: {error_msg}")

        denied: list[str] = []
        for line in _decode_process_output(stdout).splitlines():
            candidate = line.strip()
            if candidate and _is_denied_path(context, candidate) and candidate not in denied:
                denied.append(candidate)
        return denied


class GitLogTool(BaseTool):
    """
    查看 Git 提交历史。（只读）

    显示最近的提交记录。
    权限: AUTO（只读操作）
    """

    name = "git_log"
    result_kind = "code"
    activity_kind = "workspaceSearch"
    display_label = "Git log"
    read_only = True
    description = (
        "Show Git commit history with messages, authors, and timestamps. "
        "Use this to understand recent changes, find when a bug was introduced, or review a file's evolution. "
        "Defaults to 10 recent commits. Pass file_path to see history for a specific file. "
        "Use git_diff for the actual code changes; git_log shows the commit metadata only."
    )
    permission = PermissionLevel.AUTO
    workspace_path_fields = ("file_path",)

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
            proc = await spawn_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await _communicate_git(proc)

            if proc.returncode != 0:
                error_msg = _decode_process_output(stderr).strip()
                if "not a git repository" in error_msg.lower():
                    return self._success_result("当前工作区不是 Git 仓库；没有 Git 提交历史。")
                return self._error_result(f"Git 命令失败: {error_msg}")

            output = _decode_process_output(stdout).strip()
            if not output:
                output = "没有提交历史"

            return self._success_result(output)

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except SubprocessOutputLimitError:
            return self._error_result("Git log 输出超过 20 MB；请降低 limit 或指定 file_path")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")


class GitCommitTool(BaseTool):
    """
    创建 Git 提交。

    将暂存区的更改提交到仓库。
    权限: CONFIRM（需要用户确认）
    """

    name = "git_commit"
    result_kind = "command"
    activity_kind = "commandExecution"
    display_label = "Git commit"
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
                add_proc = await spawn_exec(
                    "git",
                    "add",
                    "-A",
                    cwd=str(root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _add_stdout, add_stderr = await _communicate_git(add_proc)
                if add_proc.returncode != 0:
                    error_msg = _decode_process_output(add_stderr).strip()
                    return self._error_result(f"暂存失败，未创建提交: {error_msg}")

                # git add and git commit are one user-approved operation. An
                # interrupt between the two phases must not start phase two.
                _raise_if_cancelled(context)

            # 创建提交
            proc = await spawn_exec(
                "git",
                "commit",
                "-m",
                message,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await _communicate_git(proc)

            if proc.returncode != 0:
                error_msg = _decode_process_output(stderr).strip()
                if "nothing to commit" in error_msg.lower():
                    return self._error_result("没有需要提交的更改")
                return self._error_result(f"提交失败: {error_msg}")

            output = _decode_process_output(stdout).strip()

            return self._success_result(f"提交成功:\n{output}")

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except SubprocessOutputLimitError:
            return self._error_result("Git commit 输出超过 20 MB；提交结果无法可靠确认")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")
