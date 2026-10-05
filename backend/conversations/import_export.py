from __future__ import annotations
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, Field
from .models import ConversationRecord
from .public_projection import project_public_conversation
from .repository import ConversationRepository


class ImportTree(BaseModel):
    schema_name: Literal["minicode.conversation.export"] = Field(alias="schema")
    version: Literal[1]
    selected_conversation_id: str
    conversations: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


def import_conversation_tree(
    repository: ConversationRepository, payload: dict, workspace_root: str = ""
) -> dict:
    tree = ImportTree.model_validate(payload)
    sources = [
        ConversationRecord.from_dict(project_public_conversation(item))
        for item in tree.conversations
    ]
    if len({record.id for record in sources}) != len(sources):
        raise ValueError("导入文件包含重复会话 ID。")
    identities = {record.id: "conv_" + uuid4().hex[:12] for record in sources}
    if tree.selected_conversation_id not in identities:
        raise ValueError("导入文件缺少选中的会话。")

    def remap(value):
        if isinstance(value, list):
            return [remap(item) for item in value]
        if isinstance(value, dict):
            return {
                key: identities.get(item, item)
                if key
                in {
                    "conversation_id",
                    "parent_conversation_id",
                    "source_conversation_id",
                }
                and isinstance(item, str)
                else remap(item)
                for key, item in value.items()
            }
        return value

    for record in sources:
        record.id = identities[record.id]
        record.parent_conversation_id = identities.get(
            record.parent_conversation_id, ""
        )
        record.merged_into_conversation_id = identities.get(
            record.merged_into_conversation_id, ""
        )
        record.revision = record.content_revision = 0
        record.permission_mode = "confirm"
        record.permission_previous_mode = ""
        record.permission_overrides = {}
        record.archived = False
        record.archived_at = ""
        record.workspace_root = workspace_root
        record.worktree_path = record.git_branch = ""
        record.git_isolated = False
        record.transcript = remap(record.transcript)
        record.context_snapshot = remap(record.context_snapshot)
        if record.goal.get("status") in {"running", "active", "queued"}:
            record.goal["status"] = "paused"
    for record in sources:
        repository.save_conversation(record)
    return {
        "conversation_id": identities[tree.selected_conversation_id],
        "count": len(sources),
        "conversations": [
            {
                "id": record.id,
                "title": record.title,
                "message_count": len(record.transcript),
            }
            for record in sources
        ],
    }
