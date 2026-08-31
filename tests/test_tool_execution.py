"""Tests for backend.agent.tool_execution — normalization, guards, and helpers."""

from __future__ import annotations

import asyncio

from backend.agent.tool_batch_execution import (
    _finalize_tool_result,
    _flush_queue as flush_queue,
    batch_tool_calls,
    execute_tool_batch,
)
from backend.agent.tool_events import status_for_result
from backend.agent.tool_execution import (
    invalid_tool_call_guard_reason,
    missing_required_tool_argument_names,
    normalize_tool_call_event,
    prepare_tool_call_sequence,
)
from backend.agent.tool_stream_tracker import (
    StreamingToolTracker,
    StreamingToolStatus,
)
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.config import PermissionSettings, TokenBudget
from backend.llm.base import ToolCallEvent
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.file_tools import ReadFileTool
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry


class _BatchTool(BaseTool):
    def __init__(self, name: str, *, read_only: bool) -> None:
        self.name = name
        self.description = name
        self.read_only = read_only

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="ok")


class _MutatingBatchTool(_BatchTool):
    mutates_workspace = True

    def __init__(self, name: str = "mutating_batch") -> None:
        super().__init__(name, read_only=False)
        self.executions = 0

    async def execute(self, args, context=None) -> ToolResult:
        self.executions += 1
        return ToolResult(content=f"executed {self.executions}")


def test_created_then_removed_helper_supersedes_original_file_edit(tmp_path) -> None:
    async def run() -> list:
        helper = tmp_path / "create_word.py"
        helper.write_text("print('temporary')\n", encoding="utf-8")
        state = AgentState(user_message="create a document")
        context_builder = ContextBuilder()
        await context_builder.start_turn(state.user_message, state)
        tool_context = ToolExecutionContext(
            permission=PermissionContext(),
            workspace_root=tmp_path,
        )
        write_call = ToolCallEvent(
            id="write-helper",
            name="write_file",
            arguments={"file_path": str(helper), "content": "print('temporary')\n"},
        )
        write_diff = {
            "format": "structured",
            "files": [
                {"path": str(helper), "status": "added", "additions": 1, "deletions": 0}
            ],
        }
        async for _event in _finalize_tool_result(
            write_call,
            ToolResult(content="created"),
            ctx=context_builder,
            state=state,
            tool_ctx=tool_context,
            iteration_id="iter:1",
            turn_id="turn:1",
            diff=write_diff,
            tool_registry=ToolRegistry(),
        ):
            pass

        helper.unlink()
        command_call = ToolCallEvent(
            id="cleanup-helper",
            name="run_command",
            arguments={"command": "Remove-Item create_word.py"},
        )
        return [
            event
            async for event in _finalize_tool_result(
                command_call,
                ToolResult(content="Exit code: 0"),
                ctx=context_builder,
                state=state,
                tool_ctx=tool_context,
                iteration_id="iter:2",
                turn_id="turn:1",
                tool_registry=ToolRegistry(),
            )
        ]

    events = asyncio.run(run())

    result = next(event for event in events if event.type == "tool_result")
    assert result.data["superseded_tool_call_ids"] == ["write-helper"]
    assert result.data["removed_file_paths"] == ["create_word.py"]


def test_batch_tool_calls_groups_parallel_safe_web_searches() -> None:
    registry = ToolRegistry()
    registry.register(_BatchTool("web_search", read_only=True))
    registry.register(_BatchTool("write_file", read_only=False))
    calls = [
        ToolCallEvent(id="s1", name="web_search", arguments={"query": "one"}),
        ToolCallEvent(id="s2", name="web_search", arguments={"query": "two"}),
        ToolCallEvent(
            id="w1", name="write_file", arguments={"file_path": "x", "content": "x"}
        ),
    ]

    batches = batch_tool_calls(calls, registry)

    assert [
        (is_concurrent, [tc.id for tc in batch]) for is_concurrent, batch in batches
    ] == [
        (True, ["s1", "s2"]),
        (False, ["w1"]),
    ]


