from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.worktree import create_agent_worktree
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.hooks.manager import HookResult
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_tools import TaskStatusTool, TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.swarm_tools import SendMessageTool
from backend.tools.toolsets import SESSION_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy


class _RecordingLLM(LLMAdapter):
    def __init__(self) -> None:
        self.calls = 0

    async def stream_chat(self, messages, tools=None, metadata=None):
        self.calls += 1
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="Audit answer")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "Audit answer"


@pytest.mark.asyncio
async def test_continuation_metrics_sum_query_turns_once(startup_case, monkeypatch):
    from backend.agent.message import AgentEvent
    from backend.agent.query_engine import QueryEngine
    from backend.llm.base import ToolCallEvent, UsageInfo
    from backend.tools import agent_tools

    run_ids = []
    agent_ids = []
    turns = [UsageInfo(input_tokens=100, output_tokens=20, cost_usd=0.1), UsageInfo(input_tokens=80, output_tokens=10, cost_usd=0.2)]

    async def submit(self, submission):
        index = len(run_ids)
        run_ids.append(submission.runtime.metadata["run_id"])
        agent_ids.append(submission.runtime.metadata["agent_id"])
        submission.runtime.run_context.llm_turn_context = SimpleNamespace(usage=turns[index])
        submission.state.reply = f"Turn {index + 1} answer"
        submission.state.iterations = index + 1
        submission.state.tool_calls = [ToolCallEvent(id=f"tool-{index}", name="read_file", arguments={})]
        yield AgentEvent.done(status="completed", reason="success")

    monkeypatch.setattr(QueryEngine, "submit", submit)
    monkeypatch.setattr(agent_tools._SubagentLifecycleOwner, "after_subagent_stop", AsyncMock(side_effect=[SimpleNamespace(action="continue", prompt="Continue the review"), SimpleNamespace(action="terminal")]))
    child_id, result = await _launch(startup_case, "foreground")
    assert not result.is_error
    assert len(set(run_ids)) == 2
    assert agent_ids == [child_id, child_id]
    stored = startup_case.runtime._subagent_results[child_id]
    assert stored.iterations == 3
    assert stored.tool_call_count == 2
    assert stored.usage["input_tokens"] == 180
    assert stored.usage["output_tokens"] == 30
    assert stored.usage["cost_usd"] == pytest.approx(0.3)


@pytest.fixture
def startup_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    runtime.start_run(run_id="parent", conversation_id="conversation")
    runtime.create_swarm_team(
        team_name="audit", conversation_id="conversation", created_by="parent"
    )
    hooks = SimpleNamespace(
        has_hooks=lambda event: False,
        bind_runtime=lambda **kwargs: None,
        run_task_created=AsyncMock(return_value=HookResult()),
        run_subagent_start=AsyncMock(return_value=HookResult()),
        run_subagent_stop=AsyncMock(return_value=HookResult()),
        run_teammate_idle=AsyncMock(return_value=HookResult(
            prevent_continuation=True, stop_reason="audit finished"
        )),
    )
    monkeypatch.setattr(
        "backend.hooks.manager.load_hook_manager_for_workspace",
        lambda *args, **kwargs: hooks,
    )
    monkeypatch.setattr(
        "backend.hooks.manager.register_hook_manager_for_session",
        lambda *args, **kwargs: None,
    )
    model = _RecordingLLM()
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(), workspace_root=workspace)
    tool = TaskTool(
        llm_provider=model,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=checker,
        agent_settings_provider=AgentSettings(max_iterations=2),
        token_budget_provider=TokenBudget(),
    )
    registry.register(tool)
    events = []
    workers = []

    async def emit(event_type, payload):
        events.append((event_type, payload))
        if event_type == "subagent.start":
            worker = runtime._subagent_tasks.get(payload["subagent_id"])
            if worker is not None:
                workers.append(worker)

    context = ToolExecutionContext(
        permission=checker.build_context(mode="bypass"),
        workspace_root=workspace,
        session_id="session",
        conversation_id="conversation",
        task_id="parent-task",
        emit_event=emit,
        metadata={"run_id": "parent", "_tool_registry": registry},
        run_context=RunContext(agent_runtime=runtime, hook_manager=hooks),
    )
    yield SimpleNamespace(
        runtime=runtime, tool=tool, context=context, hooks=hooks,
        model=model, events=events, workers=workers, workspace=workspace,
    )
    runtime.close(release_lease=True)


async def _launch(case, delivery: str, **arguments):
    launch = await case.tool.execute({
        "description": "Audit startup",
        "prompt": "Answer the audit question.",
        **({"run_in_background": True} if delivery == "background" else {}),
        **({"name": "alice", "team_name": "audit"} if delivery == "teammate" else {}),
        **arguments,
    }, context=case.context)
    return launch.runtime_metadata["subagent_id"], launch


