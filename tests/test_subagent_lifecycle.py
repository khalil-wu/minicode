from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

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
from backend.tools.swarm_tools import TaskUpdateTool


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
