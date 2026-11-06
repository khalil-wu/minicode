from pathlib import Path

import pytest
from fastapi import HTTPException

from backend.workspace.service import WorkspaceService
from backend.workspace.text_search import SearchBuffer, WorkspaceTextSearchRequest, search_workspace_text


def service(root: Path) -> WorkspaceService:
    return WorkspaceService(get_workspace_root=lambda: root)


def test_unicode_crlf_ranges_capture_groups_and_filters(tmp_path: Path):
    (tmp_path / "src").mkdir()
    source = "😀 foo\r\nFoo foobar foo\r\n"
    (tmp_path / "src/main.ts").write_bytes(source.encode("utf-8"))
    (tmp_path / "src/main.test.ts").write_text("foo", encoding="utf-8")
    result = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="(foo)", regex=True, whole_word=True,
        include=["src/**/*.{ts,tsx}"], exclude=["**/*.test.ts"]))
    assert result["match_count"] == 3
    assert result["files"][0]["content"] == source
    first, second, third = result["files"][0]["matches"]
    assert (first["offset"], first["length"], first["line"], first["column"], first["end_column"]) == (3, 3, 1, 4, 7)
    assert (second["offset"], second["line"], second["column"]) == (8, 2, 1)
    assert (third["offset"], third["column"]) == (19, 12)
    assert first["groups"] == [{"offset": 3, "length": 3}]
    exact = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="Foo", case_sensitive=True))
    assert exact["match_count"] == 1


def test_live_buffers_override_disk_and_unreadable_buffer_does_not_block_other_files(tmp_path: Path):
    (tmp_path / "main.py").write_text("disk value", encoding="utf-8")
    (tmp_path / ".minicode").mkdir()
    (tmp_path / ".minicode/private.json").write_text("hidden value", encoding="utf-8")
    result = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="value", buffers=[
        SearchBuffer(path="main.py", content="😀 unsaved value", original="disk value", content_hash="old-hash"),
        SearchBuffer(path=".minicode/private.json", content="private value", original="hidden value"),
    ]))
    assert [file["path"] for file in result["files"]] == ["main.py"]
    assert result["files"][0]["from_buffer"] is True
    assert result["files"][0]["content_hash"] == "old-hash"
    assert result["issues"][0]["path"] == ".minicode/private.json"


def test_limit_is_explicit_and_regex_error_is_actionable(tmp_path: Path):
    (tmp_path / "many.txt").write_text("foo foo foo", encoding="utf-8")
    result = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="foo", limit=2))
    assert result["truncated"] is True
    assert result["match_count"] == 2
    assert len(result["files"][0]["matches"]) == 2
    with pytest.raises(HTTPException) as error:
        search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="[", regex=True))
    assert error.value.status_code == 400


def test_fixed_gui_config_buffer_uses_the_same_editor_read_permission(tmp_path: Path):
    (tmp_path / ".minicode").mkdir()
    (tmp_path / ".minicode/launch.json").write_text('{"name":"disk"}', encoding="utf-8")
    result = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="preview", buffers=[
        SearchBuffer(path=".minicode/launch.json", content='{"name":"preview"}', original='{"name":"disk"}'),
    ]))
    assert result["match_count"] == 1
    assert result["files"][0]["path"] == ".minicode/launch.json"


def test_files_without_a_match_skip_snapshot_hash_and_utf16_index(tmp_path: Path, monkeypatch):
    from backend.workspace import text_search

    for index in range(20):
        (tmp_path / f"miss_{index}.txt").write_bytes(b"nothing here\n" * 50)
    (tmp_path / "hit.txt").write_bytes(b"a needle\n")
    hashed: list[str] = []
    indexed: list[int] = []
    real_hash, real_index = WorkspaceService.content_hash, text_search._text_index
    monkeypatch.setattr(WorkspaceService, "content_hash", staticmethod(lambda content: hashed.append(content) or real_hash(content)))
    monkeypatch.setattr(text_search, "_text_index", lambda content: indexed.append(len(content)) or real_index(content))
    result = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="needle"))
    assert [file["path"] for file in result["files"]] == ["hit.txt"]
    assert result["files"][0]["content_hash"] == real_hash("a needle\n")
    assert result["files"][0]["matches"][0]["column"] == 3
    assert hashed == ["a needle\n"]
    assert len(indexed) <= 1


def test_read_boundaries_are_still_reported_for_files_without_a_match(tmp_path: Path):
    (tmp_path / "binary.dat").write_bytes(b"\xff\xfe\x00bad")
    (tmp_path / "big.txt").write_text("x" * 64, encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=needle", encoding="utf-8")
    (tmp_path / "ok.txt").write_text("needle", encoding="utf-8")
    small = WorkspaceService(get_workspace_root=lambda: tmp_path, max_file_bytes=32)
    result = search_workspace_text(small, WorkspaceTextSearchRequest(query="needle"))
    assert [file["path"] for file in result["files"]] == ["ok.txt"]
    messages = {issue["path"]: issue["message"] for issue in result["issues"]}
    assert "UTF-8" in messages["binary.dat"]
    assert "too large" in messages["big.txt"]


def test_symlinked_file_outside_workspace_is_never_searched(tmp_path: Path):
    workspace, outside = tmp_path / "ws", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    try:
        (workspace / "link.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("symlink creation is not permitted on this host")
    result = search_workspace_text(service(workspace), WorkspaceTextSearchRequest(query="needle"))
    assert result["files"] == []


def test_file_removed_after_the_walk_is_an_issue_not_a_server_error(tmp_path: Path, monkeypatch):
    from backend.workspace import text_search

    (tmp_path / "gone.txt").write_text("needle", encoding="utf-8")
    real_walk = text_search.iter_search_paths

    def walk_then_delete(root, **kwargs):
        listed = list(real_walk(root, **kwargs))
        (tmp_path / "gone.txt").unlink()
        return iter(listed)

    monkeypatch.setattr(text_search, "iter_search_paths", walk_then_delete)
    result = search_workspace_text(service(tmp_path), WorkspaceTextSearchRequest(query="needle"))
    assert result["files"] == []
    assert result["issues"][0]["path"] == "gone.txt"
