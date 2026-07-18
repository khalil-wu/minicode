from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Iterable

from backend.feature_flags import feature_enabled


_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_LOCAL_JSX_COMPONENT_ALIASES = {
    "prompt-form": "prompt-form",
    "prompt_form": "prompt-form",
}
_PROMPT_FORM_FIELD_TYPES = {"text", "textarea", "select"}
_MAX_PROMPT_FORM_FIELDS = 12
_MAX_PROMPT_FORM_OPTIONS = 40
_MAX_PLUGIN_PAYLOAD_TEXT = 8000


def _availability(*, reason: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"kind": "always", "scope": "session"}
    if reason:
        payload["reason"] = reason
    return payload


def default_plugin_roots() -> list[Path]:
    roots: list[Path] = []
    explicit = os.environ.get("MINICODE_PLUGINS_DIR", "").strip()
    if explicit:
        roots.extend(Path(part).expanduser() for part in explicit.split(os.pathsep) if part.strip())

    minicode_home = Path(os.environ.get("MINICODE_HOME") or (Path.home() / ".minicode"))
    roots.append(minicode_home / "plugins")

    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    roots.append(codex_home / "plugins")

    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        key = str(root.resolve()) if root.exists() else str(root)
        if key in seen:
            continue
        seen.add(key)
        unique.append(root)
    return unique


def plugin_settings(settings_data: dict[str, Any] | None = None) -> dict[str, Any]:
    if settings_data is None:
        try:
            from backend.config import _load_settings_json

            settings_data = _load_settings_json()
        except Exception:
            settings_data = {}
    raw = settings_data.get("plugins") if isinstance(settings_data, dict) else {}
    return raw if isinstance(raw, dict) else {}


def disabled_plugin_names(settings_data: dict[str, Any] | None = None) -> set[str]:
    try:
        from backend.services.plugin_settings_service import get_disabled_plugin_names

        return set(get_disabled_plugin_names(settings_data))
    except Exception:
        raw = plugin_settings(settings_data).get("disabled")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return set()
        return {
            _plugin_name_key(str(name))
            for name in raw
            if _plugin_name_key(str(name))
        }


def is_plugin_enabled(plugin_name: str, settings_data: dict[str, Any] | None = None) -> bool:
    key = _plugin_name_key(plugin_name)
    if not key:
        return False
    return key not in disabled_plugin_names(settings_data)


def plugin_name_for_dir(plugin_dir: Path) -> str:
    try:
        from backend.services.plugin_settings_service import plugin_name_from_directory

        return plugin_name_from_directory(plugin_dir)
    except Exception:
        plugin_dir = Path(plugin_dir).expanduser()
        for manifest_path in (
            plugin_dir / ".codex-plugin" / "plugin.json",
            plugin_dir / "plugin.json",
            plugin_dir / "commands.json",
        ):
            if not manifest_path.is_file():
                continue
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(raw, dict):
                name = str(raw.get("name") or "").strip()
                if name:
                    return name
        return plugin_dir.name


def _plugin_name_key(plugin_name: str) -> str:
    return str(plugin_name or "").strip().casefold()


def get_plugin_composer_command_catalog(plugin_roots: Iterable[Path] | None = None) -> list[dict[str, Any]]:
    if not feature_enabled("plugin_commands", True):
        return []
    entries: list[dict[str, Any]] = []
    disabled = disabled_plugin_names()
    for manifest_path in _iter_plugin_manifests(plugin_roots or default_plugin_roots()):
        entries.extend(_commands_from_manifest(manifest_path, disabled_plugins=disabled))
    return entries