def _assert_terminal(case, subagent_id: str, status: str, epoch: int = 1):
    record = case.runtime.get_subagent(subagent_id)
    result = case.runtime._subagent_results[subagent_id]
    assert record.status == result.status == status
    assert record.mailbox_epoch == result.mailbox_epoch == epoch
    assert case.runtime._swarm_store.get_subagent(subagent_id) == record.to_dict()
    assert case.runtime._swarm_store.get_subagent_result(subagent_id) == result.to_dict()
    assert [event_type for event_type, _payload in case.events] == [
        "subagent.start", "subagent.done"
    ]
    done = case.events[-1][1]
    assert done["status"] == done["record"]["status"] == done["result"]["status"] == status
    assert done["agent_path"] == record.agent_path
    assert done["mailbox_epoch"] == epoch
    assert done["result"]["iterations"] == 0
    assert done["result"]["tool_call_count"] == 0
    return record, result


@pytest.mark.parametrize("delivery", ["foreground", "background", "teammate", "resume", "teammate_resume"])
@pytest.mark.parametrize("fault", ["veto", "hook", "journal_open", "journal_read", "context"])
def test_startup_failures_finish_the_owned_incarnation(
    startup_case, monkeypatch: pytest.MonkeyPatch, delivery: str, fault: str
):
    case = startup_case

    async def run():
        subagent_id = ""
        resuming = delivery in {"resume", "teammate_resume"}
        if resuming:
            subagent_id, _first = await _launch(
                case, "teammate" if delivery == "teammate_resume" else "foreground"
            )
            assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
            await asyncio.gather(*case.workers)
            assert case.runtime.get_subagent(subagent_id).status == "completed"
            case.events.clear()
            case.workers.clear()
        previous_calls = case.model.calls
        expected_error = "audit startup failure"
        if fault == "veto":
            case.hooks.run_subagent_start.return_value = HookResult(
                blocked=True, message=expected_error
            )
        elif fault == "hook":
            case.hooks.run_subagent_start.side_effect = RuntimeError(expected_error)
        elif fault == "journal_open":
            monkeypatch.setattr(case.runtime, "execution_journal", Mock(
                side_effect=OSError(expected_error)
            ))
            expected_error = "journal could not be opened"
        elif fault == "journal_read":
            monkeypatch.setattr("backend.tools.agent_tools.ExecutionJournal.read_events", Mock(
                side_effect=OSError(expected_error)
            ))
            expected_error = "journal could not be opened"
        else:
            monkeypatch.setattr(case.tool, "_build_subagent_context_builder", Mock(
                side_effect=RuntimeError(expected_error)
            ))
        if resuming:
            await case.tool.resume_background_subtask(
                subagent_id=subagent_id, prompt="Continue the audit.", context=case.context
            )
        else:
            subagent_id, launch = await _launch(case, delivery)
            if delivery == "foreground":
                assert launch.status == "failed"
                assert launch.is_error
                assert expected_error in launch.content
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        await asyncio.sleep(0)
        record, result = _assert_terminal(
            case, subagent_id, "failed", epoch=2 if resuming else 1
        )
        assert expected_error in result.error
        assert case.model.calls == previous_calls
        assert not case.runtime._subagent_tasks
        assert not case.runtime._subagent_slot_reservations
        status = await TaskStatusTool().execute(
            {"subagent_id": subagent_id}, context=case.context
        )
        assert status.status == record.status
        assert expected_error in status.content

    asyncio.run(run())


@pytest.mark.parametrize("delivery", ["foreground", "background", "teammate", "resume"])
def test_cancellation_during_start_hook_finishes_the_owned_incarnation(startup_case, delivery):
    case = startup_case

    async def run():
        entered = asyncio.Event()

        async def hold_start(**kwargs):
            entered.set()
            await asyncio.Event().wait()

        if delivery == "resume":
            subagent_id, first = await _launch(case, "foreground")
            assert first.status == "completed"
            case.events.clear()
        previous_calls = case.model.calls
        case.hooks.run_subagent_start.side_effect = hold_start
        if delivery == "foreground":
            worker = asyncio.create_task(_launch(case, delivery))
        elif delivery == "resume":
            await case.tool.resume_background_subtask(
                subagent_id=subagent_id, prompt="Continue the audit.", context=case.context
            )
            worker = case.workers[-1]
        else:
            subagent_id, _launch_result = await _launch(case, delivery)
            worker = case.workers[-1]
        await asyncio.wait_for(entered.wait(), timeout=5)
        subagent_id = case.events[0][1]["subagent_id"]
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=1)
        _assert_terminal(case, subagent_id, "cancelled", epoch=2 if delivery == "resume" else 1)
        assert case.model.calls == previous_calls

    asyncio.run(run())


