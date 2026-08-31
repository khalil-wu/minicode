from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from backend.agent.message import AgentEvent
from backend.agent.context import ContextBuilder
from backend.agent.runtime import AgentRuntime
from backend.agent.run_context import RunContext
from backend.agent.state import ToolCallRecord
from backend.artifact.store import ArtifactStore
from backend.config import AgentSettings, PermissionSettings, TokenBudget
from backend.llm.base import LLMAdapter, LLMMessage, StreamEvent, StreamEventType, ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult
from backend.tools.agent_tools import (
    TaskStatusTool,
    TaskStopTool,
    TaskTool,
    _exclusive_parallel_task_scopes,
    _subagent_metadata,
    _subagent_prompt_cache_fork_diagnostic,
)
from backend.tools.registry import ToolRegistry
from backend.tools.subagent_context import AgentExecutionProfile
from backend.tools.toolsets import SESSION_TOOLSET_POLICY_METADATA_KEY, ToolsetPolicy
from backend.tools.swarm_tools import SendMessageTool
from backend.hooks.manager import HookResult


def _subagent_fence(runtime: AgentRuntime, subagent_id: str) -> dict[str, object]:
    record = runtime.get_subagent(subagent_id)
    assert record is not None
    return {"agent_path": record.agent_path, "mailbox_epoch": record.mailbox_epoch}


def _task_tool() -> TaskTool:
    return TaskTool(
        llm_provider=object(),
        tool_registry_provider=ToolRegistry(),
        artifact_store=object(),
        permission_checker_provider=lambda: PermissionChecker(PermissionSettings()),
    )


def _subagent_id_from(content: str) -> str:
    match = re.search(r"subagent-[0-9a-f]{8}", content)
    assert match is not None, content
    return match.group(0)


def _subagent_ids_from(content: str) -> list[str]:
    return re.findall(r"subagent-[0-9a-f]{8}", content)


def test_task_tool_description_carries_cc_delegation_disciplines() -> None:
    description = _task_tool().model_description()

    assert "name the files or modules they own and the exact change" in description
    assert "do not read its transcript/output file" in description
    assert "still running" in description
    assert "give status, not a guess" in description


def test_in_process_teammate_cannot_spawn_background_subagent() -> None:
    async def run() -> None:
        result = await _task_tool().execute(
            {
                "description": "nested background",
                "prompt": "Inspect the repository.",
                "run_in_background": True,
            },
            context=ToolExecutionContext(
                permission=PermissionContext(source="teammate:reviewer"),
                session_id="reviewer@release-team",
                task_id="reviewer@release-team",
            ),
        )

        assert result.is_error is True
        assert result.status == "blocked"
        assert "cannot spawn background agents" in result.content

    asyncio.run(run())


def test_in_process_teammate_cannot_spawn_another_named_teammate() -> None:
    async def run() -> None:
        result = await _task_tool().execute(
            {
                "description": "nested teammate",
                "prompt": "Inspect the repository.",
                "name": "nested-reviewer",
                "team_name": "release-team",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(source="teammate:reviewer"),
                session_id="reviewer@release-team",
                task_id="reviewer@release-team",
            ),
        )

        assert result.is_error is True
        assert result.status == "blocked"
        assert "cannot spawn other teammates" in result.content

    asyncio.run(run())


def test_explicit_execution_profile_overrides_legacy_teammate_source() -> None:
    async def run() -> None:
        result = await _task_tool().execute(
            {
                "description": "nested inspect",
                "prompt": "Inspect the repository.",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(source="teammate:legacy"),
                session_id="legacy-child",
                task_id="legacy-child",
                metadata={
                    "_agent_execution_profile": AgentExecutionProfile(
                        role="subagent",
                        delivery="foreground",
                        delegation="none",
                        task_coordination=True,
                        message_coordination=True,
                    )
                },
            ),
        )

        assert result.is_error is True
        assert result.status == "blocked"
        assert "Recursive subagent delegation is blocked" in result.content

    asyncio.run(run())


def test_hierarchical_execution_profile_can_delegate_foreground_or_background() -> None:
    async def run() -> None:
        profile = AgentExecutionProfile(
            role="subagent",
            delivery="background",
            delegation="any",
            task_coordination=True,
            message_coordination=True,
            constrained_async_surface=True,
        )
        context = ToolExecutionContext(
            permission=PermissionContext(source="subagent:codex"),
            session_id="codex-child",
            task_id="codex-child",
            metadata={"_agent_execution_profile": profile},
        )

        foreground = await _task_tool().execute(
            {"prompt": "Inspect the repository."},
            context=context,
        )
        background = await _task_tool().execute(
            {
                "prompt": "Inspect the repository.",
                "run_in_background": True,
            },
            context=context,
        )

        # Missing-description validation happens after the delegation guard.
        # Reaching it proves both delivery modes were authorized by capability,
        # not by a provider/source-name branch.
        assert "Missing description argument" in foreground.content
        assert "Missing description argument" in background.content
        assert foreground.status != "blocked" or "Recursive" not in foreground.content
        assert background.status != "blocked" or "background agents" not in background.content

    asyncio.run(run())


def test_task_tool_can_start_background_subagent(monkeypatch, tmp_path):
    asyncio.run(_test_task_tool_can_start_background_subagent(monkeypatch, tmp_path))


def test_background_task_created_hook_gates_queueing(monkeypatch, tmp_path):
    async def run() -> None:
        calls: list[str] = []

        class _HookManager:
            async def run_task_created(self, **kwargs):
                calls.append(str(kwargs.get("task_id") or ""))
                return HookResult(blocked=True, message="background task denied")

        async def should_not_start(**_kwargs):
            raise AssertionError("blocked background task must not start a child loop")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", should_not_start)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        hook_manager = _HookManager()
        ctx = ToolExecutionContext(
            permission=PermissionContext(),
            session_id="session-background-veto",
            task_id="parent-task",
            metadata={"run_id": "parent-run"},
            run_context=RunContext(
                agent_runtime=runtime,
                hook_manager=hook_manager,
            ),
        )

        result = await _task_tool().execute(
            {
                "description": "background inspect",
                "prompt": "inspect files",
                "agent_type": "explore",
                "read_only": True,
                "run_in_background": True,
            },
            context=ctx,
        )
        assert result.is_error is True
        assert result.status == "blocked"
        assert "background task denied" in result.content
        assert len(calls) == 1
        assert runtime.list_runs(include_subagents=True)["subagents"] == []

    asyncio.run(run())


def test_parallel_task_can_run_explicitly_in_background(monkeypatch, tmp_path):

    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

    async def fake_run_agent_loop(**kwargs):
        started.set()
        await release.wait()
        yield AgentEvent.agent_message_completed("worker result")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        ctx = ToolExecutionContext(
            permission=PermissionContext(),
            session_id="session-parallel-default",
            task_id="parent-task",
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )

        result = await _task_tool().execute(
            {
                "parallel_tasks": [
                    {"description": "inspect alpha", "prompt": "inspect alpha", "read_only": True},
                    {"description": "inspect beta", "prompt": "inspect beta", "read_only": True},
                ],
                "run_in_background": True,
            },
            context=ctx,
        )

        assert result.status == "running"
        assert "background subagents" in result.content
        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        for _ in range(40):
            if all(item.get("status") == "completed" for item in runtime.list_runs(include_subagents=True)["subagents"]):
                break
            await asyncio.sleep(0.01)
        assert len(runtime.list_runs(include_subagents=True)["subagents"]) == 2

    asyncio.run(run())


def test_single_read_only_task_can_run_explicitly_in_background(monkeypatch, tmp_path):

    async def run() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

    async def fake_run_agent_loop(**kwargs):
        started.set()
        await release.wait()
        yield AgentEvent.agent_message_completed("worker result")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        ctx = ToolExecutionContext(
            permission=PermissionContext(),
            session_id="session-single-default",
            task_id="parent-task",
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )

        result = await asyncio.wait_for(
            _task_tool().execute(
                {
                    "description": "inspect alpha",
                    "prompt": "inspect alpha",
                    "agent_type": "explore",
                    "run_in_background": True,
                },
                context=ctx,
            ),
            timeout=0.25,
        )

        assert result.status == "running"
        subagent_id = _subagent_id_from(result.content)
        await asyncio.wait_for(started.wait(), timeout=1)
        assert runtime.get_subagent_snapshot(subagent_id, include_result=False)["status"] == "running"

        release.set()
        for _ in range(40):
            snapshot = runtime.get_subagent_snapshot(subagent_id, include_result=False)
            if snapshot and snapshot.get("status") == "completed":
                break
            await asyncio.sleep(0.01)
        assert runtime.get_subagent_snapshot(subagent_id, include_result=False)["status"] == "completed"

    asyncio.run(run())


