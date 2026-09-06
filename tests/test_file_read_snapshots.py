from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

import backend.tools.read_file as read_module
from backend.artifact.store import ArtifactStore
from backend.atomic_io import canonical_file_path_key
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.edit_file import EditFileTool
from backend.tools.file_tools_common import content_hash
from backend.tools.read_file import ReadFileTool
from backend.workspace.file_state_cache import FileStateCache


def _reader(tmp_path, monkeypatch):
    cache = FileStateCache()
    monkeypatch.setattr(read_module, "get_global_file_cache", lambda: cache)
    context = ToolExecutionContext(permission=PermissionContext(mode="bypass"), workspace_root=tmp_path)
    return ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts")), context, cache


@pytest.mark.parametrize("focused", [False, True])
def test_file_replaced_after_read_keeps_its_observed_hash_and_refreshes_next_read(tmp_path, monkeypatch, focused):
    path = tmp_path / "sample.txt"
    old = "before\noriginal tail\n"
    new = "after\na different tail\n"
    path.write_bytes(old.encode())
    reader, context, cache = _reader(tmp_path, monkeypatch)
    original_open = Path.open
    changed = False

    class ReplaceAfterRead:
        def __init__(self, handle):
            self.handle = handle

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __iter__(self):
            return iter(self.handle)

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            nonlocal changed
            result = self.handle.__exit__(*args)
            if not changed:
                changed = True
                path.write_bytes(new.encode())
            return result

    def racing_open(target, mode="r", *args, **kwargs):
        handle = original_open(target, mode, *args, **kwargs)
        return ReplaceAfterRead(handle) if target == path and mode.startswith("r") else handle

    monkeypatch.setattr(Path, "open", racing_open)
    args = {"file_path": "sample.txt", **({"start_line": 1, "end_line": 1} if focused else {})}
    first = asyncio.run(reader.execute(args, context))
    key = canonical_file_path_key(path)
    assert not first.is_error, first.content
    assert "before" in first.content
    assert context.metadata["_read_file_hashes"][key] == content_hash(old)
    assert cache.get(path) is None

    stale_edit = asyncio.run(EditFileTool().execute({
        "file_path": "sample.txt", "old_string": "after", "new_string": "overwritten",
        "expected_hash": context.metadata["_read_file_hashes"][key],
    }, context))
    assert stale_edit.is_error
    assert path.read_bytes() == new.encode()

    second = asyncio.run(reader.execute(args, context))
    assert not second.is_error, second.content
    assert "after" in second.content
    assert context.metadata["_read_file_hashes"][key] == content_hash(new)
    assert cache.get(path).content == new


def test_atomic_replacement_invalidates_cache_even_with_same_size_and_mtime(tmp_path, monkeypatch):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"before")
    reader, context, cache = _reader(tmp_path, monkeypatch)
    asyncio.run(reader.execute({"file_path": "sample.txt"}, context))
    observed = path.stat()
    replacement = tmp_path / "replacement.txt"
    replacement.write_bytes(b"after!")
    os.utime(replacement, ns=(observed.st_atime_ns, observed.st_mtime_ns))
    replacement.replace(path)

    assert cache.get(path) is None
    result = asyncio.run(reader.execute({"file_path": "sample.txt"}, context))
    assert "after!" in result.content


@pytest.mark.parametrize("suffix", [".png", ".pdf"])
def test_line_range_does_not_bypass_non_text_size_limit(tmp_path, monkeypatch, suffix):
    path = tmp_path / f"oversized{suffix}"
    path.write_bytes(b"x" * 64)
    monkeypatch.setattr(read_module, "MAX_FILE_READ_BYTES", 32)
    reader, context, _ = _reader(tmp_path, monkeypatch)
    result = asyncio.run(reader.execute({"file_path": path.name, "start_line": 1, "end_line": 1}, context))
    assert result.is_error
    assert "too large" in result.content
    assert not result.images


def test_focused_snapshot_keeps_unicode_separators_within_the_same_file_line(tmp_path, monkeypatch):
    content = "first\u2028still first\nsecond\n"
    path = tmp_path / "sample.txt"
    path.write_bytes(content.encode())
    reader, context, _ = _reader(tmp_path, monkeypatch)
    result = asyncio.run(reader.execute({"file_path": path.name, "start_line": 1, "end_line": 1}, context))
    assert "still first" in result.content
    assert "second" not in result.content
    assert context.metadata["_read_file_hashes"][canonical_file_path_key(path)] == content_hash(content)


@pytest.mark.parametrize("large", [False, True])
def test_readable_range_without_a_complete_utf8_snapshot_has_no_edit_hash(tmp_path, monkeypatch, large):
    path = tmp_path / "sample.txt"
    path.write_bytes(b"first\n" + b"x" * 9000 + b"\xff")
    if large:
        monkeypatch.setattr(read_module, "MAX_FILE_READ_BYTES", 32)
    reader, context, cache = _reader(tmp_path, monkeypatch)
    result = asyncio.run(reader.execute({"file_path": path.name, "start_line": 1, "end_line": 1}, context))
    assert not result.is_error, result.content
    assert "first" in result.content
    assert "range only" in result.content
    assert canonical_file_path_key(path) not in context.metadata.get("_read_file_hashes", {})
    assert cache.get(path) is None
