from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from backend.agent.checkpoint import load_latest_run_checkpoint
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.hooks.manager import (
    HookEvent,
    HookResult,
)
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_support import _SubagentLifecycleOwner
from backend.tools.swarm_tools import SendMessageTool, TaskUpdateTool


def _runtime(tmp_path: Path) -> AgentRuntime:
    return AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )


class _TwoAnswerLLM(LLMAdapter):
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self._answers = ["draft answer", "revised answer"]

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        self.prompts.append(messages[-1].content)
        answer = self._answers.pop(0)
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=answer)
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages: list[LLMMessage]) -> str:
        return "revised answer"


class _OrdinaryChildHooks:
    def __init__(self, runtime: AgentRuntime) -> None:
        self.runtime = runtime
        self.stop_calls: list[tuple[str, str]] = []
        self.subagent_id = ""

    def has_hooks(self, event: HookEvent) -> bool:
        return event == HookEvent.SUBAGENT_STOP

    def bind_runtime(self, **_: Any) -> None:
        return None

    async def run_task_created(self, **_: Any) -> HookResult:
        return HookResult()

    async def run_subagent_start(self, **_: Any) -> HookResult:
        return HookResult()

    async def run_subagent_stop(
        self,
        *,
        subagent_id: str,
        summary: str,
        **_: Any,
    ) -> HookResult:
        self.subagent_id = subagent_id
        record = self.runtime.get_subagent(subagent_id)
        self.stop_calls.append((summary, record.status if record is not None else "missing"))
        if len(self.stop_calls) == 1:
            return HookResult(
                blocked=True,
                message="Revise the answer before stopping.",
                feedback="Revise the answer before stopping.",
            )
        return HookResult()

    async def run_stop(self, *_: Any, **__: Any) -> HookResult:
        raise AssertionError("ordinary child dispatched Stop instead of SubagentStop")

    async def run_task_completed(self, **_: Any) -> HookResult:
        raise AssertionError("ordinary child dispatched TaskCompleted")

    async def run_teammate_idle(self, **_: Any) -> HookResult:
        raise AssertionError("ordinary child dispatched TeammateIdle")


def test_ordinary_child_subagent_stop_veto_continues_before_durable_seal(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    runtime = _runtime(tmp_path)
    llm = _TwoAnswerLLM()
    hooks = _OrdinaryChildHooks(runtime)
    monkeypatch.setattr(
        "backend.hooks.manager.load_hook_manager_for_workspace",
        lambda *_args, **_kwargs: hooks,
    )
    monkeypatch.setattr(
        "backend.hooks.manager.register_hook_manager_for_session",
        lambda *_args, **_kwargs: None,
    )
    checker = PermissionChecker(PermissionSettings())
    tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=ToolRegistry(),
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=checker,
        agent_settings_provider=AgentSettings(max_iterations=3),
        token_budget_provider=TokenBudget(),
    )

    async def run() -> Any:
        return await tool.execute(
            {
                "description": "Inspect lifecycle",
                "prompt": "Return a lifecycle summary.",
                "agent_type": "explore",
            },
            context=ToolExecutionContext(
                permission=checker.build_context(mode="confirm"),
                session_id="session-1",
                task_id="parent-1",
                run_context=RunContext(
                    agent_runtime=runtime,
                    hook_manager=hooks,
                ),
            ),
        )

    try:
        result = asyncio.run(run())
        record = runtime.get_subagent(hooks.subagent_id)
    finally:
        runtime.close(release_lease=True)

    assert result.status == "completed"
    assert "revised answer" in result.content
    assert hooks.stop_calls == [
        ("draft answer", "running"),
        ("revised answer", "running"),
    ]
    assert any("Revise the answer" in prompt for prompt in llm.prompts)
    assert record is not None
    assert record.status == "completed"


class _TeammateGateHooks:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.block_task_once = True
        self.block_idle_once = True

    async def run_task_completed(self, *, task_id: str, **_: Any) -> HookResult:
        self.calls.append(("task_completed", task_id))
        if self.block_task_once:
            self.block_task_once = False
            return HookResult(
                blocked=True,
                message="Finish the owned task first.",
                feedback="Finish the owned task first.",
            )
        return HookResult()

    async def run_teammate_idle(self, *, teammate_name: str, **_: Any) -> HookResult:
        self.calls.append(("teammate_idle", teammate_name))
        if self.block_idle_once:
            self.block_idle_once = False
            return HookResult(
                blocked=True,
                message="Send the final handoff.",
                feedback="Send the final handoff.",
            )
        return HookResult()


