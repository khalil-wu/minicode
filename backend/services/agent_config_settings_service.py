"""Editable agent defaults already consumed by the execution configuration."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from backend.config import SETTINGS_FILE, _update_settings_json, load_config_layer_stack
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook


def agent_config_settings(workspace_root: str = "") -> dict[str, Any]:
    stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    effective = stack.effective_config()
    layers = stack.get_layers()
    user = next(layer for layer in layers if layer.source.kind == "user")
    return {"path": user.source.file, "approval_policy": effective.get("approval_policy", "on-request"),
            "sandbox_mode": effective.get("sandbox_mode", "workspace-write"),
            "web_search": effective.get("web_search", "cached"),
            "network_access": effective.get("sandbox_workspace_write", {}).get("network_access", False),
            "model_verbosity": effective.get("model_verbosity", "medium"),
            "model_reasoning_summary": effective.get("model_reasoning_summary", "auto")}


async def update_agent_config_settings(updates: dict[str, Any], workspace_root: str = "") -> dict[str, Any]:
    stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    if "approval_policy" in updates:
        stack.requirements.ensure_approval_policy(updates["approval_policy"])
    for key, restriction in (("sandbox_mode", "allowed_sandbox_modes"), ("web_search", "allowed_web_search_modes")):
        allowed = stack.requirements.value_for(restriction)
        if key in updates and allowed is not None and updates[key] not in allowed:
            raise ValueError(f"{key} is restricted by administrator policy.")
    result = await run_config_change_hook(source="user_settings", file_path=str(SETTINGS_FILE))
    raise_if_config_change_blocked(result, source="user_settings", file_path=str(SETTINGS_FILE))
    def apply(settings: dict[str, Any]) -> None:
        for key, value in updates.items():
            if key == "network_access": settings.setdefault("sandbox_workspace_write", {})["network_access"] = value
            else: settings[key] = value
    _update_settings_json(apply)
    after = agent_config_settings(workspace_root)
    if any(after[key] != value for key, value in updates.items()):
        raise ValueError("Another configuration layer overrides this setting.")
    return after
