"""Conversation state, queue, worktree, terminal and recovery ownership boundaries.

Each test drives the production entry the read-only audit reproduced with its
probes: the shared sidebar order file, deliverable identity by path domain,
cold conversation purges, batch child preparation, canonical child paths,
retained v2 targets, cross-window queue edits, missing managed worktrees,
large tool arguments on recovery, cleanup-pending terminals, scheduled-run
transcripts and command-side subagent resumes.
"""
from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import backend.agent.runtime as runtime_module
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.message_board import AgentMessageBoard, BoardActor
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.runtime_records import AgentRunRecord, SubagentRunRecord
from backend.agent.swarm_store import FileSwarmStore
from backend.artifact.store import ArtifactStore
from backend.config import AppConfig, LLMSettings
from backend.conversations.repository import ConversationRepository
from backend.conversations.sidebar_order import project_key
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.services.conversation_payload_service import cleanup_isolated_worktree
from backend.services.scheduled_task_runner import _scheduled_tool_call_records
from backend.services.workspace_service import is_path_within, main_worktree_root, worktree_has_local_changes
from backend.services.worktree_settings_service import (
    WorktreePreferences, managed_worktree_record, managed_worktree_root, register_managed_worktree,
    update_worktree_preferences,
)
from backend.terminal.session import TerminalSession, TerminalSessionManager
from backend.tools.agent_control_plane import AgentControlPlane
from backend.tools.agent_tools import TaskTool
from backend.tools.base import ToolResult
from backend.tools.subagent_support import _SubagentLLMResolution
from backend.tools.swarm_tools import SourceSendMessageTool
from backend.tools.write_file import WriteFileTool
from backend.ws.agent_runner import _reply_attachments_from_tool_calls
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.conversation_stream import frozen_stream_pages
from backend.ws.handlers import misc as agent_message_handlers
from backend.ws.run_manager import SessionRunManager
from backend.ws.stream_state import apply_stream_event, create_stream_state


def _runtime(folder: Path) -> AgentRuntime:
    folder.mkdir(parents=True, exist_ok=True)
    return AgentRuntime(metrics_file=folder / "metrics.jsonl", swarm_store_dir=folder / "swarm", enable_lease_heartbeat=False)


# ---------------------------------------------------------------- B-S01

IDS = {"a": "conv_aaaaaaaaaaaa", "b": "conv_bbbbbbbbbbbb", "old": "conv_cccccccccccc"}


def _sidebar_repository(storage: Path, target: Path, other: Path) -> ConversationRepository:
    repository = ConversationRepository(storage)
    repository.create_conversation(conversation_id=IDS["old"], workspace_root=str(target), title="Old target thread")
    repository.create_conversation(conversation_id=IDS["a"], workspace_root=str(other), title="Move A")
    repository.create_conversation(conversation_id=IDS["b"], workspace_root=str(other), title="Move B")
    repository.set_sidebar_sort_mode("manual", {str(target): [IDS["old"]]})
    return repository


def _target_order(repository: ConversationRepository, target: Path) -> list[str]:
    summaries = repository.list_conversations()
    return repository.get_sidebar_order(summaries)["sidebar_order"]["project_thread_orders"][project_key(str(target))]


def test_project_move_updates_the_shared_sidebar_order_under_the_store_lock(tmp_path):
    target, other = tmp_path / "target", tmp_path / "other"
    target.mkdir()
    other.mkdir()
    repository = _sidebar_repository(tmp_path / "store", target, other)
    locked_reads = []
    original_read = repository._read_sidebar_order

    def observed_read():
        locked_reads.append(repository._store_file_lock.is_locked)
        return original_read()

    repository._read_sidebar_order = observed_read
    try:
        repository.set_conversation_project(IDS["a"], str(target))
    finally:
        del repository._read_sidebar_order
    assert locked_reads == [True]
    assert _target_order(repository, target) == [IDS["a"], IDS["old"]]


