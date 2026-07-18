from __future__ import annotations

from typing import Any


def seq_from_restore_payload(data: dict[str, Any]) -> int:
    for key in ("last_seq", "last_seen_seq", "last_event_seq"):
        value = data.get(key)
        try:
            seq = int(value)
        except (TypeError, ValueError):
            continue
        if seq > 0:
            return seq
    return 0


def build_restored_runtime_snapshot(
    runtime_snapshot: dict[str, Any],
    *,
    restored_conversation_id: str | None,
    active_payload: dict[str, Any] | None,
    restored_workspace: dict[str, Any] | None,
) -> dict[str, Any]:
    snapshot = dict(runtime_snapshot)
    if restored_conversation_id:
        snapshot.update({
            "active_conversation_id": restored_conversation_id,
            "active_conversation": active_payload,
        })
        restored_permission_mode = str((active_payload or {}).get("permission_mode") or "").strip()
        if restored_permission_mode:
            snapshot["permission_mode"] = restored_permission_mode
    if restored_workspace:
        snapshot["workspace_root"] = restored_workspace.get("root_path")
    return snapshot


def build_session_restored_payload(
    result: dict[str, Any],
    *,
    restored_conversation_id: str | None,
    active_payload: dict[str, Any] | None,
    restored_workspace: dict[str, Any] | None,
    runtime_snapshot: dict[str, Any],
    selected_model: str,
    provider: str,
    available_models: list[str],
    missed_events: bool,
    last_seq: int,
    current_seq: int,
    replayed_events: int = 0,
    provider_id: str = "",
    base_url: str = "",
    wire_api: str = "",
    models_source: str = "",
) -> dict[str, Any]:
    return {
        "type": "session.restored",
        "session_id": result["session_id"],
        "restored": result["restored"],
        "active_conversation_id": restored_conversation_id,
        "conversation_switched_follows": bool(restored_conversation_id and active_payload),
        "conversation": active_payload,
        "active_conversation": active_payload,
        "workspace": restored_workspace,
        "working_directory": restored_workspace.get("root_path") if restored_workspace else "",
        "model": selected_model,
        "current_model": selected_model,
        "provider": provider,
        "provider_id": provider_id,
        "base_url": base_url,
        "wire_api": wire_api,
        "available_models": available_models,
        "models_source": models_source,
        "session": runtime_snapshot,
        "messages": result.get("messages", []),
        "error": result.get("error"),
        "missed_events": bool(missed_events),
        "last_seq": last_seq,
        "current_seq": current_seq,
        "replayed_events": max(0, int(replayed_events or 0)),
    }


def build_restore_conversation_switched_payload(
    *,
    restored_conversation_id: str,
    active_payload: dict[str, Any],
    is_hydrating: bool,
    runtime_snapshot: dict[str, Any],
) -> dict[str, Any]:
    return {
        "type": "conversation.switched",
        "conversation_id": restored_conversation_id,
        "conversation": active_payload,
        "is_hydrating": is_hydrating,
        "session": runtime_snapshot,
    }


def build_session_synced_payload(
    result: dict[str, Any],
    *,
    protocol_version: str,
    active_conversation: Any | None,
    active_conversation_id: str | None,
    workspace_root: Any | None,
    selected_model: str,
    provider: str,
    available_models: list[str],
    last_seq: int,
    current_seq: int,
    provider_id: str = "",
    base_url: str = "",
    wire_api: str = "",
    models_source: str = "",
) -> dict[str, Any]:
    active_is_visible = active_conversation is not None and not getattr(active_conversation, "archived", False)
    return {
        "type": "session.synced",
        "protocol_version": protocol_version,
        "session_id": result["session_id"],
        "synced": result["synced"],
        "incremental": result["incremental"],
        "changes": result.get("changes", []),
        "session": result["session"],
        "active_conversation_id": active_conversation_id if active_is_visible else None,
        "active_conversation": active_conversation.to_dict() if active_is_visible else None,
        "working_directory": str(workspace_root) if workspace_root is not None else "",
        "model": selected_model,
        "current_model": selected_model,
        "provider": provider,
        "provider_id": provider_id,
        "base_url": base_url,
        "wire_api": wire_api,
        "available_models": available_models,
        "models_source": models_source,
        "missed_events": bool(last_seq and last_seq < current_seq),
        "last_seq": last_seq,
        "current_seq": current_seq,
    }