def test_single_read_only_task_waits_for_result_by_default(monkeypatch, tmp_path):
    async def run() -> None:
        async def fake_run_agent_loop(**kwargs):
            yield AgentEvent.agent_message_completed("inline worker result")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool().execute(
            {
                "description": "inspect inline",
                "prompt": "inspect inline",
                "agent_type": "explore",
            },
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-single-barrier",
                task_id="parent-task",
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.status == "completed"
        assert "inline worker result" in result.content

    asyncio.run(run())


def test_parallel_prompt_similarity_is_not_a_scheduling_gate():
    scopes = _exclusive_parallel_task_scopes([
        {"description": "review one", "prompt": "same prompt"},
        {"description": "review two", "prompt": "same prompt"},
    ])
    assert scopes == ["review one", "review two"]


def test_parallel_scope_rejects_only_structured_write_collisions():
    assert _exclusive_parallel_task_scopes([
        {"description": "API", "prompt": "same", "write_scope": ["src/api"]},
        {"description": "UI", "prompt": "same", "write_scope": ["src/api/routes"]},
    ]) == []


def test_parallel_scopes_allow_same_description_with_distinct_prompts():
    scopes = _exclusive_parallel_task_scopes([
        {"description": "查询省会天气", "prompt": "查询石家庄天气"},
        {"description": "查询省会天气", "prompt": "查询太原天气"},
        {"description": "查询省会天气", "prompt": "查询济南天气"},
    ])
    assert scopes == ["查询省会天气", "查询省会天气", "查询省会天气"]


def test_parallel_scopes_allow_duplicate_text_for_independent_review():
    assert _exclusive_parallel_task_scopes([
        {"description": "查询省会天气", "prompt": "查询石家庄天气"},
        {"description": "查询省会天气", "prompt": "查询石家庄天气"},
    ]) == ["查询省会天气", "查询省会天气"]


def test_parallel_scopes_do_not_apply_semantic_similarity_to_chinese_text():
    assert _exclusive_parallel_task_scopes([
        {"description": "探索项目结构和核心代码", "prompt": "梳理项目结构与核心代码"},
        {"description": "探索项目核心模块", "prompt": "梳理项目核心模块"},
    ]) == ["探索项目结构和核心代码", "探索项目核心模块"]


def test_existing_task_text_is_not_used_as_a_delegation_gate():
    scopes = _exclusive_parallel_task_scopes([
        {"description": "first review", "prompt": "inspect core modules"},
        {"description": "second review", "prompt": "inspect core module"},
    ])
    assert scopes == ["first review", "second review"]


def test_parallel_scopes_preserve_explicit_generic_worker_labels():
    scopes = _exclusive_parallel_task_scopes([
        {"description": "Agent 1", "prompt": "查询石家庄天气"},
        {"description": "Agent 2", "prompt": "查询太原天气"},
        {"description": "Agent 3", "prompt": "查询济南天气"},
    ])
    assert scopes == ["Agent 1", "Agent 2", "Agent 3"]


def test_parallel_scopes_keep_distinct_prompt_suffixes_after_long_common_prefix():
    prefix = "审查共享模块的输入输出契约与错误处理，" * 8
    scopes = _exclusive_parallel_task_scopes([
        {"description": "Agent 1", "prompt": f"{prefix}只检查 API 路由"},
        {"description": "Agent 2", "prompt": f"{prefix}只检查 WebSocket 事件"},
    ])

    assert len(scopes) == 2
    assert scopes[0] != scopes[1]


def test_parallel_scopes_allow_generic_labels_with_identical_prompts():
    assert _exclusive_parallel_task_scopes([
        {"description": "Agent 1", "prompt": "查询石家庄、太原、济南天气"},
        {"description": "Agent 2", "prompt": "查询石家庄、太原、济南天气"},
        {"description": "Agent 3", "prompt": "查询石家庄、太原、济南天气"},
    ]) == ["Agent 1", "Agent 2", "Agent 3"]


def test_task_tool_emits_task_lifecycle_hooks(monkeypatch, tmp_path):
    asyncio.run(_test_task_tool_emits_task_lifecycle_hooks(monkeypatch, tmp_path))


def test_read_only_subagent_does_not_inherit_parent_live_permission_provider(
    monkeypatch,
    tmp_path,
):
    async def run() -> None:
        parent_live_permission = {"value": PermissionContext(mode="bypass")}

        async def fake_run_agent_loop(**kwargs):
            assert kwargs["permission_context"].mode == "plan"
            assert kwargs["permission_context"].sandbox_mode == "read-only"
            assert "permission_context_provider" not in kwargs["metadata"]
            assert "permission_mode_setter" not in kwargs["metadata"]
            assert "command_prompt_allow_rules_setter" not in kwargs["metadata"]
            yield AgentEvent.agent_message_completed("child stayed read-only")
            yield AgentEvent.done(status="completed")

        monkeypatch.setattr(
            "backend.agent.query_engine.run_agent_loop",
            fake_run_agent_loop,
        )
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="inspect files",
            prompt="inspect files deeply",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(mode="confirm"),
                session_id="session-child-permission-snapshot",
                task_id="parent-task",
                metadata={"run_id": "parent-run"},
                run_context=RunContext(
                    agent_runtime=runtime,
                    permission_context_provider=lambda: parent_live_permission["value"],
                    permission_mode_setter=lambda _mode: None,
                    command_prompt_allow_rules_setter=lambda _rules: None,
                ),
            ),
        )

        parent_live_permission["value"] = PermissionContext(mode="bypass")
        assert result.is_error is False
        assert "child stayed read-only" in result.content

    asyncio.run(run())


def test_task_tool_marks_query_engine_failure_as_error(monkeypatch, tmp_path):
    async def run() -> None:
        async def fake_run_agent_loop(**kwargs):
            if False:
                yield AgentEvent.agent_message_completed("never")
            raise RuntimeError("child provider failed")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="inspect files",
            prompt="inspect files deeply",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-1",
                task_id="parent-task",
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.is_error is True
        # Runtime failures are deliberately distinguished from provider API
        # errors; the user-facing text stays generic while logs retain the
        # original exception.
        assert "internal runtime processing failed" in result.content

    asyncio.run(run())


def test_subagent_failure_keeps_the_output_it_already_produced(monkeypatch, tmp_path):
    """An error that ends a child early must not discard its work.

    A rate limit or provider error can hit after the child has already answered
    most of the question. Throwing that away forces the parent to re-delegate
    from scratch, so a run that produced text is returned as unfinished rather
    than as a bare failure.
    """
    async def run() -> None:
        emitted_events: list[tuple[str, dict]] = []

        async def emit(event_type: str, data: dict) -> None:
            emitted_events.append((event_type, data))

        async def fake_run_agent_loop(**kwargs):
            yield AgentEvent.agent_message_completed(
                "Found the bug: rewrite.py mishandles the cwd fixture.",
                source="model_final",
            )
            raise RuntimeError("provider rate limit")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="find the bug",
            prompt="find the bug",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-partial",
                task_id="parent-task",
                emit_event=emit,
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        # The work survives, and the parent can see it is incomplete.
        assert "rewrite.py mishandles the cwd fixture" in result.content
        assert "did not finish" in result.content
        assert result.status == "partial"
        assert result.is_error is False
        done_events = [data for event_type, data in emitted_events if event_type == "subagent.done"]
        assert len(done_events) == 1
        assert done_events[0]["status"] == "partial"
        assert done_events[0]["termination_reason"] == "runtime_error"

    asyncio.run(run())


def test_subagent_checkpoint_failure_cannot_report_completed(monkeypatch, tmp_path):
    async def run() -> None:
        async def fake_run_agent_loop(**kwargs):
            yield AgentEvent.agent_message_completed("Useful work before persistence.")
            yield AgentEvent.done(status="completed")

        def fail_checkpoint(**kwargs):
            raise OSError("disk full")

        monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setattr(
            "backend.agent.query_engine.run_agent_loop",
            fake_run_agent_loop,
        )
        monkeypatch.setattr(
            "backend.agent.turn_kernel.save_run_checkpoint",
            fail_checkpoint,
        )
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="persist recovery state",
            prompt="persist recovery state",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-checkpoint-failure",
                task_id="parent-task",
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.status == "partial"
        assert result.is_error is False
        assert "Useful work before persistence" in result.content
        assert "could not save a resumable checkpoint" in result.content
        records = runtime.list_runs(include_subagents=True)["subagents"]
        assert len(records) == 1
        assert records[0]["status"] == "partial"

    asyncio.run(run())


def test_subagent_failure_without_output_stays_an_error(monkeypatch, tmp_path):
    """Nothing to salvage means the delegation simply failed."""
    async def run() -> None:
        async def fake_run_agent_loop(**kwargs):
            if False:
                yield AgentEvent.agent_message_completed("never")
            raise RuntimeError("provider rate limit")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="find the bug",
            prompt="find the bug",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-empty",
                task_id="parent-task",
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.is_error is True
        assert result.status != "partial"

    asyncio.run(run())