def test_concurrent_project_moves_keep_both_sidebar_slots(tmp_path):
    target, other = tmp_path / "target", tmp_path / "other"
    target.mkdir()
    other.mkdir()
    storage = tmp_path / "store"
    first = _sidebar_repository(storage, target, other)
    second = ConversationRepository(storage)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(first.set_conversation_project, IDS["a"], str(target)),
                   pool.submit(second.set_conversation_project, IDS["b"], str(target))]
        for future in futures:
            future.result(timeout=30)
    order = _target_order(first, target)
    assert order in ([IDS["a"], IDS["b"], IDS["old"]], [IDS["b"], IDS["a"], IDS["old"]])
    assert first._read_sidebar_order()["project_thread_orders"][project_key(str(target))] == order


# ---------------------------------------------------------------- B-S02

@pytest.mark.parametrize("paths,expected", [
    (["/repo/out/Report.txt", "/repo/out/report.txt"], 2),
    (["C:/repo/out/Report.txt", "c:/repo/out/report.txt"], 1),
    (["//server/share/Report.txt", "//SERVER/share/report.txt"], 1),
])
def test_reply_attachments_fold_case_only_for_windows_paths(paths, expected):
    calls = [{"id": "tool-output", "name": "audit-output",
              "outputFiles": [{"path": path, "size": 10, "isImage": False} for path in paths]}]
    assert len(_reply_attachments_from_tool_calls(calls)) == expected


# ---------------------------------------------------------------- B-S03

def _board_rows(path: Path, tree: str) -> dict[str, int]:
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        return {table: db.execute(f"SELECT count(*) FROM {table} WHERE board=?", (tree,)).fetchone()[0]
                for table in ("channels", "posts", "subscriptions", "subscription_opt_outs", "deleted_boards")}


@pytest.mark.asyncio
async def test_cold_conversation_purge_deletes_the_message_board_like_the_live_purge(tmp_path, monkeypatch):
    outcomes = {}
    for cold in (True, False):
        folder = tmp_path / ("cold" if cold else "live")
        runtime = _runtime(folder)
        tree = "audit-board-conversation"
        run = runtime.start_run(conversation_id=tree, run_id="audit-board-run")
        context = ToolExecutionContext(permission=PermissionContext(), conversation_id=tree,
            metadata={"run_id": run.run_id}, run_context=RunContext(agent_runtime=runtime), workspace_root=tmp_path)
        board = AgentMessageBoard(runtime.message_boards.open(tree, "local"), BoardActor(context))
        await board.run("post", {"new_channel_name": "audit", "text": "private fixture"}, request_id="audit-board-post")
        board_path = runtime.message_boards.path
        before = _board_rows(board_path, tree)
        runtime.complete_run(run.run_id)
        if cold:
            runtime.close(release_lease=True)
            monkeypatch.setattr(runtime_module, "SWARM_DIR", folder / "swarm")
            monkeypatch.setattr(runtime_module, "default_runtime_if_initialized", lambda: None)
            removed = runtime_module.purge_persisted_conversation_runtime(tree)
        else:
            removed = runtime.purge_conversation(tree)
            runtime.close(release_lease=True)
        outcomes["cold" if cold else "live"] = (before, _board_rows(board_path, tree), removed["run_ids"])
    assert outcomes["cold"][0]["posts"] == outcomes["live"][0]["posts"] == 1
    cleared = {"channels": 0, "posts": 0, "subscriptions": 0, "subscription_opt_outs": 0, "deleted_boards": 1}
    assert outcomes["cold"][1] == outcomes["live"][1] == cleared
    assert outcomes["cold"][2] == outcomes["live"][2] == ["audit-board-run"]


# ---------------------------------------------------------------- B-A01