def _iter_plugin_manifests(plugin_roots: Iterable[Path]) -> list[Path]:
    manifests: list[Path] = []
    seen: set[str] = set()
    for root in plugin_roots:
        root = Path(root).expanduser()
        candidates: list[Path] = []
        if root.is_file():
            candidates.append(root)
        elif root.is_dir():
            candidates.extend(root.glob("*/.codex-plugin/plugin.json"))
            candidates.extend(root.glob("*/plugin.json"))
            candidates.extend(root.glob("*/commands.json"))
            candidates.extend(root.glob(".codex-plugin/plugin.json"))
            candidates.extend(root.glob("plugin.json"))
            candidates.extend(root.glob("commands.json"))
        for candidate in candidates:
            key = str(candidate.resolve())
            if key in seen:
                continue
            seen.add(key)
            manifests.append(candidate)
    return sorted(manifests, key=lambda path: str(path).lower())


def _commands_from_manifest(
    manifest_path: Path,
    *,
    disabled_plugins: set[str] | None = None,
    include_disabled: bool = False,
) -> list[dict[str, Any]]:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    fallback_name = manifest_path.parent.parent.name if manifest_path.parent.name == ".codex-plugin" else manifest_path.parent.name
    if isinstance(raw, list):
        plugin_name = fallback_name
        command_specs = raw
    elif isinstance(raw, dict):
        plugin_name = str(raw.get("name") or fallback_name).strip() or fallback_name
        command_specs = raw.get("commands", raw.get("slash_commands", []))
        if isinstance(command_specs, dict):
            command_specs = [
                {"name": name, **value} if isinstance(value, dict) else {"name": name, "template": value}
                for name, value in command_specs.items()
            ]
    else:
        return []
    if not include_disabled and _plugin_name_key(plugin_name) in (disabled_plugins or set()):
        return []
    if not isinstance(command_specs, list):
        return []

    entries: list[dict[str, Any]] = []
    for spec in command_specs:
        entry = _normalize_command_spec(spec, plugin_name=plugin_name, manifest_path=manifest_path)
        if entry is not None:
            entries.append(entry)
    return entries


def _normalize_command_spec(
    spec: Any,
    *,
    plugin_name: str,
    manifest_path: Path,
) -> dict[str, Any] | None:
    if not isinstance(spec, dict):
        return None
    command = str(spec.get("command") or spec.get("name") or "").strip().lstrip("/")
    if not command or not _COMMAND_NAME_RE.match(command):
        return None
    command = command.lower()

    raw_command_type = str(spec.get("type") or spec.get("kind") or "template").strip().lower()
    command_type = raw_command_type
    if command_type in {"local-ui", "ui", "ui-action", "panel", "local_jsx", "local-jsx"}:
        command_type = "local"
    if command_type not in {"template", "protocol", "local"}:
        return None
    if command_type == "template" and not feature_enabled("plugin_template_commands", True):
        return None
    if command_type == "protocol" and not feature_enabled("plugin_protocol_commands", True):
        return None
    if command_type == "local" and not feature_enabled("plugin_local_ui_commands", True):
        return None

    template = str(spec.get("template") or spec.get("prompt") or spec.get("content") or "").strip()
    if command_type == "template" and not template:
        return None
    protocol_command = _protocol_command_from_spec(spec)
    if command_type == "protocol" and not protocol_command:
        return None
    component = ""
    has_component = _has_local_jsx_component(spec)
    if command_type == "local" and raw_command_type in {"local_jsx", "local-jsx"} and has_component:
        if not feature_enabled("plugin_local_jsx_commands", True):
            return None
        component = _local_jsx_component_from_spec(spec)
        if not component:
            return None
        ui_action = "open_plugin_component"
    else:
        ui_action = _ui_action_from_spec(spec)
    if command_type == "local" and not ui_action:
        return None

    description = str(spec.get("description") or spec.get("summary") or f"{plugin_name} plugin command").strip()
    entry: dict[str, Any] = {
        "id": str(spec.get("id") or f"plugin:{plugin_name}:{command}"),
        "name": command,
        "command": command,
        "label": str(spec.get("label") or f"/{command}"),
        "description": description,
        "type": command_type,
        "kind": command_type,
        "source": "plugin",
        "source_level": "plugin",
        "plugin_name": plugin_name,
        "plugin_path": str(manifest_path.parent),
        "enabled": bool(spec.get("enabled", True)),
        "availability": spec.get("availability") if isinstance(spec.get("availability"), dict) else _availability(),
    }
    if template:
        entry["template"] = template
    if protocol_command and command_type == "protocol":
        entry["protocol_command"] = protocol_command
        entry["command_type"] = protocol_command
    if ui_action:
        entry["ui_action"] = ui_action
        if component:
            entry["component"] = component
        if ui_action.startswith("open_panel:"):
            entry["panel"] = ui_action.split(":", 1)[1]
        elif isinstance(spec.get("panel"), str) and spec.get("panel").strip():
            entry["panel"] = str(spec["panel"]).strip()
    payload = _payload_from_spec(spec, component=component)
    if payload:
        entry["payload"] = payload
    arg_key = str(spec.get("arg_key") or spec.get("argKey") or "").strip()
    if arg_key:
        entry["arg_key"] = arg_key
    if spec.get("search_text"):
        entry["search_text"] = str(spec["search_text"])
    else:
        entry["search_text"] = " ".join(part for part in ("plugin", plugin_name, command, description) if part)
    if isinstance(spec.get("args"), list):
        entry["args"] = spec["args"]
    return entry


