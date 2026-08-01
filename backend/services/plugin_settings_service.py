from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from collections.abc import Awaitable, Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from backend.config import _load_settings_json, _write_settings_json
from backend.feature_flags import feature_enabled

ConfigChangeHook = Callable[..., Awaitable[None]]


class PluginSettingsError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def normalize_plugin_name(name: str) -> str:
    return str(name or "").strip().casefold()


_PLUGIN_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
}
_PLUGIN_EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}
_PLUGIN_MANIFEST_FIELDS = {
    "name", "version", "description", "keywords", "skills", "hooks",
    "mcpServers", "apps", "interface",
}
_MAX_PLUGIN_PACKAGE_BYTES = 50 * 1024 * 1024
_MAX_PLUGIN_PACKAGE_FILE_BYTES = 5 * 1024 * 1024
_MAX_PLUGIN_ARCHIVE_ENTRIES = 1000


def plugin_install_root() -> Path:
    return Path.home() / ".agents" / "plugins" / "plugins"


def get_disabled_plugin_names(settings_data: Mapping[str, Any] | None = None) -> set[str]:
    data = settings_data if settings_data is not None else _load_settings_json()
    raw_plugins = data.get("plugins") if isinstance(data, Mapping) else {}
    if not isinstance(raw_plugins, Mapping):
        return set()
    raw_disabled = raw_plugins.get("disabled", [])
    if isinstance(raw_disabled, str):
        raw_disabled = [raw_disabled]
    if not isinstance(raw_disabled, Iterable):
        return set()
    return {
        normalize_plugin_name(str(item))
        for item in raw_disabled
        if normalize_plugin_name(str(item))
    }


def plugin_name_from_directory(plugin_dir: Path) -> str:
    plugin_dir = Path(plugin_dir)
    name = plugin_name_from_manifest(plugin_dir / ".codex-plugin" / "plugin.json")
    if name:
        return name
    return plugin_dir.name


