"""Chat REST endpoint and document upload endpoint."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from backend.agent.loop import run_agent_loop
from backend.agent.query_engine import QueryEngine
from backend.services.chat_api_service import (
    ChatApiServiceError,
    run_rest_chat,
    upload_document_payload,
)

from . import _state
from .models import ChatRequest, ChatResponse, UploadResponse
from .tool_registry import _get_attachment_store

router = APIRouter()


@router.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Synchronous REST chat endpoint for simple calls and tests."""
    return ChatResponse(
        **(
            await run_rest_chat(
                message=request.message,
                max_iterations=request.max_iterations,
                bootstrap=_state.bootstrap,
                query_engine=QueryEngine(runner=run_agent_loop),
            )
        )
    )


@router.post("/api/uploads", response_model=UploadResponse)
async def upload_document(
    session_id: str = Query(..., min_length=1),
    file: UploadFile = File(...),
) -> UploadResponse:
    """Upload a document into the active WebSocket session."""
    raw_content = await file.read()
    try:
        payload = upload_document_payload(
            session_id=session_id,
            file_name=file.filename,
            raw_content=raw_content,
            ws_manager=_state.ws_manager,
            attachment_store=_get_attachment_store(),
        )
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        await file.close()

    return UploadResponse(**payload)
