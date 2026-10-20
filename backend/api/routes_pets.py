"""Local pet creation preparation; generated images use the normal tool path."""
from fastapi import APIRouter, HTTPException, Query, Response

from backend.api import _state
from backend.services.pet_hatch_service import prepare_hatch_pet

router = APIRouter()


@router.post("/api/pets/hatch/prepare")
async def prepare_pet_api(response: Response, session_id: str = Query(..., min_length=1)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    if _state.bootstrap is None:
        raise HTTPException(503, "The local Skill catalog is not ready.")
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session is not connected.")
    provider = session.provider if not session.active_conversation_id and session._model_override_active else None
    try:
        return await prepare_hatch_pet(_state.bootstrap.skill_manager, provider=provider)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
