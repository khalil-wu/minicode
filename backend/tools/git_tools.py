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
from backend.subprocesses import communicate, spawn_exec

logger = logging.getLogger(__name__)


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


def _secret_exclude_pathspecs(context: Any) -> list[str]:
    """Git pathspecs that keep secret contents out of diff output.

    ``git diff`` with no path argument prints the before/after text of every
    changed file, so an AUTO read-only tool would otherwise echo .env and
    secrets/ in full — contents the file tools refuse to read.
    """
    from backend.security.sensitive_files import (
        SENSITIVE_FILE_NAMES,
        SENSITIVE_FILE_PREFIXES,
        SENSITIVE_FILE_SUFFIXES,
        SENSITIVE_PATH_PARTS,
    )

    patterns: list[str] = []
    patterns.extend(sorted(SENSITIVE_FILE_NAMES))
    patterns.extend(f"{prefix}*" for prefix in sorted(SENSITIVE_FILE_PREFIXES))
    patterns.extend(f"*{suffix}" for suffix in sorted(SENSITIVE_FILE_SUFFIXES))
    patterns.extend(f"{part}/" for part in sorted(SENSITIVE_PATH_PARTS))

    checker = getattr(context, "permission_checker", None) if context is not None else None
    if checker is not None:
        permission = getattr(context, "permission", None)
        constraints = getattr(permission, "filesystem_constraints", None) or {}
        if "denylist" in constraints:
            configured = list(constraints["denylist"])
        else:
            settings = getattr(checker, "_settings", None)
            configured = list(getattr(settings, "path_denylist", ()) or ())
        patterns.extend(str(pattern) for pattern in configured)

    specs: list[str] = []
    for pattern in patterns:
        cleaned = str(pattern or "").replace("\\", "/").strip()
        if not cleaned:
            continue
        if cleaned.endswith("/"):
            cleaned = f"{cleaned.rstrip('/')}/**"
        spec = f":(exclude,glob)**/{cleaned}" if "/" not in cleaned else f":(exclude,glob){cleaned}"
        if spec not in specs:
            specs.append(spec)
    return specs


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

            stdout, stderr = await communicate(proc)

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

        if file_path:
            if _is_denied_path(context, str(file_path)):
                return self._error_result(
                    f"路径 '{file_path}' 属于受保护的敏感路径，git_diff 不会输出其内容。"
                )
            # "--" terminates option parsing so a path like "--output=..." is
            # treated as a pathspec, not a git flag (which could write files).
            # Mirrors git_log below, which already separates with "--".
            cmd.extend(["--", file_path])
        else:
            # A bare diff would print secret contents verbatim; exclude them the
            # same way read_file and grep_files do.
            cmd.append("--")
            cmd.extend(_secret_exclude_pathspecs(context))

        try:
            proc = await spawn_exec(
                *cmd,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stdout, stderr = await communicate(proc)

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
        except Exception as e:
            return self._error_result(f"执行失败: {e}")


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

            stdout, stderr = await communicate(proc)

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
                await communicate(add_proc)

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

            stdout, stderr = await communicate(proc)

            if proc.returncode != 0:
                error_msg = _decode_process_output(stderr).strip()
                if "nothing to commit" in error_msg.lower():
                    return self._error_result("没有需要提交的更改")
                return self._error_result(f"提交失败: {error_msg}")

            output = _decode_process_output(stdout).strip()

            return self._success_result(f"提交成功:\n{output}")

        except FileNotFoundError:
            return self._error_result("Git 未安装")
        except Exception as e:
            return self._error_result(f"执行失败: {e}")
