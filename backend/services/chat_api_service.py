from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import load_config
from backend.documents.service import ingest_uploaded_document
from backend.permissions.context import PermissionContext

logger = logging.getLogger(__name__)

class ChatApiServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def run_rest_chat(
    *,
    message: str,
    max_iterations: int | None,
    bootstrap: Any,
    query_engine: QueryEngine | None,
    workspace_root: Any | None = None,
    permission_mode: str = "confirm",
    conversation_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    config = bootstrap.config or load_config()

    artifact_store = ArtifactStore()
    tool_registry = bootstrap.create_tool_registry(artifact_store)
    permission_checker = bootstrap.create_permission_checker()

    try:
        llm = bootstrap.create_llm()
    except Exception as exc:
        error_message = f"LLM initialization failed: {exc}"
        return {
            "reply": "(No reply)",
            "stopped_reason": "api_error",
            "status": "failed",
            "errors": [error_message],
            "iterations": 0,
            "tool_calls": [],
        }

    reply_parts: list[str] = []
    error_messages: list[str] = []
    stopped_reason = "completed"
    terminal_status = "completed"
    configured_max_iterations = (
        max(0, int(max_iterations))
        if max_iterations is not None
        else max(0, int(getattr(config.agent, "max_iterations", 0) or 0))
    )
    state = AgentState(user_message=message, max_iterations=configured_max_iterations)
    agent_settings = (
        replace(config.agent, max_iterations=configured_max_iterations)
        if max_iterations is not None
        else config.agent
    )

    normalized_permission_mode = str(permission_mode or "confirm").strip().lower()
    if normalized_permission_mode == "auto_approve":
        normalized_permission_mode = "auto"
    runtime = AgentLoopSessionContext(
        permission_context=PermissionContext(
            mode=normalized_permission_mode if normalized_permission_mode in {"default", "plan", "confirm", "bypass", "auto", "accept_edits"} else "confirm",
            workspace_scope="project",
            source="scheduled_task" if run_id else "rest_api",
        ),
        workspace_root=workspace_root,
        session_id=f"scheduled:{run_id}" if run_id else "rest_api",
        metadata={
            "source": "scheduled_task" if run_id else "rest_api",
            "conversation_id": conversation_id,
            "run_id": run_id,
            "requires_explicit_workspace": bool(workspace_root),
        },
    )
    state.conversation_id = conversation_id
    engine = query_engine or QueryEngine(runner=run_agent_loop)
    async for event in engine.submit(QuerySubmission(
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
        runtime=runtime,
    )):
        if event.type == "item.completed":
            item = event.data.get("item") if isinstance(event.data.get("item"), dict) else {}
            if item.get("type") == "agent_message":
                reply_parts[:] = [str(item.get("text") or "")]
        elif event.type == "error":
            stopped_reason = event.data.get("error_type", "api")
            message_text = str(event.data.get("message") or "").strip()
            if message_text:
                error_messages.append(message_text)
        elif event.type == "done":
            status = str(event.data.get("status") or "completed")
            terminal_status = status if status in {"completed", "partial", "cancelled", "failed"} else "failed"
            if status == "completed":
                stopped_reason = "completed"
            elif stopped_reason == "completed":
                stopped_reason = str(event.data.get("reason") or status)

    if state.stopped_reason:
        stopped_reason = state.stopped_reason
    if state.terminal_status:
        terminal_status = state.terminal_status

    return {
        "reply": "".join(reply_parts) or state.reply or "(No reply)",
        "stopped_reason": stopped_reason,
        "status": terminal_status,
        "errors": error_messages,
        "iterations": state.iterations,
        "tool_calls": [
            {
                "tool_name": tool_call.tool_name,
                "tool_input": tool_call.tool_input,
                "tool_output": tool_call.tool_output,
                "artifact_id": tool_call.artifact_id,
                "status": tool_call.status,
                "error_kind": tool_call.error_kind,
                "user_summary": tool_call.user_summary,
                "developer_detail": tool_call.developer_detail,
                "recoverable": tool_call.recoverable,
                "projection": tool_call.projection,
                "model_observation": tool_call.model_observation,
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

    if not session.active_conversation_id:
        session._ensure_active_conversation()
    conversation_id = str(session.active_conversation_id or "")
    workspace_root = session._workspace_root_for_conversation()

    try:
        result = ingest_uploaded_document(
            file_name=file_name or "upload.txt",
            raw_content=raw_content,
            artifact_store=session.artifact_store,
            conversation_id=conversation_id,
            workspace_root=workspace_root,
        )
    except ValueError as exc:
        raise ChatApiServiceError(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to ingest uploaded document '%s': %s", file_name, exc)
        raise ChatApiServiceError(500, "Failed to ingest uploaded document.") from exc

    attachment = result.attachment.to_dict()
    if conversation_id:
        native_data = str(attachment.get("data") or "")
        persisted_attachment = dict(attachment)
        persisted_attachment.pop("data", None)
        try:
            attachment_store.save(
                artifact_id=result.artifact_id,
                content=result.full_text,
                metadata={
                    "conversation_id": conversation_id,
                    "workspace_root": str(workspace_root or ""),
                    "attachment": persisted_attachment,
                },
                native_data=native_data,
            )
        except (OSError, ValueError) as exc:
            logger.exception("Failed to persist uploaded attachment '%s': %s", file_name, exc)
            raise ChatApiServiceError(500, "Failed to persist uploaded attachment.") from exc

    return {
        "file_name": result.file_name,
        "doc_id": result.doc_id,
        "artifact_id": result.artifact_id,
        "attachment": attachment,
    }
