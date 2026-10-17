"""Changes metadata and exact content versions through the real WS consumer."""
from __future__ import annotations

import base64
from pathlib import Path

import pytest

from backend.services.workspace_api_service import capture_workspace_execution
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.tests.test_executor_git_changes_ws import request, review_action, seed
from backend.workspace.executor_service import ExecutorWorkspaceService


async def git(server, *args):
    async with capture_workspace_execution(server.session, server.params["conversation_id"],
            environment_id=server.params["environment_id"]) as context:
        async with ExecutorWorkspaceService(context) as service:
            result = await service.run(["git", *args])
            assert result.returncode == 0, result.stderr
            return result.stdout.decode().strip()


async def contents(server, event, *items, **fields):
    result = await request(server, "diff.git_file_contents", expected="command.result",
        source=event["source"], snapshot_generation=event["snapshot_generation"], requests=list(items), **fields)
    assert result["level"] == "success", result
    data = result["data"]
    assert data["environment_id"] == server.params["environment_id"]
    assert data["cwd_uri"] == server.host.workspace.as_uri()
    return data


def item(path, side="new", format="text"):
    return {"path": path, "side": side, "format": format}


@pytest.mark.asyncio
async def test_ws_whitespace_numstat_full_index_rename_and_untracked_metadata_are_one_contract(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"real change\n")
    (root / "wild1.txt").write_bytes(b"  before  \n")
    (root / "new 中文.txt").write_bytes(b"first\nsecond\n")
    await git(server, "mv", "rename 元.txt", "rename 后.txt")
    plain = await request(server, "diff.git_working_tree", hide_whitespace=False)
    hidden = await request(server, "diff.git_working_tree", hide_whitespace=True)
    assert plain["type"] == hidden["type"] == "diff.git_working_tree", (plain, hidden)
    assert {file["path"] for file in plain["files"]} == {"tracked.txt", "wild1.txt"}
    assert [file["path"] for file in hidden["files"]] == ["tracked.txt"]
    assert hidden["hide_whitespace"] is True and hidden["source"] == "unstaged"
    assert hidden["snapshot_generation"] != plain["snapshot_generation"]
    added = hidden["untracked_files"]
    assert [file["path"] for file in added] == ["new 中文.txt"]
    assert added[0]["status"] == "A" and added[0]["old_oid"] is None and added[0]["additions"] == 2
    all_files = [*hidden["files"], *added]
    assert hidden["total_additions"] == sum(file["additions"] for file in all_files) == 3
    assert hidden["total_deletions"] == sum(file["deletions"] for file in all_files) == 1
    for file in all_files:
        assert len(file["new_oid"]) in (40, 64) and file["revision"]
        assert file["new_oid"] in file["patch"]
    staged = await request(server, "diff.git_staged", hide_whitespace=True)
    renamed = staged["files"][0]
    assert renamed["path"] == "rename 后.txt" and renamed["old_path"] == "rename 元.txt"
    assert renamed["status"] == "R" and renamed["old_oid"] == renamed["new_oid"]
    assert renamed["old_mode"] == renamed["new_mode"] == "100644"
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
async def test_ws_complete_sides_preserve_bom_crlf_empty_and_unterminated_lines(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"\xef\xbb\xbfindex\r\nlast")
    stage = await review_action(server, "diff.git_stage_file", path="tracked.txt")
    assert stage["level"] == "success", stage
    staged = await request(server, "diff.git_staged")
    (root / "tracked.txt").write_bytes(b"\xef\xbb\xbfworking\r\nlast")
    (root / "empty.md").write_bytes(b"")
    working = await request(server, "diff.git_working_tree")
    index = await contents(server, staged, item("tracked.txt", "old"), item("tracked.txt"))
    assert index["results"][0]["lines"] == ["before\n"]
    assert index["results"][1]["lines"] == ["\ufeffindex\r\n", "last"]
    assert all(result["source_kind"] == "object" for result in index["results"])
    live = await contents(server, working, item("tracked.txt", "old"), item("tracked.txt"),
        item("empty.md", "old"), item("empty.md"))
    assert live["results"][0]["lines"] == ["\ufeffindex\r\n", "last"]
    assert live["results"][1]["lines"] == ["\ufeffworking\r\n", "last"]
    assert live["results"][1]["source_kind"] == "disk"
    assert live["results"][2]["lines"] == live["results"][3]["lines"] == []
    assert live["results"][2]["source_kind"] == "absent"


