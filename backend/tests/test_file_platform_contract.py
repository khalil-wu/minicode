from __future__ import annotations

import threading
from pathlib import Path
import os
from types import SimpleNamespace

import pytest

from backend.atomic_io import atomic_write_text, file_mutation_locks
from backend.tools.path_resolution import (
    PathTraversalError,
    _resolve_path,
    windows_path_safety_reason,
)


def test_atomic_write_text_preserves_existing_crlf(tmp_path: Path) -> None:
    path = tmp_path / "windows.txt"
    path.write_bytes(b"first\r\nsecond\r\n")

    atomic_write_text(path, "first\nchanged\n")

    assert path.read_bytes() == b"first\r\nchanged\r\n"


def test_atomic_write_create_only_does_not_overwrite_existing_content(tmp_path: Path) -> None:
    path = tmp_path / "create-only.txt"
    path.write_text("original", encoding="utf-8")

    with pytest.raises(FileExistsError):
        atomic_write_text(path, "replacement", overwrite=False)

    assert path.read_text(encoding="utf-8") == "original"


def test_atomic_write_create_only_has_one_concurrent_winner(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-create.txt"
    barrier = threading.Barrier(12)
    outcomes: list[tuple[str, bool]] = []
    outcomes_lock = threading.Lock()

    def create(index: int) -> None:
        value = f"writer-{index}"
        barrier.wait()
        try:
            atomic_write_text(path, value, overwrite=False)
        except FileExistsError:
            won = False
        else:
            won = True
        with outcomes_lock:
            outcomes.append((value, won))

    threads = [threading.Thread(target=create, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    winners = [value for value, won in outcomes if won]
    assert len(winners) == 1
    assert path.read_text(encoding="utf-8") == winners[0]


def test_shared_mutation_lock_protects_read_modify_write_sequence(tmp_path: Path) -> None:
    path = tmp_path / "counter.txt"
    path.write_text("0", encoding="utf-8")

    def increment() -> None:
        with file_mutation_locks([path]):
            current = int(path.read_text(encoding="utf-8"))
            atomic_write_text(path, str(current + 1))

    threads = [threading.Thread(target=increment) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert path.read_text(encoding="utf-8") == "32"


@pytest.mark.parametrize("name", ["CON", "nul.txt", "folder/COM1.log", "LPT9"])
def test_resolve_path_rejects_windows_reserved_devices(tmp_path: Path, name: str) -> None:
    context = SimpleNamespace(workspace_root=str(tmp_path), permission=None)

    with pytest.raises(PathTraversalError, match="Windows device paths"):
        _resolve_path(name, context)


@pytest.mark.parametrize(
    "name",
    [
        "folder/GIT~1/file.txt",
        r"\\?\C:\workspace\file.txt",
        "folder/.../file.txt",
        "folder/file.txt.",
        "folder/file.txt ",
        "folder/file.txt.CON",
        r"\\server\share\file.txt",
        "//server/share/file.txt",
    ],
)
def test_windows_suspicious_path_spellings_are_rejected(name: str) -> None:
    assert windows_path_safety_reason(name)


@pytest.mark.skipif(os.name != "nt", reason="ADS syntax is interpreted by Windows")
def test_windows_alternate_data_stream_path_is_rejected() -> None:
    assert windows_path_safety_reason("folder/file.txt:secret")
