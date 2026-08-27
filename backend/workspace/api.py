from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from .models import (
    WorkspaceDeleteResponse,
    WorkspaceDirectoryCreateRequest,
    WorkspaceFileResponse,
    WorkspaceFileCompareWriteRequest,
    WorkspaceSearchResponse,
    WorkspaceGitWorktreeRemoveResponse,
    WorkspaceGitWorktreeResponse,
    WorkspaceGitWorktreeSwitchRequest,
    WorkspaceGitWorktreeSnapshotsResponse,
    WorkspaceGitWorktreeRestoreRequest,
    WorkspaceGitWorktreeRestoreResponse,
    WorkspaceFileUpdateRequest,
    WorkspacePathRenameRequest,
    WorkspacePathResponse,
    WorkspaceTreeResponse,
    ProjectImportRequest,
    ProjectImportResponse,
)
from backend.services.workspace_api_service import (
    import_project_payload,
    is_path_within,
    list_recent_projects_payload,
    remove_recent_project_payload,
    remove_workspace_git_worktree_payload,
    resolve_workspace_git_root,
    restore_workspace_git_worktree_snapshot_payload,
    switch_workspace_git_worktree_payload,
    validate_project_path_payload,
    workspace_git_diff_payload,
    workspace_search_payload,
    workspace_git_status_payload,
    workspace_git_worktree_payload,
    workspace_git_worktree_snapshots_payload,
)
from .service import WorkspaceService