def _protocol_command_from_spec(spec: dict[str, Any]) -> str:
    for key in ("protocol_command", "protocolCommand", "command_type", "commandType", "handler", "action"):
        value = str(spec.get(key) or "").strip()
        if value:
            return value
    return ""


def _has_local_jsx_component(spec: dict[str, Any]) -> bool:
    return any(key in spec for key in ("component", "jsx_component", "jsxComponent"))


def _local_jsx_component_from_spec(spec: dict[str, Any]) -> str:
    for key in ("component", "jsx_component", "jsxComponent"):
        value = str(spec.get(key) or "").strip().lower().replace("_", "-")
        if not value:
            continue
        return _LOCAL_JSX_COMPONENT_ALIASES.get(value, "")
    return ""


def _bounded_text(value: Any, *, max_len: int = _MAX_PLUGIN_PAYLOAD_TEXT) -> str:
    text = str(value or "").strip()
    if len(text) > max_len:
        return text[:max_len]
    return text


def _payload_from_spec(spec: dict[str, Any], *, component: str = "") -> dict[str, Any]:
    raw = spec.get("payload") if isinstance(spec.get("payload"), dict) else spec.get("data")
    if not isinstance(raw, dict):
        return {}
    if component == "prompt-form":
        return _sanitize_prompt_form_payload(raw)
    return dict(raw)


