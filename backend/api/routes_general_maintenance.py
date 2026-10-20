"""Owned desktop idle scheduler entry point."""
from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict
from typing import Literal

from backend.services.general_maintenance import maintain_local_chats
from . import _state

router = APIRouter()


class GeneralMaintenanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auto_archive_after_days: Literal[0, 1, 7, 14, 30, 90]
    compress_local_history: bool


@router.post("/api/settings/general/maintenance")
async def general_maintenance(request: GeneralMaintenanceRequest):
    return await maintain_local_chats(_state.ws_manager, **request.model_dump())
