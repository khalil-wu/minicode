from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import load_config
from backend.documents.service import ingest_uploaded_document

logger = logging.getLogger(__name__)

class ChatApiServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def run_rest_chat(
    *,
    message: str,
    max_iterations: int,
    bootstrap: Any,
    query_engine: QueryEngine,
) -> dict[str, Any]:
    config = bootstrap.config or load_config()

    artifact_store = ArtifactStore()
    tool_registry = bootstrap.create_tool_registry(artifact_store)
    permission_checker = bootstrap.create_permission_checker()

    try:
        llm = bootstrap.create_llm()
    except Exception as exc:
        return {
            "reply": f"LLM initialization failed: {exc}",
            "stopped_reason": "api_error",
            "iterations": 0,
            "tool_calls": [],
        }

    reply_parts: list[str] = []
    stopped_reason = "completed"
    state = AgentState(
        user_message=message,
        max_iterations=max_iterations,
    )
    try:
        agent_settings = replace(config.agent, max_iterations=max_iterations)
    except TypeError:
        agent_settings = config.agent

    async for event in query_engine.submit(QuerySubmission(
        user_message=message,
        session=AgentSession(
            llm=llm,
            tool_registry=tool_registry,
            artifact_store=artifact_store,
            permission_checker=permission_checker,
            agent_settings=agent_settings,
            token_budget=config.token_budget,
        ),
        state=state,
    )):
        if event.type == "text_chunk":
            reply_parts.append(event.data.get("content", ""))
        elif event.type == "error":
            stopped_reason = event.data.get("error_type", "api")
            reply_parts.append(event.data.get("message", ""))
        elif event.type == "done":
            status = str(event.data.get("status") or "completed")
            if status == "completed":
                stopped_reason = "completed"
            elif stopped_reason == "completed":
                stopped_reason = str(event.data.get("reason") or status)

    if state.stopped_reason:
        stopped_reason = state.stopped_reason

    return {
        "reply": "".join(reply_parts) or state.reply or "(No reply)",
        "stopped_reason": stopped_reason,
        "iterations": state.iterations,
        "tool_calls": [
            {
                "tool_name": tool_call.tool_name,
                "tool_input": tool_call.tool_input,
                "tool_output": tool_call.tool_output,
                "artifact_id": tool_call.artifact_id,
                "status": tool_call.status,
            }
            for tool_call in state.tool_calls
        ],
    }


def upload_document_payload(
    *,
    session_id: str,
    file_name: str | None,
    raw_content: bytes,
    ws_manager: Any,
    attachment_store: Any,
) -> dict[str, Any]:
    session = ws_manager.get_session(session_id)
    if session is None:
        raise ChatApiServiceError(404, f"Session '{session_id}' is not connected.")

    try:
        result = ingest_uploaded_document(
            file_name=file_name or "upload.txt",
            raw_content=raw_content,
            artifact_store=session.artifact_store,
        )
    except ValueError as exc:
        raise ChatApiServiceError(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to ingest uploaded document '%s': %s", file_name, exc)
        raise ChatApiServiceError(500, "Failed to ingest uploaded document.") from exc

    if not session.active_conversation_id:
        session._ensure_active_conversation()

    attachment = result.attachment.to_dict()
    if session.active_conversation_id:
        attachment_store.save(
            artifact_id=result.artifact_id,
            content=result.full_text,
            metadata={
                "conversation_id": session.active_conversation_id,
                "attachment": attachment,
            },
        )

    return {
        "file_name": result.file_name,
        "doc_id": result.doc_id,
        "artifact_id": result.artifact_id,
        "indexed_chunks": result.indexed_chunks,
        "attachment": attachment,
    }
