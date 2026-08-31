from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from backend.managed_settings import (
    default_minicode_managed_dir as _default_minicode_managed_dir,
    load_minicode_managed_settings,
)


logger = logging.getLogger(__name__)

_SERVER_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_POLICY_ARRAY_FIELDS = ("allowed_mcp_servers", "denied_mcp_servers")
_CUSTOMIZATION_SURFACES = frozenset({"agents", "hooks", "mcp", "skills"})


class MCPPolicyError(ValueError):
    """An admin-owned MCP policy could not be parsed or validated."""


class MCPPolicyConfig(Protocol):
    name: str
    command: str
    args: list[str]
    transport: str
    url: str | None
    source: str


@dataclass(frozen=True)
class MCPValueMatcher:
    operation: str
    value: str

    def matches(self, candidate: str) -> bool:
        if self.operation == "exact":
            return candidate == self.value
        if self.operation == "prefix":
            return candidate.startswith(self.value)
        return re.fullmatch(self.value, candidate) is not None


@dataclass(frozen=True)
class MCPRequirement:
    kind: str
    command: str = ""
    args: tuple[MCPValueMatcher, ...] = ()
    url: MCPValueMatcher | None = None
    exact_identity: bool = False

    def matches(self, config: MCPPolicyConfig) -> bool:
        if self.kind == "command":
            if config.transport != "stdio" or config.command != self.command:
                return False
            if self.exact_identity:
                return True
            return len(self.args) == len(config.args) and all(
                matcher.matches(argument)
                for matcher, argument in zip(self.args, config.args, strict=True)
            )
        return (
            config.transport in {"sse", "http", "ws"}
            and self.url is not None
            and self.url.matches(str(config.url or ""))
        )


@dataclass(frozen=True)
class MCPPolicy:
    allowed: tuple[Mapping[str, Any], ...] | None = None
    denied: tuple[Mapping[str, Any], ...] = ()
    strict_plugin_only: bool = False
    requirements: Mapping[str, MCPRequirement] | None = None
    plugin_requirements: Mapping[str, Mapping[str, MCPRequirement]] | None = None
    source: str = ""

    def allows(self, config: MCPPolicyConfig) -> bool:
        if _policy_entry_matches(self.denied, config, deny=True):
            return False
        if self.allowed is None:
            return True
        if not self.allowed:
            return False
        command_entries = tuple(entry for entry in self.allowed if "command" in entry)
        url_entries = tuple(entry for entry in self.allowed if "url" in entry)
        if config.transport == "stdio":
            if command_entries:
                return _policy_entry_matches(command_entries, config)
            return _name_matches(self.allowed, config.name)
        if config.transport in {"sse", "http", "ws"}:
            if url_entries:
                return _policy_entry_matches(url_entries, config)
            return _name_matches(self.allowed, config.name)
        return _name_matches(self.allowed, config.name)

    def disabled_reason(self, config: MCPPolicyConfig) -> str | None:
        plugin_identity = _plugin_config_identity(config)
        if plugin_identity is not None:
            if self.plugin_requirements is None:
                return None
            plugin_name, server_name = plugin_identity
            plugin_rules = self.plugin_requirements.get(plugin_name)
            requirement = plugin_rules.get(server_name) if plugin_rules is not None else None
        else:
            if self.requirements is None:
                return None
            requirement = self.requirements.get(config.name)
        if requirement is not None and requirement.matches(config):
            return None
        return f"Disabled by MiniCode MCP requirements ({self.source})"


def default_minicode_managed_dir() -> Path:
    return _default_minicode_managed_dir()


def load_mcp_policy(
    *,
    managed_settings_dir: Path | None = None,
    config_stack: Any | None = None,
) -> MCPPolicy:
    managed_settings: Mapping[str, Any] = {}
    settings_snapshot: Mapping[str, Any] = {}
    requirements_payload: Mapping[str, Any] = {}
    requirements_source = ""
    if config_stack is not None:
        policy_config = getattr(config_stack, "policy_config", None)
        candidate = policy_config() if callable(policy_config) else None
        managed_settings = candidate if isinstance(candidate, Mapping) else {}
        effective_config = getattr(config_stack, "effective_config", None)
        candidate = (
            effective_config(apply_requirements=False)
            if callable(effective_config)
            else {}
        )
        settings_snapshot = candidate if isinstance(candidate, Mapping) else {}
        requirements = getattr(config_stack, "requirements", None)
        raw_requirements = getattr(requirements, "raw", None)
        if isinstance(raw_requirements, Mapping):
            requirements_payload = raw_requirements
        source_for = getattr(requirements, "source_for", None)
        source = source_for("mcp_servers") if callable(source_for) else None
        requirements_source = str(source or "managed requirements snapshot")
    elif managed_settings_dir is not None:
        managed_settings = load_minicode_managed_settings(
            Path(managed_settings_dir)
        ).settings

    managed = _validate_policy_settings(managed_settings)
    merged: dict[str, Any] = {}
    _merge_policy_settings(merged, _validate_policy_settings(settings_snapshot))
    _merge_policy_settings(merged, managed)
    managed_only = managed.get("allow_managed_mcp_servers_only") is True
    allow_source = managed if managed_only else merged
    allowed = allow_source.get("allowed_mcp_servers")
    denied = merged.get("denied_mcp_servers")
    strict_values = (
        managed.get("strict_plugin_only_customization"),
        requirements_payload.get("strict_plugin_only_customization"),
    )
    strict_plugin_only = any(
        value is True or (isinstance(value, list) and "mcp" in value)
        for value in strict_values
    )
    identity = mcp_policy_from_requirements(
        requirements_payload,
        source=requirements_source,
    )
    return MCPPolicy(
        allowed=tuple(allowed) if isinstance(allowed, list) else None,
        denied=tuple(denied) if isinstance(denied, list) else (),
        strict_plugin_only=strict_plugin_only,
        requirements=identity.requirements,
        plugin_requirements=identity.plugin_requirements,
        source=identity.source,
    )


