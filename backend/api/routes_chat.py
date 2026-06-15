"""Chat REST endpoint and document upload endpoint."""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from backend.agent.loop import run_agent_loop
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import load_config
from backend.documents.service import ingest_uploaded_document

from . import _state
from .models import ChatRequest, ChatResponse, ToolCallRecord, UploadResponse
from .tool_registry import _get_attachment_store

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """Synchronous REST chat endpoint for simple calls and tests."""
    bootstrap = _state.bootstrap
    config = bootstrap.config or load_config()

    # Create session-level resources
    artifact_store = ArtifactStore()
    tool_registry = bootstrap.create_tool_registry(artifact_store)
    permission_checker = bootstrap.create_permission_checker()

    # Create LLM adapter
    try:
        llm = bootstrap.create_llm()
    except Exception as exc:
        return ChatResponse(
            reply=f"LLM initialization failed: {exc}",
            stopped_reason="api_error",
            iterations=0,
            tool_calls=[],
        )

    # Run Agent Loop, collecting all events
    reply_parts: list[str] = []
    tool_records: list[ToolCallRecord] = []
    stopped_reason = "completed"
    iterations = 0

    state = AgentState(
        user_message=request.message,
        max_iterations=request.max_iterations,
    )

    async for event in run_agent_loop(
        user_message=request.message,
        llm=llm,
        tool_registry=tool_registry,
        artifact_store=artifact_store,
        permission_checker=permission_checker,
        agent_settings=config.agent,
        token_budget=config.token_budget,
        state=state,
        vector_memory=bootstrap.vector_memory,
    ):
        if event.type == "text_chunk":
            reply_parts.append(event.data.get("content", ""))

        elif event.type == "tool_result":
            tool_records.append(
                ToolCallRecord(
                    tool_name=event.data.get("name", "unknown"),
                    tool_output=event.data.get("summary", ""),
                    artifact_id=event.data.get("artifact_id"),
                )
            )

        elif event.type == "error":
            error_type = event.data.get("error_type", "api")
            stopped_reason = error_type
            reply_parts.append(event.data.get("message", ""))

        elif event.type == "done":
            stopped_reason = "completed"

    # Get more accurate info from state
    iterations = state.iterations
    if state.stopped_reason:
        stopped_reason = state.stopped_reason

    # Build tool call records (from state for more complete info)
    final_tool_calls = []
    for tc in state.tool_calls:
        final_tool_calls.append(
            ToolCallRecord(
                tool_name=tc.tool_name,
                tool_input=tc.tool_input,
                tool_output=tc.tool_output,
                artifact_id=tc.artifact_id,
                status=tc.status,
            )
        )

    reply = "".join(reply_parts) or state.reply or "(No reply)"

    return ChatResponse(
        reply=reply,
        stopped_reason=stopped_reason,
        iterations=iterations,
        tool_calls=final_tool_calls,
    )


@router.post("/api/uploads", response_model=UploadResponse)
async def upload_document(
    session_id: str = Query(..., min_length=1),
    file: UploadFile = File(...),
) -> UploadResponse:
    """Upload a document into the active WebSocket session."""
    session = _state.ws_manager.get_session(session_id)
    if session is None:
        raise HTTPException(
            status_code=404,
            detail=f"Session '{session_id}' is not connected.",
        )

    raw_content = await file.read()
    try:
        result = ingest_uploaded_document(
            file_name=file.filename or "upload.txt",
            raw_content=raw_content,
            artifact_store=session.artifact_store,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to ingest uploaded document '%s': %s", file.filename, exc)
        raise HTTPException(
            status_code=500,
            detail="Failed to ingest uploaded document.",
        ) from exc
    finally:
        await file.close()

    if not session.active_conversation_id:
        session._ensure_active_conversation()

    if session.active_conversation_id:
        attachment = result.attachment.to_dict()
        _get_attachment_store().save(
            artifact_id=result.artifact_id,
            content=result.full_text,
            metadata={
                "conversation_id": session.active_conversation_id,
                "attachment": attachment,
            },
        )

    return UploadResponse(
        file_name=result.file_name,
        doc_id=result.doc_id,
        artifact_id=result.artifact_id,
        indexed_chunks=result.indexed_chunks,
        attachment=result.attachment.to_dict(),
    )
