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
_COMMAND_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SAFE_PROMPT_FORM_COMPONENTS = {"prompt-form", "prompt_form"}
_MAX_PLUGIN_PACKAGE_BYTES = 50 * 1024 * 1024
_MAX_PLUGIN_PACKAGE_FILE_BYTES = 5 * 1024 * 1024
_MAX_PLUGIN_ARCHIVE_ENTRIES = 1000


def plugin_install_root() -> Path:
    from backend.commands.plugins import default_plugin_roots

    explicit_home = Path(str(Path.home() / ".minicode"))
    try:
        import os

        explicit_home = Path(os.environ.get("MINICODE_HOME") or explicit_home)
    except Exception:
        pass
    target = explicit_home.expanduser() / "plugins"
    roots = default_plugin_roots()
    if any(_same_path(target, root) for root in roots):
        return target
    return target


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
    for manifest_path in (
        plugin_dir / ".codex-plugin" / "plugin.json",
        plugin_dir / "plugin.json",
        plugin_dir / "commands.json",
    ):
        name = plugin_name_from_manifest(manifest_path)
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
        _commands_from_manifest,
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
                "description": "",
                "version": "",
                "path": str(plugin_dir),
                "manifest_path": str(manifest_path),
                "manifest_paths": [],
                "command_count": 0,
                "skill_count": 0,
                "enabled": normalize_plugin_name(name) not in disabled,
            },
        )
        entry["manifest_paths"].append(str(manifest_path))
        entry["command_count"] += len(_commands_from_manifest(manifest_path, include_disabled=True))
        _merge_manifest_metadata(entry, manifest_path)

    for entry in inventory.values():
        plugin_dir = Path(str(entry["path"]))
        entry["skill_count"] = _count_plugin_skills(plugin_dir)
        entry["disabled"] = not bool(entry["enabled"])

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
        raise PluginSettingsError("Plugin source path must be an existing directory or .minicode-plugin.zip file", status_code=400)
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
    if package.suffix.lower() != ".zip" or not package.name.lower().endswith(".minicode-plugin.zip"):
        raise PluginSettingsError("Plugin package must end with .minicode-plugin.zip", status_code=400)

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
    command_count = 0
    manifests: list[dict[str, Any]] = []

    if not manifest_paths:
        warnings.append("No plugin manifest found; only bundled skills will be available.")

    for manifest_path in manifest_paths:
        manifest_result = _validate_plugin_manifest(manifest_path)
        manifests.append(manifest_result["manifest"])
        warnings.extend(manifest_result["warnings"])
        errors.extend(manifest_result["errors"])
        command_count += int(manifest_result["manifest"].get("command_count") or 0)

    skill_count = _count_plugin_skills(source)
    if command_count <= 0 and skill_count <= 0:
        errors.append("Plugin must expose at least one command or bundled skill.")

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
        "command_count": command_count,
        "skill_count": skill_count,
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
        entry["description"] = str(raw.get("description") or raw.get("summary") or "").strip()
    if not entry.get("version"):
        entry["version"] = str(raw.get("version") or "").strip()
    raw_enabled = raw.get("enabled")
    if isinstance(raw_enabled, bool) and raw_enabled is False:
        entry["manifest_enabled"] = False


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
    candidates = [
        plugin_dir / ".codex-plugin" / "plugin.json",
        plugin_dir / "plugin.json",
        plugin_dir / "commands.json",
    ]
    return [path for path in candidates if path.is_file()]


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
            metadata["description"] = str(raw.get("description") or raw.get("summary") or "").strip()
    return metadata


