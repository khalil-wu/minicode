"""Replay export and evaluation endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response

from backend.services.replay_service import ReplayExportError, replay_export_payload

from . import _state

router = APIRouter(prefix="/api/replay", tags=["replay"])


@router.get("/{session_id}")
async def export_replay(
    session_id: str,
    response: Response,
    limit: int = Query(default=500, ge=1, le=5_000),
    conversation_id: str = Query(default=""),
    after_seq: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    """Return a sanitized websocket replay export for a desktop session."""

    response.headers["Cache-Control"] = "no-store"
    try:
        return replay_export_payload(
            session_id=session_id,
            limit=limit,
            conversation_id=conversation_id,
            after_seq=after_seq,
            ws_manager=_state.ws_manager,
        )
    except ReplayExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
