"""Source-style ownership of Bubblewrap's synthetic mount targets.

Private tmpfs/bind-data mounts supply the sandbox contents. Host targets are
held until the last actual child exits, never removed at namespace readiness.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import psutil
from filelock import FileLock


MOUNT_TARGET_REGISTRY_ROOT = Path(tempfile.gettempdir()) / "minicode-bwrap-mount-targets"


def _identity(value: os.stat_result) -> list[int]:
    return [value.st_dev, value.st_ino]


def _empty_kind(path: Path, value: os.stat_result) -> str | None:
    if stat.S_ISREG(value.st_mode) and value.st_size == 0:
        return "file"
    if stat.S_ISDIR(value.st_mode) and not any(path.iterdir()):
        return "directory"
    return None


def _stat(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _prune_dead_owners(directory: Path) -> list[Path]:
    owners = []
    for marker in directory.glob("owner-*"):
        pid = int(marker.name.split("-", 2)[1])
        if psutil.pid_exists(pid):
            owners.append(marker)
        else:
            marker.unlink(missing_ok=True)
    return owners


class BubblewrapMountTargets:
    """One command's registrations, shared with overlapping command owners."""

    def __init__(self) -> None:
        self.registry_root = MOUNT_TARGET_REGISTRY_ROOT
        self.token = f"owner-{os.getpid()}-{uuid4().hex}"
        self.masks: dict[Path, str] = {}
        self._registrations: list[Path] = []
        self._preserved_files: list[BinaryIO] = []

    def bind_data_descriptor(self) -> int:
        source = open(os.devnull, "rb")
        self._preserved_files.append(source)
        return source.fileno()

    @property
    def preserved_fds(self) -> tuple[int, ...]:
        return tuple(source.fileno() for source in self._preserved_files)

    def acquire(self, path: Path, *, kind: str, protected_metadata: bool) -> bool:
        path = path.expanduser().absolute()
        if path in self.masks:
            return True
        self.registry_root.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(os.fsencode(path)).hexdigest()
        directory = self.registry_root / key
        with FileLock(self.registry_root / "lock"):
            directory.mkdir(exist_ok=True)
            _prune_dead_owners(directory)
            state_path = directory / "target.json"
            state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else None
            value = _stat(path)
            if state is not None and value is not None and state["identity"] == _identity(value):
                selected_kind = state["kind"]
            elif value is not None:
                # Source preserves real pre-existing empty metadata inodes.
                # Other real paths keep their ordinary read-only bind.
                selected_kind = _empty_kind(path, value) if protected_metadata else None
                if selected_kind is None:
                    if not any(directory.iterdir()):
                        directory.rmdir()
                    return False
                state = {"path": str(path), "kind": selected_kind, "identity": _identity(value), "synthetic": False}
            else:
                selected_kind = kind
                if kind == "directory":
                    path.mkdir()
                else:
                    path.touch(exist_ok=False)
                state = {"path": str(path), "kind": kind, "identity": _identity(path.lstat()), "synthetic": True}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            (directory / self.token).write_text("synthetic" if state["synthetic"] else "existing", encoding="utf-8")
            self._registrations.append(directory)
            self.masks[path] = selected_kind
        return True

    def close(self) -> None:
        while self._registrations:
            directory = self._registrations[-1]
            with FileLock(self.registry_root / "lock"):
                (directory / self.token).unlink(missing_ok=True)
                if not _prune_dead_owners(directory):
                    state_path = directory / "target.json"
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    path = Path(state["path"])
                    value = _stat(path)
                    if state["synthetic"] and value is not None and state["identity"] == _identity(value):
                        if state["kind"] == "file" and stat.S_ISREG(value.st_mode) and value.st_size == 0:
                            path.unlink()
                        elif state["kind"] == "directory" and stat.S_ISDIR(value.st_mode):
                            try:
                                path.rmdir()
                            except OSError as error:
                                if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                                    raise
                    state_path.unlink()
                    directory.rmdir()
            self._registrations.pop()
        self.masks.clear()
        for source in self._preserved_files:
            source.close()
        self._preserved_files.clear()
