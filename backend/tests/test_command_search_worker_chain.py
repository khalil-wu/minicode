from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.execution_journal import ExecutionJournal
from backend.agent.message import AgentEvent
from backend.agent.query_journal import QueryJournalRecorder
from backend.agent.state import AgentState
from backend.agent.tool_execution import run_tool_with_timeout, store_result
from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.sandbox import SandboxPolicy, SandboxResult
from backend.subprocesses import SubprocessOutputLimitError, SubprocessTimeoutError, record_unproven_cleanup
from backend.tools import command_tool, search_support, search_tools
from backend.tools.registry import ToolRegistry


def owner(root: Path, *, call_id: str = "owned-call") -> ToolExecutionContext:
    permission = PermissionContext(mode="bypass", workspace_root=root)
    return ToolExecutionContext(
        permission=permission, workspace_root=root, conversation_id="search-owner",
        permission_checker=PermissionChecker(PermissionSettings(), root),
        tool_call_id=call_id, metadata={"run_id": "owned-run"},
    )


async def cancel_while_owned(task, entered, release, finished):
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and not finished.is_set()
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)
    assert finished.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grep", "glob"])
async def test_python_search_cancel_keeps_actual_file_reader_owned(tmp_path, monkeypatch, kind):
    (tmp_path / "source.py").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    loop = asyncio.get_running_loop()
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    original = search_support._iter_candidate_files

    def candidates(*args, **kwargs):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        try:
            yield from original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(search_support, "_iter_candidate_files", candidates)
    monkeypatch.setattr(search_tools, "_iter_candidate_files", candidates)
    tool = search_tools.GrepFilesTool() if kind == "grep" else search_tools.GlobFilesTool()
    args = {"pattern": "NEEDLE" if kind == "grep" else "**/*.py"}
    task = asyncio.create_task(tool.execute(args, owner(tmp_path)))
    try:
        await cancel_while_owned(task, entered, release, finished)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["grep_candidates", "grep_sort", "glob_sort"])
async def test_native_search_selection_and_sort_are_owned_workers(tmp_path, monkeypatch, phase):
    (tmp_path / "source.py").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", True)

    # These cases prove ownership of the real Python selection/sort workers;
    # the native process result is their input, not a runner prerequisite.
    async def spawn(*args, **kwargs):
        assert args[0] == "rg" and kwargs["cwd"] == str(tmp_path)
        return SimpleNamespace(args=args, returncode=0)

    async def communicate(proc, **_kwargs):
        filename = str(tmp_path / "source.py")
        if "--files" in proc.args:
            return (filename + "\0").encode(), b""
        if "--files-with-matches" in proc.args:
            return (filename + "\n").encode(), b""
        return (filename + ":1:NEEDLE\n").encode(), b""

    monkeypatch.setattr(search_support, "spawn_exec", spawn)
    monkeypatch.setattr(search_support, "communicate_bounded", communicate)
    loop = asyncio.get_running_loop()
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    if phase == "grep_candidates":
        original = search_support._ripgrep_path_batches

        def batches(*args, **kwargs):
            iterator = original(*args, **kwargs)
            class Batches:
                def __iter__(self):
                    return self
                def __next__(self):
                    assert threading.current_thread() is not threading.main_thread()
                    loop.call_soon_threadsafe(entered.set)
                    assert release.wait(3)
                    try:
                        return next(iterator)
                    finally:
                        finished.set()
            return Batches()

        monkeypatch.setattr(search_support, "_ripgrep_path_batches", batches)
    else:
        original = search_support._sort_glob_matches

        def sort(*args, **kwargs):
            assert threading.current_thread() is not threading.main_thread()
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(3)
            try:
                return original(*args, **kwargs)
            finally:
                finished.set()

        monkeypatch.setattr(search_support, "_sort_glob_matches", sort)
    tool = search_tools.GlobFilesTool() if phase == "glob_sort" else search_tools.GrepFilesTool()
    args = {"pattern": "**/*.py" if phase == "glob_sort" else "NEEDLE"}
    task = asyncio.create_task(tool.execute(args, owner(tmp_path)))
    try:
        await cancel_while_owned(task, entered, release, finished)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True], ids=["artifact", "persisted-output"])
