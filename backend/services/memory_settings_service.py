"""Host-owned memory preferences; changing them never rewrites thread modes."""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path

from backend.application_network import reload_application_config
from backend.config import _update_settings_json, load_config, load_config_layer_stack
from backend.feature_flags import load_feature_flags, write_feature_values
from backend.hooks.runtime import raise_if_config_change_blocked
from backend.memory.settings import memory_settings_from_config

MEMORY_FIELDS = ("generate_memories", "use_memories", "disable_on_external_context")
MANAGED_SOURCES = {"mdm", "enterprise_managed", "policy", "session_flags", "legacy_managed_config_file", "legacy_managed_config_mdm"}


def get_memory_settings() -> dict:
    stack = load_config_layer_stack(cwd=None)
    settings = stack.effective_config()
    features = load_feature_flags(settings, managed_requirements=stack.requirements.feature_requirements)
    memories = memory_settings_from_config(settings)
    origins = stack.origins()
    locked = [name for name in MEMORY_FIELDS if (origin := origins.get(f"memories.{name}")) is not None and origin.source.kind in MANAGED_SOURCES]
    feature_locked = "memories" in stack.requirements.feature_requirements or "MINICODE_FEATURE_MEMORIES" in os.environ
    feature_origin = origins.get("features.memories")
    feature_locked = feature_locked or feature_origin is not None and feature_origin.source.kind in MANAGED_SOURCES
    user = next(layer for layer in stack.get_layers() if layer.source.kind == "user")
    return {"feature_enabled": features.enabled("memories"), "memories": asdict(memories), "path": user.source.file,
        "locked_fields": locked, "feature_locked": bool(feature_locked)}


async def update_memory_settings(updates: dict, *, settings_file: Path, config_change_hook):
    before = get_memory_settings()
    if "enabled" in updates and (before["feature_locked"] or any(name in before["locked_fields"] for name in ("generate_memories", "use_memories"))):
        raise ValueError("Memory preferences are managed by administrator or environment configuration.")
    if "disable_on_external_context" in updates and "disable_on_external_context" in before["locked_fields"]:
        raise ValueError("The external-context memory preference is managed by administrator configuration.")
    hook = await config_change_hook(source="user_settings", file_path=str(settings_file))
    raise_if_config_change_blocked(hook, source="user_settings", file_path=str(settings_file))

    def apply(document):
        memories = document.setdefault("memories", {})
        if "enabled" in updates:
            enabled = updates["enabled"]
            write_feature_values(document, {"memories": enabled})
            memories["generate_memories"] = enabled
            memories["use_memories"] = enabled
        if "disable_on_external_context" in updates:
            memories["disable_on_external_context"] = updates["disable_on_external_context"]
            memories.pop("no_memories_if_mcp_or_web_search", None)

    _update_settings_json(apply, settings_file=settings_file)
    config = reload_application_config(load_config)
    return get_memory_settings(), config