@pytest.mark.parametrize("cancelled", [False, True], ids=["failure", "cancellation"])
def test_late_startup_exit_does_not_overwrite_a_new_incarnation(startup_case, cancelled):
    case = startup_case

    async def run():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def hold_start(**kwargs):
            entered.set()
            await release.wait()
            raise RuntimeError("old startup failure")

        case.hooks.run_subagent_start.side_effect = hold_start
        subagent_id, _launch_result = await _launch(case, "background")
        await asyncio.wait_for(entered.wait(), timeout=5)
        worker = case.workers[-1]
        old = case.runtime.get_subagent(subagent_id)
        case.runtime.complete_subagent(
            subagent_id, "cancelled", agent_path=old.agent_path, mailbox_epoch=old.mailbox_epoch
        )
        replacement = case.runtime.start_subagent(
            subagent_id=subagent_id, parent_run_id="parent", agent_type=old.agent_type,
            session_id="session", background=True,
        )
        if cancelled:
            worker.cancel()
        else:
            release.set()
        await asyncio.gather(worker, return_exceptions=True)
        assert case.runtime.get_subagent(subagent_id).to_dict() == replacement.to_dict()
        assert case.runtime._subagent_results.get(subagent_id) is None
        assert case.runtime._swarm_store.get_subagent_result(subagent_id) is None
        assert [event_type for event_type, _payload in case.events] == ["subagent.start"]
        assert case.model.calls == 0

    asyncio.run(run())


def _prepare_git(workspace: Path):
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "seed.txt").write_text("audit fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "seed.txt"], cwd=workspace, check=True)
    subprocess.run([
        "git", "-c", "user.name=Audit Fixture", "-c", "user.email=audit@example.invalid",
        "commit", "-qm", "fixture",
    ], cwd=workspace, check=True)


@pytest.mark.parametrize("changed", [False, True], ids=["clean", "changed"])
@pytest.mark.parametrize("cancelled", [False, True], ids=["veto", "cancellation"])
def test_startup_exit_cleans_only_its_owned_worktree(startup_case, changed, cancelled):
    case = startup_case
    _prepare_git(case.workspace)

    async def run():
        entered = asyncio.Event()
        worktree_path = None

        async def stop_start(**kwargs):
            nonlocal worktree_path
            record = case.runtime.get_subagent(kwargs["subagent_id"])
            worktree_path = Path(record.resume_config["worktree_path"])
            if changed:
                (worktree_path / "user-change.txt").write_text("keep this\n", encoding="utf-8")
            entered.set()
            if cancelled:
                await asyncio.Event().wait()
            return HookResult(blocked=True, message="audit veto")

        case.hooks.run_subagent_start.side_effect = stop_start
        subagent_id, _launch_result = await _launch(case, "background", isolation="worktree")
        await asyncio.wait_for(entered.wait(), timeout=5)
        if cancelled:
            case.workers[-1].cancel()
        await asyncio.gather(*case.workers, return_exceptions=True)
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=1)
        record, _result = _assert_terminal(case, subagent_id, "cancelled" if cancelled else "failed")
        assert worktree_path.exists() is changed
        resource, = record.cleanup_resources
        assert resource["resource_id"] == str(worktree_path)
        assert resource["state"] == ("retained" if changed else "released")
        if changed:
            assert (worktree_path / "user-change.txt").read_text(encoding="utf-8") == "keep this\n"

    asyncio.run(run())


@pytest.mark.parametrize("fault", ["team", "worktree", "resource", "resume_config"])
def test_early_setup_failure_publishes_a_terminal_result(startup_case, monkeypatch, fault):
    case = startup_case
    if fault == "team":
        monkeypatch.setattr(case.runtime, "add_swarm_team_member", Mock(return_value=None))
    elif fault == "worktree":
        monkeypatch.setattr("backend.agent.worktree.create_agent_worktree", Mock(
            return_value=(None, "audit worktree unavailable")
        ))
    elif fault == "resource":
        _prepare_git(case.workspace)
        monkeypatch.setattr(case.runtime, "register_subagent_cleanup_resource", Mock(return_value=False))
    else:
        monkeypatch.setattr(case.runtime, "update_subagent_resume_config", Mock(return_value=None))

    async def run():
        subagent_id, _launch_result = await _launch(
            case, "teammate" if fault == "team" else "background",
            **({"isolation": "worktree"} if fault in {"worktree", "resource"} else {}),
        )
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        _assert_terminal(case, subagent_id, "failed")
        assert not (case.workspace / ".minicode" / "worktrees" / subagent_id).exists()
        assert case.model.calls == 0

    asyncio.run(run())


