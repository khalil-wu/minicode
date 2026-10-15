"""Codex agent-role TOML layers and bounded child configuration.

The reference is codex-rs/agent-roles and core/src/agent/{role,child_config}.
Markdown Agent definitions remain addressable as legacy user configuration.
"""

from __future__ import annotations

import logging
import re
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from backend.config_layers import ConfigLayer, ConfigLayerSource, ConfigLayerStack
from backend.feature_flags import FeatureFlags
from backend.llm.model_request_policy import source_model_request_capabilities

logger = logging.getLogger(__name__)
SOURCE_ROLE_NAMES = frozenset({"default", "explorer", "worker"})
BOUNDED_ROLE_FEATURES = frozenset({"shell_tool", "apps", "plugins", "memory_tool", "request_permissions_tool"})
_AGENTS_SETTINGS = frozenset({"enabled", "interrupt_message", "max_threads", "max_depth", "max_concurrent_threads_per_session", "job_max_runtime_seconds", "default_subagent_model", "default_subagent_reasoning_effort"})


@dataclass(frozen=True)
class SourceAgentRole:
    name: str
    description: str
    config_file: Path | None = None
    nickname_candidates: tuple[str, ...] | None = None
    overrides: dict[str, Any] = field(default_factory=dict)

    @property
    def model(self) -> str:
        return self.overrides.get("model", "")

    @property
    def effort(self) -> str:
        return self.overrides.get("model_reasoning_effort", "")

    @property
    def prompt(self) -> str:
        # Role instructions are developer messages, never part of the user's task.
        return ""


