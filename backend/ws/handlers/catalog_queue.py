"""Runtime skill roots and durable queued-input mutations."""
from __future__ import annotations

from typing import Any

from backend.agent.message import AgentEvent, UserCommand
from backend.ws.command_results import emit_command_error
from backend.ws.conversation_stream import response_origin
from backend.ws.durable_user_queue import DurableUserMessageQueue
from backend.ws.utils import USER_INPUT_METADATA_KEYS, normalize_attachment_payloads, normalize_user_input_metadata


async def handle_skills_extra_roots_set(session, data: dict[str, Any]) -> bool:
    manager = session.skill_manager
    if manager is None or manager.runtime_roots is None:
        await emit_command_error(session, "skills/extraRoots/set", "Runtime skill roots are unavailable")
        return True
    try:
        await manager.runtime_roots.set_extra_roots(data.get("extraRoots"))
    except ValueError as exc:
        await emit_command_error(session, "skills/extraRoots/set", exc)
        return True
    await session.emit_command_result("skills/extraRoots/set", "", level="success", data={})
    return True


def _queue_owner(session, data: dict[str, Any]) -> str:
    conversation_id = data.get("conversation_id")
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("conversation_id is required")
    if session.conversation_repo.get_conversation_summary(conversation_id) is None:
        raise ValueError("The queued conversation no longer exists")
    return conversation_id


def _queue_payload(session, conversation_id: str, data: dict[str, Any]) -> dict[str, Any]:
    content = data.get("content")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    payload = {"content": content, "attachments": normalize_attachment_payloads(data.get("attachments", [])),
        **normalize_user_input_metadata(data)}
    conversation = session.conversation_repo.get_conversation_summary(conversation_id)
    workspace = str(session.session_lifecycle.workspace_root_for_conversation(conversation) or "")
    unavailable = [item for item in payload["attachments"] if session.attachment_store.get_payload(item["artifact_id"],
        conversation_id=conversation_id, workspace_root=workspace) is None]
    if unavailable:
        from backend.agent.attachment_policy import AttachmentUnavailableError
        raise ValueError(str(AttachmentUnavailableError(unavailable)))
    refs = payload.get("context_refs", [])
    payload["skills"] = [{"name": item["name"], "path": item["path"]}
        for item in refs if item["kind"] == "skill" and item.get("path")]
    payload["plugins"] = [{"config_name": item.get("configName", item["name"]), "path": item.get("path", "")}
        for item in refs if item["kind"] == "plugin"]
    record = session.conversation_repo.get_conversation(conversation_id)
    payload["_guardian_acceptance_order"] = session.guardian_host_for_conversation(conversation_id,
        evidence_checkpoint=record.context_snapshot.get("guardian_evidence")).evidence.reserve_order()
    return payload


async def _publish_queue(session, conversation_id: str, *, updated_message_id: str = "") -> None:
    for row in session.run_manager.queued_user_message_snapshot(conversation_id):
        event = AgentEvent.user_message_queue_updated(status="queued", conversation_id=conversation_id,
            message_id=row["message_id"], user_message_id=row["user_message_id"], position=row["position"],
            paused=row["paused"], reason="queue_updated" if row["message_id"] == updated_message_id else "queue_reordered")
        event.data["input"] = {key: row[key] for key in ("content", "attachments", *USER_INPUT_METADATA_KEYS) if key in row}
        await session.send_event(event)


async def handle_queue_add(session, data: dict[str, Any]) -> bool:
    command = "user_message.queue.add"
    try:
        conversation_id = _queue_owner(session, data)
        for key in ("message_id", "user_message_id", "client_command_id"):
            if not isinstance(data.get(key), str) or not data[key]:
                raise ValueError(f"{key} is required")
        payload = {**_queue_payload(session, conversation_id, data), "conversation_id": conversation_id,
            "assistant_message_id": data["message_id"], "user_message_id": data["user_message_id"],
            "client_command_id": data["client_command_id"],
            "_client_queue_key": DurableUserMessageQueue.queue_key_for_session(response_origin(data["client_command_id"]) or session.session_id),
            "_submitted_content": data["content"]}
        session.run_manager.add_queued_user_message(conversation_id, UserCommand(type="user_message", data=payload))
    except ValueError as exc:
        await emit_command_error(session, command, exc)
        return True
    await _publish_queue(session, conversation_id, updated_message_id=data["message_id"])
    await session.emit_command_result(command, "", level="success", data={"conversation_id": conversation_id,
        "message_id": data["message_id"], "assistant_message_id": data["message_id"], "user_message_id": data["user_message_id"], "queued": True})
    return True


async def handle_queue_update(session, data: dict[str, Any]) -> bool:
    command = "user_message.queue.update"
    try:
        conversation_id = _queue_owner(session, data)
        message_id = data.get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("message_id is required")
        payload = _queue_payload(session, conversation_id, data)
        updated = session.run_manager.update_queued_user_message(conversation_id, message_id, payload)
        if updated is None:
            raise ValueError("Queued message is no longer available")
    except ValueError as exc:
        await emit_command_error(session, command, exc)
        return True
    await _publish_queue(session, conversation_id, updated_message_id=message_id)
    await session.emit_command_result(command, "", level="success", data={"conversation_id": conversation_id,
        "message_id": message_id, "queued": True})
    return True


async def handle_queue_reorder(session, data: dict[str, Any]) -> bool:
    command = "user_message.queue.reorder"
    try:
        conversation_id = _queue_owner(session, data)
        message_ids = data.get("message_ids")
        if not isinstance(message_ids, list) or any(not isinstance(value, str) for value in message_ids):
            raise ValueError("message_ids must be an array of strings")
        session.run_manager.reorder_queued_user_messages(conversation_id, message_ids)
    except ValueError as exc:
        await emit_command_error(session, command, exc)
        return True
    await _publish_queue(session, conversation_id)
    await session.emit_command_result(command, "", level="success", data={"conversation_id": conversation_id, "message_ids": message_ids})
    return True


HANDLERS = {
    "skills/extraRoots/set": handle_skills_extra_roots_set,
    "user_message.queue.update": handle_queue_update,
    "user_message.queue.add": handle_queue_add,
    "user_message.queue.reorder": handle_queue_reorder,
}
