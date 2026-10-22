"""Provider-aware settings for the current window's actual CUA selection."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil

from fastapi import HTTPException

from backend.mcp.cua_approval_store import NativeAppApprovalStore, native_approval_home
from backend.mcp.cua_repl import CuaRuntime, is_configured_cua
from backend.owner_scope import canonical_workspace_root


def _selection(manager, session_id, conversation_id, workspace_root):
    session = manager.get_session(session_id)
    if session is None:
        raise HTTPException(404, "The original settings session is no longer connected")
    active = session.active_conversation_id or None
    root = session.session_lifecycle.workspace_root_for_conversation()
    if (conversation_id or None) != active or canonical_workspace_root(workspace_root) != canonical_workspace_root(root):
        raise HTTPException(409, "The settings conversation or workspace has changed")
    owner = {"session_id": session_id, "conversation_id": active, "workspace_root": str(root) if root else None}
    mcp = session._mcp_manager_for_workspace(root)
    try:
        configs = [config for config in mcp.load_config() if is_configured_cua(config)] if mcp is not None else []
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    if len(configs) > 1:
        raise HTTPException(422, "The receiving settings scope has more than one selected CUA provider")
    selected = configs[0] if configs else None
    receiving_ids = set()
    receiving_known = active is None
    if active:
        thread = manager.execution_contexts.thread_runtime_for(session.conversation_repo.store_instance_id(), active)
        if thread is not None:
            if thread.thread_environments is not None:
                receiving_ids.update(selection.environment_id for selection in thread.thread_environments.selections())
                receiving_known = bool(receiving_ids)
            pool = thread.cua_repl_companions
            companion = pool.instances.get(pool.active_key) if pool is not None else None
            if companion is not None:
                receiving_ids.add(companion.config.environment_id)
                receiving_known = True
    receiving_ids = tuple(sorted(receiving_ids))
    selection_id = hashlib.sha256(json.dumps([owner, asdict(selected), receiving_ids, receiving_known], sort_keys=True).encode()).hexdigest() if selected else None
    return owner, selected, selection_id, receiving_ids, receiving_known


def _payload(owner, selected, selection_id, receiving_ids, receiving_known):
    payload = {"owner": owner, "provider_id": None, "selection_id": selection_id, "navigation_visible": False,
        "available": False, "status": "not_configured", "reason": None,
        "capabilities": {"persistent_approvals": False, "revoke_app_approval": False, "persist_modes": [],
            "sound_mode": False, "locked_use": False}, "approved_apps": None, "controls": []}
    if selected is None:
        return payload
    provider = selected.env.get("MINICODE_CUA_PROVIDER", "installed-source")
    payload["provider_id"] = provider
    if not selected.enabled:
        denied = bool(selected.disabled_reason)
        return {**payload, "navigation_visible": denied, "status": "policy_denied" if denied else "disabled",
            "reason": selected.disabled_reason or "The selected Computer Use provider is disabled"}
    if not receiving_known:
        return {**payload, "status": "unsupported", "reason": "The original receiving environment is not captured for this conversation"}
    if selected.environment_id != "local" or any(identity != "local" for identity in receiving_ids):
        return {**payload, "status": "unsupported", "reason": "Persistent app settings cannot manage a remote or mixed receiving store"}
    if os.name != "nt":
        return {**payload, "status": "unsupported", "reason": "The selected Computer Use provider requires a Windows receiving host"}
    if provider == "installed-source":
        try:
            CuaRuntime.selected(selected)
        except (ValueError, OSError) as error:
            return {**payload, "status": "unavailable", "reason": str(error)}
        # Never infer an arbitrary installed helper's store format from the
        # current desktop UI version, and never touch the user's Codex home.
        return {**payload, "available": True, "status": "ready",
            "reason": "Persistent app approval management is unsupported for this selected installed runtime contract"}
    command = selected.command
    if not command or not (Path(command).is_file() or shutil.which(command)):
        return {**payload, "status": "unavailable", "reason": "The selected Computer Use Node executable is unavailable"}
    store = NativeAppApprovalStore(native_approval_home())
    return {**payload, "navigation_visible": True, "available": True, "status": "ready",
        "capabilities": {**payload["capabilities"], "persistent_approvals": True, "revoke_app_approval": True,
            "persist_modes": ["session", "always"]},
        "approved_apps": [{key: app[key] for key in ("app_id", "display_name", "icon_data_url")} for app in store.list_apps()]}


def get_computer_use_settings(manager, *, session_id, conversation_id=None, workspace_root=None):
    owner, selected, identity, receiving_ids, receiving_known = _selection(manager, session_id, conversation_id, workspace_root)
    payload = _payload(owner, selected, identity, receiving_ids, receiving_known)
    if _selection(manager, session_id, conversation_id, workspace_root)[2] != identity:
        raise HTTPException(409, "The selected Computer Use provider has changed")
    return payload


def remove_computer_use_app_approval(manager, *, session_id, conversation_id, workspace_root,
        provider_id, selection_id, app_id):
    owner, selected, identity, receiving_ids, receiving_known = _selection(manager, session_id, conversation_id, workspace_root)
    provider = selected.env.get("MINICODE_CUA_PROVIDER", "installed-source") if selected else None
    if identity != selection_id or provider != provider_id:
        raise HTTPException(409, "The selected Computer Use provider has changed")
    current = _payload(owner, selected, identity, receiving_ids, receiving_known)
    if not current["capabilities"]["revoke_app_approval"]:
        raise HTTPException(503, current["reason"] or "This provider cannot revoke persistent app approvals")
    NativeAppApprovalStore(native_approval_home()).remove(app_id)
    return get_computer_use_settings(manager, session_id=session_id, conversation_id=conversation_id, workspace_root=workspace_root)
