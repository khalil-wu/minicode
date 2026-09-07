import os
from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.workspace.service import WorkspaceService


def test_workspace_read_metadata_follows_the_opened_file(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    replacement = tmp_path / "replacement.txt"
    target.write_bytes(b"old\n")
    expected = "中文新快照\r\n"
    replacement.write_bytes(expected.encode("utf-8"))
    os.utime(target, (1_700_000_000, 1_700_000_000))
    os.utime(replacement, (1_700_000_100, 1_700_000_100))
    original_open = Path.open

    def replacing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            replacement.replace(target)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replacing_open)
    service = WorkspaceService(lambda: tmp_path)
    snapshot = service.read_file("sample.txt")

    assert snapshot.content == expected
    assert snapshot.content_hash == service.content_hash(expected)
    assert snapshot.size_bytes == len(expected.encode("utf-8"))
    assert snapshot.modified_at == service.iso_timestamp(1_700_000_100)


@pytest.mark.skipif(os.name == "nt", reason="Windows does not replace this open file handle")
def test_workspace_read_does_not_stat_a_replacement_after_open(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    replacement = tmp_path / "replacement.txt"
    target.write_bytes(b"opened snapshot\r\n")
    replacement.write_bytes(b"replacement")
    os.utime(target, (1_700_000_000, 1_700_000_000))
    os.utime(replacement, (1_700_000_100, 1_700_000_100))
    original_open = Path.open

    def replacing_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        if path == target and mode == "rb":
            replacement.replace(target)
        return handle

    monkeypatch.setattr(Path, "open", replacing_open)
    service = WorkspaceService(lambda: tmp_path)
    snapshot = service.read_file("sample.txt")

    assert snapshot.content == "opened snapshot\r\n"
    assert snapshot.size_bytes == 17
    assert snapshot.modified_at == service.iso_timestamp(1_700_000_000)
    assert target.stat().st_size == 11


@pytest.mark.parametrize("operation", ["read_file", "preview_file"])
@pytest.mark.parametrize("limit", [16, 64])
def test_workspace_read_is_bounded_even_when_the_file_grows_after_open(tmp_path, monkeypatch, limit, operation):
    target = tmp_path / "sample.txt"
    target.write_bytes(b"old")
    original_open = Path.open
    read_sizes = []

    class GrowingFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            read_sizes.append(size)
            with original_open(target, "ab") as writer:
                writer.write(b"longer" * limit)
            return self.handle.read(size)

    def growing_open(path, mode="r", *args, **kwargs):
        handle = original_open(path, mode, *args, **kwargs)
        return GrowingFile(handle) if path == target and mode == "rb" else handle

    monkeypatch.setattr(Path, "open", growing_open)
    monkeypatch.setattr("backend.workspace.service.WORKSPACE_MAX_PREVIEW_BYTES", limit)
    service = WorkspaceService(lambda: tmp_path, max_file_bytes=limit)

    with pytest.raises(HTTPException) as failure:
        getattr(service, operation)("sample.txt")

    assert failure.value.status_code == 413
    assert "too large" in failure.value.detail
    assert read_sizes == [limit + 1]


def test_workspace_preview_size_matches_the_bytes_sent_to_the_parser(tmp_path, monkeypatch):
    target = tmp_path / "sample.txt"
    replacement = tmp_path / "replacement.txt"
    target.write_bytes(b"old\n")
    expected = "中文新快照\r\n"
    replacement.write_bytes(expected.encode("utf-8"))
    original_open = Path.open

    def replacing_open(path, mode="r", *args, **kwargs):
        if path == target and mode == "rb":
            replacement.replace(target)
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", replacing_open)
    service = WorkspaceService(lambda: tmp_path)

    snapshot = service.preview_file("sample.txt")

    assert snapshot["content"] == "中文新快照"
    assert snapshot["size_bytes"] == len(expected.encode("utf-8"))


@pytest.mark.parametrize("content", [b"", b"at limit", "中文\r\n".encode("utf-8")], ids=["empty", "ascii", "crlf-unicode"])
def test_workspace_read_accepts_exact_byte_limit(tmp_path, content):
    (tmp_path / "sample.txt").write_bytes(content)
    service = WorkspaceService(lambda: tmp_path, max_file_bytes=len(content))

    snapshot = service.read_file("sample.txt")

    assert snapshot.content.encode("utf-8") == content
    assert snapshot.size_bytes == len(content)
    assert snapshot.content_hash == service.content_hash(snapshot.content)


@pytest.mark.parametrize("limit, status", [(16, 400), (2, 413)])
def test_workspace_read_distinguishes_encoding_and_size_errors(tmp_path, limit, status):
    (tmp_path / "sample.txt").write_bytes(b"\xff\xfe\xfd\xfc")
    service = WorkspaceService(lambda: tmp_path, max_file_bytes=limit)

    with pytest.raises(HTTPException) as failure:
        service.read_file("sample.txt")

    assert failure.value.status_code == status
