"""
Git API

Git 分支管理相关的 API 端点
"""

from pathlib import Path
from typing import List
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import subprocess
import os

from backend.runtime_env import sanitized_subprocess_env
from backend.workspace.state import get_active_workspace_root

router = APIRouter(prefix="/api/git", tags=["git"])


class GitBranchInfo(BaseModel):
    name: str
    current: bool = False
    remote: bool = False


class GitCheckoutRequest(BaseModel):
    branch: str
    create: bool = False


def _resolve_git_cwd(workspace_path: str | None) -> Path:
    active_root = get_active_workspace_root(Path(os.getcwd())).resolve()
    cwd = Path(workspace_path or active_root).resolve()
    if not cwd.exists() or not cwd.is_dir():
        raise HTTPException(status_code=404, detail="Workspace path not found.")
    try:
        cwd.relative_to(active_root)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="Workspace path is outside the active workspace.") from exc
    return cwd


def _git_env_for_workspace(cwd: Path) -> dict[str, str]:
    env = sanitized_subprocess_env()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_CEILING_DIRECTORIES"] = str(cwd.parent)
    return env


def _is_inside_git_worktree(cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=_git_env_for_workspace(cwd),
            capture_output=True,
            text=True, encoding="utf-8",
            timeout=3,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


@router.get("/branches")
async def get_git_branches(workspace_path: str | None = None) -> List[GitBranchInfo]:
    """获取 Git 分支列表"""
    try:
        # 获取当前工作目录
        cwd = _resolve_git_cwd(workspace_path)
        if not _is_inside_git_worktree(cwd):
            return []

        # 执行 git branch 命令
        result = subprocess.run(
            ["git", "branch", "-a"],
            cwd=cwd,
            env=_git_env_for_workspace(cwd),
            capture_output=True,
            text=True, encoding="utf-8",
            check=True
        )

        branches = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue

            is_current = line.startswith('*')
            branch_name = line.replace('*', '').strip()

            # 跳过 HEAD 指针
            if 'HEAD' in branch_name:
                continue

            # 处理远程分支
            is_remote = branch_name.startswith('remotes/')
            if is_remote:
                branch_name = branch_name.replace('remotes/origin/', '')

            # 避免重复
            if not any(b.name == branch_name for b in branches):
                branches.append(GitBranchInfo(
                    name=branch_name,
                    current=is_current,
                    remote=is_remote
                ))

        return branches
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git command failed: {e.stderr}")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="Git not found")


@router.post("/checkout")
async def checkout_branch(request: GitCheckoutRequest, workspace_path: str | None = None) -> dict:
    """切换 Git 分支"""
    try:
        cwd = _resolve_git_cwd(workspace_path)
        if not _is_inside_git_worktree(cwd):
            raise HTTPException(status_code=400, detail="Workspace is not a Git repository.")

        # 构建 git checkout 命令
        cmd = ["git", "checkout"]
        if request.create:
            cmd.append("-b")
        cmd.append(request.branch)

        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=_git_env_for_workspace(cwd),
            capture_output=True,
            text=True, encoding="utf-8",
            check=True
        )

        return {
            "status": "success",
            "branch": request.branch,
            "message": result.stdout.strip()
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git checkout failed: {e.stderr}")


@router.get("/status")
async def get_git_status(workspace_path: str | None = None) -> dict:
    """获取 Git 状态"""
    try:
        cwd = _resolve_git_cwd(workspace_path)
        if not _is_inside_git_worktree(cwd):
            return {
                "has_changes": False,
                "changes": [],
                "error": "Workspace is not a Git repository.",
            }

        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            env=_git_env_for_workspace(cwd),
            capture_output=True,
            text=True, encoding="utf-8",
            check=True
        )

        # 解析状态
        changes = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            status = line[:2]
            file_path = line[3:]
            changes.append({
                "status": status.strip(),
                "path": file_path
            })

        return {
            "has_changes": len(changes) > 0,
            "changes": changes
        }
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"Git status failed: {e.stderr}")


@router.get("/current-branch")
async def get_current_branch(workspace_path: str | None = None) -> dict:
    """获取当前分支名称"""
    try:
        cwd = _resolve_git_cwd(workspace_path)
        if not _is_inside_git_worktree(cwd):
            return {"branch": None}

        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=cwd,
            env=_git_env_for_workspace(cwd),
            capture_output=True,
            text=True, encoding="utf-8",
            check=True
        )

        return {
            "branch": result.stdout.strip()
        }
    except subprocess.CalledProcessError:
        return {"branch": None}
