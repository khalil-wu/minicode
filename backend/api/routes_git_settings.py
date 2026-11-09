"""Local desktop Git preferences; saving never executes a Git operation."""
from fastapi import APIRouter, HTTPException, Response

from backend.services.git_settings_service import GitPreferences, get_git_preferences, update_git_preferences


router = APIRouter()


@router.get("/api/settings/git")
def get_git_settings(response: Response) -> GitPreferences:
    response.headers["Cache-Control"] = "no-store"
    return get_git_preferences()


@router.put("/api/settings/git")
def put_git_settings(request: GitPreferences, response: Response) -> GitPreferences:
    response.headers["Cache-Control"] = "no-store"
    try:
        return update_git_preferences(request)
    except OSError as error:
        raise HTTPException(500, f"无法保存 Git 偏好：{error}") from error
