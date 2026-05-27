from __future__ import annotations

import os
from pathlib import Path


def _is_windows(platform_name: str | None = None) -> bool:
    return (platform_name or os.name).lower() == "nt"


def _looks_like_drive_path(path_value: str) -> bool:
    return len(path_value) >= 2 and path_value[1] == ":" and path_value[0].isalpha()


def is_windows_root_relative_path(path_value: str, *, platform_name: str | None = None) -> bool:
    """Return True for Windows-style '/folder' absolute paths without a drive letter."""
    if not _is_windows(platform_name):
        return False

    raw = (path_value or "").strip().replace("\\", "/")
    if not raw.startswith("/"):
        return False
    if raw.startswith("//"):
        return False
    if _looks_like_drive_path(raw):
        return False
    if len(raw) >= 3 and raw[1].isalpha() and raw[2] == "/":
        return False
    return True


def normalize_project_import_path(
    raw_path: str,
    *,
    platform_name: str | None = None,
    cwd: Path | None = None,
) -> Path:
    """Normalize imported project path from UI/websocket into an absolute local path."""
    sanitized = (raw_path or "").strip().strip('"').strip("'")
    expanded = os.path.expanduser(sanitized)

    if _is_windows(platform_name):
        normalized = expanded.replace("\\", "/")
        if len(normalized) >= 3 and normalized[0] == "/" and normalized[1].isalpha() and normalized[2] == "/":
            # Support msys/cygwin style input like /c/Users/name/project.
            expanded = f"{normalized[1]}:{normalized[2:]}"
        elif is_windows_root_relative_path(normalized, platform_name=platform_name):
            drive = (cwd or Path.cwd()).drive
            if drive:
                expanded = f"{drive}{normalized}"

    return Path(expanded).resolve()


def build_missing_path_hint(
    original_path: str,
    *,
    platform_name: str | None = None,
) -> str | None:
    if is_windows_root_relative_path(original_path, platform_name=platform_name):
        return (
            "检测到以 '/' 开头且未带盘符的路径。"
            "如果来源于浏览器拖拽，这通常是虚拟路径。"
            "请粘贴 Windows 绝对路径，例如 C:\\Users\\你\\项目。"
        )
    return None
