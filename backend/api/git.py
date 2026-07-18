"""
Git API

Git 分支管理相关的 API 端点
"""

from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.services.git_api_service import (
    GitApiServiceError,
    checkout_git_branch,
    get_current_git_branch_payload,
    get_git_status_payload,
    git_env_for_workspace,
    is_inside_git_worktree,
    list_git_branches,
    resolve_git_cwd,
)

router = APIRouter(prefix="/api/git", tags=["git"])


class GitBranchInfo(BaseModel):
    name: str
    current: bool = False
    remote: bool = False


class GitCheckoutRequest(BaseModel):
    branch: str
    create: bool = False


def _to_http_error(exc: GitApiServiceError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _resolve_git_cwd(workspace_path: str | None) -> Path:
    try:
        return resolve_git_cwd(workspace_path)
    except GitApiServiceError as exc:
        raise _to_http_error(exc) from exc


def _git_env_for_workspace(cwd: Path) -> dict[str, str]:
    return git_env_for_workspace(cwd)


def _is_inside_git_worktree(cwd: Path) -> bool:
    return is_inside_git_worktree(cwd)


@router.get("/branches")
async def get_git_branches(workspace_path: str | None = None) -> List[GitBranchInfo]:
    """获取 Git 分支列表"""
    try:
        return [GitBranchInfo(**branch.to_dict()) for branch in list_git_branches(workspace_path)]
    except GitApiServiceError as exc:
        raise _to_http_error(exc) from exc


@router.post("/checkout")
async def checkout_branch(request: GitCheckoutRequest, workspace_path: str | None = None) -> dict:
    """切换 Git 分支"""
    try:
        return checkout_git_branch(
            branch=request.branch,
            create=request.create,
            workspace_path=workspace_path,
        )
    except GitApiServiceError as exc:
        raise _to_http_error(exc) from exc


@router.get("/status")
async def get_git_status(workspace_path: str | None = None) -> dict:
    """获取 Git 状态"""
    try:
        return get_git_status_payload(workspace_path)
    except GitApiServiceError as exc:
        raise _to_http_error(exc) from exc


@router.get("/current-branch")
async def get_current_branch(workspace_path: str | None = None) -> dict:
    """获取当前分支名称"""
    try:
        return get_current_git_branch_payload(workspace_path)
    except GitApiServiceError as exc:
        raise _to_http_error(exc) from exc
