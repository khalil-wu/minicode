"""Owned-state fixtures; no real worktree mutation or native shell is used."""
import asyncio
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.message import AgentEvent
from backend.diff.git_integration import _parse_diff_output
from backend.workspace.worktree_snapshots import WorktreeSnapshotRecord, WorktreeSnapshotStore
from backend.ws.compaction_coordinator import compact_conversation
from backend.ws.conversation_runtime import ConversationRuntime
from backend.ws.manager import _dispose_unadopted_connection_resources
from backend.services.conversation_worktree_handoff_service import build_handoff_preflight
from backend.ws.command_scope import resolve_command_scope
from backend.ws.session_lifecycle import SessionLifecycle
from backend.ws.permission_runtime import _managed_permission_projection


@pytest.mark.asyncio
async def test_cancelled_adapter_close_still_releases_unadopted_artifact_owner():
    llm = SimpleNamespace(aclose=AsyncMock(side_effect=asyncio.CancelledError))
    store = SimpleNamespace(flush=AsyncMock(), shutdown=Mock(), clear=Mock())
    with pytest.raises(asyncio.CancelledError):
        await _dispose_unadopted_connection_resources(llm, store)
    store.flush.assert_awaited_once()
    store.shutdown.assert_called_once()
    store.clear.assert_called_once()


def test_worktree_snapshot_list_salvages_valid_metadata_beside_a_json_scalar(tmp_path):
    store = WorktreeSnapshotStore(tmp_path)
    store.save(WorktreeSnapshotRecord(id="valid", snapshot_sha="commit"))
    (tmp_path / "scalar.json").write_text("[]", encoding="utf-8")
    assert [record.id for record in store.list()] == ["valid"]


def test_worktree_snapshot_list_salvages_valid_metadata_beside_invalid_utf8(tmp_path):
    store = WorktreeSnapshotStore(tmp_path)
    store.save(WorktreeSnapshotRecord(id="valid", snapshot_sha="commit"))
    (tmp_path / "undecodable.json").write_bytes(b"\xff")
    assert [record.id for record in store.list()] == ["valid"]


def test_worktree_snapshot_read_permission_failure_does_not_become_not_found(tmp_path, monkeypatch):
    store = WorktreeSnapshotStore(tmp_path)
    store.save(WorktreeSnapshotRecord(id="valid", snapshot_sha="commit"))
    def unreadable(_path, *args, **kwargs):
        raise PermissionError("metadata cannot be read")
    monkeypatch.setattr(Path, "read_text", unreadable)
    with pytest.raises(PermissionError, match="metadata cannot be read"):
        store.get("valid")


def test_diff_counts_content_that_starts_with_plus_or_minus_header_prefixes():
    raw = ":100644 100644 old new M\0source.cpp\0\0diff --git a/source.cpp b/source.cpp\n--- a/source.cpp\n+++ b/source.cpp\n@@ -1,2 +1,2 @@\n----old\n---counter;\n++++new\n+++counter;\n"
    result = _parse_diff_output(raw)
    assert result.files[0].additions == result.total_additions == 2
    assert result.files[0].deletions == result.total_deletions == 2


@pytest.mark.asyncio
async def test_cancelled_compaction_reloads_the_committed_repository_snapshot(monkeypatch):
    builder = ContextBuilder()
    builder.append_user("Before compaction")
    before = builder.export_snapshot()
    current = SimpleNamespace(revision=1, context_snapshot=deepcopy(before))
    started, release = Event(), Event()
    def commit(_owner, **kwargs):
        started.set()
        assert release.wait(5)
        current.context_snapshot = deepcopy(kwargs["context_snapshot"])
        current.revision += 1
        return current
    async def compact(self, **kwargs):
        self.clear()
        self.append_user("Committed summary")
        return "Committed summary"
    monkeypatch.setattr(ContextBuilder, "compact", compact)
    repo = SimpleNamespace(get_conversation=lambda _owner: current, commit_compaction=commit)
    projection_lock = asyncio.Lock()
    runtime = ConversationRuntime(conversation_repo=repo, context_builder=builder, build_summary_from_transcript=lambda _items: "",
                                  projection_lock_for=lambda _owner: projection_lock)
    runtime.active_conversation_id = "owner"
    session = SimpleNamespace(conversation_runtime=runtime, conversation_repo=repo,
                              _conversation_projection_lock=lambda _owner: projection_lock,
                              _on_conversation_hydration_complete=AsyncMock())
    task = asyncio.create_task(compact_conversation(session, conversation_id="owner", context_builder=builder))
    try:
        await asyncio.to_thread(started.wait, 5)
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await runtime.wait_for_hydration("owner")
    assert builder.export_snapshot()["history"] == current.context_snapshot["history"]


