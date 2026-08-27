from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from backend.config_requirements import (
    ConfigRequirements,
    RequirementsLayerEntry,
    load_config_requirements,
)
from backend.runtime_env import ShellEnvironmentPolicy, ShellEnvironmentPolicyError


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
# MiniCode defaults project-root discovery to ``.git`` only. Other markers
# remain available through the native ``project_root_markers`` config key;
# silently broadening the default changes which project config and instructions
# belong to a turn.
_DEFAULT_PROJECT_ROOT_MARKERS = (".git",)
_PROJECT_LOCAL_CONFIG_DENYLIST = frozenset(
    {
        "openai_base_url",
        "chatgpt_base_url",
        "apps_mcp_product_sku",
        "model_provider",
        "model_providers",
        "notify",
        "profile",
        "profiles",
        "experimental_realtime_webrtc_call_base_url",
        "experimental_realtime_ws_base_url",
        "otel",
        # MiniCode keeps provider endpoints under this table. Project content
        # must not choose where host-owned credentials are sent.
        "llm",
    }
)


class ConfigLayerError(ValueError):
    pass


@dataclass(frozen=True)
class ConfigLayerSource:
    kind: str
    file: str = ""
    profile: str = ""
    domain: str = ""
    key: str = ""
    source_id: str = ""
    name: str = ""
    project_config_folder: str = ""

    @property
    def precedence(self) -> int:
        return {
            "mdm": 0,
            "system": 10,
            "enterprise_managed": 15,
            "user": 21 if self.profile else 20,
            # Managed policy settings remain explicit so consumers can enforce
            # source-sensitive rules without re-reading disk.
            "project": 25,
            "session_flags": 30,
            "legacy_managed_config_file": 40,
            "legacy_managed_config_mdm": 50,
            # Managed policy settings are high-precedence and read-only. They
            # must win over user/project/session values.
            "policy": 60,
        }.get(self.kind, 20)

    def display(self) -> str:
        if self.kind == "mdm":
            return f"MDM ({self.domain}:{self.key})"
        if self.kind == "system":
            return f"system ({self.file})"
        if self.kind == "enterprise_managed":
            label = self.name or "enterprise-managed"
            return f"enterprise-managed ({label}, {self.source_id})"
        if self.kind == "user":
            suffix = f", profile={self.profile}" if self.profile else ""
            return f"user ({self.file}{suffix})"
        if self.kind == "project":
            return f"project ({Path(self.project_config_folder) / 'config.toml'})"
        if self.kind == "session_flags":
            return "session-flags"
        if self.kind == "policy":
            label = self.name or "managed policy"
            return f"{label} ({self.file or self.source_id})"
        if self.kind == "legacy_managed_config_file":
            return f"legacy managed config ({self.file})"
        if self.kind == "legacy_managed_config_mdm":
            return "legacy managed config (MDM)"
        return self.kind

    def to_payload(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "file": self.file or None,
            "profile": self.profile or None,
            "domain": self.domain or None,
            "key": self.key or None,
            "id": self.source_id or None,
            "name": self.name or None,
            "project_config_folder": self.project_config_folder or None,
            "precedence": self.precedence,
            "display": self.display(),
        }


@dataclass(frozen=True)
class ConfigLayerMetadata:
    source: ConfigLayerSource
    version: str

    def to_payload(self) -> dict[str, Any]:
        return {"source": self.source.to_payload(), "version": self.version}


@dataclass(frozen=True)
class ConfigLayer:
    source: ConfigLayerSource
    config: Mapping[str, Any]
    version: str = ""
    disabled_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config, Mapping):
            raise ConfigLayerError(f"{self.source.display()} must contain a table/object")
        copied = copy.deepcopy(dict(self.config))
        _validate_project_document_config(copied, self.source.display())
        shell_environment_policy = copied.get("shell_environment_policy")
        if shell_environment_policy is not None and not self.disabled_reason:
            try:
                ShellEnvironmentPolicy.from_mapping(shell_environment_policy)
            except ShellEnvironmentPolicyError as exc:
                raise ConfigLayerError(
                    f"Invalid shell_environment_policy in {self.source.display()}: {exc}"
                ) from exc
        object.__setattr__(self, "config", copied)
        if not self.version:
            object.__setattr__(self, "version", _version_for_config(copied))

    @property
    def is_disabled(self) -> bool:
        return self.disabled_reason is not None

    def metadata(self) -> ConfigLayerMetadata:
        return ConfigLayerMetadata(self.source, self.version)

    def to_payload(self) -> dict[str, Any]:
        return {
            "source": self.source.to_payload(),
            "version": self.version,
            "disabled_reason": self.disabled_reason,
        }