def test_task_tool_retains_max_iteration_fallback_as_partial(monkeypatch, tmp_path):
    async def run() -> None:
        from backend.llm.cost_tracker import CostTracker

        tracker = CostTracker.get_instance()
        tracker.reset()
        emitted_events: list[tuple[str, dict]] = []

        async def emit(event_type: str, data: dict) -> None:
            emitted_events.append((event_type, data))

        async def fake_run_agent_loop(**kwargs):
            kwargs["state"].stopped_reason = "max_iterations"
            kwargs["state"].reply = "成都天气可用总结"
            yield AgentEvent.agent_message_completed("成都天气可用总结")
            yield AgentEvent.error(
                "已达到最大迭代次数限制（24次）。",
                recoverable=True,
                error_type="budget",
            )
            yield AgentEvent.done(
                input_tokens=3,
                output_tokens=5,
                cache_read_input_tokens=2,
                status="failed",
                reason="max_iterations",
            )

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="调研成都天气",
            prompt="调研成都天气并给出来源",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-1",
                task_id="parent-task",
                emit_event=emit,
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.is_error is False
        assert result.status == "partial"
        assert "成都天气可用总结" in result.content
        assert "Error:" not in result.content
        done_events = [data for event_type, data in emitted_events if event_type == "subagent.done"]
        assert len(done_events) == 1
        subagent_id = done_events[0]["subagent_id"]
        snapshot = runtime.get_subagent_snapshot(subagent_id)
        assert snapshot is not None
        assert snapshot["status"] == "partial"
        assert snapshot["result"]["status"] == "partial"
        assert done_events[0]["status"] == "partial"
        assert done_events[0]["termination_reason"] == "max_iterations"
        assert done_events[0]["usage"] == {
            "input_tokens": 3,
            "output_tokens": 5,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 2,
            "prompt_cache_total_tokens": 3,
            "prompt_cache_hit_rate": 66.7,
            "ordinary_input_tokens": 1,
            "input_includes_cache_read": True,
            "input_includes_cache_write": True,
        }
        assert tracker.get_summary()["input_tokens"] == 3

    asyncio.run(run())


def test_foreground_subagent_approval_is_forwarded_with_namespaced_id(monkeypatch, tmp_path):
    async def run() -> None:
        emitted: list[tuple[str, dict[str, Any]]] = []
        parent_ids: list[str] = []

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            emitted.append((event_type, data))

        async def approve(tool_call_id: str) -> dict[str, str]:
            parent_ids.append(tool_call_id)
            return {"action": "approve"}

        async def fake_run_agent_loop(**kwargs):
            response = kwargs["approval_handler"]
            yield AgentEvent.approval_request(
                tool_call_id="call_1",
                tool_name="terminal.exec",
                args={"command": "echo hi"},
            )
            assert await response("call_1") == {"action": "approve"}
            yield AgentEvent.tool_result(
                id="call_1",
                summary="approved",
                status="success",
            )
            yield AgentEvent.agent_message_completed("approved")
            yield AgentEvent.done(status="completed")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="run command",
            prompt="run command",
            agent_type="general-purpose",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-1",
                task_id="parent-task",
                emit_event=emit,
                approval_handler=approve,
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.is_error is False
        assert parent_ids and parent_ids[0].startswith("subagent-")
        assert parent_ids[0].endswith(":call_1")
        approval_events = [data for event_type, data in emitted if event_type == "approval_request"]
        assert approval_events and approval_events[0]["tool_call_id"] == parent_ids[0]
        assert approval_events[0]["subagent_id"] == parent_ids[0].split(":", 1)[0]

    asyncio.run(run())


def test_subagent_approval_bridge_stops_at_parent_turn_deadline(monkeypatch, tmp_path):
    async def run() -> None:
        decisions: list[dict[str, str]] = []
        parent_ids: list[str] = []
        bridge_wait_started: list[float] = []

        async def emit(_event_type: str, _data: dict[str, Any]) -> None:
            return None

        async def approve(tool_call_id: str) -> dict[str, str]:
            parent_ids.append(tool_call_id)
            return {"action": "approve"}

        async def fake_run_agent_loop(**kwargs):
            context.deadline_monotonic = time.monotonic() + 0.02
            bridge_wait_started.append(time.monotonic())
            decisions.append(await kwargs["approval_handler"]("call_before_bridge"))
            yield AgentEvent.agent_message_completed(
                "Approval was rejected at the parent deadline.",
                source="model_final",
            )
            yield AgentEvent.done(status="partial", reason="approval_deadline")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        context = ToolExecutionContext(
            permission=PermissionContext(),
            session_id="session-approval-bridge-deadline",
            task_id="parent-task",
            emit_event=emit,
            approval_handler=approve,
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )
        result = await asyncio.wait_for(
            _task_tool()._run_single_subtask(
                description="approval bridge deadline",
                prompt="request approval",
                agent_type="general-purpose",
                context=context,
            ),
            timeout=1.0,
        )

        assert time.monotonic() - bridge_wait_started[0] < 0.5
        assert result.status == "partial"
        assert decisions and decisions[0]["action"] == "reject"
        assert "parent turn's time budget expired" in decisions[0]["guidance"]
        assert parent_ids == []

    asyncio.run(run())


def test_subagent_parent_approval_answer_stops_at_parent_turn_deadline(monkeypatch, tmp_path):
    async def run() -> None:
        decisions: list[dict[str, str]] = []
        parent_ids: list[str] = []
        approval_started_at: list[float] = []
        approval_decided_at: list[float] = []
        release = asyncio.Event()

        async def emit(event_type: str, _data: dict[str, Any]) -> None:
            if event_type == "approval_request":
                context.deadline_monotonic = time.monotonic() + 0.05

        async def resistant_approve(tool_call_id: str) -> dict[str, str]:
            parent_ids.append(tool_call_id)
            approval_started_at.append(time.monotonic())
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return {"action": "approve"}

        async def fake_run_agent_loop(**kwargs):
            yield AgentEvent.approval_request(
                tool_call_id="call_waiting_for_parent",
                tool_name="terminal.exec",
                args={"command": "echo hi"},
            )
            decisions.append(
                await kwargs["approval_handler"]("call_waiting_for_parent")
            )
            approval_decided_at.append(time.monotonic())
            yield AgentEvent.agent_message_completed(
                "Approval timed out without executing the command.",
                source="model_final",
            )
            yield AgentEvent.done(status="partial", reason="approval_deadline")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        context = ToolExecutionContext(
            permission=PermissionContext(),
            session_id="session-parent-approval-deadline",
            task_id="parent-task",
            emit_event=emit,
            approval_handler=resistant_approve,
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )
        result = await asyncio.wait_for(
            _task_tool()._run_single_subtask(
                description="parent approval answer deadline",
                prompt="request approval",
                agent_type="general-purpose",
                context=context,
            ),
            timeout=1.0,
        )
        release.set()
        await asyncio.sleep(0.01)

        assert approval_decided_at[0] - approval_started_at[0] < 0.5
        assert result.status == "partial"
        assert parent_ids and parent_ids[0].endswith(":call_waiting_for_parent")
        assert decisions and decisions[0]["action"] == "reject"
        assert "parent turn's time budget expired" in decisions[0]["guidance"]

    asyncio.run(run())


def test_task_tool_bridges_subagent_internal_progress(monkeypatch, tmp_path):
    asyncio.run(_test_task_tool_bridges_subagent_internal_progress(monkeypatch, tmp_path))


def test_running_subagent_receives_parent_mailbox_messages(tmp_path):
    asyncio.run(_test_running_subagent_receives_parent_mailbox_messages(tmp_path))


def test_subagent_context_builder_isolates_parent_history_without_mutating_parent(tmp_path):
    asyncio.run(_test_subagent_context_builder_isolates_parent_history_without_mutating_parent(tmp_path))


def test_subagent_prompt_cache_fork_diagnostic_reports_prefix_reuse():
    diagnostic = _subagent_prompt_cache_fork_diagnostic(
        {
            "stable_system_hash": "stable-a",
            "full_system_hash": "full-a",
            "tools_hash": "tools-a",
            "tool_names": ["read_file", "task"],
            "message_count": 4,
        },
        {
            "stable_system_hash": "stable-a",
            "full_system_hash": "full-a",
            "tools_hash": "tools-b",
            "tool_names": ["read_file"],
            "message_count": 6,
        },
    )

    assert diagnostic["status"] == "prefix_reused"
    assert diagnostic["stable_prefix"] == "same"
    assert diagnostic["tool_schemas"] == "changed"
    assert diagnostic["cacheable_prefix_reused"] is True
    assert diagnostic["message_count_delta"] == 2
    assert diagnostic["tool_delta"] == {"added": [], "removed": ["task"]}
    assert diagnostic["prefix_shadow"] == {
        "parent_stable_system_hash": "stable-a",
        "child_stable_system_hash": "stable-a",
    }
    assert diagnostic["schema_shadow"] == {
        "parent_tools_hash": "tools-a",
        "child_tools_hash": "tools-b",
        "parent_tool_count": 2,
        "child_tool_count": 1,
        "child_tool_subset_of_parent": True,
    }


