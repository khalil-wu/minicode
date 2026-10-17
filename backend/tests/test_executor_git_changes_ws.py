"""The Changes consumer's real WebSocket commands on local/Source owners."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from uuid import uuid4

import pytest

from backend.environments.errors import ExecutorDisconnected
from backend.services.workspace_api_service import capture_workspace_execution
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_service import ExecutorWorkspaceService


async def request(server, command, *, expected=None, **fields):
    request_id = uuid4().hex
    await server.websocket.send(json.dumps({"type": command, "conversation_id": server.params["conversation_id"],
        "workspace_root": str(server.view), "environment_id": server.params["environment_id"],
        "cwd_uri": server.host.workspace.as_uri(), "request_id": request_id, **fields}))
    async with asyncio.timeout(25):
        while True:
            item = json.loads(await server.websocket.recv())
            details = item.get("data", {}) if item["type"] == "command.result" else item
            if details.get("request_id") != request_id:
                if item["type"] == "command.result" and item.get("command") == command and item.get("level") == "error":
                    return item
                continue
            if item["type"] == (expected or command) or item["type"] == "command.result" and item.get("level") == "error":
                return item


async def review_action(server, command, *, source=None, snapshot=None, expected="command.result", **fields):
    source = source or ("staged" if "unstage" in command else "unstaged")
    if snapshot is None:
        scope = {key: fields[key] for key in ("conversation_id", "workspace_root", "execution_cwd", "cwd_uri", "workspace", "environment_id") if key in fields}
        snapshot = await request(server, "diff.git_staged" if source == "staged" else "diff.git_working_tree",
            source=source, **scope)
        assert snapshot["type"] != "command.result", snapshot
    return await request(server, command, expected=expected, source=source,
        snapshot_generation=snapshot["snapshot_generation"], **fields)


async def seed(server):
    root = server.host.workspace
    for name in ("tracked.txt", "报告 文档.txt", "wild[1].txt", "wild1.txt", "rename 元.txt"):
        (root / name).write_bytes(b"before\n")
    async with capture_workspace_execution(server.session, server.params["conversation_id"],
            environment_id=server.params["environment_id"]) as context:
        async with ExecutorWorkspaceService(context) as service:
            for argv in (["git", "init", "-q"], ["git", "config", "core.autocrlf", "false"], ["git", "add", "--all"],
                    ["git", "-c", "user.name=MiniCode", "-c", "user.email=test@example.invalid", "commit", "--no-gpg-sign", "-qm", "base"]):
                result = await service.run(argv)
                assert result.returncode == 0, result.stderr


def assert_owner(server, item):
    details = item.get("data", {}) if item["type"] == "command.result" else item
    assert details["conversation_id"] == server.params["conversation_id"]
    assert details["environment_id"] == server.params["environment_id"]
    assert details["cwd_uri"] == server.host.workspace.as_uri()
    assert details["path_uri"] == server.host.workspace.as_uri()
    assert details["workspace_root"] in (str(server.view), server.host.workspace.as_uri())


@pytest.mark.asyncio
async def test_ws_changes_working_staged_untracked_and_all_actions_keep_original_executor(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"staged target\n")
    staged = await review_action(server, "diff.git_stage_file", path="tracked.txt")
    assert staged["level"] == "success", staged
    assert_owner(server, staged)
    (root / "tracked.txt").write_bytes(b"working target\n")
    (root / "报告 文档.txt").write_bytes(b"unicode target\n")
    (root / "new file.txt").write_bytes(b"untracked target\n")
    working = await request(server, "diff.git_working_tree")
    assert_owner(server, working)
    assert working["is_git_repo"] is True and working["untracked"] == ["new file.txt"]
    patches = {file["path"]: file["patch"] for file in working["files"]}
    assert "+working target" in patches["tracked.txt"] and "-staged target" in patches["tracked.txt"]
    assert "+unicode target" in patches["报告 文档.txt"]
    index = await request(server, "diff.git_staged")
    assert_owner(server, index)
    assert [file["path"] for file in index["files"]] == ["tracked.txt"]
    assert "+staged target" in index["files"][0]["patch"] and "working target" not in index["files"][0]["patch"]
    for command, fields in (("diff.git_unstage_file", {"path": "tracked.txt"}),
            ("diff.git_stage_all", {}), ("diff.git_unstage_all", {})):
        result = await review_action(server, command, **fields)
        assert result["level"] == "success", json.dumps(result, ensure_ascii=False)
        assert_owner(server, result)
        index = await request(server, "diff.git_staged")
        assert len(index["files"]) == (3 if command == "diff.git_stage_all" else 0), index
    denied = await review_action(server, "diff.git_revert_file", path="tracked.txt")
    assert denied["level"] == "error" and "confirmed=true" in denied["message"]
    assert (root / "tracked.txt").read_bytes() == b"working target\n"
    reverted = await review_action(server, "diff.git_revert_file", path="tracked.txt", confirmed=True)
    assert reverted["level"] == "success", reverted
    assert_owner(server, reverted)
    assert (root / "tracked.txt").read_bytes() == b"before\n"
    assert (server.view / "same.txt").read_text() == "controller must remain untouched\n"
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
async def test_ws_patch_undo_is_confirmed_path_scoped_and_cleans_original_executor_on_git_failure(editor_server, monkeypatch):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    for name in ("wild[1].txt", "wild1.txt", "报告 文档.txt"):
        (root / name).write_bytes(b"after\n")
    working = await request(server, "diff.git_working_tree")
    patch = "".join(file["patch"] for file in working["files"] if file["path"] != "wild1.txt")
    denied = await request(server, "diff.git_revert_patch", expected="command.result", patch=patch)
    assert denied["level"] == "error" and "Confirm undoing" in denied["message"]
    assert (root / "wild[1].txt").read_bytes() == b"after\n"
    result = await request(server, "diff.git_revert_patch", expected="command.result", patch=patch, confirmed=True)
    assert result["level"] == "success", result
    assert_owner(server, result)
    assert (root / "wild[1].txt").read_bytes() == (root / "报告 文档.txt").read_bytes() == b"before\n"
    assert (root / "wild1.txt").read_bytes() == b"after\n"
    failed = await request(server, "diff.git_revert_patch", expected="command.result", patch=patch, confirmed=True)
    assert failed["level"] == "error" and failed["data"]["exit_code"] != 0, failed
    assert "patch does not apply" in failed["data"]["stderr"]
    assert_owner(server, failed)
    assert not list((root / ".git").glob(".minicode-undo-*"))
    assert not list(root.glob(".minicode-undo-*"))
    (root / "rename 元.txt").rename(root / "rename 后.txt")
    rename = 'diff --git "a/rename 元.txt" "b/rename 后.txt"\nsimilarity index 100%\nrename from rename 元.txt\nrename to rename 后.txt\n'
    renamed = await request(server, "diff.git_revert_patch", expected="command.result", patch=rename, confirmed=True)
    assert renamed["level"] == "success", renamed
    assert (root / "rename 元.txt").read_bytes() == b"before\n" and not (root / "rename 后.txt").exists()
    assert not list((root / ".git").glob(".minicode-undo-*"))
    assert not list(root.glob(".minicode-undo-*"))
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
async def test_ws_changes_uri_owner_non_git_and_real_errors_never_read_controller(editor_server, monkeypatch):
    server = editor_server
    empty = await request(server, "diff.git_working_tree", workspace_root=server.host.workspace.as_uri())
    assert empty["is_git_repo"] is False and empty["files"] == empty["untracked"] == [], empty
    assert_owner(server, empty)
    await seed(server)
    (server.host.workspace / "tracked.txt").write_bytes(b"executor only\n")
    snapshot = await request(server, "diff.git_working_tree")
    original = server.host.environments.primary().environment.process.start
    starts = []
    async def refused(argv, cwd, **kwargs):
        starts.append((argv, cwd))
        raise ExecutorDisconnected("owned executor disconnected; do not retry controller")
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", refused)
    for command in ("diff.git_stage_file", "diff.git_working_tree", "diff.git_staged"):
        result = await request(server, command, path="tracked.txt", source="unstaged", snapshot_generation=snapshot["snapshot_generation"])
        assert result["type"] == "command.result" and result["level"] == "error", result
        assert "owned executor disconnected" in result["message"]
        assert_owner(server, result)
    assert starts and all(cwd == server.host.workspace.as_uri() for _, cwd in starts)
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", original)
    bad_uri = await request(server, "diff.git_working_tree", workspace_root=(server.host.workspace / "other").as_uri())
    assert bad_uri["level"] == "error" and "captured execution owner" in bad_uri["message"], bad_uri
    (server.host.workspace / ".git/config").write_text("[broken", encoding="utf-8")
    broken = await request(server, "diff.git_working_tree")
    assert broken["type"] == "command.result" and broken["level"] == "error", broken
    assert broken["data"]["exit_code"] != 0 and "bad config" in broken["data"]["stderr"]
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", (False, True))
async def test_ws_changes_late_reply_keeps_captured_conversation_and_request(editor_server, monkeypatch, fail):
    server = editor_server
    await seed(server)
    (server.host.workspace / "tracked.txt").write_bytes(b"original owner bytes\n")
    original = server.host.environments.primary().environment.process.start
    started, release = asyncio.Event(), asyncio.Event()
    async def delayed(argv, cwd, **kwargs):
        if "diff" in argv:
            started.set()
            await release.wait()
            if fail:
                return await original(["git", "--minicode-owned-invalid-option"], cwd, **kwargs)
        return await original(argv, cwd, **kwargs)
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", delayed)
    pending = asyncio.create_task(request(server, "diff.git_working_tree"))
    await asyncio.wait_for(started.wait(), 15)
    server.session.active_conversation_id = "a-new-reader"
    if server.host.kind == "source" and not fail:
        replacement = server.host.manager.upsert({"id": server.params["environment_id"],
            "program": "minicode-replacement-must-not-start"})
        async def unexpected(*_args, **_kwargs):
            raise AssertionError("A late Changes result must retain its original registration")
        monkeypatch.setattr(replacement.process, "start", unexpected)
    try:
        release.set()
        result = await pending
        assert_owner(server, result)
        if fail:
            assert result["data"]["exit_code"] != 0 and "unknown option" in result["data"]["stderr"], result
        else:
            assert "+original owner bytes" in result["files"][0]["patch"]
    finally:
        server.session.active_conversation_id = server.params["conversation_id"]


@pytest.mark.asyncio
async def test_ws_changes_read_suppresses_real_repository_clean_process_and_diff_helpers(editor_server):
    server = editor_server
    await seed(server)
    root = server.host.workspace
    marker = root / "must-not-run.txt"
    helper = root / "forbidden-helper.sh"
    helper.write_text(f'echo ran > "{marker.as_posix()}"\ncat\n', encoding="utf-8")
    (root / ".gitattributes").write_text("tracked.txt filter=owned diff=owned\n", encoding="utf-8")
    (root / "tracked.txt").write_bytes(b"changed with attributes\n")
    async with capture_workspace_execution(server.session, server.params["conversation_id"],
            environment_id=server.params["environment_id"]) as context:
        async with ExecutorWorkspaceService(context) as service:
            for key in ("filter.owned.clean", "filter.owned.process", "diff.owned.command", "diff.owned.textconv", "core.fsmonitor"):
                result = await service.run(["git", "config", key, f'sh "{helper.as_posix()}"'])
                assert result.returncode == 0, result.stderr
    result = await request(server, "diff.git_working_tree")
    assert result["type"] == "diff.git_working_tree", result
    assert "+changed with attributes" in result["files"][0]["patch"]
    assert not marker.exists()


@pytest.mark.asyncio
async def test_ws_user_git_uses_standalone_host_and_does_not_change_model_permissions(editor_server, monkeypatch):
    from backend.sandbox.policy import (FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry,
        FileSystemSandboxPolicy, FileSystemSpecialPath, PermissionProfile)

    server = editor_server
    await seed(server)
    root = server.host.workspace
    (root / "tracked.txt").write_bytes(b"confirmed target\n")
    (root / "报告 文档.txt").write_bytes(b"second confirmed target\n")
    starts, scopes = [], []
    original_start = server.host.environments.primary().environment.process.start
    async def host_start(argv, cwd, **kwargs):
        assert kwargs["sandbox"] is None
        starts.append((argv, cwd))
        return await original_start(argv, cwd, **kwargs)
    monkeypatch.setattr(server.host.environments.primary().environment.process, "start", host_start)
    @asynccontextmanager
    async def managed_capture(*args, **kwargs):
        async with capture_workspace_execution(*args, **kwargs) as original:
            policy = PermissionProfile.managed(FileSystemSandboxPolicy.restricted([
                FileSystemSandboxEntry(FileSystemPath.special(FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
                FileSystemSandboxEntry(FileSystemPath("path", (root / "wild1.txt").as_uri()), FileSystemAccessMode.DENY),
            ]))
            scope = replace(original.permission.environment_scope, permission_profile=policy)
            scopes.append(scope)
            yield replace(original, permission=replace(original.permission, mode="confirm", environment_scope=scope))
    monkeypatch.setattr("backend.services.workspace_api_service.capture_workspace_execution", managed_capture)
    for command, fields in (("diff.git_stage_file", {"path": "tracked.txt"}),
            ("diff.git_unstage_file", {"path": "tracked.txt"}), ("diff.git_stage_all", {}),
            ("diff.git_unstage_all", {})):
        result = await review_action(server, command, **fields)
        assert result["level"] == "success", json.dumps(result, ensure_ascii=False)
    diff = await request(server, "diff.git_working_tree")
    assert diff["type"] == "diff.git_working_tree", diff
    result = await request(server, "diff.git_revert_patch", expected="command.result",
        patch="".join(file["patch"] for file in diff["files"]), confirmed=True)
    assert result["level"] == "success", json.dumps(result, ensure_ascii=False)
    assert (root / "tracked.txt").read_bytes() == b"before\n"
    assert (root / "报告 文档.txt").read_bytes() == b"before\n"
    (root / "tracked.txt").write_bytes(b"restore confirmed target\n")
    restored = await review_action(server, "diff.git_revert_file", path="tracked.txt", confirmed=True)
    assert restored["level"] == "success", json.dumps(restored, ensure_ascii=False)
    assert (root / "tracked.txt").read_bytes() == b"before\n"
    (root / "wild1.txt").write_bytes(b"user Git action is independent of model permissions\n")
    restored = await review_action(server, "diff.git_revert_file", path="wild1.txt", confirmed=True)
    assert restored["level"] == "success", json.dumps(restored, ensure_ascii=False)
    assert (root / "wild1.txt").read_bytes() == b"before\n"
    from backend.workspace.executor_git import run_executor_git
    before_model = len(starts)
    async with managed_capture(server.session, server.params["conversation_id"],
            environment_id=server.params["environment_id"]) as context:
        async with ExecutorWorkspaceService(context) as service:
            with pytest.raises(PermissionError, match="workspace-write authority"):
                await run_executor_git(service, ["git", "add", "--", "tracked.txt"], write_metadata=True)
    assert len(starts) == before_model
    assert starts and all(cwd == root.as_uri() for _, cwd in starts)
    assert scopes and all(not scope.resolve_access(root.as_uri()).can_write for scope in scopes)
    assert all(scope.resolve_access((root / "wild1.txt").as_uri()) is FileSystemAccessMode.DENY for scope in scopes)
    assert not list((root / ".git").glob(".minicode-undo-*"))
    assert not list(root.glob(".minicode-undo-*"))
    assert not (server.view / ".git").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("repository", (False, True), ids=("plain-folder", "repository-subdirectory"))
async def test_ws_undo_applies_workspace_relative_patch_in_plain_folder_and_git_subdirectory(editor_server, repository):
    from backend.agent.environment_selection import TurnEnvironmentRequest, TurnEnvironmentSelection

    server = editor_server
    root = server.host.workspace
    if repository:
        await seed(server)
        child = root / "nested"
        child.mkdir()
        await server.host.owner.update_selections((TurnEnvironmentSelection.from_request(TurnEnvironmentRequest(
            server.params["environment_id"], child.as_uri(), (root.as_uri(),))),))
    else:
        child = root
    (root / "outside.txt").write_bytes(b"outside must remain\n")
    target = child / "edit.txt"
    target.write_bytes(b"after\n")
    patch = "diff --git a/edit.txt b/edit.txt\n--- a/edit.txt\n+++ b/edit.txt\n@@ -1 +1 @@\n-before\n+after\n"
    result = await request(server, "diff.git_revert_patch", expected="command.result", patch=patch, confirmed=True,
        workspace_root=child.as_uri(), cwd_uri=child.as_uri())
    assert result["level"] == "success", json.dumps(result, ensure_ascii=False)
    assert result["data"]["cwd_uri"] == child.as_uri()
    assert target.read_text(encoding="utf-8") == "before\n"
    assert not (child / "nested/edit.txt").exists()
    assert (root / "outside.txt").read_bytes() == b"outside must remain\n"
    assert not (server.view / "edit.txt").exists()
    assert not list(child.glob(".minicode-undo-*"))