@dataclass(frozen=True)
class ConfigLayerStack:
    layers: tuple[ConfigLayer, ...]
    requirements: ConfigRequirements = field(default_factory=ConfigRequirements)
    startup_warnings: tuple[str, ...] = ()
    managed_policy_errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        previous = -10_000
        previous_project: Path | None = None
        for layer in self.layers:
            precedence = layer.source.precedence
            if precedence < previous:
                raise ConfigLayerError("Config layers are not in precedence order")
            previous = precedence
            if layer.source.kind != "project":
                continue
            current = Path(layer.source.project_config_folder).resolve()
            if previous_project is not None:
                try:
                    current.relative_to(previous_project.parent)
                except ValueError as exc:
                    raise ConfigLayerError(
                        "Project config layers are not ordered from root to cwd"
                    ) from exc
            previous_project = current

    def get_layers(
        self,
        *,
        highest_precedence_first: bool = False,
        include_disabled: bool = False,
    ) -> tuple[ConfigLayer, ...]:
        layers = tuple(
            layer for layer in self.layers if include_disabled or not layer.is_disabled
        )
        return tuple(reversed(layers)) if highest_precedence_first else layers

    def effective_config(self, *, apply_requirements: bool = True) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        managed_permission_rules_only = (
            self.requirements.allow_managed_permission_rules_only is True
        )
        for layer in self.get_layers():
            incoming = copy.deepcopy(dict(layer.config))
            if managed_permission_rules_only and layer.source.kind != "policy":
                _strip_permission_rule_fields(incoming)
            if layer.source.kind == "policy":
                _deep_merge_managed_policy(merged, incoming)
            else:
                _deep_merge(merged, incoming)
        if apply_requirements:
            merged = self.requirements.apply_exact_to_config(merged)
        return merged

    def effective_user_config(self) -> dict[str, Any] | None:
        merged: dict[str, Any] = {}
        found = False
        for layer in self.get_layers():
            if layer.source.kind != "user":
                continue
            found = True
            _deep_merge(merged, layer.config)
        return merged if found else None

    def effective_non_project_config(self) -> dict[str, Any]:
        """Merge active config while excluding project-local `.minicode` layers.

        Boundary settings must be resolved without allowing a project-local
        file to redirect the discovery root that selected it.
        """

        merged: dict[str, Any] = {}
        for layer in self.get_layers():
            if layer.source.kind == "project":
                continue
            incoming = copy.deepcopy(dict(layer.config))
            if layer.source.kind == "policy":
                _deep_merge_managed_policy(merged, incoming)
            else:
                _deep_merge(merged, incoming)
        return merged

    def project_instruction_config(self) -> dict[str, Any]:
        """Return MiniCode's effective project-instruction configuration.

        `project_root_markers` is resolved without project layers so a nested
        `.minicode/config.toml` cannot redirect its own discovery boundary. The
        byte budget and fallback filenames remain ordinary effective settings,
        matching the final MiniCode configuration projection.
        """

        merged = self.effective_config()
        non_project = self.effective_non_project_config()
        if "project_root_markers" in non_project:
            merged["project_root_markers"] = copy.deepcopy(
                non_project["project_root_markers"]
            )
        else:
            merged.pop("project_root_markers", None)
        return merged

    def policy_config(self) -> dict[str, Any] | None:
        merged: dict[str, Any] = {}
        found = False
        for layer in self.get_layers():
            if layer.source.kind != "policy":
                continue
            found = True
            _deep_merge(merged, layer.config)
        return merged if found else None

    @property
    def fingerprint(self) -> str:
        payload = {
            "layers": [
                {
                    "source": layer.source.to_payload(),
                    "version": layer.version,
                    "disabled": layer.disabled_reason,
                }
                for layer in self.layers
            ],
            "requirements": _json_safe(self.requirements.raw),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def origins(self) -> dict[str, ConfigLayerMetadata]:
        origins: dict[str, ConfigLayerMetadata] = {}
        for layer in self.get_layers():
            _record_origins(layer.config, layer.metadata(), (), origins)
        for field_name, source in self.requirements.sources.items():
            if field_name == "features":
                for feature_name in self.requirements.feature_requirements:
                    origins[f"feature_flags.{feature_name}"] = ConfigLayerMetadata(
                        ConfigLayerSource(
                            "system",
                            file=source.location,
                            name=source.name,
                            source_id=source.source_id,
                        ),
                        "managed-requirement",
                    )
        return origins

    def to_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "layers": [layer.to_payload() for layer in self.get_layers(include_disabled=True)],
            "origins": {
                path: metadata.to_payload() for path, metadata in sorted(self.origins().items())
            },
            "requirements": self.requirements.to_payload(),
            "startup_warnings": list(self.startup_warnings),
            "managed_policy_errors": list(self.managed_policy_errors),
        }