def _description(raw: Any, label: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{label} cannot be blank")
    return raw.strip()


def _nicknames(raw: Any, label: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw or any(not isinstance(value, str) for value in raw):
        raise ValueError(f"{label} must contain at least one name")
    names = tuple(value.strip() for value in raw)
    if any(not name or re.fullmatch(r"[A-Za-z0-9 _-]+", name) is None for name in names) or len(set(names)) != len(names):
        raise ValueError(f"{label} requires unique, non-blank ASCII names")
    return names


def _bounded_overrides(raw: dict[str, Any], base: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in ("developer_instructions", "model", "model_reasoning_effort", "model_reasoning_summary", "model_verbosity", "personality", "service_tier"):
        if name in raw:
            value = raw[name]
            if not isinstance(value, str):
                raise ValueError(f"{name} must be a string")
            if name == "developer_instructions" and not value.strip():
                raise ValueError("developer_instructions cannot be blank")
            values = {"model_reasoning_summary": {"auto", "concise", "detailed", "none"},
                "model_verbosity": {"low", "medium", "high"}, "personality": {"none", "friendly", "pragmatic"}}
            if name in values and value not in values[name]:
                raise ValueError(f"invalid {name}: {value!r}")
            result[name] = value
    raw_features = raw.get("features", {})
    if not isinstance(raw_features, dict):
        raise ValueError("features must be a TOML table")
    for name, enabled in raw_features.items():
        if name in BOUNDED_ROLE_FEATURES and not isinstance(enabled, bool):
            raise ValueError(f"features.{name} must be a boolean")
    features = {name: False for name, enabled in raw_features.items() if name in BOUNDED_ROLE_FEATURES and enabled is False}
    if features:
        result["features"] = features
    skills = raw.get("skills", {})
    if not isinstance(skills, dict) or not isinstance(skills.get("config", []), list):
        raise ValueError("skills must be a TOML table with a config array")
    if not isinstance(skills.get("bundled", {}), dict) or not isinstance(skills.get("bundled", {}).get("enabled", True), bool):
        raise ValueError("skills.bundled requires an enabled boolean")
    if "include_instructions" in skills and not isinstance(skills["include_instructions"], bool):
        raise ValueError("skills.include_instructions must be a boolean")
    disabled = []
    for entry in skills.get("config", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            raise ValueError("skills.config entries require an enabled boolean")
        if any(entry.get(key) is not None and not isinstance(entry[key], str) for key in ("path", "name")):
            raise ValueError("skills.config path and name must be strings")
        if entry["enabled"] is not False:
            continue
        selector: dict[str, Any] = {"enabled": False}
        if entry.get("path") is not None:
            selector["path"] = str((base / entry["path"]).resolve())
        if entry.get("name") is not None:
            selector["name"] = entry["name"]
        disabled.append(selector)
    projected_skills: dict[str, Any] = {}
    if disabled:
        projected_skills["config"] = disabled
    if skills.get("bundled", {}).get("enabled") is False:
        projected_skills["bundled"] = {"enabled": False}
    if skills.get("include_instructions") is False:
        projected_skills["include_instructions"] = False
    if projected_skills:
        result["skills"] = projected_skills
    return result


def read_role_file(path: Path, *, name_hint: str | None = None) -> SourceAgentRole:
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    name = raw.get("name", "")
    if not isinstance(name, str):
        raise ValueError(f"{path}.name must be a string")
    name = name.strip() or name_hint
    if name is None:
        raise ValueError(f"agent role file at {path} must define a non-empty name")
    if name_hint is None and "developer_instructions" not in raw:
        raise ValueError(f"agent role file at {path} must define developer_instructions")
    return SourceAgentRole(name, _description(raw.get("description"), f"{path}.description") or "", path,
        _nicknames(raw.get("nickname_candidates"), f"{path}.nickname_candidates"), _bounded_overrides(raw, path.parent))


def _layer_folder(layer: ConfigLayer) -> Path | None:
    source = layer.source
    if source.project_config_folder:
        return Path(source.project_config_folder)
    return Path(source.file).parent if source.file else None


def load_source_agent_roles(stack: ConfigLayerStack | None) -> dict[str, SourceAgentRole]:
    if stack is None:
        return {}
    roles: dict[str, SourceAgentRole] = {}
    for layer in stack.get_layers():
        folder = _layer_folder(layer)
        layer_roles: dict[str, SourceAgentRole] = {}
        declared_files: set[Path] = set()
        for declared_name, declaration in layer.config.get("agents", {}).items():
            if declared_name in _AGENTS_SETTINGS:
                continue
            try:
                if not isinstance(declaration, dict):
                    raise ValueError(f"agents.{declared_name} must be a TOML table")
                description = _description(declaration.get("description"), f"agents.{declared_name}.description")
                nicknames = _nicknames(declaration.get("nickname_candidates"), f"agents.{declared_name}.nickname_candidates")
                config_file = declaration.get("config_file")
                if config_file is not None:
                    path = Path(config_file)
                    if not path.is_absolute():
                        if folder is None:
                            raise ValueError("relative agent config_file requires a config folder")
                        path = folder / path
                    path = path.resolve()
                    role = read_role_file(path, name_hint=declared_name)
                    role = replace(role, description=role.description or description or "", nickname_candidates=role.nickname_candidates or nicknames)
                    declared_files.add(path)
                else:
                    role = SourceAgentRole(declared_name, description or "", nickname_candidates=nicknames)
                if role.name in layer_roles:
                    raise ValueError(f"duplicate agent role name {role.name!r} declared in the same config layer")
                layer_roles[role.name] = role
            except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
                logger.warning("Ignoring malformed agent role definition: %s", error)
        agents_dir = folder / "agents" if folder is not None else None
        if agents_dir is not None and agents_dir.is_dir():
            for path in sorted(agents_dir.rglob("*.toml")):
                if path.resolve() in declared_files:
                    continue
                try:
                    role = read_role_file(path.resolve())
                    if role.name in layer_roles:
                        raise ValueError(f"duplicate agent role name {role.name!r} discovered in {agents_dir}")
                    layer_roles[role.name] = role
                except (OSError, ValueError, tomllib.TOMLDecodeError) as error:
                    logger.warning("Ignoring malformed agent role definition: %s", error)
        for name, role in layer_roles.items():
            previous = roles.get(name)
            if previous is not None:
                role = replace(role, description=role.description or previous.description,
                    config_file=role.config_file or previous.config_file,
                    nickname_candidates=role.nickname_candidates or previous.nickname_candidates,
                    overrides=role.overrides if role.config_file is not None else previous.overrides)
            if not role.description:
                logger.warning("Ignoring malformed agent role definition: agent role %r must define a description", name)
                continue
            roles[name] = role
    return roles


def discover_execution_agents(workspace_root: str | Path | None = None, *, config: Any = None) -> dict[str, Any]:
    from backend.agents.loader import discover_agents
    from backend.config import load_config

    config = config if config is not None else load_config(cwd=workspace_root, resolve_credentials=False)
    legacy = discover_agents(workspace_root)
    names = {name: definition for name, definition in legacy.items() if name not in SOURCE_ROLE_NAMES}
    names.update({f"legacy:{name}": definition for name, definition in legacy.items() if name in SOURCE_ROLE_NAMES})
    return {**names, **load_source_agent_roles(config.config_layer_stack)}


def get_execution_agent(name: str, workspace_root: str | Path | None = None, *, config: Any = None) -> Any:
    return discover_execution_agents(workspace_root, config=config).get(name)


def source_spawn_version(config: Any, *, model: str = "", definition: Any = None) -> str:
    if config.feature_flags.enabled("multi_agent_v2"):
        return "v2"
    model_version = source_model_request_capabilities(model or config.llm.model, definition).get("multi_agent_version")
    return model_version or "v1"


def is_legacy_read_only_role(name: str, workspace_root: str | Path | None = None, *, config: Any = None) -> bool:
    return name in {"explore", "plan"} and not isinstance(get_execution_agent(name, workspace_root, config=config), SourceAgentRole)


def source_child_role(config: Any, *, role_name: str, role_requested: bool, fork_turns: str, version: str | None = None) -> dict[str, Any]:
    full_history = fork_turns == "all"
    version = version or source_spawn_version(config)
    if version == "v1" and full_history and role_requested:
        raise ValueError("Full-history forked agents inherit the parent agent type; omit agent_type, or spawn without a full-history fork.")
    if full_history and not (version == "v2" and role_requested):
        return {}
    role = load_source_agent_roles(config.config_layer_stack).get(role_name)
    return deepcopy(role.overrides) if role is not None else {}


def project_role_config(config: Any, overrides: dict[str, Any]) -> Any:
    """Apply one session layer without replacing parent-owned permissions."""
    if not overrides:
        return config
    stack = config.config_layer_stack
    if stack is None:
        stack = ConfigLayerStack(())
    role_layer = ConfigLayer(ConfigLayerSource("session_flags"), overrides)
    layers = list(stack.layers)
    index = next((index for index, layer in enumerate(layers) if layer.source.precedence > role_layer.source.precedence), len(layers))
    layers.insert(index, role_layer)
    flags = {**config.feature_flags.flags, **overrides.get("features", {})}
    return replace(config, config_layer_stack=replace(stack, layers=tuple(layers)), feature_flags=FeatureFlags(flags))


def inherit_role_restrictions(config: Any, overrides: dict[str, Any]) -> dict[str, Any]:
    """Retain all parent role restrictions through fresh spawn and cold resume."""
    parent = config.config_layer_stack.effective_config() if config.config_layer_stack is not None else {}
    inherited = {key: deepcopy(parent[key]) for key in ("developer_instructions", "model_reasoning_summary", "model_verbosity", "personality", "service_tier") if key in parent}
    features = {name: False for name, enabled in config.feature_flags.flags.items() if name in BOUNDED_ROLE_FEATURES and enabled is False}
    features.update(overrides.get("features", {}))
    if features:
        inherited["features"] = features
    parent_skills = parent.get("skills", {})
    child_skills = overrides.get("skills", {})
    skills: dict[str, Any] = {}
    disabled = {(entry.get("path"), entry.get("name")): deepcopy(entry) for entry in [*parent_skills.get("config", []), *child_skills.get("config", [])] if entry["enabled"] is False}
    if disabled:
        skills["config"] = list(disabled.values())
    if parent_skills.get("bundled", {}).get("enabled") is False or child_skills.get("bundled", {}).get("enabled") is False:
        skills["bundled"] = {"enabled": False}
    if parent_skills.get("include_instructions") is False or child_skills.get("include_instructions") is False:
        skills["include_instructions"] = False
    inherited.update({key: deepcopy(value) for key, value in overrides.items() if key not in {"features", "skills"}})
    if skills:
        inherited["skills"] = skills
    return inherited


def role_tool_denials(overrides: dict[str, Any], registry: Any) -> list[str]:
    disabled = overrides.get("features", {})
    denied: list[str] = []
    for name in registry.list_tools():
        spec = registry.get_tool_spec(name)
        owner = registry.get_tool_owner(name) or ""
        if ((disabled.get("shell_tool") is False and (spec.capability.startswith("shell.") or name == "monitor"))
            or (disabled.get("memory_tool") is False and (spec.toolset == "memory" or spec.capability.startswith("memory.") or name.startswith("memory_")))
            or (disabled.get("request_permissions_tool") is False and name == "request_permissions")
            or (disabled.get("plugins") is False and (owner.startswith("plugin:") or owner.startswith("mcp:plugin:")))
            or (disabled.get("apps") is False and (spec.toolset == "apps" or owner.startswith("app:") or owner.startswith("mcp:codex_apps")))):
            denied.append(name)
    return denied