@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_batch_preparation_releases_prepared_adapters_on_any_exit(tmp_path, cancel):
    runtime = _runtime(tmp_path)
    root = runtime.start_run(conversation_id="audit-batch", run_id="audit-batch-root")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path,
        metadata={"run_id": root.run_id}, conversation_id=root.conversation_id, run_context=RunContext(agent_runtime=runtime))
    tool = TaskTool(artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))
    client = httpx.AsyncClient()
    blocked = asyncio.Event()
    calls = 0

    async def prepare(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _SubagentLLMResolution(client, AppConfig(llm=LLMSettings(api_key="audit-no-key")), "audit", "audit", "low", owns_llm=True)
        blocked.set()
        if not cancel:
            raise RuntimeError("second preparation failure")
        await asyncio.Event().wait()

    tool._prepare_subagent_llm_resolution = prepare
    task = asyncio.create_task(tool._start_background_subtasks(tasks=[
        {"description": "one", "prompt": "audit one", "agent_type": "default"},
        {"description": "two", "prompt": "audit two", "agent_type": "default"}], context=context))
    try:
        await asyncio.wait_for(blocked.wait(), 5)
        if cancel:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.is_error
        assert calls == 2
        assert client.is_closed
        assert runtime.list_runs(include_subagents=True)["subagents"] == []
    finally:
        await client.aclose()
        runtime.complete_run(root.run_id)
        runtime.close(release_lease=True)


# ---------------------------------------------------------------- B-A02

async def _parallel_names(folder: Path, names: list[str], *, background: bool):
    runtime = _runtime(folder)
    root = runtime.start_run(conversation_id="audit-names", run_id="audit-names-root")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=folder,
        metadata={"run_id": root.run_id}, conversation_id=root.conversation_id,
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v1"))
    tool = TaskTool(artifact_store=ArtifactStore(storage_dir=folder / "artifacts"))

    async def execution_without_provider(**kwargs):
        agent_id = kwargs["subagent_id"]
        requested = kwargs["subagent_metadata"]["_task_name"]
        record = runtime.start_subagent(subagent_id=agent_id, parent_run_id=root.run_id,
            agent_type="default", task_id=requested, agent_path_segment=requested)
        runtime.complete_subagent(agent_id, agent_path=record.agent_path, mailbox_epoch=record.mailbox_epoch)
        return ToolResult(content="execution seam", status="completed")

    tool._run_single_subtask = execution_without_provider
    tasks = [{"description": name, "prompt": "audit only", "agent_type": "default",
              "task_name": name, "_source_role_requested": False, "read_only": True} for name in names]
    try:
        result = await (tool._start_background_subtasks(tasks=tasks, context=context) if background
            else tool._run_parallel_subtasks(tasks, context))
        return result, AgentControlPlane(context).public_tree_paths()
    finally:
        runtime.complete_run(root.run_id)
        runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_foreground_parallel_tasks_reserve_canonical_child_paths(tmp_path):
    duplicate, paths = await _parallel_names(tmp_path / "duplicate", ["duplicate", "duplicate"], background=False)
    assert duplicate.is_error and duplicate.status == "blocked" and paths == {}
    distinct, paths = await _parallel_names(tmp_path / "distinct", ["first", "second"], background=False)
    assert not distinct.is_error and sorted(paths.values()) == ["/root/first", "/root/second"]
    background, paths = await _parallel_names(tmp_path / "background", ["duplicate", "duplicate"], background=True)
    assert background.is_error and background.status == "blocked" and paths == {}


# ---------------------------------------------------------------- B-A03

@pytest.mark.asyncio
@pytest.mark.parametrize("filler_runs", [0, 1000])
async def test_retained_child_targets_resolve_through_the_durable_owner(tmp_path, filler_runs):
    store = FileSwarmStore(tmp_path / "swarm")
    old_root = AgentRunRecord(run_id="old-root", conversation_id="retained-tree", status="completed", phase="final",
                              agent_path="old-root", started_at=1, completed_at=2)
    store.upsert_agent_run(old_root.to_dict())
    child = SubagentRunRecord(subagent_id="subagent-old-worker", parent_run_id=old_root.run_id, status="completed",
                              task_id="old_worker", agent_path="old-root/old_worker", mailbox_epoch=1,
                              resume_config={"task_name": "old_worker", "source_spawn_version": "v2"}, started_at=1, completed_at=2)
    store.upsert_subagent(child.to_dict())
    for index in range(filler_runs):
        store.upsert_agent_run(AgentRunRecord(run_id=f"recent-{index:04}", conversation_id="unrelated-history",
            status="completed", phase="final", agent_path=f"recent-{index:04}", started_at=10 + index, completed_at=20 + index).to_dict())
    runtime = _runtime(tmp_path)
    current = runtime.start_run(conversation_id=old_root.conversation_id, run_id="current-root")
    context = ToolExecutionContext(permission=PermissionContext(), workspace_root=tmp_path,
        conversation_id=current.conversation_id, metadata={"run_id": current.run_id},
        run_context=RunContext(agent_runtime=runtime, multi_agent_version="v2"))
    try:
        assert (runtime.get_run(old_root.run_id) is not None) == (filler_runs == 0)
        assert AgentControlPlane(context).resolve_persisted_message_target(child.subagent_id) is not None
        response = await SourceSendMessageTool().execute({"target": child.subagent_id, "message": "queue-only message"}, context)
        assert not response.is_error, response.content
    finally:
        runtime.complete_run(current.run_id)
        runtime.close(release_lease=True)


# ---------------------------------------------------------------- B-Q01

def _queued(conversation_id: str, suffix: str, content: str) -> UserCommand:
    return UserCommand("user_message", {
        "conversation_id": conversation_id, "content": content,
        "user_message_id": "user_" + suffix, "assistant_message_id": "assistant_" + suffix,
        "client_command_id": "client_" + suffix, "_client_queue_key": "window_a",
        "_submitted_content": content, "attachments": [],
    })


def test_an_unrelated_queue_append_keeps_another_windows_edit(tmp_path):
    storage = tmp_path / "conversations"
    repository = ConversationRepository(storage)
    edited, other = "conv_aaaaaaaaaaaa", "conv_bbbbbbbbbbbb"
    repository.create_conversation(conversation_id=edited, title="Edited queued prompt")
    repository.create_conversation(conversation_id=other, title="Other running conversation")
    window_a = SessionRunManager(SimpleNamespace(session_id="window_a", conversation_repo=ConversationRepository(storage)))
    window_b = SessionRunManager(SimpleNamespace(session_id="window_b", conversation_repo=ConversationRepository(storage)))
    try:
        window_a.enqueue_user_message(edited, _queued(edited, "original", "original prompt"))
        assert window_b.update_queued_user_message(edited, "assistant_original",
            {"content": "edited prompt retained by window B", "attachments": []}) is not None
        window_a.enqueue_user_message(other, _queued(other, "new", "new follow-up"))
        assert window_b.queued_user_messages(edited)[0].data["content"] == "edited prompt retained by window B"
        claimed = window_b.dequeue_user_message(edited)
        assert claimed.data["content"] == "edited prompt retained by window B"
        window_b.finish_user_message_dispatch(edited, claimed, succeeded=True)
    finally:
        for manager in (window_a, window_b):
            manager.stop_notification_wake_intake()
            manager.close_durable_queue()


# ---------------------------------------------------------------- B-W01

@pytest.fixture
def worktree_registry(tmp_path, monkeypatch):
    prefs = tmp_path / "prefs"
    prefs.mkdir()
    monkeypatch.setattr("backend.services.worktree_settings_service.WORKTREE_PREFERENCES_FILE", prefs / "worktree-preferences.json")
    monkeypatch.setattr("backend.services.worktree_settings_service.MANAGED_WORKTREES_FILE", prefs / "managed-worktrees.json")
    return prefs


def _missing_worktree_cleanup(tmp_path: Path, label: str, *, register: bool = True) -> tuple[dict, Path]:
    source = tmp_path / label / "actual-repository"
    source.mkdir(parents=True)
    update_worktree_preferences(WorktreePreferences(
        worktree_root=str(tmp_path / "custom-checkouts") if label == "custom" else ""))
    conversation_id = "conv_" + label.ljust(12, "0")[:12]
    missing = ((source / ".minicode" / "worktrees") if label == "legacy" else managed_worktree_root()) / conversation_id
    if register:
        register_managed_worktree(missing, source, branch="codex/audit")
        assert managed_worktree_record(missing) is not None
    repository = ConversationRepository(tmp_path / label / "conversations")
    record = repository.create_conversation(conversation_id=conversation_id, workspace_root=str(source), title="Missing checkout")
    record = repository.update_workspace_binding(record.id, workspace_root=str(missing), worktree_path=str(missing),
                                                 git_isolated=True, git_branch="codex/audit")
    cleanup = cleanup_isolated_worktree(record, current_workspace_root=None, main_worktree_root=main_worktree_root,
                                        is_path_within=is_path_within, worktree_has_local_changes=worktree_has_local_changes)
    return cleanup, source


@pytest.mark.parametrize("label", ["default", "legacy", "custom"])
def test_missing_managed_worktree_cleanup_restores_the_registered_repository(tmp_path, worktree_registry, label):
    cleanup, source = _missing_worktree_cleanup(tmp_path, label)
    assert cleanup["removed"] is True
    assert Path(cleanup["workspace_root"]) == source


def test_missing_unregistered_worktree_is_not_rebound_to_a_guessed_directory(tmp_path, worktree_registry):
    cleanup, _source = _missing_worktree_cleanup(tmp_path, "unknown", register=False)
    assert cleanup["removed"] is False
    assert "workspace_root" not in cleanup
    assert "not a registered MiniCode-managed worktree" in cleanup["error"]


# ---------------------------------------------------------------- R01

class _CaptureSession(SessionApprovalRuntimeMixin):
    async def send_event(self, event):
        self.sent.append(event)


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [200_000, 270_000])
async def test_recovery_replays_tool_arguments_the_live_turn_accepted(size):
    owner, message = "audit-recovery-budget", "audit-assistant"
    args = {"file_path": "audit-large.txt", "content": "x" * size}
    assert not WriteFileTool().validate_input(args)
    start = AgentEvent.tool_call("audit-write", "write_file", args, turn_id="audit-turn")
    start.data.update(conversation_id=owner, message_id=message)
    state = create_stream_state(owner, message, "audit-turn")
    streams = {owner: state}
    apply_stream_event(streams, owner, start.type, start.data)
    session = _CaptureSession()
    session.ws_manager = None
    session.active_conversation_id = owner
    session._conversation_streams = streams
    session.sent = []
    waiting = asyncio.create_task(asyncio.Event().wait())
    session.run_manager = SimpleNamespace(run_tasks={owner: waiting})
    try:
        await session.reemit_pending_state(owner)
        assert [event.type for event in session.sent] == ["stream_resume"]
        resumed = session.sent[0].data["tool_states"][0]
        assert (resumed["id"], resumed["name"]) == ("audit-write", "write_file")
        assert "x" * 10_000 in resumed["args"]["content"]
        assert session.sent[0].data["tool_calls_pending"][0]["id"] == "audit-write"
        assert len(frozen_stream_pages(owner, state)) == 1
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


