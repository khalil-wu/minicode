"""Agent settings projected from, and written to, their actual config layer."""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import tomlkit

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.config import _update_settings_json, load_config_layer_stack
from backend.config_layers import ConfigLayer, ConfigLayerStack, _sanitize_project_config
from backend.hooks.runtime import raise_if_config_change_blocked, run_config_change_hook


AGENT_FIELDS = ("approval_policy", "sandbox_mode", "web_search", "network_access", "model_verbosity", "model_reasoning_summary")
MANAGED_SOURCES = frozenset({"mdm", "enterprise_managed", "session_flags", "legacy_managed_config_file", "legacy_managed_config_mdm", "policy"})
_DEFAULTS = {"approval_policy": "on-request", "sandbox_mode": "workspace-write", "web_search": "cached", "network_access": False,
             "model_verbosity": None, "model_reasoning_summary": "auto"}


def _values(config: Mapping[str, Any]) -> dict[str, Any]:
    values = {key: deepcopy(config[key]) for key in AGENT_FIELDS if key != "network_access" and key in config}
    network = config.get("sandbox_workspace_write", {})
    if "network_access" in network:
        values["network_access"] = network["network_access"]
    return values


def _scope_layers(stack: ConfigLayerStack) -> dict[str, ConfigLayer]:
    layers = stack.get_layers(include_disabled=True)
    user = next(layer for layer in reversed(layers) if layer.source.kind == "user")
    managed = next((layer for layer in reversed(layers) if layer.source.kind in MANAGED_SOURCES), None)
    projects = {f"project:{_scope_workspace(layer)}": layer for layer in layers if layer.source.kind == "project"}
    return {**projects, "user": user, **({"managed": managed} if managed is not None else {})}


def _scope_workspace(layer: ConfigLayer) -> str | None:
    return str(Path(layer.source.project_config_folder).parent) if layer.source.kind == "project" else None


def _scope_path(layer: ConfigLayer) -> str | None:
    if layer.source.kind == "project":
        return str(Path(layer.source.project_config_folder) / "config.toml")
    file = layer.source.file
    merged_files = layer.source.kind == "policy" and layer.source.source_id == "managed_files" and "; " in file
    return file if file and not merged_files and Path(file).is_absolute() else None


def _origins(stack: ConfigLayerStack) -> dict[str, dict[str, Any]]:
    origins = stack.origins()
    result = {}
    for field in AGENT_FIELDS:
        key = "sandbox_workspace_write.network_access" if field == "network_access" else field
        origin = origins.get(key)
        if origin is not None:
            result[field] = origin.to_payload()
    return result


def _settings_payload(stack: ConfigLayerStack, workspace_root: str = "") -> dict[str, Any]:
    scopes = _scope_layers(stack)
    effective = {**_DEFAULTS, **_values(stack.effective_config())}
    requirements = {key: deepcopy(stack.requirements.value_for(key)) for key in (
        "allowed_approval_policies", "allowed_sandbox_modes", "allowed_web_search_modes",
    ) if stack.requirements.value_for(key) is not None}
    default = f"project:{Path(workspace_root).resolve()}" if workspace_root else "user"
    return {"path": scopes["user"].source.file, **effective, "effective": effective,
            "default_scope_id": default if default in scopes else next(iter(scopes)),
            "scopes": [{"id": key, "kind": "project" if layer.source.kind == "project" else key,
                        "path": _scope_path(layer), "workspace_root": _scope_workspace(layer), "version": layer.version,
                        "disabled_reason": layer.disabled_reason, "values": _values(layer.config),
                        "readonly": key == "managed", "source": layer.source.to_payload()}
                       for key, layer in scopes.items()],
            "origins": _origins(stack), "requirements": requirements,
            "warnings": stack.to_payload()["startup_warnings"]}


def agent_config_settings(workspace_root: str = "") -> dict[str, Any]:
    return _settings_payload(load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None), workspace_root)


def agent_config_target(workspace_root: str = "", scope_id: str = "user") -> dict[str, str]:
    stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    layer = _scope_layers(stack).get(scope_id)
    if layer is None:
        raise ValueError("Configuration scope is no longer available.")
    path = _scope_path(layer)
    if path is None:
        raise ValueError("This configuration source has no individual file to open.")
    target = {"path": str(Path(path).resolve())}
    if layer.source.kind == "project":
        target["workspace_root"] = _scope_workspace(layer)
    return target


