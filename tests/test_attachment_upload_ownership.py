from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, UploadFile

from backend.agent.attachment_policy import build_attachment_input_plan
from backend.api import routes_chat
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.conversations.repository import ConversationRepository
from backend.services.chat_api_service import (
    ChatApiServiceError,
    reserve_attachment_upload_context,
    upload_document_payload,
)
from backend.ws.manager import WebSocketManager


def _owners(tmp_path: Path, monkeypatch):
    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    store = AttachmentStore(tmp_path / "attachments")
    session = SimpleNamespace(
        _attachment_upload_lock=threading.RLock(),
        active_conversation_id=None,
        conversation_repo=repo,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        session_lifecycle=SimpleNamespace(
            workspace_root_for_conversation=lambda conversation: Path(conversation.workspace_root)
            if conversation.workspace_root else None,
        ),
    )
    manager = WebSocketManager()
    monkeypatch.setattr(manager, "get_session", lambda session_id: session if session_id == "upload-session" else None)
    monkeypatch.setattr(routes_chat._state, "ws_manager", manager)
    monkeypatch.setattr(routes_chat, "_get_attachment_store", lambda: store)
    return manager, session, store


@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelled_upload_keeps_delete_reservation_until_worker_settles(tmp_path, monkeypatch, worker_fails):
    manager, session, _ = _owners(tmp_path, monkeypatch)
    conversation = session.conversation_repo.create_conversation()
    entered, finish, released = threading.Event(), threading.Event(), threading.Event()
    release = manager.release_attachment_upload

    def release_upload(token):
        release(token)
        released.set()

    def persist(**_kwargs):
        entered.set()
        assert finish.wait(3)
        if worker_fails:
            raise ChatApiServiceError(500, "parser failed")
        return {}

    monkeypatch.setattr(manager, "release_attachment_upload", release_upload)
    monkeypatch.setattr(routes_chat, "upload_document_payload", persist)

    async def run():
        upload = UploadFile(filename="source.txt", file=io.BytesIO(b"contents"))
        task = asyncio.create_task(routes_chat.upload_document(
            session_id="upload-session", conversation_id=conversation.id, workspace_root="", file=upload,
        ))
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert upload.file.closed
        assert not released.is_set()
        token, reason, count = manager.begin_conversation_delete(conversation.id)
        assert (token, reason, count) == (None, "attachment_upload_active", 1)
        finish.set()
        assert await asyncio.to_thread(released.wait, 2)
        token, reason, count = manager.begin_conversation_delete(conversation.id)
        assert token and reason == "" and count == 0
        manager.end_conversation_delete(conversation.id, token)

    asyncio.run(run())


def test_upload_rejected_during_body_read_releases_reservation(tmp_path, monkeypatch):
    manager, session, _ = _owners(tmp_path, monkeypatch)
    conversation = session.conversation_repo.create_conversation()
    monkeypatch.setattr(routes_chat, "MAX_ATTACHMENT_CONTENT_BYTES", 3)
    upload = UploadFile(filename="source.txt", file=io.BytesIO(b"contents"))
    with pytest.raises(HTTPException, match="50 MB") as exc:
        asyncio.run(routes_chat.upload_document(
            session_id="upload-session", conversation_id=conversation.id, workspace_root="", file=upload,
        ))
    assert exc.value.status_code == 413
    assert upload.file.closed
    token, reason, count = manager.begin_conversation_delete(conversation.id)
    assert token and reason == "" and count == 0
    manager.end_conversation_delete(conversation.id, token)


def test_first_upload_persists_workspace_owner_and_rejects_other_workspace(tmp_path, monkeypatch):
    manager, session, store = _owners(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    trust_file = tmp_path / "trusted.json"
    trust_file.write_text(json.dumps([str(workspace)]), encoding="utf-8")
    monkeypatch.setattr("backend.workspace.trust.TRUSTED_WORKSPACES_FILE", trust_file)
    context = reserve_attachment_upload_context(
        session_id="upload-session", workspace_root=str(workspace), ws_manager=manager, attachment_store=store,
    )
    assert context.workspace_root == workspace
    assert session.conversation_repo.get_conversation(context.conversation_id).workspace_root == str(workspace)
    context.release()
    with pytest.raises(ChatApiServiceError, match="another workspace") as exc:
        reserve_attachment_upload_context(
            session_id="upload-session", conversation_id=context.conversation_id,
            workspace_root=str(tmp_path / "other"), ws_manager=manager, attachment_store=store,
        )
    assert exc.value.status_code == 409
    assert not manager._attachment_upload_owners


@pytest.mark.parametrize("raw", [b"ordinary text, not a PDF", b"\x00\x01\xffnot-pdf"])
def test_renamed_pdf_never_becomes_native_pdf_input(tmp_path, monkeypatch, raw):
    manager, session, store = _owners(tmp_path, monkeypatch)
    context = reserve_attachment_upload_context(session_id="upload-session", ws_manager=manager, attachment_store=store)
    try:
        result = upload_document_payload(context=context, file_name="renamed.pdf", raw_content=raw)
    finally:
        context.release()
    payload = store.get_payload(result["artifact_id"], conversation_id=context.conversation_id)
    assert base64.b64decode(payload["native_data"]) == raw
    plan = build_attachment_input_plan(
        [result["attachment"]], attachment_store=store, conversation_id=context.conversation_id,
        retain_native_media=True,
    )
    assert not plan.documents and not plan.images
    if b"\x00" not in raw:
        assert result["attachment"]["media_type"] == "text/plain"
        assert plan.inlined_texts[0]["content"] == raw.decode()
    else:
        assert result["attachment"]["media_type"] == "application/octet-stream"
        assert plan.text_hints