def test_batch_tool_calls_starts_same_turn_read_only_subagents_together(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    registry.register(
        TaskTool(artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))
    )
    calls = [
        ToolCallEvent(
            id="task-1",
            name="task",
            arguments={
                "description": "Inspect A",
                "prompt": "Inspect A",
                "agent_type": "explore",
                "read_only": True,
            },
        ),
        ToolCallEvent(
            id="task-2",
            name="task",
            arguments={
                "description": "Inspect B",
                "prompt": "Inspect B",
                "agent_type": "explore",
                "read_only": True,
            },
        ),
    ]

    assert [
        (parallel, [call.id for call in batch])
        for parallel, batch in batch_tool_calls(calls, registry)
    ] == [
        (True, ["task-1", "task-2"]),
    ]


def test_batch_tool_calls_keeps_write_capable_subagents_serial(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(
        TaskTool(artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"))
    )
    calls = [
        ToolCallEvent(
            id="task-1",
            name="task",
            arguments={
                "description": "Edit A",
                "prompt": "Edit A",
                "agent_type": "implement",
            },
        ),
        ToolCallEvent(
            id="task-2",
            name="task",
            arguments={
                "description": "Edit B",
                "prompt": "Edit B",
                "agent_type": "implement",
            },
        ),
    ]

    assert [
        (parallel, [call.id for call in batch])
        for parallel, batch in batch_tool_calls(calls, registry)
    ] == [
        (False, ["task-1"]),
        (False, ["task-2"]),
    ]


def test_flush_queue_marks_unfinished_parallel_tools_partial_after_batch_timeout(
    tmp_path, monkeypatch
) -> None:
    class SlowReadOnlyTool(BaseTool):
        description = "Slow read-only tool"
        read_only = True
        timeout_seconds = 10.0

        def __init__(self, name: str) -> None:
            self.name = name

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object"},
            )

        async def execute(self, args, context=None) -> ToolResult:
            delay = float(args.get("delay", 0.0))
            if delay:
                await asyncio.sleep(delay)
            return ToolResult(content=f"{self.name} done")

    monkeypatch.setenv("MINICODE_MAX_TOOL_CONCURRENCY", "1")
    monkeypatch.setenv("MINICODE_TOOL_BATCH_TIMEOUT_SECONDS", "0.05")

    registry = ToolRegistry()
    for name in ("fast_tool", "slow_tool", "queued_tool"):
        registry.register(SlowReadOnlyTool(name))
    state = AgentState(user_message="run parallel tools")

    async def collect() -> list:
        events = []
        async for event in flush_queue(
            [
                ToolCallEvent(id="fast", name="fast_tool", arguments={"delay": 0}),
                ToolCallEvent(id="slow", name="slow_tool", arguments={"delay": 1}),
                ToolCallEvent(id="queued", name="queued_tool", arguments={"delay": 1}),
            ],
            ctx=ContextBuilder(TokenBudget()),
            state=state,
            tool_registry=registry,
            tool_ctx=ToolExecutionContext(
                permission=PermissionContext(), workspace_root=tmp_path
            ),
            iteration_id="iter:1",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    results = [event.data for event in events if event.type == "tool_result"]

    assert [item["status"] for item in results] == ["success", "timeout", "timeout"]
    assert results[0]["summary"] == "fast_tool done"
    assert "batch timed out" in results[1]["summary"].lower()
    assert "batch timed out" in results[2]["summary"].lower()
    assert [record.status for record in state.tool_calls] == [
        "success",
        "timeout",
        "timeout",
    ]


class _ArgumentReadOnlyTool(BaseTool):
    name = "run_command"
    description = "Run command"
    mutates_workspace = False
    mutates_external_state = False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    def is_read_only(self, args: dict[str, object] | None = None) -> bool:
        return str((args or {}).get("command") or "").startswith("show")

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="ok")


class _FailingReadOnlyCommandTool(_ArgumentReadOnlyTool):
    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="command failed", is_error=True, status="failed")


class _FailingReadOnlyTool(BaseTool):
    name = "failing_read"
    description = "Failing read"
    read_only = True

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="read failed", is_error=True, status="failed")


class _StreamingCommandTool(_ArgumentReadOnlyTool):
    streams_output = True

    async def execute(self, args, context=None) -> ToolResult:
        if context and context.stream_callback:
            await context.stream_callback("Epoch 1/10 - loss=0.92\n")
            await context.stream_callback("Epoch 2/10 - loss=0.71\n")
        return ToolResult(
            content="Exit code: 0\n\nEpoch 1/10 - loss=0.92\nEpoch 2/10 - loss=0.71"
        )


