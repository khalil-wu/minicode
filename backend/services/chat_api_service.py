from __future__ import annotations

import base64
import binascii
import logging
from contextlib import nullcontext, suppress
from dataclasses import dataclass, field, replace
from typing import Any, Callable
from uuid import uuid4

from backend.agent.loop import AgentLoopSessionContext
from backend.agent.conversation_query_guard import (
    ConversationQueryClaim,
    conversation_query_guards,
)
from backend.agent.execution_journal import execution_journal_owner
from backend.agent.query_engine import AgentSession, QueryEngine, QuerySubmission
from backend.agent.runtime import default_runtime
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import load_config
from backend.documents.service import ingest_uploaded_document
from backend.permissions.context import PermissionContext

logger = logging.getLogger(__name__)

ATTACHMENT_PREVIEW_CONTENT_CHARS = 2 * 1024 * 1024
GENERATED_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
GENERATED_IMAGE_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass(frozen=True)
class AttachmentUploadContext:
    """Immutable owner snapshot reserved before an HTTP body is consumed."""

    session_id: str
    conversation_id: str
    conversation: Any
    workspace_root: Any | None
    artifact_store: Any
    attachment_store: Any
    release_upload: Callable[[], None] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def release(self) -> None:
        release = self.release_upload
        if callable(release):
            release()


class ChatApiServiceError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


async def _owned_query_events(stream: Any, query_claim: ConversationQueryClaim):
    """Yield only claim-owned events and deterministically close the stream."""

    try:
        async for event in stream:
            if not conversation_query_guards().owns(query_claim):
                return
            yield event
    finally:
        with suppress(Exception):
            await stream.aclose()


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
    owner_id = f"{'scheduled' if run_id else 'rest'}:{run_id or id(query_engine)}"
    query_guards = conversation_query_guards()
    query_claim = query_guards.try_start(
        conversation_id,
        owner_id=owner_id,
    )
    if query_claim is None:
        return {
            "reply": "(No reply)",
            "stopped_reason": "conversation_busy",
            "status": "failed",
            "errors": [
                "This conversation already has an active turn. Use steer/follow-up or wait for completion."
            ],
            "iterations": 0,
            "tool_calls": [],
        }
    try:
        return await run_owned_rest_chat(
            message=message,
            max_iterations=max_iterations,
            bootstrap=bootstrap,
            query_engine=query_engine,
            workspace_root=workspace_root,
            permission_mode=permission_mode,
            conversation_id=conversation_id,
            run_id=run_id,
            query_claim=query_claim,
        )
    finally:
        query_guards.end(query_claim)


async def run_owned_rest_chat(
    *,
    message: str,
    max_iterations: int | None,
    bootstrap: Any,
    query_engine: QueryEngine | None,
    workspace_root: Any | None = None,
    permission_mode: str = "confirm",
    conversation_id: str = "",
    run_id: str = "",
    query_claim: ConversationQueryClaim,
) -> dict[str, Any]:
    if conversation_id:
        active_claim = conversation_query_guards().active_claim(conversation_id)
        if active_claim != query_claim:
            raise RuntimeError("The REST turn does not own the active conversation query generation.")
    config = load_config(cwd=workspace_root)

    artifact_store = ArtifactStore()
    mcp_manager = getattr(bootstrap, "mcp_manager", None)
    ensure_mcp_manager = getattr(bootstrap, "ensure_mcp_manager", None)
    if callable(ensure_mcp_manager):
        mcp_manager = await ensure_mcp_manager(workspace_root)
    iter_connected_mcp = getattr(mcp_manager, "iter_connected_clients", None)
    connected_mcp_servers = [
        str(name)
        for name, _client in (
            iter_connected_mcp() if callable(iter_connected_mcp) else []
        )
        if str(name).strip()
    ]
    tool_registry = bootstrap.create_tool_registry(
        artifact_store,
        config=config,
        mcp_manager=mcp_manager,
    )
    permission_checker = bootstrap.create_permission_checker(config=config)

    try:
        llm = bootstrap.create_llm(config=config)
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
    journal_turn_id = str(run_id or uuid4().hex)
    journal_owner = execution_journal_owner(
        "main",
        conversation_id or "rest",
        journal_turn_id,
    )
    runtime = AgentLoopSessionContext(
        permission_context=PermissionContext(
            mode=normalized_permission_mode if normalized_permission_mode in {"plan", "confirm", "bypass", "auto"} else "confirm",
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
            "_execution_journal": default_runtime().execution_journal(journal_owner),
            "_mcp_manager": mcp_manager,
            "connected_mcp_servers": connected_mcp_servers,
        },
    )
    state.conversation_id = conversation_id
    engine = query_engine or QueryEngine()
    stream = engine.submit(QuerySubmission(
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
    ))
    async for event in _owned_query_events(stream, query_claim):
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

    if not conversation_query_guards().owns(query_claim):
        return {
            "reply": "(No reply)",
            "stopped_reason": "conversation_ownership_lost",
            "status": "cancelled",
            "errors": ["The conversation turn no longer owns the active query generation."],
            "iterations": state.iterations,
            "tool_calls": [],
        }

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


