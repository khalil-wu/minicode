from __future__ import annotations

import copy
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Iterable, Mapping, TypeVar

from backend.plugins.identity import parse_plugin_id_strict


T = TypeVar("T")

_APPROVAL_POLICIES = frozenset({"untrusted", "on-request", "granular", "never"})
_APPROVAL_REVIEWERS = frozenset({"user", "auto_review"})
_SANDBOX_MODES = frozenset(
    {"read-only", "workspace-write", "danger-full-access", "external-sandbox"}
)
_WEB_SEARCH_MODES = frozenset({"disabled", "cached", "indexed", "live"})
_CUSTOMIZATION_SURFACES = frozenset({"agents", "hooks", "mcp", "skills"})


class ConfigRequirementsError(ValueError):
    pass


class RequirementViolation(ConfigRequirementsError):
    def __init__(
        self,
        field: str,
        candidate: object,
        allowed: object,
        source: "RequirementSource | None",
    ) -> None:
        source_label = str(source) if source is not None else "<unspecified>"
        super().__init__(
            f"Managed requirement rejected {field}={candidate!r}; "
            f"allowed={allowed!r}; source={source_label}"
        )
        self.field = field
        self.candidate = candidate
        self.allowed = allowed
        self.source = source


@dataclass(frozen=True)
class RequirementSource:
    kind: str
    location: str = ""
    name: str = ""
    source_id: str = ""

    def __str__(self) -> str:
        if self.kind == "system_requirements_toml":
            return self.location or "system requirements.toml"
        if self.kind == "enterprise_managed":
            label = self.name or "enterprise-managed requirements"
            return f"{label} ({self.source_id})" if self.source_id else label
        if self.kind == "mdm_managed_preferences":
            return f"MDM {self.location}" if self.location else "MDM managed preferences"
        if self.kind == "legacy_managed_config":
            return self.location or "legacy managed config"
        if self.kind == "composite":
            return self.location or "composite requirements"
        return self.location or self.name or "<unspecified>"

    def to_payload(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "location": self.location,
            "name": self.name,
            "id": self.source_id,
            "display": str(self),
        }


@dataclass(frozen=True)
class Sourced(Generic[T]):
    value: T
    source: RequirementSource


@dataclass(frozen=True)
class RequirementsLayerEntry:
    source: RequirementSource
    requirements: Mapping[str, Any]
    base_dir: Path | None = None


