from __future__ import annotations

from backend.async_cleanup import to_thread_cancel_safe

import asyncio
from contextlib import suppress
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
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
    WorkspaceProjectIndexResponse,
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
from .executor_editor import ExecutorEditorService
from .text_search import WorkspaceTextSearchRequest, search_workspace_text
from .editor_language import EditorLanguageRequest, EditorLanguageLifecycleOperation, editor_language_request, editor_language_lifecycle
from .editor_ai import EditorCompletionRequest, editor_completion_response
from .delivery import GitDeliveryRequest, git_delivery_action, git_delivery_status
from .git_message_generation import GitMessageRequest, generate_git_message
from .environment_setup import EnvironmentSetupRequest, environment_setup_response, environment_setup_status


def create_workspace_router() -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])

    def _service(workspace_root: str) -> WorkspaceService:
        from backend.workspace.trust import is_workspace_trusted

        value = str(workspace_root or "").strip()
        if not value:
            raise HTTPException(status_code=422, detail="workspace_root is required")
        root = Path(value).expanduser().resolve()
        if not root.is_dir():
            raise HTTPException(status_code=409, detail="Workspace folder does not exist.")
        if not is_workspace_trusted(root):
            raise HTTPException(status_code=403, detail="Workspace folder is not trusted.")
        return WorkspaceService(get_workspace_root=lambda: root)

    def _resolve_git_root(path: str, workspace_root: str) -> tuple[Path, Path]:
        """git 端点优先使用调用方显式传入的目录，避免全局工作区切换的竞态。"""
        service = _service(workspace_root)
        owner_root = service.workspace_root_path()
        return resolve_workspace_git_root(path, owner_root), owner_root

    async def _request_service(request: Request):
        from backend.api import _state
        from backend.environments.errors import ExecutorError, ExecutorRpcError
        from backend.sandbox.runner import SandboxUnavailableError
        from backend.services.workspace_api_service import capture_workspace_execution
        from backend.workspace.executor_editor import ExecutorEditorService
        from backend.workspace.executor_paths import ExecutorPath

        params = request.query_params
        session_id, conversation_id = params.get("session_id", ""), params.get("conversation_id", "")
        root = params.get("workspace_root") or params.get("execution_cwd", "")
        browser = request.url.path.rsplit("/", 1)[-1].startswith("browser-")
        if not session_id and not conversation_id:
            configured = _state.bootstrap.environment_manager if _state.bootstrap is not None else None
            if root.startswith("file:") or params.get("environment_id") or configured is not None and configured.default_environment_id != "local":
                raise HTTPException(409, "A connected session and conversation owner are required for executor workspace access.")
            yield _service(root)
            return
        if not session_id or not conversation_id:
            raise HTTPException(422, "session_id and conversation_id are required together.")
        session = _state.ws_manager.get_session(session_id)
        if session is None:
            raise HTTPException(404, "Session is not connected.")
        if conversation_id != session.active_conversation_id:
            raise HTTPException(409, "Workspace conversation owner is stale.")
        conversation = await asyncio.to_thread(session.conversation_repo.get_conversation_summary, conversation_id)
        if conversation is None or conversation.archived:
            raise HTTPException(404, "The target conversation is not available.")
        bound = conversation.execution_cwd or conversation.worktree_path or conversation.workspace_root
        try:
            async with capture_workspace_execution(session, conversation_id, environment_id=params.get("environment_id")) as context:
                if context is None:
                    if root.startswith("file:"):
                        raise HTTPException(409, "No usable execution environment is attached to this workspace URI.")
                    yield (_browser_service(session_id, conversation_id, root) if browser else _service(root))
                    return
                scope = context.permission.environment_scope
                bound_matches = (ExecutorPath.parse(root) == ExecutorPath.parse(scope.cwd_uri) if root.startswith("file:")
                    else bool(bound) and not bound.startswith("file:") and Path(root).expanduser().resolve() == Path(bound).expanduser().resolve())
                if not bound_matches:
                    raise HTTPException(409, "Workspace execution owner is stale.")
                editor_root = None
                if not context.turn_environment.is_remote:
                    from backend.workspace.trust import is_workspace_trusted
                    local_root = ExecutorPath.parse(scope.cwd_uri).to_local_path()
                    if not is_workspace_trusted(local_root):
                        raise HTTPException(403, "Workspace folder is not trusted.")
                    if browser and not conversation.workspace_root and not conversation.worktree_path and conversation.projectless_browser_root:
                        editor_root = ExecutorPath.parse(_browser_service(session_id, conversation_id, root).workspace_root_path().as_uri())
                service = ExecutorEditorService(context, application_config=session.config, editor_root=editor_root)
                try:
                    yield service
                except SandboxUnavailableError as error:
                    raise HTTPException(503, {"code": "sandbox_unavailable", "message": str(error),
                        "environment_id": context.environment_id}) from error
                finally:
                    await service.aclose()
        except ExecutorRpcError as error:
            raise HTTPException(404 if error.code == -32004 else 502, {"message": str(error), "executor_code": error.code}) from error
        except ExecutorError as error:
            raise HTTPException(503, str(error)) from error
        except PermissionError as error:
            raise HTTPException(403, str(error)) from error
        except (FileNotFoundError, IsADirectoryError) as error:
            raise HTTPException(404, str(error)) from error
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    async def _call(service, method, *args, **kwargs):
        from backend.workspace.executor_editor import ExecutorEditorService
        if isinstance(service, ExecutorEditorService):
            return await getattr(service, method)(*args, **kwargs)
        return await to_thread_cancel_safe(getattr(service, method), *args, **kwargs)

    def _local_service(service):
        from backend.workspace.executor_editor import ExecutorEditorService
        if isinstance(service, ExecutorEditorService):
            if service.context.turn_environment.is_remote:
                raise HTTPException(503, "This operation requires a service configured on the selected executor.")
            return WorkspaceService(get_workspace_root=lambda: service.cwd.to_local_path())
        return service

    @router.get("/tree", response_model=WorkspaceTreeResponse)
    async def workspace_tree_api(
        path: str = Query(".", min_length=1),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceTreeResponse:
        # Directory walks stay off the event loop (cc refreshes file
        # suggestions in the background; to_thread is the minimal equivalent).
        return await _call(service, "list_tree", path)

    def _browser_service(session_id: str, conversation_id: str, execution_cwd: str) -> WorkspaceService:
        from backend.api import _state

        session = _state.ws_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session is not connected.")
        if conversation_id != session.active_conversation_id:
            raise HTTPException(status_code=409, detail="File browser conversation owner is stale.")
        conversation = session.conversation_repo.get_conversation_summary(conversation_id)
        if conversation is None or conversation.archived:
            raise HTTPException(status_code=404, detail="The target conversation is not available.")
        if not conversation.execution_cwd:
            raise HTTPException(status_code=409, detail="The conversation has no fixed execution owner.")
        actual_directory = Path(conversation.execution_cwd).expanduser().resolve()
        if actual_directory != Path(execution_cwd).expanduser().resolve():
            raise HTTPException(status_code=409, detail="File browser execution owner is stale.")
        execution_service = _service(str(actual_directory))
        if conversation.workspace_root or conversation.worktree_path or not conversation.projectless_browser_root:
            return execution_service
        browser_root = Path(conversation.projectless_browser_root).expanduser().resolve()
        return WorkspaceService(get_workspace_root=lambda: browser_root)

    @router.get("/browser-tree", response_model=WorkspaceTreeResponse)
    async def projectless_browser_tree_api(
        session_id: str = Query(..., min_length=1),
        conversation_id: str = Query(..., min_length=1),
        execution_cwd: str = Query(..., min_length=1),
        path: str = Query(".", min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceTreeResponse:
        return await _call(service, "list_tree", path)

    @router.get("/browser-file", response_model=WorkspaceFileResponse)
    async def projectless_browser_file_api(
        session_id: str = Query(..., min_length=1),
        conversation_id: str = Query(..., min_length=1),
        execution_cwd: str = Query(..., min_length=1),
        path: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceFileResponse:
        return await _call(service, "read_file", path)

    @router.get("/browser-preview")
    async def projectless_browser_preview_api(
        session_id: str = Query(..., min_length=1),
        conversation_id: str = Query(..., min_length=1),
        execution_cwd: str = Query(..., min_length=1),
        path: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> dict[str, object]:
        return await _call(service, "preview_file", path)

    @router.get("/browser-raw")
    async def projectless_browser_raw_api(
        session_id: str = Query(..., min_length=1),
        conversation_id: str = Query(..., min_length=1),
        execution_cwd: str = Query(..., min_length=1),
        path: str = Query(..., min_length=1),
        raw_token: str | None = Query(None),
        service=Depends(_request_service),
    ) -> FileResponse:
        _ = raw_token
        return await _call(service, "raw_file_response", path)

    @router.get("/search", response_model=WorkspaceSearchResponse)
    async def workspace_search_api(
        query: str = Query(..., min_length=1),
        limit: int = Query(20, ge=1, le=100),
        include_tests: bool = Query(True),
        kind: str = Query("file", pattern="^(file|folder|all)$"),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceSearchResponse:
        if isinstance(service, ExecutorEditorService):
            return WorkspaceSearchResponse(**(await service.search(query, limit=limit, include_tests=include_tests, kind=kind)))
        try:
            payload = await asyncio.to_thread(
                workspace_search_payload,
                root=service.workspace_root_path(),
                query=query,
                limit=limit,
                include_tests=include_tests,
                kind=kind,
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Workspace search failed: {exc}") from exc
        return WorkspaceSearchResponse(**payload)

    @router.post("/search-content")
    async def workspace_text_search_api(
        request: WorkspaceTextSearchRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> dict:
        if isinstance(service, ExecutorEditorService):
            return await service.search_content(request)
        return await asyncio.to_thread(search_workspace_text, service, request)

    @router.post("/language")
    async def workspace_language_api(
        request: EditorLanguageRequest,
        http_request: Request,
        workspace_root: str = Query(..., min_length=1),
        window_binding_token: str = Header(default="", alias="x-minicode-window-binding-token"),
        service=Depends(_request_service),
    ) -> dict:
        from .editor_language import editor_language_http_request
        target = service if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote else _local_service(service)
        return await editor_language_http_request(http_request, target, request, binding_token=window_binding_token)

    @router.post("/language/lifecycle")
    async def workspace_language_lifecycle_api(
        request: EditorLanguageLifecycleOperation,
        window_binding_token: str = Header(default="", alias="x-minicode-window-binding-token"),
        desktop_lifecycle_token: str = Header(default="", alias="x-minicode-desktop-lifecycle-token"),
    ) -> dict:
        return await editor_language_lifecycle(request, binding_token=window_binding_token, desktop_lifecycle_token=desktop_lifecycle_token)

    @router.post("/inline-completion")
    async def workspace_inline_completion_api(
        request: EditorCompletionRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ):
        if isinstance(service, ExecutorEditorService):
            from .editor_ai import executor_editor_completion_response
            return await executor_editor_completion_response(service, request)
        return editor_completion_response(service, request)

    @router.get("/git/delivery")
    async def workspace_git_delivery_status(workspace_root: str = Query(..., min_length=1), service=Depends(_request_service)):
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_delivery import executor_delivery_status
            return await executor_delivery_status(service)
        return await git_delivery_status(_local_service(service).workspace_root_path())

    @router.get("/environment")
    async def workspace_environment_status(workspace_root: str = Query(..., min_length=1), service=Depends(_request_service)):
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_environment import executor_environment_status
            result = await executor_environment_status(service)
            return {key: value for key, value in result.items() if key != "programs"}
        return environment_setup_status(_local_service(service).workspace_root_path())

    @router.post("/environment/setup")
    async def workspace_environment_setup(request: EnvironmentSetupRequest, workspace_root: str = Query(..., min_length=1), service=Depends(_request_service)):
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_environment import executor_environment_setup
            return await executor_environment_setup(service, request)
        return environment_setup_response(_local_service(service).workspace_root_path(), request)

    @router.post("/git/delivery")
    async def workspace_git_delivery_action(request: GitDeliveryRequest, http_request: Request, workspace_root: str = Query(..., min_length=1), service=Depends(_request_service)):
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_delivery import executor_delivery_action
            from .executor_commands import workbench_http_request
            return await workbench_http_request(http_request, executor_delivery_action(service, request))
        return await git_delivery_action(_local_service(service).workspace_root_path(), request)

    @router.post("/git/generate-message")
    async def workspace_git_generate_message(request: GitMessageRequest, http_request: Request, workspace_root: str = Query(..., min_length=1), service=Depends(_request_service)):
        from .executor_commands import workbench_http_request
        return await workbench_http_request(http_request, generate_git_message(service, request))

    @router.get("/project-index", response_model=WorkspaceProjectIndexResponse)
    async def workspace_project_index_api(
        request: Request,
        workspace_root: str = Query(..., min_length=1),
        include_dependencies: bool = Query(False),
        include_dependency_paths: bool = Query(False),
        previous_revision: int | None = Query(None, ge=0),
        previous_instance_id: str | None = Query(None),
        service=Depends(_request_service),
    ) -> WorkspaceProjectIndexResponse:
        from .project_index_runtime import project_index_runtime

        async def wait_for_disconnect() -> None:
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        scan = asyncio.create_task(service.project_index(include_dependencies=include_dependencies, include_dependency_paths=include_dependency_paths)
            if isinstance(service, ExecutorEditorService) else project_index_runtime.read(service, include_dependencies=include_dependencies, include_dependency_paths=include_dependency_paths, previous_revision=previous_revision, previous_instance_id=previous_instance_id))
        disconnect = asyncio.create_task(wait_for_disconnect())
        try:
            completed, _ = await asyncio.wait((scan, disconnect), return_when=asyncio.FIRST_COMPLETED)
            if scan not in completed:
                scan.cancel()
                with suppress(asyncio.CancelledError):
                    await scan
                raise HTTPException(status_code=499, detail="Workspace project scan cancelled because the client disconnected.")
            return await scan
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Workspace project scan failed: {exc}") from exc
        finally:
            disconnect.cancel()
            with suppress(asyncio.CancelledError):
                await disconnect
            if not scan.done():
                scan.cancel()
                with suppress(asyncio.CancelledError):
                    await scan

    @router.get("/file", response_model=WorkspaceFileResponse)
    async def workspace_read_file_api(
        path: str = Query(..., min_length=1),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceFileResponse:
        # Blocking disk I/O + sha256 must not stall the event loop while a
        # conversation streams tokens over the same process's WebSocket.
        return await _call(service, "read_file", path)

    @router.get("/raw")
    async def workspace_raw_file_api(
        path: str = Query(..., min_length=1),
        workspace_root: str = Query(..., min_length=1),
        raw_token: str | None = Query(None),
        service=Depends(_request_service),
    ) -> FileResponse:
        _ = raw_token
        return await _call(service, "raw_file_response", path)

    @router.get("/preview")
    async def workspace_preview_file_api(
        path: str = Query(..., min_length=1),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> dict[str, object]:
        """Return a bounded, parser-backed preview for a workspace deliverable."""
        return await _call(service, "preview_file", path)

    @router.put("/file", response_model=WorkspaceFileResponse)
    async def workspace_write_file_api(
        request: WorkspaceFileUpdateRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceFileResponse:
        return await _call(
            service, "write_file",
            path=request.path,
            content=request.content,
        )

    @router.put("/file/compare-write", response_model=WorkspaceFileResponse)
    async def workspace_compare_write_file_api(
        request: WorkspaceFileCompareWriteRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceFileResponse:
        return await _call(
            service, "compare_and_write_file",
            path=request.path,
            expected_hash=request.expected_hash,
            content=request.content,
        )

    @router.post("/directory", response_model=WorkspacePathResponse)
    async def workspace_create_directory_api(
        request: WorkspaceDirectoryCreateRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspacePathResponse:
        return await _call(service, "create_directory", request.path)

    @router.post("/rename", response_model=WorkspacePathResponse)
    async def workspace_rename_path_api(
        request: WorkspacePathRenameRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspacePathResponse:
        return await _call(
            service, "rename_path",
            path=request.path,
            new_path=request.new_path,
        )

    @router.delete("/path", response_model=WorkspaceDeleteResponse)
    async def workspace_delete_path_api(
        path: str = Query(..., min_length=1),
        recursive: bool = Query(False),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceDeleteResponse:
        return await _call(
            service, "delete_path",
            path=path,
            recursive=recursive,
        )

    @router.post("/import", response_model=ProjectImportResponse)
    async def import_project_api(request: ProjectImportRequest) -> ProjectImportResponse:
        """导入项目文件夹，构建上下文"""
        try:
            return ProjectImportResponse(**(await import_project_payload(request.path)))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
        return await to_thread_cancel_safe(remove_recent_project_payload, path)

    @router.get("/git/status")
    async def git_status_api(
        path: str = Query(""),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> dict:
        """返回 git 状态：分支、已修改/暂存/未跟踪文件列表"""
        # git subprocesses are blocking calls with multi-second timeouts; keep
        # them off the event loop so streaming is never stalled by status polls.
        if isinstance(service, ExecutorEditorService):
            return await service.git("status", path=path)
        root, owner_root = await asyncio.to_thread(_resolve_git_root, path, workspace_root)
        return await asyncio.to_thread(workspace_git_status_payload, root, workspace_root=owner_root)

    @router.get("/git/diff")
    async def git_diff_api(
        file: str = Query(""),
        path: str = Query(""),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> dict:
        """返回指定文件的 git diff"""
        if isinstance(service, ExecutorEditorService):
            return await service.git("diff", path=path, file=file)
        root, owner_root = await asyncio.to_thread(_resolve_git_root, path, workspace_root)
        if file and not is_path_within((root / file).resolve(), root):
            raise HTTPException(status_code=400, detail="Git file is outside workspace root.")
        return await asyncio.to_thread(workspace_git_diff_payload, root, file, workspace_root=owner_root)

    @router.get("/git/worktree", response_model=WorkspaceGitWorktreeResponse)
    async def git_worktree_api(
        path: str = Query(""),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceGitWorktreeResponse:
        """Return linked worktree metadata for the active workspace."""
        if isinstance(service, ExecutorEditorService):
            return WorkspaceGitWorktreeResponse(**(await service.git("worktree", path=path)))
        local = _local_service(service)
        root, owner_root = await asyncio.to_thread(_resolve_git_root, path, str(local.workspace_root_path()))
        return WorkspaceGitWorktreeResponse(**(await asyncio.to_thread(workspace_git_worktree_payload, root, workspace_root=owner_root)))

    @router.post("/git/worktree/switch", response_model=ProjectImportResponse)
    async def git_worktree_switch_api(
        request: WorkspaceGitWorktreeSwitchRequest,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> ProjectImportResponse:
        """Switch the active workspace to an existing git worktree."""
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_worktree import executor_worktree_action
            return ProjectImportResponse(**(await executor_worktree_action(service, "switch", path=request.path)))
        return ProjectImportResponse(
            **(await switch_workspace_git_worktree_payload(_local_service(service).workspace_root_path(), request.path))
        )

    @router.delete("/git/worktree", response_model=WorkspaceGitWorktreeRemoveResponse)
    async def git_worktree_remove_api(
        http_request: Request,
        path: str = Query(..., min_length=1),
        force: bool = Query(False),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceGitWorktreeRemoveResponse:
        """Remove a MiniCode isolated worktree."""
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_worktree import executor_worktree_action
            from .executor_commands import workbench_http_request
            return WorkspaceGitWorktreeRemoveResponse(**(await workbench_http_request(http_request,
                executor_worktree_action(service, "remove", path=path, force=force))))
        return WorkspaceGitWorktreeRemoveResponse(
            **(await to_thread_cancel_safe(
                remove_workspace_git_worktree_payload,
                root=_local_service(service).workspace_root_path(),
                path=path,
                force=force,
            ))
        )

    @router.get("/git/worktree/snapshots", response_model=WorkspaceGitWorktreeSnapshotsResponse)
    async def git_worktree_snapshots_api(
        conversation_id: str | None = Query(None),
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceGitWorktreeSnapshotsResponse:
        """List pre-deletion worktree snapshots, newest first."""
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_worktree import executor_worktree_action
            return WorkspaceGitWorktreeSnapshotsResponse(**(await executor_worktree_action(service, "snapshots", conversation_id=conversation_id)))
        root = await asyncio.to_thread(_local_service(service).workspace_root_path)
        payload = await asyncio.to_thread(
            workspace_git_worktree_snapshots_payload,
            root,
            conversation_id,
        )
        return WorkspaceGitWorktreeSnapshotsResponse(**payload)

    @router.post("/git/worktree/restore", response_model=WorkspaceGitWorktreeRestoreResponse)
    async def git_worktree_restore_api(
        request: WorkspaceGitWorktreeRestoreRequest,
        http_request: Request,
        workspace_root: str = Query(..., min_length=1),
        service=Depends(_request_service),
    ) -> WorkspaceGitWorktreeRestoreResponse:
        """Restore a worktree snapshot to a detached worktree."""
        if isinstance(service, ExecutorEditorService) and service.context.turn_environment.is_remote:
            from .executor_worktree import executor_worktree_action
            from .executor_commands import workbench_http_request
            return WorkspaceGitWorktreeRestoreResponse(**(await workbench_http_request(http_request,
                executor_worktree_action(service, "restore", snapshot_id=request.snapshot_id, dest=request.dest))))
        return WorkspaceGitWorktreeRestoreResponse(
            **(await to_thread_cancel_safe(
                restore_workspace_git_worktree_snapshot_payload,
                root=_local_service(service).workspace_root_path(),
                snapshot_id=request.snapshot_id,
                dest=request.dest,
            ))
        )

    return router


def _is_path_within(path: Path, parent: Path) -> bool:
    return is_path_within(path, parent)


