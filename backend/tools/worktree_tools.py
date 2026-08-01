"""
Git Worktree 工具（参考 Claude Code 的 worktree 支持）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema

if TYPE_CHECKING:
    from backend.permissions.context import ToolExecutionContext


async def _run_worktree_hook(event: str, *, path: str, branch: str = "", base: str = "", reason: str = "") -> None:
    from backend.hooks import get_hook_manager

    hook_mgr = get_hook_manager()
    if not hook_mgr:
        return
    try:
        if event == "create":
            await hook_mgr.run_worktree_create(path=path, branch=branch, base=base)
        elif event == "remove":
            await hook_mgr.run_worktree_remove(path=path, reason=reason)
    except Exception:
        # Worktree hooks are audit/automation side effects; tool success has
        # already happened, so hook failures should not roll it back.
        return


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

        worktrees = await asyncio.to_thread(manager.list_worktrees)

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

        manager = await _resolve_worktree_manager(context)

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        path = Path(path_str)

        success = await asyncio.to_thread(
            manager.create_worktree,
            path=path,
            branch=branch,
            new_branch=new_branch,
            commit=commit,
        )

        if success:
            branch_info = f"分支 {branch}" if branch else f"提交 {commit or 'HEAD'}"
            await _run_worktree_hook(
                "create",
                path=str(path),
                branch=str(branch or ""),
                base=str(commit or "HEAD"),
            )
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

        manager = await _resolve_worktree_manager(context)

        if manager is None:
            return self._error_result("当前目录不是 Git 仓库")

        path = Path(path_str)

        success = await asyncio.to_thread(manager.remove_worktree, path=path, force=force)

        if success:
            await _run_worktree_hook(
                "remove",
                path=str(path),
                reason="force" if force else "remove",
            )
            return self._success_result(f"已删除 worktree: {path}")
        else:
            return self._error_result(
                f"删除 worktree 失败: {path}\n"
                "提示: 如果有未提交的更改，请使用 force=true"
            )


async def _resolve_worktree_manager(context: "ToolExecutionContext | None"):
    """优先用当前工作区根构造 manager,回退到全局单例。非 Git 仓库返回 None。"""
    from backend.workspace.worktree import WorktreeManager, get_global_worktree_manager

    root = getattr(context, "workspace_root", None) if context else None
    if root:
        try:
            return await asyncio.to_thread(WorktreeManager, Path(root))
        except ValueError:
            return None
    return await asyncio.to_thread(get_global_worktree_manager)


class SnapshotWorktreeTool(BaseTool):
    """为 worktree 抓取可恢复快照"""

    name = "worktree_snapshot"
    result_kind = "workspace"
    activity_kind = "genericTool"
    display_label = "Snapshot worktree"
    mutates_external_state = True
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

        record = await asyncio.to_thread(
            manager.snapshot_worktree,
            Path(path_str),
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
        result = await asyncio.to_thread(
            manager.restore_snapshot,
            snapshot_id,
            dest=Path(dest) if dest else None,
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
        "列出已保存的 worktree 快照(删除前自动或手动抓取的可恢复点),最新在前。"
        "可选 conversation_id 过滤。"
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
        records = await asyncio.to_thread(manager.list_snapshots, conversation_id)
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
