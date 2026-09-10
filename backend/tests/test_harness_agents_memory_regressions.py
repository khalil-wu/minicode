from __future__ import annotations

import asyncio
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.agent.agent_identity import coordination_agent_id
from backend.agent.mailbox_delivery import inject_parent_notifications, inject_subagent_mailbox_updates, subagent_mailbox_participant_id
from backend.agent.rollout_budget import RolloutBudget
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.agent.state import AgentState
from backend.agent.swarm_store import FileSwarmStore
from backend.agent.turn_budget_runtime import TurnBudgetRuntime
from backend.agent import worktree
from backend.agents import loader
from backend.config import AppConfig, LLMSettings
from backend.llm.base import UsageInfo
from backend.memory import generation
from backend.memory.job_store import MemoryJobStore
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tasks.manager import TaskManager
from backend.tasks import scheduler as scheduler_module
from backend.tools import agent_tools, subagent_control_tools, subagent_support
from backend.tools.agent_control_plane import AgentControlPlane
from backend.tools.base import ToolResult
from backend.tools.subagent_context import AgentExecutionProfile, _child_denied_tools, build_agent_execution_profile
from backend.tools.swarm_tools import SendMessageTool, TaskOutputTool, TaskUpdateTool


def test_swarm_reads_close_connections_and_preserve_forward_dependencies(tmp_path, monkeypatch):
    store = FileSwarmStore(tmp_path / "swarm")
    store.create_task({"task_id": "a", "title": "A", "conversation_id": "conv", "blocks": ["b"]})
    store.create_task({"task_id": "b", "title": "B", "conversation_id": "conv"})
    assert store.get_task("a", conversation_id="conv")["blocks"] == ["b"]
    assert store.get_task("b", conversation_id="conv")["blocked_by"] == ["a"]
    opened = []
    connect = store._connect

    def tracking_connect():
        connection = connect()
        opened.append(connection)
        return connection

    monkeypatch.setattr(store, "_connect", tracking_connect)
    store.get_task("a", conversation_id="conv")
    store.list_tasks(conversation_id="conv")
    store.list_subagents()
    for connection in opened:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_write_scopes_canonicalize_and_rebase_without_widening(tmp_path):
    root = tmp_path / "repo"
    child = root / "src"
    child.mkdir(parents=True)
    narrow = subagent_support._narrowed_subagent_scope_metadata
    with pytest.raises(ValueError, match="intersect"):
        narrow({"write_scope": ["src"]}, {"write_scope": ["src/../outside.py"]}, workspace_root=root)
    result = narrow({"write_scope": ["src"], "read_only": True}, {"read_only": False}, workspace_root=root, child_workspace_root=child)
    assert result == {"read_only": True, "write_scope": ["."]}
    isolated = narrow({"write_scope": ["src"]}, {"write_scope": ["src/a.py"]}, workspace_root=root, child_workspace_root=tmp_path / "isolated", isolated=True)
    assert isolated["write_scope"] == ["src/a.py"]
    external = tmp_path / "shared"
    inherited = narrow({"write_scope": [str(external)]}, {}, workspace_root=root, child_workspace_root=child)
    assert (child / inherited["write_scope"][0]).resolve() == external


def test_parallel_scope_overlap_uses_cwd_and_root_paths(tmp_path):
    scopes = subagent_support._exclusive_parallel_task_scopes
    assert scopes([{"description": "root", "write_scope": ["."]}, {"description": "src", "write_scope": ["src"]}], tmp_path) == []
    assert scopes([{"description": "one", "write_scope": ["src/a.py"]}, {"description": "two", "cwd": str(tmp_path / "src"), "write_scope": ["a.py"]}], tmp_path) == []
    assert scopes([{"description": "one", "isolation": "worktree", "write_scope": ["."]}, {"description": "two", "write_scope": ["."]}], tmp_path) == ["one", "two"]