# ---------------------------------------------------------------- TERM01

@pytest.mark.asyncio
async def test_cleanup_pending_terminal_stays_addressable_across_admissions():
    manager = TerminalSessionManager(max_sessions=3)
    stalled = TerminalSession("stalled", conversation_id="chat")
    stalled._process = SimpleNamespace(pid=1, returncode=0)
    exited = TerminalSession("exited", conversation_id="chat")
    exited._process = SimpleNamespace(pid=2, returncode=0)
    manager._sessions.update({stalled.session_id: stalled, exited.session_id: exited})

    async def incomplete_tree_close(_process):
        return False

    with patch("backend.terminal.session.terminate_process_tree", incomplete_tree_close):
        with pytest.raises(RuntimeError, match="could not be proven stopped"):
            await manager.destroy_session("stalled", conversation_id="chat")
    assert stalled.cleanup_pending and not stalled.is_alive

    async def inert_start(session):
        session._process = SimpleNamespace(pid=3, returncode=None)

    with patch.object(TerminalSession, "start", inert_start):
        await manager.create_session(conversation_id="chat")
    assert manager.get_session("stalled") is stalled
    assert manager.get_session("exited") is None
    manager.upsert_external_session("external", cwd=None, shell=None, pid=4, is_alive=True, conversation_id="chat")
    assert manager.get_session("stalled") is stalled
    assert manager.count == 3


