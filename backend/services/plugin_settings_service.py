from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.atomic_io import canonical_file_path_key, file_mutation_locks
from backend.config import (
    SETTINGS_FILE,
    _SETTINGS_WRITE_LOCK,
    _load_settings_json,
    _write_settings_json as _config_write_settings_json,
)
from backend.feature_flags import feature_enabled
from backend.hooks.runtime import raise_if_config_change_blocked
from backend.plugins.dependencies import find_reverse_dependents, verify_and_demote
from backend.plugins.identity import (
    has_explicit_marketplace,
    normalize_plugin_id,
    parse_plugin_id,
    parse_plugin_id_strict,
    plugin_id as canonical_plugin_id,
    version_satisfies,
)
from backend.plugins.layout import plugin_install_root
from backend.plugins.manifest import (
    _count_plugin_skills,
    _declared_component_payload,
    _declared_plugin_component_path,
    _filesystem_path_key,
    _iter_hook_maps,
    _looks_like_versioned_store,
    _manifest_component_counts,
    _manifest_mcp_server_names,
    _manifest_source_descriptor,
    _merge_manifest_metadata,
    _plugin_marketplace_for_manifest,
    _prefer_active_store_entries,
    _primary_plugin_manifest,
    _read_plugin_manifest,
    _resolve_manifest_asset,
    _validate_versioned_store_path,
    plugin_directory_for_manifest,
    plugin_name_from_directory,
    plugin_name_from_manifest,
)
from backend.plugins.package import (
    _extract_plugin_package,
    _is_plugin_directory,
    _is_relative_to,
    _looks_like_remote_plugin_source,
    _normalized_extracted_plugin_dir,
    _plugin_name_from_package,
    _plugin_symlink_paths,
    _remove_within_root,
    _safe_plugin_data_segment,
    _safe_plugin_folder_name,
    _same_path,
    validate_plugin_directory,
)
from backend.plugins.policy import (
    ManagedPluginPolicy,
    PluginSettingsError,
    _plugin_policy_from_stack,
    normalize_plugin_name,
)

ConfigChangeHook = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


def _write_settings_json(data: dict[str, Any]) -> None:
    """Write the user settings layer through the shared atomic transaction.

    Keep this narrow compatibility seam in the plugin service: embedders and
    older tests inject the settings reader/writer here to isolate plugin
    lifecycle operations. The default implementation still delegates to the
    config layer's durable writer, so production callers retain its lock,
    atomic replace, and permission behavior.
    """

    _config_write_settings_json(data)


