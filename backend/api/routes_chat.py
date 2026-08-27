"""Chat REST endpoint and document upload endpoint."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from backend.agent.query_engine import QueryEngine
from backend.attachments.store import MAX_ATTACHMENT_CONTENT_CHARS
from backend.services.chat_api_service import (
    ChatApiServiceError,
    attachment_native_payload,
    attachment_preview_payload,
    generated_artifact_native_payload,
    reserve_attachment_upload_context,
    run_rest_chat,
    upload_document_payload,
)

_UPLOAD_READ_CHUNK = 1024 * 1024

from . import _state
from .models import ChatRequest, ChatResponse, UploadResponse
from backend.services.tool_registry_factory import get_attachment_store as _get_attachment_store

router = APIRouter()

@router.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Synchronous REST chat endpoint for simple calls and tests."""
    return ChatResponse(
        **(
            await run_rest_chat(
                message=request.message,
                max_iterations=request.max_iterations,
                conversation_id=request.conversation_id,
                bootstrap=_state.bootstrap,
                query_engine=QueryEngine(),
            )
        )
    )


@router.post("/api/uploads", response_model=UploadResponse)
async def upload_document(
    session_id: str = Query(..., min_length=1),
    conversation_id: str = Query(""),
    file: UploadFile = File(...),
) -> UploadResponse:
    """Upload a document into one fixed conversation owner."""
    try:
        # Reserve the owner on the ASGI event-loop thread before the first
        # await. Conversation switches during body transfer or parsing can no
        # longer move the attachment into a different chat.
        upload_context = reserve_attachment_upload_context(
            session_id=session_id,
            conversation_id=conversation_id,
            ws_manager=_state.ws_manager,
            attachment_store=_get_attachment_store(),
        )
    except ChatApiServiceError as exc:
        await file.close()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    # Read in bounded chunks and reject early once the same 50 MB limit the
    # attachment store enforces is exceeded, instead of pulling an unbounded
    # body fully into memory (and then base64/vectorizing it) before the store's
    # post-hoc size check runs. Closes the upload OOM window.
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(_UPLOAD_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ATTACHMENT_CONTENT_CHARS:
                raise HTTPException(status_code=413, detail="Upload exceeds the 50 MB limit.")
            chunks.append(chunk)
        raw_content = b"".join(chunks)
        try:
            # PDF/Office/archive extraction is synchronous and can be CPU or disk
            # intensive. Keep it off the ASGI event loop so an upload cannot starve
            # WebSocket heartbeat/reconnect traffic for the same desktop session.
            payload = await run_in_threadpool(
                upload_document_payload,
                context=upload_context,
                file_name=file.filename,
                raw_content=raw_content,
            )
        except ChatApiServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        upload_context.release()
        await file.close()

    return UploadResponse(**payload)


@router.get("/api/attachments/preview")
async def preview_attachment(
    session_id: str = Query(..., min_length=1),
    conversation_id: str = Query(..., min_length=1),
    artifact_id: str = Query(..., min_length=1),
) -> dict[str, object]:
    """Return a bounded, session-owned attachment preview over HTTP."""
    try:
        return await run_in_threadpool(
            attachment_preview_payload,
            session_id=session_id,
            conversation_id=conversation_id,
            artifact_id=artifact_id,
            ws_manager=_state.ws_manager,
            attachment_store=_get_attachment_store(),
        )
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


def _byte_range(value: str, total: int) -> tuple[int, int] | None:
    raw = str(value or "").strip()
    if not raw.startswith("bytes=") or "," in raw or total <= 0:
        return None
    start_text, separator, end_text = raw[6:].partition("-")
    if not separator:
        return None
    try:
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                return None
            return max(0, total - suffix), total - 1
        start = int(start_text)
        end = int(end_text) if end_text else total - 1
    except ValueError:
        return None
    if start < 0 or start >= total or end < start:
        return None
    return start, min(end, total - 1)


def _native_body_response(
    request: Request,
    *,
    body: bytes,
    media_type: str,
    file_name: str,
) -> Response:
    total = len(body)
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": f"inline; filename*=UTF-8''{quote(file_name, safe='')}",
        "X-Content-Type-Options": "nosniff",
    }
    selected = _byte_range(request.headers.get("range", ""), total)
    if selected is None:
        headers["Content-Length"] = str(total)
        return Response(content=body, media_type=media_type, headers=headers)
    start, end = selected
    headers["Content-Range"] = f"bytes {start}-{end}/{total}"
    headers["Content-Length"] = str(end - start + 1)
    return Response(content=body[start:end + 1], status_code=206, media_type=media_type, headers=headers)


@router.get("/api/attachments/raw")
async def raw_attachment(
    request: Request,
    session_id: str = Query(..., min_length=1),
    conversation_id: str = Query(..., min_length=1),
    artifact_id: str = Query(..., min_length=1),
    asset_token: str | None = Query(None),
) -> Response:
    """Stream a native image/PDF body for the internal attachment viewer."""
    # The HTTP auth middleware validates this short-lived token against the
    # session and artifact before routing the request. Keeping the query
    # parameter here makes that authorization boundary explicit.
    _ = asset_token
    try:
        body, media_type, file_name = await run_in_threadpool(
            attachment_native_payload,
            session_id=session_id,
            conversation_id=conversation_id,
            artifact_id=artifact_id,
            ws_manager=_state.ws_manager,
            attachment_store=_get_attachment_store(),
        )
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    return _native_body_response(
        request,
        body=body,
        media_type=media_type,
        file_name=file_name,
    )


@router.get("/api/artifacts/raw")
async def raw_generated_artifact(
    request: Request,
    session_id: str = Query(..., min_length=1),
    conversation_id: str = Query(..., min_length=1),
    artifact_id: str = Query(..., min_length=1),
    asset_token: str | None = Query(None),
) -> Response:
    """Stream an owner-scoped generated image for chat and context thumbnails."""

    _ = asset_token
    try:
        body, media_type, file_name = await run_in_threadpool(
            generated_artifact_native_payload,
            session_id=session_id,
            conversation_id=conversation_id,
            artifact_id=artifact_id,
            ws_manager=_state.ws_manager,
        )
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return _native_body_response(
        request,
        body=body,
        media_type=media_type,
        file_name=file_name,
    )
