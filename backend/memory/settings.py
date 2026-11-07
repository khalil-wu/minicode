"""The memory policy captured with each application/run configuration."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Mapping


class MemoryVersion(StrEnum):
    V1 = "v1"
    V2 = "v2"


@dataclass(frozen=True)
class MemorySettings:
    generate_memories: bool = True
    use_memories: bool = True
    disable_on_external_context: bool = False
    version: MemoryVersion = MemoryVersion.V1
    dual_write: bool = False

    def generation_versions(self) -> tuple[MemoryVersion, ...]:
        return (MemoryVersion.V1, MemoryVersion.V2) if self.dual_write and self.version is MemoryVersion.V1 else (self.version,)


def memory_settings_from_config(settings: Mapping[str, Any], *, enabled: bool = False) -> MemorySettings:
    values = settings.get("memories", {})
    selected = values.get("version")
    if selected is None and enabled and values.get("dual_write") is not False:
        from backend.memory.file_memory import MEMORY_DIR
        try:
            MEMORY_DIR.stat()
        except FileNotFoundError:
            selected = MemoryVersion.V2
        except OSError:
            # Source preserves the V1 selection when absence cannot be proved.
            selected = MemoryVersion.V1
    return MemorySettings(generate_memories=values.get("generate_memories", True),
        use_memories=values.get("use_memories", True),
        disable_on_external_context=values.get("disable_on_external_context", values.get("no_memories_if_mcp_or_web_search", False)),
        version=MemoryVersion(selected if selected is not None else MemoryVersion.V1), dual_write=values.get("dual_write", False))


def memory_policy_snapshot(config) -> dict[str, Any]:
    return {"enabled": config.feature_flags.enabled("memories"), **asdict(config.memories)}


def memory_settings_for_workspace(workspace_root=None) -> MemorySettings:
    from pathlib import Path
    from backend.config import load_config_layer_stack
    from backend.feature_flags import load_feature_flags
    stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    settings = stack.effective_config()
    features = load_feature_flags(settings, managed_requirements=stack.requirements.feature_requirements)
    return memory_settings_from_config(settings, enabled=features.enabled("memories"))
