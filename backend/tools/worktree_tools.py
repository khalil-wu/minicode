"""
Git Worktree 工具（参考 Claude Code 的 worktree 支持）。
"""

from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe

import asyncio
import concurrent.futures
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext

# Conversation metadata owns durable hook registrations. Standalone callers
# without a conversation repository retain their process-local ownership.
_HOOK_CREATED_WORKTREES: set[str] = set()


def _worktree_repository(context: Any) -> Any | None:
    return context.run_context.conversation_repository if context and context.run_context else None


def _is_registered_hook_worktree(path: str, context: Any) -> bool:
    from backend.atomic_io import canonical_file_path_key

    repository = _worktree_repository(context)
    if repository is None or not context.conversation_id:
        return path in _HOOK_CREATED_WORKTREES
    record = repository.get_conversation(context.conversation_id)
    return record is not None and any(
        entry["backend"] == "hook" and canonical_file_path_key(entry["path"]) == canonical_file_path_key(path)
        for entry in record.worktree_registrations
    )


def _owned_git_worktree_path(manager: Any, requested_path: Path) -> Path:
    from backend.workspace.worktree import isolated_worktree_root

    owned_root = isolated_worktree_root(Path(manager.repo_root))
    if requested_path.is_absolute():
        candidate = requested_path.expanduser().resolve()
    elif tuple(requested_path.parts[:2]) == (".minicode", "worktrees"):
        candidate = (Path(manager.repo_root) / requested_path).resolve()
    else:
        candidate = (owned_root / requested_path).resolve()
    try:
        relative = candidate.relative_to(owned_root)
    except ValueError as exc:
        raise ValueError(
            f"Worktree path must stay under MiniCode's owned root: {owned_root}"
        ) from exc
    if not relative.parts:
        raise ValueError("Worktree path must identify a child of the owned root")
    return candidate


async def _run_worktree_hook(
    hook_manager: Any | None,
    event: str,
    *,
    path: str,
    branch: str = "",
    base: str = "",
    reason: str = "",
) -> Any | None:
    from backend.hooks.manager import HookEvent

    hook_event = (
        HookEvent.WORKTREE_CREATE
        if event == "create"
        else HookEvent.WORKTREE_REMOVE
    )
    if hook_manager is None or not hook_manager.has_hooks(hook_event):
        return None
    if event == "create":
        return await hook_manager.run_worktree_create(
            path=path,
            branch=branch,
            base=base,
        )
    return await hook_manager.run_worktree_remove(path=path, reason=reason)


def _hook_result_error(result: Any, *, event: str) -> str:
    """Render a stable user-facing error from a HookResult-like value."""

    if result is None:
        return f"Worktree{event} hook did not return a result"
    return str(
        getattr(result, "message", "")
        or getattr(result, "feedback", "")
        or "\n".join(str(item) for item in (getattr(result, "errors", ()) or ()) if item)
        or f"Worktree{event} hook failed"
    ).strip()


def _normalize_hook_worktree_path(raw_path: Any, *, requested_path: Path, context: Any) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        root = getattr(context, "workspace_root", None) if context is not None else None
        candidate = Path(root or requested_path.parent) / candidate
    return candidate.resolve()