def _apply_updates(document: MutableMapping[str, Any], updates: dict[str, Any]) -> None:
    for key, value in updates.items():
        if key == "network_access":
            if value is None:
                if "sandbox_workspace_write" in document:
                    document["sandbox_workspace_write"].pop("network_access", None)
            else:
                if "sandbox_workspace_write" not in document:
                    document["sandbox_workspace_write"] = {}
                document["sandbox_workspace_write"]["network_access"] = value
        elif value is None:
            document.pop(key, None)
        else:
            document[key] = value


def _validate_requirements(stack: ConfigLayerStack, updates: dict[str, Any]) -> None:
    if updates.get("approval_policy") is not None:
        stack.requirements.ensure_approval_policy(updates["approval_policy"])
    for key, restriction in (("sandbox_mode", "allowed_sandbox_modes"), ("web_search", "allowed_web_search_modes")):
        allowed = stack.requirements.value_for(restriction)
        if updates.get(key) is not None and allowed is not None and updates[key] not in allowed:
            raise ValueError(f"{key} is restricted by administrator policy.")


def _write_layer(stack: ConfigLayerStack, layer: ConfigLayer, updates: dict[str, Any], expected_version: str | None) -> str:
    path = Path(_scope_path(layer))

    def document_layer(raw: Mapping[str, Any]) -> ConfigLayer:
        config = deepcopy(dict(raw))
        if layer.source.kind == "project":
            _sanitize_project_config(config)
        return ConfigLayer(layer.source, config)

    def apply(document: MutableMapping[str, Any], raw: Mapping[str, Any]) -> None:
        if expected_version is not None and document_layer(raw).version != expected_version:
            raise ValueError("Configuration changed since it was read; reload before saving.")
        resets = [key for key, value in updates.items() if value is None]
        if resets:
            candidate = deepcopy(dict(raw))
            _apply_updates(candidate, updates)
            changed_layer = document_layer(candidate)
            changed_stack = replace(stack, layers=tuple(changed_layer if item is layer else item for item in stack.layers))
            effective = {**_DEFAULTS, **_values(changed_stack.effective_config())}
            _validate_requirements(changed_stack, {key: effective[key] for key in resets})
        _apply_updates(document, updates)

    if layer.source.kind == "user" and not layer.source.profile:
        document = _update_settings_json(lambda document: apply(document, document), settings_file=path)
        return document_layer(document).version
    else:
        with file_mutation_locks([path]):
            document = tomlkit.parse(path.read_text(encoding="utf-8") if path.exists() else "")
            apply(document, document.unwrap())
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(path, tomlkit.dumps(document))
            return document_layer(document.unwrap()).version


async def update_agent_config_settings(updates: dict[str, Any], workspace_root: str = "", *,
                                       scope_id: str = "user", expected_version: str | None = None) -> dict[str, Any]:
    stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    layer = _scope_layers(stack).get(scope_id)
    if layer is None:
        raise ValueError("Configuration scope is no longer available.")
    if scope_id == "managed":
        raise ValueError("Administrator configuration is read-only.")
    if layer.is_disabled:
        raise ValueError(layer.disabled_reason)
    origins = _origins(stack)
    for field in updates:
        origin = origins.get(field)
        if origin is not None and origin["source"]["type"] in MANAGED_SOURCES:
            raise ValueError(f"{field} is managed by administrator policy.")
    _validate_requirements(stack, updates)
    path = _scope_path(layer)
    change_source = "project_settings" if layer.source.kind == "project" else "user_settings"
    result = await run_config_change_hook(source=change_source, file_path=path)
    raise_if_config_change_blocked(result, source=change_source, file_path=path)
    written_version = _write_layer(stack, layer, updates, expected_version)
    after_stack = load_config_layer_stack(cwd=Path(workspace_root) if workspace_root else None)
    after = _settings_payload(after_stack, workspace_root)
    origins = after_stack.origins()
    precedence = {item.source: index for index, item in enumerate(after_stack.get_layers())}
    fields = {}
    for key, requested in updates.items():
        effective = after["effective"][key]
        origin = after["origins"].get(key)
        field_path = "sandbox_workspace_write.network_access" if key == "network_access" else key
        overridden = origin is not None and precedence[origins[field_path].source] > precedence[layer.source] and requested != effective
        fields[key] = {"requested_value": requested, "effective_value": effective,
                       **({"overriding_source": origin} if overridden else {})}
    after["write"] = {"status": "overridden" if any("overriding_source" in field for field in fields.values()) else "applied",
                      "path": path, "version": written_version, "fields": fields}
    return after
