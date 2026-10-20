"""Settings and lifecycle actions for host-local managed worktrees."""
from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict

from backend.api import _state
from backend.services.worktree_settings_service import (
    WorktreePreferences, default_worktree_root, get_worktree_preferences,
    list_managed_worktrees, remove_managed_worktree, update_worktree_preferences,
)


router = APIRouter()


@router.get("/api/settings/worktrees")
def get_worktree_settings(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {**get_worktree_preferences().model_dump(), "default_worktree_root": str(default_worktree_root())}


@router.put("/api/settings/worktrees")
def put_worktree_settings(request: WorktreePreferences, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {**update_worktree_preferences(request).model_dump(), "default_worktree_root": str(default_worktree_root())}


@router.get("/api/settings/worktrees/managed")
def get_managed_worktrees(response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    return {"worktrees": list_managed_worktrees(ws_manager=_state.ws_manager)}


class WorktreeRemovalRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    path: str


@router.post("/api/settings/worktrees/remove")
def remove_worktree(request: WorktreeRemovalRequest) -> dict:
    return remove_managed_worktree(request.path, ws_manager=_state.ws_manager)
