from pathlib import Path

from backend.agent.attachment_policy import build_attachment_input_plan
from backend.agent.context import ContextBuilder
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.conversations.repository import ConversationRepository
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.utils import build_summary_from_transcript


def _runtime(repo: ConversationRepository) -> ConversationRuntime:
    return ConversationRuntime(
        conversation_repo=repo,
        context_builder=ContextBuilder(),
        build_summary_from_transcript=build_summary_from_transcript,
    )


def test_recall_keeps_durable_attachment_readable_only_by_the_same_owner(
    tmp_path: Path,
) -> None:
    conversation_id = "conv_attachment_recall"
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    content = "The durable attachment survives a transcript rewind."

    artifact_store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    attachment_store = AttachmentStore(tmp_path / "attachments")
    artifact_id = artifact_store.save(
        content,
        source="upload:requirements.txt",
        type="document",
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    )
    attachment = {
        "id": "att_requirements",
        "kind": "document",
        "file_name": "requirements.txt",
        "media_type": "text/plain",
        "artifact_id": artifact_id,
        "doc_id": "doc_requirements",
        "size_bytes": len(content.encode("utf-8")),
    }
    attachment_store.save(
        artifact_id=artifact_id,
        content=content,
        metadata={
            "conversation_id": conversation_id,
            "workspace_root": str(workspace_root),
            "attachment": attachment,
        },
    )

    repo = ConversationRepository(base_dir=tmp_path / "conversations")
    conversation = repo.create_conversation(
        conversation_id=conversation_id,
        workspace_root=str(workspace_root),
        transcript=[
            {
                "id": "user-with-attachment",
                "role": "user",
                "content": "Read this file",
                "attachments": [attachment],
            },
            {
                "id": "assistant-answer",
                "role": "assistant",
                "content": "Initial answer",
            },
        ],
        context_snapshot={
            "history": [
                {
                    "role": "user",
                    "content": "Read this file",
                    "attachment_refs": [attachment],
                },
                {
                    "role": "assistant",
                    "content": "Initial answer",
                },
            ],
            "turn_admissions": {
                "user-with-attachment": {
                    "history_start": 0,
                    "history_end": 1,
                },
            },
        },
    )

    updated = _runtime(repo).rewind_to_user_turn(
        conversation=conversation,
        retry_from_message_id="user-with-attachment",
    )

    assert updated is not None
    assert updated.transcript == []
    assert artifact_store.get(
        artifact_id,
        conversation_id=conversation_id,
        workspace_root=workspace_root,
    ) == content
    assert attachment_store.get(
        artifact_id,
        conversation_id=conversation_id,
        workspace_root=str(workspace_root),
    ) == content

    second_send_plan = build_attachment_input_plan(
        [attachment],
        attachment_store=attachment_store,
        conversation_id=conversation_id,
        workspace_root=str(workspace_root),
    )
    assert second_send_plan.inlined_texts == [
        {
            "file_name": "requirements.txt",
            "artifact_id": artifact_id,
            "content": content,
        }
    ]

    wrong_owner_plan = build_attachment_input_plan(
        [attachment],
        attachment_store=attachment_store,
        conversation_id="conv_other",
        workspace_root=str(workspace_root),
    )
    assert wrong_owner_plan.images == []
    assert wrong_owner_plan.documents == []
    assert wrong_owner_plan.inlined_texts == []
    assert wrong_owner_plan.text_hints == []
    assert attachment_store.get(
        artifact_id,
        conversation_id="conv_other",
        workspace_root=str(workspace_root),
    ) is None
