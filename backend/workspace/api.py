from __future__ import annotations

from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from .models import (
    WorkspaceDeleteResponse,
    WorkspaceDirectoryCreateRequest,
    WorkspaceFileResponse,
    WorkspaceFileCompareWriteRequest,
    WorkspaceSearchResponse,
    WorkspaceSearchResultResponse,
    WorkspaceGitWorktreeEntryResponse,
    WorkspaceGitWorktreeRemoveResponse,
    WorkspaceGitWorktreeResponse,
    WorkspaceGitWorktreeSwitchRequest,
    WorkspaceGitWorktreeSnapshotsResponse,
    WorkspaceGitWorktreeRestoreRequest,
    WorkspaceGitWorktreeRestoreResponse,
    WorktreeSnapshotEntryResponse,
    WorkspaceFileUpdateRequest,
    WorkspacePathRenameRequest,
    WorkspacePathResponse,
    WorkspaceTreeResponse,
    ProjectImportRequest,
    ProjectImportResponse,
)
from .service import WorkspaceService
from .context import WorkspaceContext
from .state import get_active_workspace_root, set_active_workspace_root
from .path_utils import build_missing_path_hint, normalize_project_import_path
from .worktree import WorktreeManager, summarize_worktree_status
from .fuzzy_search import get_global_fuzzy_search


