"""Real executor worktrees, durable refs and original attachment ownership."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os

import pytest

from backend.services.workspace_api_service import capture_workspace_execution
from backend.tests.test_executor_editor_api import editor_server
from backend.tests.test_executor_tool_admission import executor_host
from backend.workspace.executor_editor import ExecutorEditorService
from backend.workspace.executor_paths import ExecutorPath
from backend.workspace.executor_worktree import executor_worktree_action


async def _git(service, *args, cwd=None):
    result = await service.run(["git", *args], cwd=cwd)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode("utf-8").strip()


async def _prepare(server, service):
    await _git(service, "init", "-b", "main")
    await service.write_bytes("tracked.txt", b"committed\n")
    await service.write_bytes(".gitignore", b"ignored.txt\n.minicode/\n")
    await service.write_bytes(".gitattributes", b"*.txt text eol=crlf\n")
    await _git(service, "add", "tracked.txt", ".gitignore", ".gitattributes")
    await _git(service, "-c", "user.name=Owned Test", "-c", "user.email=owned@test.invalid", "commit", "-m", "Owned baseline")
    linked = service.cwd.joinpath(".minicode/worktrees/owned")
    await _git(service, "worktree", "add", "-b", "owned", linked.native)
    return linked


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_real_dirty_worktree_snapshot_restore_and_controller_shadow(editor_server):
    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            await service.write_bytes(linked.joinpath("tracked.txt"), b"dirty tracked\n")
            await service.write_bytes(linked.joinpath("untracked.txt"), b"untracked bytes\x00\xff")
            await service.write_bytes(linked.joinpath("ignored.txt"), b"ignored bytes\n")
    inventory = await server.client.get("/api/workspace/git/worktree", params=server.params)
    assert inventory.status_code == 200, inventory.text
    assert next(item for item in inventory.json()["worktrees"] if item["path_uri"] == linked.uri)["can_remove"]
    denied = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": linked.uri})
    assert denied.status_code == 200 and not denied.json()["removed"] and "force" in denied.json()["error"], denied.text
    removed = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": linked.uri, "force": True})
    assert removed.status_code == 200 and removed.json()["removed"], removed.text
    snapshot = removed.json()["snapshot_id"]
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            assert await service.metadata(linked) is None
            assert not [item for item in await service.list_directory(service.cwd.joinpath(".git")) if item["fileName"].startswith("minicode-wt-index-")]
    listing = await server.client.get("/api/workspace/git/worktree/snapshots", params=server.params)
    assert listing.status_code == 200, listing.text
    record = next(item for item in listing.json()["snapshots"] if item["id"] == snapshot)
    assert record["original_path"] == linked.uri and record["snapshot_ref"] == removed.json()["snapshot_ref"]
    restored = await server.client.post("/api/workspace/git/worktree/restore", params=server.params, json={"snapshot_id": snapshot})
    assert restored.status_code == 200 and restored.json()["restored"], restored.text
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"dirty tracked\n"
            assert await service.read_bytes(linked.joinpath("untracked.txt")) == b"untracked bytes\x00\xff"
            assert await service.read_bytes(linked.joinpath("ignored.txt")) == b"ignored bytes\n"
            assert await _git(service, "rev-parse", "HEAD", cwd=linked) == record["snapshot_sha"]
    again = await server.client.post("/api/workspace/git/worktree/restore", params=server.params, json={"snapshot_id": snapshot})
    assert again.status_code == 200 and again.json()["restored"] and "owned-restored" in again.json()["path"], again.text
    outside = await server.client.post("/api/workspace/git/worktree/restore", params=server.params,
        json={"snapshot_id": snapshot, "dest": server.view.as_uri()})
    assert outside.status_code == 409, outside.text
    assert (server.view / "same.txt").read_text() == "controller must remain untouched\n"
    assert not (server.view / ".git").exists()


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_worktree_rejects_current_unregistered_embedded_and_read_only(editor_server):
    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            root = service.cwd
            await service.write_bytes(linked.joinpath("nested/.git/config"), b"[core]\n")
            for path in (root.uri, server.view.as_uri()):
                response = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": path, "force": True})
                assert response.status_code in (403, 409), response.text
            response = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": linked.uri, "force": True})
            assert response.status_code == 409 and "embedded" in response.text, response.text
            assert await service.read_bytes(linked.joinpath("nested/.git/config")) == b"[core]\n"
            from backend.sandbox.policy import FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, PermissionProfile
            from backend.sandbox.policy import FileSystemSpecialPath
            profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted((
                FileSystemSandboxEntry(FileSystemPath("special", FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),)))
            scope = replace(service.scope, permission_profile=profile)
            service.scope = scope
            service.sandbox = scope.sandbox_context()
            service.context = replace(service.context, permission=replace(service.context.permission, environment_scope=scope))
            with pytest.raises(PermissionError, match="write.*denied"):
                await executor_worktree_action(service, "remove", path=linked.uri, force=True)


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_failed_snapshot_or_remove_preserves_original_index_and_snapshot(editor_server, monkeypatch):
    import backend.workspace.executor_worktree as lifecycle
    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            await service.write_bytes(linked.joinpath("tracked.txt"), b"staged\n")
            await _git(service, "add", "tracked.txt", cwd=linked)
            await service.write_bytes(linked.joinpath("tracked.txt"), b"working\n")
            gitdir = service.resolve(await _git(service, "rev-parse", "--path-format=absolute", "--git-dir", cwd=linked))
            index = await service.read_bytes(gitdir.joinpath("index"))
            original_git = lifecycle._git

            async def fail_snapshot(captured, programs, *args, **kwargs):
                if args[0] == "write-tree":
                    args = ("write-tree", "--owned-invalid-option")
                return await original_git(captured, programs, *args, **kwargs)

            monkeypatch.setattr(lifecycle, "_git", fail_snapshot)
            response = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": linked.uri, "force": True})
            assert response.status_code == 409, response.text
            assert await service.read_bytes(gitdir.joinpath("index")) == index
            assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"working\n"
            monkeypatch.setattr(lifecycle, "_git", original_git)

            async def fail_remove(captured, programs, *args, **kwargs):
                if args[:2] == ("worktree", "remove"):
                    args = (*args[:2], "--owned-invalid-option", *args[2:])
                    kwargs["allowed"] = (129,)
                return await original_git(captured, programs, *args, **kwargs)

            monkeypatch.setattr(lifecycle, "_git", fail_remove)
            response = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": linked.uri, "force": True})
            assert response.status_code == 200 and not response.json()["removed"] and response.json()["snapshot_id"], response.text
            assert await service.read_bytes(gitdir.joinpath("index")) == index
            assert await _git(service, "rev-parse", response.json()["snapshot_ref"])
            assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"working\n"


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_cancelled_transaction_holds_original_executor_until_real_remove_settles(editor_server, monkeypatch):
    import backend.workspace.executor_worktree as lifecycle
    server = editor_server
    entered, release = asyncio.Event(), asyncio.Event()
    original_git = lifecycle._git
    captured_environments = []

    async def gate_remove(service, programs, *args, **kwargs):
        if args[:2] == ("worktree", "remove"):
            captured_environments.append(service.environment)
            entered.set()
            await release.wait()
        return await original_git(service, programs, *args, **kwargs)

    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            original_environment = service.environment
            monkeypatch.setattr(lifecycle, "_git", gate_remove)
            task = asyncio.create_task(executor_worktree_action(service, "remove", path=linked.uri, force=False))
            try:
                await asyncio.wait_for(entered.wait(), 30)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done() and await service.metadata(linked) is not None
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 30)
                assert captured_environments == [original_environment]
                assert await service.metadata(linked) is None
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_websocket_worktree_activation_creates_its_own_original_executor_and_next_run(editor_server):
    from backend.agent.environment_runtime import prepare_run_environments
    from backend.agent.environment_selection import bind_tool_execution_environment, close_tool_execution_environment
    from backend.agent.run_context import RunContext
    from backend.permissions.context import ToolExecutionContext
    from backend.workspace.executor_service import ExecutorWorkspaceService

    server = editor_server
    old_id = server.session.active_conversation_id
    old_record = server.session.conversation_repo.get_conversation(old_id)
    old_selections = server.host.owner.selections()
    async with capture_workspace_execution(server.session, old_id) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            original = service.environment
            await service.write_bytes("old-unsaved-on-disk.txt", b"old workspace bytes\n")
    replacement_root = server.view / "replacement-target"
    replacement_root.mkdir()
    replacement_home = server.view / "replacement-source-home"
    replacement_home.mkdir()
    replacement = server.host.manager.upsert({"id": server.params["environment_id"], "program": os.environ["MINICODE_SOURCE_EXECUTOR"],
        "args": ["exec-server", "--listen", "stdio", "--concurrent-requests", "4"], "cwd": str(replacement_root),
        "env": {"CODEX_HOME": str(replacement_home)}})
    assert replacement is not original
    await server.websocket.send(json.dumps({"type": "workspace.set", "path": linked.uri, "permission_mode": "bypass"}))
    switched = receipt = None
    async with asyncio.timeout(60):
        while receipt is None:
            event = json.loads(await server.websocket.recv())
            if event["type"] == "conversation.switched":
                switched = event
            if event["type"] == "command.result" and event.get("command") == "workspace.set":
                receipt = event
    assert receipt["level"] == "success", receipt
    assert switched is not None, receipt
    new_id = server.session.active_conversation_id
    assert new_id != old_id
    created = server.session.conversation_repo.get_conversation(new_id)
    assert created.execution_cwd == linked.uri and created.workspace_root == linked.uri
    assert server.session.conversation_repo.get_conversation(old_id).execution_cwd == old_record.execution_cwd
    assert server.host.owner.selections() == old_selections
    assert server.session.runtime_snapshot()["workspace_root"] == linked.uri
    params = {**server.params, "workspace_root": linked.uri, "conversation_id": new_id}
    read = await server.client.get("/api/workspace/file", params={**params, "path": "tracked.txt"})
    assert read.status_code == 200 and read.json()["content"].replace("\r\n", "\n") == "committed\n", read.text
    protected = await server.client.get("/api/workspace/file", params={**params, "path": ".git"})
    assert protected.status_code == 403, protected.text
    escape = await server.client.get("/api/workspace/file", params={**params, "path": "../outside.txt"})
    assert escape.status_code == 400, escape.text
    saved = await server.client.put("/api/workspace/file/compare-write", params=params,
        json={"path": "tracked.txt", "expected_hash": read.json()["content_hash"], "content": "next conversation write\n"})
    assert saved.status_code == 200, saved.text
    stale = await server.client.get("/api/workspace/file", params={**server.params, "path": "tracked.txt"})
    assert stale.status_code == 409, stale.text
    runtime = server.manager.execution_contexts.thread_runtime(server.session.conversation_repo.store_instance_id(), new_id)
    from backend.workspace.executor_activation import prepare_executor_workspace_activation
    accepted_owner = runtime.thread_environments
    repeat = await prepare_executor_workspace_activation(server.session, linked.uri)
    await repeat.bind(server.session, created)
    assert runtime.thread_environments is accepted_owner
    run = RunContext(environment_manager=server.host.manager, environment_thread_runtime=runtime, execution_cwd_uri=linked.uri)
    permission = server.session.permission_context_for_conversation(created, source="owned-worktree-run")
    snapshot = await prepare_run_environments(run, agent_session=None, permission_context=permission,
        config_stack=server.session.config.config_layer_stack, workspace_root=None, conversation_id=new_id)
    try:
        assert snapshot.primary().environment is original
        assert snapshot.primary().selection.cwd == linked.uri
        bound = bind_tool_execution_environment(ToolExecutionContext(permission=permission, run_context=run), snapshot.primary())
        try:
            async with ExecutorWorkspaceService(bound) as service:
                actual = await _git(service, "rev-parse", "--show-toplevel")
                assert service.resolve(actual) == linked
                assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"next conversation write\n"
                assert await service.read_bytes(service.cwd.parent.parent.parent.joinpath("tracked.txt")) == b"committed\n"
                assert await service.read_bytes(service.cwd.parent.parent.parent.joinpath("old-unsaved-on-disk.txt")) == b"old workspace bytes\n"
        finally:
            await close_tool_execution_environment(bound)
    finally:
        await snapshot.aclose()
    accepted_owner = runtime.thread_environments
    runtime.thread_environments = None
    try:
        unavailable = await server.client.get("/api/workspace/file", params={**params, "path": "tracked.txt"})
        assert unavailable.status_code == 503 and "original" in unavailable.text, unavailable.text
        assert replacement.client.connect_task is None
    finally:
        runtime.thread_environments = accepted_owner
    assert list(replacement_root.iterdir()) == []


@pytest.mark.parametrize("executor_host", ["local"], indirect=True)
@pytest.mark.asyncio
async def test_same_executor_engine_on_local_transport_preserves_raw_snapshot_bytes(editor_server):
    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            await service.write_bytes(linked.joinpath("tracked.txt"), b"raw LF\n")
            await service.write_bytes(linked.joinpath("ignored.txt"), b"raw CRLF\r\n")
            removed = await executor_worktree_action(service, "remove", path=linked.uri, force=True)
            assert removed["removed"], removed
            restored = await executor_worktree_action(service, "restore", snapshot_id=removed["snapshot_id"], dest=None)
            assert restored["restored"], restored
            assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"raw LF\n"
            assert await service.read_bytes(linked.joinpath("ignored.txt")) == b"raw CRLF\r\n"


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_snapshot_index_cleanup_error_retains_original_callback_owner(editor_server, monkeypatch):
    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            await service.write_bytes(linked.joinpath("tracked.txt"), b"must survive cleanup error\n")
            filesystem = service.filesystem
            original_remove = filesystem.remove
            release = asyncio.Event()

            async def gated_remove(path, **kwargs):
                if "minicode-wt-index-" in path and not release.is_set():
                    raise PermissionError("owned original snapshot index cleanup is paused")
                await original_remove(path, **kwargs)

            monkeypatch.setattr(filesystem, "remove", gated_remove)
            response = await server.client.delete("/api/workspace/git/worktree", params={**server.params, "path": linked.uri, "force": True})
            assert response.status_code == 403 and "cleanup is paused" in response.text, response.text
            assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"must survive cleanup error\n"
            pending = [command.runner for command in server.session.background_manager._commands.values()
                if command.runner is not None and command.runner.cleanup_callbacks and not command.runner.closed]
            assert len(pending) == 1 and pending[0].environment is service.environment
            assert not pending[0].lease.released and pending[0].handle is None
            release.set()
            assert await pending[0].cleanup()
            assert pending[0].closed and pending[0].lease.released
            assert not [item for item in await service.list_directory(service.cwd.joinpath(".git")) if item["fileName"].startswith("minicode-wt-index-")]


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.parametrize("symbolic_link", [False, True])
@pytest.mark.asyncio
async def test_snapshot_preserves_git_executable_mode_and_real_symbolic_link(editor_server, symbolic_link):
    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            await _git(service, "update-index", "--chmod=+x", "tracked.txt", cwd=linked)
            await _git(service, "-c", "user.name=Owned Test", "-c", "user.email=owned@test.invalid", "commit", "-m", "Executable baseline", cwd=linked)
            local_link = server.host.workspace / ".minicode/worktrees/owned/link.txt"
            if symbolic_link:
                try:
                    os.symlink("tracked.txt", local_link)
                except OSError as error:
                    if getattr(error, "winerror", None) == 1314:
                        pytest.skip("Windows user lacks symbolic-link creation privilege; no ACL or account was changed")
                    raise
            await service.write_bytes(linked.joinpath("tracked.txt"), b"mode and link recovery\n")
            removed = await executor_worktree_action(service, "remove", path=linked.uri, force=True)
            assert removed["removed"], removed
            tree = await _git(service, "ls-tree", "-r", removed["snapshot_ref"])
            assert "100755 blob" in next(line for line in tree.splitlines() if line.endswith("\ttracked.txt"))
            if symbolic_link:
                assert "120000 blob" in next(line for line in tree.splitlines() if line.endswith("\tlink.txt"))
            restored = await executor_worktree_action(service, "restore", snapshot_id=removed["snapshot_id"], dest=None)
            assert restored["restored"], restored
            if symbolic_link:
                assert local_link.is_symlink() and os.readlink(local_link) == "tracked.txt"
            assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"mode and link recovery\n"


def test_file_uri_owner_scope_keeps_executor_namespace_without_controller_resolution(monkeypatch):
    from pathlib import Path
    from backend.owner_scope import canonical_workspace_root

    def controller_resolve(*args, **kwargs):
        raise AssertionError("Remote URI ownership cannot resolve a controller path")

    with monkeypatch.context() as owned:
        owned.setattr(Path, "resolve", controller_resolve)
        assert canonical_workspace_root("file:///executor/project/.minicode/worktrees/owned") == "file:///executor/project/.minicode/worktrees/owned"
        assert canonical_workspace_root("file:///C:/Executor/Owned") == "file:///c:/executor/owned"


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_snapshot_uses_complete_structured_git_output_beyond_tool_tail(editor_server):
    from backend.workspace.executor_git import run_executor_git

    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as context:
        async with ExecutorEditorService(context, application_config=server.session.config) as service:
            linked = await _prepare(server, service)
            bulk = server.host.workspace / ".minicode/worktrees/owned/bulk"
            bulk.mkdir()
            for index in range(1024):
                (bulk / f"{index:04d}-original.txt").write_bytes(b"raw original LF\n")
            removed = await executor_worktree_action(service, "remove", path=linked.uri, force=True)
            assert removed["removed"], removed
            tree = await run_executor_git(service, ["git", "ls-tree", "-r", "-z", removed["snapshot_ref"]])
            assert tree.returncode == 0
            assert len([entry for entry in tree.stdout.split(b"\0") if entry]) == 1027
            for name in ("0000-original.txt", "1023-original.txt"):
                blob = await run_executor_git(service, ["git", "cat-file", "blob", removed["snapshot_ref"] + ":bulk/" + name])
                assert blob.returncode == 0 and blob.stdout == b"raw original LF\n"


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_retargeted_workspace_write_can_commit_only_registered_git_metadata(editor_server, record_testsuite_property):
    from backend.agent.environment_runtime import resolve_environment_config, thread_environment_defaults
    from backend.agent.environment_selection import EnvironmentConfigState, ThreadEnvironments, bind_tool_execution_environment, close_tool_execution_environment
    from backend.agent.run_context import RunContext
    from backend.permissions.context import ToolExecutionContext
    from backend.sandbox.policy import FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemSpecialPath, PermissionProfile
    from backend.workspace.executor_commands import target_programs
    from backend.workspace.executor_delivery import executor_delivery_action
    from backend.workspace.delivery import GitDeliveryRequest
    from backend.workspace.executor_git import executor_git_write_scope, run_executor_git
    from backend.workspace.executor_service import executor_registration_id

    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as original:
        async with ExecutorEditorService(original, application_config=server.session.config) as source:
            linked = await _prepare(server, source)
            await _git(source, "config", "user.name", "Owned Confirm Test")
            await _git(source, "config", "user.email", "confirm@test.invalid")
            profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted((
                FileSystemSandboxEntry(FileSystemPath("special", FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
                FileSystemSandboxEntry(FileSystemPath("path", linked.uri), FileSystemAccessMode.WRITE))))
            permission = replace(original.permission, mode="confirm", sandbox_mode="workspace-write")
            attachment = original.turn_environment
            config = replace(attachment.config, permission_profile=profile)
            captured = replace(attachment, config_origin="Owner", selection=replace(attachment.selection,
                config=EnvironmentConfigState.ready(config)))
            owner = ThreadEnvironments.retarget(server.host.manager,
                thread_environment_defaults(permission, server.session.config.config_layer_stack),
                resolve_config=resolve_environment_config, attachment=captured, cwd=linked.uri, workspace_roots=(linked.uri,))
            try:
                snapshot = await owner.snapshot()
                try:
                    bound = bind_tool_execution_environment(ToolExecutionContext(permission=permission,
                        conversation_id=server.params["conversation_id"], session_id=server.params["session_id"],
                        background_manager=server.session.background_manager,
                        run_context=RunContext(turn_environments=snapshot, environment_manager=server.host.manager)), snapshot.primary())
                    try:
                        async with ExecutorEditorService(bound, application_config=server.session.config) as service:
                            assert service.environment is source.environment
                            assert service.scope.resolve_access(linked.uri).can_write
                            assert not service.scope.resolve_access(source.cwd.joinpath("tracked.txt").uri).can_write
                            record_testsuite_property("retarget_commit_original_wire", json.dumps(service.sandbox))
                            assert "git" in await target_programs(service)
                            await service.write_bytes("tracked.txt", b"confirmed linked change\n")
                            staged = await run_executor_git(service, ["git", "add", "tracked.txt"], write_metadata=True)
                            assert staged.returncode == 0, staged.stderr
                            await executor_delivery_action(service, GitDeliveryRequest(action="commit", message="Owned confirm commit",
                                expected_branch="owned", expected_registration_id=executor_registration_id(bound)))
                            assert await _git(service, "log", "-1", "--format=%s") == "Owned confirm commit"
                            granted = await executor_git_write_scope(service)
                            assert granted.resolve_access(source.cwd.joinpath(".git/config").uri).can_write
                            assert not granted.resolve_access(source.cwd.joinpath("tracked.txt").uri).can_write
                            backlink = server.host.workspace / ".git/worktrees/owned/gitdir"
                            before = backlink.read_bytes()
                            try:
                                backlink.write_text(source.cwd.joinpath(".git").native + "\n", encoding="utf-8")
                                with pytest.raises(PermissionError, match="backlink"):
                                    await executor_git_write_scope(service)
                            finally:
                                backlink.write_bytes(before)
                    finally:
                        await close_tool_execution_environment(bound)
                finally:
                    await snapshot.aclose()
            finally:
                await owner.aclose()


@pytest.mark.parametrize("executor_host", ["source"], indirect=True)
@pytest.mark.asyncio
async def test_gui_worktree_action_temporarily_writes_only_its_validated_directory_pool(editor_server, record_testsuite_property):
    from backend.sandbox.policy import FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, FileSystemSpecialPath, PermissionProfile
    from backend.workspace.executor_commands import target_programs
    from backend.workspace.executor_worktree import _action_parent, _inventory

    server = editor_server
    async with capture_workspace_execution(server.session, server.params["conversation_id"]) as original:
        async with ExecutorEditorService(original, application_config=server.session.config) as source:
            linked = await _prepare(server, source)
            await source.write_bytes(linked.joinpath("tracked.txt"), b"workspace-write recovery\n")
            pool = source.cwd.joinpath(".minicode/worktrees")
            entries = (
                FileSystemSandboxEntry(FileSystemPath("special", FileSystemSpecialPath.ROOT), FileSystemAccessMode.READ),
                FileSystemSandboxEntry(FileSystemPath("path", source.cwd.uri), FileSystemAccessMode.WRITE),
                FileSystemSandboxEntry(FileSystemPath("path", source.cwd.joinpath(".git").uri), FileSystemAccessMode.READ),
                FileSystemSandboxEntry(FileSystemPath("path", pool.uri), FileSystemAccessMode.READ),
            )
            profile = PermissionProfile.managed(FileSystemSandboxPolicy.restricted(entries))
            scope = replace(source.scope, permission_profile=profile)
            context = replace(original, permission=replace(original.permission, mode="confirm", sandbox_mode="workspace-write", environment_scope=scope))
            async with ExecutorEditorService(context, application_config=server.session.config) as service:
                assert service.scope.resolve_access(service.cwd.uri).can_write
                assert not service.scope.resolve_access(linked.uri).can_write
                record_testsuite_property("gui_worktree_original_wire", json.dumps(service.sandbox))
                removed = await executor_worktree_action(service, "remove", path=linked.uri, force=True)
                assert removed["removed"], removed
                restored = await executor_worktree_action(service, "restore", snapshot_id=removed["snapshot_id"], dest=None)
                assert restored["restored"], restored
                assert await service.read_bytes(linked.joinpath("tracked.txt")) == b"workspace-write recovery\n"
                assert not service.scope.resolve_access(linked.uri).can_write
                assert not service.context.permission.environment_scope.resolve_access(linked.uri).can_write
                denied = linked.joinpath("denied.txt")
                inventory = await _inventory(service, await target_programs(service))
                service.scope = replace(service.scope, permission_profile=PermissionProfile.managed(
                    FileSystemSandboxPolicy.restricted((*entries,
                        FileSystemSandboxEntry(FileSystemPath("path", denied.uri), FileSystemAccessMode.DENY)))))
                service.sandbox = service.scope.sandbox_context()
                granted = await _action_parent(service, inventory, pool)
                assert granted.scope.resolve_access(denied.uri) is FileSystemAccessMode.DENY
                assert service.scope.resolve_access(linked.uri) is FileSystemAccessMode.READ
