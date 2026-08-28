"""Plugin packaging, archive safety, and install-directory validation."""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any
from collections.abc import Mapping
from uuid import uuid4

from backend.feature_flags import feature_enabled
from backend.plugins.identity import parse_plugin_id
from backend.plugins.layout import (
    PLUGIN_MANIFEST_DIRECTORY,
    plugin_install_root,
    plugin_manifest_path,
)
from backend.plugins.manifest import (
    _count_plugin_skills,
    _manifest_component_counts,
    _plugin_manifest_paths,
    _plugin_metadata,
    _validate_plugin_manifest,
    plugin_name_from_directory,
)
from backend.plugins.policy import PluginSettingsError

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
    tmp_path = output_root / f".{filename}.{uuid4().hex}.tmp"

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

def validate_plugin_directory(source_path: str | Path) -> dict[str, Any]:
    if not feature_enabled("plugin_lifecycle_api", True):
        raise PluginSettingsError("Plugin lifecycle API is disabled", status_code=404)

    source = _resolve_plugin_source_directory(source_path)
    manifest_paths = _plugin_manifest_paths(source)
    warnings: list[str] = []
    errors: list[str] = []
    manifests: list[dict[str, Any]] = []

    if not manifest_paths:
        errors.append("Plugin must contain .minicode-plugin/plugin.json.")

    for manifest_path in manifest_paths:
        manifest_result = _validate_plugin_manifest(manifest_path)
        manifests.append(manifest_result["manifest"])
        warnings.extend(manifest_result["warnings"])
        errors.extend(manifest_result["errors"])

    skill_count = _count_plugin_skills(source)
    component_counts = _manifest_component_counts(manifest_paths[0]) if manifest_paths else {
        "mcp_server_count": 0,
        "app_count": 0,
        "hook_count": 0,
        "extension_count": 0,
    }
    if skill_count <= 0 and not any(component_counts.values()):
        warnings.append(
            "Plugin manifest does not expose skills, MCP servers, apps, hooks, or extensions."
        )

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

def _looks_like_remote_plugin_source(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.startswith(("http://", "https://", "git@", "ssh://", "npm:")):
        return True
    # Accept GitHub shorthand (owner/repo or owner/repo#ref).
    if "/" in text and not text.startswith(("./", "../", ".\\", "..\\", "/")):
        parts = text.split("/", 2)
        if len(parts) == 2 and all(parts) and all(
            all(char.isalnum() or char in "-_.@#" for char in part)
            for part in parts
        ):
            return True
    return False

def _collect_packable_plugin_files(plugin_dir: Path) -> tuple[list[tuple[Path, str, int]], list[str]]:
    files: list[tuple[Path, str, int]] = []
    excluded: list[str] = []
    total_bytes = 0
    for root, dirnames, filenames in os.walk(plugin_dir):
        root_path = Path(root)
        kept_dirs: list[str] = []
        for dirname in dirnames:
            rel = _relative_plugin_path(root_path / dirname, plugin_dir)
            if dirname in _PLUGIN_EXCLUDED_DIRS or dirname.startswith(".") and dirname != PLUGIN_MANIFEST_DIRECTORY:
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
        for info, rel in _validated_plugin_package_infos(archive):
            target = destination / rel
            try:
                target.resolve().relative_to(destination.resolve())
            except ValueError as exc:
                raise PluginSettingsError(f"Plugin package escapes destination: {info.filename}", status_code=400) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)

def _plugin_name_from_package(package_path: Path) -> str:
    try:
        archive = zipfile.ZipFile(package_path)
    except zipfile.BadZipFile as exc:
        raise PluginSettingsError("Plugin package is not a valid zip file", status_code=400) from exc
    with archive:
        candidates = [
            (info, rel)
            for info, rel in _validated_plugin_package_infos(archive)
            if Path(rel).parts[-2:] == (PLUGIN_MANIFEST_DIRECTORY, "plugin.json")
        ]
        if not candidates:
            raise PluginSettingsError(
                "Plugin package must contain .minicode-plugin/plugin.json",
                status_code=400,
            )
        candidates.sort(key=lambda item: (len(Path(item[1]).parts), item[1].casefold()))
        try:
            raw = json.loads(archive.read(candidates[0][0]).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise PluginSettingsError(
                "Plugin package manifest is not valid UTF-8 JSON",
                status_code=400,
            ) from exc
        name = str(raw.get("name") or "").strip() if isinstance(raw, Mapping) else ""
        if not name:
            raise PluginSettingsError(
                "Plugin package manifest name is required",
                status_code=400,
            )
        return name

def _validated_plugin_package_infos(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, str]]:
    infos = [info for info in archive.infolist() if not info.is_dir()]
    if not infos:
        raise PluginSettingsError("Plugin package is empty", status_code=400)
    if len(infos) > _MAX_PLUGIN_ARCHIVE_ENTRIES:
        raise PluginSettingsError("Plugin package has too many files", status_code=400)
    validated: list[tuple[zipfile.ZipInfo, str]] = []
    total_bytes = 0
    for info in infos:
        rel = _safe_zip_member_path(info.filename)
        if rel is None:
            raise PluginSettingsError(
                f"Plugin package contains unsafe path: {info.filename}",
                status_code=400,
            )
        if Path(rel).parts[0] in _PLUGIN_EXCLUDED_DIRS:
            continue
        if info.file_size > _MAX_PLUGIN_PACKAGE_FILE_BYTES:
            raise PluginSettingsError(
                f"Plugin package file is too large: {rel}",
                status_code=400,
            )
        total_bytes += info.file_size
        if total_bytes > _MAX_PLUGIN_PACKAGE_BYTES:
            raise PluginSettingsError("Plugin package is too large", status_code=400)
        validated.append((info, rel))
    return validated

def _safe_zip_member_path(name: str) -> str | None:
    raw = str(name or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("~"):
        return None
    normalized = raw.strip("/")
    if not normalized:
        return None
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return None
    if re.match(r"^[A-Za-z]:", parts[0]):
        return None
    return "/".join(parts)

def _safe_plugin_data_segment(plugin_identity: str) -> str:
    parsed = parse_plugin_id(plugin_identity)
    name = parsed.name or "plugin"
    marketplace = parsed.marketplace or "local"
    # Keep the segment stable across platforms and independent of user input.
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in f"{name}-{marketplace}")

def _normalized_extracted_plugin_dir(extract_root: Path) -> Path:
    if _is_plugin_directory(extract_root):
        return extract_root
    children = [child for child in extract_root.iterdir() if child.is_dir()]
    if len(children) == 1 and _is_plugin_directory(children[0]):
        return children[0]
    raise PluginSettingsError(
            "Plugin package must contain .minicode-plugin/plugin.json at the root",
            status_code=400,
        )

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

def _is_plugin_directory(path: Path) -> bool:
    return plugin_manifest_path(path).is_file()

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

_MAX_PLUGIN_PACKAGE_BYTES = 50 * 1024 * 1024

_MAX_PLUGIN_PACKAGE_FILE_BYTES = 5 * 1024 * 1024

_MAX_PLUGIN_ARCHIVE_ENTRIES = 1000