def reserve_attachment_upload_context(
    *,
    session_id: str,
    conversation_id: str = "",
    ws_manager: Any,
    attachment_store: Any,
) -> AttachmentUploadContext:
    session = ws_manager.get_session(session_id)
    if session is None:
        raise ChatApiServiceError(404, f"Session '{session_id}' is not connected.")

    # Real WebSocketSession instances always expose this lock. Keep the
    # fallback for narrow service test doubles without silently weakening the
    # production invariant.
    upload_lock = getattr(session, "_attachment_upload_lock", None)
    lock_context = upload_lock if upload_lock is not None else nullcontext()
    with lock_context:
        requested_id = str(conversation_id or "").strip()
        conversation = None
        if requested_id:
            conversation = session.conversation_repo.get_conversation(requested_id)
            if conversation is None or bool(getattr(conversation, "archived", False)):
                raise ChatApiServiceError(404, "The target conversation is not available.")
        else:
            active_id = str(session.active_conversation_id or "").strip()
            if active_id:
                conversation = session.conversation_repo.get_conversation(active_id)
                if conversation is not None and bool(getattr(conversation, "archived", False)):
                    conversation = None
            if conversation is None:
                conversation = session.conversation_repo.create_conversation()
                session.active_conversation_id = conversation.id

        fixed_conversation_id = str(getattr(conversation, "id", "") or "").strip()
        if not fixed_conversation_id:
            raise ChatApiServiceError(500, "Failed to reserve an attachment conversation.")
        workspace_root = session._workspace_root_for_conversation(conversation)
        reserve_upload = getattr(ws_manager, "reserve_attachment_upload", None)
        release_upload: Callable[[], None] | None = None
        if callable(reserve_upload):
            upload_token = reserve_upload(fixed_conversation_id)
            if not upload_token:
                raise ChatApiServiceError(
                    409,
                    "The target conversation is being deleted; retry the upload after it settles.",
                )
            released = False

            def release_reserved_upload() -> None:
                nonlocal released
                if released:
                    return
                released = True
                release = getattr(ws_manager, "release_attachment_upload", None)
                if callable(release):
                    release(upload_token)

            release_upload = release_reserved_upload
        return AttachmentUploadContext(
            session_id=session_id,
            conversation_id=fixed_conversation_id,
            conversation=conversation,
            workspace_root=workspace_root,
            artifact_store=session.artifact_store,
            attachment_store=attachment_store,
            release_upload=release_upload,
        )