def test_task_tool_emits_prompt_cache_fork_diagnostic_from_real_child_loop(tmp_path):
    asyncio.run(_test_task_tool_emits_prompt_cache_fork_diagnostic_from_real_child_loop(tmp_path))


def test_send_message_resumes_completed_agent_with_canonical_checkpoint(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MINICODE_STATE_ROOT", str(tmp_path / "state"))
    asyncio.run(_test_send_message_resumes_completed_agent_with_full_sidechain(tmp_path))


async def _test_send_message_resumes_completed_agent_with_full_sidechain(tmp_path):
    events: list[tuple[str, dict[str, Any]]] = []

    async def emit(event_type: str, data: dict[str, Any]) -> None:
        events.append((event_type, data))

    class _ResumeLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, Any]] | None = None,
            metadata: dict[str, Any] | None = None,
        ):
            self.calls.append([str(message.content) for message in messages])
            answer = "initial child result" if len(self.calls) == 1 else "resumed child result"
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content=answer)
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return ""

    llm = _ResumeLLM()
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(run_id="parent-run", conversation_id="conversation-1")
    registry = ToolRegistry()
    tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=lambda: PermissionChecker(
            PermissionSettings(), workspace_root=tmp_path
        ),
        agent_settings_provider=lambda: AgentSettings(max_iterations=2),
        token_budget_provider=lambda: TokenBudget(),
    )
    registry.register(tool)
    parent_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        workspace_root=tmp_path,
        session_id="session-1",
        task_id="parent-task",
        conversation_id="conversation-1",
        emit_event=emit,
        metadata={
            "run_id": "parent-run",
            "_tool_registry": registry,
            SESSION_TOOLSET_POLICY_METADATA_KEY: ToolsetPolicy.from_iterables(
                enabled_toolsets=(), enabled_tools=["task"]
            ),
        },
        run_context=RunContext(agent_runtime=runtime),
    )

    initial = await tool.execute(
        {
            "description": "inspect parser",
            "prompt": "Inspect the parser and report once.",
            "agent_type": "general-purpose",
        },
        context=parent_context,
    )
    assert initial.status == "completed"
    starts = [data for event_type, data in events if event_type == "subagent.start"]
    assert starts
    subagent_id = str(starts[-1]["subagent_id"])
    first_record = runtime.get_subagent(subagent_id)
    assert first_record is not None and first_record.status == "completed"
    assert first_record.resume_config["session_toolset_policy"] == {
        "enabled_toolsets": [],
        "disabled_toolsets": [],
        "enabled_tools": ["task"],
        "disabled_tools": [],
        "include_deferred_directly": False,
        "availability_filters": [],
    }
    first_epoch = first_record.mailbox_epoch

    initial_transcript = runtime.load_agent_transcript(subagent_id)
    initial_assistant_messages = [
        item for item in initial_transcript["history"] if item.get("role") == "assistant"
    ]
    assert [item["content"] for item in initial_assistant_messages] == ["initial child result"]

    resumed = await SendMessageTool().execute(
        {
            "recipient": subagent_id,
            "message": "Now check the recovery edge case.",
        },
        context=parent_context,
    )
    assert resumed.status == "running"
    resumed_record = runtime.get_subagent(subagent_id)
    assert resumed_record is not None
    assert resumed_record.mailbox_epoch == first_epoch + 1

    for _ in range(500):
        resumed_record = runtime.get_subagent(subagent_id)
        if resumed_record is not None and resumed_record.status == "completed":
            break
        await asyncio.sleep(0.01)
    assert resumed_record is not None and resumed_record.status == "completed", (
        runtime.get_subagent_snapshot(subagent_id, include_result=True)
    )
    assert len(llm.calls) == 2
    resumed_messages = llm.calls[1]
    assert any("Inspect the parser and report once." in text for text in resumed_messages)
    assert resumed_messages.count("initial child result") == 1
    assert resumed_messages[-1].endswith("Now check the recovery edge case.")
    assert "You are a general-purpose agent" not in resumed_messages[-1]

    final_transcript = runtime.load_agent_transcript(subagent_id)
    assistant_messages = [
        item for item in final_transcript["history"] if item.get("role") == "assistant"
    ]
    assert [item["content"] for item in assistant_messages] == [
        "initial child result",
        "resumed child result",
    ]

    # Resume still works after the in-memory controller is evicted because the
    # durable runtime record and canonical context checkpoint own recovery.
    resumed_epoch = resumed_record.mailbox_epoch
    runtime._subagents.pop(subagent_id, None)
    assert runtime.get_subagent(subagent_id) is None

    resumed_from_transcript = await SendMessageTool().execute(
        {
            "recipient": subagent_id,
            "message": "Finally check the checkpoint recovery path.",
        },
        context=parent_context,
    )
    assert resumed_from_transcript.status == "running"
    for _ in range(500):
        resumed_record = runtime.get_subagent(subagent_id)
        if resumed_record is not None and resumed_record.status == "completed":
            break
        await asyncio.sleep(0.01)
    assert resumed_record is not None and resumed_record.status == "completed"
    assert resumed_record.mailbox_epoch == resumed_epoch + 1
    assert len(llm.calls) == 3
    assert "initial child result" in llm.calls[2]
    assert "resumed child result" in llm.calls[2]
    assert llm.calls[2][-1].endswith("Finally check the checkpoint recovery path.")


async def _test_task_tool_emits_prompt_cache_fork_diagnostic_from_real_child_loop(tmp_path):
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    class _FinalLLM(LLMAdapter):
        def __init__(self) -> None:
            self.metadata_seen: list[dict[str, Any]] = []

        async def stream_chat(
            self,
            messages: list[LLMMessage],
            tools: list[dict[str, Any]] | None = None,
            metadata: dict[str, Any] | None = None,
        ):
            self.metadata_seen.append(dict(metadata or {}))
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="child result")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return ""

    llm = _FinalLLM()
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    registry = ToolRegistry()
    parent_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        session_id="session-1",
        task_id="parent-task",
        conversation_id="conversation-1",
        emit_event=emit,
        metadata={
            "run_id": "parent-run",
            "prompt_cache_safe_params": {
                "stable_system_hash": "parent-stable",
                "full_system_hash": "parent-full",
                "tools_hash": "parent-tools",
                "tool_names": ["task"],
                "message_count": 4,
            },
        },
        run_context=RunContext(agent_runtime=runtime),
    )
    tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=lambda: PermissionChecker(
            PermissionSettings(), workspace_root=tmp_path
        ),
        agent_settings_provider=lambda: AgentSettings(max_iterations=1),
        token_budget_provider=lambda: TokenBudget(),
    )

    result = await tool.execute(
        {
            "description": "quick child",
            "prompt": "Return a concise result.",
            "agent_type": "general-purpose",
        },
        context=parent_context,
    )

    assert result.is_error is False
    done_events = [data for event_type, data in events if event_type == "subagent.done"]
    assert done_events
    fork = done_events[-1].get("prompt_cache_fork")
    assert isinstance(fork, dict)
    assert fork["stable_prefix"] in {"same", "changed"}
    assert fork["stable_prefix"] != "missing"
    assert fork["parent_message_count"] == 4
    assert fork["child_message_count"] >= 1
    assert fork["prefix_shadow"]["parent_stable_system_hash"] == "parent-stable"
    assert fork["schema_shadow"]["parent_tools_hash"] == "parent-tools"
    assert llm.metadata_seen
    child_metadata = llm.metadata_seen[-1]
    assert child_metadata["prompt_cache_fork_status"] == fork["status"]
    assert child_metadata["prompt_cache_parent_stable_hash"] == "parent-stable"
    assert child_metadata["prompt_cache_parent_tools_hash"] == "parent-tools"


async def _test_subagent_context_builder_isolates_parent_history_without_mutating_parent(tmp_path):
    parent_builder = ContextBuilder()
    parent_builder.append_user("parent cached prefix")
    parent_builder.append_assistant_tool_calls(
        [ToolCallEvent(id="parent-task-call", name="task", arguments={})],
        content="",
    )

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    parent_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        session_id="session-1",
        task_id="parent-task",
        conversation_id="conversation-1",
        metadata={
            "run_id": "parent-run",
            "_context_builder": parent_builder,
        },
        run_context=RunContext(agent_runtime=runtime),
    )
    sub_builder = TaskTool._build_subagent_context_builder(
        context=parent_context,
        token_budget=TokenBudget(),
        agent_settings=AgentSettings(max_iterations=3),
    )

    assert sub_builder is not parent_builder
    assert sub_builder._history == []

    sub_builder.append_user("child-only context")

    assert [message.content for message in parent_builder._history] == [
        "parent cached prefix",
        "",
    ]
    assert parent_builder._history[-1].tool_calls[0].name == "task"  # type: ignore[index]
    assert [message.content for message in sub_builder._history] == ["child-only context"]


