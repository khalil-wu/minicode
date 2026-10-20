"""Storage review uses the existing conversation deletion lifecycle."""
from fastapi import APIRouter, HTTPException, Query, Response
from starlette.concurrency import run_in_threadpool

from backend.services.storage_settings_service import app_disk_usage, review_storage
from . import _state

router = APIRouter()


@router.get("/api/settings/storage")
async def get_storage_usage(response: Response):
    response.headers["Cache-Control"] = "no-store"
    return await run_in_threadpool(app_disk_usage)


@router.get("/api/settings/storage/review")
async def get_storage_review(response: Response, session_id: str = Query(..., min_length=1),
                             days: int = Query(90), archived_only: bool = Query(True)):
    response.headers["Cache-Control"] = "no-store"
    if days not in {30, 90, 180, 365}:
        raise HTTPException(422, "Use 30, 90, 180, or 365 days.")
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "Session is not connected.")
    return await run_in_threadpool(review_storage, session.conversation_repo, days=days, archived_only=archived_only)