def _sanitize_prompt_form_payload(raw: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for source_key, target_key in (
        ("title", "title"),
        ("description", "description"),
        ("prompt_template", "prompt_template"),
        ("promptTemplate", "prompt_template"),
        ("submit_label", "submit_label"),
        ("submitLabel", "submit_label"),
    ):
        value = _bounded_text(raw.get(source_key))
        if value and target_key not in payload:
            payload[target_key] = value

    fields = _sanitize_prompt_form_fields(raw.get("fields"))
    if fields:
        payload["fields"] = fields
    return payload


def _sanitize_prompt_form_fields(raw_fields: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_fields, list):
        return []
    fields: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in raw_fields[:_MAX_PROMPT_FORM_FIELDS]:
        if not isinstance(raw, dict):
            continue
        name = _bounded_text(raw.get("name"), max_len=64)
        if not name or not re.match(r"^[A-Za-z0-9_.-]{1,64}$", name) or name in seen:
            continue
        seen.add(name)
        field_type = _bounded_text(raw.get("type"), max_len=32).lower()
        if field_type not in _PROMPT_FORM_FIELD_TYPES:
            field_type = "text"
        field: dict[str, Any] = {
            "name": name,
            "label": _bounded_text(raw.get("label"), max_len=120) or name,
            "type": field_type,
        }
        placeholder = _bounded_text(raw.get("placeholder"), max_len=500)
        if placeholder:
            field["placeholder"] = placeholder
        default_value = _bounded_text(raw.get("default", raw.get("defaultValue")), max_len=4000)
        if default_value:
            field["defaultValue"] = default_value
        if isinstance(raw.get("required"), bool):
            field["required"] = bool(raw["required"])
        if field_type == "select":
            options = _sanitize_prompt_form_options(raw.get("options"))
            if not options:
                continue
            field["options"] = options
        fields.append(field)
    return fields


def _sanitize_prompt_form_options(raw_options: Any) -> list[str]:
    if not isinstance(raw_options, list):
        return []
    options: list[str] = []
    seen: set[str] = set()
    for raw_option in raw_options[:_MAX_PROMPT_FORM_OPTIONS]:
        option = _bounded_text(raw_option, max_len=160)
        if not option or option in seen:
            continue
        seen.add(option)
        options.append(option)
    return options


_UI_ACTION_ALIASES = {
    "skills": "open_skills_marketplace",
    "skills_marketplace": "open_skills_marketplace",
    "open_skills": "open_skills_marketplace",
    "open_skills_marketplace": "open_skills_marketplace",
    "settings": "open_settings",
    "open_settings": "open_settings",
    "plugins": "open_settings:plugins",
    "open_plugins": "open_settings:plugins",
    "quick_open": "open_quick_open",
    "open_quick_open": "open_quick_open",
    "agent_editor": "open_agent_editor",
    "agents": "open_agent_editor",
    "open_agent_editor": "open_agent_editor",
    "automations": "open_automations",
    "automation": "open_automations",
    "open_automations": "open_automations",
    "live_artifacts": "open_live_artifacts",
    "artifacts": "open_live_artifacts",
    "open_live_artifacts": "open_live_artifacts",
}

_ALLOWED_SETTINGS_TABS = {
    "general",
    "provider",
    "connectors",
    "scheduler",
    "features",
    "plugins",
    "advanced",
    "diagnostics",
}

_ALLOWED_RIGHT_STACK_TABS = {
    "preview",
    "terminal",
    "tasks",
    "plan",
    "subagents",
    "inspector",
    "diagnostics",
}

_ALLOWED_DOCK_TABS = {
    "terminal",
    "git",
    "tasks",
    "timeline",
    "debug",
    "budget",
}

_ALLOWED_PANELS = {
    "chat",
    "diff",
    "editor",
    "preview",
    "terminal",
    "plan",
    "tasks",
    "subagents",
    "inspector",
}


def _ui_action_from_spec(spec: dict[str, Any]) -> str:
    for key in ("ui_action", "uiAction", "local_action", "localAction", "panel", "action"):
        value = str(spec.get(key) or "").strip().lower().replace("-", "_")
        if not value:
            continue
        if key == "panel":
            return _panel_action(value)
        if value in _UI_ACTION_ALIASES:
            return _UI_ACTION_ALIASES[value]
        if value.startswith("open_settings:"):
            tab = value.split(":", 1)[1].strip()
            return f"open_settings:{tab}" if tab in _ALLOWED_SETTINGS_TABS else ""
        if value.startswith("open_right_stack:"):
            tab = value.split(":", 1)[1].strip()
            return f"open_right_stack:{tab}" if tab in _ALLOWED_RIGHT_STACK_TABS else ""
        if value.startswith("open_dock:"):
            tab = value.split(":", 1)[1].strip()
            return f"open_dock:{tab}" if tab in _ALLOWED_DOCK_TABS else ""
        if value.startswith("open_panel:"):
            panel = value.split(":", 1)[1].strip()
            return _panel_action(panel)
    return ""


def _panel_action(panel: str) -> str:
    normalized = panel.strip().lower().replace("-", "_")
    if normalized in _ALLOWED_RIGHT_STACK_TABS:
        return f"open_right_stack:{normalized}"
    if normalized in _ALLOWED_PANELS:
        return f"open_panel:{normalized}"
    if normalized in _ALLOWED_DOCK_TABS:
        return f"open_dock:{normalized}"
    return ""