async def _test_running_subagent_receives_parent_mailbox_messages(tmp_path):
    class _MailboxAwareLLM(LLMAdapter):
        def __init__(self) -> None:
            self.calls = 0
            self.seen_messages: list[list[str]] = []

        async def stream_chat(self, messages: list[LLMMessage], tools: list[dict[str, Any]] | None = None):
            self.calls += 1
            self.seen_messages.append([str(message.content) for message in messages])
            if self.calls == 1:
                yield StreamEvent(
                    type=StreamEventType.TOOL_CALL,
                    tool_calls=[
                        ToolCallEvent(
                            id="wait-for-parent",
                            name="pause_once",
                            arguments={},
                        )
                    ],
                )
                yield StreamEvent(type=StreamEventType.DONE)
                return
            yield StreamEvent(type=StreamEventType.TEXT_CHUNK, content="saw mailbox")
            yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages: list[LLMMessage]) -> str:
            return ""

    class _PauseOnceTool(BaseTool):
        name = "pause_once"
        description = "Pause once so the parent can send a message."
        permission = PermissionLevel.AUTO
        read_only = True
        mutates_workspace = False

        def get_spec(self):
            from backend.tools.contracts import ToolSpec

            return ToolSpec(
                name=self.name,
                capability="test.pause",
                toolset="core",
                exposure="core",
            )

        def get_schema(self):
            from backend.tools.base import ToolSchema

            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args, context=None):
            subagent_id = str((context.metadata or {}).get("run_id") or "")
            await SendMessageTool().execute(
                {
                    "recipient": subagent_id,
                    "message": "Parent update: stop waiting and summarize the mailbox.",
                    "sender": "parent-run",
                },
                context=parent_context,
            )
            return ToolResult(content="parent message sent")

    llm = _MailboxAwareLLM()
    registry = ToolRegistry()
    registry.register(_PauseOnceTool())
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    parent_context = ToolExecutionContext(
        permission=PermissionContext(mode="bypass"),
        session_id="session-1",
        task_id="parent-task",
        conversation_id="conversation-1",
        metadata={"run_id": "parent-run"},
        run_context=RunContext(agent_runtime=runtime),
    )
    tool = TaskTool(
        llm_provider=llm,
        tool_registry_provider=registry,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
        permission_checker_provider=lambda: PermissionChecker(
            PermissionSettings(), workspace_root=tmp_path
        ),
        agent_settings_provider=lambda: AgentSettings(max_iterations=3),
        token_budget_provider=lambda: TokenBudget(),
    )

    result = await tool.execute(
        {
            "description": "wait for parent update",
            "prompt": "Call pause_once, then continue from any mailbox update.",
            "agent_type": "general-purpose",
        },
        context=parent_context,
    )

    assert result.is_error is False
    assert "saw mailbox" in result.content
    assert llm.calls == 2
    second_call_text = "\n".join(llm.seen_messages[1])
    assert "<subagent_mailbox>" in second_call_text
    assert "Parent update: stop waiting" in second_call_text


async def _test_task_tool_bridges_subagent_internal_progress(monkeypatch, tmp_path):
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    async def fake_run_agent_loop(**kwargs):
        bridge = kwargs["session_context"].emit_event
        assert bridge is not None
        await bridge("tool_call", {"id": "child-tool-1", "name": "read_file"})
        await bridge(
            "agent.progress",
            {
                "message": "Reading workspace files",
                "tool_name": "read_file",
                "tool_call_id": "child-tool-1",
            },
        )
        await bridge(
            "tool_call",
            {
                "id": "call_0ee211964b704028a47cfbfe",
                "name": "call_0ee211964b704028a47cfbfe",
            },
        )
        yield AgentEvent.agent_item(
            id="process-1",
            kind="process_text",
            content="I am narrowing the search to the authentication path.",
        )
        yield AgentEvent.agent_message_completed("delegated result")

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session-1",
        task_id="parent-task",
        emit_event=emit,
        metadata={"run_id": "parent-run"},
        run_context=RunContext(agent_runtime=runtime),
    )

    result = await _task_tool().execute(
        {
            "description": "inspect files",
            "prompt": "inspect files deeply",
            "agent_type": "explore",
        },
        context=ctx,
    )

    assert result.is_error is False
    progress_events = [data for event_type, data in events if event_type == "subagent.progress"]
    assert len(progress_events) >= 2
    assert progress_events[0]["source_event_type"] == "tool_call"
    assert progress_events[0]["tool_name"] == "read_file"
    assert progress_events[0]["tool_call_id"] == "child-tool-1"
    assert progress_events[0]["subagent_id"].startswith("subagent-")
    assert progress_events[1]["source_event_type"] == "agent.progress"
    assert progress_events[1]["current_activity"] == "Reading workspace files"
    assert progress_events[2]["tool_name"] == "call_0ee211964b704028a47cfbfe"
    assert progress_events[2]["current_activity"] == "inspect files"
    assert not progress_events[2].get("detail")
    assert progress_events[2]["waiting_on"] == "tool"
    process_progress = next(
        item for item in progress_events if item.get("source_event_type") == "agent.item"
    )
    assert process_progress["detail"] == "I am narrowing the search to the authentication path."
    assert process_progress["waiting_on"] == "model"


def test_subagent_push_transcript_is_monotonic_and_terminally_complete(
    monkeypatch,
    tmp_path,
) -> None:
    async def run() -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        async def fake_run_agent_loop(**_kwargs):
            yield AgentEvent.tool_call(
                "call-readme",
                "read_file",
                {"path": "README.md"},
            )
            yield AgentEvent.tool_result(
                "call-readme",
                "README contents",
                status="completed",
            )
            yield AgentEvent.agent_message_started(
                item_id="child-answer",
                source="model_final",
            )
            yield AgentEvent.agent_message_delta(
                "Final",
                item_id="child-answer",
                source="model_final",
            )
            yield AgentEvent.agent_message_delta(
                " child answer",
                item_id="child-answer",
            )
            yield AgentEvent.agent_message_completed(
                "Final child answer",
                item_id="child-answer",
            )
            yield AgentEvent.done(status="completed")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="inspect transcript",
            prompt="Inspect README.md and report.",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-transcript",
                task_id="parent-task",
                emit_event=emit,
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.status == "completed"
        progress = [
            data for event_type, data in events
            if event_type == "subagent.progress" and "transcript_snapshot" in data
        ]
        done = [data for event_type, data in events if event_type == "subagent.done"]
        assert progress
        assert len(done) == 1

        snapshots = [item["transcript_snapshot"] for item in progress]
        snapshots.append(done[0]["transcript_snapshot"])
        seqs = [snapshot["seq"] for snapshot in snapshots]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)

        running_tool = next(
            block["record"]
            for message in snapshots[0]["messages"]
            for block in message.get("blocks", [])
            if block.get("type") == "tool_call"
        )
        assert running_tool["id"] == "call-readme"
        assert running_tool["status"] == "running"

        finished_tool = next(
            block["record"]
            for message in snapshots[-1]["messages"]
            for block in message.get("blocks", [])
            if block.get("type") == "tool_call"
        )
        assert finished_tool["status"] == "success"
        assert finished_tool["outputPreview"] == "README contents"
        live_answer = next(
            block
            for snapshot in snapshots[:-1]
            for message in snapshot["messages"]
            for block in message.get("blocks", [])
            if block.get("type") == "text" and block.get("is_streaming") is True
        )
        assert live_answer["content"] == "Final"
        assert live_answer["source"] == "model_final"
        assert any(
            message.get("role") == "assistant"
            and message.get("content") == "Final child answer"
            for message in snapshots[-1]["messages"]
        )

        subagent_id = str(done[0]["subagent_id"])
        transcript = runtime.load_agent_transcript(subagent_id)
        assert transcript["events"][-1]["event_type"] == "terminal"
        assert transcript["events"][-1]["payload"]["status"] == "completed"

    asyncio.run(run())


