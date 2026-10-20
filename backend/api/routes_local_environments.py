"""Explicit local environment editing; saving does not launch project scripts."""
from pathlib import Path

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from backend.services.local_environment_settings import (
    EnvironmentConflict, LocalEnvironment, config_path_for, project_environments, read_environment, save_environment, select_environment, selected_config, source_checkout_root,
)
from backend.workspace.recent_projects import RecentProjectStore
from backend.workspace.trust import is_workspace_trusted

router = APIRouter()


def _root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if not root.is_dir(): raise HTTPException(409, "Project folder does not exist.")
    if not is_workspace_trusted(root): raise HTTPException(403, "Project folder is not trusted.")
    return root


def _worktree(root: Path, path: str) -> Path | None:
    from backend.services.worktree_settings_service import is_managed_worktree_path
    target = Path(path).resolve() if path else None
    if target is not None and not is_managed_worktree_path(target, source_checkout_root(root)):
        raise HTTPException(403, "Worktree does not belong to this project.")
    return target


def _document_root(root: Path, config_path: str, worktree: Path | None) -> Path:
    path = Path(config_path).resolve()
    document_root = path.parent.parent.parent
    if worktree is not None and document_root.is_relative_to(worktree):
        config_path_for(document_root, str(path))
        return document_root
    if path.is_relative_to(root):
        config_path_for(root, str(path))
        return root
    raise ValueError("Environment file does not belong to the selected project or its worktree.")


@router.get("/api/settings/local-environments")
def get_environments(response: Response, workspace_root: str = "", config_path: str = "", execution_root: str = "") -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        if workspace_root:
            root = _root(workspace_root)
            from backend.services.worktree_settings_service import is_managed_worktree_path
            execution_checkout = source_checkout_root(Path(execution_root).resolve()) if execution_root else None
            worktree = execution_checkout if execution_checkout is not None and is_managed_worktree_path(execution_checkout, source_checkout_root(root)) else None
            if config_path: return read_environment(_document_root(root, config_path, worktree), config_path)
            info = project_environments(root)
            selected = selected_config(root, worktree)
            if selected is not None and str(selected) not in {item["config_path"] for item in info["configs"]}:
                info["configs"].append(read_environment(_document_root(root, str(selected), worktree), str(selected)))
            default_root = worktree / root.relative_to(source_checkout_root(root)) if worktree is not None else root
            return {**info, "worktree_path": str(worktree) if worktree else "", "selected_config_path": str(selected) if selected else "", "default_config_path": str(config_path_for(default_root))}
        return {"projects": [{**project.to_dict(), "path": project.execution_root} for project in RecentProjectStore().list()]}
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


class EnvironmentSave(BaseModel):
    workspace_root: str
    config_path: str
    expected_revision: str
    environment: LocalEnvironment
    worktree_path: str = ""


@router.put("/api/settings/local-environments")
def put_environment(request: EnvironmentSave) -> dict:
    try:
        root = _root(request.workspace_root)
        worktree = _worktree(root, request.worktree_path)
        return save_environment(_document_root(root, request.config_path, worktree), request.config_path, request.expected_revision, request.environment)
    except EnvironmentConflict as error:
        raise HTTPException(409, str(error)) from error
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


class EnvironmentSelection(BaseModel):
    workspace_root: str
    config_path: str
    worktree_path: str = ""


@router.put("/api/settings/local-environments/selection")
def put_selection(request: EnvironmentSelection) -> dict:
    root = _root(request.workspace_root)
    worktree = _worktree(root, request.worktree_path)
    try:
        document_root = _document_root(root, request.config_path, worktree) if request.config_path else root
        select_environment(document_root, request.config_path, worktree, source_workspace_root=root)
        return {"selected_config_path": request.config_path}
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error
