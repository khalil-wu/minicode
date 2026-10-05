from __future__ import annotations

import mimetypes
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from backend.services.chat_api_service import ChatApiServiceError
from backend.owner_scope import canonical_workspace_root


def conversation_resource_page(
    *, session_id: str, conversation_id: str, ws_manager: Any,
    after: str = "", limit: int = 60, query: str = "", kind: str = "all", expected_workspace_root: str | None = None,
) -> dict[str, Any]:
    session = ws_manager.get_session(session_id)
    if session is None:
        raise ChatApiServiceError(404, "Session is not connected.")
    conversation = session.conversation_repo.get_conversation(conversation_id)
    if conversation is None or conversation.archived:
        raise ChatApiServiceError(404, "Conversation is not available.")
    workspace_root = str(session.session_lifecycle.workspace_root_for_conversation(conversation) or "")
    if expected_workspace_root is not None and canonical_workspace_root(expected_workspace_root) != canonical_workspace_root(workspace_root):
        raise ChatApiServiceError(409, "The conversation workspace changed. Refresh its resources.")
    items: dict[str, dict[str, Any]] = {}
    for meta in session.artifact_store.list_artifacts(conversation_id=conversation_id, workspace_root=workspace_root):
        items[f"artifact:{meta.artifact_id}"] = {
            "id": f"artifact:{meta.artifact_id}", "artifact_id": meta.artifact_id,
            "source": "artifact", "kind": meta.type, "name": meta.source or meta.artifact_id,
            "media_type": meta.media_type, "size_bytes": meta.size,
            "occurred_at": meta.created_at * 1000, "execution_result": False,
        }
    for artifact_id, metadata in session.attachment_store.list_metadata(conversation_id=conversation_id, workspace_root=workspace_root):
        attachment = metadata.get("attachment", {})
        items[f"attachment:{artifact_id}"] = {
            "id": f"attachment:{artifact_id}", "artifact_id": artifact_id,
            "source": "attachment", "kind": "attachment", "name": attachment.get("file_name") or attachment.get("title") or artifact_id,
            "media_type": attachment.get("media_type", ""), "size_bytes": attachment.get("size_bytes", 0),
            "occurred_at": metadata.get("created_at", 0) * 1000, "execution_result": False,
        }
    for message in conversation.transcript:
        records = [("artifact", record) for record in message.get("artifacts", [])]
        records += [("attachment", record) for record in message.get("attachmentRefs", [])]
        records += [("workspace", record) for record in message.get("reply_attachments", [])]
        tools = list(message.get("tool_calls", []))
        tools += [block["record"] for block in message.get("blocks", []) if block.get("type") == "tool_call"]
        for tool in tools:
            records.append(("artifact", {
                "artifactId": tool.get("artifactId") or tool.get("artifact_id"),
                "summary": tool.get("displaySummary") or tool.get("display_summary") or tool.get("summary"),
                "mediaType": tool.get("artifactMediaType") or tool.get("artifact_media_type"),
                "executionResult": tool.get("name") in {"tool_exec", "tool_wait"}
                    and (tool.get("artifactKind") or tool.get("artifact_kind") or "") in {"", "text", "json"},
            }))
            records += [("workspace", file) for file in tool.get("outputFiles", tool.get("output_files", []))]
        for source, record in records:
            artifact_id = record.get("artifactId") or record.get("artifact_id") or record.get("docId") or record.get("id")
            path = record.get("path", "") if source == "workspace" else ""
            key = f"workspace:{path}" if source == "workspace" else f"{source}:{artifact_id}"
            if source == "workspace" and path:
                items.setdefault(key, {
                    "id": key, "source": source, "kind": "file", "name": PurePosixPath(path.replace("\\", "/")).name,
                    "path": path, "media_type": mimetypes.guess_type(path)[0] or "text/plain",
                    "size_bytes": record.get("size", 0), "execution_result": False,
                })
            if key not in items:
                continue
            item = items[key]
            name = record.get("name") or record.get("summary")
            if name:
                item["name"] = name
            media_type = record.get("mediaType") or record.get("media_type")
            if media_type:
                item["media_type"] = media_type
            timestamp = message.get("timestamp", item.get("occurred_at", 0))
            occurred_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000 if isinstance(timestamp, str) else timestamp
            item.update({"message_id": message["id"], "turn_id": message.get("turn_id", message.get("turnId", "")),
                         "occurred_at": occurred_at})
            item["execution_result"] = item["execution_result"] or bool(record.get("executionResult"))
    needle = query.strip().casefold()
    visible = []
    for item in items.values():
        item.update({"conversation_id": conversation_id, "workspace_root": workspace_root})
        image = str(item["media_type"]).startswith("image/")
        media_type = str(item["media_type"])
        execution = item["execution_result"] and not image and (not media_type or media_type.startswith("text/") or media_type == "application/json")
        item["execution_result"] = execution
        matches_kind = kind == "all" or kind == "image" and image or kind == "attachment" and item["source"] == "attachment" or kind == "execution" and execution or kind == "file" and item["source"] != "attachment" and not image and not execution
        if not matches_kind:
            continue
        if needle and needle not in f"{item['name']} {item.get('path', '')} {item.get('turn_id', '')}".casefold():
            continue
        visible.append(item)
    visible.sort(key=lambda item: (item.get("occurred_at", 0), item["id"]), reverse=True)
    start = next((index + 1 for index, item in enumerate(visible) if item["id"] == after), 0) if after else 0
    if after and start == 0:
        raise ChatApiServiceError(409, "The resource page changed. Refresh the list.")
    page = visible[start:start + limit]
    return {"conversation_id": conversation_id, "workspace_root": workspace_root, "items": page,
            "total": len(visible), "has_more": start + len(page) < len(visible), "after": page[-1]["id"] if page else ""}


def background_command_detail(session: Any, conversation_id: str, command_id: str, *, cursor: int, max_chars: int) -> dict[str, Any]:
    conversation = session.conversation_repo.get_conversation_summary(conversation_id)
    if conversation is None or conversation.archived:
        raise ChatApiServiceError(404, "Conversation is not available.")
    command = session.background_manager.get_status(command_id, conversation_id=conversation_id)
    if command is None:
        from backend.terminal.task_persistence import load_task
        from backend.terminal.task_output import get_task_output_path, read_task_output_chunk

        saved = load_task(session.session_id, command_id)
        if saved is None or saved.conversation_id != conversation_id:
            raise ChatApiServiceError(404, "Background command was not found in this conversation.")
        output_path = get_task_output_path(session.session_id, conversation_id, command_id)
        output, next_cursor, has_more = read_task_output_chunk(output_path, cursor, max_chars)
        return {"command_id": saved.task_id, "command": saved.command, "description": saved.description,
                "cwd": saved.cwd, "status": "unknown", "exit_code": None,
                "started_at": saved.started_at, "completed_at": saved.cleanup_completed_at,
                "conversation_id": conversation_id, "cleanup_pending": saved.cleanup_pending,
                "cleanup_reason": saved.cleanup_reason, "cleanup_error": {}, "managed": False,
                "output": output, "next_cursor": next_cursor, "has_more": has_more}
    output, next_cursor, has_more, _ = session.background_manager.get_output_chunk(
        command_id, conversation_id=conversation_id, cursor=cursor, max_chars=max_chars,
    )
    return {**command.to_dict(), "managed": True, "output": output, "next_cursor": next_cursor, "has_more": has_more}