async def test_foreground_output_cancel_finishes_owned_persistence_and_capture_cleanup(tmp_path, monkeypatch, fallback):
    raw = tmp_path / "captured.stdout"
    full = "BEGIN\n" + "中文🙂" * 20_000 + "\nFINAL_FAILURE\n"
    raw.write_bytes(full.encode("utf-8"))
    captured = []

    class Runner:
        def __init__(self, policy):
            self.policy = policy
        async def run(self, command, **kwargs):
            captured.append((command, self.policy, kwargs))
            return SandboxResult(stdout="preview", stderr="", exit_code=7, stdout_path=str(raw))

    monkeypatch.setattr(command_tool, "SandboxRunner", Runner)
    context = owner(tmp_path)
    context.sandbox_policy = SandboxPolicy.bypass()
    store = ArtifactStore(storage_dir=tmp_path / "artifacts")
    token = store.bind_owner(context.conversation_id, str(tmp_path))
    tool = command_tool.RunCommandTool(store)
    loop = asyncio.get_running_loop()
    entered, release, finished = asyncio.Event(), threading.Event(), threading.Event()
    references = []
    if fallback:
        def unavailable(*args, **kwargs):
            raise OSError("recorded artifact boundary failure")
        monkeypatch.setattr(store, "save", unavailable)
        original = command_tool.persist_tool_result
    else:
        original = store.save

    def persist(*args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3)
        try:
            reference = original(*args, **kwargs)
            references.append(reference)
            return reference
        finally:
            finished.set()

    monkeypatch.setattr(command_tool if fallback else store, "persist_tool_result" if fallback else "save", persist)
    task = asyncio.create_task(tool.execute({"command": "Write-Output 'recorded'", "max_chars": 32, "env": {"TASK_MODE": "audit"}}, context))
    try:
        await cancel_while_owned(task, entered, release, finished)
        assert not raw.exists() and len(references) == 1
        assert captured[0][0] == "Write-Output 'recorded'"
        assert captured[0][1].env_overrides["TASK_MODE"] == "audit"
        if fallback:
            persisted = references[0]
            assert Path(persisted.filepath).read_text(encoding="utf-8") == full
            from backend.agent.tool_result_persistence import is_tool_result_path
            assert is_tool_result_path(persisted.filepath, conversation_id=context.conversation_id, workspace_root=tmp_path)
            assert not is_tool_result_path(persisted.filepath, conversation_id="other", workspace_root=tmp_path)
        else:
            assert store.get(references[0], conversation_id=context.conversation_id, workspace_root=tmp_path) == full
            assert store.get(references[0], conversation_id="other", workspace_root=tmp_path) is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        store.reset_owner(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("file_type", ["vue", "unknown-type"])
async def test_python_search_does_not_silently_drop_an_unsupported_type(tmp_path, monkeypatch, file_type):
    (tmp_path / "unrelated.txt").write_text("NEEDLE", encoding="utf-8")
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", False)
    def unexpected(*args, **kwargs):
        pytest.fail("Search ignored the requested file type")
    monkeypatch.setattr(search_tools, "_grep_candidates", unexpected)
    result = await search_tools.GrepFilesTool().execute({"pattern": "NEEDLE", "type": file_type}, owner(tmp_path))
    assert result.is_error and "requires ripgrep" in result.content
    assert "unrelated.txt" not in result.content


def failing_native_boundary(monkeypatch, *, failure, reaped, entered=None):
    proc = SimpleNamespace(pid=4242, returncode=0)
    async def spawn(*args, **kwargs):
        return proc
    async def communicate(*args, **kwargs):
        if failure == "cancelled":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                record_unproven_cleanup(exc, reaped=reaped, proc=proc)
                raise
        exc = SubprocessTimeoutError("recorded native search deadline") if failure == "timeout" else SubprocessOutputLimitError(stream_name="stdout", limit_bytes=1, captured=b"x")
        record_unproven_cleanup(exc, reaped=reaped, proc=proc)
        raise exc
    monkeypatch.setattr(search_support, "spawn_exec", spawn)
    monkeypatch.setattr(search_support, "communicate_bounded", communicate)
    monkeypatch.setattr(search_tools, "_HAS_RIPGREP", True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grep", "glob"])
@pytest.mark.parametrize("failure", ["timeout", "overflow"])
@pytest.mark.parametrize("reaped", [False, True], ids=["pending-tree", "proven-exit"])
async def test_native_search_failure_keeps_actual_cleanup_through_result_and_journal(tmp_path, monkeypatch, kind, failure, reaped):
    failing_native_boundary(monkeypatch, failure=failure, reaped=reaped)
    context = owner(tmp_path)
    tool = search_tools.GrepFilesTool() if kind == "grep" else search_tools.GlobFilesTool()
    registry = ToolRegistry()
    registry.register(tool)
    call = ToolCallEvent(id=context.tool_call_id, name=tool.name, arguments={"pattern": "NEEDLE" if kind == "grep" else "**/*.py"})
    result = await run_tool_with_timeout(call, registry, context, iteration_id="iter:1")
    assert result.is_error
    if reaped:
        assert not result.cleanup_receipt.get("pending")
        return
    receipt = result.cleanup_receipt
    assert receipt["pending"] == 1 and receipt["completed"] is False
    resource = receipt["resource_cleanup"]
    assert resource["resource_id"] == "4242" and resource["resource_kind"] == "process"
    assert resource["conversation_id"] == context.conversation_id and resource["workspace_root"] == str(tmp_path)
    assert resource["reason"] == "subprocess_tree_survived_kill:pid=4242"
    state, builder = AgentState(user_message="search"), ContextBuilder(conversation_id=context.conversation_id, workspace_root=tmp_path)
    builder.append_assistant_tool_calls([call])
    event = store_result(call, result, builder, state, iteration_id="iter:1", turn_id="owned-run", tool_ctx=context, tool_registry=registry)
    assert event.data["cleanup_receipt"]["resource_cleanup"] == resource
    journal = ExecutionJournal("native-search", base_dir=tmp_path / "journal")
    recorder = QueryJournalRecorder(journal, {"run_id": "owned-run", "_tool_execution_context": context}, state, builder, None, context.conversation_id)
    recorder.record_event(AgentEvent(type="tool_call", data={"id": call.id, "name": call.name, "args": call.arguments, "turn_id": "owned-run", "iteration_id": "iter:1"}))
    recorder.record_event(event)
    recorder.runtime_terminal_receipt_recorded = True
    recorder.record_terminal(AgentEvent.done(status="failed", reason="native_search_failed"))
    terminal = [entry.payload for entry in journal.read_events() if entry.event_type == "terminal"][-1]
    assert terminal["cleanup_pending_count"] == 1 and terminal["manual_recovery_required"]
    assert terminal["cleanup_receipts"][call.id]["resource_cleanup"] == resource
    assert "pending/manual recovery" in result.to_context_string()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["grep", "glob"])
async def test_cancelled_search_wrapper_cannot_mark_unreaped_tree_as_complete(tmp_path, monkeypatch, kind):
    entered = asyncio.Event()
    failing_native_boundary(monkeypatch, failure="cancelled", reaped=False, entered=entered)
    context = owner(tmp_path)
    tool = search_tools.GrepFilesTool() if kind == "grep" else search_tools.GlobFilesTool()
    registry = ToolRegistry()
    registry.register(tool)
    call = ToolCallEvent(id=context.tool_call_id, name=tool.name, arguments={"pattern": "NEEDLE" if kind == "grep" else "**/*.py"})
    task = asyncio.create_task(run_tool_with_timeout(call, registry, context, iteration_id="iter:1"))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=3)
    for _ in range(3):
        await asyncio.sleep(0)
    assert not context.pending_cleanup_tasks
    receipt = context.cleanup_receipts[call.id]
    assert receipt["pending"] == 1 and receipt["completed"] is False
    assert receipt["resource_cleanup"]["resource_id"] == "4242"
    assert receipt["resource_cleanup"]["conversation_id"] == context.conversation_id
    assert receipt["manual_recovery_required"] and not receipt["retry_safe"]
    assert not receipt.get("cleanup_completed_after_deadline")