# ---------------------------------------------------------------- AUTO-TRACE01

def test_scheduled_run_transcript_keeps_the_tool_process(tmp_path):
    repository = ConversationRepository(tmp_path / "conversations")
    record = repository.create_conversation(conversation_id="conv_schedulerun1", title="Scheduled run")
    rest_records = [
        {"tool_name": "read_file", "tool_call_id": "code_read", "tool_input": {"file_path": "notes.md"},
         "tool_output": "# notes", "status": "success", "artifact_id": None, "recoverable": True,
         "delegations": [], "command_actions": [], "recipient_ids": []},
        {"tool_name": "run_command", "tool_call_id": "code_run", "tool_input": {"command": "python check.py"},
         "tool_output": "", "status": "failed", "error_kind": "tool", "exit_code": 1,
         "delegations": [], "command_actions": [], "recipient_ids": []},
        {"tool_name": "orphan", "tool_call_id": "", "tool_input": {}},
    ]
    assistant_message = {
        "id": "assistant_schedule_run", "role": "assistant", "content": "done",
        "timestamp": "2026-11-01T00:00:00+00:00", "terminal_status": "completed", "termination_reason": "completed",
        "tool_calls": _scheduled_tool_call_records(rest_records),
        "metadata": {"source": "scheduled_task"},
    }
    assert repository.commit_turn_projection(record.id, assistant_message=assistant_message, context_snapshot={}) is not None
    stored = repository.get_conversation(record.id).transcript[-1]
    calls = {call["id"]: call for call in stored["tool_calls"]}
    assert set(calls) == {"code_read", "code_run"}
    assert calls["code_read"]["name"] == "read_file"
    assert calls["code_read"]["args"] == {"file_path": "notes.md"}
    assert calls["code_read"]["summary"] == "# notes"
    assert (calls["code_run"]["status"], calls["code_run"]["exitCode"]) == ("failed", 1)