def mcp_policy_from_requirements(
    payload: Mapping[str, Any],
    *,
    source: str,
) -> MCPPolicy:
    requirements = _parse_requirement_map(payload.get("mcp_servers"), field="mcp_servers")
    plugin_payload = payload.get("plugins")
    plugin_requirements: dict[str, Mapping[str, MCPRequirement]] | None = None
    if plugin_payload is not None:
        if not isinstance(plugin_payload, Mapping):
            raise MCPPolicyError("MiniCode requirements plugins must be a table")
        plugin_requirements = {}
        for plugin_name, raw_plugin in plugin_payload.items():
            if not isinstance(raw_plugin, Mapping):
                raise MCPPolicyError(
                    f"MiniCode requirements plugins.{plugin_name} must be a table"
                )
            raw_mcp = raw_plugin.get("mcp_servers")
            parsed = _parse_requirement_map(
                raw_mcp,
                field=f"plugins.{plugin_name}.mcp_servers",
            )
            if parsed is not None:
                plugin_requirements[str(plugin_name)] = parsed
    strict = payload.get("strict_plugin_only_customization")
    strict_plugin_only = strict is True or (
        isinstance(strict, list) and "mcp" in strict
    )
    return MCPPolicy(
        strict_plugin_only=strict_plugin_only,
        requirements=requirements,
        plugin_requirements=plugin_requirements,
        source=source,
    )


