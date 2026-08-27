from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.mcp.registry import normalize_name_for_mcp
from backend.workspace.trust import is_workspace_trusted


PROJECT_MCP_APPROVED = "approved"
PROJECT_MCP_REJECTED = "rejected"
PROJECT_MCP_PENDING = "pending"
_PROJECT_SETTINGS_LOCK = threading.RLock()


def project_mcp_config_paths(workspace_root: Path | None) -> tuple[Path, ...]:
    """Return the trusted MiniCode project MCP configuration path."""

    if workspace_root is None or not is_workspace_trusted(workspace_root):
        return ()
    try:
        root = Path(workspace_root).expanduser().resolve()
    except OSError:
        return ()
    return (root / ".minicode" / "mcp.json",)


def project_local_settings_path(workspace_root: Path) -> Path:
    return Path(workspace_root).expanduser().resolve() / ".minicode" / "mcp.local.json"


def read_project_local_settings(workspace_root: Path) -> dict[str, Any]:
    path = project_local_settings_path(workspace_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Failed to read project local settings at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Project local settings at {path} must be a JSON object")
    return dict(payload)


def project_mcp_server_status(server_name: str, workspace_root: Path) -> str:
    """Resolve rejected, approved/all, then pending project MCP status."""

    settings = read_project_local_settings(workspace_root)
    normalized = normalize_name_for_mcp(server_name)
    disabled = _normalized_name_set(settings.get("disabled_servers"))
    if normalized in disabled:
        return PROJECT_MCP_REJECTED
    enabled = _normalized_name_set(settings.get("enabled_servers"))
    if normalized in enabled or settings.get("approve_all") is True:
        return PROJECT_MCP_APPROVED
    return PROJECT_MCP_PENDING


def approve_project_mcp_server(
    server_name: str,
    workspace_root: Path,
    *,
    approve_all: bool = False,
) -> Path:
    path = project_local_settings_path(workspace_root)
    with _PROJECT_SETTINGS_LOCK, file_mutation_locks([path]):
        settings = _trusted_project_settings(workspace_root)
        enabled = _string_list(settings.get("enabled_servers"))
        normalized = normalize_name_for_mcp(server_name)
        if normalized not in {normalize_name_for_mcp(item) for item in enabled}:
            enabled.append(server_name)
        settings["enabled_servers"] = enabled
        disabled = [
            item
            for item in _string_list(settings.get("disabled_servers"))
            if normalize_name_for_mcp(item) != normalized
        ]
        if disabled:
            settings["disabled_servers"] = disabled
        else:
            settings.pop("disabled_servers", None)
        if approve_all:
            settings["approve_all"] = True
        return _write_project_local_settings(workspace_root, settings)


def reject_project_mcp_server(server_name: str, workspace_root: Path) -> Path:
    path = project_local_settings_path(workspace_root)
    with _PROJECT_SETTINGS_LOCK, file_mutation_locks([path]):
        settings = _trusted_project_settings(workspace_root)
        disabled = _string_list(settings.get("disabled_servers"))
        normalized = normalize_name_for_mcp(server_name)
        if normalized not in {normalize_name_for_mcp(item) for item in disabled}:
            disabled.append(server_name)
        settings["disabled_servers"] = disabled
        enabled = [
            item
            for item in _string_list(settings.get("enabled_servers"))
            if normalize_name_for_mcp(item) != normalized
        ]
        if enabled:
            settings["enabled_servers"] = enabled
        else:
            settings.pop("enabled_servers", None)
        return _write_project_local_settings(workspace_root, settings)


def _trusted_project_settings(workspace_root: Path) -> dict[str, Any]:
    if not is_workspace_trusted(workspace_root):
        raise ValueError("The active workspace is not trusted for project MCP servers")
    return read_project_local_settings(workspace_root)


def _write_project_local_settings(workspace_root: Path, settings: dict[str, Any]) -> Path:
    path = project_local_settings_path(workspace_root)
    atomic_write_text(path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
    return path


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _normalized_name_set(value: Any) -> set[str]:
    return {normalize_name_for_mcp(item) for item in _string_list(value)}