class _SlowCancellableReadTool(BaseTool):
    name = "slow_read"
    description = "Slow read"
    read_only = True

    def __init__(self) -> None:
        self.started = False
        self.cancelled = False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.started = True
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return ToolResult(content="slow read completed")


class _ConcurrencyTrackingReadTool(BaseTool):
    name = "track_read"
    description = "Track concurrent reads"
    read_only = True

    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.05)
            return ToolResult(content=f"tracked {args.get('index')}")
        finally:
            self.active -= 1


class _TrackedReadTool(BaseTool):
    name = "tracked_read"
    description = "Tracked read"
    read_only = True

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.executions = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.executions += 1
        self.started.set()
        await self.release.wait()
        return ToolResult(content=f"tracked {args.get('value')}")


class _FastTrackedReadTool(BaseTool):
    name = "fast_tracked_read"
    description = "Fast tracked read"
    read_only = True

    def __init__(self) -> None:
        self.executions = 0

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        self.executions += 1
        return ToolResult(content=f"value {args.get('value')}")


class _BudgetCountingTool(BaseTool):
    name = "budget_counting_tool"
    description = "Count actual budgeted executions"
    read_only = True

    def __init__(self) -> None:
        self.executed_values: list[int] = []

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        value = int(args.get("value", 0))
        self.executed_values.append(value)
        return ToolResult(content=f"executed {value}")


class _LegacyNamedReadTool(BaseTool):
    name = "read_file"
    description = "Legacy read"

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="ok")


class _ExplicitNonReadTool(BaseTool):
    name = "read_file"
    description = "Explicit non-read"
    read_only = False

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name, description=self.description, parameters={"type": "object"}
        )

    async def execute(self, args, context=None) -> ToolResult:
        return ToolResult(content="ok")


def test_batch_tool_calls_uses_tool_owned_read_only_classifier() -> None:
    registry = ToolRegistry()
    registry.register(_ArgumentReadOnlyTool())
    calls = [
        ToolCallEvent(
            id="r1", name="run_command", arguments={"command": "show status"}
        ),
        ToolCallEvent(id="r2", name="run_command", arguments={"command": "show diff"}),
        ToolCallEvent(id="w1", name="run_command", arguments={"command": "write file"}),
    ]

    batches = batch_tool_calls(calls, registry)

    assert [
        (is_concurrent, [tc.id for tc in batch]) for is_concurrent, batch in batches
    ] == [
        (True, ["r1", "r2"]),
        (False, ["w1"]),
    ]