def test_cancellation_waits_for_inflight_worktree_acquisition(startup_case, monkeypatch):
    case = startup_case
    _prepare_git(case.workspace)
    release = threading.Event()

    async def run():
        acquired = asyncio.Event()
        loop = asyncio.get_running_loop()
        created = []

        def delayed_create(subagent_id, workspace):
            result = create_agent_worktree(subagent_id, workspace)
            created.append(result[0])
            loop.call_soon_threadsafe(acquired.set)
            release.wait(timeout=5)
            return result

        monkeypatch.setattr("backend.agent.worktree.create_agent_worktree", delayed_create)
        subagent_id, _launch_result = await _launch(case, "background", isolation="worktree")
        try:
            await asyncio.wait_for(acquired.wait(), timeout=5)
            worker = case.workers[-1]
            worker.cancel()
            await asyncio.sleep(0)
            assert not worker.done()
            release.set()
            await asyncio.gather(worker, return_exceptions=True)
            assert await case.runtime.wait_for_subagent(subagent_id, timeout=1)
            record, _result = _assert_terminal(case, subagent_id, "cancelled")
            worktree, = created
            assert not worktree.worktree_path.exists()
            resource, = record.cleanup_resources
            assert resource["resource_id"] == str(worktree.worktree_path)
            assert resource["state"] == "released"
            assert case.model.calls == 0
        finally:
            release.set()

    asyncio.run(run())


@pytest.mark.parametrize("persisted_policy", [False, True], ids=["absent-policy", "explicit-policy"])
def test_named_teammate_resume_keeps_identity_membership_and_permission(startup_case, persisted_policy):
    case = startup_case
    if persisted_policy:
        case.context.metadata[SESSION_TOOLSET_POLICY_METADATA_KEY] = ToolsetPolicy(
            disabled_tools=frozenset({"bash"})
        )

    async def run():
        subagent_id, _first = await _launch(case, "teammate", mode="auto")
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        initial = case.runtime.get_subagent(subagent_id)
        assert initial.status == "completed"
        assert initial.teammate_name == "alice"
        case.runtime._subagent_task_metadata.pop(subagent_id, None)
        sent = await SendMessageTool().execute({
            "recipient": "alice", "message": "Continue the audit with the same identity."
        }, context=case.context)
        assert not sent.is_error, sent.content
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        resumed = case.runtime.get_subagent(subagent_id)
        assert resumed.status == "completed"
        assert resumed.agent_path == initial.agent_path
        assert resumed.mailbox_epoch == initial.mailbox_epoch + 1
        assert resumed.teammate_name == initial.teammate_name
        assert resumed.permission_mode == initial.permission_mode
        assert resumed.resume_config["session_toolset_policy"] == initial.resume_config["session_toolset_policy"]
        team, = case.runtime.list_swarm_teams(conversation_id="conversation", team_name="audit")
        assert [member.id for member in team.members] == [subagent_id]
        assert case.runtime.resolve_subagent_name("alice") == subagent_id
        assert case.model.calls == 2
        assert [payload["status"] for kind, payload in case.events if kind == "subagent.done"] == [
            "completed", "completed"
        ]

    asyncio.run(run())


@pytest.mark.parametrize("policy", [False, [], {"availability_filters": [False]}])
def test_resume_still_rejects_malformed_persisted_policy(startup_case, policy):
    case = startup_case

    async def run():
        subagent_id, initial = await _launch(case, "foreground")
        assert initial.status == "completed"
        record = case.runtime.get_subagent(subagent_id)
        record.resume_config["session_toolset_policy"] = policy
        case.runtime._swarm_store.upsert_subagent(
            record.to_dict(), expected_owner_token=record.runtime_owner_token,
        )
        before = case.runtime.get_subagent(subagent_id).to_dict()
        with pytest.raises(RuntimeError, match="invalid persisted tool capability policy"):
            await case.tool.resume_background_subtask(
                subagent_id=subagent_id, prompt="Continue.", context=case.context
            )
        assert case.runtime.get_subagent(subagent_id).to_dict() == before
        assert case.model.calls == 1

    asyncio.run(run())


def test_resume_snapshot_failure_finishes_the_new_incarnation(startup_case, monkeypatch):
    case = startup_case

    async def run():
        subagent_id, initial = await _launch(case, "foreground")
        assert initial.status == "completed"
        case.events.clear()
        monkeypatch.setattr("backend.agent.context.ContextBuilder.load_snapshot", Mock(
            side_effect=ValueError("audit snapshot could not be loaded")
        ))
        await case.tool.resume_background_subtask(
            subagent_id=subagent_id, prompt="Continue.", context=case.context
        )
        assert await case.runtime.wait_for_subagent(subagent_id, timeout=5)
        await asyncio.gather(*case.workers)
        _record, result = _assert_terminal(case, subagent_id, "failed", epoch=2)
        assert "audit snapshot could not be loaded" in result.error
        assert case.model.calls == 1

    asyncio.run(run())
