"""Computer Use settings stay bound to the requesting window's WS owner."""
from fastapi import APIRouter, Body, Response
from pydantic import BaseModel, ConfigDict
from typing import Annotated, Literal
from typing import Literal

from backend.api import _state
from backend.async_cleanup import to_thread_cancel_safe
from backend.services.computer_use_settings import (
    get_computer_use_settings, remove_computer_use_app_approval, selected_computer_use_plugin_control,
)

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


class PluginControlOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    conversation_id: str | None = None
    workspace_root: str | None = None
    provider_id: str
    selection_id: str
    control_id: Literal["any-app"]


class InstallPluginControl(PluginControlOwner):
    action: Literal["install"]


class SetPluginControlEnabled(PluginControlOwner):
    action: Literal["set_enabled"]
    enabled: bool


PluginControlAction = Annotated[InstallPluginControl | SetPluginControlEnabled, Body(discriminator="action")]


@router.post("/api/settings/computer-use/controls/plugin")
async def mutate_plugin_control(request: PluginControlAction, response: Response):
    from backend.api import routes_skills

    response.headers["Cache-Control"] = "no-store"
    values = request.model_dump()
    control = await to_thread_cancel_safe(selected_computer_use_plugin_control, _state.ws_manager,
        **{key: value for key, value in values.items() if key != "enabled"})
    if request.action == "install":
        mutation = await routes_skills.install_plugin_api(routes_skills.PluginInstallRequest(
            plugin_name=control["plugin_name"], marketplace=control["marketplace"], refresh_marketplace=False,
            workspace_root=request.workspace_root or ""), response)
    else:
        mutation = await routes_skills.update_plugin_state_api(control["plugin_id"],
            routes_skills.PluginStateUpdateRequest(enabled=request.enabled, workspace_root=request.workspace_root or ""), response)
    result = await to_thread_cancel_safe(get_computer_use_settings, _state.ws_manager, session_id=request.session_id,
        conversation_id=request.conversation_id, workspace_root=request.workspace_root)
    if "runtime_refresh" in mutation:
        result["runtime_refresh"] = mutation["runtime_refresh"]
    return result