class ListWorktreesTool(BaseTool):
    """列出所有 Git worktree"""

    name = "list_worktrees"
    result_kind = "workspace"
    activity_kind = "workspaceSearch"
    display_label = "List worktrees"
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
        manager = await _resolve_worktree_manager(context)

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        worktrees = await to_thread_cancel_safe(manager.list_worktrees)

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
    result_kind = "workspace"
    activity_kind = "genericTool"
    display_label = "Create worktree"
    mutates_workspace = True
    description = (
        "创建新的 Git worktree。"
        "Worktree 允许在独立的目录中检出不同的分支，"
        "适用于并行开发、测试、代码审查等场景。"
    )
    permission = PermissionLevel.CONFIRM
    # The checker only fences declared path arguments, so leaving this off
    # let a worktree be created or restored anywhere on the host.
    workspace_path_fields = ("path",)

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

        requested_path = Path(path_str)
        hook_manager = (
            context.run_context.hook_manager
            if context is not None and context.run_context is not None
            else None
        )

        # Claude delegates WorktreeCreate to the configured hook as the VCS
        # backend.  Check this before resolving a Git manager: hook-only
        # projects are valid even when the current directory is not a Git
        # repository.
        hook_result = await _run_worktree_hook(
            hook_manager,
            "create",
            path=str(requested_path),
            branch=str(branch or ""),
            base=str(commit or "HEAD"),
        )
        if hook_result is not None:
            if getattr(hook_result, "blocked", False) or getattr(hook_result, "failed", False):
                return self._error_result(
                    f"WorktreeCreate hook blocked creation: {_hook_result_error(hook_result, event='Create')}"
                )
            hook_path = _normalize_hook_worktree_path(
                getattr(hook_result, "worktree_path", ""),
                requested_path=requested_path,
                context=context,
            )
            if hook_path is None:
                return self._error_result(
                    "WorktreeCreate hook completed without returning a worktree path"
                )
            _HOOK_CREATED_WORKTREES.add(str(hook_path))
            repository = _worktree_repository(context)
            if repository is not None and context.conversation_id:
                repository.register_worktree(
                    context.conversation_id, path=str(hook_path), backend="hook",
                    workspace_root=str(context.workspace_root or ""),
                )
            return self._success_result(
                f"已通过 WorktreeCreate hook 创建 worktree: {hook_path}"
            )

        manager = await _resolve_worktree_manager(context)

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        try:
            path = _owned_git_worktree_path(manager, requested_path)
        except ValueError as exc:
            return self._error_result(str(exc))

        success = await to_thread_cancel_safe(
            manager.create_worktree,
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
    result_kind = "workspace"
    activity_kind = "genericTool"
    display_label = "Remove worktree"
    mutates_workspace = True
    destructive = True
    description = (
        "删除指定的 Git worktree。"
        "注意: 如果 worktree 中有未提交的更改，需要使用 force=true 强制删除。"
    )
    permission = PermissionLevel.CONFIRM
    # The checker only fences declared path arguments, so leaving this off
    # let a worktree be created or restored anywhere on the host.
    workspace_path_fields = ("path",)

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

        requested_path = Path(path_str)
        normalized_path = str(_normalize_hook_worktree_path(path_str, requested_path=requested_path, context=context))
        hook_manager = (
            context.run_context.hook_manager
            if context is not None and context.run_context is not None
            else None
        )
        hook_mgr_result = None
        registered_hook = _is_registered_hook_worktree(normalized_path, context)
        if registered_hook:
            hook_mgr_result = await _run_worktree_hook(
                hook_manager,
                "remove",
                path=normalized_path,
                reason="force" if force else "remove",
            )
        if hook_mgr_result is not None:
            if getattr(hook_mgr_result, "blocked", False) or getattr(hook_mgr_result, "failed", False):
                return self._error_result(
                    f"WorktreeRemove hook blocked removal: {_hook_result_error(hook_mgr_result, event='Remove')}"
                )
            _HOOK_CREATED_WORKTREES.discard(normalized_path)
            repository = _worktree_repository(context)
            if repository is not None and context.conversation_id:
                repository.unregister_worktree(context.conversation_id, path=normalized_path)
            return self._success_result(
                f"已通过 WorktreeRemove hook 删除 worktree: {normalized_path}"
            )
        if registered_hook:
            return self._error_result("该 worktree 由 hook 创建；请配置 WorktreeRemove hook 后再删除。")

        manager = await _resolve_worktree_manager(context)

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        try:
            path = _owned_git_worktree_path(manager, requested_path)
        except ValueError as exc:
            return self._error_result(str(exc))

        success = await to_thread_cancel_safe(manager.remove_worktree, path=path, force=force)

        if success:
            return self._success_result(f"已删除 worktree: {path}")
        else:
            return self._error_result(
                f"删除 worktree 失败: {path}\n"
                "提示: 如果有未提交的更改，请使用 force=true"
            )


async def _resolve_worktree_manager(context: "ToolExecutionContext | None"):
    """Bind model-facing Git execution to the current workspace and sandbox."""
    from backend.workspace.worktree import NotGitRepositoryError, WorktreeManager, get_global_worktree_manager

    if context is None:
        return await to_thread_cancel_safe(get_global_worktree_manager)
    root = context.workspace_root
    if root:
        from backend.sandbox.policy import (
            AdditionalPermissionProfile, FileSystemAccessMode, FileSystemPath,
            FileSystemPermissions, FileSystemSandboxEntry, sandbox_policy_for_permission_context,
        )
        from backend.tools.git_support import _run_git, SandboxRunner

        workspace = Path(root).resolve()
        captured_policy = context.sandbox_policy or sandbox_policy_for_permission_context(workspace, context.permission)
        loop = asyncio.get_running_loop()

        def git_runner(argv, *, cwd, env, check, index_file=None, text=False, encoding="utf-8", **options):
            policy = captured_policy
            if argv[1:3] in (["worktree", "add"], ["worktree", "remove"]):
                target_index = 5 if argv[3] == "-b" else 4 if argv[3] in {"--detach", "--force"} else 3
                target = Path(argv[target_index]).resolve()
                # A fixed Git operation addresses the tool's owned path (or
                # a repository-validated restore record). Reopen only the
                # default metadata mask where this captured policy already
                # grants write access; explicit readonly/denied paths stay so.
                if policy.resolve(cwd=workspace).resolve_access(target) is FileSystemAccessMode.WRITE:
                    entries = []
                    if policy.protect_workspace_metadata:
                        # Materialize the metadata parent even before it
                        # exists. An ancestor scratch/TMP write root must not
                        # make siblings of this exact worktree writable.
                        entries.append(FileSystemSandboxEntry(
                            FileSystemPath.path(workspace / ".minicode"), FileSystemAccessMode.READ,
                        ))
                    entries.append(FileSystemSandboxEntry(FileSystemPath.path(target), FileSystemAccessMode.WRITE))
                    policy = policy.with_additional_permissions(AdditionalPermissionProfile(
                        file_system=FileSystemPermissions(tuple(entries)),
                    ))
            git_args = argv[2:] if argv[1] == "--no-optional-locks" else argv[1:]
            read_only = git_args[0] in {"rev-parse", "status"} or git_args[:2] in (["worktree", "list"], ["branch", "--show-current"])
            launch_cwd = Path(cwd)
            if (read_only and launch_cwd.resolve() != workspace
                    and SandboxRunner(policy).capability(cwd=launch_cwd).backend == "windows-elevated-wfp"):
                # Prepare the captured repository owner before Git follows a
                # linked checkout's metadata pointer. Keep status scoped by -C.
                argv = [argv[0], "-C", str(launch_cwd), *argv[1:]]
                launch_cwd = workspace
            operation = asyncio.run_coroutine_threadsafe(_run_git(
                argv, root=workspace, cwd=launch_cwd, context=context,
                sandbox_policy=policy, write_git_metadata=not read_only,
                timeout=options["timeout"],
                index_file=index_file,
            ), loop)
            try:
                result = operation.result()
            except concurrent.futures.CancelledError:
                raise asyncio.CancelledError
            if text:
                result = subprocess.CompletedProcess(result.args, result.returncode,
                    result.stdout.decode(encoding), result.stderr.decode(encoding))
            if check and result.returncode:
                raise subprocess.CalledProcessError(result.returncode, argv, output=result.stdout, stderr=result.stderr)
            return result

        try:
            return await to_thread_cancel_safe(WorktreeManager, workspace, git_runner=git_runner)
        except NotGitRepositoryError:
            return None
    return None


class SnapshotWorktreeTool(BaseTool):
    """为 worktree 抓取可恢复快照"""

    name = "worktree_snapshot"
    result_kind = "workspace"
    activity_kind = "genericTool"
    display_label = "Snapshot worktree"
    mutates_workspace = True
    description = (
        "为一个 worktree 抓取可恢复的快照(包含未提交的 tracked 与 untracked 改动)。"
        "适合在清理/删除一个隔离 worktree 之前手动保存,之后可用 worktree_restore 恢复。"
        "省略 path 时默认对当前工作区。"
    )
    permission = PermissionLevel.CONFIRM
    # The checker only fences declared path arguments, so leaving this off
    # let a worktree be created or restored anywhere on the host.
    workspace_path_fields = ("path",)

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要快照的 worktree 路径(可选,默认当前工作区)",
                    },
                    "label": {
                        "type": "string",
                        "description": "可选的备注标签",
                    },
                },
                "required": [],
            },
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        manager = await _resolve_worktree_manager(context)
        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        path_str = args.get("path") or (getattr(context, "workspace_root", None) if context else None)
        if not path_str:
            return self._error_result("缺少 path 参数,且无法从上下文推断工作区")

        snapshot_path = Path(path_str).expanduser()
        if not snapshot_path.is_absolute():
            snapshot_path = Path(context.workspace_root if context is not None else manager.repo_root) / snapshot_path
        record = await to_thread_cancel_safe(
            manager.snapshot_worktree,
            snapshot_path.resolve(),
            conversation_id=context.conversation_id if context is not None else "",
            label=str(args.get("label", "")),
        )
        if record is None:
            return self._error_result(f"快照失败: {path_str}")

        return self._success_result(
            f"已保存 worktree 快照\n"
            f"  快照 ID: {record.id}\n"
            f"  提交: {record.snapshot_sha[:8]}\n"
            f"  引用: {record.snapshot_ref}\n"
            f"  原路径: {record.original_path}\n"
            f"用 worktree_restore(snapshot_id=\"{record.id}\") 可恢复。"
        )


