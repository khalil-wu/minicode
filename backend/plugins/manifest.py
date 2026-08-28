"""Plugin manifest reading, validation, and inventory metadata helpers."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Mapping

from backend.plugins.identity import (
    is_valid_identifier,
    normalize_plugin_id,
    parse_plugin_id,
    parse_plugin_id_strict,
)
from backend.plugins.layout import PLUGIN_MANIFEST_DIRECTORY, plugin_manifest_path

def plugin_name_from_directory(plugin_dir: Path) -> str:
    plugin_dir = Path(plugin_dir)
    name = plugin_name_from_manifest(plugin_manifest_path(plugin_dir))
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
    if manifest_path.parent.name == PLUGIN_MANIFEST_DIRECTORY:
        return manifest_path.parent.parent
    return manifest_path.parent

def _read_plugin_manifest(manifest_path: Path) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None

def _primary_plugin_manifest(plugin_dir: Path) -> Path | None:
    """Return the canonical MiniCode plugin manifest."""

    for manifest in _plugin_manifest_paths(Path(plugin_dir)):
        if manifest.is_file():
            return manifest
    return None

def _looks_like_versioned_store(plugin_dir: Path) -> bool:
    """Recognize ``marketplace/name/version`` stores without trusting names."""
    parts = [part for part in plugin_dir.resolve().parts if part not in {"", "."}]
    if len(parts) < 3:
        return False
    candidate = parts[-1]
    return candidate == "local" or bool(
        re.fullmatch(r"v?\d+(?:\.\d+){1,3}(?:[-+][0-9A-Za-z.-]+)?", candidate)
    )

def _validate_versioned_store_path(
    plugin_dir: Path,
    plugin_roots: Iterable[Path],
) -> str:
    """Validate a store-backed plugin before exposing its manifest."""
    if not _looks_like_versioned_store(plugin_dir):
        return ""
    resolved = plugin_dir.resolve() if plugin_dir.exists() else plugin_dir.absolute()
    for raw_root in plugin_roots:
        root = Path(raw_root).expanduser()
        try:
            root_resolved = root.resolve()
            relative = resolved.relative_to(root_resolved)
        except (OSError, ValueError):
            continue
        if len(relative.parts) < 3:
            continue
        if root.name.casefold() != "store" and not (plugin_dir / ".plugin-store.json").is_file():
            continue
        try:
            from backend.plugins.store import PluginStore
            records = PluginStore(root).list()
        except Exception as exc:
            return f"invalid plugin store record: {exc}"
        for record in records:
            if record.path.absolute() == resolved:
                if not record.active:
                    return "plugin store record is not the active version"
                return ""
        return "plugin store record failed integrity validation"
    return ""

def _plugin_marketplace_for_manifest(
    manifest_path: Path,
    raw_manifest: Mapping[str, Any] | None,
    plugin_roots: Iterable[Path],
) -> str:
    # Marketplace identity comes from the registry/store path, never from
    # self-authored manifest metadata.
    plugin_dir = plugin_directory_for_manifest(manifest_path)
    try:
        resolved = plugin_dir.resolve()
    except OSError:
        resolved = plugin_dir.absolute()
    # Versioned stores use <root>/<marketplace>/<plugin>/<version>; cache
    # layouts use <root>/cache/<owner>/<plugin>/<version>.
    for raw_root in plugin_roots:
        root = Path(raw_root).expanduser()
        try:
            rel = resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            continue
        parts = rel.parts
        if len(parts) == 1 and "@" in parts[0]:
            projected = parse_plugin_id(parts[0])
            manifest_name = str((raw_manifest or {}).get("name") or "").strip()
            if (
                projected.name == manifest_name
                and is_valid_identifier(projected.name, projected.marketplace)
            ):
                return projected.marketplace
        if len(parts) >= 3 and parts[-1] and _looks_like_versioned_store(resolved):
            return str(parts[-3])
        if len(parts) >= 4 and parts[0].casefold() == "cache":
            return str(parts[1])
    return "local"

def _manifest_source_descriptor(
    plugin_dir: Path,
    marketplace: str,
    raw_manifest: Mapping[str, Any] | None,
) -> dict[str, Any]:
    # A manifest is untrusted plugin content and cannot self-attest a trusted
    # Git/GitHub/NPM provenance.  A versioned store record is written by the
    # installer boundary and may carry the verified source descriptor.
    if _looks_like_versioned_store(plugin_dir):
        try:
            from backend.plugins.store import PluginStore
            store_root = plugin_dir.parents[2]
            record = next(
                (item for item in PluginStore(store_root).list() if item.path.absolute() == plugin_dir.absolute()),
                None,
            )
            if record is not None and isinstance(record.source, Mapping) and record.source.get("source"):
                return dict(record.source)
        except Exception:
            # A malformed store must never make its self-authored metadata
            # authoritative.  Fall through to an untrusted directory source;
            # the caller will mark the record disabled via integrity checks.
            pass
    return {
        "source": "directory",
        "path": str(plugin_dir),
        "marketplace": marketplace,
    }

def _filesystem_path_key(value: Any) -> str:
    return os.path.normcase(os.path.normpath(str(value or "")))

def _prefer_active_store_entries(
    inventory: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Collapse flat compatibility projections to one active record per id."""
    selected: dict[str, tuple[int, str, dict[str, Any]]] = {}
    for entry in inventory.values():
        identity = normalize_plugin_id(str(entry.get("id") or entry.get("name") or ""))
        if not identity:
            identity = f"<invalid>:{_filesystem_path_key(entry.get('path'))}"
        provenance = entry.get("provenance") if isinstance(entry.get("provenance"), Mapping) else {}
        rank = 2 if str(provenance.get("store") or "") == "versioned" else 1
        path = str(entry.get("path") or "")
        previous = selected.get(identity)
        if previous is None or (rank, path.casefold()) > (previous[0], previous[1].casefold()):
            selected[identity] = (rank, path, entry)
    return {
        f"{identity}::{_filesystem_path_key(entry.get('path'))}": entry
        for identity, (_rank, _path, entry) in selected.items()
    }