def test_terminal_snapshot_closes_an_unresolved_tool_as_an_error(
    monkeypatch,
    tmp_path,
) -> None:
    async def run() -> None:
        events: list[tuple[str, dict[str, Any]]] = []

        async def emit(event_type: str, data: dict[str, Any]) -> None:
            events.append((event_type, data))

        async def fake_run_agent_loop(**_kwargs):
            yield AgentEvent.tool_call(
                "call-unresolved",
                "read_file",
                {"path": "missing.txt"},
            )
            yield AgentEvent.agent_message_completed("Recovered final answer")
            yield AgentEvent.done(status="completed")

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        result = await _task_tool()._run_single_subtask(
            description="recover dangling tool",
            prompt="Return a final answer even if the read is interrupted.",
            agent_type="explore",
            context=ToolExecutionContext(
                permission=PermissionContext(),
                session_id="session-unresolved",
                task_id="parent-task",
                emit_event=emit,
                metadata={"run_id": "parent-run"},
                run_context=RunContext(agent_runtime=runtime),
            ),
        )

        assert result.status == "completed"
        done = next(data for event_type, data in events if event_type == "subagent.done")
        final_messages = done["transcript_snapshot"]["messages"]
        tool_record = next(
            block["record"]
            for message in final_messages
            for block in message.get("blocks", [])
            if block.get("type") == "tool_call"
        )
        assert tool_record["status"] == "failed"
        assert tool_record["outputPreview"] == "[Tool result missing because the turn reached terminal state]"
        assert any(
            message.get("content") == "Recovered final answer"
            for message in final_messages
        )

        transcript = runtime.load_agent_transcript(str(done["subagent_id"]))
        synthetic = next(
            event for event in transcript["events"]
            if event["event_type"] == "tool_result"
            and event["payload"].get("synthetic")
        )
        assert synthetic["payload"]["status"] == "failed"
        assert synthetic["payload"]["termination_reason"] == "completed"
        assert transcript["events"][-1]["event_type"] == "terminal"

    asyncio.run(run())


async def _test_task_tool_emits_task_lifecycle_hooks(monkeypatch, tmp_path):
    hook_calls: list[tuple[str, dict[str, str]]] = []

    class _HookManager:
        async def run_task_created(self, **kwargs):
            hook_calls.append(("created", dict(kwargs)))

        async def run_task_completed(self, **kwargs):
            hook_calls.append(("completed", dict(kwargs)))

        async def run_teammate_idle(self, **kwargs):
            hook_calls.append(("idle", dict(kwargs)))

        async def run_subagent_start(self, **kwargs):
            pass

        async def run_subagent_stop(self, **kwargs):
            pass

    async def fake_run_agent_loop(**kwargs):
        yield AgentEvent.agent_message_completed("delegated result")

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    hook_manager = _HookManager()
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session-1",
        task_id="parent-task",
        metadata={"run_id": "parent-run"},
        run_context=RunContext(
            agent_runtime=runtime,
            hook_manager=hook_manager,
        ),
    )

    result = await _task_tool().execute(
        {
            "description": "inspect files",
            "prompt": "inspect files deeply",
            "agent_type": "explore",
        },
        context=ctx,
    )

    assert result.is_error is False
    assert "delegated result" in result.content
    assert [name for name, _ in hook_calls] == ["created"]
    created = hook_calls[0][1]
    assert created["task_id"].startswith("subagent-")
    assert created["subject"] == "inspect files"
    assert created["description"] == "inspect files deeply"
    assert created["teammate_name"] == "explore"


async def _test_task_tool_can_start_background_subagent(monkeypatch, tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, dict]] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    async def fake_run_agent_loop(**kwargs):
        started.set()
        await release.wait()
        yield AgentEvent.agent_message_completed("background result")

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session-1",
        task_id="parent-task",
        emit_event=emit,
        metadata={"run_id": "parent-run"},
        run_context=RunContext(agent_runtime=runtime),
    )

    result = await _task_tool().execute(
        {
            "description": "inspect files",
            "prompt": "inspect files",
            "objective": "Inspect files safely",
            "read_only": True,
            "write_scope": ["README.md"],
            "run_in_background": True,
        },
        context=ctx,
    )

    subagent_id = _subagent_id_from(result.content)
    assert result.status == "running"
    assert "task_stop" in result.content

    await asyncio.wait_for(started.wait(), timeout=1)
    assert events[0][0] == "subagent.start"
    assert events[0][1]["subagent_id"] == subagent_id
    assert events[0][1]["read_only"] is True
    snapshot = runtime.list_runs(include_subagents=True)
    assert snapshot["subagents"][0]["background_task"] == "running"

    release.set()
    for _ in range(20):
        if any(event_type == "subagent.done" for event_type, _ in events):
            break
        await asyncio.sleep(0.01)

    done_events = [data for event_type, data in events if event_type == "subagent.done"]
    assert done_events
    assert done_events[-1]["subagent_id"] == subagent_id
    assert done_events[-1]["summary"] == "background result"
    assert done_events[-1]["record"]["status"] == "completed"
    assert done_events[-1]["result"]["status"] == "completed"
    assert "background result" in done_events[-1]["result"]["content"]
    assert runtime.get_subagent_snapshot(subagent_id, include_result=False)["result_available"] is True

    status_result = await TaskStatusTool().execute({"subagent_id": subagent_id}, context=ctx)
    assert status_result.status == "completed"
    assert "background result" in status_result.content
    assert "Stats:" in status_result.content

    consumed_result = await TaskStatusTool().execute(
        {"subagent_id": subagent_id, "consume": True},
        context=ctx,
    )
    assert "Retained result cache released." in consumed_result.content

    after_consume = await TaskStatusTool().execute({"subagent_id": subagent_id}, context=ctx)
    assert after_consume.status == "completed"
    assert "No retained result is available." in after_consume.content


def test_task_stop_cancels_background_subagent(monkeypatch, tmp_path):
    asyncio.run(_test_task_stop_cancels_background_subagent(monkeypatch, tmp_path))


async def _test_task_stop_cancels_background_subagent(monkeypatch, tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    events: list[tuple[str, dict]] = []
    seen_cancel_events: list[asyncio.Event] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    async def fake_run_agent_loop(**kwargs):
        started.set()
        cancel_event = (kwargs["session_context"].metadata or {}).get("cancel_event")
        if isinstance(cancel_event, asyncio.Event):
            seen_cancel_events.append(cancel_event)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield AgentEvent.agent_message_completed("unreachable")

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session-1",
        task_id="parent-task",
        emit_event=emit,
        metadata={"run_id": "parent-run"},
        run_context=RunContext(agent_runtime=runtime),
    )

    start_result = await _task_tool().execute(
        {
            "description": "slow check",
            "prompt": "slow check",
            "run_in_background": True,
            "cancel_with_parent": True,
        },
        context=ctx,
    )
    subagent_id = _subagent_id_from(start_result.content)
    await asyncio.wait_for(started.wait(), timeout=1)

    stop_result = await TaskStopTool().execute({"subagent_id": subagent_id}, context=ctx)

    assert stop_result.status == "cancelled"
    assert seen_cancel_events
    assert seen_cancel_events[-1].is_set()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    for _ in range(20):
        if any(data.get("error") == "cancelled" for event_type, data in events if event_type == "subagent.done"):
            break
        await asyncio.sleep(0.01)

    done_events = [data for event_type, data in events if event_type == "subagent.done"]
    assert done_events[-1]["subagent_id"] == subagent_id
    assert done_events[-1]["error"] == "cancelled"

    status_result = await TaskStatusTool().execute({"subagent_id": subagent_id}, context=ctx)
    assert status_result.status == "cancelled"
    assert "cancelled" in status_result.content


def test_parent_run_cancels_background_subagent(monkeypatch, tmp_path):
    asyncio.run(_test_parent_run_cancels_background_subagent(monkeypatch, tmp_path))


async def _test_parent_run_cancels_background_subagent(monkeypatch, tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    seen_cancel_events: list[asyncio.Event] = []

    async def fake_run_agent_loop(**kwargs):
        started.set()
        cancel_event = (kwargs["session_context"].metadata or {}).get("cancel_event")
        if isinstance(cancel_event, asyncio.Event):
            seen_cancel_events.append(cancel_event)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        yield AgentEvent.agent_message_completed("unreachable")

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session-1",
        task_id="parent-task",
        metadata={"run_id": "parent-run"},
        run_context=RunContext(agent_runtime=runtime),
    )

    start_result = await _task_tool().execute(
        {
            "description": "slow check",
            "prompt": "slow check",
            "run_in_background": True,
        },
        context=ctx,
    )
    subagent_id = _subagent_id_from(start_result.content)
    await asyncio.wait_for(started.wait(), timeout=1)

    cancelled_ids = runtime.cancel_child_subagent_tasks("parent-run", reason="parent_cancelled")

    assert cancelled_ids == [subagent_id]
    assert seen_cancel_events
    assert seen_cancel_events[-1].is_set()
    await asyncio.wait_for(cancelled.wait(), timeout=1)


def test_task_tool_can_start_parallel_background_subagents(monkeypatch, tmp_path):
    asyncio.run(_test_task_tool_can_start_parallel_background_subagents(monkeypatch, tmp_path))