@pytest.mark.asyncio
async def test_ws_binary_preview_reads_head_index_disk_bytes_instead_of_current_file_for_every_side(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    before, index, disk = b"\x89PNG\r\n\x00HEAD", b"\x89PNG\r\n\x00INDEX", b"\x89PNG\r\n\x00DISK"
    (root / "image.png").write_bytes(before)
    await git(server, "add", "image.png")
    await git(server, "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "image")
    (root / "image.png").write_bytes(index)
    staged_action = await review_action(server, "diff.git_stage_file", path="image.png")
    assert staged_action["level"] == "success", staged_action
    staged = await request(server, "diff.git_staged")
    (root / "image.png").write_bytes(disk)
    (root / "preview.pdf").write_bytes(b"%PDF-1.7\n\x00owned PDF bytes")
    working = await request(server, "diff.git_working_tree")
    old_index = await contents(server, staged, item("image.png", "old", "binary"), item("image.png", "new", "binary"))
    assert [base64.b64decode(result["data_base64"]) for result in old_index["results"]] == [before, index]
    assert all(result["media_type"] == "image/png" for result in old_index["results"])
    index_disk = await contents(server, working, item("image.png", "old", "binary"), item("image.png", "new", "binary"),
        item("preview.pdf", "new", "binary"))
    assert [base64.b64decode(result["data_base64"]) for result in index_disk["results"]] == [index, disk, (root / "preview.pdf").read_bytes()]
    assert index_disk["results"][2]["media_type"] == "application/pdf"


@pytest.mark.asyncio
async def test_ws_old_missing_object_never_falls_back_and_stale_never_returns_new_disk(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"captured\n")
    working = await request(server, "diff.git_working_tree")
    oid = working["files"][0]["old_oid"]
    object_path = root / ".git/objects" / oid[:2] / oid[2:]
    object_path.chmod(0o600)
    object_path.unlink()
    missing = await contents(server, working, item("tracked.txt", "old"))
    assert missing["results"][0]["error"]["type"] == "not-found"
    (root / "tracked.txt").write_bytes(b"newer disk must not be returned\n")
    stale = await contents(server, working, item("tracked.txt"))
    assert stale["status"] == "stale-snapshot"
    assert stale["results"][0]["error"]["type"] == "stale-snapshot"


@pytest.mark.asyncio
async def test_ws_object_limit_and_generation_retirement_are_explicit(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"new content larger than limit\n")
    first = await request(server, "diff.git_working_tree")
    large = await contents(server, first, item("tracked.txt", "old"), item("tracked.txt"), max_object_bytes=3)
    assert [result["error"]["type"] for result in large["results"]] == ["too-large", "too-large"]
    assert all(result["error"]["limit_bytes"] == 3 for result in large["results"])
    second = await request(server, "diff.git_working_tree")
    assert first["snapshot_generation"] != second["snapshot_generation"]
    old_generation = await contents(server, first, item("tracked.txt"))
    assert old_generation["status"] == "stale-snapshot"
    current = await contents(server, second, item("tracked.txt"))
    assert current["results"][0]["lines"] == ["new content larger than limit\n"]


@pytest.mark.asyncio
async def test_ws_untracked_embedded_repository_is_one_gitlink_not_recursive_children(editor_server):
    server = editor_server
    await seed(server)
    name = "embedded repo"
    await git(server, "init", "-q", name)
    (server.host.workspace / name / "private-child.txt").write_bytes(b"nested repository contents\n")
    await git(server, "-C", name, "add", "private-child.txt")
    await git(server, "-C", name, "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid",
        "commit", "--no-gpg-sign", "-qm", "embedded")
    oid = await git(server, "-C", name, "rev-parse", "HEAD")
    working = await request(server, "diff.git_working_tree")
    assert working["untracked"] == [f"{name}/"]
    row, = working["untracked_files"]
    assert row["path"] == f"{name}/" and row["status"] == "A"
    assert row["new_mode"] == "160000" and row["new_oid"] == oid
    assert row["old_oid"] is None and row["old_mode"] == "000000"
    assert row["revision"] == f"untracked:160000:{oid}"
    assert row["additions"] == working["total_additions"] == 1
    assert row["deletions"] == working["total_deletions"] == 0
    assert f"+Subproject commit {oid}\n" in row["patch"]
    assert "private-child" not in row["patch"] and "nested repository contents" not in row["patch"]
