"""Actual file/Git/checkpoint consumers on local and installed Source peers."""
from __future__ import annotations

from dataclasses import replace
import json

import pytest
import pytest_asyncio

from backend.agent.environment_selection import bind_tool_execution_environment, close_tool_execution_environment
from backend.agent.environment_selection import TurnEnvironmentSnapshot
from backend.agent.turn_diff_tracker import TurnDiffTracker
from backend.checkpoint.manager import CheckpointManager
from backend.checkpoint.store import CheckpointStore
from backend.permissions.review import build_structured_diff_payload
from backend.sandbox.policy import FileSystemAccessMode, FileSystemPath, FileSystemSandboxEntry, FileSystemSandboxPolicy, PermissionProfile
from backend.services.checkpoint_service import list_checkpoints, rewind_checkpoint
from backend.tests.test_executor_tool_admission import batch, executor_host
from backend.tools.apply_patch import ApplyPatchTool
from backend.tools.edit_file import EditFileTool
from backend.tools.fuzzy_search_tool import FuzzySearchTool
from backend.tools.git_tools import GitCommitTool, GitDiffTool, GitLogTool, GitStatusTool
from backend.tools.list_files import ListFilesTool
from backend.tools.notebook_tool import NotebookEditTool
from backend.tools.read_file import ReadFileTool
from backend.tools.search_tools import GlobFilesTool, GrepFilesTool
from backend.tools.write_file import WriteFileTool
from backend.workspace.executor_service import ExecutorWorkspaceService, executor_read_hash, text_hash
from backend.workspace.executor_tool_io import generate_executor_tool_diff
from backend.workspace.executor_context_restore import restored_executor_file_blocks


@pytest_asyncio.fixture
async def accepted(executor_host):
    context = bind_tool_execution_environment(executor_host.context, executor_host.environments.primary())
    try:
        yield executor_host, context
    finally:
        await close_tool_execution_environment(context)


@pytest.mark.asyncio
async def test_actual_text_and_notebook_edits_preserve_bytes_and_uri_diff_identity(accepted):
    host, context = accepted
    target = host.workspace / "spaces #%.txt"
    target.write_bytes(b"before\r\nsecond\r\n")
    read = await ReadFileTool(host.context.artifact_store).execute({"file_path": target.name}, context)
    assert not read.is_error, read.content
    assert executor_read_hash(context, target.as_uri()) == text_hash("before\nsecond\n")
    args = {"file_path": target.name, "old_string": "before\nsecond", "new_string": "after\nsecond"}
    diff = await generate_executor_tool_diff("edit_file", args, context)
    assert diff["files"][0]["path_uri"] == target.as_uri()
    assert diff["files"][0]["environment_id"] == context.environment_id
    result = await EditFileTool().execute(args, context)
    assert not result.is_error, result.content
    assert target.read_bytes() == b"after\r\nsecond\r\n"
    target.write_bytes(b"external\r\n")
    stale = await WriteFileTool().execute({"file_path": target.name, "content": "wrong", "expected_hash": args["expected_hash"]}, context)
    assert stale.is_error and "changed" in stale.content.lower()
    assert target.read_bytes() == b"external\r\n"
    notebook = host.workspace / "owned.ipynb"
    notebook.write_text(json.dumps({"cells": [{"id": "one", "cell_type": "code", "source": ["old"], "outputs": [1]}]}), encoding="utf-8")
    assert not (await ReadFileTool(host.context.artifact_store).execute({"file_path": notebook.name}, context)).is_error
    changed = await NotebookEditTool().execute({"notebook_path": notebook.name, "cell_id": "one", "new_source": "print(2)"}, context)
    assert not changed.is_error, changed.content
    cell = json.loads(notebook.read_text())["cells"][0]
    assert cell["id"] == "one" and cell["outputs"] == [] and cell["source"] == ["print(2)"]
    assert changed.runtime_metadata["committed_diff"]["files"][0]["path_uri"] == notebook.as_uri()


