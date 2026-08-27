from __future__ import annotations

import os
import hashlib
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from filelock import FileLock


@dataclass
class _MutationLockEntry:
    lock: threading.RLock
    process_lock: FileLock
    users: int = 0


_MUTATION_LOCKS_GUARD = threading.Lock()
_MUTATION_LOCKS: dict[str, _MutationLockEntry] = {}
_MUTATION_LOCK_ROOT = Path(tempfile.gettempdir()) / "minicode-mutation-locks"
_ATOMIC_REPLACE_ATTEMPTS = 5


def canonical_file_path_key(path: Path | str) -> str:
    """Return the canonical identity for path-keyed mutable file state.

    ``resolve(strict=False)`` gives an existing file and any symlink aliases the
    same queue, matching MiniCode's mutation-queue contract. ``normcase`` keeps POSIX
    paths case-sensitive while treating Windows aliases case-insensitively.
    """
    candidate = Path(path).expanduser().resolve(strict=False)
    return os.path.normcase(os.path.normpath(str(candidate)))


def canonical_path_mapping_key(mapping: dict[str, Any], path: Path | str) -> str:
    """Return ``path``'s canonical key and migrate one legacy alias in-place.

    Older persisted sessions keyed read hashes with ``str(Path.resolve())``.
    On Windows that spelling can differ only by case from the canonical key;
    migration keeps those sessions editable after the identity fix.
    """
    key = canonical_file_path_key(path)
    if key in mapping:
        return key
    for stored_key in list(mapping):
        try:
            matches = canonical_file_path_key(stored_key) == key
        except (OSError, RuntimeError, ValueError):
            matches = False
        if matches:
            mapping[key] = mapping.pop(stored_key)
            break
    return key


@contextmanager
def file_mutation_locks(paths: Iterable[Path]) -> Iterator[None]:
    """Serialize mutations to the same files while allowing unrelated writes.

    Multiple paths are acquired in canonical sort order so a rename or
    multi-file patch cannot deadlock another overlapping operation. Registry
    entries are reference-counted and removed after the final waiter leaves.
    The locks are re-entrant because an API or tool may call a lower-level
    mutation helper while already holding the same file lock.  The lock also
    spans separate MiniCode runtime processes: process-local ``RLock`` alone
    would still allow two desktop/backend instances to lose a read-modify-write
    update.
    """
    keys = sorted({canonical_file_path_key(path) for path in paths})
    entries: list[tuple[str, _MutationLockEntry]] = []
    with _MUTATION_LOCKS_GUARD:
        for key in keys:
            entry = _MUTATION_LOCKS.get(key)
            if entry is None:
                _MUTATION_LOCK_ROOT.mkdir(parents=True, exist_ok=True)
                lock_name = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()
                entry = _MutationLockEntry(
                    lock=threading.RLock(),
                    process_lock=FileLock(str(_MUTATION_LOCK_ROOT / f"{lock_name}.lock"), timeout=60),
                )
                _MUTATION_LOCKS[key] = entry
            entry.users += 1
            entries.append((key, entry))

    acquired: list[_MutationLockEntry] = []
    process_acquired: list[_MutationLockEntry] = []
    try:
        for _key, entry in entries:
            entry.lock.acquire()
            acquired.append(entry)
        for _key, entry in entries:
            entry.process_lock.acquire()
            process_acquired.append(entry)
        yield
    finally:
        for entry in reversed(process_acquired):
            entry.process_lock.release()
        for entry in reversed(acquired):
            entry.lock.release()
        with _MUTATION_LOCKS_GUARD:
            for key, entry in entries:
                entry.users -= 1
                if entry.users == 0 and _MUTATION_LOCKS.get(key) is entry:
                    _MUTATION_LOCKS.pop(key, None)


def _fsync_parent(path: Path) -> None:
    """Persist a rename on platforms that allow directory handles."""
    try:
        fd = os.open(path.parent, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _replace_with_retry(source: Path, target: Path) -> None:
    """Replace a destination while tolerating transient Windows sharing locks.

    Editors, antivirus scanners, and file indexers can briefly keep the
    destination open even after MiniCode has acquired its own mutation lock.
    ``Path.replace`` delegates to the platform's atomic replace primitive and
    is deliberately used here so the retry boundary remains testable on
    Windows as well as on POSIX.
    """

    last_error: PermissionError | None = None
    for attempt in range(_ATOMIC_REPLACE_ATTEMPTS):
        try:
            source.replace(target)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 >= _ATOMIC_REPLACE_ATTEMPTS:
                raise
            time.sleep(0.05 * (attempt + 1))
    if last_error is not None:  # pragma: no cover - loop always returns/raises
        raise last_error


def atomic_write_bytes(path: Path, content: bytes, *, overwrite: bool = True) -> None:
    """Publish bytes under the shared cross-process mutation lock."""
    with file_mutation_locks([path]):
        _atomic_write_bytes_unlocked(path, content, overwrite=overwrite)


def _atomic_write_bytes_unlocked(path: Path, content: bytes, *, overwrite: bool = True) -> None:
    """Publish bytes atomically, optionally refusing an existing target.

    ``overwrite=False`` is the create-only primitive used by UI file creation.
    A check followed by ``os.replace`` is not sufficient: another process can
    create the target between those two operations.  Publishing the prepared
    temporary inode with a hard link gives the destination an atomic
    no-overwrite claim on POSIX and Windows; a pre-existing destination causes
    ``FileExistsError`` and is never replaced.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode: int | None = None
    try:
        existing_mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        pass

    # ``mkstemp`` always creates mode 0600, which silently turns ordinary new
    # workspace files private after the atomic replace. Open with the regular
    # 0666 creation mode instead (the process umask still applies), then restore
    # the exact mode for an existing target as MiniCode does.
    temp_name = ""
    fd = -1
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    for _ in range(100):
        candidate = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            fd = os.open(candidate, flags, 0o666)
            temp_name = str(candidate)
            break
        except FileExistsError:
            continue
    if fd < 0:
        raise FileExistsError(f"Unable to allocate an atomic-write temporary file for {path}")

    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            if existing_mode is not None:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), existing_mode)
                else:
                    os.chmod(temp_name, existing_mode)
            os.fsync(handle.fileno())
        if overwrite:
            _replace_with_retry(Path(temp_name), path)
        else:
            try:
                os.link(temp_name, path)
            except FileExistsError:
                raise
            else:
                os.unlink(temp_name)
        temp_name = ""
        _fsync_parent(path)
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def atomic_write_text(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    overwrite: bool = True,
) -> None:
    """Publish text under the shared cross-process mutation lock."""
    with file_mutation_locks([path]):
        _atomic_write_text_unlocked(path, content, encoding=encoding, overwrite=overwrite)


def _atomic_write_text_unlocked(
    path: Path,
    content: str,
    *,
    encoding: str = "utf-8",
    overwrite: bool = True,
) -> None:
    path = Path(path)
    line_ending = "\n"
    if path.exists():
        try:
            with path.open("rb") as handle:
                sample = handle.read(4096)
        except OSError:
            sample = b""
        crlf_count = sample.count(b"\r\n")
        bare_lf_count = sample.count(b"\n") - crlf_count
        if crlf_count > bare_lf_count:
            line_ending = "\r\n"
    if line_ending == "\r\n":
        content = content.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    _atomic_write_bytes_unlocked(path, content.encode(encoding), overwrite=overwrite)
