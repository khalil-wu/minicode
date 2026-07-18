from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
from typing import Any

from backend.runtime_env import sanitized_git_env
from backend.workspace.state import get_active_workspace_root


class GitApiServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True)
class GitBranchRecord:
    name: str
    current: bool = False
    remote: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "current": self.current, "remote": self.remote}


def resolve_git_cwd(workspace_path: str | None) -> Path:
    active_root = get_active_workspace_root(Path(os.getcwd())).resolve()
    cwd = Path(workspace_path or active_root).resolve()
    if not cwd.exists() or not cwd.is_dir():
        raise GitApiServiceError(404, "Workspace path not found.")
    try:
        cwd.relative_to(active_root)
    except ValueError as exc:
        raise GitApiServiceError(403, "Workspace path is outside the active workspace.") from exc
    return cwd


def git_env_for_workspace(cwd: Path) -> dict[str, str]:
    # Repo discovery may ascend (opening a subdirectory of a repo must find the
    # enclosing repo) but stops at the user's home directory so a stray ~/.git
    # never masquerades as the workspace repo.
    return sanitized_git_env(cwd)


def is_inside_git_worktree(cwd: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=cwd,
            env=git_env_for_workspace(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=3,
        )
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, FileNotFoundError, OSError):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == "true"


def list_git_branches(workspace_path: str | None = None) -> list[GitBranchRecord]:
    cwd = resolve_git_cwd(workspace_path)
    if not is_inside_git_worktree(cwd):
        return []

    try:
        result = subprocess.run(
            ["git", "branch", "-a"],
            cwd=cwd,
            env=git_env_for_workspace(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitApiServiceError(500, f"Git command failed: {exc.stderr}") from exc
    except FileNotFoundError as exc:
        raise GitApiServiceError(500, "Git not found") from exc

    return parse_git_branches(result.stdout)


def parse_git_branches(stdout: str) -> list[GitBranchRecord]:
    branches: list[GitBranchRecord] = []
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue

        is_current = line.startswith("*")
        branch_name = line.replace("*", "").strip()

        if "HEAD" in branch_name:
            continue

        is_remote = branch_name.startswith("remotes/")
        if is_remote:
            branch_name = branch_name.replace("remotes/origin/", "")

        if not any(branch.name == branch_name for branch in branches):
            branches.append(GitBranchRecord(name=branch_name, current=is_current, remote=is_remote))

    return branches


def checkout_git_branch(*, branch: str, create: bool = False, workspace_path: str | None = None) -> dict[str, Any]:
    cwd = resolve_git_cwd(workspace_path)
    if not is_inside_git_worktree(cwd):
        raise GitApiServiceError(400, "Workspace is not a Git repository.")

    cmd = ["git", "checkout"]
    if create:
        cmd.append("-b")
    cmd.append(branch)

    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=git_env_for_workspace(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitApiServiceError(500, f"Git checkout failed: {exc.stderr}") from exc

    return {"status": "success", "branch": branch, "message": result.stdout.strip()}


def get_git_status_payload(workspace_path: str | None = None) -> dict[str, Any]:
    cwd = resolve_git_cwd(workspace_path)
    if not is_inside_git_worktree(cwd):
        return {
            "has_changes": False,
            "changes": [],
            "error": "Workspace is not a Git repository.",
        }

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            env=git_env_for_workspace(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise GitApiServiceError(500, f"Git status failed: {exc.stderr}") from exc

    changes = parse_git_status(result.stdout)
    return {"has_changes": len(changes) > 0, "changes": changes}


def parse_git_status(stdout: str) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for line in stdout.strip().split("\n"):
        if not line.strip():
            continue
        changes.append({"status": line[:2].strip(), "path": line[3:]})
    return changes


def get_current_git_branch_payload(workspace_path: str | None = None) -> dict[str, str | None]:
    cwd = resolve_git_cwd(workspace_path)
    if not is_inside_git_worktree(cwd):
        return {"branch": None}

    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=cwd,
            env=git_env_for_workspace(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
    except subprocess.CalledProcessError:
        return {"branch": None}

    return {"branch": result.stdout.strip()}