def test_task_tool_queues_independent_background_subagents_at_global_limit(monkeypatch, tmp_path):
    async def run() -> None:
        release = asyncio.Event()
        started = 0
        active = 0
        peak_active = 0
        first_wave_started = asyncio.Event()

        async def fake_run_agent_loop(**kwargs):
            nonlocal started, active, peak_active
            started += 1
            active += 1
            peak_active = max(peak_active, active)
            if started == 4:
                first_wave_started.set()
            try:
                await release.wait()
                yield AgentEvent.agent_message_completed("done")
            finally:
                active -= 1

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        ctx = ToolExecutionContext(
            permission=PermissionContext(),
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )

        results = []
        for index in range(6):
            results.append(await _task_tool().execute({
                "description": f"task {index}",
                "prompt": f"task {index}",
                "run_in_background": True,
            }, context=ctx))

        assert all(result.status == "running" for result in results)
        await asyncio.wait_for(first_wave_started.wait(), timeout=1)
        snapshot = runtime.list_runs(include_subagents=True)["subagents"]
        assert len([
            item for item in snapshot
            if item.get("status") == "running"
        ]) == 4
        assert len([
            item for item in snapshot
            if item.get("status") == "pending" and item.get("background_task") == "queued"
        ]) == 2

        release.set()
        for _ in range(100):
            if started == 6 and not any(
                item.get("status") in {"pending", "running"}
                for item in runtime.list_runs(include_subagents=True)["subagents"]
            ):
                break
            await asyncio.sleep(0.01)

        assert started == 6
        assert peak_active == 4
        assert len([
            item for item in runtime.list_runs(include_subagents=True)["subagents"]
            if item.get("status") == "completed"
        ]) == 6

    asyncio.run(run())


def test_single_foreground_subagent_waits_for_global_worker_slot(monkeypatch, tmp_path):
    async def run() -> None:
        release = asyncio.Event()
        first_wave_started = asyncio.Event()
        foreground_started = asyncio.Event()
        started_prompts: list[str] = []
        active = 0
        peak_active = 0

        async def fake_run_agent_loop(**kwargs):
            nonlocal active, peak_active
            prompt = str(kwargs.get("user_message") or "")
            started_prompts.append(prompt)
            active += 1
            peak_active = max(peak_active, active)
            if active == 4:
                first_wave_started.set()
            if "foreground fifth" in prompt:
                foreground_started.set()
            try:
                await release.wait()
                yield AgentEvent.agent_message_completed("done")
            finally:
                active -= 1

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        ctx = ToolExecutionContext(
            permission=PermissionContext(),
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )

        for index in range(4):
            result = await _task_tool().execute(
                {
                    "description": f"background {index}",
                    "prompt": f"background {index}",
                    "run_in_background": True,
                },
                context=ctx,
            )
            assert result.status == "running"

        await asyncio.wait_for(first_wave_started.wait(), timeout=1)
        foreground_task = asyncio.create_task(
            _task_tool().execute(
                {
                    "description": "foreground fifth",
                    "prompt": "foreground fifth",
                },
                context=ctx,
            )
        )
        await asyncio.sleep(0.05)

        assert not foreground_task.done()
        assert not foreground_started.is_set()
        assert len(started_prompts) == 4

        release.set()
        foreground_result = await asyncio.wait_for(foreground_task, timeout=2)

        assert foreground_result.status == "completed"
        assert foreground_started.is_set()
        assert len(started_prompts) == 5
        assert peak_active == 4

    asyncio.run(run())


def test_parallel_background_batch_runs_eight_with_four_workers_and_can_cancel_queued(monkeypatch, tmp_path):
    async def run() -> None:
        release = asyncio.Event()
        first_wave_started = asyncio.Event()
        started_prompts: list[str] = []
        active = 0
        peak_active = 0

        async def fake_run_agent_loop(**kwargs):
            nonlocal active, peak_active
            prompt = str(kwargs.get("user_message") or "")
            started_prompts.append(prompt)
            active += 1
            peak_active = max(peak_active, active)
            if len(started_prompts) == 4:
                first_wave_started.set()
            try:
                await release.wait()
                yield AgentEvent.agent_message_completed(f"completed {len(started_prompts)}")
            finally:
                active -= 1

        monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        ctx = ToolExecutionContext(
            permission=PermissionContext(),
            metadata={"run_id": "parent-run"},
            run_context=RunContext(agent_runtime=runtime),
        )
        tasks = [
            {
                "description": f"task {index}",
                "prompt": f"inspect task {index}",
                "read_only": True,
            }
            for index in range(8)
        ]

        result = await _task_tool().execute(
            {"parallel_tasks": tasks, "run_in_background": True},
            context=ctx,
        )
        subagent_ids = _subagent_ids_from(result.content)

        assert len(subagent_ids) == 8
        await asyncio.wait_for(first_wave_started.wait(), timeout=1)
        snapshot = runtime.list_runs(include_subagents=True)["subagents"]
        assert len([item for item in snapshot if item.get("status") == "running"]) == 4
        queued = [
            item
            for item in snapshot
            if item.get("status") == "pending" and item.get("background_task") == "queued"
        ]
        assert len(queued) == 4

        queued_id = str(queued[-1]["subagent_id"])
        queued_status = await TaskStatusTool().execute({"subagent_id": queued_id}, context=ctx)
        assert queued_status.status == "pending"
        stopped = await TaskStopTool().execute({"subagent_id": queued_id}, context=ctx)
        assert stopped.status == "cancelled"

        release.set()
        for _ in range(150):
            snapshot = runtime.list_runs(include_subagents=True)["subagents"]
            if len(started_prompts) == 7 and not any(
                item.get("status") in {"pending", "running"}
                for item in snapshot
            ):
                break
            await asyncio.sleep(0.01)

        assert len(started_prompts) == 7
        assert peak_active == 4
        assert not any(f"inspect task 7" in prompt for prompt in started_prompts)
        completed = [
            item
            for item in runtime.list_runs(include_subagents=True)["subagents"]
            if item.get("status") == "completed"
        ]
        assert len(completed) == 7

    asyncio.run(run())


async def _test_task_tool_can_start_parallel_background_subagents(monkeypatch, tmp_path):
    started_count = 0
    all_started = asyncio.Event()
    release = asyncio.Event()
    events: list[tuple[str, dict]] = []
    prompts: list[str] = []

    async def emit(event_type: str, data: dict) -> None:
        events.append((event_type, data))

    async def fake_run_agent_loop(**kwargs):
        nonlocal started_count
        prompt = str(kwargs.get("user_message") or "")
        prompts.append(prompt)
        started_count += 1
        if started_count == 2:
            all_started.set()
        await release.wait()
        yield AgentEvent.agent_message_completed(
            "alpha result"
            if "inspect alpha" in prompt
            else "beta result"
        )

    monkeypatch.setattr("backend.agent.query_engine.run_agent_loop", fake_run_agent_loop)

    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        session_id="session-1",
        task_id="parent-task",
        emit_event=emit,
        metadata={"run_id": "parent-run"},
        run_context=RunContext(agent_runtime=runtime),
    )

    result = await _task_tool().execute(
        {
            "parallel_tasks": [
                {
                    "description": "alpha check",
                    "prompt": "inspect alpha",
                    "read_only": True,
                },
                {
                    "description": "beta check",
                    "prompt": "inspect beta",
                    "read_only": True,
                },
            ],
            "run_in_background": True,
        },
        context=ctx,
    )

    subagent_ids = _subagent_ids_from(result.content)
    assert result.status == "running"
    assert len(subagent_ids) == 2
    await asyncio.wait_for(all_started.wait(), timeout=1)
    assert any("inspect alpha" in prompt for prompt in prompts)
    assert any("inspect beta" in prompt for prompt in prompts)

    snapshot = runtime.list_runs(include_subagents=True)
    running = [item for item in snapshot["subagents"] if item.get("background_task") == "running"]
    assert len(running) == 2

    release.set()
    for _ in range(40):
        if len([event for event in events if event[0] == "subagent.done"]) == 2:
            break
        await asyncio.sleep(0.01)

    done_events = [data for event_type, data in events if event_type == "subagent.done"]
    assert len(done_events) == 2
    status_contents = [
        (await TaskStatusTool().execute({"subagent_id": subagent_id}, context=ctx)).content
        for subagent_id in subagent_ids
    ]
    assert any("alpha result" in content for content in status_contents)
    assert any("beta result" in content for content in status_contents)


def test_task_status_returns_summary_first_for_long_retained_results(tmp_path):
    asyncio.run(_test_task_status_returns_summary_first_for_long_retained_results(tmp_path))


def test_task_status_lists_and_waits_for_background_subagents(tmp_path):
    asyncio.run(_test_task_status_lists_and_waits_for_background_subagents(tmp_path))


def test_task_status_collects_a_parallel_batch_in_one_call(tmp_path):
    asyncio.run(_test_task_status_collects_a_parallel_batch_in_one_call(tmp_path))


