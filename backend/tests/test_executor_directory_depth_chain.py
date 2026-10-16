"""Source walk-depth semantics and real directory consumers on local/Source peers."""
from __future__ import annotations

import json

import pytest

from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_file_chain import accepted
from backend.tests.test_executor_tool_admission import executor_host
from backend.tools.list_files import ListFilesTool
from backend.tools.search_tools import GlobFilesTool, GrepFilesTool
from backend.workspace.executor_paths import ExecutorPath
from backend.workspace.executor_service import ExecutorWorkspaceService
from backend.workspace.executor_tool_io import executor_candidate_files


@pytest.mark.asyncio
async def test_actual_walk_zero_one_two_and_read_directory_keep_source_depth_contract(executor_host, request):
    host = executor_host
    root = host.workspace / "depth-contract"
    (root / "visible/deeper").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "root.txt").write_text("root")
    (root / "visible/child.txt").write_text("child")
    (root / "visible/deeper/leaf.txt").write_text("leaf")
    (root / ".git/HEAD").write_text("owned metadata")
    filesystem = host.environments.primary().environment.filesystem
    direct = await filesystem.read_directory(root.as_uri(), sandbox=None)
    assert {entry["fileName"] for entry in direct} == {".git", "visible", "root.txt"}
    expected = {".git", "visible", "root.txt"}
    observed = {}
    for depth in (0, 1, 2):
        if depth == 1:
            expected |= {".git/HEAD", "visible/child.txt", "visible/deeper"}
        elif depth == 2:
            expected |= {"visible/deeper/leaf.txt"}
        walked = await filesystem.walk(root.as_uri(), {"maxDepth": depth, "maxDirectories": 100,
            "maxEntries": 100, "followDirectorySymlinks": False}, sandbox=None)
        assert not walked["truncated"] and not walked["errors"], walked
        paths = [ExecutorPath.parse(item["path"]).relative_to(ExecutorPath.parse(root.as_uri())).as_posix()
            for item in walked["entries"]]
        assert set(paths) == expected and len(paths) == len(expected), walked
        observed[str(depth)] = sorted(paths)
    request.node.user_properties.append(("actual_executor_walk_depth_contract", json.dumps({
        "peer": host.kind, "depth_zero_is_direct": True, "read_directory_is_direct": True, "paths": observed})))


@pytest.mark.asyncio
async def test_actual_http_tree_keeps_git_and_ordinary_descendants_beneath_requested_directory(editor_server, request):
    server = editor_server
    root = server.host.workspace / "owned-tree"
    (root / ".git/hooks").mkdir(parents=True)
    (root / ".git/HEAD").write_text("owned metadata")
    (root / "folder/deeper").mkdir(parents=True)
    (root / "folder/child.txt").write_text("child")
    (root / "folder/deeper/leaf.txt").write_text("leaf")
    (root / "ignored-only/.git").mkdir(parents=True)
    (root / "ignored-only/.git/HEAD").write_text("hidden child")
    (root / "empty").mkdir()
    (root / "top.txt").write_text("top")

    async def tree(path):
        response = await server.client.get("/api/workspace/tree", params={**server.params, "path": path})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["environment_id"] == server.params["environment_id"]
        assert body["conversation_id"] == server.params["conversation_id"]
        assert body["session_id"] == server.session.session_id
        return body

    top = await tree("owned-tree")
    assert [entry["name"] for entry in top["entries"]] == ["empty", "folder", "ignored-only", "top.txt"]
    assert {entry["path"] for entry in top["entries"]} == {
        "owned-tree/empty", "owned-tree/folder", "owned-tree/ignored-only", "owned-tree/top.txt"}
    children = {entry["name"]: entry["has_children"] for entry in top["entries"]}
    assert children["folder"] and not children["empty"] and not children["ignored-only"]
    assert all(ExecutorPath.parse(entry["path_uri"]).parent == ExecutorPath.parse(root.as_uri()) for entry in top["entries"])
    folder = await tree("owned-tree/folder")
    assert [entry["name"] for entry in folder["entries"]] == ["deeper", "child.txt"]
    assert {entry["path"] for entry in folder["entries"]} == {"owned-tree/folder/deeper", "owned-tree/folder/child.txt"}
    deep = await tree("owned-tree/folder/deeper")
    assert [entry["path"] for entry in deep["entries"]] == ["owned-tree/folder/deeper/leaf.txt"]
    request.node.user_properties.append(("actual_executor_http_tree_depth", json.dumps({
        "peer": server.host.kind, "top": [entry["path"] for entry in top["entries"]],
        "folder": [entry["path"] for entry in folder["entries"]], "leaf": deep["entries"][0]["path"],
        "original_owner_retained": True, "ignored_only_has_no_children": True})))


@pytest.mark.asyncio
async def test_actual_list_glob_grep_own_recursion_once_and_prune_ignored_subtrees(accepted, request):
    host, context = accepted
    root = host.workspace / "owned-search"
    (root / "src/deep").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "node_modules/pkg").mkdir(parents=True)
    for path in ("top.py", "src/child.py", "src/deep/leaf.py", ".git/secret.py", "node_modules/pkg/dependency.py", "ignored.py"):
        (root / path).write_text("DEPTH_NEEDLE\n")
    (root / ".gitignore").write_text("ignored.py\n")
    listing = await ListFilesTool().execute({"path": "owned-search"}, context)
    assert not listing.is_error, listing.content
    assert "src/" in listing.content.splitlines() and "src/child.py" not in listing.content
    recursive = await ListFilesTool().execute({"path": "owned-search", "recursive": True}, context)
    assert not recursive.is_error, recursive.content
    lines = recursive.content.splitlines()
    assert lines.count("src/deep/leaf.py") == 1 and len(lines) == len(set(lines)), recursive.content
    async with ExecutorWorkspaceService(context) as service:
        candidates = [path.relative_to(service.resolve("owned-search")).as_posix()
            async for path in executor_candidate_files(service, service.resolve("owned-search"))]
    expected = {".gitignore", "top.py", "src/child.py", "src/deep/leaf.py"}
    assert set(candidates) == expected and len(candidates) == len(expected), candidates
    glob = await GlobFilesTool().execute({"path": "owned-search", "pattern": "**/*.py"}, context)
    assert not glob.is_error, glob.content
    for path in expected - {".gitignore"}:
        assert glob.content.splitlines().count(f"- {path}") == 1, glob.content
    assert "secret.py" not in glob.content and "dependency.py" not in glob.content and "ignored.py" not in glob.content
    grep = await GrepFilesTool().execute({"path": "owned-search", "pattern": "DEPTH_NEEDLE", "output_mode": "files_with_matches"}, context)
    assert not grep.is_error, grep.content
    for path in expected - {".gitignore"}:
        assert grep.content.splitlines().count(path) == 1, grep.content
    assert "secret.py" not in grep.content and "dependency.py" not in grep.content and "ignored.py" not in grep.content
    request.node.user_properties.append(("actual_executor_directory_consumers", json.dumps({
        "peer": host.kind, "nonrecursive_list_is_direct": True, "recursive_list_has_no_duplicates": True,
        "search_candidates": sorted(candidates), "glob_and_grep_match_once": True})))