def test_teammate_exit_gates_only_owned_in_progress_tasks_before_idle(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    owned = runtime.create_swarm_task(
        title="Owned",
        assignee="alice",
        status="in_progress",
        team_name="core",
        conversation_id="conversation-1",
    )
    runtime.create_swarm_task(
        title="Already done",
        assignee="alice",
        status="completed",
        team_name="core",
        conversation_id="conversation-1",
    )
    runtime.create_swarm_task(
        title="Other owner",
        assignee="bob",
        status="in_progress",
        team_name="core",
        conversation_id="conversation-1",
    )
    runtime.create_swarm_task(
        title="Other team",
        assignee="alice",
        status="in_progress",
        team_name="other",
        conversation_id="conversation-1",
    )
    runtime.create_swarm_task(
        title="Other conversation",
        assignee="alice",
        status="in_progress",
        team_name="core",
        conversation_id="conversation-2",
    )
    hooks = _TeammateGateHooks()
    owner = _SubagentLifecycleOwner(
        subagent_id="alice@core",
        agent_type="general-purpose",
        runtime=runtime,
        hook_manager=hooks,
        team_mode=True,
        teammate_name="alice",
        team_name="core",
        conversation_id="conversation-1",
    )
    state = AgentState(user_message="done")
    owner.bind_turn_state(state)
    async def run() -> tuple[Any, Any, Any]:
        first = await owner.after_subagent_stop(state)
        second = await owner.after_subagent_stop(state)
        third = await owner.after_subagent_stop(state)
        return first, second, third

    try:
        first, second, third = asyncio.run(run())
    finally:
        runtime.close(release_lease=True)

    assert (first.action, first.gate, first.task_id) == (
        "continue",
        "task_completed",
        owned.task_id,
    )
    assert second.action == "continue"
    assert second.gate == "teammate_idle"
    assert third.action == "idle"
    assert hooks.calls == [
        ("task_completed", owned.task_id),
        ("task_completed", owned.task_id),
        ("teammate_idle", "alice"),
        ("task_completed", owned.task_id),
        ("teammate_idle", "alice"),
    ]


class _PersistentTeammateHooks(_OrdinaryChildHooks):
    def __init__(self, runtime: AgentRuntime, trigger: str) -> None:
        super().__init__(runtime)
        self.trigger = trigger
        self.idle = asyncio.Event()
        self.gate_calls = 0

    async def run_subagent_stop(self, *, subagent_id: str, summary: str, **_: Any) -> HookResult:
        record = self.runtime.get_subagent(subagent_id)
        self.stop_calls.append((summary, record.status))
        return HookResult()

    def _gate(self) -> HookResult:
        self.gate_calls += 1
        if self.gate_calls == 1:
            return HookResult(
                blocked=self.trigger != "mailbox",
                message="Finish the second audit step.",
            )
        return HookResult(prevent_continuation=True, stop_reason="audit finished")

    async def run_task_completed(self, **_: Any) -> HookResult:
        assert self.trigger == "task_gate"
        return self._gate()

    async def run_teammate_idle(self, **_: Any) -> HookResult:
        self.idle.set()
        return self._gate()


@pytest.mark.parametrize("trigger", ["task_gate", "idle_gate", "mailbox"])
@pytest.mark.parametrize("legacy_run", [False, True], ids=["fresh", "legacy-run"])
def test_named_teammate_keeps_mailbox_identity_across_query_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str, legacy_run: bool
) -> None:
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    runtime = _runtime(tmp_path)
    runtime.start_run(run_id="parent", conversation_id="conversation")
    runtime.create_swarm_team(team_name="audit", conversation_id="conversation", created_by="parent")
    agent_id = "alice@audit"
    if legacy_run:
        runtime.start_run(run_id=agent_id, conversation_id="conversation")
        runtime.commit_terminal(agent_id, summary="old turn")
    if trigger == "task_gate":
        runtime.create_swarm_task(
            title="Owned task", assignee="alice", status="in_progress",
            team_name="audit", conversation_id="conversation",
        )
    llm = _TwoAnswerLLM()
    hooks = _PersistentTeammateHooks(runtime, trigger)
    monkeypatch.setattr("backend.hooks.manager.load_hook_manager_for_workspace", lambda *_args, **_kwargs: hooks)
    monkeypatch.setattr("backend.hooks.manager.register_hook_manager_for_session", lambda *_args, **_kwargs: None)
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=checker,
        agent_settings_provider=AgentSettings(max_iterations=2),
        token_budget_provider=TokenBudget(),
    )
    registry.register(tool)
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        events.append((event_type, payload))

    context = ToolExecutionContext(
        permission=checker.build_context(mode="bypass"),
        workspace_root=tmp_path,
        session_id="session",
        task_id="parent-task",
        conversation_id="conversation",
        emit_event=emit,
        metadata={"run_id": "parent", "_tool_registry": registry},
        run_context=RunContext(agent_runtime=runtime, hook_manager=hooks),
    )

    async def run() -> None:
        try:
            launched = await tool.execute({
                "description": "Audit named teammate",
                "prompt": "Answer the first audit step.",
                "agent_type": "general-purpose",
                "name": "alice", "team_name": "audit",
            }, context=context)
            assert launched.status == "teammate_spawned"
            if trigger == "mailbox":
                await asyncio.wait_for(hooks.idle.wait(), timeout=5)
                sent = await SendMessageTool().execute({
                    "recipient": agent_id, "message": "Answer the next audit step."
                }, context=context)
                assert not sent.is_error
            assert await runtime.wait_for_subagent(agent_id, timeout=5)
            await asyncio.sleep(0)
        finally:
            tasks = list(runtime._subagent_tasks.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    try:
        asyncio.run(run())
        record = runtime.get_subagent(agent_id)
        assert record.status == "completed"
        assert record.mailbox_epoch == 1
        assert hooks.stop_calls == [("draft answer", "running"), ("revised answer", "running")]
        assert hooks.gate_calls == 2
        assert len(llm.prompts) == 2
        expected_prompt = "Answer the next audit step." if trigger == "mailbox" else "Finish the second audit step."
        assert expected_prompt in llm.prompts[-1]
        runs = [
            row for row in runtime._swarm_store.list_agent_runs(conversation_id="conversation")
            if row["parent_run_id"] == agent_id
        ]
        run_ids = {row["run_id"] for row in runs}
        assert len(run_ids) == 2
        assert agent_id not in run_ids
        assert all(row["status"] == "completed" for row in runs)
        assert all(row["agent_path"] == record.agent_path for row in runs)
        assert all(row["mailbox_epoch"] == record.mailbox_epoch for row in runs)
        checkpoint = load_latest_run_checkpoint(agent_id, conversation_id="conversation")
        assert checkpoint is not None
        assert checkpoint.run_id in run_ids
        assert checkpoint.reply == "revised answer"
        assert len([item for item in events if item[0] == "subagent.start"]) == 1
        done = [payload for event_type, payload in events if event_type == "subagent.done"]
        assert len(done) == 1
        assert done[0]["subagent_id"] == agent_id
        assert done[0]["mailbox_epoch"] == 1
        assert done[0]["agent_path"] == record.agent_path
        assert done[0]["result"]["content"] == "revised answer"
        if legacy_run:
            assert runtime.get_run(agent_id).summary == "old turn"
            assert runtime.get_run(agent_id).status == "completed"
    finally:
        runtime.close(release_lease=True)


class _TaskUpdateHooks:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def run_task_completed(self, **payload: Any) -> HookResult:
        self.calls.append({key: str(value) for key, value in payload.items()})
        if len(self.calls) == 1:
            return HookResult(
                blocked=True,
                message="Completion evidence is missing.",
                feedback="Completion evidence is missing.",
            )
        return HookResult()


def test_task_update_runs_completion_gate_before_status_write(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    task = runtime.create_swarm_task(
        title="Ship lifecycle",
        description="Close the lifecycle contract.",
        assignee="alice",
        status="in_progress",
        team_name="core",
        conversation_id="conversation-1",
    )
    hooks = _TaskUpdateHooks()
    context = ToolExecutionContext(
        permission=PermissionChecker(PermissionSettings()).build_context(mode="auto"),
        conversation_id="conversation-1",
        run_context=RunContext(
            agent_runtime=runtime,
            hook_manager=hooks,
        ),
        metadata={
            "teammate_name": "alice",
            "team_name": "core",
        },
    )
    tool = TaskUpdateTool()

    async def run() -> tuple[Any, Any, Any, Any]:
        blocked = await tool.execute(
            {"task_id": task.task_id, "status": "completed"},
            context,
        )
        after_block = runtime.get_swarm_task(
            task.task_id,
            conversation_id="conversation-1",
        )
        completed = await tool.execute(
            {"task_id": task.task_id, "status": "completed"},
            context,
        )
        after_complete = runtime.get_swarm_task(
            task.task_id,
            conversation_id="conversation-1",
        )
        return blocked, after_block, completed, after_complete

    try:
        blocked, after_block, completed, after_complete = asyncio.run(run())
    finally:
        runtime.close(release_lease=True)

    assert blocked.status == "blocked"
    assert after_block.status == "in_progress"
    assert completed.is_error is False
    assert after_complete.status == "completed"
    assert hooks.calls == [
        {
            "task_id": task.task_id,
            "subject": "Ship lifecycle",
            "description": "Close the lifecycle contract.",
            "teammate_name": "alice",
            "team_name": "core",
        },
        {
            "task_id": task.task_id,
            "subject": "Ship lifecycle",
            "description": "Close the lifecycle contract.",
            "teammate_name": "alice",
            "team_name": "core",
        },
    ]