def load_enterprise_mcp_payload(
    managed_settings_dir: Path | None = None,
) -> tuple[Path, Mapping[str, Any] | None]:
    path = Path(managed_settings_dir or default_minicode_managed_dir()) / "managed-mcp.json"
    if not path.is_file():
        return path, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPPolicyError(f"Failed to read enterprise MCP config {path}: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise MCPPolicyError(f"Enterprise MCP config {path} must contain an object")
    unknown_fields = sorted(set(payload) - {"servers"})
    if unknown_fields:
        raise MCPPolicyError(
            f"Enterprise MCP config {path} contains unsupported fields: "
            + ", ".join(str(field) for field in unknown_fields)
        )
    servers = payload.get("servers")
    if not isinstance(servers, Mapping):
        raise MCPPolicyError(f"Enterprise MCP config {path} must contain a servers object")
    return path, servers


def _validate_policy_settings(payload: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field_name in _POLICY_ARRAY_FIELDS:
        if field_name not in payload:
            continue
        entries = payload[field_name]
        if not isinstance(entries, list):
            raise MCPPolicyError(f"{field_name} must be an array")
        result[field_name] = [
            _validate_policy_entry(entry, field=f"{field_name}[{index}]")
            for index, entry in enumerate(entries)
        ]
    managed_only = payload.get("allow_managed_mcp_servers_only")
    if managed_only is not None:
        if not isinstance(managed_only, bool):
            raise MCPPolicyError("allow_managed_mcp_servers_only must be a boolean")
        result["allow_managed_mcp_servers_only"] = managed_only
    if "strict_plugin_only_customization" in payload:
        strict = payload["strict_plugin_only_customization"]
        if isinstance(strict, bool):
            result["strict_plugin_only_customization"] = strict
        elif isinstance(strict, list):
            result["strict_plugin_only_customization"] = [
                item
                for item in strict
                if isinstance(item, str) and item in _CUSTOMIZATION_SURFACES
            ]
    return result


def _validate_policy_entry(entry: Any, *, field: str) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise MCPPolicyError(f"{field} must be an object")
    keys = [
        key
        for key in ("name", "command", "url")
        if key in entry and entry[key] is not None
    ]
    if len(keys) != 1:
        raise MCPPolicyError(
            f"{field} must have exactly one of name, command, or url"
        )
    key = keys[0]
    value = entry[key]
    if key == "name":
        if not isinstance(value, str) or _SERVER_NAME_RE.fullmatch(value) is None:
            raise MCPPolicyError(f"{field}.name is invalid")
    elif key == "command":
        if (
            not isinstance(value, list)
            or not value
            or any(not isinstance(item, str) for item in value)
        ):
            raise MCPPolicyError(f"{field}.command must be a non-empty string array")
        value = list(value)
    elif not isinstance(value, str):
        raise MCPPolicyError(f"{field}.url must be a string")
    return {key: value}


def _merge_policy_settings(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(target.get(key), list) and isinstance(value, list):
            target[key].extend(value)
        else:
            target[key] = list(value) if isinstance(value, list) else value


def _name_matches(entries: tuple[Mapping[str, Any], ...], name: str) -> bool:
    return any(entry.get("name") == name for entry in entries)


def _policy_entry_matches(
    entries: tuple[Mapping[str, Any], ...],
    config: MCPPolicyConfig,
    *,
    deny: bool = False,
) -> bool:
    if deny and _name_matches(entries, config.name):
        return True
    if config.transport == "stdio":
        command = [config.command, *config.args]
        return any(entry.get("command") == command for entry in entries)
    if config.transport in {"sse", "http", "ws"}:
        url = str(config.url or "")
        return any(
            isinstance(pattern, str) and _url_matches(url, pattern)
            for entry in entries
            if (pattern := entry.get("url")) is not None
        )
    return False


def _url_matches(url: str, pattern: str) -> bool:
    expression = re.escape(pattern).replace(r"\*", ".*")
    return re.fullmatch(expression, url) is not None


def _parse_requirement_map(
    value: Any,
    *,
    field: str,
) -> dict[str, MCPRequirement] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MCPPolicyError(f"MiniCode requirements {field} must be a table")
    return {
        str(name): _parse_requirement(requirement, field=f"{field}.{name}")
        for name, requirement in value.items()
    }


def _parse_requirement(value: Any, *, field: str) -> MCPRequirement:
    if not isinstance(value, Mapping) or not isinstance(value.get("identity"), Mapping):
        raise MCPPolicyError(f"MiniCode requirement {field} must contain an identity table")
    identity = value["identity"]
    keys = [key for key in ("command", "url") if key in identity]
    if len(keys) != 1:
        raise MCPPolicyError(
            f"MiniCode requirement {field}.identity must contain exactly one of command or url"
        )
    key = keys[0]
    matcher = identity[key]
    if isinstance(matcher, str):
        if key == "command":
            return MCPRequirement(
                kind="command",
                command=matcher,
                exact_identity=True,
            )
        return MCPRequirement(
            kind="url",
            url=MCPValueMatcher("exact", matcher),
            exact_identity=True,
        )
    if not isinstance(matcher, Mapping):
        raise MCPPolicyError(f"MiniCode requirement {field}.identity.{key} is invalid")
    if key == "command":
        if set(matcher) != {"executable", "args"}:
            raise MCPPolicyError(
                f"MiniCode requirement {field}.identity.command has unknown or missing fields"
            )
        executable = matcher.get("executable")
        args = matcher.get("args")
        if not isinstance(executable, str) or not isinstance(args, list):
            raise MCPPolicyError(
                f"MiniCode requirement {field}.identity.command requires executable and args"
            )
        return MCPRequirement(
            kind="command",
            command=executable,
            args=tuple(
                _parse_value_matcher(item, field=f"{field}.identity.command.args[{index}]")
                for index, item in enumerate(args)
            ),
        )
    return MCPRequirement(
        kind="url",
        url=_parse_value_matcher(matcher, field=f"{field}.identity.url"),
    )


def _parse_value_matcher(value: Any, *, field: str) -> MCPValueMatcher:
    if not isinstance(value, Mapping):
        raise MCPPolicyError(f"MiniCode matcher {field} must be a table")
    operation = value.get("match")
    expected = {"match", "expression"} if operation == "regex" else {"match", "value"}
    if operation not in {"exact", "prefix", "regex"} or set(value) != expected:
        raise MCPPolicyError(f"MiniCode matcher {field} is invalid")
    candidate = value.get("expression" if operation == "regex" else "value")
    if not isinstance(candidate, str):
        raise MCPPolicyError(f"MiniCode matcher {field} requires a string value")
    if operation == "regex":
        try:
            re.compile(candidate)
            re.compile(rf"\A(?:{candidate})\Z")
        except re.error as exc:
            raise MCPPolicyError(f"MiniCode matcher {field} has invalid regex: {exc}") from exc
    return MCPValueMatcher(operation, candidate)


def _plugin_config_identity(config: MCPPolicyConfig) -> tuple[str, str] | None:
    if not config.source.startswith("plugin:"):
        return None
    plugin_name = config.source.removeprefix("plugin:")
    prefix = f"plugin:{plugin_name}:"
    server_name = config.name.removeprefix(prefix)
    return plugin_name, server_name