def _iter_hook_maps(payload: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            yield from _iter_hook_maps(item)
        return
    if not isinstance(payload, Mapping):
        return
    hooks = payload.get("hooks")
    yield hooks if isinstance(hooks, Mapping) else payload

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
    if not entry.get("dependencies"):
        dependencies = raw.get("dependencies")
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        if isinstance(dependencies, list):
            entry["dependencies"] = sorted({
                str(item).strip() for item in dependencies
                if isinstance(item, str) and item.strip()
            }, key=str.casefold)
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

def _count_plugin_skills(plugin_dir: Path) -> int:
    names: set[str] = set()
    skills_dirs: list[Path] = []
    manifest_path = _primary_plugin_manifest(plugin_dir)
    try:
        if manifest_path is None:
            raise FileNotFoundError(str(plugin_dir))
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

def _plugin_manifest_paths(plugin_dir: Path) -> list[Path]:
    manifest = plugin_manifest_path(plugin_dir)
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
    elif not is_valid_identifier(
        plugin_name,
        str(raw.get("marketplace") or "local").strip() or "local",
    ):
        errors.append(
            f"{rel_manifest}: name/marketplace contains unsafe characters; use a canonical plugin id."
        )
    else:
        try:
            parse_plugin_id_strict(
                plugin_name,
                str(raw.get("marketplace") or "local").strip() or "local",
            )
        except ValueError as exc:
            errors.append(f"{rel_manifest}: invalid plugin identity: {exc}")
    unsupported = sorted(str(key) for key in raw.keys() if str(key) not in _PLUGIN_MANIFEST_FIELDS)
    if unsupported:
        errors.append(
            f"{rel_manifest}: unsupported manifest fields: {', '.join(unsupported)}."
        )
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
    extensions = raw.get("extensions")
    if extensions is not None and not (
        isinstance(extensions, str)
        or isinstance(extensions, list)
        and all(isinstance(item, str) and item.strip() for item in extensions)
    ):
        errors.append(
            f"{rel_manifest}: extensions must be a relative path string or string array."
        )
    if isinstance(extensions, str):
        extensions = [extensions]
    if isinstance(extensions, list):
        plugin_root = plugin_directory_for_manifest(manifest_path)
        for extension_path in extensions:
            if not str(extension_path).strip() or _declared_plugin_component_path(
                plugin_root, str(extension_path)
            ) is None:
                errors.append(
                    f"{rel_manifest}: extension path escapes the plugin root: {extension_path!r}."
                )
    apps = raw.get("apps")
    if apps is not None and not isinstance(apps, str):
        errors.append(f"{rel_manifest}: apps must be a relative path string.")
    mcp_servers = raw.get("mcp_servers")
    if mcp_servers is not None and not isinstance(mcp_servers, (str, Mapping)):
        errors.append(f"{rel_manifest}: mcp_servers must be a relative path string or object.")
    dependencies = raw.get("dependencies")
    if dependencies is not None and not (
        isinstance(dependencies, str)
        or isinstance(dependencies, list)
        and all(isinstance(item, str) and item.strip() for item in dependencies)
    ):
        errors.append(f"{rel_manifest}: dependencies must be a plugin id string or string array.")
    if isinstance(dependencies, str) and not dependencies.strip():
        errors.append(f"{rel_manifest}: dependencies must not contain an empty id.")
    marketplace = raw.get("marketplace")
    if marketplace is not None and (
        not isinstance(marketplace, str) or not marketplace.strip()
    ):
        errors.append(f"{rel_manifest}: marketplace must be a non-empty string.")
    source = raw.get("source")
    if source is not None and not isinstance(source, Mapping):
        errors.append(f"{rel_manifest}: source must be an object.")
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

def _manifest_component_counts(manifest_path: Path) -> dict[str, int]:
    try:
        raw = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        raw = {}
    if not isinstance(raw, Mapping):
        raw = {}
    plugin_dir = plugin_directory_for_manifest(Path(manifest_path))
    mcp_servers = _declared_component_payload(
        plugin_dir,
        raw.get("mcp_servers"),
        ".mcp.json",
        "servers",
    )
    apps = _declared_component_payload(plugin_dir, raw.get("apps"), ".app.json", "apps")
    hooks = _declared_component_payload(plugin_dir, raw.get("hooks"), "hooks/hooks.json", "hooks")
    extensions = raw.get("extensions")
    extension_paths = (
        [extensions]
        if isinstance(extensions, str) and extensions.strip()
        else [
            item
            for item in extensions
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(extensions, list)
        else []
    )
    return {
        "mcp_server_count": len(mcp_servers) if isinstance(mcp_servers, Mapping) else int(bool(mcp_servers)),
        "app_count": len(apps) if isinstance(apps, Mapping) else int(bool(apps)),
        "hook_count": _count_hook_declarations(hooks),
        "extension_count": len(extension_paths),
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
        raw.get("mcp_servers"),
        ".mcp.json",
        "servers",
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

def _declared_plugin_component_path(plugin_root: Path, declaration: str) -> Path | None:
    """Return a lexical in-root component path, preserving symlink evidence."""

    raw = str(declaration or "").strip()
    if (
        not raw
        or "\x00" in raw
        or raw.startswith(("/", "\\", "~"))
        or re.match(r"^[A-Za-z]:", raw)
    ):
        return None
    lexical = (plugin_root / raw).expanduser().absolute()
    try:
        lexical.resolve(strict=False).relative_to(plugin_root.resolve(strict=True))
    except (OSError, ValueError):
        return None
    return lexical

_PLUGIN_MANIFEST_FIELDS = {
    "name", "version", "description", "keywords", "skills", "hooks",
    "mcp_servers", "apps", "interface", "dependencies", "marketplace",
    "source", "commands", "prompts", "extensions",
}