class RestoreWorktreeTool(BaseTool):
    """从快照恢复 worktree"""

    name = "worktree_restore"
    result_kind = "workspace"
    activity_kind = "genericTool"
    display_label = "Restore worktree"
    mutates_workspace = True
    description = (
        "把一个 worktree 快照恢复成新的 worktree(detached 在快照提交上),"
        "找回当时未提交的全部改动。用 list_worktree_snapshots 查看可用快照。"
    )
    permission = PermissionLevel.CONFIRM
    # The checker only fences declared path arguments, so leaving this off
    # let a worktree be created or restored anywhere on the host.
    workspace_path_fields = ("dest",)

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "snapshot_id": {
                        "type": "string",
                        "description": "要恢复的快照 ID(来自 list_worktree_snapshots)",
                    },
                    "dest": {
                        "type": "string",
                        "description": "恢复目标路径(可选,默认原路径;被占用时自动加 -restored 后缀)",
                    },
                },
                "required": ["snapshot_id"],
            },
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        snapshot_id = str(args.get("snapshot_id", "")).strip()
        if not snapshot_id:
            return self._error_result("缺少 snapshot_id 参数")

        manager = await _resolve_worktree_manager(context)
        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        dest = args.get("dest")
        if dest:
            dest = Path(dest).expanduser()
            if not dest.is_absolute():
                dest = Path(context.workspace_root if context is not None else manager.repo_root) / dest
        result = await to_thread_cancel_safe(
            manager.restore_snapshot,
            snapshot_id,
            dest=dest.resolve() if dest else None,
        )
        if not result.restored:
            return self._error_result(f"恢复失败: {result.error or snapshot_id}")

        return self._success_result(
            f"已从快照 {snapshot_id} 恢复 worktree\n"
            f"  路径: {result.path}\n"
            "该 worktree 处于 detached HEAD,改动以快照提交的形式存在。"
        )


