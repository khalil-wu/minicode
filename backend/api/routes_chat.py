"""Chat REST endpoint and document upload endpoint."""

from __future__ import annotations

import asyncio

from backend.artifact.media import AUDIO_MEDIA_EXTENSIONS

from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from starlette.concurrency import run_in_threadpool

from backend.agent.query_engine import QueryEngine
from backend.async_cleanup import _consume_task_result
from backend.attachments.store import MAX_ATTACHMENT_CONTENT_BYTES
from backend.services.chat_api_service import (
    ChatApiServiceError,
    attachment_native_payload,
    attachment_preview_payload,
    generated_artifact_native_payload,
    reserve_attachment_upload_context,
    run_rest_chat,
    upload_document_payload,
)

from . import _state
from .models import ChatRequest, ChatResponse, UploadResponse
from backend.services.tool_registry_factory import get_attachment_store as _get_attachment_store
from backend.services.conversation_resources_service import background_command_detail, conversation_resource_page
from backend.ws.command_scope import resolve_command_scope
from backend.conversations.import_export import import_conversation_tree

_UPLOAD_READ_CHUNK = 1024 * 1024

router = APIRouter()

@router.post("/api/conversations/import")
async def import_conversations(
    file: UploadFile = File(...), session_id: str = Query(..., min_length=1), workspace_root: str = Query(""),
):
    import json
    from pathlib import Path
    from backend.workspace.trust import is_workspace_trusted
    session = _state.ws_manager.get_session(session_id)
    if session is None: raise HTTPException(status_code=404, detail="Session is not connected.")
    if workspace_root and not is_workspace_trusted(Path(workspace_root)):
        raise HTTPException(status_code=403, detail="目标项目尚未受信任。")
    content = await file.read(25 * 1024 * 1024 + 1)
    if len(content) > 25 * 1024 * 1024: raise HTTPException(status_code=413, detail="会话导入文件不能超过 25 MiB。")
    try:
        payload = json.loads(content)
        return await run_in_threadpool(import_conversation_tree, session.conversation_repo, payload, workspace_root)
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/api/conversations/{conversation_id}/resources")
async def conversation_resources(
    conversation_id: str, session_id: str = Query(..., min_length=1),
    after: str = Query(""), limit: int = Query(60, ge=1, le=200),
    query: str = Query(""), kind: str = Query("all", pattern="^(all|file|image|attachment|execution)$"),
    workspace_root: str | None = Query(None),
) -> dict:
    try:
        return await run_in_threadpool(conversation_resource_page, session_id=session_id,
                                       conversation_id=conversation_id, ws_manager=_state.ws_manager,
                                       after=after, limit=limit, query=query, kind=kind, expected_workspace_root=workspace_root)
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/api/conversations/{conversation_id}/background-commands/{command_id}")
async def background_command(
    conversation_id: str, command_id: str, session_id: str = Query(..., min_length=1),
    cursor: int = Query(0, ge=0), max_chars: int = Query(20000, ge=1, le=100000),
) -> dict:
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session is not connected.")
    try:
        return await run_in_threadpool(background_command_detail, session, conversation_id, command_id,
                                       cursor=cursor, max_chars=max_chars)
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/api/conversations/{conversation_id}/background-commands/{command_id}/stop")
async def stop_background_command(
    conversation_id: str, command_id: str, session_id: str = Query(..., min_length=1),
) -> dict:
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session is not connected.")
    try:
        scope = resolve_command_scope(session, {"conversation_id": conversation_id})
        if session.background_manager.get_status(command_id, conversation_id=scope.conversation_id) is None:
            raise ChatApiServiceError(404, "Background command was not found in this conversation.")
        stopped = await session.background_manager.cancel(command_id, conversation_id=scope.conversation_id)
        detail = await run_in_threadpool(background_command_detail, session, scope.conversation_id, command_id,
                                        cursor=0, max_chars=20000)
        return {**detail, "stopped": stopped}
    except ChatApiServiceError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

@router.get("/api/conversations/{conversation_id}/messages")
async def conversation_messages(
    conversation_id: str,
    session_id: str = Query(..., min_length=1),
    before_message_id: str = Query(""),
    limit: int = Query(80, ge=1, le=200),
) -> dict:
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        page = await run_in_threadpool(
            session.conversation_repo.get_transcript_page, conversation_id,
            limit=limit, before_message_id=before_message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return page

@router.get("/api/conversations/{conversation_id}/messages/{message_id}/tools")
async def conversation_message_tools(
    conversation_id: str, message_id: str,
    session_id: str = Query(..., min_length=1), before: int = Query(..., ge=0),
    limit: int = Query(40, ge=1, le=200),
    revision: str = Query("", max_length=64),
) -> dict:
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    try:
        page = await run_in_threadpool(session.conversation_repo.get_message_tool_items,
                                      conversation_id, message_id, before=before, limit=limit, revision=revision)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if page is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return page


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
    workspace_root: str = Query(""),
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
            workspace_root=workspace_root,
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
    upload_task: asyncio.Task[dict] | None = None
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await file.read(_UPLOAD_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_ATTACHMENT_CONTENT_BYTES:
                raise HTTPException(status_code=413, detail="Upload exceeds the 50 MB limit.")
            chunks.append(chunk)
        raw_content = b"".join(chunks)
        try:
            # PDF/Office/archive extraction is synchronous and can be CPU or disk
            # intensive. Keep it off the ASGI event loop so an upload cannot starve
            # WebSocket heartbeat/reconnect traffic for the same desktop session.
            def persist_upload() -> dict:
                try:
                    return upload_document_payload(
                        context=upload_context,
                        file_name=file.filename,
                        raw_content=raw_content,
                    )
                finally:
                    upload_context.release()

            # Cancelling the HTTP request does not stop a parsing thread. Its
            # owner reservation must remain live until its final disk write.
            upload_task = asyncio.create_task(run_in_threadpool(persist_upload))
            upload_task.add_done_callback(_consume_task_result)
            payload = await asyncio.shield(upload_task)
        except ChatApiServiceError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        if upload_task is None:
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
    download: bool = False,
) -> Response:
    total = len(body)
    inline_media = media_type == "application/pdf" or media_type in AUDIO_MEDIA_EXTENSIONS or media_type in {
        "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp", "image/avif",
    }
    disposition = "inline" if inline_media and not download else "attachment"
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=300",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(file_name, safe='')}",
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
    download: bool = Query(False),
) -> Response:
    """Return the owner-scoped original for native preview or download."""
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
        download=download,
    )


@router.get("/api/artifacts/raw")
async def raw_generated_artifact(
    request: Request,
    session_id: str = Query(..., min_length=1),
    conversation_id: str = Query(..., min_length=1),
    artifact_id: str = Query(..., min_length=1),
    asset_token: str | None = Query(None),
) -> Response:
    """Stream owner-scoped generated images and audio, including byte ranges."""

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