@dataclass(frozen=True)
class ConfigRequirements:
    raw: Mapping[str, Any] = field(default_factory=dict)
    sources: Mapping[str, RequirementSource] = field(default_factory=dict)

    def source_for(self, field_name: str) -> RequirementSource | None:
        return self.sources.get(field_name)

    def value_for(self, field_name: str, default: Any = None) -> Any:
        return self.raw.get(field_name, default)

    def sourced(self, field_name: str) -> Sourced[Any] | None:
        if field_name not in self.raw:
            return None
        return Sourced(self.raw[field_name], self.sources.get(field_name, RequirementSource("unknown")))

    @property
    def feature_requirements(self) -> Mapping[str, bool]:
        raw = self.raw.get("features", self.raw.get("feature_requirements", {}))
        return raw if isinstance(raw, Mapping) else {}

    @property
    def allow_managed_hooks_only(self) -> bool | None:
        value = self.raw.get("allow_managed_hooks_only")
        return value if isinstance(value, bool) else None

    @property
    def allow_managed_permission_rules_only(self) -> bool | None:
        value = self.raw.get("allow_managed_permission_rules_only")
        return value if isinstance(value, bool) else None

    @property
    def disable_all_hooks(self) -> bool | None:
        value = self.raw.get("disable_all_hooks")
        return value if isinstance(value, bool) else None

    @property
    def allowed_http_hook_urls(self) -> tuple[str, ...] | None:
        value = self.raw.get("allowed_http_hook_urls")
        if value is None:
            return None
        return tuple(str(item) for item in value) if isinstance(value, list) else ()

    @property
    def http_hook_allowed_env_vars(self) -> tuple[str, ...] | None:
        value = self.raw.get("http_hook_allowed_env_vars")
        if value is None:
            return None
        return tuple(str(item) for item in value) if isinstance(value, list) else ()

    @property
    def enabled_plugins(self) -> Mapping[str, Any]:
        value = self.raw.get("enabled_plugins")
        return value if isinstance(value, Mapping) else {}

    @property
    def strict_known_marketplaces(self) -> tuple[Mapping[str, Any], ...] | None:
        value = self.raw.get("strict_known_marketplaces")
        if value is None:
            return None
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @property
    def blocked_marketplaces(self) -> tuple[Mapping[str, Any], ...]:
        value = self.raw.get("blocked_marketplaces")
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @property
    def plugin_trust_message(self) -> str:
        value = self.raw.get("plugin_trust_message")
        return value if isinstance(value, str) else ""

    @property
    def sandbox_settings(self) -> Mapping[str, Any]:
        value = self.raw.get("sandbox")
        return value if isinstance(value, Mapping) else {}

    @property
    def network_constraints(self) -> Mapping[str, Any]:
        value = self.raw.get("network")
        return value if isinstance(value, Mapping) else {}

    @property
    def marketplace_requirements(self) -> Mapping[str, Any]:
        value = self.raw.get("marketplaces")
        return value if isinstance(value, Mapping) else {}

    def restricts_customization_to_plugins(self, surface: str) -> bool:
        value = self.raw.get("strict_plugin_only_customization")
        return value is True or (
            isinstance(value, list) and str(surface) in value
        )

    @property
    def filesystem_deny_read(self) -> tuple[str, ...]:
        permissions = self.raw.get("permissions")
        filesystem = permissions.get("filesystem") if isinstance(permissions, Mapping) else None
        deny_read = filesystem.get("deny_read") if isinstance(filesystem, Mapping) else None
        if not isinstance(deny_read, list):
            return ()
        return tuple(str(value) for value in deny_read if isinstance(value, str) and value)

    def ensure_feature_value(self, name: str, value: bool) -> None:
        requirements = self.feature_requirements
        if name not in requirements:
            return
        required = bool(requirements[name])
        if bool(value) != required:
            raise RequirementViolation(
                f"features.{name}",
                bool(value),
                required,
                self.source_for("features") or self.source_for("feature_requirements"),
            )

    def apply_exact_to_config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        effective = copy.deepcopy(dict(config))
        if self.feature_requirements:
            raw_flags = effective.get("feature_flags")
            flags = dict(raw_flags) if isinstance(raw_flags, Mapping) else {}
            for name, required in self.feature_requirements.items():
                flags[str(name)] = bool(required)
            effective["feature_flags"] = flags
        return effective

    def ensure_permission_mode(self, mode: str) -> None:
        normalized = str(mode or "").strip().lower().replace("-", "_")
        approval_policy, sandbox_mode = permission_mode_requirements(normalized)
        allowed_approval = self.raw.get("allowed_approval_policies")
        # codex enforces allowed_approval_policies for EVERY requested policy
        # (config requirements raise, not silently converge); bypass is not
        # special-cased.
        if (
            isinstance(allowed_approval, list)
            and approval_policy not in allowed_approval
        ):
            raise RequirementViolation(
                "approval_policy",
                approval_policy,
                allowed_approval,
                self.source_for("allowed_approval_policies"),
            )
        allowed_sandbox = self.raw.get("allowed_sandbox_modes")
        if isinstance(allowed_sandbox, list) and sandbox_mode not in allowed_sandbox:
            raise RequirementViolation(
                "sandbox_mode",
                sandbox_mode,
                allowed_sandbox,
                self.source_for("allowed_sandbox_modes"),
            )

    def approval_policy_for_mode(self, mode: str) -> str:
        requested, _sandbox = permission_mode_requirements(mode)
        allowed = self.raw.get("allowed_approval_policies")
        if not isinstance(allowed, list) or requested in allowed:
            return requested
        return str(allowed[0])

    def sandbox_mode_for_permission_mode(self, mode: str) -> str:
        _approval, sandbox = permission_mode_requirements(mode)
        return sandbox

    def resolve_permission_mode(self, mode: str) -> tuple[str, RequirementViolation | None]:
        try:
            self.ensure_permission_mode(mode)
            return mode, None
        except RequirementViolation as violation:
            # Managed policy can reject a requested mode, but it must never
            # replace that explicit choice with a permitted mode. Keep the
            # requested token and return structured evidence to the caller.
            return mode, violation

    def to_payload(self) -> dict[str, Any]:
        return {
            "configured": bool(self.raw),
            "fields": {
                key: {
                    "value": _redact_requirement_value(key, value),
                    "source": (
                        self.sources[key].to_payload() if key in self.sources else None
                    ),
                }
                for key, value in sorted(self.raw.items())
            },
        }


