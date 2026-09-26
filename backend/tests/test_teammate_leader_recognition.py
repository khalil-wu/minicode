"""A teammate recognizes its leader across leader turns.

The leader's run id is query-run scoped, so it changes every turn. A teammate
spawned in one turn must still accept the leader's shutdown/plan messages sent
from a later turn, and a shutdown addressed to ``name@team`` must reach the
mailbox even when the caller did not pass ``team_name`` explicitly.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import backend.hooks.manager as hook_manager_module
from backend.agent.run_context import RunContext
from backend.agent.runtime import AgentRuntime
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.hooks.manager import HookResult
from backend.llm.base import LLMAdapter, StreamEvent, StreamEventType
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import ToolExecutionContext
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.tools.swarm_tools import SendMessageTool


class _TeammateLLM(LLMAdapter):
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def stream_chat(self, messages, tools=None):
        self.prompts.append(str(messages[-1].content))
        yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=f"answer {len(self.prompts)}")
        yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(self, messages):
        return "x"


class _IdleHooks:
    def __init__(self) -> None:
        self.idle = asyncio.Event()

    def has_hooks(self, event):
        return False

    def bind_runtime(self, **_):
        return None

    async def run_task_created(self, **_):
        return HookResult()

    async def run_subagent_start(self, **_):
        return HookResult()

    async def run_subagent_stop(self, **_):
        return HookResult()

    async def run_task_completed(self, **_):
        return HookResult()

    async def run_teammate_idle(self, **_):
        self.idle.set()
        return HookResult()


async def _shutdown_after_spawn(
    tmp_path: Path, monkeypatch, *, later_turn: bool, pass_team_name: bool
) -> tuple[bool, str, bool, list[str]]:
    runtime = AgentRuntime(
        metrics_file=tmp_path / "metrics.jsonl",
        swarm_store_dir=tmp_path / "swarm",
        enable_lease_heartbeat=False,
    )
    runtime.start_run(run_id="leader-turn-1", conversation_id="conv")
    runtime.create_swarm_team(team_name="audit", conversation_id="conv", created_by="leader-turn-1")
    hooks = _IdleHooks()
    monkeypatch.setattr(hook_manager_module, "load_hook_manager_for_workspace", lambda *a, **k: hooks)
    monkeypatch.setattr(hook_manager_module, "register_hook_manager_for_session", lambda *a, **k: None)
    registry = ToolRegistry()
    checker = PermissionChecker(PermissionSettings(), workspace_root=tmp_path)
    llm = _TeammateLLM()
    tool = TaskTool(
        llm_provider=llm, tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=str(tmp_path / "artifacts")),
        permission_checker_provider=checker, agent_settings_provider=AgentSettings(max_iterations=2),
        token_budget_provider=TokenBudget(),
    )
    registry.register(tool)

    async def _emit(_type, _payload):
        return None

    def _ctx(run_id: str) -> ToolExecutionContext:
        return ToolExecutionContext(
            permission=checker.build_context(mode="bypass"), workspace_root=tmp_path,
            session_id="s", task_id=f"task-{run_id}", conversation_id="conv", emit_event=_emit,
            metadata={"run_id": run_id, "_tool_registry": registry},
            run_context=RunContext(agent_runtime=runtime, hook_manager=hooks),
        )

    try:
        launched = await tool.execute(
            {"description": "audit", "prompt": "Do step one.", "agent_type": "general-purpose",
             "name": "alice", "team_name": "audit"},
            context=_ctx("leader-turn-1"),
        )
        assert launched.status == "teammate_spawned", launched.content
        await asyncio.wait_for(hooks.idle.wait(), timeout=10)

        sender_run = "leader-turn-1"
        if later_turn:
            runtime.commit_terminal("leader-turn-1", "completed", summary="turn 1 done")
            runtime.start_run(run_id="leader-turn-2", conversation_id="conv")
            sender_run = "leader-turn-2"

        message = {"type": "shutdown_request", "request_id": "sd-1", "from": "team-lead"}
        args = {"recipient": "alice@audit", "message": json.dumps(message)}
        if pass_team_name:
            args["team_name"] = "audit"
        sent = await SendMessageTool().execute(args, context=_ctx(sender_run))
        assert not sent.is_error, sent.content

        finished = await runtime.wait_for_subagent("alice@audit", timeout=5)
        record = runtime.get_subagent("alice@audit")
        replies = [
            m.content
            for m in runtime.list_swarm_messages(conversation_id="conv")
            if "shutdown_response" in m.content
        ]
        return finished, str(record.status), bool(replies), list(llm.prompts)
    finally:
        for task in list(runtime._subagent_tasks.values()):
            task.cancel()
        await asyncio.gather(*runtime._subagent_tasks.values(), return_exceptions=True)
        runtime.close(release_lease=True)


@pytest.mark.parametrize("later_turn", [False, True])
def test_teammate_accepts_leader_shutdown_across_turns(tmp_path, monkeypatch, later_turn):
    finished, status, replied, prompts = asyncio.run(
        _shutdown_after_spawn(tmp_path, monkeypatch, later_turn=later_turn, pass_team_name=True)
    )
    assert finished is True
    assert status == "completed"
    assert replied is True
    # The shutdown was consumed as a lifecycle message, never fed to the model.
    assert not any("shutdown_request" in prompt for prompt in prompts)


def test_shutdown_to_named_teammate_derives_team_without_an_explicit_argument(tmp_path, monkeypatch):
    finished, status, replied, prompts = asyncio.run(
        _shutdown_after_spawn(tmp_path, monkeypatch, later_turn=True, pass_team_name=False)
    )
    assert finished is True and status == "completed" and replied is True
    assert not any("shutdown_request" in prompt for prompt in prompts)