class ListWorktreeSnapshotsTool(BaseTool):
    """列出 worktree 快照"""

    name = "list_worktree_snapshots"
    result_kind = "workspace"
    activity_kind = "workspaceSearch"
    display_label = "List worktree snapshots"
    read_only = True
    description = (
        "列出通过 worktree_snapshot 手动保存的 worktree 快照,最新在前。"
        "快照不会在删除 worktree 前自动创建——移除包含未提交更改的 worktree 前,"
        "必须先显式调用 worktree_snapshot。可选 conversation_id 过滤。"
    )
    permission = PermissionLevel.AUTO

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "conversation_id": {
                        "type": "string",
                        "description": "按会话过滤(可选)",
                    },
                },
                "required": [],
            },
        )

    async def execute(
        self, args: dict[str, Any], context: ToolExecutionContext | None = None
    ) -> ToolResult:
        manager = await _resolve_worktree_manager(context)
        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        conversation_id = str(args.get("conversation_id", "")).strip() or None
        records = await to_thread_cancel_safe(manager.list_snapshots, conversation_id)
        if not records:
            return self._success_result("没有找到 worktree 快照")

        lines = [f"找到 {len(records)} 个快照:\n"]
        for i, record in enumerate(records, 1):
            lines.append(f"{i}. {record.id}  ({record.snapshot_sha[:8]})")
            lines.append(f"   原路径: {record.original_path}")
            lines.append(f"   分支: {record.branch or '-'}  时间: {record.created_at}")
            if record.label:
                lines.append(f"   备注: {record.label}")
        return self._success_result("\n".join(lines))