def test_handoff_samples_one_local_checkout_once(tmp_path, monkeypatch):
    commands = []
    def git(root, *args, **kwargs):
        commands.append(args)
        if args[0] == "status": return True, ""
        if args[0] == "rev-parse": return True, "HEAD-ID"
        if args[0] == "branch": return True, "main"
        return False, "ref absent"
    monkeypatch.setattr("backend.services.conversation_worktree_handoff_service._git_snapshot", git)
    conversation = SimpleNamespace(id="owner", workspace_root=str(tmp_path), worktree_path="", git_isolated=False, git_branch="")
    result = build_handoff_preflight(conversation, target="worktree", conversation_repo=SimpleNamespace(list_conversations=lambda: []), main_worktree_root=lambda _path: tmp_path, has_running_turn=False)
    assert result["allowed"] is True
    assert commands.count(("status", "--porcelain=v1", "--untracked-files=all")) == 1
    assert commands.count(("rev-parse", "HEAD")) == 1
    assert commands.count(("branch", "--show-current")) == 1


@pytest.mark.asyncio
async def test_projectless_activation_clears_the_real_session_workspace_scope(tmp_path):
    conversation = SimpleNamespace(id="projectless", workspace_root="", worktree_path="")
    session = SimpleNamespace(active_conversation_id="old", conversation_repo=SimpleNamespace(get_conversation=lambda _owner: conversation),
                              refresh_tool_registry_if_mcp_changed=Mock())
    lifecycle = SessionLifecycle(session)
    lifecycle.workspace_root = tmp_path
    lifecycle.workspace_context = SimpleNamespace(root_path=tmp_path)
    session.session_lifecycle = lifecycle
    session.resolve_requested_workspace = Mock(side_effect=AssertionError("projectless owner must not resolve an old root"))
    assert await lifecycle.switch_workspace_for_conversation(conversation, announce=False) is True
    session.active_conversation_id = conversation.id
    scope = resolve_command_scope(session, {"conversation_id": conversation.id})
    assert scope.workspace_root == ""
    with pytest.raises(ValueError, match="Open a workspace"):
        resolve_command_scope(session, {"conversation_id": conversation.id}, require_workspace=True)


@pytest.mark.asyncio
async def test_connection_disposal_keeps_both_adopted_resources_and_accepts_no_store():
    llm = SimpleNamespace(aclose=AsyncMock())
    store = SimpleNamespace(flush=AsyncMock(), shutdown=Mock(), clear=Mock())
    await _dispose_unadopted_connection_resources(llm, store, adopted_session=SimpleNamespace(llm=llm, artifact_store=store))
    llm.aclose.assert_not_called()
    store.flush.assert_not_called()
    await _dispose_unadopted_connection_resources(llm, None)
    llm.aclose.assert_awaited_once()


def test_permission_projection_keeps_the_requirement_source_and_violation(monkeypatch):
    violation = ValueError("managed policy disallows requested mode")
    requirements = SimpleNamespace(resolve_permission_mode=lambda mode: (mode, None),
        source_for=lambda key: "managed" if key == "allowed_approval_policies" else "",
        approval_policy_for_mode=lambda _mode: "on-request", sandbox_mode_for_permission_mode=lambda _mode: "workspace-write")
    monkeypatch.setattr("backend.config.get_config_requirements", lambda: requirements)
    assert _managed_permission_projection("default") == ("default", "on-request", "workspace-write", "managed")
    requirements.resolve_permission_mode = lambda mode: (mode, violation)
    with pytest.raises(ValueError, match="managed policy disallows requested mode"):
        _managed_permission_projection("default")


def test_stream_resume_bounds_nested_args_once_and_preserves_the_object_contract(monkeypatch):
    from backend.agent import message
    original = message._bounded_event_record
    calls = []
    def bounded(*args, **kwargs):
        calls.append(kwargs["field_name"])
        return original(*args, **kwargs)
    monkeypatch.setattr(message, "_bounded_event_record", bounded)
    event = AgentEvent.stream_resume("owner", "assistant", tool_calls_pending=[{"id": "tool", "name": "read_file", "args": {"path": "src/app.py"}}])
    assert event.data["tool_calls_pending"][0]["args"] == {"path": "src/app.py"}
    assert calls == ["tool_calls_pending"]
    with pytest.raises(ValueError, match="args must be an object"):
        AgentEvent.stream_resume("owner", "assistant", tool_calls_pending=[{"id": "tool", "name": "read_file", "args": []}])