def permission_mode_requirements(mode: str) -> tuple[str, str]:
    normalized = str(mode or "").strip().lower().replace("-", "_")
    if normalized not in {"plan", "confirm", "bypass", "auto"}:
        raise ValueError(f"Unsupported permission mode: {mode!r}")
    if normalized == "bypass":
        return "never", "danger-full-access"
    if normalized == "plan":
        return "on-request", "read-only"
    if normalized == "auto":
        return "on-request", "workspace-write"
    return "on-request", "workspace-write"


def default_requirements_path() -> Path:
    configured = str(os.environ.get("MINICODE_REQUIREMENTS_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        program_data = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
        return program_data / "MiniCode" / "requirements.toml"
    return Path("/etc/minicode/requirements.toml")


def load_requirements_toml(
    path: Path,
    *,
    source: RequirementSource | None = None,
    required: bool = False,
) -> RequirementsLayerEntry | None:
    resolved = Path(path).expanduser()
    try:
        contents = resolved.read_bytes()
    except FileNotFoundError:
        if required:
            raise ConfigRequirementsError(f"Requirements file not found: {resolved}")
        return None
    except OSError as exc:
        raise ConfigRequirementsError(f"Failed to read requirements {resolved}: {exc}") from exc
    try:
        parsed = tomllib.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigRequirementsError(f"Invalid requirements TOML {resolved}: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigRequirementsError(f"Requirements {resolved} must contain a TOML table")
    layer_source = source or RequirementSource(
        "system_requirements_toml", location=str(resolved.resolve())
    )
    base_dir = resolved.resolve().parent
    return RequirementsLayerEntry(
        layer_source,
        _normalize_requirements(parsed, layer_source, base_dir=base_dir),
        base_dir,
    )


def compose_requirements(
    layers: Iterable[RequirementsLayerEntry],
) -> ConfigRequirements:
    merged: dict[str, Any] = {}
    sources: dict[str, RequirementSource] = {}
    for layer in layers:
        payload = _normalize_requirements(
            layer.requirements,
            layer.source,
            base_dir=layer.base_dir,
        )
        existing = copy.deepcopy(merged)
        _merge_requirement_layer(merged, payload, existing)
        for key in payload:
            previous_source = sources.get(key)
            if (
                previous_source is not None
                and isinstance(existing.get(key), Mapping)
                and isinstance(payload.get(key), Mapping)
            ):
                sources[key] = RequirementSource(
                    "composite",
                    location=f"requirements layers: {layer.source}, {previous_source}",
                )
            else:
                sources[key] = layer.source
    _validate_composed_requirements(merged, sources)
    return ConfigRequirements(raw=merged, sources=sources)


def load_config_requirements(
    *,
    system_path: Path | None = None,
    enterprise_layers: Iterable[RequirementsLayerEntry] = (),
    mdm_layers: Iterable[RequirementsLayerEntry] = (),
) -> ConfigRequirements:
    layers: list[RequirementsLayerEntry] = []
    system = load_requirements_toml(system_path or default_requirements_path())
    if system is not None:
        layers.append(system)
    layers.extend(enterprise_layers)
    layers.extend(mdm_layers)
    return compose_requirements(layers)


def _normalize_requirements(
    payload: Mapping[str, Any],
    source: RequirementSource,
    *,
    base_dir: Path | None = None,
) -> dict[str, Any]:
    normalized = copy.deepcopy(dict(payload))
    if "feature_requirements" in normalized and "features" not in normalized:
        normalized["features"] = normalized.pop("feature_requirements")
    if "experimental_network" in normalized and "network" in normalized:
        raise ConfigRequirementsError(
            f"experimental_network and network cannot both be set in {source}"
        )
    if "experimental_network" in normalized:
        normalized["network"] = normalized.pop("experimental_network")
    _normalize_network_requirements(normalized, source)
    _normalize_filesystem_requirements(normalized, base_dir)
    reviewer = normalized.get("allowed_approvals_reviewers")
    if isinstance(reviewer, list):
        normalized["allowed_approvals_reviewers"] = [
            "auto_review" if value == "guardian_subagent" else value for value in reviewer
        ]
    policies = normalized.get("allowed_approval_policies")
    if isinstance(policies, list):
        normalized["allowed_approval_policies"] = [
            "on-request" if value == "on-failure" else value for value in policies
        ]
    _validate_layer_requirements(normalized, source)
    return normalized


def _validate_layer_requirements(
    payload: Mapping[str, Any], source: RequirementSource
) -> None:
    _validate_string_list(
        payload,
        "allowed_approval_policies",
        _APPROVAL_POLICIES,
        source,
    )
    _validate_string_list(
        payload,
        "allowed_approvals_reviewers",
        _APPROVAL_REVIEWERS,
        source,
    )
    _validate_string_list(payload, "allowed_sandbox_modes", _SANDBOX_MODES, source)
    _validate_string_list(payload, "allowed_web_search_modes", _WEB_SEARCH_MODES, source)
    for bool_field in (
        "allow_managed_permission_rules_only",
        "allow_managed_hooks_only",
        "disable_all_hooks",
        "allow_appshots",
        "allow_remote_control",
        "check_for_update_on_startup",
        "allow_login_shell",
    ):
        value = payload.get(bool_field)
        if value is not None and not isinstance(value, bool):
            raise ConfigRequirementsError(
                f"{bool_field} in {source} must be a boolean"
            )
    for string_array_field in (
        "allowed_http_hook_urls",
        "http_hook_allowed_env_vars",
    ):
        value = payload.get(string_array_field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise ConfigRequirementsError(
                f"{string_array_field} in {source} must be a string array"
            )

    enabled_plugins = payload.get("enabled_plugins")
    if enabled_plugins is not None:
        if not isinstance(enabled_plugins, Mapping):
            raise ConfigRequirementsError(
                f"enabled_plugins in {source} must be a table/object"
            )
        for plugin_id, value in enabled_plugins.items():
            if not isinstance(plugin_id, str) or not plugin_id.strip():
                raise ConfigRequirementsError(
                    f"enabled_plugins in {source} must use non-empty string keys"
                )
            try:
                # Keep the requirements layer on the same canonical identity
                # grammar as Claude/Codex.  Accepting malformed keys here and
                # silently dropping them later creates a policy bypass: the
                # UI reports a configured rule while runtime consumers never
                # see it.  Validate at the source boundary instead.
                parse_plugin_id_strict(plugin_id)
            except ValueError as exc:
                raise ConfigRequirementsError(
                    f"enabled_plugins.{plugin_id} in {source} has invalid plugin id: {exc}"
                ) from exc
            if isinstance(value, bool):
                continue
            if not isinstance(value, list) or any(
                not isinstance(item, str) or not item.strip() for item in value
            ):
                raise ConfigRequirementsError(
                    f"enabled_plugins.{plugin_id} in {source} must be a boolean or string array"
                )

    for marketplace_field in ("strict_known_marketplaces", "blocked_marketplaces"):
        value = payload.get(marketplace_field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, Mapping) for item in value)
        ):
            raise ConfigRequirementsError(
                f"{marketplace_field} in {source} must be an array of objects"
            )
    trust_message = payload.get("plugin_trust_message")
    if trust_message is not None and not isinstance(trust_message, str):
        raise ConfigRequirementsError(
            f"plugin_trust_message in {source} must be a string"
        )
    strict_customization = payload.get("strict_plugin_only_customization")
    if strict_customization is not None:
        if isinstance(strict_customization, bool):
            pass
        elif not isinstance(strict_customization, list) or any(
            not isinstance(item, str) or item not in _CUSTOMIZATION_SURFACES
            for item in strict_customization
        ):
            raise ConfigRequirementsError(
                f"strict_plugin_only_customization in {source} must be a boolean "
                "or an array containing agents, hooks, mcp, and/or skills"
            )
    for mapping_field in ("hooks", "sandbox", "permissions"):
        value = payload.get(mapping_field)
        if value is not None and not isinstance(value, Mapping):
            raise ConfigRequirementsError(
                f"{mapping_field} in {source} must be a table/object"
            )
    _validate_network_requirements(payload.get("network"), source)
    _validate_marketplace_requirements(payload.get("marketplaces"), source)
    plugins = payload.get("plugins")
    if plugins is not None:
        if not isinstance(plugins, Mapping) or any(
            not isinstance(name, str) or not isinstance(value, Mapping)
            for name, value in plugins.items()
        ):
            raise ConfigRequirementsError(
                f"plugins in {source} must be a plugin-name-to-table mapping"
            )
    features = payload.get("features")
    if features is not None:
        if not isinstance(features, Mapping):
            raise ConfigRequirementsError(f"features in {source} must be a table")
        invalid = [name for name, value in features.items() if not isinstance(value, bool)]
        if invalid:
            raise ConfigRequirementsError(
                f"features in {source} must contain only booleans: {', '.join(map(str, invalid))}"
            )
    profiles = payload.get("allowed_permission_profiles")
    if profiles is not None and (
        not isinstance(profiles, Mapping)
        or any(not isinstance(value, bool) for value in profiles.values())
    ):
        raise ConfigRequirementsError(
            f"allowed_permission_profiles in {source} must be a string-to-boolean table"
        )
    default_permissions = payload.get("default_permissions")
    if default_permissions is not None and (
        not isinstance(default_permissions, str) or not default_permissions.strip()
    ):
        raise ConfigRequirementsError(
            f"default_permissions in {source} must be a non-empty string"
        )
    permissions = payload.get("permissions")
    if permissions is not None and not isinstance(permissions, Mapping):
        raise ConfigRequirementsError(f"permissions in {source} must be a table")
    filesystem = permissions.get("filesystem") if isinstance(permissions, Mapping) else None
    if filesystem is not None and not isinstance(filesystem, Mapping):
        raise ConfigRequirementsError(f"permissions.filesystem in {source} must be a table")
    deny_read = filesystem.get("deny_read") if isinstance(filesystem, Mapping) else None
    if deny_read is not None and (
        not isinstance(deny_read, list)
        or any(not isinstance(value, str) or not value for value in deny_read)
    ):
        raise ConfigRequirementsError(
            f"permissions.filesystem.deny_read in {source} must be a non-empty string array"
        )


def _validate_string_list(
    payload: Mapping[str, Any],
    field_name: str,
    accepted: frozenset[str],
    source: RequirementSource,
) -> None:
    value = payload.get(field_name)
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigRequirementsError(f"{field_name} in {source} must be a string array")
    unknown = [item for item in value if item not in accepted]
    if unknown:
        raise ConfigRequirementsError(
            f"{field_name} in {source} contains unsupported values: {unknown!r}"
        )


def _normalize_network_requirements(
    payload: dict[str, Any], source: RequirementSource
) -> None:
    network = payload.get("network")
    if network is None:
        return
    if not isinstance(network, Mapping):
        raise ConfigRequirementsError(f"network in {source} must be a table/object")
    normalized = copy.deepcopy(dict(network))
    domains = normalized.get("domains")
    legacy_allowed = normalized.get("allowed_domains")
    legacy_denied = normalized.get("denied_domains")
    if domains is not None and (legacy_allowed is not None or legacy_denied is not None):
        raise ConfigRequirementsError(
            f"network.domains in {source} cannot be combined with allowed_domains or denied_domains"
        )
    if domains is None and (legacy_allowed is not None or legacy_denied is not None):
        compiled: dict[str, str] = {}
        for pattern in legacy_allowed or []:
            compiled[str(pattern)] = "allow"
        for pattern in legacy_denied or []:
            compiled[str(pattern)] = "deny"
        if compiled:
            normalized["domains"] = compiled
        normalized.pop("allowed_domains", None)
        normalized.pop("denied_domains", None)
    unix_sockets = normalized.get("unix_sockets")
    legacy_sockets = normalized.get("allow_unix_sockets")
    if unix_sockets is not None and legacy_sockets is not None:
        raise ConfigRequirementsError(
            f"network.unix_sockets in {source} cannot be combined with allow_unix_sockets"
        )
    if unix_sockets is None and legacy_sockets is not None:
        if legacy_sockets:
            normalized["unix_sockets"] = {
                str(path): "allow" for path in legacy_sockets
            }
        normalized.pop("allow_unix_sockets", None)
    payload["network"] = normalized


def _normalize_filesystem_requirements(
    payload: dict[str, Any], base_dir: Path | None
) -> None:
    if base_dir is None:
        return
    permissions = payload.get("permissions")
    filesystem = permissions.get("filesystem") if isinstance(permissions, Mapping) else None
    deny_read = filesystem.get("deny_read") if isinstance(filesystem, Mapping) else None
    if not isinstance(deny_read, list):
        return
    normalized: list[Any] = []
    for value in deny_read:
        if not isinstance(value, str) or not value:
            normalized.append(value)
            continue
        expanded = Path(value).expanduser()
        if expanded.is_absolute():
            normalized.append(str(expanded))
        else:
            normalized.append(str((base_dir / expanded).resolve()))
    filesystem["deny_read"] = normalized


def _validate_network_requirements(value: Any, source: RequirementSource) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ConfigRequirementsError(f"network in {source} must be a table/object")
    boolean_fields = (
        "enabled",
        "allow_upstream_proxy",
        "dangerously_allow_non_loopback_proxy",
        "dangerously_allow_all_unix_sockets",
        "managed_allowed_domains_only",
        "allow_local_binding",
    )
    for field_name in boolean_fields:
        field_value = value.get(field_name)
        if field_value is not None and not isinstance(field_value, bool):
            raise ConfigRequirementsError(
                f"network.{field_name} in {source} must be a boolean"
            )
    for field_name in ("http_port", "socks_port"):
        field_value = value.get(field_name)
        if field_value is not None and (
            isinstance(field_value, bool)
            or not isinstance(field_value, int)
            or not 1 <= field_value <= 65535
        ):
            raise ConfigRequirementsError(
                f"network.{field_name} in {source} must be a valid port"
            )
    for field_name in ("domains", "unix_sockets"):
        entries = value.get(field_name)
        if entries is not None and (
            not isinstance(entries, Mapping)
            or any(
                not isinstance(pattern, str)
                or not pattern
                or decision not in {"allow", "deny"}
                for pattern, decision in entries.items()
            )
        ):
            raise ConfigRequirementsError(
                f"network.{field_name} in {source} must map strings to allow/deny"
            )


def _validate_marketplace_requirements(
    value: Any, source: RequirementSource
) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping):
        raise ConfigRequirementsError(
            f"marketplaces in {source} must be a table/object"
        )
    unknown = set(value) - {"restrict_to_allowed_sources", "allowed_sources"}
    if unknown:
        raise ConfigRequirementsError(
            f"marketplaces in {source} contains unsupported fields: {sorted(unknown)!r}"
        )
    restricted = value.get("restrict_to_allowed_sources")
    if restricted is not None and not isinstance(restricted, bool):
        raise ConfigRequirementsError(
            f"marketplaces.restrict_to_allowed_sources in {source} must be a boolean"
        )
    allowed_sources = value.get("allowed_sources", {})
    if not isinstance(allowed_sources, Mapping):
        raise ConfigRequirementsError(
            f"marketplaces.allowed_sources in {source} must be a table/object"
        )
    for name, rule in allowed_sources.items():
        if not isinstance(name, str) or not isinstance(rule, Mapping):
            raise ConfigRequirementsError(
                f"marketplaces.allowed_sources in {source} must map names to tables"
            )
        unknown_rule = set(rule) - {"source", "url", "ref", "host_pattern", "path"}
        if unknown_rule:
            raise ConfigRequirementsError(
                f"marketplaces.allowed_sources.{name} in {source} has unsupported fields: "
                f"{sorted(unknown_rule)!r}"
            )
        kind = rule.get("source")
        if kind not in {"git", "host_pattern", "local"}:
            raise ConfigRequirementsError(
                f"marketplaces.allowed_sources.{name}.source in {source} must be git, host_pattern, or local"
            )
        required_field = {"git": "url", "host_pattern": "host_pattern", "local": "path"}[str(kind)]
        required_value = rule.get(required_field)
        if not isinstance(required_value, str) or not required_value.strip():
            raise ConfigRequirementsError(
                f"marketplaces.allowed_sources.{name}.{required_field} in {source} must be non-empty"
            )
        if kind == "host_pattern":
            try:
                re.compile(required_value)
            except re.error as exc:
                raise ConfigRequirementsError(
                    f"marketplaces.allowed_sources.{name}.host_pattern in {source} is invalid: {exc}"
                ) from exc
        if kind == "local" and not Path(required_value).expanduser().is_absolute():
            raise ConfigRequirementsError(
                f"marketplaces.allowed_sources.{name}.path in {source} must be absolute"
            )
        if kind == "git" and "ref" in rule:
            ref = rule.get("ref")
            if not isinstance(ref, str) or not ref.strip():
                raise ConfigRequirementsError(
                    f"marketplaces.allowed_sources.{name}.ref in {source} must be a non-empty string"
                )


def _validate_composed_requirements(
    payload: Mapping[str, Any], sources: Mapping[str, RequirementSource]
) -> None:
    for field_name in (
        "allowed_approval_policies",
        "allowed_approvals_reviewers",
        "allowed_sandbox_modes",
    ):
        value = payload.get(field_name)
        if isinstance(value, list) and not value:
            raise ConfigRequirementsError(
                f"{field_name} in {sources.get(field_name)} must not be empty"
            )
    sandbox_modes = payload.get("allowed_sandbox_modes")
    if isinstance(sandbox_modes, list) and "read-only" not in sandbox_modes:
        raise ConfigRequirementsError(
            "allowed_sandbox_modes must include 'read-only' so a safe permission profile remains available"
        )
    profiles = payload.get("allowed_permission_profiles")
    default_permissions = payload.get("default_permissions")
    if default_permissions is not None and not isinstance(profiles, Mapping):
        raise ConfigRequirementsError(
            "default_permissions requires allowed_permission_profiles"
        )
    if isinstance(profiles, Mapping) and default_permissions is not None:
        if profiles.get(default_permissions) is not True:
            raise ConfigRequirementsError(
                f"default_permissions {default_permissions!r} must be enabled by allowed_permission_profiles"
            )


def _deep_merge(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for key, value in incoming.items():
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge(current, value)
        else:
            target[str(key)] = copy.deepcopy(value)


def _merge_requirement_layer(
    target: dict[str, Any], incoming: Mapping[str, Any], existing: Mapping[str, Any]
) -> None:
    incoming_deny = _nested_string_list(incoming, "permissions", "filesystem", "deny_read")
    existing_deny = _nested_string_list(existing, "permissions", "filesystem", "deny_read")
    hook_events = _requirement_event_lists(incoming.get("hooks"), existing.get("hooks"))
    _deep_merge(target, incoming)
    if incoming_deny is not None:
        permissions = target.setdefault("permissions", {})
        if isinstance(permissions, dict):
            filesystem = permissions.setdefault("filesystem", {})
            if isinstance(filesystem, dict):
                filesystem["deny_read"] = list(dict.fromkeys([*incoming_deny, *(existing_deny or [])]))
    if hook_events:
        hooks = target.setdefault("hooks", {})
        if isinstance(hooks, dict):
            for event_name, values in hook_events.items():
                hooks[event_name] = values


def _nested_string_list(value: Mapping[str, Any], *keys: str) -> list[str] | None:
    current: Any = value
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    if not isinstance(current, list) or any(not isinstance(item, str) for item in current):
        return None
    return list(current)


def _requirement_event_lists(incoming: Any, existing: Any) -> dict[str, list[Any]]:
    if not isinstance(incoming, Mapping):
        return {}
    previous = existing if isinstance(existing, Mapping) else {}
    output: dict[str, list[Any]] = {}
    for key, value in incoming.items():
        if key in {"managed_dir", "windows_managed_dir"} or not isinstance(value, list):
            continue
        lower = previous.get(key)
        output[str(key)] = [*copy.deepcopy(value), *(copy.deepcopy(lower) if isinstance(lower, list) else [])]
    return output


def _redact_requirement_value(field_name: str, value: Any) -> Any:
    if field_name in {"guardian_policy_config", "model_catalog_json"}:
        return "<configured>"
    return copy.deepcopy(value)