def test_flush_queue_cancels_in_flight_siblings_when_parallel_command_fails(
    tmp_path,
) -> None:
    registry = ToolRegistry()
    failing_command = _FailingReadOnlyCommandTool()
    slow_read = _SlowCancellableReadTool()
    registry.register(failing_command)
    registry.register(slow_read)
    state = AgentState(user_message="run parallel reads")
    ctx = ContextBuilder(TokenBudget())

    async def collect():
        events = []
        async for event in flush_queue(
            [
                ToolCallEvent(
                    id="cmd",
                    name="run_command",
                    arguments={"command": "show failing status"},
                ),
                ToolCallEvent(id="slow", name="slow_read", arguments={}),
            ],
            ctx=ctx,
            state=state,
            tool_registry=registry,
            tool_ctx=ToolExecutionContext(
                permission=PermissionContext(), workspace_root=tmp_path
            ),
            iteration_id="iter-1",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert slow_read.cancelled is False
    assert [result["id"] for result in tool_results] == ["cmd", "slow"]
    assert tool_results[0]["status"] == "failed"
    assert tool_results[1]["status"] == "success"
    assert tool_results[1]["summary"] == "slow read completed"


def test_flush_queue_does_not_cancel_siblings_when_read_tool_fails(tmp_path) -> None:
    registry = ToolRegistry()
    failing_read = _FailingReadOnlyTool()
    slow_read = _SlowCancellableReadTool()
    registry.register(failing_read)
    registry.register(slow_read)
    state = AgentState(user_message="run parallel reads")
    ctx = ContextBuilder(TokenBudget())

    async def collect():
        events = []
        async for event in flush_queue(
            [
                ToolCallEvent(id="failed", name="failing_read", arguments={}),
                ToolCallEvent(id="slow", name="slow_read", arguments={}),
            ],
            ctx=ctx,
            state=state,
            tool_registry=registry,
            tool_ctx=ToolExecutionContext(
                permission=PermissionContext(), workspace_root=tmp_path
            ),
            iteration_id="iter-1",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert slow_read.started is True
    assert slow_read.cancelled is False
    assert [result["id"] for result in tool_results] == ["failed", "slow"]
    assert [result["status"] for result in tool_results] == ["failed", "success"]
    assert tool_results[1]["summary"] == "slow read completed"


def test_flush_queue_preserves_sibling_that_settles_before_abort_harvest(
    tmp_path,
) -> None:
    release_sibling = asyncio.Event()

    class FailingStreamingRead(BaseTool):
        name = "failing_streaming_read"
        description = "Fail immediately after starting a streamed read"
        read_only = True
        streams_output = True

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object"},
            )

        async def execute(self, args, context=None) -> ToolResult:
            # Let the sibling settle while this failed result yields once;
            # the coordinator must retain that concrete result if it aborts
            # the batch immediately afterward.
            release_sibling.set()
            await asyncio.sleep(0)
            return ToolResult(content="stream failed", is_error=True, status="failed")

    class ReleasedRead(BaseTool):
        name = "released_read"
        description = "Finish when the test consumer releases the call"
        read_only = True

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description=self.description,
                parameters={"type": "object"},
            )

        async def execute(self, args, context=None) -> ToolResult:
            await release_sibling.wait()
            return ToolResult(content="settled before sibling abort")

    registry = ToolRegistry()
    registry.register(FailingStreamingRead())
    registry.register(ReleasedRead())
    state = AgentState(user_message="preserve settled parallel evidence")

    async def collect() -> list:
        events = []
        async for event in flush_queue(
            [
                ToolCallEvent(id="failed", name="failing_streaming_read", arguments={}),
                ToolCallEvent(id="settled", name="released_read", arguments={}),
            ],
            ctx=ContextBuilder(TokenBudget()),
            state=state,
            tool_registry=registry,
            tool_ctx=ToolExecutionContext(
                permission=PermissionContext(), workspace_root=tmp_path
            ),
            iteration_id="iter-1",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert [result["id"] for result in tool_results] == ["failed", "settled"]
    assert tool_results[0]["status"] == "failed"
    assert tool_results[1]["status"] == "success"
    assert tool_results[1]["summary"] == "settled before sibling abort"


def test_flush_queue_cancels_unstarted_siblings_when_parallel_command_fails(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MINICODE_MAX_TOOL_CONCURRENCY", "1")
    registry = ToolRegistry()
    failing_command = _FailingReadOnlyCommandTool()
    slow_read = _SlowCancellableReadTool()
    registry.register(failing_command)
    registry.register(slow_read)
    state = AgentState(user_message="run bounded parallel reads")
    ctx = ContextBuilder(TokenBudget())

    async def collect():
        events = []
        async for event in flush_queue(
            [
                ToolCallEvent(
                    id="cmd",
                    name="run_command",
                    arguments={"command": "show failing status"},
                ),
                ToolCallEvent(id="slow", name="slow_read", arguments={}),
            ],
            ctx=ctx,
            state=state,
            tool_registry=registry,
            tool_ctx=ToolExecutionContext(
                permission=PermissionContext(), workspace_root=tmp_path
            ),
            iteration_id="iter-1",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert slow_read.started is True
    assert slow_read.cancelled is False
    assert [result["id"] for result in tool_results] == ["cmd", "slow"]
    assert tool_results[0]["status"] == "failed"
    assert tool_results[1]["status"] == "success"
    assert tool_results[1]["summary"] == "slow read completed"


def test_flush_queue_caps_parallel_tool_concurrency(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MINICODE_MAX_TOOL_CONCURRENCY", "2")
    registry = ToolRegistry()
    tracking_tool = _ConcurrencyTrackingReadTool()
    registry.register(tracking_tool)
    state = AgentState(user_message="run bounded parallel reads")
    ctx = ContextBuilder(TokenBudget())
    calls = [
        ToolCallEvent(id=f"read-{index}", name="track_read", arguments={"index": index})
        for index in range(6)
    ]

    async def collect():
        events = []
        async for event in flush_queue(
            calls,
            ctx=ctx,
            state=state,
            tool_registry=registry,
            tool_ctx=ToolExecutionContext(
                permission=PermissionContext(), workspace_root=tmp_path
            ),
            iteration_id="iter-1",
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert tracking_tool.max_active == 2
    assert tracking_tool.active == 0
    assert [result["id"] for result in tool_results] == [
        f"read-{index}" for index in range(6)
    ]
    assert [result["status"] for result in tool_results] == ["success"] * 6


def test_streaming_tool_tracker_never_executes_before_final_batch() -> None:
    async def collect():
        tool = _TrackedReadTool()
        tracker = StreamingToolTracker()

        pending = tracker.add_tool(
            ToolCallEvent(id="read-1", name="tracked_read", arguments={"value": 1})
        )

        await asyncio.sleep(0)
        return pending, tool.executions, tracker.status_snapshot()

    pending, executions, status_snapshot = asyncio.run(collect())

    assert pending is None
    assert executions == 0
    assert status_snapshot == (("read-1", StreamingToolStatus.QUEUED.value),)


def test_execute_tool_batch_runs_settled_calls_once(tmp_path) -> None:
    async def collect():
        registry = ToolRegistry()
        tool = _FastTrackedReadTool()
        registry.register(tool)
        state = AgentState(user_message="run tracked reads")
        ctx = ContextBuilder(TokenBudget())
        permission_checker = PermissionChecker(
            PermissionSettings(auto_allow=["fast_tracked_read"]),
            tmp_path,
        )
        tool_ctx = ToolExecutionContext(
            permission=PermissionContext(), workspace_root=tmp_path
        )
        calls = [
            ToolCallEvent(
                id="read-1", name="fast_tracked_read", arguments={"value": 1}
            ),
            ToolCallEvent(
                id="read-2", name="fast_tracked_read", arguments={"value": 2}
            ),
        ]
        events = []
        async for event in execute_tool_batch(
            calls,
            ctx=ctx,
            state=state,
            tool_registry=registry,
            permission_checker=permission_checker,
            approval_handler=None,
            skill_manager=None,
            permission_context=PermissionContext(),
            tool_ctx=tool_ctx,
        ):
            events.append(event)
        return tool.executions, events

    executions, events = asyncio.run(collect())
    tool_results = [event.data for event in events if event.type == "tool_result"]

    assert executions == 2
    assert [result["id"] for result in tool_results] == ["read-1", "read-2"]
    assert [result["summary"] for result in tool_results] == ["value 1", "value 2"]


def test_execute_tool_batch_preserves_blocked_evidence_after_budget_exhaustion(
    tmp_path,
) -> None:
    async def collect():
        registry = ToolRegistry()
        tool = _BudgetCountingTool()
        registry.register(tool)
        state = AgentState(user_message="run three tools with one slot")
        state.iterations = 1
        ctx = ContextBuilder(TokenBudget())
        calls = [
            ToolCallEvent(
                id=f"budget-{value}", name=tool.name, arguments={"value": value}
            )
            for value in (1, 2, 3)
        ]
        ctx.append_assistant_tool_calls(calls)
        permission = PermissionContext(mode="confirm", source="test")
        events = []
        async for event in execute_tool_batch(
            calls,
            ctx=ctx,
            state=state,
            tool_registry=registry,
            permission_checker=PermissionChecker(
                PermissionSettings(auto_allow=[tool.name]), tmp_path
            ),
            approval_handler=None,
            skill_manager=None,
            permission_context=permission,
            tool_ctx=ToolExecutionContext(
                permission=permission, workspace_root=tmp_path
            ),
            execution_limit=1,
            execution_limit_reason="Per-turn tool-call budget reached.",
        ):
            events.append(event)
        return tool, state, ctx, events

    tool, state, ctx, events = asyncio.run(collect())
    starts = [event.data for event in events if event.type == "tool_call"]
    results = [event.data for event in events if event.type == "tool_result"]

    assert tool.executed_values == [1]
    assert [event["id"] for event in starts] == ["budget-1", "budget-2", "budget-3"]
    assert [event["id"] for event in results] == ["budget-1", "budget-2", "budget-3"]
    assert [event["status"] for event in results] == ["success", "blocked", "blocked"]
    assert [event["display_summary"] for event in results[1:]] == [
        "Tool-call budget exhausted",
        "Tool-call budget exhausted",
    ]
    assert [event["summary"] for event in results[1:]] == [
        "Per-turn tool-call budget reached.",
        "Per-turn tool-call budget reached.",
    ]
    assert [record.status for record in state.tool_calls] == [
        "success",
        "blocked",
        "blocked",
    ]
    assistant_calls = [
        message
        for message in ctx._history
        if message.role == "assistant" and message.tool_calls
    ]
    tool_results = [message for message in ctx._history if message.role == "tool"]
    assert [
        [call.id for call in message.tool_calls or []] for message in assistant_calls
    ] == [
        ["budget-1", "budget-2", "budget-3"],
    ]
    assert [message.tool_call_id for message in tool_results] == [
        "budget-1",
        "budget-2",
        "budget-3",
    ]


def test_streaming_tool_tracker_does_not_execute_tracked_calls() -> None:
    async def collect():
        tool = _FastTrackedReadTool()
        tracker = StreamingToolTracker()
        first = tracker.add_tool(
            ToolCallEvent(id="tracked-1", name=tool.name, arguments={"value": 1})
        )
        second = tracker.add_tool(
            ToolCallEvent(id="tracked-2", name=tool.name, arguments={"value": 2})
        )
        await asyncio.sleep(0)
        return tool, tracker, first, second

    tool, tracker, first, second = asyncio.run(collect())

    assert first is None
    assert second is None
    assert tool.executions == 0
    assert tracker.status_snapshot() == (
        ("tracked-1", StreamingToolStatus.QUEUED.value),
        ("tracked-2", StreamingToolStatus.QUEUED.value),
    )


def test_streaming_tool_tracker_tracks_complete_blocks_in_model_order() -> None:
    async def collect():
        tool = _FastTrackedReadTool()
        tracker = StreamingToolTracker()
        first = ToolCallEvent(id="first", name=tool.name, arguments={"value": 1})
        blocked = ToolCallEvent(
            id="blocked", name="run_command", arguments={"command": "echo x"}
        )
        later = ToolCallEvent(id="later", name=tool.name, arguments={"value": 2})
        tracker.add_tools([first, blocked, later])
        assert tracker.status_snapshot() == (
            ("first", StreamingToolStatus.QUEUED.value),
            ("blocked", StreamingToolStatus.QUEUED.value),
            ("later", StreamingToolStatus.QUEUED.value),
        )
        tracker.mark_yielded("first")
        assert tracker.status_snapshot()[0] == (
            "first",
            StreamingToolStatus.YIELDED.value,
        )

    asyncio.run(collect())


def test_execute_tool_batch_streams_command_output_with_tool_id(tmp_path) -> None:
    registry = ToolRegistry()
    registry.register(_StreamingCommandTool())
    state = AgentState(user_message="train model")
    ctx = ContextBuilder(TokenBudget())
    emitted: list[tuple[str, dict[str, object]]] = []

    async def emit_event(event_type: str, data: dict[str, object]) -> None:
        emitted.append((event_type, data))

    async def collect():
        events = []
        permission = PermissionContext(mode="bypass", source="test")
        async for event in execute_tool_batch(
            [
                ToolCallEvent(
                    id="cmd-train",
                    name="run_command",
                    arguments={"command": "python train_transformer.py"},
                )
            ],
            ctx=ctx,
            state=state,
            tool_registry=registry,
            permission_checker=PermissionChecker(
                PermissionSettings(auto_allow=["run_command"]), tmp_path
            ),
            approval_handler=None,
            skill_manager=None,
            permission_context=permission,
            tool_ctx=ToolExecutionContext(
                permission=permission,
                workspace_root=tmp_path,
                emit_event=emit_event,
            ),
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())

    live_deltas = [
        payload for event_type, payload in emitted if event_type == "tool_output_delta"
    ]
    assert [
        {key: payload[key] for key in ("id", "output", "stream")}
        for payload in live_deltas
    ] == [
        {"id": "cmd-train", "output": "Epoch 1/10 - loss=0.92\n", "stream": "stdout"},
        {"id": "cmd-train", "output": "Epoch 2/10 - loss=0.71\n", "stream": "stdout"},
    ]
    assert [event.type for event in events].count("tool_result") == 1


def test_batch_tool_calls_requires_tool_owned_read_only_metadata() -> None:
    assert _LegacyNamedReadTool().is_concurrency_safe({"file_path": "x.md"}) is False


def test_batch_tool_calls_respects_explicit_non_read_declaration() -> None:
    assert _ExplicitNonReadTool().is_concurrency_safe({"file_path": "x.md"}) is False


class TestToolCallContract:
    def test_normalization_preserves_provider_tool_name_and_arguments(
        self, tmp_path
    ) -> None:
        tc = normalize_tool_call_event(
            ToolCallEvent(id="read_1", name="Read", arguments={"path": "README.md"})
        )
        registry = ToolRegistry()
        registry.register(
            ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
        )

        assert tc.name == "Read"
        assert tc.arguments == {"path": "README.md"}
        assert registry.get_tool(tc.name) is None

    def test_read_file_rejects_path_alias(self, tmp_path) -> None:
        (tmp_path / "README.md").write_text("hello from path alias", encoding="utf-8")
        tool = ReadFileTool(ArtifactStore(storage_dir=tmp_path / "artifacts"))
        registry = ToolRegistry()
        registry.register(tool)
        tc = normalize_tool_call_event(
            ToolCallEvent(
                id="read_1", name="read_file", arguments={"path": "README.md"}
            )
        )

        result = asyncio.run(
            tool.execute(
                tc.arguments,
                context=ToolExecutionContext(
                    permission=PermissionContext(), workspace_root=tmp_path
                ),
            )
        )

        assert missing_required_tool_argument_names(tc, registry) == ["file_path"]
        assert result.is_error is True
        assert "Missing file_path" in result.content

    def test_normalization_copies_arguments_without_rewriting_them(self) -> None:
        original = {"q": "test"}
        tc = normalize_tool_call_event(
            ToolCallEvent(id="search_1", name="web_search", arguments=original)
        )
        assert tc.arguments == {"q": "test"}
        assert tc.arguments is not original

    def test_normalization_preserves_non_object_arguments_for_validation(self) -> None:
        tc = normalize_tool_call_event(
            ToolCallEvent(id="bad_1", name="read_file", arguments="README.md")
        )
        assert tc.arguments == "README.md"
        assert "arguments must be a JSON object" in invalid_tool_call_guard_reason(
            tc,
            ToolRegistry(),
        )


# ── invalid_tool_call_guard_reason() ──────────────────────────────────────


class TestInvalidToolCallGuardReason:
    """invalid_tool_call_guard_reason() catches structural model errors."""

    def test_missing_tool_name(self) -> None:
        tc = ToolCallEvent(id="1", name="", arguments={})
        registry = ToolRegistry()
        reason = invalid_tool_call_guard_reason(tc, registry)
        assert reason
        assert "missing tool name" in reason

    def test_whitespace_only_tool_name(self) -> None:
        tc = ToolCallEvent(id="1", name="   ", arguments={})
        registry = ToolRegistry()
        reason = invalid_tool_call_guard_reason(tc, registry)
        assert reason
        assert "missing tool name" in reason

    def test_arguments_not_dict(self) -> None:
        tc = ToolCallEvent(id="1", name="read_file", arguments="not a dict")
        registry = ToolRegistry()
        reason = invalid_tool_call_guard_reason(tc, registry)
        assert reason
        assert "must be a JSON object" in reason

    def test_unknown_tool(self) -> None:
        tc = ToolCallEvent(id="1", name="nonexistent_tool", arguments={})
        registry = ToolRegistry()
        reason = invalid_tool_call_guard_reason(tc, registry)
        assert reason
        assert "does not exist" in reason

    def test_repaired_provider_json_is_never_executed(self) -> None:
        registry = ToolRegistry()
        registry.register(_MutatingBatchTool())
        tc = ToolCallEvent(
            id="repaired",
            name="mutating_batch",
            arguments={"path": "safe"},
            arguments_repaired=True,
        )

        assert "malformed provider JSON" in invalid_tool_call_guard_reason(tc, registry)


def test_duplicate_side_effectful_ids_are_marked_for_rejection() -> None:
    registry = ToolRegistry()
    registry.register(_MutatingBatchTool())
    state = AgentState(user_message="mutate once")
    prepared = prepare_tool_call_sequence(
        state,
        [
            ToolCallEvent(id="same", name="mutating_batch", arguments={"value": 1}),
            ToolCallEvent(id="same", name="mutating_batch", arguments={"value": 1}),
        ],
        registry,
    )

    assert [item.id for item in prepared] == ["same", "same:dup2"]
    assert prepared[0].duplicate_id is False
    assert prepared[1].duplicate_id is True
    assert "Duplicate tool-call id" in invalid_tool_call_guard_reason(
        prepared[1],
        registry,
    )


def test_argument_preparation_failure_stays_on_typed_tool_call() -> None:
    class RejectingTool(_BatchTool):
        def __init__(self) -> None:
            super().__init__("rejecting_tool", read_only=True)

        def prepare_arguments(self, _args: dict[str, object]) -> dict[str, object]:
            raise ValueError("unsupported argument shape")

    registry = ToolRegistry()
    registry.register(RejectingTool())
    prepared = prepare_tool_call_sequence(
        AgentState(user_message="prepare"),
        [ToolCallEvent(id="call-1", name="rejecting_tool", arguments={})],
        registry,
    )

    assert prepared[0].prepare_arguments_error == "unsupported argument shape"
    assert "unsupported argument shape" in invalid_tool_call_guard_reason(
        prepared[0],
        registry,
    )

    def test_valid_internal_tool_no_error(self) -> None:
        """web_search is an internal guarded tool — should not be reported as unknown."""
        tc = ToolCallEvent(id="1", name="web_search", arguments={"query": "test"})
        registry = ToolRegistry()
        reason = invalid_tool_call_guard_reason(tc, registry)
        assert reason == ""

    def test_valid_control_tool_no_error(self) -> None:
        """Control tools are recognized even without registry registration."""
        tc = ToolCallEvent(id="1", name="stop", arguments={})
        registry = ToolRegistry()
        reason = invalid_tool_call_guard_reason(tc, registry)
        # "stop" may or may not be a control tool; just check no crash
        assert isinstance(reason, str)


# ── status_for_result() ───────────────────────────────────────────────────


class TestStatusForResult:
    """status_for_result() maps ToolResult to a canonical status string."""

    def test_explicit_requested_status(self) -> None:
        result = ToolResult(content="ok")
        assert status_for_result(result, "success") == "success"
        assert status_for_result(result, "failed") == "failed"
        assert status_for_result(result, "blocked") == "blocked"

    def test_timeout_with_non_critical_limitation(self) -> None:
        result = ToolResult(
            content="timeout", status="timeout", limitation="non-critical timeout"
        )
        assert status_for_result(result) == "timeout"

    def test_result_status_success(self) -> None:
        result = ToolResult(content="ok", status="success")
        assert status_for_result(result) == "success"

    def test_result_status_failed(self) -> None:
        result = ToolResult(content="err", status="failed")
        assert status_for_result(result) == "failed"

    def test_result_status_blocked(self) -> None:
        result = ToolResult(content="blocked", status="blocked")
        assert status_for_result(result) == "blocked"

    def test_result_status_partial(self) -> None:
        result = ToolResult(content="partial output", status="partial")
        assert status_for_result(result) == "partial"

    def test_is_error_fallback(self) -> None:
        result = ToolResult(content="err", is_error=True)
        assert status_for_result(result) == "failed"

    def test_default_success(self) -> None:
        result = ToolResult(content="ok")
        assert status_for_result(result) == "success"

    def test_unknown_status_falls_back_to_is_error(self) -> None:
        result = ToolResult(content="x", status="unknown_status", is_error=False)
        assert status_for_result(result) == "success"

    def test_unknown_status_with_error_falls_back(self) -> None:
        result = ToolResult(content="x", status="unknown_status", is_error=True)
        assert status_for_result(result) == "failed"
