"""Selected host paths survive admission and cold model-history recovery."""
from pathlib import Path

import pytest

from backend.agent.attachment_policy import build_attachment_input_plan
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.services.chat_api_service import AttachmentUploadContext, upload_document_payload


@pytest.mark.asyncio
async def test_import_receipt_projects_authoritative_path_and_content_into_cold_history(tmp_path):
    source = tmp_path / "selected 中文.txt"
    source.write_text("Edit this original document.\n完整正文", encoding="utf-8")
    store_path = tmp_path / "attachments"
    store = AttachmentStore(store_path)
    context = AttachmentUploadContext(session_id="original-session", conversation_id="original-conversation",
        conversation=None, workspace_root=tmp_path, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        attachment_store=store, release_upload=lambda: None)
    result = upload_document_payload(context=context, file_name=source.name, raw_content=source.read_bytes(),
        source_path=str(source.resolve()))
    # The renderer cannot replace the host's selected path in a send receipt.
    reference = {**result["attachment"], "source_path": str(tmp_path / "forged.txt")}
    builder = ContextBuilder(conversation_id=context.conversation_id, workspace_root=tmp_path)
    builder._attachment_store = store
    admitted = await builder.start_turn("Update the selected file", AgentState(user_message="Update the selected file",
        conversation_id=context.conversation_id, workspace_root=tmp_path, attachments=[reference]))
    assert str(source.resolve()) in admitted.content
    assert "完整正文" in admitted.content
    assert "forged.txt" not in admitted.content
    snapshot = builder.export_snapshot()
    cold = ContextBuilder(conversation_id=context.conversation_id, workspace_root=tmp_path)
    cold._attachment_store = AttachmentStore(store_path)
    cold.load_snapshot(snapshot)
    assert str(source.resolve()) in cold.export_snapshot()["history"][-1]["content"]
    assert "完整正文" in cold.export_snapshot()["history"][-1]["content"]
    plan = build_attachment_input_plan(snapshot["history"][-1]["attachment_refs"],
        attachment_store=cold._attachment_store, conversation_id=context.conversation_id, workspace_root=str(tmp_path))
    assert any(str(source.resolve()) in hint for hint in plan.text_hints)
    other = build_attachment_input_plan([reference], attachment_store=cold._attachment_store,
        conversation_id="other-conversation", workspace_root=str(tmp_path))
    assert other.unavailable and not other.text_hints


def test_regular_upload_and_untrusted_refs_cannot_establish_an_original_host_path(tmp_path):
    store = AttachmentStore(tmp_path / "attachments")
    context = AttachmentUploadContext(session_id="session", conversation_id="conversation", conversation=None,
        workspace_root=tmp_path, artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        attachment_store=store, release_upload=lambda: None)
    result = upload_document_payload(context=context, file_name="upload.txt", raw_content=b"uploaded copy")
    forged = {**result["attachment"], "source_path": str(tmp_path / "forged.txt")}
    plan = build_attachment_input_plan([forged], attachment_store=store,
        conversation_id=context.conversation_id, workspace_root=str(tmp_path))
    assert not plan.text_hints
    direct = build_attachment_input_plan([{"file_name": "copy.txt", "source_path": "/private/original.txt"}])
    assert not direct.text_hints
