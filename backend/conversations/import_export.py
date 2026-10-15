from __future__ import annotations
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, Field
from .models import ConversationRecord
from .public_projection import project_public_conversation
from .repository import ConversationRepository, rebind_transcript_turn_diffs
from backend.owner_scope import canonical_workspace_root


class ImportTree(BaseModel):
    schema_name: Literal["minicode.conversation.export"] = Field(alias="schema")
    version: Literal[1]
    selected_conversation_id: str
    conversations: list[dict[str, Any]] = Field(min_length=1, max_length=1000)


def import_conversation_tree(
    repository: ConversationRepository, payload: dict, workspace_root: str = "",
    *, attachment_store: Any | None = None, artifact_store: Any | None = None,
) -> dict:
    tree = ImportTree.model_validate(payload)
    sources = [
        ConversationRecord.from_dict(project_public_conversation(item, defer_turn_diff=False))
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
                    "thread_id",
                    "parent_conversation_id",
                    "source_conversation_id",
                }
                and isinstance(item, str)
                else remap(item)
                for key, item in value.items()
            }
        return value

    def artifact_refs(value: Any) -> set[str]:
        if isinstance(value, list):
            return set().union(*(artifact_refs(item) for item in value)) if value else set()
        if not isinstance(value, dict):
            return set()
        found = {item for key, item in value.items()
                 if key in {"artifact_id", "artifactId"} and isinstance(item, str) and item}
        for item in value.values():
            found.update(artifact_refs(item))
        return found

    resource_sources: list[tuple[str, str, set[str], set[str]]] = []
    local_projectless_owners: set[str] = set()
    for record in sources:
        local = repository.get_conversation(record.id)
        requested_refs = artifact_refs([record.transcript, record.context_snapshot])
        trusted_refs: set[str] = set()
        source_workspace = str(record.execution_cwd or record.worktree_path or record.workspace_root or "")
        if (local is not None
                and canonical_workspace_root(local.workspace_root) == canonical_workspace_root(record.workspace_root)
                and canonical_workspace_root(local.execution_cwd or local.worktree_path or local.workspace_root) == canonical_workspace_root(source_workspace)):
            trusted_refs = artifact_refs([local.transcript, local.context_snapshot])
            if (local.projectless_browser_root
                    and canonical_workspace_root(local.projectless_browser_root) == canonical_workspace_root(record.projectless_browser_root)
                    and canonical_workspace_root(local.projectless_output_dir) == canonical_workspace_root(record.projectless_output_dir)):
                local_projectless_owners.add(record.id)
        # Uploaded JSON is external content. Its conversation ids/paths do not
        # grant bytes; only matching persisted provenance and refs can do so.
        resource_sources.append((record.id, source_workspace, requested_refs, requested_refs & trusted_refs))

    for record in sources:
        has_local_projectless_owner = record.id in local_projectless_owners
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
        record.execution_cwd = workspace_root or record.execution_cwd
        if workspace_root or not has_local_projectless_owner:
            record.projectless_output_dir = record.projectless_browser_root = ""
        record.worktree_path = record.git_branch = ""
        record.git_isolated = False
        record.transcript = remap(record.transcript)
        rebind_transcript_turn_diffs(record.transcript, record.id)
        record.context_snapshot = remap(record.context_snapshot)
        if record.goal.get("status") in {"running", "active", "queued"}:
            record.goal["status"] = "paused"
    stores = [store for store in (attachment_store, artifact_store) if store is not None]
    unavailable_artifacts: list[dict[str, str]] = []
    execution_roots = {record.id: record.execution_cwd for record in sources}
    try:
        for source_id, source_workspace, requested_refs, allowed_refs in resource_sources:
            target_id = identities[source_id]
            target_execution_cwd = execution_roots[target_id]
            if allowed_refs:
                for store in stores:
                    store.share_for_conversation(
                        source_id, target_id, target_execution_cwd,
                        artifact_ids=allowed_refs, source_workspace_root=source_workspace,
                    )
            for artifact_id in sorted(requested_refs):
                available = False
                if artifact_id in allowed_refs:
                    if attachment_store is not None:
                        available = attachment_store.get_payload(artifact_id, conversation_id=target_id, workspace_root=target_execution_cwd) is not None
                    if not available and artifact_store is not None:
                        available = artifact_store.get(artifact_id, conversation_id=target_id, workspace_root=target_execution_cwd) is not None
                if not available:
                    unavailable_artifacts.append({
                        "conversation_id": target_id, "artifact_id": artifact_id,
                        "reason": "source_unverified" if artifact_id not in allowed_refs else "original_unavailable",
                    })
        for record in sources:
            repository.save_conversation(record)
    except Exception as failure:
        cleanup_errors: list[Exception] = []
        # Only newly minted identities are rolled back; source histories and
        # original attachment bytes remain owned by their existing scopes.
        for target_id in identities.values():
            for store in stores:
                try:
                    store.delete_for_conversation(target_id)
                except Exception as exc:
                    cleanup_errors.append(exc)
        for target_id in identities.values():
            try:
                repository.delete_conversation(target_id)
            except Exception as exc:
                cleanup_errors.append(exc)
        if cleanup_errors:
            raise ExceptionGroup("Conversation import failed and resource rollback is incomplete", [failure, *cleanup_errors]) from failure
        raise
    return {
        "conversation_id": identities[tree.selected_conversation_id],
        "count": len(sources),
        "unavailable_artifacts": unavailable_artifacts,
        "conversations": [
            {
                "id": record.id,
                "title": record.title,
                "message_count": len(record.transcript),
            }
            for record in sources
        ],
    }
