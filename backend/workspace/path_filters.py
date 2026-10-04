from __future__ import annotations

from pathlib import Path
import os


def is_local_filename(name: str) -> bool:
    """A platform filename token without directory or namespace syntax."""
    return (
        name not in {"", ".", ".."}
        and "/" not in name and "\0" not in name
        and (os.name != "nt" or ("\\" not in name and ":" not in name))
    )

_WINDOWS_RESERVED_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def is_windows_reserved_path(path: Path | str) -> bool:
    """Return true when any path segment is a Windows device name.

    Windows treats names such as ``nul`` and ``con.txt`` specially even when
    they appear inside a directory, which can make normal file scans fail.
    """
    parts = Path(path).parts if not isinstance(path, Path) else path.parts
    for part in parts:
        normalized = part.rstrip(" .").split(".", 1)[0].lower()
        if normalized in _WINDOWS_RESERVED_DEVICE_NAMES:
            return True
    return False