def plugin_name_from_manifest(manifest_path: Path) -> str:
    manifest_path = Path(manifest_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return ""
    plugin_dir = plugin_directory_for_manifest(manifest_path)
    if isinstance(raw, Mapping):
        name = str(raw.get("name") or "").strip()
        if name:
            return name
    return plugin_dir.name


def plugin_directory_for_manifest(manifest_path: Path) -> Path:
    manifest_path = Path(manifest_path)
    if manifest_path.parent.name == ".codex-plugin":
        return manifest_path.parent.parent
    return manifest_path.parent


def get_plugin_settings(plugin_roots: Iterable[Path] | None = None) -> dict[str, Any]:
    from backend.commands.plugins import (
        _iter_plugin_manifests,
        default_plugin_roots,
    )

    settings_data = _load_settings_json()
    disabled = get_disabled_plugin_names(settings_data)
    inventory: dict[str, dict[str, Any]] = {}

    for manifest_path in _iter_plugin_manifests(plugin_roots or default_plugin_roots()):
        plugin_dir = plugin_directory_for_manifest(manifest_path)
        name = plugin_name_from_manifest(manifest_path) or plugin_dir.name
        key = str(plugin_dir.resolve()) if plugin_dir.exists() else str(plugin_dir.absolute())
        entry = inventory.setdefault(
            key,
            {
                "name": name,
                "displayName": "",
                "description": "",
                "shortDescription": "",
                "longDescription": "",
                "developerName": "",
                "category": "",
                "capabilities": [],
                "version": "",
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
                "runtime_support": {
                    "skills": True,
                    "mcpServers": True,
                    "apps": False,
                    "hooks": False,
                },
                "enabled": normalize_plugin_name(name) not in disabled,
            },
        )
        entry["manifest_paths"].append(str(manifest_path))
        _merge_manifest_metadata(entry, manifest_path)
        counts = _manifest_component_counts(manifest_path)
        entry["mcp_server_count"] += counts["mcp_server_count"]
        entry["mcp_server_names"] = sorted({
            *entry.get("mcp_server_names", []),
            *_manifest_mcp_server_names(manifest_path),
        }, key=str.casefold)
        entry["app_count"] += counts["app_count"]
        entry["hook_count"] += counts["hook_count"]

    for entry in inventory.values():
        plugin_dir = Path(str(entry["path"]))
        entry["skill_count"] = _count_plugin_skills(plugin_dir)
        entry["disabled"] = not bool(entry["enabled"])
        entry["managed"] = _is_relative_to(plugin_dir, plugin_install_root())

    plugins = sorted(
        inventory.values(),
        key=lambda item: (not bool(item.get("enabled")), str(item.get("name", "")).casefold(), str(item.get("path", "")).casefold()),
    )
    return {
        "plugins": plugins,
        "disabled": sorted(disabled),
        "feature_enabled": feature_enabled("plugin_lifecycle_api", True),
    }


async def update_plugin_enabled(
    name: str,
    enabled: bool,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)
    if not clean_name:
        raise PluginSettingsError("Plugin name is required", status_code=400)
    if not isinstance(enabled, bool):
        raise PluginSettingsError("Plugin enabled must be a boolean", status_code=400)
    known_plugins = get_plugin_settings()
    known_names = {
        normalize_plugin_name(str(item.get("name", "")))
        for item in known_plugins.get("plugins", [])
        if isinstance(item, Mapping)
    }
    normalized = normalize_plugin_name(clean_name)
    if normalized not in known_names:
        raise PluginSettingsError(f"Plugin '{clean_name}' was not found", status_code=404)

    settings_data = _load_settings_json()
    raw_plugins = settings_data.get("plugins")
    plugins = dict(raw_plugins) if isinstance(raw_plugins, Mapping) else {}
    disabled = _disabled_plugin_name_map(plugins)

    if enabled:
        disabled.pop(normalized, None)
    else:
        disabled[normalized] = clean_name

    if disabled:
        plugins["disabled"] = [disabled[key] for key in sorted(disabled)]
    else:
        plugins.pop("disabled", None)

    if plugins:
        settings_data["plugins"] = plugins
    else:
        settings_data.pop("plugins", None)

    _write_settings_json(settings_data)
    await config_change_hook(source="plugins", file_path=str(settings_file))
    return get_plugin_settings()


async def remove_plugin(
    name: str,
    *,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise PluginSettingsError("Plugin name is required", status_code=400)
    install_root = plugin_install_root()
    normalized = normalize_plugin_name(clean_name)
    installed = [
        item for item in get_plugin_settings([install_root]).get("plugins", [])
        if isinstance(item, Mapping)
        and normalize_plugin_name(str(item.get("name") or "")) == normalized
    ]
    if not installed:
        raise PluginSettingsError(
            f"Plugin '{clean_name}' is not managed by the local plugin installer",
            status_code=404,
        )
    for item in installed:
        _remove_within_root(Path(str(item.get("path") or "")), install_root)

    settings_data = _load_settings_json()
    raw_plugins = settings_data.get("plugins")
    if isinstance(raw_plugins, Mapping):
        plugins = dict(raw_plugins)
        disabled = _disabled_plugin_name_map(plugins)
        disabled.pop(normalized, None)
        if disabled:
            plugins["disabled"] = [disabled[key] for key in sorted(disabled)]
        else:
            plugins.pop("disabled", None)
        if plugins:
            settings_data["plugins"] = plugins
        else:
            settings_data.pop("plugins", None)
        _write_settings_json(settings_data)
    await config_change_hook(source="plugins", file_path=str(settings_file))
    return {**get_plugin_settings(), "removed": {"name": clean_name}}


async def import_plugin_from_path(
    source_path: str | Path,
    *,
    overwrite: bool = False,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    source = Path(str(source_path or "")).expanduser()
    if not str(source).strip():
        raise PluginSettingsError("Plugin source path is required", status_code=400)
    if source.is_file():
        return await import_plugin_package(
            source,
            overwrite=overwrite,
            settings_file=settings_file,
            config_change_hook=config_change_hook,
        )
    if not source.is_dir():
        raise PluginSettingsError("Plugin source path must be an existing directory or .zip file", status_code=400)
    return await _install_plugin_directory(
        source,
        overwrite=overwrite,
        settings_file=settings_file,
        config_change_hook=config_change_hook,
        import_kind="directory",
    )


async def import_plugin_package(
    package_path: str | Path,
    *,
    overwrite: bool = False,
    settings_file: Path,
    config_change_hook: ConfigChangeHook,
) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    package = Path(str(package_path or "")).expanduser()
    if not str(package).strip():
        raise PluginSettingsError("Plugin package path is required", status_code=400)
    if not package.is_file():
        raise PluginSettingsError("Plugin package path must be an existing file", status_code=400)
    if package.suffix.lower() != ".zip":
        raise PluginSettingsError("Plugin package must be a .zip file", status_code=400)

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
) -> dict[str, Any]:
    if not _is_plugin_directory(source):
        raise PluginSettingsError("Plugin directory must contain a plugin manifest or skills/SKILL.md entries", status_code=400)
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

    install_root = plugin_install_root()
    install_root.mkdir(parents=True, exist_ok=True)
    destination = install_root / folder_name
    source_resolved = source.resolve()
    install_root_resolved = install_root.resolve()
    destination_resolved = destination.resolve() if destination.exists() else (install_root_resolved / folder_name)
    if not _is_relative_to(destination_resolved, install_root_resolved):
        raise PluginSettingsError("Plugin destination escapes the plugin root", status_code=400)
    if _same_path(source_resolved, destination_resolved):
        return {
            **get_plugin_settings(),
            "imported": {
                "name": plugin_name,
                "path": str(destination),
                "already_installed": True,
            },
        }
    if _is_relative_to(install_root_resolved, source_resolved):
        raise PluginSettingsError("Cannot import a plugin directory into itself", status_code=400)
    if destination.exists() and not overwrite:
        raise PluginSettingsError(f"Plugin '{plugin_name}' is already installed", status_code=409)

    tmp_destination = install_root / f".{folder_name}.tmp"
    if tmp_destination.exists():
        _remove_within_root(tmp_destination, install_root)
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
        _remove_within_root(destination, install_root)
    tmp_destination.replace(destination)

    settings_data = _load_settings_json()
    plugins = settings_data.get("plugins")
    if isinstance(plugins, Mapping):
        disabled = _disabled_plugin_name_map(plugins)
        normalized = normalize_plugin_name(plugin_name)
        if normalized in disabled:
            next_plugins = dict(plugins)
            disabled.pop(normalized, None)
            if disabled:
                next_plugins["disabled"] = [disabled[key] for key in sorted(disabled)]
            else:
                next_plugins.pop("disabled", None)
            if next_plugins:
                settings_data["plugins"] = next_plugins
            else:
                settings_data.pop("plugins", None)
            _write_settings_json(settings_data)

    await config_change_hook(source="plugins", file_path=str(settings_file))
    return {
        **get_plugin_settings(),
        "imported": {
            "name": plugin_name,
            "path": str(destination),
            "already_installed": False,
            "kind": import_kind,
            **({"package_path": str(package_path)} if package_path is not None else {}),
        },
    }


def validate_plugin_directory(source_path: str | Path) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    source = _resolve_plugin_source_directory(source_path)
    manifest_paths = _plugin_manifest_paths(source)
    warnings: list[str] = []
    errors: list[str] = []
    manifests: list[dict[str, Any]] = []

    if not manifest_paths:
        errors.append("Plugin must contain .codex-plugin/plugin.json.")

    for manifest_path in manifest_paths:
        manifest_result = _validate_plugin_manifest(manifest_path)
        manifests.append(manifest_result["manifest"])
        warnings.extend(manifest_result["warnings"])
        errors.extend(manifest_result["errors"])

    skill_count = _count_plugin_skills(source)
    component_counts = _manifest_component_counts(manifest_paths[0]) if manifest_paths else {
        "mcp_server_count": 0, "app_count": 0, "hook_count": 0,
    }
    if skill_count <= 0 and not any(component_counts.values()):
        warnings.append("Plugin manifest does not expose skills, MCP servers, apps, or hooks.")

    linked_paths = _plugin_symlink_paths(source)
    if linked_paths:
        errors.append(
            "Plugin directories cannot contain symbolic links: "
            + ", ".join(linked_paths[:3])
        )

    files, excluded = _collect_packable_plugin_files(source)
    if not files:
        errors.append("Plugin directory has no packageable files.")

    metadata = _plugin_metadata(source, manifest_paths)
    plugin_name = metadata.get("name") or plugin_name_from_directory(source)
    plugin = {
        "name": plugin_name,
        "version": metadata.get("version", ""),
        "description": metadata.get("description", ""),
        "path": str(source),
        "manifest_paths": [str(path) for path in manifest_paths],
        "skill_count": skill_count,
        **component_counts,
        "file_count": len(files),
        "total_bytes": sum(size for _path, _rel, size in files),
    }
    return {
        "ok": not errors,
        "plugin": plugin,
        "manifests": manifests,
        "warnings": warnings,
        "errors": errors,
        "excluded": excluded[:80],
    }


def load_enabled_plugin_hook_settings() -> list[dict[str, Any]]:
    """Load executable hook groups declared by enabled Codex plugins.

    The settings screen previously counted these declarations but the runtime
    never consumed them. Keep the plugin root on each command hook so the hook
    runner can apply Codex/CC's ``${CLAUDE_PLUGIN_ROOT}`` contract.
    """
    layers: list[dict[str, Any]] = []
    seen_manifests: set[str] = set()
    for plugin in get_plugin_settings().get("plugins", []):
        if not isinstance(plugin, Mapping) or not bool(plugin.get("enabled")):
            continue
        plugin_root = Path(str(plugin.get("path") or "")).resolve()
        for raw_manifest in plugin.get("manifest_paths", []):
            manifest_path = Path(str(raw_manifest))
            key = str(manifest_path.resolve())
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
                    layers.append({"hooks": annotated})
    return layers


def _iter_hook_maps(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_hook_maps(item)
        return
    if not isinstance(payload, Mapping):
        return
    hooks = payload.get("hooks")
    yield hooks if isinstance(hooks, Mapping) else payload


def resolve_enabled_plugin_mentions(
    mentions: Iterable[Mapping[str, Any]],
    *,
    connected_mcp_servers: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Resolve structured ``plugin://`` mentions against enabled local plugins.

    This mirrors Codex's explicit plugin mention gate: client-supplied display
    metadata is ignored, disabled or missing plugins are dropped, and only MCP
    servers that are actually connected for this session are advertised.
    """
    inventory = {
        normalize_plugin_name(str(item.get("name") or "")): item
        for item in get_plugin_settings().get("plugins", [])
        if isinstance(item, Mapping)
        and bool(item.get("enabled"))
        and normalize_plugin_name(str(item.get("name") or ""))
    }
    connected = {str(name).strip() for name in connected_mcp_servers if str(name).strip()}
    resolved: list[dict[str, Any]] = []
    seen: set[str] = set()
    for mention in mentions:
        if not isinstance(mention, Mapping):
            continue
        config_name = str(
            mention.get("config_name")
            or mention.get("configName")
            or mention.get("name")
            or ""
        ).strip()
        path = str(mention.get("path") or "").strip()
        if path.startswith("plugin://"):
            config_name = path.removeprefix("plugin://").strip()
        key = normalize_plugin_name(config_name)
        plugin = inventory.get(key)
        if plugin is None or key in seen:
            continue
        seen.add(key)
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


def package_plugin_directory(source_path: str | Path, output_dir: str | Path | None = None) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    source = _resolve_plugin_source_directory(source_path)
    validation = validate_plugin_directory(source)
    if not validation.get("ok"):
        first_error = str((validation.get("errors") or ["Plugin validation failed"])[0])
        raise PluginSettingsError(first_error, status_code=400)

    files, excluded = _collect_packable_plugin_files(source)
    if not files:
        raise PluginSettingsError("Plugin directory has no packageable files", status_code=400)

    plugin = validation.get("plugin") if isinstance(validation.get("plugin"), Mapping) else {}
    plugin_name = str(plugin.get("name") or plugin_name_from_directory(source)).strip()
    version = str(plugin.get("version") or "dev").strip()
    filename = _plugin_package_filename(plugin_name, version)
    output_root = Path(str(output_dir)).expanduser() if output_dir else plugin_install_root().parent / "plugin-packages"
    output_root.mkdir(parents=True, exist_ok=True)
    package_path = output_root / filename
    tmp_path = output_root / f".{filename}.tmp"
    if tmp_path.exists():
        tmp_path.unlink()

    with zipfile.ZipFile(tmp_path, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path, rel_path, _size in files:
            archive.write(file_path, rel_path)
    tmp_path.replace(package_path)

    return {
        "ok": True,
        "package": {
            "name": filename,
            "path": str(package_path),
            "file_count": len(files),
            "total_bytes": sum(size for _path, _rel, size in files),
        },
        "validation": {
            **validation,
            "excluded": excluded[:80],
        },
    }


def _disabled_plugin_name_map(plugins: Mapping[str, Any]) -> dict[str, str]:
    raw_disabled = plugins.get("disabled", [])
    if isinstance(raw_disabled, str):
        raw_disabled = [raw_disabled]
    if not isinstance(raw_disabled, Iterable):
        return {}
    result: dict[str, str] = {}
    for item in raw_disabled:
        value = str(item).strip()
        normalized = normalize_plugin_name(value)
        if normalized:
            result[normalized] = value
    return result


def _merge_manifest_metadata(entry: dict[str, Any], manifest_path: Path) -> None:
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return
    if not isinstance(raw, Mapping):
        return
    if not entry.get("description"):
        entry["description"] = str(raw.get("description") or "").strip()
    if not entry.get("version"):
        entry["version"] = str(raw.get("version") or "").strip()
    interface = raw.get("interface") if isinstance(raw.get("interface"), Mapping) else {}
    scalar_fields = {
        "displayName": "displayName",
        "shortDescription": "shortDescription",
        "longDescription": "longDescription",
        "developerName": "developerName",
        "category": "category",
        "brandColor": "brandColor",
    }
    for target, source in scalar_fields.items():
        if not entry.get(target):
            entry[target] = str(interface.get(source) or "").strip()
    if not entry.get("websiteUrl"):
        website_url = str(interface.get("websiteUrl") or interface.get("websiteURL") or "").strip()
        if website_url.startswith(("https://", "http://")):
            entry["websiteUrl"] = website_url
    if not entry.get("capabilities"):
        capabilities = interface.get("capabilities")
        if isinstance(capabilities, list):
            entry["capabilities"] = [str(item).strip() for item in capabilities if str(item).strip()]
    if not entry.get("defaultPrompt"):
        default_prompt = interface.get("defaultPrompt")
        if isinstance(default_prompt, str) and default_prompt.strip():
            entry["defaultPrompt"] = [default_prompt.strip()]
        elif isinstance(default_prompt, list):
            entry["defaultPrompt"] = [str(item).strip() for item in default_prompt if str(item).strip()]
    if not entry.get("iconVariant"):
        for field, variant in (("logo", "logo"), ("composerIcon", "composer"), ("logoDark", "logo-dark")):
            if _resolve_manifest_asset(manifest_path, interface.get(field)) is not None:
                entry["iconVariant"] = variant
                break


def _resolve_manifest_asset(manifest_path: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    plugin_dir = plugin_directory_for_manifest(manifest_path)
    try:
        candidate = (plugin_dir / value).resolve()
        candidate.relative_to(plugin_dir.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


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
    manifest = requested / ".codex-plugin" / "plugin.json"
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    interface = payload.get("interface") if isinstance(payload, Mapping) else None
    if not isinstance(interface, Mapping):
        return None
    field = {"composer": "composerIcon", "logo": "logo", "logo-dark": "logoDark"}.get(variant)
    return _resolve_manifest_asset(manifest, interface.get(field)) if field else None


def _resolve_plugin_source_directory(source_path: str | Path) -> Path:
    source_text = str(source_path or "").strip()
    if not source_text:
        raise PluginSettingsError("Plugin source path is required", status_code=400)
    source = Path(source_text).expanduser()
    if not source.is_dir():
        raise PluginSettingsError("Plugin source path must be an existing directory", status_code=400)
    try:
        return source.resolve()
    except OSError:
        return source.absolute()


def _plugin_manifest_paths(plugin_dir: Path) -> list[Path]:
    manifest = plugin_dir / ".codex-plugin" / "plugin.json"
    return [manifest] if manifest.is_file() else []


def _plugin_metadata(plugin_dir: Path, manifest_paths: list[Path]) -> dict[str, str]:
    metadata = {
        "name": plugin_dir.name,
        "version": "",
        "description": "",
    }
    for manifest_path in manifest_paths:
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(raw, Mapping):
            continue
        if not metadata["name"] or metadata["name"] == plugin_dir.name:
            name = str(raw.get("name") or "").strip()
            if name:
                metadata["name"] = name
        if not metadata["version"]:
            metadata["version"] = str(raw.get("version") or "").strip()
        if not metadata["description"]:
            metadata["description"] = str(raw.get("description") or "").strip()
    return metadata


def _validate_plugin_manifest(manifest_path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    rel_manifest = str(manifest_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "manifest": {"path": rel_manifest},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: invalid JSON at line {exc.lineno} column {exc.colno}"],
        }
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "manifest": {"path": rel_manifest},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: could not be read: {exc}"],
        }

    if not isinstance(raw, Mapping):
        return {
            "manifest": {"path": rel_manifest},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: manifest must be a JSON object."],
        }
    plugin_name = str(raw.get("name") or "").strip()
    if not plugin_name:
        errors.append(f"{rel_manifest}: manifest name is required.")
    unsupported = sorted(str(key) for key in raw.keys() if str(key) not in _PLUGIN_MANIFEST_FIELDS)
    if unsupported:
        errors.append(f"{rel_manifest}: unsupported fields: {', '.join(unsupported)}.")
    skills = raw.get("skills")
    if skills is not None and not (
        isinstance(skills, str)
        or isinstance(skills, list) and all(isinstance(item, str) for item in skills)
    ):
        errors.append(f"{rel_manifest}: skills must be a relative path string or string array.")
    hooks = raw.get("hooks")
    if hooks is not None and not (
        isinstance(hooks, (str, Mapping))
        or isinstance(hooks, list) and all(isinstance(item, (str, Mapping)) for item in hooks)
    ):
        errors.append(f"{rel_manifest}: hooks must be a path, path array, object, or object array.")
    apps = raw.get("apps")
    if apps is not None and not isinstance(apps, str):
        errors.append(f"{rel_manifest}: apps must be a relative path string.")
    mcp_servers = raw.get("mcpServers")
    if mcp_servers is not None and not isinstance(mcp_servers, (str, Mapping)):
        errors.append(f"{rel_manifest}: mcpServers must be a relative path string or object.")
    interface = raw.get("interface")
    if interface is not None and not isinstance(interface, Mapping):
        errors.append(f"{rel_manifest}: interface must be an object.")

    return {
        "manifest": {
            "path": rel_manifest,
            "plugin_name": plugin_name,
            **_manifest_component_counts(manifest_path),
        },
        "warnings": warnings,
        "errors": errors,
    }


def _collect_packable_plugin_files(plugin_dir: Path) -> tuple[list[tuple[Path, str, int]], list[str]]:
    files: list[tuple[Path, str, int]] = []
    excluded: list[str] = []
    total_bytes = 0
    for root, dirnames, filenames in os.walk(plugin_dir):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel = _relative_plugin_path(root_path / dirname, plugin_dir)
            if dirname in _PLUGIN_EXCLUDED_DIRS or dirname.startswith(".") and dirname not in {".codex-plugin"}:
                excluded.append(rel)
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs

        for filename in filenames:
            file_path = root_path / filename
            rel = _relative_plugin_path(file_path, plugin_dir)
            if filename in _PLUGIN_EXCLUDED_FILES or filename.endswith((".pyc", ".pyo", ".tmp", ".log")):
                excluded.append(rel)
                continue
            try:
                size = file_path.stat().st_size
            except OSError:
                excluded.append(rel)
                continue
            if size > _MAX_PLUGIN_PACKAGE_FILE_BYTES:
                excluded.append(rel)
                continue
            if total_bytes + size > _MAX_PLUGIN_PACKAGE_BYTES:
                excluded.append(rel)
                continue
            files.append((file_path, rel, size))
            total_bytes += size
    files.sort(key=lambda item: item[1].lower())
    return files, sorted(excluded)


def _plugin_symlink_paths(plugin_dir: Path, *, limit: int = 20) -> list[str]:
    """Return in-tree links without ever traversing their targets.

    Plugin packages must be self-contained. Following a directory-import link
    would otherwise silently copy files from outside the selected plugin, and
    package creation would archive those external files under a trusted name.
    """
    found: list[str] = []
    for root, dirnames, filenames in os.walk(plugin_dir, followlinks=False):
        root_path = Path(root)
        for name in [*dirnames, *filenames]:
            candidate = root_path / name
            if not candidate.is_symlink():
                continue
            found.append(_relative_plugin_path(candidate, plugin_dir))
            if len(found) >= limit:
                return found
    return found


def _extract_plugin_package(package_path: Path, destination: Path) -> None:
    try:
        archive = zipfile.ZipFile(package_path)
    except zipfile.BadZipFile as exc:
        raise PluginSettingsError("Plugin package is not a valid zip file", status_code=400) from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if not infos:
            raise PluginSettingsError("Plugin package is empty", status_code=400)
        if len(infos) > _MAX_PLUGIN_ARCHIVE_ENTRIES:
            raise PluginSettingsError("Plugin package has too many files", status_code=400)

        total_bytes = 0
        for info in infos:
            rel = _safe_zip_member_path(info.filename)
            if rel is None:
                raise PluginSettingsError(f"Plugin package contains unsafe path: {info.filename}", status_code=400)
            if Path(rel).parts[0] in _PLUGIN_EXCLUDED_DIRS:
                continue
            if info.file_size > _MAX_PLUGIN_PACKAGE_FILE_BYTES:
                raise PluginSettingsError(f"Plugin package file is too large: {rel}", status_code=400)
            total_bytes += info.file_size
            if total_bytes > _MAX_PLUGIN_PACKAGE_BYTES:
                raise PluginSettingsError("Plugin package is too large", status_code=400)

            target = destination / rel
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise PluginSettingsError(f"Plugin package escapes destination: {info.filename}", status_code=400) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _safe_zip_member_path(name: str) -> str | None:
    normalized = str(name or "").replace("\\", "/").strip("/")
    if not normalized:
        return None
    if normalized.startswith("/") or normalized.startswith("~"):
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return "/".join(parts)


def _normalized_extracted_plugin_dir(extract_root: Path) -> Path:
    if _is_plugin_directory(extract_root):
        return extract_root
    children = [child for child in extract_root.iterdir() if child.is_dir()]
    if len(children) == 1 and _is_plugin_directory(children[0]):
        return children[0]
    raise PluginSettingsError("Plugin package must contain a plugin manifest or skills/SKILL.md entries at the root", status_code=400)


def _relative_plugin_path(path: Path, root: Path) -> str:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        rel = path.absolute().relative_to(root.absolute())
    return rel.as_posix()


def _plugin_package_filename(plugin_name: str, version: str) -> str:
    base = _safe_plugin_folder_name(plugin_name) or "plugin"
    version_part = _safe_plugin_folder_name(version) or "dev"
    return f"{base}-{version_part}.zip"


def _count_plugin_skills(plugin_dir: Path) -> int:
    names: set[str] = set()
    skills_dirs: list[Path] = []
    manifest_path = plugin_dir / ".codex-plugin" / "plugin.json"
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        configured = raw.get("skills") if isinstance(raw, Mapping) else None
        configured_paths = [configured] if isinstance(configured, str) else configured if isinstance(configured, list) else []
        for configured_path in configured_paths:
            if not isinstance(configured_path, str) or not configured_path.strip():
                continue
            candidate = (plugin_dir / configured_path).resolve()
            candidate.relative_to(plugin_dir.resolve())
            skills_dirs.append(candidate)
    except (OSError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    if not skills_dirs:
        skills_dirs.append(plugin_dir / "skills")
    for skills_dir in skills_dirs:
        if not skills_dir.is_dir():
            continue
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
                names.add(skill_dir.name)
    return len(names)


def _manifest_component_counts(manifest_path: Path) -> dict[str, int]:
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    plugin_dir = plugin_directory_for_manifest(Path(manifest_path))
    mcp_servers = _declared_component_payload(plugin_dir, raw.get("mcpServers"), ".mcp.json", "mcpServers")
    apps = _declared_component_payload(plugin_dir, raw.get("apps"), ".app.json", "apps")
    hooks = _declared_component_payload(plugin_dir, raw.get("hooks"), "hooks/hooks.json", "hooks")
    return {
        "mcp_server_count": len(mcp_servers) if isinstance(mcp_servers, Mapping) else int(bool(mcp_servers)),
        "app_count": len(apps) if isinstance(apps, Mapping) else int(bool(apps)),
        "hook_count": _count_hook_declarations(hooks),
    }


def _manifest_mcp_server_names(manifest_path: Path) -> list[str]:
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(raw, Mapping):
        return []
    plugin_dir = plugin_directory_for_manifest(Path(manifest_path))
    servers = _declared_component_payload(
        plugin_dir,
        raw.get("mcpServers"),
        ".mcp.json",
        "mcpServers",
    )
    if not isinstance(servers, Mapping):
        return []
    return sorted(
        {str(name).strip() for name in servers if str(name).strip()},
        key=str.casefold,
    )


def _declared_component_payload(
    plugin_dir: Path,
    declaration: Any,
    default_path: str,
    container_key: str,
) -> Any:
    if isinstance(declaration, Mapping):
        return declaration.get(container_key, declaration)
    inline_payloads: list[Any] = []
    if isinstance(declaration, list) and container_key == "hooks":
        paths = []
        for item in declaration:
            if isinstance(item, str) and item.strip():
                paths.append(item)
            elif isinstance(item, Mapping):
                inline_payloads.append(item.get(container_key, item))
    else:
        paths = [declaration] if isinstance(declaration, str) and declaration.strip() else [default_path]
    payloads: list[Any] = []
    for raw_path in paths:
        candidate = (plugin_dir / str(raw_path)).resolve()
        try:
            candidate.relative_to(plugin_dir.resolve())
        except ValueError:
            continue
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(payload, Mapping):
            payloads.append(payload.get(container_key, payload))
    payloads = [*inline_payloads, *payloads]
    if len(payloads) == 1:
        return payloads[0]
    return payloads


def _count_hook_declarations(payload: Any) -> int:
    if isinstance(payload, list):
        return sum(_count_hook_declarations(item) for item in payload)
    if not isinstance(payload, Mapping):
        return 0
    hooks = payload.get("hooks") if isinstance(payload.get("hooks"), Mapping) else payload
    total = 0
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            handlers = entry.get("hooks")
            total += len(handlers) if isinstance(handlers, list) else int(bool(entry.get("command")))
    return total


def _is_plugin_directory(path: Path) -> bool:
    return (path / ".codex-plugin" / "plugin.json").is_file()


def _safe_plugin_folder_name(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(name or "").strip()).strip(".-")
    return value[:80]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


def _remove_within_root(path: Path, root: Path) -> None:
    root_resolved = root.resolve()
    path_resolved = path.resolve()
    if not _is_relative_to(path_resolved, root_resolved) or path_resolved == root_resolved:
        raise PluginSettingsError("Refusing to remove a path outside the plugin root", status_code=400)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