def create_workspace_router(get_project_root: Callable[[], Path]) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])
    _ = get_project_root

    def _service(workspace_root: str) -> WorkspaceService:
        value = str(workspace_root or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail="workspace_root is required")
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(status_code=409, detail="Workspace folder does not exist.")
        return WorkspaceService(get_workspace_root=lambda: root)

    def _resolve_git_root(path: str, workspace_root: str) -> Path:
        """git 端点优先使用调用方显式传入的目录，避免全局工作区切换的竞态。"""
        service = _service(workspace_root)
        return resolve_workspace_git_root(path, service.workspace_root_path())

    @router.get("/tree", response_model=WorkspaceTreeResponse)
    async def workspace_tree_api(
        path: str = Query(".", min_length=1),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceTreeResponse:
        # Directory walks stay off the event loop (cc refreshes file
        # suggestions in the background; to_thread is the minimal equivalent).
        return await asyncio.to_thread(_service(workspace_root).list_tree, path)

    @router.get("/search", response_model=WorkspaceSearchResponse)
    async def workspace_search_api(
        query: str = Query(..., min_length=1),
        limit: int = Query(20, ge=1, le=100),
        include_tests: bool = Query(True),
        kind: str = Query("file", pattern="^(file|folder|all)$"),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceSearchResponse:
        service = _service(workspace_root)
        payload = await asyncio.to_thread(
            workspace_search_payload,
            root=service.workspace_root_path(),
            query=query,
            limit=limit,
            include_tests=include_tests,
            kind=kind,
        )
        return WorkspaceSearchResponse(**payload)

    @router.get("/file", response_model=WorkspaceFileResponse)
    async def workspace_read_file_api(
        path: str = Query(..., min_length=1),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceFileResponse:
        # Blocking disk I/O + sha256 must not stall the event loop while a
        # conversation streams tokens over the same process's WebSocket.
        return await asyncio.to_thread(_service(workspace_root).read_file, path)

    @router.get("/raw")
    async def workspace_raw_file_api(
        path: str = Query(..., min_length=1),
        workspace_root: str = Query(..., min_length=1),
        raw_token: str | None = Query(None),
    ) -> FileResponse:
        _ = raw_token
        return await asyncio.to_thread(_service(workspace_root).raw_file_response, path)

    @router.get("/preview")
    async def workspace_preview_file_api(
        path: str = Query(..., min_length=1),
        workspace_root: str = Query(..., min_length=1),
    ) -> dict[str, object]:
        """Return a bounded, parser-backed preview for a workspace deliverable."""
        service = _service(workspace_root)
        return await run_in_threadpool(service.preview_file, path)

    @router.put("/file", response_model=WorkspaceFileResponse)
    async def workspace_write_file_api(
        request: WorkspaceFileUpdateRequest,
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceFileResponse:
        return await asyncio.to_thread(
            _service(workspace_root).write_file,
            path=request.path,
            content=request.content,
        )

    @router.put("/file/compare-write", response_model=WorkspaceFileResponse)
    async def workspace_compare_write_file_api(
        request: WorkspaceFileCompareWriteRequest,
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceFileResponse:
        return await asyncio.to_thread(
            _service(workspace_root).compare_and_write_file,
            path=request.path,
            expected_hash=request.expected_hash,
            content=request.content,
        )

    @router.post("/directory", response_model=WorkspacePathResponse)
    async def workspace_create_directory_api(
        request: WorkspaceDirectoryCreateRequest,
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspacePathResponse:
        return await asyncio.to_thread(_service(workspace_root).create_directory, request.path)

    @router.post("/rename", response_model=WorkspacePathResponse)
    async def workspace_rename_path_api(
        request: WorkspacePathRenameRequest,
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspacePathResponse:
        return await asyncio.to_thread(
            _service(workspace_root).rename_path,
            path=request.path,
            new_path=request.new_path,
        )

    @router.delete("/path", response_model=WorkspaceDeleteResponse)
    async def workspace_delete_path_api(
        path: str = Query(..., min_length=1),
        recursive: bool = Query(False),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceDeleteResponse:
        return await asyncio.to_thread(
            _service(workspace_root).delete_path,
            path=path,
            recursive=recursive,
        )

    @router.post("/import", response_model=ProjectImportResponse)
    async def import_project_api(request: ProjectImportRequest) -> ProjectImportResponse:
        """导入项目文件夹，构建上下文"""
        return ProjectImportResponse(**(await import_project_payload(request.path)))

    @router.post("/validate")
    async def validate_path_api(request: ProjectImportRequest) -> dict:
        """验证路径有效性"""
        return await asyncio.to_thread(validate_project_path_payload, request.path)

    @router.get("/recent")
    async def recent_projects_api(limit: int = Query(10, ge=1, le=20)) -> dict:
        """获取最近项目列表"""
        return await asyncio.to_thread(list_recent_projects_payload, limit)

    @router.delete("/recent")
    async def remove_recent_project_api(path: str = Query(..., min_length=1)) -> dict:
        """Remove a project from the recent list without touching files on disk."""
        return await asyncio.to_thread(remove_recent_project_payload, path)

    @router.get("/git/status")
    async def git_status_api(
        path: str = Query(""),
        workspace_root: str = Query(..., min_length=1),
    ) -> dict:
        """返回 git 状态：分支、已修改/暂存/未跟踪文件列表"""
        # git subprocesses are blocking calls with multi-second timeouts; keep
        # them off the event loop so streaming is never stalled by status polls.
        root = await asyncio.to_thread(_resolve_git_root, path, workspace_root)
        return await asyncio.to_thread(workspace_git_status_payload, root)

    @router.get("/git/diff")
    async def git_diff_api(
        file: str = Query(""),
        path: str = Query(""),
        workspace_root: str = Query(..., min_length=1),
    ) -> dict:
        """返回指定文件的 git diff"""
        root = await asyncio.to_thread(_resolve_git_root, path, workspace_root)
        return await asyncio.to_thread(workspace_git_diff_payload, root, file)

    @router.get("/git/worktree", response_model=WorkspaceGitWorktreeResponse)
    async def git_worktree_api(
        path: str = Query(""),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceGitWorktreeResponse:
        """Return linked worktree metadata for the active workspace."""
        root = await asyncio.to_thread(_resolve_git_root, path, workspace_root)
        return WorkspaceGitWorktreeResponse(**(await asyncio.to_thread(workspace_git_worktree_payload, root)))

    @router.post("/git/worktree/switch", response_model=ProjectImportResponse)
    async def git_worktree_switch_api(
        request: WorkspaceGitWorktreeSwitchRequest,
        workspace_root: str = Query(..., min_length=1),
    ) -> ProjectImportResponse:
        """Switch the active workspace to an existing git worktree."""
        return ProjectImportResponse(
            **(await switch_workspace_git_worktree_payload(_service(workspace_root).workspace_root_path(), request.path))
        )

    @router.delete("/git/worktree", response_model=WorkspaceGitWorktreeRemoveResponse)
    async def git_worktree_remove_api(
        path: str = Query(..., min_length=1),
        force: bool = Query(False),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceGitWorktreeRemoveResponse:
        """Remove a MiniCode isolated worktree."""
        return WorkspaceGitWorktreeRemoveResponse(
            **remove_workspace_git_worktree_payload(
                root=_service(workspace_root).workspace_root_path(),
                path=path,
                force=force,
            )
        )

    @router.get("/git/worktree/snapshots", response_model=WorkspaceGitWorktreeSnapshotsResponse)
    async def git_worktree_snapshots_api(
        conversation_id: str | None = Query(None),
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceGitWorktreeSnapshotsResponse:
        """List pre-deletion worktree snapshots, newest first."""
        root = await asyncio.to_thread(_service(workspace_root).workspace_root_path)
        payload = await asyncio.to_thread(
            workspace_git_worktree_snapshots_payload,
            root,
            conversation_id,
        )
        return WorkspaceGitWorktreeSnapshotsResponse(**payload)

    @router.post("/git/worktree/restore", response_model=WorkspaceGitWorktreeRestoreResponse)
    async def git_worktree_restore_api(
        request: WorkspaceGitWorktreeRestoreRequest,
        workspace_root: str = Query(..., min_length=1),
    ) -> WorkspaceGitWorktreeRestoreResponse:
        """Restore a worktree snapshot to a detached worktree."""
        return WorkspaceGitWorktreeRestoreResponse(
            **restore_workspace_git_worktree_snapshot_payload(
                root=_service(workspace_root).workspace_root_path(),
                snapshot_id=request.snapshot_id,
                dest=request.dest,
            )
        )

    return router


def _is_path_within(path: Path, parent: Path) -> bool:
    return is_path_within(path, parent)