def upload_document_payload(
    *,
    context: AttachmentUploadContext,
    file_name: str | None,
    raw_content: bytes,
) -> dict[str, Any]:
    conversation_id = context.conversation_id
    workspace_root = context.workspace_root

    try:
        result = ingest_uploaded_document(
            file_name=file_name or "upload.txt",
            raw_content=raw_content,
            artifact_store=context.artifact_store,
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
            context.attachment_store.save(
                artifact_id=result.artifact_id,
                content=result.full_text,
                metadata={
                    "conversation_id": conversation_id,
                    "workspace_root": str(workspace_root or ""),
                    "attachment": persisted_attachment,
                },
                native_data=native_data,
            )
        except ValueError as exc:
            logger.info("Rejected uploaded attachment '%s': %s", file_name, exc)
            status_code = 413 if "exceeds the" in str(exc).lower() else 400
            raise ChatApiServiceError(status_code, str(exc)) from exc
        except OSError as exc:
            logger.exception("Failed to persist uploaded attachment '%s': %s", file_name, exc)
            raise ChatApiServiceError(500, "Failed to persist uploaded attachment.") from exc

    # The binary body already crossed the HTTP upload boundary and is stored in
    # AttachmentStore. Keep the WebSocket payload reference-only so a PDF or
    # image can never be serialized into a client command frame.
    client_attachment = dict(attachment)
    client_attachment.pop("data", None)

    return {
        "conversation_id": conversation_id,
        "file_name": result.file_name,
        "doc_id": result.doc_id,
        "artifact_id": result.artifact_id,
        "attachment": client_attachment,
    }


def _session_attachment_payload(
    *,
    session_id: str,
    conversation_id: str,
    artifact_id: str,
    ws_manager: Any,
    attachment_store: Any,
) -> tuple[dict[str, Any], dict[str, Any], str, str]:
    session = ws_manager.get_session(session_id)
    if session is None:
        raise ChatApiServiceError(404, f"Session '{session_id}' is not connected.")
    fixed_conversation_id = str(conversation_id or "").strip()
    if not fixed_conversation_id:
        raise ChatApiServiceError(400, "conversation_id is required.")
    conversation = session.conversation_repo.get_conversation(fixed_conversation_id)
    if conversation is None or bool(getattr(conversation, "archived", False)):
        raise ChatApiServiceError(404, "The attachment conversation is not available.")
    workspace_root = str(session._workspace_root_for_conversation(conversation) or "")
    payload = attachment_store.get_payload(
        artifact_id,
        conversation_id=fixed_conversation_id,
        workspace_root=workspace_root,
    )
    if payload is None:
        raise ChatApiServiceError(404, "Attachment was not found in this conversation.")
    metadata = payload.get("metadata")
    attachment = metadata.get("attachment") if isinstance(metadata, dict) else None
    if not isinstance(attachment, dict):
        raise ChatApiServiceError(404, "The requested artifact is not an uploaded attachment.")
    return payload, attachment, fixed_conversation_id, workspace_root


def attachment_preview_payload(
    *,
    session_id: str,
    conversation_id: str,
    artifact_id: str,
    ws_manager: Any,
    attachment_store: Any,
) -> dict[str, Any]:
    payload, attachment, conversation_id, _ = _session_attachment_payload(
        session_id=session_id,
        conversation_id=conversation_id,
        artifact_id=artifact_id,
        ws_manager=ws_manager,
        attachment_store=attachment_store,
    )
    content = str(payload.get("content") or "")
    visible_content = content[:ATTACHMENT_PREVIEW_CONTENT_CHARS]
    native_data = payload.get("native_data")
    has_native = isinstance(native_data, str) and bool(native_data)
    return {
        "artifact_id": str(payload.get("artifact_id") or artifact_id),
        "conversation_id": conversation_id,
        "file_name": str(attachment.get("file_name") or attachment.get("title") or "Attachment"),
        "media_type": str(attachment.get("media_type") or "text/plain"),
        "kind": str(attachment.get("kind") or "document"),
        "size_bytes": int(attachment.get("size_bytes") or 0),
        "summary": str(attachment.get("summary") or ""),
        "parse_error": str(attachment.get("parse_error") or ""),
        "content": visible_content,
        "content_chars": len(content),
        "truncated": len(visible_content) < len(content),
        "has_native": has_native,
    }


def attachment_native_payload(
    *,
    session_id: str,
    conversation_id: str,
    artifact_id: str,
    ws_manager: Any,
    attachment_store: Any,
) -> tuple[bytes, str, str]:
    payload, attachment, _, _ = _session_attachment_payload(
        session_id=session_id,
        conversation_id=conversation_id,
        artifact_id=artifact_id,
        ws_manager=ws_manager,
        attachment_store=attachment_store,
    )
    native_data = payload.get("native_data")
    if not isinstance(native_data, str) or not native_data:
        raise ChatApiServiceError(404, "A native preview is not available for this attachment.")
    try:
        content = base64.b64decode(native_data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ChatApiServiceError(500, "The stored attachment body is invalid.") from exc
    return (
        content,
        str(attachment.get("media_type") or "application/octet-stream"),
        str(attachment.get("file_name") or "attachment"),
    )


def generated_artifact_native_payload(
    *,
    session_id: str,
    conversation_id: str,
    artifact_id: str,
    ws_manager: Any,
) -> tuple[bytes, str, str]:
    """Return one owner-scoped generated image without routing it over WebSocket."""

    session = ws_manager.get_session(session_id)
    if session is None:
        raise ChatApiServiceError(404, f"Session '{session_id}' is not connected.")
    fixed_conversation_id = str(conversation_id or "").strip()
    if not fixed_conversation_id:
        raise ChatApiServiceError(400, "conversation_id is required.")
    conversation = session.conversation_repo.get_conversation(fixed_conversation_id)
    if conversation is None or bool(getattr(conversation, "archived", False)):
        raise ChatApiServiceError(404, "The artifact conversation is not available.")

    artifact_store = getattr(session, "artifact_store", None)
    if artifact_store is None:
        raise ChatApiServiceError(500, "The artifact store is unavailable.")
    workspace_root = str(session._workspace_root_for_conversation(conversation) or "")
    content = artifact_store.get(
        artifact_id,
        conversation_id=fixed_conversation_id,
        workspace_root=workspace_root,
    )
    meta = artifact_store.get_meta(
        artifact_id,
        conversation_id=fixed_conversation_id,
        workspace_root=workspace_root,
    )
    if content is None or meta is None:
        raise ChatApiServiceError(404, "Generated image was not found in this conversation.")

    media_type = str(getattr(meta, "media_type", "") or "").split(";", 1)[0].strip().lower()
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    if getattr(meta, "type", "") != "image" or media_type not in GENERATED_IMAGE_MEDIA_TYPES:
        raise ChatApiServiceError(415, "The requested artifact is not a supported generated image.")
    try:
        body = base64.b64decode(str(content or "").strip(), validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ChatApiServiceError(500, "The stored generated image body is invalid.") from exc
    if not body:
        raise ChatApiServiceError(500, "The stored generated image body is empty.")

    valid_magic = (
        len(body) >= 12 and body.startswith(b"RIFF") and body[8:12] == b"WEBP"
        if media_type == "image/webp"
        else body.startswith(b"\x89PNG\r\n\x1a\n")
        if media_type == "image/png"
        else body.startswith(b"\xff\xd8\xff")
        if media_type == "image/jpeg"
        else body.startswith((b"GIF87a", b"GIF89a"))
    )
    if not valid_magic:
        raise ChatApiServiceError(500, "The stored generated image does not match its media type.")

    extension = GENERATED_IMAGE_EXTENSIONS[media_type]
    return body, media_type, f"generated-{artifact_id}.{extension}"