class CaptureTaskTool(agent_tools.TaskTool):
    async def _run_parallel_subtasks(self, tasks, context):
        return ToolResult(content="dispatched", status="dispatched")

    async def _start_background_subtasks(self, *, tasks, context):
        return ToolResult(content="dispatched", status="dispatched")


@pytest.mark.asyncio
async def test_parallel_tasks_obey_workspace_and_parent_profile_admission(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    tool = CaptureTaskTool(llm_provider=object(), tool_registry_provider=object(), artifact_store=object(), permission_checker_provider=object())
    context = ToolExecutionContext(permission=PermissionContext(mode="auto"), workspace_root=root, run_context=RunContext())
    monkeypatch.setattr(agent_tools, "_resolve_subagent_llm", AsyncMock(return_value=None))
    tasks = [{"description": "outside", "prompt": "Read", "agent_type": "explore", "cwd": str(outside)}, {"description": "inside", "prompt": "Read", "agent_type": "explore"}]
    assert (await tool.execute(tasks[0], context)).is_error
    assert (await tool.execute({"parallel_tasks": tasks}, context)).is_error
    context = replace(context, metadata={"_agent_execution_profile": AgentExecutionProfile(role="teammate", delivery="persistent", delegation="foreground")})
    tasks[0].pop("cwd")
    result = await tool.execute({"parallel_tasks": tasks, "run_in_background": True}, context)
    assert result.is_error
    assert result.status != "dispatched"


def test_rollout_counts_each_child_turn_once_with_stable_agent_identity():
    budget = RolloutBudget(token_limit=10000)
    contexts = [{"agent_id": "child", "run_id": run, "agent_path": "root/child", "mailbox_epoch": 1} for run in ("turn-one", "turn-two")]
    for metadata, usage in zip(contexts, [UsageInfo(input_tokens=100, output_tokens=20), UsageInfo(input_tokens=80, output_tokens=10)]):
        runtime = TurnBudgetRuntime(state=None, tool_context=SimpleNamespace(metadata=metadata), usage=lambda: usage, rollout_budget=budget, deadlines=None, controller=None, termination=None)
        runtime.record_provider_usage_total(usage)
        runtime.record_provider_usage_total(usage)
        assert coordination_agent_id(metadata) == "child"
    assert budget.tokens_used() == 210


@pytest.fixture
def runtime(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "runtime" / "metrics.jsonl", swarm_store_dir=tmp_path / "swarm", enable_lease_heartbeat=False)
    runtime.start_run(run_id="root", conversation_id="conv")
    yield runtime
    runtime.close(release_lease=True)


@pytest.mark.asyncio
async def test_grandchild_notification_is_delivered_to_its_parent_only(runtime):
    child = runtime.start_subagent(subagent_id="child", parent_run_id="root", agent_type="general-purpose", background=True)
    runtime.start_run(run_id="child-turn", parent_run_id="root", conversation_id="conv", role="subagent", task_id="child", mailbox_epoch=child.mailbox_epoch)
    grandchild = runtime.start_subagent(subagent_id="grandchild", parent_run_id="child", agent_type="general-purpose", background=True)
    fence = {"agent_path": grandchild.agent_path, "mailbox_epoch": grandchild.mailbox_epoch}
    runtime.store_subagent_result("grandchild", status="completed", content="Grandchild result", **fence)
    runtime.complete_subagent("grandchild", **fence)
    assert runtime.list_parent_notifications(parent_run_id="root", conversation_id="conv") == []
    injected = []
    count = await inject_parent_notifications(ctx=SimpleNamespace(append_user=injected.append), state=AgentState(user_message="Read updates"), metadata={"agent_id": "child", "run_id": "child-turn", "agent_mode": "subagent", "agent_role": "subagent:general-purpose"}, runtime=runtime, run_context=RunContext(), parent_run_id="child", conversation_id="conv")
    assert count == 1
    assert "Grandchild result" in str(injected)


@pytest.mark.asyncio
async def test_teammate_mailbox_and_reply_use_stable_identity(runtime, tmp_path):
    child = runtime.start_subagent(subagent_id="reviewer@team", parent_run_id="root", agent_type="general-purpose", teammate_name="reviewer", team_name="team", background=True)
    runtime.start_run(run_id="turn-123", parent_run_id=child.subagent_id, conversation_id="conv", role="subagent", task_id=child.subagent_id, mailbox_epoch=child.mailbox_epoch)
    metadata = {"agent_id": child.subagent_id, "run_id": "turn-123", "agent_mode": "subagent", "agent_role": "subagent:general-purpose", "agent_path": child.agent_path, "mailbox_epoch": child.mailbox_epoch, "_agent_execution_profile": build_agent_execution_profile(team_mode=True)}
    context = ToolExecutionContext(permission=PermissionContext(source="teammate:general-purpose"), workspace_root=tmp_path, conversation_id="conv", task_id=child.subagent_id, metadata=metadata, run_context=RunContext(agent_runtime=runtime))
    runtime.send_swarm_message(sender_id="root", recipient_id=child.subagent_id, content="Please review", conversation_id="conv")
    injected = []
    count = await inject_subagent_mailbox_updates(ctx=SimpleNamespace(append_user=injected.append), state=AgentState(user_message="Continue"), metadata=metadata, conversation_id="conv", run_context=context.run_context)
    assert count == 1
    assert subagent_mailbox_participant_id(metadata) == child.subagent_id
    assert AgentControlPlane(context).root_run_id() == "root"
    assert not (await SendMessageTool().execute({"recipient": "parent", "message": "Received"}, context)).is_error
    assert runtime.list_swarm_messages(conversation_id="conv")[-1].sender_id == child.subagent_id


@pytest.mark.asyncio
async def test_task_output_completion_uses_same_hook_as_task_update(runtime):
    hook = SimpleNamespace(run_task_completed=AsyncMock(return_value=SimpleNamespace(blocked=True, message="Verification incomplete")))
    context = ToolExecutionContext(permission=PermissionContext(), conversation_id="conv", metadata={"run_id": "root"}, run_context=RunContext(agent_runtime=runtime, hook_manager=hook))
    for tool, extra in ((TaskUpdateTool(), {}), (TaskOutputTool(), {"content": "Report"})):
        task = runtime.create_swarm_task(title="Review", conversation_id="conv")
        result = await tool.execute({"task_id": task.task_id, "status": "completed", **extra}, context)
        assert result.is_error
        assert runtime.get_swarm_task(task.task_id).status != "completed"
    assert hook.run_task_completed.await_count == 2


@pytest.mark.asyncio
async def test_selected_same_model_default_effort_builds_new_adapter(tmp_path, monkeypatch):
    model = SimpleNamespace(id="same-model", reasoning=True, default_reasoning_effort="low", reasoning_effort_levels=("low", "high"), context_window=0)
    parent = SimpleNamespace(_provider="provider", _model="same-model", current_reasoning_effort=lambda: "high", supported_reasoning_efforts=lambda: ("low", "high"))
    child = SimpleNamespace(_provider="provider", _model="same-model", supported_reasoning_efforts=lambda: ("low", "high"))
    apply_effort = Mock()
    monkeypatch.setattr(subagent_support, "apply_model_thinking_level", apply_effort)
    build = Mock(return_value=child)
    monkeypatch.setattr("backend.llm.model_registry.create_session_llm", build)
    context = RunContext(subagent_parent_runtime={"llm": parent, "config": AppConfig(llm=LLMSettings(api_key="fixture")), "provider": "provider", "model": "same-model", "thinking_level": "high", "model_runtime": SimpleNamespace(get_model=lambda p, m: model)})
    result = await subagent_support._resolve_subagent_llm(parent, parent_metadata={}, run_context=context, agent_type="general-purpose", model_override="same-model", workspace_root=tmp_path)
    assert result.llm is child and result.owns_llm
    assert result.effort == "low"
    apply_effort.assert_called_once_with(child, model, "low")


@pytest.mark.asyncio
async def test_task_provider_override_resolves_in_target_provider(tmp_path, monkeypatch):
    parent = SimpleNamespace(_provider="original", _model="original-sonnet")
    target_model = SimpleNamespace(id="target-sonnet", name="Sonnet", reasoning=False, context_window=0)
    registry = SimpleNamespace(get_models=lambda provider: [target_model] if provider == "requested" else [], get_model=lambda provider, model: target_model if (provider, model) == ("requested", "target-sonnet") else None, get_provider=lambda provider: object())
    context = RunContext(subagent_parent_runtime={"llm": parent, "config": AppConfig(llm=LLMSettings(api_key="fixture")), "provider": "original", "model": "original-sonnet", "model_runtime": registry})
    resolved = await subagent_support._resolve_subagent_llm(parent, parent_metadata={}, run_context=context, agent_type="general-purpose", model_override="sonnet", provider_override="requested", workspace_root=tmp_path, build_adapter=False)
    assert (resolved.provider, resolved.model) == ("requested", "target-sonnet")
    with pytest.raises(ValueError, match="conflicts"):
        await subagent_support._resolve_subagent_llm(parent, parent_metadata={}, run_context=context, agent_type="general-purpose", model_override="other/model", provider_override="requested", workspace_root=tmp_path, build_adapter=False)


def test_new_memory_input_keeps_running_owner_and_queues_next_revision(tmp_path):
    store = MemoryJobStore(tmp_path / "memory.sqlite3")
    store.enqueue_phase2(now=100)
    args = {"lease_seconds": 60, "retry_limit": 3, "success_cooldown_seconds": 0}
    first = store.claim_phase2(worker_id="one", now=100, **args)
    store.enqueue_phase2(now=101)
    assert store.owns_phase2(first)
    assert store.claim_phase2(worker_id="two", now=101, **args) is None
    assert store.complete_phase2(first, [], now=102)
    second = store.claim_phase2(worker_id="two", now=103, **args)
    assert second.input_revision > first.input_revision
    assert not store.owns_phase2(first)
    assert store.complete_phase2(second, [], now=104)


@pytest.mark.asyncio
async def test_phase2_lease_loss_cancels_the_actual_writer(monkeypatch):
    coordinator = object.__new__(generation.MemoryGenerationCoordinator)
    coordinator.store = SimpleNamespace(heartbeat_phase2=lambda *args, **kwargs: False)
    monkeypatch.setattr(generation, "PHASE2_HEARTBEAT_SECONDS", 0)
    entered = asyncio.Event()

    async def writer():
        entered.set()
        await asyncio.Future()

    owner = asyncio.create_task(writer())
    await entered.wait()
    await coordinator._heartbeat_phase2(object(), owner)
    with pytest.raises(asyncio.CancelledError):
        await owner


def git(path, *args):
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def test_worktree_uses_requested_checkout_and_resume_preserves_original_head(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Regression Fixture")
    git(root, "config", "user.email", "fixture@local.invalid")
    (root / "version.txt").write_text("MAIN", encoding="utf-8")
    git(root, "add", "version.txt")
    git(root, "commit", "-qm", "main")
    alternate = tmp_path / "alternate"
    git(root, "worktree", "add", "-qb", "alternate", str(alternate))
    (alternate / "version.txt").write_text("ALTERNATE", encoding="utf-8")
    git(alternate, "commit", "-qam", "alternate")
    child, error = worktree.create_agent_worktree("fixture-child", alternate)
    assert child is not None, error
    assert (child.worktree_path / "version.txt").read_text(encoding="utf-8") == "ALTERNATE"
    original_head = child.head_commit
    (child.worktree_path / "version.txt").write_text("CHILD", encoding="utf-8")
    git(child.worktree_path, "commit", "-qam", "child")
    resumed = worktree.resume_agent_worktree(child.worktree_path, expected_repo_root=alternate, expected_subagent_id="fixture-child", expected_head_commit=original_head)
    assert resumed.head_commit == original_head
    assert worktree.has_worktree_changes(resumed)
    monkeypatch.setattr(worktree, "_ACTIVE_WORKTREE_PATHS", set())
    monkeypatch.setattr(worktree, "_STALE_SWEEP_DONE", set())
    worktree.cleanup_stale_worktrees(root)
    assert child.worktree_path.is_dir()


@pytest.mark.asyncio
async def test_managed_task_retains_source_until_cancel_cleanup_finishes():
    manager = TaskManager(max_tasks=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    managed = None

    async def source():
        manager.cancel(managed.id)
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            entered.set()
            await release.wait()

    managed = manager.create("fixture", source(), timeout=10)
    await entered.wait()
    await asyncio.sleep(0)
    assert managed.cleanup_pending
    assert not managed.source_task.done()
    assert not managed.is_terminal
    manager.prune()
    assert manager.get(managed.id) is managed
    release.set()
    await asyncio.gather(managed.source_task, managed.task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not managed.cleanup_pending
    assert managed.is_terminal


@pytest.mark.asyncio
async def test_scheduler_cancel_before_worker_start_finalizes_receipt(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler_module, "SCHEDULE_FILE", tmp_path / "schedule.json")
    scheduler = scheduler_module.TaskScheduler()
    task = scheduler.add_task("fixture", "unused", "* * * * *")
    run = scheduler.run_now(task.id)
    worker = scheduler._run_tasks[run.id]
    assert scheduler.cancel_run(run.id)
    await asyncio.gather(worker, return_exceptions=True)
    await asyncio.sleep(0)
    assert run.status == "cancelled"
    assert not run.cleanup_pending
    assert run.cleanup_completed_at
    assert run.id not in scheduler._run_tasks


def test_agent_editor_preserves_policy_frontmatter(tmp_path, monkeypatch):
    source = tmp_path / "agent.md"
    original = loader.AgentDefinition(name="fixture", description="fixture", prompt="Inspect", source="project", source_path=source, permission_mode="plan", background=True, has_output_schema=True)
    source.write_text(loader._render_agent_markdown(original), encoding="utf-8")
    monkeypatch.setattr(loader, "discover_agent_definitions", lambda *_: [original])
    loader.save_custom_agent("fixture", description="updated", prompt="New prompt", workspace_root=tmp_path, source="project", source_path=source)
    loaded = loader._parse_agent_file(source)
    assert (loaded.permission_mode, loaded.background, loaded.has_output_schema) == ("plan", True, True)
    assert loaded.prompt == "New prompt"


@pytest.mark.asyncio
async def test_task_stop_forwards_reason_and_lifecycle_tools_follow_profile(monkeypatch):
    target = SimpleNamespace(subagent_id="child")
    control = SimpleNamespace(can_use_operation=lambda _: True, interrupt=Mock(return_value=SimpleNamespace(interrupt_status="cancelled", target=target)))
    monkeypatch.setattr(subagent_control_tools, "require_runtime_from_context", lambda _: object())
    monkeypatch.setattr(subagent_control_tools, "_authorized_target", lambda *args, **kwargs: (control, target))
    result = await subagent_control_tools.TaskStopTool().execute({"subagent_id": "child", "reason": "  User changed scope  "})
    assert not result.is_error
    control.interrupt.assert_called_once_with(target, reason="User changed scope")
    profile = AgentExecutionProfile(agent_lifecycle=True)
    denied = _child_denied_tools(execution_profile=profile)
    assert not {"task_status", "task_stop"} & denied
    assert {"task_status", "task_stop"} <= _child_denied_tools(execution_profile=replace(profile, agent_lifecycle=False))
