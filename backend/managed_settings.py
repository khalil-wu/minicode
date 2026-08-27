from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import plistlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


logger = logging.getLogger(__name__)

_CUSTOMIZATION_SURFACES = frozenset({"agents", "hooks", "mcp", "skills"})
_HOOK_TYPES = frozenset({"command", "prompt", "agent", "http"})


@dataclass(frozen=True)
class ManagedSettingsResult:
    """The single MiniCode policy source selected for this process.

    Platform policy sources use first-source-wins. The file source is a merge
    of managed-settings.json followed by managed-settings.d/*.json in
    alphabetical order.
    """

    settings: Mapping[str, Any]
    source_kind: str = ""
    source_location: str = ""
    validation_errors: tuple[str, ...] = ()
    version: str = ""
    present: bool = False

    def __post_init__(self) -> None:
        copied = copy.deepcopy(dict(self.settings))
        object.__setattr__(self, "settings", copied)
        if not self.present and (copied or self.validation_errors):
            object.__setattr__(self, "present", True)
        if not self.version:
            encoded = json.dumps(
                copied,
                sort_keys=True,
                ensure_ascii=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
            object.__setattr__(self, "version", hashlib.sha256(encoded).hexdigest())

    @property
    def configured(self) -> bool:
        return bool(self.settings)

    @property
    def invalid(self) -> bool:
        return bool(self.validation_errors)


def default_minicode_managed_dir() -> Path:
    if sys.platform == "win32":
        return Path(r"C:\Program Files\MiniCode")
    if sys.platform == "darwin":
        return Path("/Library/Application Support/MiniCode")
    return Path("/etc/minicode")


def load_minicode_managed_settings(
    managed_settings_dir: Path | None = None,
    *,
    remote_settings: Mapping[str, Any] | None = None,
) -> ManagedSettingsResult:
    """Load MiniCode policy settings with platform-managed precedence.

    Remote enterprise settings are not available to the local MiniCode
    process. The remaining upstream order is preserved exactly:
    HKLM/macOS managed preference, managed files, then Windows HKCU.
    """

    accumulated_errors: list[str] = []
    if remote_settings:
        remote_payload = _validated_settings_mapping(
            remote_settings,
            source="remote managed settings",
            errors=accumulated_errors,
        )
        if remote_payload is not None and not accumulated_errors:
            return ManagedSettingsResult(
                remote_payload,
                "remote",
                "remote managed settings",
                tuple(accumulated_errors),
                present=True,
            )
        return ManagedSettingsResult(
            remote_payload or {},
            "remote",
            "remote managed settings",
            tuple(accumulated_errors),
            present=True,
        )

    platform_result = _load_admin_platform_settings()
    accumulated_errors.extend(platform_result.validation_errors)
    if platform_result.present:
        return ManagedSettingsResult(
            platform_result.settings,
            platform_result.source_kind,
            platform_result.source_location,
            tuple(accumulated_errors),
            platform_result.version,
            present=True,
        )

    directory = Path(managed_settings_dir or default_minicode_managed_dir())
    file_result = load_minicode_managed_file_settings(directory)
    accumulated_errors.extend(file_result.validation_errors)
    if file_result.present:
        return ManagedSettingsResult(
            file_result.settings,
            file_result.source_kind,
            file_result.source_location,
            tuple(accumulated_errors),
            file_result.version,
            present=True,
        )

    user_result = _load_windows_registry_settings("HKEY_CURRENT_USER")
    accumulated_errors.extend(user_result.validation_errors)
    if user_result.present:
        return ManagedSettingsResult(
            user_result.settings,
            user_result.source_kind,
            user_result.source_location,
            tuple(accumulated_errors),
            user_result.version,
            present=True,
        )
    return ManagedSettingsResult({}, validation_errors=tuple(accumulated_errors))


def load_minicode_managed_file_settings(directory: Path) -> ManagedSettingsResult:
    """Merge the MiniCode managed base file and alphabetical drop-ins."""

    root = Path(directory)
    paths = [root / "managed-settings.json"]
    drop_in_dir = root / "managed-settings.d"
    try:
        paths.extend(
            sorted(
                (
                    entry
                    for entry in drop_in_dir.iterdir()
                    if (entry.is_file() or entry.is_symlink())
                    and entry.suffix == ".json"
                    and not entry.name.startswith(".")
                ),
                key=lambda entry: entry.name,
            )
        )
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as exc:
        logger.error("Failed to enumerate MiniCode managed settings %s: %s", drop_in_dir, exc)

    merged: dict[str, Any] = {}
    sources: list[str] = []
    errors: list[str] = []
    present = False
    for path in paths:
        if path.exists() or path.is_symlink():
            present = True
            # Preserve the actual managed source even when its payload is
            # empty or malformed.  A policy error must identify the source
            # that blocked lower-precedence settings.
            sources.append(str(path))
        payload = _load_json_settings(path, errors=errors)
        if payload is None or not payload:
            continue
        _merge_settings(merged, payload)
    return ManagedSettingsResult(
        settings=merged,
        source_kind="managed_files" if present else "",
        source_location="; ".join(sources),
        validation_errors=tuple(errors),
        present=present,
    )


def normalize_minicode_policy_requirements(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Project MiniCode managed policy fields onto runtime requirements."""

    result: dict[str, Any] = {}
    fields = (
        "allow_managed_permission_rules_only",
        "allow_managed_mcp_servers_only",
        "allow_managed_hooks_only",
        "disable_all_hooks",
        "allowed_http_hook_urls",
        "http_hook_allowed_env_vars",
        "strict_plugin_only_customization",
        "strict_known_marketplaces",
        "blocked_marketplaces",
        "enabled_plugins",
        "extra_known_marketplaces",
        "plugin_trust_message",
    )
    for field_name in fields:
        if field_name in settings:
            result[field_name] = copy.deepcopy(settings[field_name])
    for field_name in ("hooks", "sandbox", "permissions"):
        if field_name in settings:
            result[field_name] = copy.deepcopy(settings[field_name])
    return result


def _merge_settings(target: dict[str, Any], incoming: Mapping[str, Any]) -> None:
    """Merge one MiniCode managed-settings layer.

    Objects merge recursively. Arrays concatenate and deduplicate while
    preserving first occurrence. Later scalar values replace earlier ones.
    """

    for raw_key, value in incoming.items():
        key = str(raw_key)
        current = target.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            _merge_settings(current, value)
        elif isinstance(current, list) and isinstance(value, list):
            for item in value:
                # Lodash uniq uses identity for objects. Primitive duplicates
                # collapse, while two separately declared object entries stay
                # distinct and retain source order.
                if isinstance(item, (str, int, float, bool, type(None))) and item in current:
                    continue
                current.append(copy.deepcopy(item))
        else:
            target[key] = copy.deepcopy(value)


def _load_admin_platform_settings() -> ManagedSettingsResult:
    if sys.platform == "win32":
        return _load_windows_registry_settings("HKEY_LOCAL_MACHINE")
    if sys.platform != "darwin":
        return ManagedSettingsResult({})

    try:
        username = os.getlogin()
    except OSError:
        username = str(os.environ.get("USER") or os.environ.get("LOGNAME") or "").strip()
    paths: list[Path] = []
    if username:
        paths.append(
            Path("/Library/Managed Preferences")
            / username
            / "com.minicode.plist"
        )
    paths.append(Path("/Library/Managed Preferences/com.minicode.plist"))
    for path in paths:
        result = _load_plist_settings(path)
        if result.present:
            return result
    return ManagedSettingsResult({})


def _load_windows_registry_settings(hive_name: str) -> ManagedSettingsResult:
    if sys.platform != "win32":
        return ManagedSettingsResult({})
    try:
        import winreg
    except ImportError:
        return ManagedSettingsResult({})
    hive = getattr(winreg, hive_name)
    location = f"{hive_name}\\SOFTWARE\\Policies\\MiniCode\\Settings"
    try:
        with winreg.OpenKey(hive, r"SOFTWARE\Policies\MiniCode") as key:
            value, value_type = winreg.QueryValueEx(key, "Settings")
    except FileNotFoundError:
        return ManagedSettingsResult({})
    except OSError as exc:
        logger.error("Failed to read MiniCode policy registry %s: %s", hive_name, exc)
        return ManagedSettingsResult(
            {},
            "hklm" if hive_name == "HKEY_LOCAL_MACHINE" else "hkcu",
            location,
            (f"{location}: failed to read managed policy: {exc}",),
            present=True,
        )
    if value_type not in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} or not isinstance(value, str):
        logger.error("MiniCode policy registry %s Settings must be REG_SZ", hive_name)
        return ManagedSettingsResult(
            {},
            "hklm" if hive_name == "HKEY_LOCAL_MACHINE" else "hkcu",
            location,
            (f"{location}: Settings must be REG_SZ",),
            present=True,
        )
    errors: list[str] = []
    payload = _parse_json_object(value, source=location, errors=errors)
    return ManagedSettingsResult(
        payload or {},
        "hklm" if hive_name == "HKEY_LOCAL_MACHINE" else "hkcu",
        location,
        tuple(errors),
        present=True,
    )


def _load_json_settings(path: Path, *, errors: list[str]) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.error("Failed to read MiniCode settings %s: %s", path, exc)
        errors.append(f"{path}: failed to read settings: {exc}")
        return None
    return _parse_json_object(raw, source=str(path), errors=errors)


def _parse_json_object(
    raw: str,
    *,
    source: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid MiniCode settings JSON %s: %s", source, exc)
        errors.append(f"{source}: invalid JSON: {exc}")
        return None
    if not isinstance(payload, Mapping):
        logger.error("MiniCode settings %s must be an object", source)
        errors.append(f"{source}: settings must be an object")
        return None
    return _validated_settings_mapping(payload, source=source, errors=errors)


def _load_plist_settings(path: Path) -> ManagedSettingsResult:
    try:
        with path.open("rb") as stream:
            payload = plistlib.load(stream)
    except FileNotFoundError:
        return ManagedSettingsResult({})
    except (OSError, plistlib.InvalidFileException) as exc:
        logger.error("Failed to read MiniCode managed preferences %s: %s", path, exc)
        return ManagedSettingsResult(
            {},
            "plist",
            str(path),
            (f"{path}: failed to read managed preferences: {exc}",),
            present=True,
        )
    if not isinstance(payload, Mapping):
        logger.error("MiniCode managed preferences %s must be a dictionary", path)
        return ManagedSettingsResult(
            {},
            "plist",
            str(path),
            (f"{path}: managed preferences must be a dictionary",),
            present=True,
        )
    errors: list[str] = []
    validated = _validated_settings_mapping(payload, source=str(path), errors=errors)
    return ManagedSettingsResult(
        validated or {},
        "plist",
        str(path),
        tuple(errors),
        present=True,
    )


def _validated_settings_mapping(
    payload: Mapping[str, Any],
    *,
    source: str,
    errors: list[str],
) -> dict[str, Any] | None:
    result = copy.deepcopy(dict(payload))
    strict = result.get("strict_plugin_only_customization")
    if isinstance(strict, list):
        result["strict_plugin_only_customization"] = [
            item for item in strict
            if isinstance(item, str) and item in _CUSTOMIZATION_SURFACES
        ]
    elif strict is not None and not isinstance(strict, bool):
        result.pop("strict_plugin_only_customization", None)

    error = _managed_settings_validation_error(result)
    if error:
        logger.error("Invalid MiniCode managed settings %s: %s", source, error)
        errors.append(f"{source}: {error}")
        return None
    return result


def _managed_settings_validation_error(payload: Mapping[str, Any]) -> str:
    canonical_fields = {
        "allow_managed_permission_rules_only",
        "allow_managed_hooks_only",
        "allow_managed_mcp_servers_only",
        "disable_all_hooks",
        "allowed_http_hook_urls",
        "http_hook_allowed_env_vars",
    }
    legacy_field_shapes = {
        "allowmanagedpermissionrulesonly",
        "allowmanagedhooksonly",
        "allowmanagedmcpserversonly",
        "disableallhooks",
        "allowedhttphookurls",
        "httphookallowedenvvars",
    }
    for raw_name in payload:
        field_name = str(raw_name)
        if field_name in canonical_fields:
            continue
        if field_name.replace("_", "").casefold() in legacy_field_shapes:
            return "managed settings must use MiniCode snake_case field names"

    for field_name in (
        "allow_managed_permission_rules_only",
        "allow_managed_hooks_only",
        "disable_all_hooks",
        "allow_managed_mcp_servers_only",
    ):
        value = payload.get(field_name)
        if value is not None and not isinstance(value, bool):
            return f"{field_name} must be a boolean"

    for field_name in ("allowed_http_hook_urls", "http_hook_allowed_env_vars"):
        value = payload.get(field_name)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            return f"{field_name} must be a string array"

    enabled_plugins = payload.get("enabled_plugins")
    if enabled_plugins is not None:
        if not isinstance(enabled_plugins, Mapping):
            return "enabled_plugins must be an object"
        for plugin_id, state in enabled_plugins.items():
            if not isinstance(plugin_id, str) or not plugin_id.strip():
                return "enabled_plugins keys must be non-empty strings"
            try:
                from backend.plugins.identity import parse_plugin_id_strict

                parse_plugin_id_strict(plugin_id)
            except ValueError as exc:
                return f"enabled_plugins.{plugin_id} has invalid plugin id: {exc}"
            if isinstance(state, bool):
                continue
            if not isinstance(state, list) or any(
                not isinstance(item, str) for item in state
            ):
                return (
                    f"enabled_plugins.{plugin_id} must be a boolean or string array"
                )

    for field_name in ("strict_known_marketplaces", "blocked_marketplaces"):
        value = payload.get(field_name)
        if value is None:
            continue
        if not isinstance(value, list):
            return f"{field_name} must be an array"
        for index, source in enumerate(value):
            error = _marketplace_source_validation_error(source)
            if error:
                return f"{field_name}[{index}] {error}"

    extra_marketplaces = payload.get("extra_known_marketplaces")
    if extra_marketplaces is not None and not isinstance(extra_marketplaces, Mapping):
        return "extra_known_marketplaces must be an object"
    trust_message = payload.get("plugin_trust_message")
    if trust_message is not None and not isinstance(trust_message, str):
        return "plugin_trust_message must be a string"

    hooks = payload.get("hooks")
    if hooks is not None:
        error = _hooks_validation_error(hooks)
        if error:
            return error

    sandbox = payload.get("sandbox")
    if sandbox is not None:
        error = _sandbox_validation_error(sandbox)
        if error:
            return error
    permissions = payload.get("permissions")
    if permissions is not None and not isinstance(permissions, Mapping):
        return "permissions must be an object"
    return ""


def _marketplace_source_validation_error(source: Any) -> str:
    if not isinstance(source, Mapping):
        return "must be an object"
    source_kind = source.get("source")
    if source_kind not in {
        "url",
        "github",
        "git",
        "npm",
        "file",
        "directory",
        "hostPattern",
        "pathPattern",
        "settings",
    }:
        return "has an unsupported source type"
    required = {
        "url": "url",
        "github": "repo",
        "git": "url",
        "npm": "package",
        "file": "path",
        "directory": "path",
        "hostPattern": "hostPattern",
        "pathPattern": "pathPattern",
        "settings": "name",
    }[str(source_kind)]
    if not isinstance(source.get(required), str) or not str(source.get(required)).strip():
        return f"requires a non-empty {required} string"
    if source_kind == "settings" and not isinstance(source.get("plugins"), list):
        return "requires a plugins array"
    for optional_string in ("ref", "path"):
        value = source.get(optional_string)
        if value is not None and not isinstance(value, str):
            return f"{optional_string} must be a string"
    return ""


def _sandbox_validation_error(sandbox: Any) -> str:
    if not isinstance(sandbox, Mapping):
        return "sandbox must be an object"
    for field_name in (
        "enabled",
        "failIfUnavailable",
        "allowUnsandboxedCommands",
        "autoAllowCommandsIfSandboxed",
        "enableWeakerNestedSandbox",
        "enableWeakerNetworkIsolation",
    ):
        value = sandbox.get(field_name)
        if value is not None and not isinstance(value, bool):
            return f"sandbox.{field_name} must be a boolean"
    excluded = sandbox.get("excludedCommands")
    if excluded is not None and (
        not isinstance(excluded, list)
        or any(not isinstance(item, str) for item in excluded)
    ):
        return "sandbox.excludedCommands must be a string array"

    network = sandbox.get("network")
    if network is not None:
        if not isinstance(network, Mapping):
            return "sandbox.network must be an object"
        for field_name in (
            "allowManagedDomainsOnly",
            "allowAllUnixSockets",
            "allowLocalBinding",
        ):
            value = network.get(field_name)
            if value is not None and not isinstance(value, bool):
                return f"sandbox.network.{field_name} must be a boolean"
        for field_name in ("allowedDomains", "allowUnixSockets"):
            value = network.get(field_name)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            ):
                return f"sandbox.network.{field_name} must be a string array"
        for field_name in ("httpProxyPort", "socksProxyPort"):
            value = network.get(field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or int(value) != value
                or not 1 <= int(value) <= 65535
            ):
                return f"sandbox.network.{field_name} must be a valid port"

    filesystem = sandbox.get("filesystem")
    if filesystem is not None:
        if not isinstance(filesystem, Mapping):
            return "sandbox.filesystem must be an object"
        managed_only = filesystem.get("allowManagedReadPathsOnly")
        if managed_only is not None and not isinstance(managed_only, bool):
            return "sandbox.filesystem.allowManagedReadPathsOnly must be a boolean"
        for field_name in ("allowWrite", "denyWrite", "denyRead", "allowRead"):
            value = filesystem.get(field_name)
            if value is not None and (
                not isinstance(value, list)
                or any(not isinstance(item, str) for item in value)
            ):
                return f"sandbox.filesystem.{field_name} must be a string array"
    return ""


def _hooks_validation_error(hooks: Any) -> str:
    if not isinstance(hooks, Mapping):
        return "hooks must be an object"
    for event_name, groups in hooks.items():
        if not isinstance(groups, list):
            return f"hooks.{event_name} must be an array"
        for group_index, group in enumerate(groups):
            if not isinstance(group, Mapping):
                return f"hooks.{event_name}[{group_index}] must be an object"
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                return f"hooks.{event_name}[{group_index}].hooks must be an array"
            for hook_index, hook in enumerate(handlers):
                if not isinstance(hook, Mapping):
                    return (
                        f"hooks.{event_name}[{group_index}].hooks[{hook_index}] "
                        "must be an object"
                    )
                hook_type = str(hook.get("type") or "command")
                if hook_type not in _HOOK_TYPES:
                    return (
                        f"hooks.{event_name}[{group_index}].hooks[{hook_index}].type "
                        f"must be one of {sorted(_HOOK_TYPES)}"
                    )
                required_field = {
                    "command": "command",
                    "prompt": "prompt",
                    "agent": "prompt",
                    "http": "url",
                }[hook_type]
                if not isinstance(hook.get(required_field), str) or not str(
                    hook.get(required_field)
                ).strip():
                    return (
                        f"hooks.{event_name}[{group_index}].hooks[{hook_index}]."
                        f"{required_field} must be a non-empty string"
                    )
    return ""