def _update_settings_json(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Apply one serialized settings update while preserving test injection."""

    with _SETTINGS_WRITE_LOCK:
        with file_mutation_locks([SETTINGS_FILE]):
            settings_data = _load_settings_json()
            mutator(settings_data)
            _write_settings_json(settings_data)
            return settings_data




def plugin_id_for_name(name: str, marketplace: str = "local") -> str:
    return canonical_plugin_id(name, marketplace)


def _canonical_configured_plugin_id(value: Any) -> str:
    """Normalize a MiniCode config key to ``<name>@<marketplace>``."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = parse_plugin_id_strict(raw)
    except ValueError:
        return ""
    return canonical_plugin_id(parsed.id)


def _coerce_plugin_enablement_state(value: Any) -> bool | tuple[str, ...] | None:
    """Return the only state shapes accepted by MiniCode plugin settings."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (list, tuple)):
        constraints = tuple(
            str(item).strip()
            for item in value
            if isinstance(item, str) and str(item).strip()
        )
        # An array containing non-string values is malformed.  Failing closed
        # is safer than silently treating it as an unconstrained enablement.
        if len(constraints) != len(value):
            return None
        return constraints
    return None


def _collect_configured_plugin_states(
    effective_config: Mapping[str, Any] | None,
    settings_data: Mapping[str, Any] | None,
) -> tuple[dict[str, bool | tuple[str, ...]], bool, dict[str, str]]:
    """Project MiniCode user configuration onto one canonical map.

    The map is deliberately the only user enablement input consumed by the
    inventory.  A plugin that merely exists under a scanned root is *not*
    enabled.

    Returns ``(states, has_explicit_config, invalid_entries)``.  The latter is
    kept separate so a malformed optional entry can be surfaced on the
    matching inventory item without making unrelated plugins executable.
    """

    states: dict[str, bool | tuple[str, ...]] = {}
    invalid: dict[str, str] = {}
    explicit = False

    def add_state(raw_id: Any, raw_state: Any, *, overwrite: bool = True) -> None:
        nonlocal explicit
        canonical = _canonical_configured_plugin_id(raw_id)
        key_text = str(raw_id or "").strip()
        if not canonical:
            if key_text:
                invalid[key_text] = "invalid plugin id"
            return
        state = _coerce_plugin_enablement_state(raw_state)
        if state is None:
            invalid[canonical] = "enabled state must be a boolean or string array"
            # An explicitly malformed entry is still an explicit deny.  This
            # prevents a typo from falling back to physical auto-enable.
            if overwrite or canonical not in states:
                states[canonical] = False
            explicit = True
            return
        if overwrite or canonical not in states:
            states[canonical] = state
        explicit = True

    def add_plugin_entries(raw: Any) -> None:
        if not isinstance(raw, Mapping):
            return
        nonlocal explicit
        saw_plugin_table = False
        for raw_id, raw_entry in raw.items():
            if not isinstance(raw_entry, Mapping) or "enabled" not in raw_entry:
                continue
            saw_plugin_table = True
            add_state(raw_id, raw_entry.get("enabled"))
        if saw_plugin_table:
            explicit = True

    effective = effective_config if isinstance(effective_config, Mapping) else {}
    add_plugin_entries(effective.get("plugins"))

    # Explicitly injected settings snapshots are used by isolated callers and
    # tests. The already-composed config stack remains authoritative.
    if isinstance(settings_data, Mapping):
        before = dict(states)
        add_plugin_entries(settings_data.get("plugins"))
        for key, value in before.items():
            states[key] = value

    return states, explicit, invalid


def _configured_plugin_state(
    plugin_id: str,
    plugin_name: str,
    version: str,
    states: Mapping[str, bool | tuple[str, ...]],
) -> tuple[bool | None, tuple[str, ...]]:
    """Resolve one inventory item against the canonical user selection map."""

    target_id = normalize_plugin_id(plugin_id)
    target_name = normalize_plugin_name(plugin_name)
    candidates: list[tuple[str, bool | tuple[str, ...]]] = []
    for raw_id, state in states.items():
        if normalize_plugin_id(raw_id) == target_id:
            candidates.append((raw_id, state))
            continue
        parsed = parse_plugin_id(raw_id)
        if normalize_plugin_name(parsed.name) == target_name and not has_explicit_marketplace(plugin_name):
            candidates.append((raw_id, state))
    if not candidates:
        return None, ()
    # A bare-name match is only safe when it resolves to one canonical id.
    identities = {normalize_plugin_id(raw_id) for raw_id, _state in candidates}
    if len(identities) > 1:
        return None, ()
    state = candidates[-1][1]
    if isinstance(state, bool):
        return state, ()
    constraints = tuple(state)
    return version_satisfies(version, constraints), constraints


def _persist_user_plugin_enablement(
    settings_data: dict[str, Any],
    plugin_id: str,
    enabled: bool,
) -> None:
    """Persist canonical MiniCode plugin state in the user layer."""

    canonical = _canonical_configured_plugin_id(plugin_id)
    if not canonical:
        raise PluginSettingsError(f"Invalid plugin identity: {plugin_id}", status_code=400)
    settings_data.pop("enabledPlugins", None)
    settings_data.pop("enabled_plugins", None)
    raw = settings_data.get("plugins")
    plugins = dict(raw) if isinstance(raw, Mapping) else {}
    plugins.pop("disabled", None)
    existing: dict[str, Any] = {}
    for raw_id in list(plugins):
        if _canonical_configured_plugin_id(raw_id).casefold() != canonical.casefold():
            continue
        raw_entry = plugins.pop(raw_id)
        if isinstance(raw_entry, Mapping):
            existing.update(dict(raw_entry))
    existing["enabled"] = bool(enabled)
    plugins[canonical] = existing
    settings_data["plugins"] = plugins


def _clear_user_plugin_enablement(settings_data: dict[str, Any], plugin_id: str) -> None:
    """Remove a user selection when a plugin is uninstalled."""

    canonical = _canonical_configured_plugin_id(plugin_id)
    settings_data.pop("enabledPlugins", None)
    settings_data.pop("enabled_plugins", None)
    raw = settings_data.get("plugins")
    if isinstance(raw, Mapping):
        plugins = dict(raw)
        plugins.pop("disabled", None)
        for raw_id in list(plugins):
            if _canonical_configured_plugin_id(raw_id).casefold() == canonical.casefold():
                plugins.pop(raw_id, None)
        if plugins:
            settings_data["plugins"] = plugins
        else:
            settings_data.pop("plugins", None)


def get_plugin_settings(
    plugin_roots: Iterable[Path] | None = None,
    *,
    config_stack: Any | None = None,
    policy: ManagedPluginPolicy | None = None,
) -> dict[str, Any]:
    from backend.commands.plugins import (
        _iter_plugin_manifests,
        default_plugin_roots,
    )

    # Resolve one config-layer snapshot up front.  Every enablement decision
    # below is derived from this object plus the explicitly injected writable
    # settings data; no consumer is allowed to re-read a second settings file
    # after inventory discovery.
    resolved_stack = config_stack
    if resolved_stack is None:
        try:
            from backend.config import load_config_layer_stack

            resolved_stack = load_config_layer_stack()
        except Exception:
            # Inventory/listing remains useful in a degraded installation, but
            # the fail-closed default below still prevents physical auto-load.
            resolved_stack = None
    effective_policy = policy or _plugin_policy_from_stack(resolved_stack)
    roots = list(plugin_roots) if plugin_roots is not None else list(default_plugin_roots())
    settings_data = _load_settings_json()
    effective_config: Mapping[str, Any] = {}
    if resolved_stack is not None:
        try:
            candidate_config = resolved_stack.effective_config()
            if isinstance(candidate_config, Mapping):
                effective_config = candidate_config
        except Exception:
            effective_config = {}
    configured_states, configured_explicit, invalid_config = _collect_configured_plugin_states(
        effective_config,
        settings_data,
    )
    inventory: dict[str, dict[str, Any]] = {}

    for manifest_path in _iter_plugin_manifests(roots):
        plugin_dir = plugin_directory_for_manifest(manifest_path)
        raw_manifest = _read_plugin_manifest(manifest_path)
        name = plugin_name_from_manifest(manifest_path) or plugin_dir.name
        store_validation_error = _validate_versioned_store_path(plugin_dir, roots)
        marketplace = _plugin_marketplace_for_manifest(
            manifest_path,
            raw_manifest,
            roots,
        )
        plugin_id = plugin_id_for_name(name, marketplace)
        identity_valid = bool(plugin_id)
        # A path is provenance, not identity.  Keep separate installations of
        # the same name from different marketplaces visible and deterministic.
        path_key = str(plugin_dir.resolve()) if plugin_dir.exists() else str(plugin_dir.absolute())
        key = f"{plugin_id.casefold()}::{_filesystem_path_key(path_key)}"
        entry = inventory.setdefault(
            key,
            {
                "name": name,
                "id": plugin_id,
                "identity_valid": identity_valid,
                "marketplace": marketplace,
                "source_kind": "directory",
                "source_descriptor": {"source": "directory", "path": path_key},
                "provenance": {
                    "manifest_path": str(manifest_path),
                    "root": str(plugin_dir),
                    "store": "versioned" if _looks_like_versioned_store(plugin_dir) else "legacy",
                },
                "displayName": "",
                "description": "",
                "shortDescription": "",
                "longDescription": "",
                "developerName": "",
                "category": "",
                "capabilities": [],
                "version": "",
                "installed_version": "",
                "active_version": "",
                "dependencies": [],
                "constraints": (),
                "load_errors": [],
                "trust": {"allowed": True, "reason": ""},
                "websiteUrl": "",
                "iconUrl": "",
                "iconVariant": "",
                "brandColor": "",
                "defaultPrompt": [],
                "path": str(plugin_dir),
                "manifest_path": str(manifest_path),
                "manifest_paths": [],
                "skill_count": 0,
                "mcp_server_count": 0,
                "mcp_server_names": [],
                "app_count": 0,
                "hook_count": 0,
                "extension_count": 0,
                "runtime_support": {
                    "skills": True,
                    "mcp_servers": True,
                    "apps": False,
                    "hooks": True,
                    "extensions": True,
                },
                # A materialized directory is not an activation grant.  The
                # final state is filled from managed/user config below.
                "enabled": False,
                "enablement_source": "unconfigured",
            },
        )
        if store_validation_error:
            entry.setdefault("load_errors", []).append(store_validation_error)
            entry["store_valid"] = False
        else:
            entry.setdefault("store_valid", True)
        entry["manifest_paths"].append(str(manifest_path))
        _merge_manifest_metadata(entry, manifest_path)
        if isinstance(raw_manifest, Mapping):
            dependencies = raw_manifest.get("dependencies")
            if isinstance(dependencies, str):
                dependencies = [dependencies]
            if isinstance(dependencies, list):
                entry["dependencies"] = sorted({
                    str(item).strip() for item in [*entry.get("dependencies", []), *dependencies]
                    if isinstance(item, str) and item.strip()
                }, key=str.casefold)
            entry["source_descriptor"] = _manifest_source_descriptor(
                plugin_dir, marketplace, raw_manifest,
            )
        counts = _manifest_component_counts(manifest_path)
        entry["mcp_server_count"] += counts["mcp_server_count"]
        entry["mcp_server_names"] = sorted({
            *entry.get("mcp_server_names", []),
            *_manifest_mcp_server_names(manifest_path),
        }, key=str.casefold)
        entry["app_count"] += counts["app_count"]
        entry["hook_count"] += counts["hook_count"]
        entry["extension_count"] += counts["extension_count"]

    inventory = _prefer_active_store_entries(inventory)
    for entry in inventory.values():
        plugin_dir = Path(str(entry["path"]))
        entry["skill_count"] = _count_plugin_skills(plugin_dir)
        entry["installed_version"] = str(entry.get("version") or "")
        if str((entry.get("provenance") or {}).get("store") or "") == "versioned":
            entry["active_version"] = plugin_dir.name
        else:
            entry["active_version"] = entry["installed_version"]
        plugin_id = str(entry.get("id") or plugin_id_for_name(
            str(entry.get("name") or ""), str(entry.get("marketplace") or "local")
        ))
        managed_state = effective_policy.managed_state(
            plugin_id,
            str(entry.get("version") or ""),
        )
        configured_state: bool | None = None
        configured_constraints: tuple[str, ...] = ()
        if managed_state is not None:
            entry["enabled"] = managed_state
            entry["enablement_source"] = "managed"
        else:
            configured_state, configured_constraints = _configured_plugin_state(
                plugin_id,
                str(entry.get("name") or ""),
                str(entry.get("version") or ""),
                configured_states,
            )
            if configured_state is None:
                # Explicit configuration is required even when the plugin is
                # installed locally. This boundary prevents a cache scan from
                # launching plugin
                # Skills/MCP/Hooks by accident.
                entry["enabled"] = False
                entry["enablement_source"] = "unconfigured"
            else:
                entry["enabled"] = configured_state
                entry["enablement_source"] = "user"
        if not bool(entry.get("identity_valid", True)):
            entry["enabled"] = False
            entry["enablement_source"] = "invalid-identity"
            entry.setdefault("load_errors", []).append("invalid plugin identity")
        if entry.get("store_valid") is False:
            entry["enabled"] = False
        managed_constraints = effective_policy.managed_constraint(plugin_id) or ()
        constraints = tuple(dict.fromkeys((*configured_constraints, *managed_constraints)))
        entry["constraints"] = constraints
        if managed_constraints and not version_satisfies(
            str(entry.get("version") or ""),
            managed_constraints,
        ):
            entry["enabled"] = False
            entry["load_errors"].append(
                f"installed version {entry.get('version') or '<unknown>'} does not satisfy {', '.join(managed_constraints)}"
            )
        try:
            effective_policy.assert_source_allowed(entry.get("source_descriptor") or {})
        except PluginSettingsError as exc:
            entry["enabled"] = False
            entry["trust"] = {"allowed": False, "reason": str(exc)}
            entry["load_errors"].append(str(exc))
        entry["disabled"] = not bool(entry["enabled"])
        entry["managed"] = (
            _is_relative_to(plugin_dir, plugin_install_root())
            or _is_relative_to(plugin_dir, plugin_install_root().parent / "store")
        )
        entry["policy_managed"] = managed_state is not None
        entry["managed_enabled"] = managed_state
        entry["configured"] = configured_state is not None if managed_state is None else True
        if plugin_id.casefold() in invalid_config:
            entry.setdefault("load_errors", []).append(
                f"invalid configured plugin state: {invalid_config[plugin_id.casefold()]}"
            )

    # Load-time fixed-point dependency safety net.  Do not write settings;
    # consumers receive the same demoted snapshot for this turn.
    demoted, dependency_errors = verify_and_demote(list(inventory.values()))
    if demoted:
        for entry in inventory.values():
            if str(entry.get("id") or "").casefold() in {value.casefold() for value in demoted}:
                entry["enabled"] = False
                entry["disabled"] = True
                entry.setdefault("load_errors", []).append("dependency-unsatisfied")
    dependency_error_payload = [
        {
            "reason": error.reason,
            "dependency": error.dependency,
            "required_by": error.required_by,
            "chain": list(error.chain),
            "message": error.message,
        }
        for error in dependency_errors
    ]

    plugins = sorted(
        inventory.values(),
        key=lambda item: (not bool(item.get("enabled")), str(item.get("name", "")).casefold(), str(item.get("path", "")).casefold()),
    )
    fingerprint = _plugin_inventory_fingerprint(
        plugins,
        effective_policy,
        configured_states=configured_states,
    )
    return {
        "plugins": plugins,
        "disabled": sorted(
            {
                str(entry.get("id") or entry.get("name") or "")
                for entry in plugins
                if not bool(entry.get("enabled"))
                and str(entry.get("id") or entry.get("name") or "")
            },
            key=str.casefold,
        ),
        "configured_plugins": {
            str(key): value for key, value in sorted(configured_states.items(), key=lambda item: item[0].casefold())
        },
        "configured_explicit": configured_explicit,
        "feature_enabled": feature_enabled("plugin_lifecycle_api", True),
        "policy": {
            "configured": bool(
                configured_explicit
                or effective_policy.enabled_plugins
                or effective_policy.strict_known_marketplaces is not None
                or effective_policy.blocked_marketplaces
                or effective_policy.marketplace_requirements
            ),
            "source": effective_policy.source,
            "fingerprint": effective_policy.fingerprint,
            "trust_message": effective_policy.trust_message,
        },
        "dependency_errors": dependency_error_payload,
        "snapshot_version": 2,
        "fingerprint": fingerprint,
    }


def _plugin_inventory_fingerprint(
    plugins: Iterable[Mapping[str, Any]],
    policy: ManagedPluginPolicy,
    *,
    configured_states: Mapping[str, Any] | None = None,
) -> str:
    material: list[dict[str, Any]] = []
    for entry in plugins:
        manifests: list[dict[str, Any]] = []
        for raw_path in entry.get("manifest_paths", ()) if isinstance(entry, Mapping) else ():
            path = Path(str(raw_path))
            try:
                stat = path.stat()
                manifests.append({"path": str(path), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
            except OSError:
                manifests.append({"path": str(path), "missing": True})
        material.append({
            "id": entry.get("id", ""),
            "path": entry.get("path", ""),
            "version": entry.get("version", ""),
            "enabled": bool(entry.get("enabled")),
            "constraints": list(entry.get("constraints", ()) or ()),
            "dependencies": list(entry.get("dependencies", ()) or ()),
            "manifests": manifests,
        })
    payload = {
        "policy": policy.fingerprint,
        "configured_plugins": {
            str(key): value
            for key, value in sorted(
                (configured_states or {}).items(),
                key=lambda item: str(item[0]).casefold(),
            )
        },
        "plugins": sorted(material, key=lambda item: (str(item["id"]).casefold(), str(item["path"]).casefold())),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def get_plugin_snapshot(
    plugin_roots: Iterable[Path] | None = None,
    *,
    config_stack: Any | None = None,
    policy: ManagedPluginPolicy | None = None,
) -> dict[str, Any]:
    """Canonical effective inventory consumed by runtime subsystems.

    Kept as a named API so callers cannot accidentally fall back to the old
    ``disabled`` list or perform a second plugin-root scan.  The payload is
    structurally the same as ``get_plugin_settings`` for frontend and API
    compatibility.
    """

    if plugin_roots is None and config_stack is None and policy is None:
        # A number of integrations monkeypatch the legacy zero-argument
        # function.  Preserve that injection seam while keeping the named
        # snapshot API canonical for real callers.
        return get_plugin_settings()
    return get_plugin_settings(plugin_roots, config_stack=config_stack, policy=policy)


async def update_plugin_enabled(
    name: str,
    enabled: bool,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    policy = _plugin_policy_from_stack()
    clean_name = str(name or "").strip()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if not clean_name:
        raise PluginSettingsError("Plugin name is required", status_code=400)
    if not isinstance(enabled, bool):
        raise PluginSettingsError("Plugin enabled must be a boolean", status_code=400)
    known_plugins = get_plugin_settings(policy=policy)
    target = _resolve_plugin_inventory_item(
        clean_name,
        known_plugins.get("plugins", []),
    )
    plugin_id = str(target.get("id") or plugin_id_for_name(
        str(target.get("name") or ""), str(target.get("marketplace") or "local")
    ))
    policy.assert_plugin_mutable(plugin_id)

    hook_result = await config_change_hook(source="plugins", file_path=str(settings_file))
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="plugins",
            file_path=str(settings_file),
        )
    except Exception as exc:
        raise PluginSettingsError(str(exc), status_code=409) from exc
    def apply_enablement(settings_data: dict[str, Any]) -> None:
        _persist_user_plugin_enablement(settings_data, plugin_id, enabled)

    _update_settings_json(apply_enablement)
    return get_plugin_settings(policy=policy)


async def remove_plugin(
    name: str,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    policy = _plugin_policy_from_stack()
    clean_name = str(name or "").strip()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if not clean_name:
        raise PluginSettingsError("Plugin name is required", status_code=400)
    install_root = plugin_install_root()
    store_root = plugin_install_root().parent / "store"
    inventory = get_plugin_settings([install_root, store_root], policy=policy).get("plugins", [])
    target = _resolve_plugin_inventory_item(clean_name, inventory)
    plugin_id = str(target.get("id") or plugin_id_for_name(
        str(target.get("name") or ""), str(target.get("marketplace") or "local")
    ))
    policy.assert_plugin_mutable(plugin_id)
    installed = [target]
    removal_root = install_root / ".removals" / uuid4().hex
    moved: list[tuple[Path, Path]] = []
    store_removal: Any | None = None
    reverse_dependents = find_reverse_dependents(
        plugin_id,
        get_plugin_snapshot(policy=policy).get("plugins", []),
    )
    # Gate the lifecycle mutation before staging or removing physical files.
    hook_result = await config_change_hook(source="plugins", file_path=str(settings_file))
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="plugins",
            file_path=str(settings_file),
        )
    except Exception as exc:
        raise PluginSettingsError(str(exc), status_code=409) from exc
    try:
        removal_root.mkdir(parents=True, exist_ok=False)
        for index, item in enumerate(installed):
            plugin_path = Path(str(item.get("path") or ""))
            if not _is_relative_to(plugin_path, install_root) and not _is_relative_to(plugin_path, store_root):
                raise PluginSettingsError(
                    "Refusing to remove a path outside the plugin root",
                    status_code=400,
                )
            if _is_relative_to(plugin_path, install_root):
                staged = removal_root / f"{index}-{plugin_path.name}"
                plugin_path.replace(staged)
                moved.append((plugin_path, staged))

        from backend.plugins.store import PluginStore

        store_removal = PluginStore().stage_remove(plugin_id)

        def clear_enablement(settings_data: dict[str, Any]) -> None:
            _clear_user_plugin_enablement(settings_data, plugin_id)

        _update_settings_json(clear_enablement)
    except Exception:
        if store_removal is not None:
            from backend.plugins.store import PluginStore

            PluginStore.rollback_remove(store_removal)
        for original, staged in reversed(moved):
            if staged.exists() and not original.exists():
                staged.replace(original)
        raise
    finally:
        if removal_root.exists() and not moved:
            _remove_within_root(removal_root, install_root)

    _remove_within_root(removal_root, install_root)
    if store_removal is not None:
        from backend.plugins.store import PluginStore

        PluginStore.commit_remove(store_removal)
    return {
        **get_plugin_settings(policy=policy),
        "removed": {
            "name": str(target.get("name") or clean_name),
            "id": plugin_id,
            "reverse_dependents": reverse_dependents,
        },
    }


async def import_plugin_from_path(
    source_path: str | Path,
    *,
    overwrite: bool = False,
    marketplace: str = "local",
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
    _policy: ManagedPluginPolicy | None = None,
    _trusted_marketplace: bool = False,
    _marketplace_source_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = _policy or _plugin_policy_from_stack()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if _trusted_marketplace and not isinstance(_marketplace_source_descriptor, Mapping):
        raise PluginSettingsError(
            "Trusted marketplace installs require the registered source provenance",
            status_code=403,
        )

    source = Path(str(source_path or "")).expanduser()
    if not str(source).strip():
        raise PluginSettingsError("Plugin source path is required", status_code=400)
    if source.is_file():
        return await import_plugin_package(
            source,
            overwrite=overwrite,
            marketplace=marketplace,
            settings_file=settings_file,
            config_change_hook=config_change_hook,
            _policy=policy,
            _trusted_marketplace=_trusted_marketplace,
            _marketplace_source_descriptor=_marketplace_source_descriptor,
        )
    source_text = str(source_path or "").strip()
    if _looks_like_remote_plugin_source(source_text):
        from backend.plugins.materializer import (
            MaterializationError,
            materialize_source,
            parse_marketplace_source,
        )

        try:
            parsed_source = parse_marketplace_source(source_text)
            source_descriptor = parsed_source.to_dict()
            source_descriptor["marketplace"] = str(marketplace or "local").strip() or "local"
            policy.assert_source_allowed(source_descriptor)
            staging_root = plugin_install_root().parent / ".remote-imports"
            staging_root.mkdir(parents=True, exist_ok=True)
            destination = staging_root / uuid4().hex
            materialized = materialize_source(parsed_source, destination)
            try:
                return await _install_plugin_directory(
                    materialized.path,
                    overwrite=overwrite,
                    settings_file=settings_file,
                    config_change_hook=config_change_hook,
                    import_kind="remote",
                    policy=policy,
                    trusted_marketplace=_trusted_marketplace,
                    marketplace_source_descriptor=_marketplace_source_descriptor,
                    source_descriptor={
                        **source_descriptor,
                        "provenance": materialized.to_dict(),
                    },
                )
            finally:
                if destination.exists():
                    _remove_within_root(destination, staging_root)
        except MaterializationError as exc:
            raise PluginSettingsError(str(exc), status_code=400) from exc
    if not source.is_dir():
        raise PluginSettingsError("Plugin source path must be an existing directory or .zip file", status_code=400)
    source_descriptor = {
        "source": "directory",
        "path": str(source.resolve()),
        "marketplace": str(marketplace or "local").strip() or "local",
    }
    policy.assert_source_allowed(
        _marketplace_source_descriptor
        if _trusted_marketplace and isinstance(_marketplace_source_descriptor, Mapping)
        else source_descriptor
    )
    return await _install_plugin_directory(
        source,
        overwrite=overwrite,
        settings_file=settings_file,
        config_change_hook=config_change_hook,
        import_kind="directory",
        policy=policy,
        trusted_marketplace=_trusted_marketplace,
        marketplace_source_descriptor=_marketplace_source_descriptor,
        source_descriptor=source_descriptor,
    )


async def import_plugin_package(
    package_path: str | Path,
    *,
    overwrite: bool = False,
    marketplace: str = "local",
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
    _policy: ManagedPluginPolicy | None = None,
    _trusted_marketplace: bool = False,
    _marketplace_source_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    policy = _policy or _plugin_policy_from_stack()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if _trusted_marketplace and not isinstance(_marketplace_source_descriptor, Mapping):
        raise PluginSettingsError(
            "Trusted marketplace installs require the registered source provenance",
            status_code=403,
        )

    package = Path(str(package_path or "")).expanduser()
    if not str(package).strip():
        raise PluginSettingsError("Plugin package path is required", status_code=400)
    if not package.is_file():
        raise PluginSettingsError("Plugin package path must be an existing file", status_code=400)
    if package.suffix.lower() != ".zip":
        raise PluginSettingsError("Plugin package must be a .zip file", status_code=400)

    source_descriptor = {
        "source": "file",
        "path": str(package.resolve()),
        "marketplace": str(marketplace or "local").strip() or "local",
    }
    policy.assert_source_allowed(
        _marketplace_source_descriptor
        if _trusted_marketplace and isinstance(_marketplace_source_descriptor, Mapping)
        else source_descriptor
    )
    policy.assert_plugin_installable(
        plugin_id_for_name(_plugin_name_from_package(package), marketplace),
        trusted_marketplace=_trusted_marketplace,
    )

    install_root = plugin_install_root()
    install_root.mkdir(parents=True, exist_ok=True)
    tmp_root = install_root / ".package-imports"
    tmp_root.mkdir(parents=True, exist_ok=True)
    token = _safe_plugin_folder_name(package.stem) or "plugin-package"
    tmp_extract = tmp_root / f"{token}.{os.getpid()}.tmp"
    if tmp_extract.exists():
        _remove_within_root(tmp_extract, install_root)
    tmp_extract.mkdir(parents=True)
    try:
        _extract_plugin_package(package, tmp_extract)
        source = _normalized_extracted_plugin_dir(tmp_extract)
        return await _install_plugin_directory(
            source,
            overwrite=overwrite,
            settings_file=settings_file,
            config_change_hook=config_change_hook,
            import_kind="package",
            package_path=package,
            policy=policy,
            trusted_marketplace=_trusted_marketplace,
            marketplace_source_descriptor=_marketplace_source_descriptor,
            source_descriptor=source_descriptor,
        )
    finally:
        if tmp_extract.exists():
            _remove_within_root(tmp_extract, install_root)


async def _install_plugin_directory(
    source: Path,
    *,
    overwrite: bool,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
    import_kind: str,
    package_path: Path | None = None,
    policy: ManagedPluginPolicy | None = None,
    source_descriptor: Mapping[str, Any] | None = None,
    trusted_marketplace: bool = False,
    marketplace_source_descriptor: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if not _is_plugin_directory(source):
        raise PluginSettingsError(
            "Plugin directory must contain .minicode-plugin/plugin.json",
            status_code=400,
        )
    linked_paths = _plugin_symlink_paths(source)
    if linked_paths:
        raise PluginSettingsError(
            "Plugin directories cannot contain symbolic links: " + ", ".join(linked_paths[:3]),
            status_code=400,
        )

    plugin_name = plugin_name_from_directory(source)
    folder_name = _safe_plugin_folder_name(plugin_name or source.name)
    if not folder_name:
        raise PluginSettingsError("Plugin name could not be resolved", status_code=400)
    effective_policy = policy or _plugin_policy_from_stack()
    if trusted_marketplace and not isinstance(marketplace_source_descriptor, Mapping):
        raise PluginSettingsError(
            "Trusted marketplace installs require the registered source provenance",
            status_code=403,
        )
    policy_source = (
        marketplace_source_descriptor
        if trusted_marketplace and isinstance(marketplace_source_descriptor, Mapping)
        else source_descriptor
        or {"source": "directory", "path": str(source.resolve())}
    )
    effective_policy.assert_source_allowed(policy_source)
    manifest_path = _primary_plugin_manifest(source)
    manifest_payload = _read_plugin_manifest(manifest_path) if manifest_path is not None else None
    marketplace = str((source_descriptor or {}).get("marketplace") or "local").strip() or "local"
    try:
        parse_plugin_id_strict(plugin_name, marketplace)
    except ValueError as exc:
        raise PluginSettingsError(f"Invalid plugin identity: {exc}", status_code=400) from exc
    plugin_id = plugin_id_for_name(plugin_name, marketplace)
    effective_policy.assert_plugin_installable(
        plugin_id,
        trusted_marketplace=trusted_marketplace,
    )
    validation = validate_plugin_directory(source)
    if not validation.get("ok"):
        first_error = str(
            (validation.get("errors") or ["Plugin validation failed"])[0]
        )
        raise PluginSettingsError(first_error, status_code=400)

    install_root = plugin_install_root()
    install_root.mkdir(parents=True, exist_ok=True)
    destination_folder = (
        folder_name
        if marketplace.casefold() == "local"
        else _safe_plugin_folder_name(f"{plugin_name}@{marketplace}")
    )
    destination = install_root / destination_folder
    source_resolved = source.resolve()
    install_root_resolved = install_root.resolve()
    destination_resolved = destination.resolve() if destination.exists() else (install_root_resolved / folder_name)
    if not _is_relative_to(destination_resolved, install_root_resolved):
        raise PluginSettingsError("Plugin destination escapes the plugin root", status_code=400)
    if _same_path(source_resolved, destination_resolved):
        return {
            **get_plugin_settings(policy=effective_policy),
            "imported": {
                "name": plugin_name,
                "id": plugin_id,
                "path": str(destination),
                "already_installed": True,
            },
        }
    if _is_relative_to(install_root_resolved, source_resolved):
        raise PluginSettingsError("Cannot import a plugin directory into itself", status_code=400)
    if destination.exists() and not overwrite:
        raise PluginSettingsError(f"Plugin '{plugin_name}' is already installed", status_code=409)

    token = uuid4().hex
    tmp_destination = install_root / f".{folder_name}.{token}.tmp"
    backup_destination = install_root / f".{folder_name}.{token}.backup"
    destination_replaced = False
    backup_created = False
    # Run the hook before any destination replacement or settings write.  A
    # blocked ConfigChange must leave both the compatibility projection and
    # the versioned store untouched.
    hook_result = await config_change_hook(source="plugins", file_path=str(settings_file))
    try:
        raise_if_config_change_blocked(
            hook_result,
            source="plugins",
            file_path=str(settings_file),
        )
    except Exception as exc:
        raise PluginSettingsError(str(exc), status_code=409) from exc
    try:
        shutil.copytree(
            source,
            tmp_destination,
            ignore=shutil.ignore_patterns(
                ".git",
                "__pycache__",
                ".pytest_cache",
                "node_modules",
                "dist",
                "build",
            ),
        )
        if destination.exists():
            if not overwrite:
                raise PluginSettingsError(
                    f"Plugin '{plugin_name}' is already installed",
                    status_code=409,
                )
            destination.replace(backup_destination)
            backup_created = True
        tmp_destination.replace(destination)
        destination_replaced = True

        # An explicit install is an explicit activation. Persist that positive
        # selection before exposing the materialized directory to consumers.
        def activate_plugin(settings_data: dict[str, Any]) -> None:
            _persist_user_plugin_enablement(settings_data, plugin_id, True)

        _update_settings_json(activate_plugin)

    except Exception:
        if destination_replaced and destination.exists():
            _remove_within_root(destination, install_root)
        if backup_created and backup_destination.exists():
            backup_destination.replace(destination)
        if tmp_destination.exists():
            _remove_within_root(tmp_destination, install_root)
        raise
    if backup_destination.exists():
        _remove_within_root(backup_destination, install_root)

    # Materialize a versioned provenance copy as the canonical store.  The
    # flat destination remains as a compatibility projection for existing
    # clients; runtime discovery can select the active version from the store
    # without exposing historical versions.
    store_record: dict[str, Any] | None = None
    try:
        from backend.plugins.store import PluginStore

        stored = PluginStore().materialize(
            source,
            name=plugin_name,
            marketplace=marketplace,
            version=str((manifest_payload or {}).get("version") or "local"),
            source=policy_source,
            activate=True,
            overwrite=overwrite,
        )
        store_record = stored.to_dict()
    except Exception as exc:
        # A read-only/legacy home must not make a validated flat import fail;
        # expose the degradation explicitly so callers can reconcile later.
        logger.warning("Failed to materialize versioned plugin store for %s: %s", plugin_id, exc)

    # cc surfaces declared-but-missing plugin dependencies instead of letting
    # an install silently proceed without them (installedPluginsManager tracks
    # the dependency closure). Report them so the UI and caller can reconcile.
    declared_dependencies = (manifest_payload or {}).get("dependencies")
    if isinstance(declared_dependencies, str):
        declared_dependencies = [declared_dependencies]
    missing_dependencies: list[str] = []
    if isinstance(declared_dependencies, list):
        installed_ids = {
            str(entry.get("id") or entry.get("name") or "").strip()
            for entry in get_plugin_settings(policy=effective_policy).get("plugins", [])
            if isinstance(entry, dict)
        }
        for dependency in declared_dependencies:
            token = str(dependency or "").strip()
            if token and token not in installed_ids:
                missing_dependencies.append(token)

    return {
        **get_plugin_settings(policy=effective_policy),
        "imported": {
            "name": plugin_name,
            "id": plugin_id,
            "path": str(destination),
            "already_installed": False,
            "kind": import_kind,
            **({"package_path": str(package_path)} if package_path is not None else {}),
            **({"store": store_record} if store_record is not None else {"store_error": "versioned store unavailable"}),
            **({"missing_dependencies": missing_dependencies} if missing_dependencies else {}),
        },
    }


def load_enabled_plugin_hook_sources(
    *,
    config_stack: Any | None = None,
) -> list[dict[str, Any]]:
    """Return source-aware hook declarations for enabled MiniCode plugins."""
    sources: list[dict[str, Any]] = []
    seen_manifests: set[str] = set()
    for plugin in get_plugin_snapshot(config_stack=config_stack).get("plugins", []):
        if not isinstance(plugin, Mapping) or not bool(plugin.get("enabled")):
            continue
        plugin_root = Path(str(plugin.get("path") or "")).resolve()
        plugin_id = str(plugin.get("id") or plugin.get("name") or plugin_root.name).strip()
        plugin_data_root = plugin_install_root().parent / "data" / _safe_plugin_data_segment(plugin_id)
        for raw_manifest in plugin.get("manifest_paths", []):
            manifest_path = Path(str(raw_manifest))
            key = _filesystem_path_key(manifest_path)
            if key in seen_manifests:
                continue
            seen_manifests.add(key)
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                continue
            if not isinstance(manifest, Mapping):
                continue
            payload = _declared_component_payload(
                plugin_root,
                manifest.get("hooks"),
                "hooks/hooks.json",
                "hooks",
            )
            for hook_map in _iter_hook_maps(payload):
                annotated: dict[str, Any] = {}
                for event_name, groups in hook_map.items():
                    if not isinstance(groups, list):
                        continue
                    copied_groups: list[Any] = []
                    for group in groups:
                        if not isinstance(group, Mapping):
                            continue
                        copied = dict(group)
                        handlers = copied.get("hooks")
                        if isinstance(handlers, list):
                            copied["hooks"] = [
                                {**dict(handler), "_plugin_root": str(plugin_root)}
                                if isinstance(handler, Mapping)
                                else handler
                                for handler in handlers
                            ]
                        copied_groups.append(copied)
                    if copied_groups:
                        annotated[str(event_name)] = copied_groups
                if annotated:
                    sources.append(
                        {
                            "hooks": annotated,
                            "source_path": str(manifest_path.resolve()),
                            "plugin_id": plugin_id,
                            "plugin_root": str(plugin_root),
                            "plugin_data_root": str(plugin_data_root),
                            "managed": bool(plugin.get("policy_managed")),
                        }
                    )
    return sources


def load_enabled_plugin_extension_sources(
    *,
    config_stack: Any | None = None,
) -> list[dict[str, Any]]:
    """Resolve enabled plugin extension declarations without executing code.

    Resolve package resources before handing executable extension paths to the
    loader. Active components come from the enabled snapshot rather than a
    scan of every materialized directory. This two-step boundary means this
    function returns lexical, root-contained paths plus provenance; the
    extension trust policy performs the final pre-import symlink/root check.

    Invalid declarations are returned as explicit error records so a broken
    plugin cannot disappear silently from diagnostics.
    """

    sources: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    snapshot = get_plugin_snapshot(config_stack=config_stack)
    for plugin in snapshot.get("plugins", []):
        if not isinstance(plugin, Mapping) or not bool(plugin.get("enabled")):
            continue
        raw_root = str(plugin.get("path") or "").strip()
        plugin_id = str(
            plugin.get("id") or plugin.get("name") or Path(raw_root).name
        ).strip()
        marketplace = str(plugin.get("marketplace") or "local").strip() or "local"
        managed = bool(plugin.get("policy_managed"))
        base = {
            "plugin_id": plugin_id,
            "marketplace": marketplace,
            "managed": managed,
            "scope": "managed" if managed else "user",
            "origin": "plugin",
            "plugin_fingerprint": str(snapshot.get("fingerprint") or ""),
        }
        if not raw_root:
            sources.append({**base, "error": "enabled plugin has no materialized root"})
            continue
        plugin_root = Path(raw_root).expanduser().absolute()
        try:
            resolved_root = plugin_root.resolve(strict=True)
        except OSError as exc:
            sources.append(
                {
                    **base,
                    "plugin_root": str(plugin_root),
                    "error": f"plugin root is unavailable: {exc}",
                }
            )
            continue
        if not resolved_root.is_dir():
            sources.append(
                {
                    **base,
                    "plugin_root": str(plugin_root),
                    "error": "plugin root is not a directory",
                }
            )
            continue
        base["plugin_root"] = str(plugin_root)

        for raw_manifest in plugin.get("manifest_paths", ()):
            manifest_path = Path(str(raw_manifest)).expanduser().absolute()
            try:
                manifest_resolved = manifest_path.resolve(strict=True)
                manifest_resolved.relative_to(resolved_root)
            except (OSError, ValueError) as exc:
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": f"plugin manifest is outside its materialized root: {exc}",
                    }
                )
                continue
            try:
                manifest = json.loads(manifest_resolved.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": f"plugin manifest could not be read: {exc}",
                    }
                )
                continue
            if not isinstance(manifest, Mapping):
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": "plugin manifest must be an object",
                    }
                )
                continue
            declared = manifest.get("extensions")
            raw_paths = [declared] if isinstance(declared, str) else declared
            if declared is None:
                continue
            if not isinstance(raw_paths, list) or any(
                not isinstance(item, str) or not item.strip() for item in raw_paths
            ):
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "error": "extensions must be a relative path string or string array",
                    }
                )
                continue
            for raw_path in raw_paths:
                candidate = _declared_plugin_component_path(plugin_root, raw_path)
                if candidate is None:
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "declaration": raw_path,
                            "error": "extension path is absolute or escapes the plugin root",
                        }
                    )
                    continue
                key = (plugin_id.casefold(), canonical_file_path_key(candidate))
                if key in seen:
                    continue
                seen.add(key)
                if not candidate.exists():
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "path": str(candidate),
                            "declaration": raw_path,
                            "error": "declared extension path does not exist",
                        }
                    )
                    continue
                if candidate.is_file() and candidate.suffix.casefold() not in {
                    ".py",
                    ".pyw",
                }:
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "path": str(candidate),
                            "declaration": raw_path,
                            "error": "declared extension file must use .py or .pyw",
                        }
                    )
                    continue
                if not candidate.is_file() and not candidate.is_dir():
                    sources.append(
                        {
                            **base,
                            "source_path": str(manifest_path),
                            "path": str(candidate),
                            "declaration": raw_path,
                            "error": "declared extension path is not a file or directory",
                        }
                    )
                    continue
                sources.append(
                    {
                        **base,
                        "source_path": str(manifest_path),
                        "path": str(candidate),
                        "declaration": raw_path,
                    }
                )
    return sources


def resolve_enabled_plugin_mentions(
    mentions: Iterable[Mapping[str, Any]],
    *,
    connected_mcp_servers: Iterable[str] = (),
    config_stack: Any | None = None,
) -> list[dict[str, Any]]:
    """Resolve structured ``plugin://`` mentions against enabled local plugins.

    Client-supplied display metadata is ignored, disabled or missing plugins
    are dropped, and only MCP
    servers that are actually connected for this session are advertised.
    """
    inventory_by_id: dict[str, Mapping[str, Any]] = {}
    inventory_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for item in get_plugin_snapshot(config_stack=config_stack).get("plugins", []):
        if not isinstance(item, Mapping) or not bool(item.get("enabled")):
            continue
        raw_id = str(item.get("id") or item.get("name") or "").strip()
        identity = plugin_id_for_name(raw_id, str(item.get("marketplace") or "local"))
        key = normalize_plugin_id(identity)
        name_key = normalize_plugin_name(str(item.get("name") or parse_plugin_id(identity).name))
        if key:
            inventory_by_id[key] = item
        if name_key:
            inventory_by_name.setdefault(name_key, []).append(item)
    connected = {str(name).strip() for name in connected_mcp_servers if str(name).strip()}
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mention in mentions:
        if not isinstance(mention, Mapping):
            continue
        config_name = str(
            mention.get("config_name")
            or mention.get("name")
            or ""
        ).strip()
        path = str(mention.get("path") or "").strip()
        if path.startswith("plugin://"):
            config_name = path.removeprefix("plugin://").strip()
        key = normalize_plugin_id(config_name)
        plugin = inventory_by_id.get(key)
        if plugin is None and "@" not in config_name:
            candidates = inventory_by_name.get(normalize_plugin_name(config_name), [])
            plugin = candidates[0] if len(candidates) == 1 else None
        if plugin is None or key in seen:
            continue
        resolved_key = normalize_plugin_id(str(plugin.get("id") or plugin.get("name") or config_name))
        if resolved_key in seen:
            continue
        seen.add(resolved_key)
        declared_servers = {
            str(name).strip()
            for name in plugin.get("mcp_server_names", [])
            if str(name).strip()
        }
        resolved.append({
            "config_name": str(plugin.get("name") or config_name),
            "display_name": str(plugin.get("displayName") or plugin.get("name") or config_name),
            "description": str(plugin.get("shortDescription") or plugin.get("description") or ""),
            "has_skills": int(plugin.get("skill_count") or 0) > 0,
            "mcp_server_names": sorted(declared_servers & connected, key=str.casefold),
            # MiniCode does not expose plugin apps in the Agent runtime yet, so
            # do not claim they are available merely because the manifest lists them.
            "available_apps": [],
        })
    return resolved


def _resolve_plugin_inventory_item(
    requested: str,
    inventory: Any,
) -> Mapping[str, Any]:
    normalized = normalize_plugin_id(requested)
    requested_name = normalize_plugin_name(parse_plugin_id(requested).name)
    candidates = [
        item
        for item in inventory
        if isinstance(item, Mapping)
        and (
            normalize_plugin_id(str(item.get("id") or item.get("name") or ""), str(item.get("marketplace") or "local")) == normalized
            or (
                "@" not in str(requested)
                and normalize_plugin_name(str(item.get("name") or "")) == requested_name
            )
        )
    ] if isinstance(inventory, Iterable) else []
    if not candidates:
        raise PluginSettingsError(f"Plugin '{requested}' was not found", status_code=404)
    if len(candidates) > 1:
        choices = ", ".join(
            sorted(str(item.get("id") or item.get("name") or "") for item in candidates)
        )
        raise PluginSettingsError(
            f"Plugin name '{requested}' is ambiguous; use one of: {choices}",
            status_code=409,
        )
    return candidates[0]


def resolve_plugin_asset(plugin_path: str | Path, variant: str) -> Path | None:
    """Resolve one official interface asset from an installed plugin."""
    try:
        requested = Path(plugin_path).expanduser().resolve()
    except OSError:
        return None
    installed = {
        Path(str(entry.get("path") or "")).expanduser().resolve()
        for entry in get_plugin_settings().get("plugins", [])
        if str(entry.get("path") or "").strip()
    }
    if requested not in installed:
        return None
    manifest = _primary_plugin_manifest(requested)
    if manifest is None:
        return None
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    interface = payload.get("interface") if isinstance(payload, Mapping) else None
    if not isinstance(interface, Mapping):
        return None
    field = {"composer": "composerIcon", "logo": "logo", "logo-dark": "logoDark"}.get(variant)
    return _resolve_manifest_asset(manifest, interface.get(field)) if field else None


