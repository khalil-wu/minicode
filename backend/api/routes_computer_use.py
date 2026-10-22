"""Computer Use settings stay bound to the requesting window's WS owner."""
from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict
from typing import Literal

from backend.api import _state
from backend.services.computer_use_settings import get_computer_use_settings, remove_computer_use_app_approval

router = APIRouter()


@router.get("/api/settings/computer-use")
def get_settings(response: Response, session_id: str, conversation_id: str | None = None, workspace_root: str | None = None):
    response.headers["Cache-Control"] = "no-store"
    return get_computer_use_settings(_state.ws_manager, session_id=session_id,
        conversation_id=conversation_id, workspace_root=workspace_root)


class RemoveAppApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    conversation_id: str | None = None
    workspace_root: str | None = None
    provider_id: Literal["minicode-win32", "installed-source"]
    selection_id: str
    app_id: str


@router.post("/api/settings/computer-use/app-approvals/remove")
def remove_approval(request: RemoveAppApproval, response: Response):
    response.headers["Cache-Control"] = "no-store"
    return remove_computer_use_app_approval(_state.ws_manager, **request.model_dump())
