"""The memory policy captured with each application/run configuration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class MemorySettings:
    generate_memories: bool = True
    use_memories: bool = True
    disable_on_external_context: bool = False


def memory_settings_from_config(settings: Mapping[str, Any]) -> MemorySettings:
    values = settings.get("memories", {})
    return MemorySettings(generate_memories=values.get("generate_memories", True),
        use_memories=values.get("use_memories", True),
        disable_on_external_context=values.get("disable_on_external_context", values.get("no_memories_if_mcp_or_web_search", False)))


def memory_policy_snapshot(config) -> dict[str, bool]:
    return {"enabled": config.feature_flags.enabled("memories"), **asdict(config.memories)}


def memory_settings_for_workspace(workspace_root=None) -> MemorySettings:
    from pathlib import Path
    from backend.config import load_config_layer_stack
    return memory_settings_from_config(load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None).effective_config())
