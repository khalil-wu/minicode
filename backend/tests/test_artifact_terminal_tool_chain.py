from __future__ import annotations

import asyncio
import base64
import threading

import pytest

from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.documents import service
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.terminal.session import TerminalSessionManager
from backend.tools.agent_artifact_tools import ReadArtifactTool
from backend.tools.registry import ToolRegistry
from backend.tools.terminal_tools import ReadTerminalTool


@pytest.mark.parametrize("args", [{"offset": 0}, {"offset": -1}, {"limit": 0}, {"limit": -2}, {"offset": True}, {"limit": "1"}])
def test_artifact_pagination_rejects_invalid_windows_before_reading(tmp_path, args, monkeypatch):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    monkeypatch.setattr(store, "get", lambda *a, **kw: pytest.fail("invalid window reached storage"))
    registry = ToolRegistry()
    registry.register(ReadArtifactTool(store))
    result = asyncio.run(registry.execute("read_artifact", {"artifact_id": "record-only", **args}))
    assert result.error_kind == "validation_error"


def test_artifact_page_keeps_exact_owner_and_excludes_the_head_preview(tmp_path):
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    artifact_id = store.save("alpha\nbeta\ngamma", source="audit", conversation_id="owner", workspace_root=tmp_path)
    registry = ToolRegistry()
    registry.register(ReadArtifactTool(store))
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", workspace_root=tmp_path)
    result = asyncio.run(registry.execute("read_artifact", {"artifact_id": artifact_id, "offset": 2, "limit": 1}, context=context))
    assert result.content.endswith("\nbeta")
    assert "alpha" not in result.to_context_string() and "gamma" not in result.to_context_string()
    foreign = ToolExecutionContext(permission=PermissionContext(), conversation_id="foreign", workspace_root=tmp_path)
    assert asyncio.run(registry.execute("read_artifact", {"artifact_id": artifact_id}, context=foreign)).is_error


def test_cancelled_artifact_reparse_retains_its_storage_worker_until_commit(tmp_path, monkeypatch):
    attachments = AttachmentStore(base_dir=tmp_path / "uploads")
    metadata = {"conversation_id": "owner", "workspace_root": str(tmp_path),
        "attachment": {"file_name": "record.pdf", "media_type": "application/pdf", "parse_error": "unavailable"}}
    attachments.save(artifact_id="upload-audit", content="PDF parse failed", metadata=metadata, native_data=base64.b64encode(b"record-only").decode())
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    def parse(*args):
        started.set()
        release.wait(2)
        return {"full_text": "derived text", "parse_error": ""}
    monkeypatch.setattr(service, "parse_document_preview", parse)
    original = attachments.update_extraction
    def update(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    monkeypatch.setattr(attachments, "update_extraction", update)
    async def run():
        context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", workspace_root=tmp_path)
        tool = ReadArtifactTool(ArtifactStore(storage_dir=tmp_path / "artifacts"), attachment_store=attachments)
        task = asyncio.create_task(tool.execute({"artifact_id": "upload-audit"}, context))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not finished.is_set()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert attachments.get("upload-audit", conversation_id="owner", workspace_root=str(tmp_path)) == "derived text"
        assert attachments.get("upload-audit", conversation_id="other", workspace_root=str(tmp_path)) is None
    asyncio.run(run())


def test_read_terminal_latest_selection_stays_inside_conversation_owner():
    manager = TerminalSessionManager()
    for identifier, owner, text in (("mine-first", "owner", "first"), ("mine-last", "owner", "latest"), ("foreign-latest", "other", "private")):
        manager.upsert_external_session(identifier, cwd="record-only", shell="record-only", conversation_id=owner)
        manager.append_external_output(identifier, text, conversation_id=owner)
    registry = ToolRegistry()
    registry.register(ReadTerminalTool())
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="owner", terminal_manager=manager)
    result = asyncio.run(registry.execute("read_terminal", {}, context=context))
    assert "mine-last" in result.content and "latest" in result.content
    assert "private" not in result.content
    rejected = asyncio.run(registry.execute("read_terminal", {"session_id": "foreign-latest"}, context=context))
    assert rejected.is_error