def _search_workspace_directories(
    root: Path,
    query: str,
    limit: int,
) -> list[WorkspaceSearchResultResponse]:
    query_lower = query.strip().lower()
    if not query_lower:
        return []

    ignored = {
        ".git",
        ".idea",
        ".vscode",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
    query_chars = set(query_lower)
    results: list[WorkspaceSearchResultResponse] = []

    for path in root.rglob("*"):
        if not path.is_dir():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if not rel_parts:
            continue
        if any(part in ignored or part.startswith(".") for part in rel_parts):
            continue

        rel = path.relative_to(root).as_posix()
        rel_lower = rel.lower()
        if query_lower not in rel_lower and not query_chars.issubset(set(rel_lower)):
            continue

        name_lower = path.name.lower()
        score = 10.0
        if name_lower == query_lower:
            score += 100
        if name_lower.startswith(query_lower):
            score += 70
        if query_lower in name_lower:
            score += 50
        if query_lower in rel_lower:
            score += 20
        score -= min(len(rel_parts), 12) * 1.5

        results.append(
            WorkspaceSearchResultResponse(
                path=rel,
                name=path.name,
                score=score,
                matched_indices=[],
                kind="folder",
            )
        )

    results.sort(key=lambda item: (-item.score, item.path))
    return results[:limit]


def create_workspace_router(get_project_root: Callable[[], Path]) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["workspace"])
    service = WorkspaceService(
        get_workspace_root=lambda: get_active_workspace_root(get_project_root().resolve())
    )

    def _resolve_git_root(path: str) -> Path:
        """git 端点优先使用调用方显式传入的目录，避免全局工作区切换的竞态。"""
        candidate = str(path or "").strip()
        if candidate:
            resolved = Path(candidate).expanduser()
            try:
                resolved = resolved.resolve()
            except OSError:
                resolved = None
            if resolved is not None and resolved.is_dir():
                return resolved
        return service.workspace_root_path()

    @router.get("/tree", response_model=WorkspaceTreeResponse)
    async def workspace_tree_api(path: str = Query(".", min_length=1)) -> WorkspaceTreeResponse:
        return service.list_tree(path)

    @router.get("/search", response_model=WorkspaceSearchResponse)
    async def workspace_search_api(
        query: str = Query(..., min_length=1),
        limit: int = Query(20, ge=1, le=100),
        include_tests: bool = Query(True),
        kind: str = Query("file", pattern="^(file|folder|all)$"),
    ) -> WorkspaceSearchResponse:
        root = service.workspace_root_path()
        results: list[WorkspaceSearchResultResponse] = []
        if kind in {"file", "all"}:
            engine = get_global_fuzzy_search(root)
            matches = engine.search(query=query, max_results=limit, include_tests=include_tests)
            results.extend(
                WorkspaceSearchResultResponse(
                    path=str(match.path.relative_to(root)).replace("\\", "/"),
                    name=match.path.name,
                    score=match.score,
                    matched_indices=match.matched_indices,
                    kind="file",
                )
                for match in matches
            )
        if kind in {"folder", "all"}:
            results.extend(_search_workspace_directories(root, query, limit))
        results.sort(key=lambda item: (-item.score, item.path))
        return WorkspaceSearchResponse(
            query=query,
            results=results[:limit],
        )

    @router.get("/file", response_model=WorkspaceFileResponse)
    async def workspace_read_file_api(path: str = Query(..., min_length=1)) -> WorkspaceFileResponse:
        return service.read_file(path)

    @router.get("/raw")
    async def workspace_raw_file_api(path: str = Query(..., min_length=1)) -> FileResponse:
        return service.raw_file_response(path)

    @router.put("/file", response_model=WorkspaceFileResponse)
    async def workspace_write_file_api(request: WorkspaceFileUpdateRequest) -> WorkspaceFileResponse:
        return service.write_file(path=request.path, content=request.content)

    @router.put("/file/compare-write", response_model=WorkspaceFileResponse)
    async def workspace_compare_write_file_api(request: WorkspaceFileCompareWriteRequest) -> WorkspaceFileResponse:
        return service.compare_and_write_file(
            path=request.path,
            expected_hash=request.expected_hash,
            content=request.content,
        )

    @router.post("/directory", response_model=WorkspacePathResponse)
    async def workspace_create_directory_api(request: WorkspaceDirectoryCreateRequest) -> WorkspacePathResponse:
        return service.create_directory(request.path)

    @router.post("/rename", response_model=WorkspacePathResponse)
    async def workspace_rename_path_api(request: WorkspacePathRenameRequest) -> WorkspacePathResponse:
        return service.rename_path(path=request.path, new_path=request.new_path)

    @router.delete("/path", response_model=WorkspaceDeleteResponse)
    async def workspace_delete_path_api(
        path: str = Query(..., min_length=1),
        recursive: bool = Query(False),
    ) -> WorkspaceDeleteResponse:
        return service.delete_path(path=path, recursive=recursive)

    @router.post("/import", response_model=ProjectImportResponse)
    async def import_project_api(request: ProjectImportRequest) -> ProjectImportResponse:
        """导入项目文件夹，构建上下文"""
        normalized_path = normalize_project_import_path(request.path)
        workspace_ctx = WorkspaceContext(normalized_path)
        metadata = await workspace_ctx.initialize()
        set_active_workspace_root(metadata.root_path)

        # 记录到最近项目
        from .recent_projects import RecentProjectStore
        store = RecentProjectStore()
        store.add(
            path=str(metadata.root_path),
            name=metadata.name,
            project_type=metadata.project_type,
        )

        return ProjectImportResponse(
            success=True,
            project=workspace_ctx.to_dict(),
            summary=workspace_ctx.get_project_summary(),
            file_count=metadata.file_count,
        )

    @router.post("/validate")
    async def validate_path_api(request: ProjectImportRequest) -> dict:
        """验证路径有效性"""
        path = normalize_project_import_path(request.path)
        if not path.exists():
            payload: dict[str, object] = {
                "valid": False,
                "error": "路径不存在",
                "normalized_path": str(path),
            }
            hint = build_missing_path_hint(request.path)
            if hint:
                payload["hint"] = hint
            return payload
        if not path.is_dir():
            return {
                "valid": False,
                "error": "不是目录",
                "normalized_path": str(path),
            }

        # 快速检测项目类型
        project_type = "unknown"
        if (path / "pyproject.toml").exists() or (path / "setup.py").exists():
            project_type = "python"
        elif (path / "package.json").exists():
            project_type = "node"
        elif (path / "Cargo.toml").exists():
            project_type = "rust"
        elif (path / "go.mod").exists():
            project_type = "go"

        return {
            "valid": True,
            "name": path.name,
            "project_type": project_type,
            "has_git": (path / ".git").is_dir(),
            "normalized_path": str(path),
        }

    @router.get("/recent")
    async def recent_projects_api(limit: int = Query(10, ge=1, le=20)) -> dict:
        """获取最近项目列表"""
        from .recent_projects import RecentProjectStore
        store = RecentProjectStore()
        projects = store.list(limit=limit)
        return {"projects": [p.to_dict() for p in projects]}

    @router.delete("/recent")
    async def remove_recent_project_api(path: str = Query(..., min_length=1)) -> dict:
        """Remove a project from the recent list without touching files on disk."""
        from .recent_projects import RecentProjectStore

        normalized_path = str(Path(path).resolve())
        store = RecentProjectStore()
        removed = store.remove(normalized_path)
        return {"removed": removed, "path": normalized_path}

    @router.get("/git/status")
    async def git_status_api(path: str = Query("")) -> dict:
        """返回 git 状态：分支、已修改/暂存/未跟踪文件列表"""
        root = _resolve_git_root(path)
        try:
            import subprocess

            from backend.runtime_env import sanitized_git_env
            result = subprocess.run(
                ["git", "status", "--porcelain=v1", "--branch"],
                cwd=root, env=sanitized_git_env(), capture_output=True, text=True, encoding="utf-8", timeout=5
            )
            lines = result.stdout.splitlines()
            branch = ""
            modified, staged, untracked = [], [], []
            for line in lines:
                if line.startswith("## "):
                    branch = line[3:].split("...")[0].strip()
                elif line.startswith("??"):
                    untracked.append(line[3:].strip())
                elif len(line) >= 2:
                    xy, path = line[:2], line[3:].strip()
                    if xy[1] != " ":
                        modified.append(path)
                    if xy[0] != " " and xy[0] != "?":
                        staged.append(path)
            return {"branch": branch, "modified": modified, "staged": staged, "untracked": untracked}
        except Exception as e:
            return {"branch": "", "modified": [], "staged": [], "untracked": [], "error": str(e)}

    @router.get("/git/diff")
    async def git_diff_api(file: str = Query(""), path: str = Query("")) -> dict:
        """返回指定文件的 git diff"""
        root = _resolve_git_root(path)
        try:
            import subprocess

            from backend.runtime_env import sanitized_git_env
            cmd = ["git", "diff", "HEAD", "--", file] if file else ["git", "diff", "HEAD"]
            result = subprocess.run(cmd, cwd=root, env=sanitized_git_env(), capture_output=True, text=True, encoding="utf-8", timeout=10)
            return {"diff": result.stdout}
        except Exception as e:
            return {"diff": "", "error": str(e)}

    @router.get("/git/worktree", response_model=WorkspaceGitWorktreeResponse)
    async def git_worktree_api(path: str = Query("")) -> WorkspaceGitWorktreeResponse:
        """Return linked worktree metadata for the active workspace."""
        root = _resolve_git_root(path)

        try:
            import subprocess

            from backend.runtime_env import sanitized_git_env

            manager = WorktreeManager(root)
            worktrees = manager.list_worktrees()
            status = summarize_worktree_status(root, worktrees)

            common_dir_result = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=root,
                env=sanitized_git_env(),
                capture_output=True,
                text=True, encoding="utf-8",
                timeout=5,
                check=True,
            )
            common_dir_raw = common_dir_result.stdout.strip()
            if common_dir_raw:
                common_dir_path = Path(common_dir_raw)
                common_dir = (
                    common_dir_path.resolve()
                    if common_dir_path.is_absolute()
                    else (root / common_dir_path).resolve()
                )
            else:
                common_dir = None

            isolated_root = (status.main_repo_path or root) / ".claude" / "worktrees"

            return WorkspaceGitWorktreeResponse(
                is_worktree=status.is_worktree,
                current_path=str(status.current_path),
                main_repo_path=str(status.main_repo_path) if status.main_repo_path else None,
                current_branch=status.current_branch,
                common_git_dir=str(common_dir) if common_dir else None,
                worktree_count=status.worktree_count,
                worktrees=[
                    WorkspaceGitWorktreeEntryResponse(
                        path=str(worktree.path),
                        branch=worktree.branch,
                        commit=worktree.commit[:8],
                        is_main=bool(status.main_repo_path and worktree.path == status.main_repo_path),
                        is_current=worktree.path == status.current_path,
                        is_detached=worktree.is_detached,
                        is_isolated=_is_path_within(worktree.path, isolated_root),
                        can_remove=(
                            _is_path_within(worktree.path, isolated_root)
                            and worktree.path != status.current_path
                        ),
                    )
                    for worktree in status.worktrees
                ],
            )
        except Exception as e:
            return WorkspaceGitWorktreeResponse(
                current_path=str(root),
                error=str(e),
            )

    @router.post("/git/worktree/switch", response_model=ProjectImportResponse)
    async def git_worktree_switch_api(request: WorkspaceGitWorktreeSwitchRequest) -> ProjectImportResponse:
        """Switch the active workspace to an existing git worktree."""
        target = Path(request.path).resolve()
        if not target.exists() or not target.is_dir():
            return ProjectImportResponse(success=False, project={}, summary="", file_count=0)

        try:
            manager = WorktreeManager(service.workspace_root_path())
            allowed_paths = {item.path.resolve() for item in manager.list_worktrees()}
            if target not in allowed_paths:
                return ProjectImportResponse(success=False, project={}, summary="", file_count=0)

            workspace_ctx = WorkspaceContext(target)
            metadata = await workspace_ctx.initialize()
            set_active_workspace_root(metadata.root_path)

            from .recent_projects import RecentProjectStore
            store = RecentProjectStore()
            store.add(
                path=str(metadata.root_path),
                name=metadata.name,
                project_type=metadata.project_type,
            )

            return ProjectImportResponse(
                success=True,
                project=workspace_ctx.to_dict(),
                summary=workspace_ctx.get_project_summary(),
                file_count=metadata.file_count,
            )
        except Exception:
            return ProjectImportResponse(success=False, project={}, summary="", file_count=0)

    @router.delete("/git/worktree", response_model=WorkspaceGitWorktreeRemoveResponse)
    async def git_worktree_remove_api(
        path: str = Query(..., min_length=1),
        force: bool = Query(False),
    ) -> WorkspaceGitWorktreeRemoveResponse:
        """Remove a MiniCode/Claude isolated worktree."""
        root = service.workspace_root_path()
        target = Path(path).resolve()

        try:
            manager = WorktreeManager(root)
            worktrees = manager.list_worktrees()
            status = summarize_worktree_status(root, worktrees)
            isolated_root = (status.main_repo_path or root) / ".claude" / "worktrees"
            target_entry = next((item for item in worktrees if item.path.resolve() == target), None)
            if target_entry is None:
                return WorkspaceGitWorktreeRemoveResponse(removed=False, path=str(target), error="Worktree not found")
            if target == status.current_path:
                return WorkspaceGitWorktreeRemoveResponse(removed=False, path=str(target), error="Cannot remove current worktree")
            if not _is_path_within(target, isolated_root):
                return WorkspaceGitWorktreeRemoveResponse(
                    removed=False,
                    path=str(target),
                    branch=target_entry.branch,
                    error="Only isolated worktrees under .claude/worktrees can be removed",
                )

            removal = manager.safe_remove_worktree(target, force=force)
            if removal.needs_force:
                return WorkspaceGitWorktreeRemoveResponse(
                    removed=False,
                    path=str(target),
                    branch=target_entry.branch,
                    error=removal.error or "Worktree has local changes; force is required",
                )
            return WorkspaceGitWorktreeRemoveResponse(
                removed=removal.removed,
                path=str(target),
                branch=target_entry.branch,
                snapshot_id=removal.snapshot.id if removal.snapshot else None,
                snapshot_ref=removal.snapshot.snapshot_ref if removal.snapshot else None,
                error=None if removal.removed else (removal.error or "git worktree remove failed"),
            )
        except Exception as e:
            return WorkspaceGitWorktreeRemoveResponse(removed=False, path=str(target), error=str(e))

    @router.get("/git/worktree/snapshots", response_model=WorkspaceGitWorktreeSnapshotsResponse)
    async def git_worktree_snapshots_api(
        conversation_id: str | None = Query(None),
    ) -> WorkspaceGitWorktreeSnapshotsResponse:
        """List pre-deletion worktree snapshots, newest first."""
        try:
            manager = WorktreeManager(service.workspace_root_path())
            records = manager.list_snapshots(conversation_id)
            return WorkspaceGitWorktreeSnapshotsResponse(
                snapshots=[
                    WorktreeSnapshotEntryResponse(
                        id=record.id,
                        conversation_id=record.conversation_id,
                        branch=record.branch,
                        original_path=record.original_path,
                        snapshot_sha=record.snapshot_sha,
                        snapshot_ref=record.snapshot_ref,
                        label=record.label,
                        created_at=record.created_at,
                    )
                    for record in records
                ],
            )
        except Exception as e:
            return WorkspaceGitWorktreeSnapshotsResponse(error=str(e))

    @router.post("/git/worktree/restore", response_model=WorkspaceGitWorktreeRestoreResponse)
    async def git_worktree_restore_api(
        request: WorkspaceGitWorktreeRestoreRequest,
    ) -> WorkspaceGitWorktreeRestoreResponse:
        """Restore a worktree snapshot to a detached worktree."""
        try:
            manager = WorktreeManager(service.workspace_root_path())
            result = manager.restore_snapshot(
                request.snapshot_id,
                dest=Path(request.dest) if request.dest else None,
            )
            return WorkspaceGitWorktreeRestoreResponse(
                restored=result.restored,
                snapshot_id=request.snapshot_id,
                path=str(result.path) if result.path else "",
                branch=result.snapshot.branch if result.snapshot else "",
                error=result.error,
            )
        except Exception as e:
            return WorkspaceGitWorktreeRestoreResponse(restored=False, snapshot_id=request.snapshot_id, error=str(e))

    return router


def _is_path_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