# ---------------------------------------------------------------- A02 / H08

@pytest.mark.asyncio
async def test_command_side_subagent_resume_events_carry_the_owning_conversation(tmp_path, monkeypatch):
    repository = ConversationRepository(tmp_path / "conversations")
    owner = repository.create_conversation(conversation_id="conv_ownerownerow", title="Owner")

    class _ResumingTaskTool:
        async def resume_background_subtask(self, *, subagent_id, prompt, context):
            assert context.conversation_id == owner.id
            await context.emit_event("subagent.start", {"subagent_id": subagent_id, "prompt": prompt})
            await context.emit_event("subagent.done", {"subagent_id": subagent_id, "status": "completed"})

    registry = SimpleNamespace(get_tool=lambda name: _ResumingTaskTool() if name == "task" else None)
    session = SimpleNamespace(
        session_id="window", conversation_repo=repository,
        permission_context_for_conversation=lambda record, source: PermissionContext(),
        _conversation_tool_registry=lambda conversation_id: registry,
        send_event=AsyncMock(), emit_command_result=AsyncMock(),
    )
    subagent = SimpleNamespace(status="completed", parent_run_id="parent-run", task_id="worker")
    runtime = _runtime(tmp_path / "runtime")
    monkeypatch.setattr("backend.agent.runtime.default_runtime", lambda: runtime)
    monkeypatch.setattr(agent_message_handlers, "_load_subagent_record", lambda runtime, recipient: subagent)
    monkeypatch.setattr(agent_message_handlers, "resolve_command_scope",
                        lambda session, data: SimpleNamespace(workspace_root=str(tmp_path)))
    monkeypatch.setattr(agent_message_handlers, "_require_subagent_owner", lambda *args, **kwargs: owner.id)

    await agent_message_handlers._handle_agent_message(
        session, {"target": "subagent-old", "message": "continue the work"}, trigger_turn=True)

    events = [call.args[0] for call in session.send_event.await_args_list]
    assert [event.type for event in events] == ["subagent.start", "subagent.done"]
    assert all(event.data["conversation_id"] == owner.id for event in events)
    assert events[0].data["subagent_id"] == "subagent-old"
    session.emit_command_result.assert_awaited()
    assert session.emit_command_result.await_args.args[1] == "Stopped subagent resumed with the message."
    runtime.close(release_lease=True)