def _validate_plugin_manifest(manifest_path: Path) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []
    rel_manifest = str(manifest_path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "manifest": {"path": rel_manifest, "command_count": 0},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: invalid JSON at line {exc.lineno} column {exc.colno}"],
        }
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "manifest": {"path": rel_manifest, "command_count": 0},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: could not be read: {exc}"],
        }

    if isinstance(raw, list):
        plugin_name = plugin_directory_for_manifest(manifest_path).name
        command_specs = raw
    elif isinstance(raw, Mapping):
        plugin_name = str(raw.get("name") or plugin_directory_for_manifest(manifest_path).name).strip()
        if not plugin_name:
            errors.append(f"{rel_manifest}: manifest name must not be empty.")
        command_specs = raw.get("commands", raw.get("slash_commands", []))
        if isinstance(command_specs, Mapping):
            command_specs = [
                {"name": name, **value} if isinstance(value, Mapping) else {"name": name, "template": value}
                for name, value in command_specs.items()
            ]
    else:
        return {
            "manifest": {"path": rel_manifest, "command_count": 0},
            "warnings": warnings,
            "errors": [f"{rel_manifest}: manifest must be an object or command list."],
        }

    if command_specs in (None, ""):
        command_specs = []
    if not isinstance(command_specs, list):
        errors.append(f"{rel_manifest}: commands must be a list or object map.")
        command_specs = []

    valid_count = 0
    seen_commands: set[str] = set()
    for index, spec in enumerate(command_specs):
        command_errors = _validate_command_spec(spec, index=index, seen_commands=seen_commands)
        if command_errors:
            errors.extend(f"{rel_manifest}: {item}" for item in command_errors)
        else:
            valid_count += 1

    if len(command_specs) > 80:
        warnings.append(f"{rel_manifest}: contains {len(command_specs)} commands; large manifests can make command search noisy.")
    if not command_specs:
        warnings.append(f"{rel_manifest}: manifest has no commands.")

    return {
        "manifest": {
            "path": rel_manifest,
            "plugin_name": plugin_name,
            "command_count": valid_count,
            "raw_command_count": len(command_specs),
        },
        "warnings": warnings,
        "errors": errors,
    }


def _validate_command_spec(spec: Any, *, index: int, seen_commands: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(spec, Mapping):
        return [f"command #{index + 1} must be an object."]

    command = str(spec.get("command") or spec.get("name") or "").strip().lstrip("/")
    if not command or not _COMMAND_NAME_RE.match(command):
        errors.append(f"command #{index + 1} has an invalid name.")
    normalized_command = command.lower()
    if normalized_command and normalized_command in seen_commands:
        errors.append(f"command '{normalized_command}' is duplicated.")
    if normalized_command:
        seen_commands.add(normalized_command)

    raw_type = str(spec.get("type") or spec.get("kind") or "template").strip().lower()
    command_type = raw_type
    if command_type in {"local-ui", "ui", "ui-action", "panel", "local_jsx", "local-jsx"}:
        command_type = "local"
    if command_type not in {"template", "protocol", "local"}:
        errors.append(f"command '{command or index + 1}' has unsupported type '{raw_type}'.")
        return errors

    if command_type == "template":
        template = str(spec.get("template") or spec.get("prompt") or spec.get("content") or "").strip()
        if not template:
            errors.append(f"template command '{command or index + 1}' must define template, prompt, or content.")

    if command_type == "protocol":
        handler = str(
            spec.get("protocol_command")
            or spec.get("protocolCommand")
            or spec.get("command_type")
            or spec.get("commandType")
            or spec.get("handler")
            or spec.get("action")
            or ""
        ).strip()
        if not handler:
            errors.append(f"protocol command '{command or index + 1}' must define handler.")

    if raw_type in {"local_jsx", "local-jsx"}:
        component = str(spec.get("component") or spec.get("jsx_component") or spec.get("jsxComponent") or "").strip().lower()
        if component not in _SAFE_PROMPT_FORM_COMPONENTS:
            errors.append(f"local-jsx command '{command or index + 1}' must use component 'prompt-form'.")

    return errors


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
    return f"{base}-{version_part}.minicode-plugin.zip"


def _count_plugin_skills(plugin_dir: Path) -> int:
    names: set[str] = set()
    for skills_dir in (plugin_dir / "skills", plugin_dir / ".codex-plugin" / "skills"):
        if not skills_dir.is_dir():
            continue
        for skill_dir in skills_dir.iterdir():
            if skill_dir.is_dir() and (skill_dir / "SKILL.md").is_file():
                names.add(skill_dir.name)
    return len(names)


def _is_plugin_directory(path: Path) -> bool:
    for manifest_path in (
        path / ".codex-plugin" / "plugin.json",
        path / "plugin.json",
        path / "commands.json",
    ):
        if manifest_path.is_file():
            return True
    return _count_plugin_skills(path) > 0


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