def test_task_status_batch_wakes_when_any_subagent_finishes(tmp_path):
    asyncio.run(_test_task_status_batch_wakes_when_any_subagent_finishes(tmp_path))


def test_task_status_batch_display_reports_mixed_status_counts(tmp_path):
    async def run() -> None:
        runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
        runtime.start_run(conversation_id="conv-mixed", run_id="run-mixed")
        runtime.start_subagent(
            subagent_id="subagent-running-mixed",
            parent_run_id="run-mixed",
            agent_type="explore",
            prompt_summary="Running task",
            background=True,
        )
        runtime.start_subagent(
            subagent_id="subagent-done-mixed",
            parent_run_id="run-mixed",
            agent_type="explore",
            prompt_summary="Completed task",
            background=True,
        )
        runtime.complete_subagent(
            "subagent-done-mixed",
            "completed",
            summary="Done",
            **_subagent_fence(runtime, "subagent-done-mixed"),
        )
        runtime.store_subagent_result(
            "subagent-done-mixed",
            status="completed",
            content="Done",
            **_subagent_fence(runtime, "subagent-done-mixed"),
        )
        context = ToolExecutionContext(
            permission=PermissionContext(),
            metadata={"run_id": "run-mixed"},
            run_context=RunContext(agent_runtime=runtime),
        )
        result = await TaskStatusTool().execute(
            {
                "subagent_ids": ["subagent-running-mixed", "subagent-done-mixed"],
                "include_result": True,
            },
            context=context,
        )
        assert result.status == "running"
        assert result.display_summary == "2 delegated task(s): 1 running, 1 completed"

    asyncio.run(run())


async def _test_task_status_collects_a_parallel_batch_in_one_call(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(conversation_id="conv-batch", run_id="run-parent")
    for index in range(2):
        subagent_id = f"subagent-batch-{index}"
        runtime.start_subagent(
            subagent_id=subagent_id,
            parent_run_id="run-parent",
            agent_type="explore",
            prompt_summary=f"City {index}",
            background=True,
            objective=f"City {index}",
        )
        runtime.complete_subagent(
            subagent_id,
            "completed",
            summary="Done",
            tool_count=1,
            **_subagent_fence(runtime, subagent_id),
        )
        runtime.store_subagent_result(
            subagent_id,
            status="completed",
            content=f"## Result\n- City {index} complete.",
            duration_ms=100,
            iterations=1,
            tool_call_count=1,
            **_subagent_fence(runtime, subagent_id),
        )
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"run_id": "run-parent"},
        run_context=RunContext(agent_runtime=runtime),
    )

    result = await TaskStatusTool().execute(
        {"subagent_ids": ["subagent-batch-0", "subagent-batch-1"], "include_result": True},
        context=ctx,
    )

    assert result.status == "completed"
    assert "City 0 complete" in result.content
    assert "City 1 complete" in result.content
    assert result.display_summary == "2 delegated task(s): completed"


async def _test_task_status_batch_wakes_when_any_subagent_finishes(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(conversation_id="conv-any", run_id="run-any")
    for subagent_id in ("subagent-fast", "subagent-slow"):
        runtime.start_subagent(
            subagent_id=subagent_id,
            parent_run_id="run-any",
            agent_type="explore",
            prompt_summary=subagent_id,
            background=True,
        )

    context = ToolExecutionContext(
        permission=PermissionContext(),
        metadata={"run_id": "run-any"},
        run_context=RunContext(agent_runtime=runtime),
    )

    async def finish_fast() -> None:
        await asyncio.sleep(0.02)
        runtime.complete_subagent(
            "subagent-fast",
            "completed",
            summary="Fast result",
            **_subagent_fence(runtime, "subagent-fast"),
        )
        runtime.store_subagent_result(
            "subagent-fast",
            status="completed",
            content="Fast result is ready.",
            **_subagent_fence(runtime, "subagent-fast"),
        )

    finisher = asyncio.create_task(finish_fast())
    result = await asyncio.wait_for(
        TaskStatusTool().execute(
            {
                "subagent_ids": ["subagent-fast", "subagent-slow"],
                "wait_seconds": 1,
                "include_result": True,
            },
            context=context,
        ),
        timeout=0.25,
    )
    await finisher

    assert result.status == "running"
    assert "Fast result is ready." in result.content
    assert "subagent-slow" in result.content
    assert result.display_summary == "2 delegated task(s): 1 completed, 1 running"


async def _test_task_status_lists_and_waits_for_background_subagents(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(
        run_id="parent-run",
        conversation_id="conversation-task-status-list",
    )
    runtime.start_subagent(
        subagent_id="subagent-running",
        parent_run_id="parent-run",
        agent_type="explore",
        prompt_summary="Inspect runtime",
        background=True,
    )
    runtime.start_subagent(
        subagent_id="subagent-complete",
        parent_run_id="parent-run",
        agent_type="verification",
        prompt_summary="Verify runtime",
        background=True,
    )
    runtime.complete_subagent(
        "subagent-complete",
        "completed",
        summary="Verified",
        **_subagent_fence(runtime, "subagent-complete"),
    )
    runtime.store_subagent_result(
        "subagent-complete",
        status="completed",
        content="Verification passed.",
        **_subagent_fence(runtime, "subagent-complete"),
    )
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        task_id="parent-run",
        conversation_id="conversation-task-status-list",
        run_context=RunContext(agent_runtime=runtime),
    )

    listing = await TaskStatusTool().execute({}, context=ctx)

    assert listing.status == "running"
    assert "2 background subagent(s)" in listing.content
    assert "subagent-running [running]" in listing.content
    assert "subagent-complete [completed]" in listing.content

    async def finish() -> None:
        await asyncio.sleep(0.02)
        runtime.complete_subagent(
            "subagent-running",
            "completed",
            summary="Done",
            **_subagent_fence(runtime, "subagent-running"),
        )
        runtime.store_subagent_result(
            "subagent-running",
            status="completed",
            content="Runtime inspected.",
            **_subagent_fence(runtime, "subagent-running"),
        )

    finisher = asyncio.create_task(finish())
    waited = await TaskStatusTool().execute(
        {"subagent_id": "subagent-running", "wait_seconds": 1},
        context=ctx,
    )
    await finisher

    assert waited.status == "completed"
    assert "Runtime inspected." in waited.content


async def _test_task_status_returns_summary_first_for_long_retained_results(tmp_path):
    runtime = AgentRuntime(metrics_file=tmp_path / "metrics.jsonl")
    runtime.start_run(
        run_id="parent-run",
        conversation_id="conversation-task-status-summary",
    )
    runtime.start_subagent(
        subagent_id="subagent-long",
        parent_run_id="parent-run",
        agent_type="explore",
        prompt_summary="Inspect long output",
        background=True,
    )
    runtime.complete_subagent(
        "subagent-long",
        "completed",
        summary="Long report",
        tool_count=3,
        **_subagent_fence(runtime, "subagent-long"),
    )
    raw_appendix = "\n".join(f"raw appendix marker {index}" for index in range(900))
    content = "\n".join(
        [
            "Subagent subagent-long (explore) completed in 1.2s, 3 iteration(s).",
            "",
            "## Arbitrary operator notes",
            "[tool] run_command produced a diagnostic line that must remain observable.",
            "",
            "## Result",
            "- API inspection passed with one minor follow-up.",
            "",
            "## Evidence",
            "- backend/api/routes_chat.py:42 validates the request shape.",
            "",
            "## Changes",
            "- None.",
            "",
            "## Verification",
            "- Read-only inspection; no tests run.",
            "",
            "## Risks or blockers",
            "- Follow-up: add a regression test.",
            "",
            "## Appendix",
            raw_appendix,
        ]
    )
    runtime.store_subagent_result(
        "subagent-long",
        status="completed",
        content=content,
        duration_ms=1200,
        iterations=3,
        tool_call_count=3,
        **_subagent_fence(runtime, "subagent-long"),
    )
    ctx = ToolExecutionContext(
        permission=PermissionContext(),
        task_id="parent-run",
        conversation_id="conversation-task-status-summary",
        run_context=RunContext(agent_runtime=runtime),
    )

    summary = await TaskStatusTool().execute({"subagent_id": "subagent-long"}, context=ctx)

    assert summary.status == "completed"
    assert "## Arbitrary operator notes" in summary.content
    assert "[tool] run_command produced a diagnostic line" in summary.content
    assert "## Result" in summary.content
    assert "API inspection passed" in summary.content
    # Pi keeps each delegated result inline up to its 50 KiB per-task cap;
    # this fixture is below that contract and should remain fully observable.
    assert "raw appendix marker 899" in summary.content

    full = await TaskStatusTool().execute(
        {"subagent_id": "subagent-long", "include_result": True},
        context=ctx,
    )
    assert "raw appendix marker 899" in full.content