@pytest.mark.asyncio
async def test_patch_checkpoint_roundtrip_is_durable_and_uses_current_accepted_scope(accepted, tmp_path):
    host, context = accepted
    original = host.workspace / "old.txt"
    original.write_bytes(b"before\r\n")
    patch = "*** Begin Patch\n*** Update File: old.txt\n*** Move to: renamed.txt\n@@\n-before\n+after\n*** Add File: added.txt\n+new\n*** End Patch"
    manager = CheckpointManager(CheckpointStore(tmp_path / "owned-checkpoints"))
    args = {"patch": patch}
    reviewed = await generate_executor_tool_diff("apply_patch", args, context)
    checkpoint = await manager.snapshot(tool_name="apply_patch", args=args, workspace_root="this is not a host workspace",
        conversation_id="owned-checkpoint", execution_context=context)
    assert checkpoint.workspace_root == host.workspace.as_uri()
    assert checkpoint.metadata["environment_id"] == context.environment_id
    assert all(item.path_uri.startswith(host.workspace.as_uri()) for item in checkpoint.files)
    result = await ApplyPatchTool().execute(args, context)
    assert not result.is_error, result.content
    assert not original.exists() and (host.workspace / "renamed.txt").read_bytes() == b"after\r\n"
    assert result.runtime_metadata["committed_diff"]["files"][0]["old_path_uri"] == original.as_uri()
    cold = CheckpointManager(CheckpointStore(tmp_path / "owned-checkpoints"))
    listed = list_checkpoints(cold, conversation_id="owned-checkpoint", execution_context=context)
    assert listed.checkpoints[0]["id"] == checkpoint.id
    readonly = replace(context.permission.environment_scope, permission_profile=PermissionProfile.managed(FileSystemSandboxPolicy.restricted((
        FileSystemSandboxEntry(FileSystemPath("path", host.workspace.as_uri()), FileSystemAccessMode.READ),))))
    with pytest.raises(PermissionError, match="current execution scope"):
        await cold.rewind(checkpoint.id, execution_context=replace(context, permission=replace(context.permission, environment_scope=readonly)))
    assert not original.exists()
    rewound = await rewind_checkpoint(cold, checkpoint.id, conversation_id="owned-checkpoint", execution_context=context)
    assert rewound.checkpoint["metadata"]["environment_id"] == context.environment_id
    assert original.read_bytes() == b"before\r\n"
    assert not (host.workspace / "renamed.txt").exists() and not (host.workspace / "added.txt").exists()


@pytest.mark.asyncio
async def test_executor_checkpoint_reads_large_file_in_blocks(accepted, tmp_path, monkeypatch):
    host, context = accepted
    target = host.workspace / "large.bin"
    chunk = bytes(range(256)) * 4096
    with target.open("wb") as handle:
        for _ in range(4):
            handle.write(chunk)
    manager = CheckpointManager(CheckpointStore(tmp_path / "streamed-checkpoints"))

    async def reject_whole_file_reads(*_args, **_kwargs):
        raise AssertionError("executor checkpoint must read blocks")

    monkeypatch.setattr(ExecutorWorkspaceService, "read_bytes", reject_whole_file_reads)
    checkpoint = await manager.snapshot(tool_name="write_file", args={"file_path": target.name},
        workspace_root=host.workspace, conversation_id="streamed", execution_context=context)
    with manager._store.open_blob(checkpoint.files[0].blob) as handle:
        for _ in range(4):
            assert handle.read(len(chunk)) == chunk
        assert handle.read(1) == b""


@pytest.mark.asyncio
async def test_patch_partial_move_keeps_real_written_destination_and_reports_it(accepted, monkeypatch):
    host, context = accepted
    original = host.workspace / "source.txt"
    original.write_text("before\n", encoding="utf-8")
    filesystem = context.turn_environment.environment.filesystem
    actual_remove = filesystem.remove

    async def rejected_remove(path, **kwargs):
        if path == original.as_uri():
            raise PermissionError("owned real remove rejection")
        return await actual_remove(path, **kwargs)

    monkeypatch.setattr(filesystem, "remove", rejected_remove)
    result = await ApplyPatchTool().execute({"patch": "*** Begin Patch\n*** Update File: source.txt\n*** Move to: destination.txt\n@@\n-before\n+after\n*** End Patch"}, context)
    assert result.is_error and result.status == "partial", result.content
    assert original.read_text() == "before\n" and (host.workspace / "destination.txt").read_text() == "after\n"
    assert result.runtime_metadata["committed_diff"]["files"][0]["path_uri"] == (host.workspace / "destination.txt").as_uri()


@pytest.mark.asyncio
async def test_list_glob_grep_and_fuzzy_read_the_executor_tree_and_ignores(accepted):
    host, context = accepted
    (host.workspace / "src").mkdir()
    (host.workspace / "src" / "sample.py").write_text("zero\nneedle\nlast\n", encoding="utf-8")
    (host.workspace / "hidden.txt").write_text("needle", encoding="utf-8")
    (host.workspace / ".gitignore").write_text("hidden.txt\n", encoding="utf-8")
    listing = await ListFilesTool().execute({"recursive": True}, context)
    assert not listing.is_error and "src/sample.py" in listing.content, listing.content
    glob = await GlobFilesTool().execute({"pattern": "**/*.py"}, context)
    assert not glob.is_error and "src/sample.py" in glob.content, glob.content
    grep = await GrepFilesTool().execute({"pattern": "needle", "context": 1, "output_mode": "content"}, context)
    assert not grep.is_error and "src/sample.py:2:needle" in grep.content and "hidden.txt" not in grep.content, grep.content
    fuzzy = await FuzzySearchTool().execute({"query": "smp"}, context)
    assert not fuzzy.is_error and "src/sample.py" in fuzzy.content, fuzzy.content