def default_system_config_path() -> Path:
    configured = str(os.environ.get("MINICODE_SYSTEM_CONFIG_FILE") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        program_data = Path(os.environ.get("ProgramData") or r"C:\ProgramData")
        return program_data / "MiniCode" / "config.toml"
    return Path("/etc/minicode/config.toml")


def load_config_layers_state(
    *,
    state_root: Path,
    user_config_file: Path,
    cwd: Path | None = None,
    profile: str | None = None,
    session_flags: Mapping[str, Any] | None = None,
    system_config_path: Path | None = None,
    requirements_path: Path | None = None,
    enterprise_config_layers: Iterable[ConfigLayer] = (),
    policy_config_layers: Iterable[ConfigLayer] = (),
    enterprise_requirements_layers: Iterable[RequirementsLayerEntry] = (),
    mdm_requirements_layers: Iterable[RequirementsLayerEntry] = (),
    trust_resolver: Callable[[Path], bool] | None = None,
    startup_warnings: Iterable[str] = (),
    managed_policy_errors: Iterable[str] = (),
) -> ConfigLayerStack:
    root = Path(state_root).expanduser().resolve()
    user_file = Path(user_config_file).expanduser().resolve()
    selected_profile = str(profile or os.environ.get("MINICODE_PROFILE") or "").strip()
    if selected_profile and _PROFILE_NAME_RE.fullmatch(selected_profile) is None:
        raise ConfigLayerError(f"Invalid config profile name: {selected_profile!r}")

    layers: list[ConfigLayer] = []
    warnings: list[str] = [str(item) for item in startup_warnings if str(item)]
    system_path = Path(system_config_path or default_system_config_path()).expanduser().resolve()
    layers.append(
        ConfigLayer(
            ConfigLayerSource("system", file=str(system_path)),
            _load_toml_object(system_path, required=False),
        )
    )
    for layer in enterprise_config_layers:
        if layer.source.kind != "enterprise_managed":
            raise ConfigLayerError("Enterprise config layers must use enterprise_managed sources")
        layers.append(layer)

    user_config = _load_json_object(user_file, required=False)
    layers.append(ConfigLayer(ConfigLayerSource("user", file=str(user_file)), user_config))

    if selected_profile:
        legacy_profile = user_config.get("profile") == selected_profile
        legacy_profiles = user_config.get("profiles")
        legacy_table = isinstance(legacy_profiles, Mapping) and selected_profile in legacy_profiles
        if legacy_profile or legacy_table:
            raise ConfigLayerError(
                f"Profile {selected_profile!r} cannot use both legacy settings.json profile data "
                "and a profile-v2 config file"
            )
        profile_path = (root / f"{selected_profile}.config.toml").resolve()
        layers.append(
            ConfigLayer(
                ConfigLayerSource(
                    "user", file=str(profile_path), profile=selected_profile
                ),
                _load_toml_object(profile_path, required=False),
            )
        )

    effective_before_project: dict[str, Any] = {}
    for layer in layers:
        _deep_merge(effective_before_project, layer.config)

    if cwd is not None:
        resolved_cwd = Path(cwd).expanduser().resolve()
        markers = _project_root_markers(effective_before_project)
        project_root = _find_project_root(resolved_cwd, markers)
        resolver = trust_resolver or _default_trust_resolver
        for directory in _directories_between(project_root, resolved_cwd):
            project_config_dir = directory / ".minicode"
            if not project_config_dir.is_dir() or project_config_dir.resolve() == root:
                continue
            config_path = project_config_dir / "config.toml"
            trusted = bool(resolver(directory))
            disabled_reason = None
            if not trusted:
                disabled_reason = (
                    f"{directory} is not trusted. Project-local config, hooks, and execution "
                    "policies remain disabled until the workspace is explicitly trusted."
                )
            try:
                project_config = _load_toml_object(config_path, required=False)
            except ConfigLayerError:
                if trusted:
                    raise
                project_config = {}
            ignored = _sanitize_project_config(project_config)
            if trusted and ignored:
                warnings.append(
                    f"Ignored unsupported project-local config keys in {config_path}: "
                    f"{', '.join(ignored)}"
                )
            layers.append(
                ConfigLayer(
                    ConfigLayerSource(
                        "project",
                        project_config_folder=str(project_config_dir.resolve()),
                    ),
                    project_config,
                    disabled_reason=disabled_reason,
                )
            )

    if session_flags:
        layers.append(
            ConfigLayer(ConfigLayerSource("session_flags"), copy.deepcopy(dict(session_flags)))
        )

    for layer in policy_config_layers:
        if layer.source.kind != "policy":
            raise ConfigLayerError("Policy config layers must use policy sources")
        layers.append(layer)

    layers.sort(key=lambda layer: layer.source.precedence)
    requirements = load_config_requirements(
        system_path=requirements_path,
        enterprise_layers=enterprise_requirements_layers,
        mdm_layers=mdm_requirements_layers,
    )
    return ConfigLayerStack(
        tuple(layers),
        requirements,
        tuple(warnings),
        tuple(str(item) for item in managed_policy_errors if str(item)),
    )


def _load_toml_object(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if required:
            raise ConfigLayerError(f"Config file not found: {path}")
        return {}
    except OSError as exc:
        raise ConfigLayerError(f"Failed to read config {path}: {exc}") from exc
    try:
        parsed = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigLayerError(f"Invalid config TOML {path}: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigLayerError(f"Config {path} must contain a TOML table")
    return copy.deepcopy(dict(parsed))


def _load_json_object(path: Path, *, required: bool) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise ConfigLayerError(f"Config file not found: {path}")
        return {}
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigLayerError(f"Failed to read config {path}: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigLayerError(f"Invalid config JSON {path}: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise ConfigLayerError(f"Config {path} must contain a JSON object")
    return copy.deepcopy(dict(parsed))


def _project_root_markers(config: Mapping[str, Any]) -> tuple[str, ...]:
    raw = config.get("project_root_markers")
    if raw is None:
        return _DEFAULT_PROJECT_ROOT_MARKERS
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        raise ConfigLayerError("project_root_markers must be an array of strings")
    return tuple(value for value in raw if value)


def _validate_project_document_config(
    config: Mapping[str, Any],
    source: str,
) -> None:
    markers = config.get("project_root_markers")
    if markers is not None and (
        not isinstance(markers, list)
        or any(not isinstance(value, str) for value in markers)
    ):
        raise ConfigLayerError(
            f"project_root_markers in {source} must be an array of strings"
        )
    fallback_names = config.get("project_doc_fallback_filenames")
    if fallback_names is not None and (
        not isinstance(fallback_names, list)
        or any(not isinstance(value, str) for value in fallback_names)
    ):
        raise ConfigLayerError(
            f"project_doc_fallback_filenames in {source} must be an array of strings"
        )
    max_bytes = config.get("project_doc_max_bytes")
    if max_bytes is not None and (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes < 0
    ):
        raise ConfigLayerError(
            f"project_doc_max_bytes in {source} must be a non-negative integer"
        )


def _find_project_root(cwd: Path, markers: tuple[str, ...]) -> Path:
    if not markers:
        return cwd
    for ancestor in (cwd, *cwd.parents):
        if any((ancestor / marker).exists() for marker in markers):
            return ancestor
    return cwd


def _directories_between(root: Path, cwd: Path) -> tuple[Path, ...]:
    if root == cwd:
        return (root,)
    try:
        relative = cwd.relative_to(root)
    except ValueError:
        return (cwd,)
    directories = [root]
    current = root
    for part in relative.parts:
        current = current / part
        directories.append(current)
    return tuple(directories)


def _default_trust_resolver(path: Path) -> bool:
    from backend.workspace.trust import is_workspace_trusted

    return is_workspace_trusted(path)


def _sanitize_project_config(config: dict[str, Any]) -> list[str]:
    ignored: list[str] = []
    for key in sorted(_PROJECT_LOCAL_CONFIG_DENYLIST):
        if key in config:
            config.pop(key, None)
            ignored.append(key)
    features = config.get("features")
    if isinstance(features, dict) and "respect_system_proxy" in features:
        features.pop("respect_system_proxy", None)
        ignored.append("features.respect_system_proxy")
    return ignored


def _deep_merge(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    for raw_key, value in incoming.items():
        key = str(raw_key)
        current = target.get(key)
        if key == "shell_environment_policy" and isinstance(value, Mapping):
            target[key] = _merge_shell_environment_policy(current, value)
        elif isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge(current, value)
        else:
            target[key] = copy.deepcopy(value)


def _deep_merge_managed_policy(
    target: dict[str, Any],
    incoming: Mapping[str, Any],
) -> None:
    """Merge a managed policy source using MiniCode's policy rules."""

    for raw_key, value in incoming.items():
        key = str(raw_key)
        current = target.get(key)
        if key == "shell_environment_policy" and isinstance(value, Mapping):
            target[key] = _merge_shell_environment_policy(current, value)
        elif isinstance(current, dict) and isinstance(value, Mapping):
            _deep_merge_managed_policy(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            for item in value:
                if isinstance(item, (str, int, float, bool, type(None))) and item in current:
                    continue
                current.append(copy.deepcopy(item))
        else:
            target[key] = copy.deepcopy(value)


def _merge_shell_environment_policy(
    current: Any,
    incoming: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge the two mutually exclusive filter representations.

    A higher-precedence ``filters`` table replaces lower legacy arrays, while
    either higher legacy array replaces a lower ``filters`` table. Canonical
    Filter keys merge case-insensitively across layers.
    """

    merged = copy.deepcopy(dict(current)) if isinstance(current, Mapping) else {}
    if "filters" in incoming:
        merged.pop("exclude", None)
        merged.pop("include_only", None)
    elif "exclude" in incoming or "include_only" in incoming:
        merged.pop("filters", None)

    for raw_key, value in incoming.items():
        key = str(raw_key)
        if key == "filters" and isinstance(value, Mapping):
            existing = merged.get("filters")
            filters = copy.deepcopy(dict(existing)) if isinstance(existing, Mapping) else {}
            for raw_pattern, action in value.items():
                pattern = str(raw_pattern)
                folded = pattern.casefold()
                for existing_pattern in tuple(filters):
                    if str(existing_pattern).casefold() == folded:
                        filters.pop(existing_pattern, None)
                filters[pattern] = copy.deepcopy(action)
            merged["filters"] = filters
            continue
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


_PERMISSION_RULE_FIELDS = frozenset(
    {
        # Public permission rule behaviors.
        "allow",
        "ask",
        "deny",
        # MiniCode's compatibility projections of the same rule behaviors.
        "auto_allow",
        "require_confirm",
        "require_diff_review",
        "always_deny",
        "content_allow_rules",
        "content_ask_rules",
        "content_deny_rules",
    }
)


def _strip_permission_rule_fields(config: dict[str, Any]) -> None:
    permissions = config.get("permissions")
    if not isinstance(permissions, Mapping):
        return
    filtered = {
        str(key): copy.deepcopy(value)
        for key, value in permissions.items()
        if str(key) not in _PERMISSION_RULE_FIELDS
    }
    if filtered:
        config["permissions"] = filtered
    else:
        config.pop("permissions", None)


def _record_origins(
    value: Mapping[str, Any],
    metadata: ConfigLayerMetadata,
    prefix: tuple[str, ...],
    output: dict[str, ConfigLayerMetadata],
) -> None:
    for raw_key, child in value.items():
        path = (*prefix, str(raw_key))
        output[".".join(path)] = metadata
        if isinstance(child, Mapping):
            _record_origins(child, metadata, path, output)


def _version_for_config(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _json_safe(config),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    isoformat = getattr(value, "isoformat", None)
    return isoformat() if callable(isoformat) else str(value)
