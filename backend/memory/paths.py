"""Memory paths must not traverse filesystem links."""

from __future__ import annotations

import os
import stat
from pathlib import Path


class MemoryBackendError(RuntimeError):
    pass


def is_link(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(metadata.st_mode) or (
        os.name == "nt"
        and metadata.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    )


def resolve_memory_path(root: Path, relative_path: Path | str = "") -> Path:
    """Check existing components without following links or requiring creation."""

    relative = Path(relative_path)
    if relative.anchor or ".." in relative.parts:
        raise MemoryBackendError(
            f"path '{relative_path}' must stay within the memories root"
        )
    target = root / relative
    absolute_target = target.absolute()
    for component in (*reversed(absolute_target.parents), absolute_target):
        if is_link(component):
            raise MemoryBackendError(
                f"memory path '{component}' must not be a symlink or junction"
            )
        if component != absolute_target and component.exists() and not component.is_dir():
            raise MemoryBackendError(
                f"path '{relative_path}' traverses through a non-directory path component"
            )
    return target