@pytest.mark.asyncio
async def test_git_capabilities_run_in_executor_repository(accepted):
    host, context = accepted
    async with ExecutorWorkspaceService(context) as service:
        for argv in (["git", "init"], ["git", "config", "user.name", "Owned QA"], ["git", "config", "user.email", "owned@example.invalid"]):
            result = await service.run(argv)
            assert result.returncode == 0, result.stderr
    target = host.workspace / "git-file.txt"
    target.write_text("before\n", encoding="utf-8")
    created = await GitCommitTool().execute({"message": "owned checkpoint", "add_all": True}, context)
    assert not created.is_error, created.content
    target.write_text("after\n", encoding="utf-8")
    status = await GitStatusTool().execute({}, context)
    assert not status.is_error and "git-file.txt" in status.content, status.content
    diff = await GitDiffTool().execute({"file_path": target.as_uri()}, context)
    assert not diff.is_error and "-before" in diff.content and "+after" in diff.content, diff.content
    history = await GitLogTool().execute({"file_path": target.as_uri()}, context)
    assert not history.is_error and "owned checkpoint" in history.content, history.content


@pytest.mark.asyncio
async def test_configured_empty_snapshot_rejects_file_consumers_before_host_io(executor_host):
    host = executor_host
    target = host.workspace / "must-stay.txt"
    target.write_text("keep\n", encoding="utf-8")
    host.context.run_context.turn_environments = TurnEnvironmentSnapshot()
    host.registry.register(ListFilesTool())
    host.registry.register(GitStatusTool())
    for name, args in (("read_file", {"file_path": target.name}),
                       ("write_file", {"file_path": target.name, "content": "wrong"}),
                       ("list_files", {"path": "."}), ("git_status", {})):
        _, result, _ = await batch(host, name, args)
        assert result["is_error"] and "No usable execution environment" in result["summary"], result
    assert target.read_text() == "keep\n"


@pytest.mark.asyncio
async def test_legacy_checkpoint_keeps_cold_local_restore_without_guessing_remote_identity(accepted, tmp_path):
    host, context = accepted
    target = host.workspace / "legacy.txt"
    target.write_bytes(b"before\r\n")
    manager = CheckpointManager(CheckpointStore(tmp_path / "legacy-checkpoints"))
    record = await manager.snapshot(tool_name="write_file", args={"file_path": target.name},
        workspace_root=host.workspace, conversation_id="legacy-cold")
    target.write_bytes(b"after\r\n")
    if host.kind == "source":
        with pytest.raises(ValueError, match="no identity"):
            await manager.rewind(record.id, execution_context=context)
        assert target.read_bytes() == b"after\r\n"
    else:
        result = await rewind_checkpoint(manager, record.id, conversation_id="legacy-cold", execution_context=context)
        assert result.checkpoint["metadata"]["environment_id"] == context.environment_id
        assert target.read_bytes() == b"before\r\n"


@pytest.mark.asyncio
async def test_compaction_restore_uses_captured_uri_and_registration_without_reauthorizing_write(accepted):
    host, context = accepted
    target = host.workspace / "restored.txt"
    target.write_bytes(b"actual executor bytes\r\nsecond\r\n")
    assert not (await ReadFileTool(host.context.artifact_store).execute({"file_path": target.name}, context)).is_error
    observed = context.metadata["_executor_observed_files"]
    assert len(observed) == 1
    context.metadata["_executor_read_file_hashes"].clear()
    blocks = await restored_executor_file_blocks(context, host.state, available_tokens=1000,
        max_files=5, per_file_tokens=1000, estimate_tokens=lambda value: len(value) // 4)
    assert len(blocks) == 1 and "### restored.txt" in blocks[0] and "1: actual executor bytes" in blocks[0]
    assert executor_read_hash(context, target.as_uri()) is None
    for value in observed.values():
        value["registration_id"] = "older-process:1"
    assert await restored_executor_file_blocks(context, host.state, available_tokens=1000,
        max_files=5, per_file_tokens=1000, estimate_tokens=lambda value: len(value) // 4) == []


def test_turn_diff_uses_environment_uri_identity_and_source_display_roots():
    tracker = TurnDiffTracker({"one": "file:///repo", "two": "file:///repo"})
    for env, new in (("one", "first\n"), ("two", "second\n")):
        tracker.track_change(old_path="same.txt", new_path="same.txt", old_path_uri="file:///repo/same.txt",
            new_path_uri="file:///repo/same.txt", environment_id=env, old_content="before\n", new_content=new)
    diff = tracker.get_unified_diff()
    assert diff.count("diff --git") == 2
    assert "a/one/same.txt b/one/same.txt" in diff and "a/two/same.txt b/two/same.txt" in diff
    single = TurnDiffTracker({"one": "file:///C:/Repo"})
    single.track_change(old_path="ignored", new_path="ignored", old_path_uri="file:///c:/repo/A.txt", new_path_uri="file:///C:/Repo/a.txt",
        environment_id="one", old_content="before\n", new_content="after\n")
    assert "a/A.txt b/A.txt" in single.get_unified_diff()
