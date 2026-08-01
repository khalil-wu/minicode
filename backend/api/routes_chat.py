"""Chat REST endpoint and document upload endpoint."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from backend.agent.loop import run_agent_loop
from backend.agent.query_engine import QueryEngine
from backend.attachments.store import MAX_ATTACHMENT_CONTENT_CHARS
from backend.services.chat_api_service import (
    ChatApiServiceError,
    run_rest_chat,
    upload_document_payload,
)

_UPLOAD_READ_CHUNK = 1024 * 1024

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
    # Read in bounded chunks and reject early once the same 50 MB limit the
    # attachment store enforces is exceeded, instead of pulling an unbounded
    # body fully into memory (and then base64/vectorizing it) before the store's
    # post-hoc size check runs. Closes the upload OOM window.
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = await file.read(_UPLOAD_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ATTACHMENT_CONTENT_CHARS:
                raise HTTPException(status_code=413, detail="Upload exceeds the 50 MB limit.")
            chunks.append(chunk)
        raw_content = b"".join(chunks)
    except HTTPException:
        await file.close()
        raise
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
