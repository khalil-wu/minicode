from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from backend.async_cleanup import (
    await_with_deadline,
    cancel_and_drain,
    cancel_and_drain_receipt,
    cancel_and_retire,
    cancel_and_drain_to_completion,
)
from backend.agent.runtime_spans import runtime_span
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_batch_execution import execute_tool_batch
from backend.agent.tool_execution import run_tool_with_timeout
from backend.llm.base import ToolCallEvent
from backend.mcp.client import MCPCallResult, MCPToolDef
from backend.mcp.registry import MCPToolProxy
from backend.config import PermissionSettings
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.registry import ToolRegistry
from backend.tools.edit_file import EditFileTool
from backend.ws.approval_runtime import SessionApprovalRuntimeMixin
from backend.ws.handlers.misc import handle_interrupt_command
from backend.ws.turn_wait_state import TurnWaitState


def test_runtime_span_enforces_tool_correlation_timing_and_bounded_data() -> None:
    event = runtime_span(
        "tool.completed",
        span_id="span-1",
        run_id="run-1",
        turn_id="turn-1",
        phase="tool",
        status="completed",
        started_at=100,
        ended_at=125,
        duration_ms=25,
        tool_call_id="call-1",
        tool_name="read_file",
        ui_visible=True,
        debug_only=False,
        data={"exit_code": 0},
    )

    assert event.type == "runtime.span"
    assert event.data == {
        "event": "tool.completed",
        "span_id": "span-1",
        "status": "completed",
        "ui_visible": True,
        "debug_only": False,
        "run_id": "run-1",
        "turn_id": "turn-1",
        "phase": "tool",
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "started_at": 100,
        "ended_at": 125,
        "duration_ms": 25,
        "data": {"exit_code": 0},
    }

    with pytest.raises(ValueError, match="requires tool_name"):
        runtime_span(
            "tool.started",
            span_id="span-1",
            phase="tool",
            tool_call_id="call-1",
        )
    with pytest.raises(ValueError, match="Unsupported tool runtime-span event"):
        runtime_span(
            "provider.started",
            span_id="span-1",
            phase="tool",
            tool_call_id="call-1",
            tool_name="read_file",
        )
    with pytest.raises(ValueError, match="requires tool/approval phase"):
        runtime_span(
            "tool.started",
            span_id="span-1",
            phase="provider",
            tool_call_id="call-1",
            tool_name="read_file",
        )
    with pytest.raises(ValueError, match="cannot precede"):
        runtime_span(
            "provider.completed",
            span_id="span-1",
            started_at=20,
            ended_at=10,
        )
    with pytest.raises(ValueError, match="must match"):
        runtime_span(
            "provider.completed",
            span_id="span-1",
            started_at=10,
            ended_at=20,
            duration_ms=9,
        )
    with pytest.raises(ValueError, match="JSON string budget"):
        runtime_span(
            "provider.completed",
            span_id="span-1",
            data={"raw": "x" * 262_145},
        )


def test_cancel_and_drain_has_a_deadline_for_cancellation_resistant_tasks() -> None:
    async def scenario() -> tuple[float, int]:
        release = asyncio.Event()

        async def resistant() -> None:
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue

        task = asyncio.create_task(resistant())
        await asyncio.sleep(0)
        started = time.monotonic()
        pending = await cancel_and_drain(
            [task],
            timeout=0.01,
            label="test cancellation",
        )
        elapsed = time.monotonic() - started
        release.set()
        await task
        return elapsed, len(pending)

    elapsed, pending_count = asyncio.run(scenario())

    assert elapsed < 0.2
    assert pending_count == 1


def test_cleanup_receipt_distinguishes_timeout_from_completion() -> None:
    async def scenario() -> tuple[bool, int, bool]:
        release = asyncio.Event()

        async def resistant() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release.wait()

        task = asyncio.create_task(resistant())
        await asyncio.sleep(0)
        receipt = await cancel_and_drain_receipt(
            [task], timeout=0.01, label="receipt timeout"
        )
        release.set()
        await task
        return receipt.timed_out, receipt.pending, receipt.completed

    timed_out, pending, completed = asyncio.run(scenario())
    assert timed_out is True
    assert pending == 1
    assert completed is False


def test_cancel_and_drain_to_completion_never_waits_past_cleanup_deadline() -> None:
    async def scenario() -> tuple[float, int]:
        async def resistant() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.Event().wait()

        task = asyncio.create_task(resistant())
        await asyncio.sleep(0)
        started = time.monotonic()
        pending = await cancel_and_drain_to_completion(
            [task], timeout=0.01, label="bounded completion"
        )
        elapsed = time.monotonic() - started
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return elapsed, len(pending)

    elapsed, pending = asyncio.run(scenario())
    assert elapsed < 0.2
    assert pending == 1


def test_cancel_and_drain_does_not_recancel_terminal_cleanup() -> None:
    async def scenario() -> tuple[bool, bool]:
        cancellation_seen = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def lifecycle_owner() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await allow_cleanup.wait()

        task = asyncio.create_task(lifecycle_owner())
        await asyncio.sleep(0)
        task.cancel()
        await cancellation_seen.wait()

        drain = asyncio.create_task(
            cancel_and_drain([task], timeout=0.1, label="terminal cleanup")
        )
        await asyncio.sleep(0)
        allow_cleanup.set()
        pending = await drain
        return task.cancelled(), bool(pending)

    was_cancelled, remained_pending = asyncio.run(scenario())

    assert was_cancelled is False
    assert remained_pending is False


def test_cancel_and_retire_is_non_blocking_and_keeps_ownership_until_settlement() -> (
    None
):
    async def scenario() -> tuple[float, bool, bool]:
        cancellation_seen = asyncio.Event()
        release = asyncio.Event()
        retired: set[asyncio.Task[None]] = set()

        async def resistant() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()

        task = asyncio.create_task(resistant())
        await asyncio.sleep(0)
        started = time.monotonic()
        cancel_and_retire(task, owner=retired)
        elapsed = time.monotonic() - started
        await cancellation_seen.wait()
        retained_while_running = task in retired
        release.set()
        await task
        await asyncio.sleep(0)
        return elapsed, retained_while_running, task in retired

    elapsed, retained_while_running, retained_after_settlement = asyncio.run(scenario())

    assert elapsed < 0.05
    assert retained_while_running is True
    assert retained_after_settlement is False


def test_await_with_deadline_handles_already_completed_tasks_without_a_wait_race() -> (
    None
):
    async def scenario() -> bool:
        async def completed() -> int:
            return 1

        return await await_with_deadline(
            completed(),
            timeout=0.1,
            label="already complete",
        )

    assert asyncio.run(scenario()) is True


def test_await_with_deadline_retains_timeout_task_in_the_supplied_owner() -> None:
    async def scenario() -> tuple[bool, bool]:
        release = asyncio.Event()
        owner: set[asyncio.Task[None]] = set()

        async def resistant() -> None:
            await release.wait()

        completed_in_time = await await_with_deadline(
            resistant(),
            timeout=0.01,
            label="owned timeout",
            owner=owner,
        )
        retained_while_running = len(owner) == 1
        release.set()
        await asyncio.gather(*tuple(owner))
        await asyncio.sleep(0)
        return completed_in_time, retained_while_running

    completed_in_time, retained_while_running = asyncio.run(scenario())

    assert completed_in_time is False
    assert retained_while_running is True


def test_tool_timeout_waits_for_tool_settlement_before_returning() -> None:
    class ResistantTool(BaseTool):
        name = "resistant_tool"
        permission = PermissionLevel.AUTO
        timeout_seconds = 0.01

        def __init__(self, release: asyncio.Event, finished: asyncio.Event) -> None:
            self.release = release
            self.finished = finished

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Ignore cancellation until released",
                parameters={"type": "object", "properties": {}},
            )

        async def execute(self, args: dict, context=None) -> ToolResult:
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                return ToolResult(content="released")
            finally:
                self.finished.set()

    async def scenario() -> tuple[float, ToolResult, bool]:
        release = asyncio.Event()
        finished = asyncio.Event()
        registry = ToolRegistry()
        registry.register(ResistantTool(release, finished))
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"))

        async def release_later() -> None:
            await asyncio.sleep(0.05)
            release.set()

        releaser = asyncio.create_task(release_later())
        started = time.monotonic()
        result = await run_tool_with_timeout(
            ToolCallEvent(id="resistant-1", name="resistant_tool", arguments={}),
            registry,
            context,
        )
        elapsed = time.monotonic() - started
        await releaser
        await asyncio.wait_for(finished.wait(), timeout=1.0)
        return elapsed, result, finished.is_set()

    elapsed, result, finished = asyncio.run(scenario())

    assert elapsed >= 0.04
    assert elapsed < 1.0
    assert result.is_error is True
    assert result.status == "timeout"
    assert finished is True
    assert result.cleanup_receipt["completed"] is True
    assert result.cleanup_receipt["pending"] == 0
    assert result.cleanup_receipt["timed_out"] is False


def test_parallel_batch_timeout_keeps_per_call_cleanup_receipts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ResistantReadTool(BaseTool):
        name = "resistant_parallel_read"
        permission = PermissionLevel.AUTO
        read_only = True

        def __init__(self, release: asyncio.Event, finished: set[str]) -> None:
            self.release = release
            self.finished = finished

        def get_schema(self) -> ToolSchema:
            return ToolSchema(
                name=self.name,
                description="Read until released",
                parameters={
                    "type": "object",
                    "properties": {"key": {"type": "string"}},
                    "required": ["key"],
                },
            )

        async def execute(self, args: dict, context=None) -> ToolResult:
            key = str(args["key"])
            try:
                while not self.release.is_set():
                    try:
                        await self.release.wait()
                    except asyncio.CancelledError:
                        continue
                return ToolResult(content=f"released {key}")
            finally:
                self.finished.add(key)

    async def scenario() -> tuple[list[dict], dict[str, dict], int, int, set[str]]:
        release = asyncio.Event()
        finished: set[str] = set()
        registry = ToolRegistry()
        registry.register(ResistantReadTool(release, finished))
        context = ToolExecutionContext(permission=PermissionContext(mode="bypass"))
        events = [
            event
            async for event in execute_tool_batch(
                [
                    ToolCallEvent(
                        id="parallel-a",
                        name="resistant_parallel_read",
                        arguments={"key": "a"},
                    ),
                    ToolCallEvent(
                        id="parallel-b",
                        name="resistant_parallel_read",
                        arguments={"key": "b"},
                    ),
                ],
                ctx=ContextBuilder(),
                state=AgentState(user_message="parallel cleanup", max_iterations=2),
                tool_registry=registry,
                permission_checker=PermissionChecker(
                    PermissionSettings(auto_allow=["*"])
                ),
                approval_handler=None,
                skill_manager=None,
                permission_context=context.permission,
                tool_ctx=context,
            )
        ]
        results = [event.data for event in events if event.type == "tool_result"]
        owned_at_return = len(context.pending_cleanup_tasks)
        release.set()
        for _ in range(100):
            if finished == {"a", "b"} and not context.pending_cleanup_tasks:
                break
            await asyncio.sleep(0.01)
        return (
            results,
            context.cleanup_receipts,
            owned_at_return,
            len(context.pending_cleanup_tasks),
            finished,
        )

    # 50ms: comfortably above process/task startup latency so the two calls
    # deterministically register their cleanup receipts before the batch
    # timeout fires (a 1ms budget raced task startup and flaked `pending`
    # under load), while the tasks themselves block on `release`, so the
    # timeout path is still the only way this batch can finish.
    monkeypatch.setenv("MINICODE_TOOL_BATCH_TIMEOUT_SECONDS", "0.05")
    monkeypatch.setattr(
        "backend.agent.tool_batch_execution.CANCELLATION_DRAIN_TIMEOUT_SECONDS",
        0.001,
    )
    results, live_receipts, owned_at_return, owned_after, finished = asyncio.run(
        scenario()
    )

    assert [item["id"] for item in results] == ["parallel-a", "parallel-b"]
    assert all(item["status"] == "timeout" for item in results)
    assert [item["cleanup_receipt"]["pending"] for item in results] == [1, 1]
    assert (
        results[0]["cleanup_receipt"]["request_digest"]
        != results[1]["cleanup_receipt"]["request_digest"]
    )
    assert owned_at_return >= 2
    assert owned_after == 0
    assert finished == {"a", "b"}
    assert all(receipt["pending"] == 0 for receipt in live_receipts.values())
    assert all(
        receipt["cleanup_completed_after_deadline"] is True
        for receipt in live_receipts.values()
    )


def test_read_only_mcp_results_are_not_cached_between_invocations() -> None:
    class Client:
        connected = True

        def __init__(self) -> None:
            self.calls = 0

        async def call_tool(self, name, args, **_kwargs):
            self.calls += 1
            return MCPCallResult(
                content=[{"type": "text", "text": f"value-{self.calls}"}]
            )

    async def scenario() -> tuple[str, str, int]:
        client = Client()
        proxy = MCPToolProxy(
            "state",
            MCPToolDef(
                name="current_value",
                description="Read mutable remote state",
                annotations={"readOnlyHint": True},
            ),
            client,
        )
        first = await proxy.execute({"key": "same"})
        second = await proxy.execute({"key": "same"})
        return first.content, second.content, client.calls

    first, second, calls = asyncio.run(scenario())

    assert (first, second, calls) == ("value-1", "value-2", 2)


def test_auto_approval_rechecks_tool_capability_metadata() -> None:
    class Runtime(SessionApprovalRuntimeMixin):
        pass

    runtime = Runtime()
    runtime.permission_checker = PermissionChecker(PermissionSettings(auto_allow=["*"]))
    runtime.permission_context = runtime.permission_checker.build_context(
        mode="auto", source="test"
    )
    runtime.tool_registry = ToolRegistry()
    runtime.tool_registry.register(EditFileTool())

    payload = {
        "type": "approval_request",
        "tool_name": "edit_file",
        "args": {"file_path": "src/app.py", "old_string": "a", "new_string": "b"},
    }

    # Generic auto mode does not waive the diff-review capability. The
    # bypass is the explicit mode that waives ordinary approval routing.
    assert runtime._pending_tool_payload_is_auto_allowed(payload) is False


def test_interrupt_does_not_emit_done_before_run_cleanup_finishes() -> None:
    class RunManager:
        def __init__(self) -> None:
            self.delivered: set[str] = set()
            self.run_task_ids: dict[str, str] = {}

        def is_delivery_complete(self, conversation_id: str) -> bool:
            return conversation_id in self.delivered

        def mark_delivery_complete(self, conversation_id: str) -> None:
            self.delivered.add(conversation_id)

    class Session:
        def __init__(self) -> None:
            self.active_conversation_id = "conv-cancel"
            self._interrupted_conversation_ids: set[str] = set()
            self.run_manager = RunManager()
            self._conversation_streams = {
                "conv-cancel": {
                    "message_id": "assistant-cancel",
                    "turn_id": "turn-cancel",
                }
            }
            self.events: list[dict] = []

        async def cancel_agent_runs(self, **_kwargs) -> bool:
            # The run owner emits DONE after its cleanup and persistence fence.
            return True

        async def send_event(self, event) -> None:
            self.events.append(event.to_ws_message())

    async def scenario() -> tuple[list[dict], set[str], set[str]]:
        session = Session()
        assert await handle_interrupt_command(session, {}) is True
        return (
            session.events,
            session.run_manager.delivered,
            session._interrupted_conversation_ids,
        )

    events, delivered, interrupted = asyncio.run(scenario())

    assert events == []
    assert delivered == set()
    # The run owner records a real cancellation. The command handler must not
    # poison the next turn when an interrupt races with an already-finished run.
    assert interrupted == set()


def test_stale_turn_interrupt_does_not_cancel_the_current_run() -> None:
    class Session:
        def __init__(self) -> None:
            self.active_conversation_id = "conv-cancel"
            self._conversation_streams = {
                "conv-cancel": {
                    "message_id": "assistant-new",
                    "turn_id": "turn-new",
                }
            }
            self.run_manager = SimpleNamespace(
                run_task_ids={"conv-cancel": "task-new"}
            )
            self.cancel_calls: list[dict] = []

        async def cancel_agent_runs(self, **kwargs) -> bool:
            self.cancel_calls.append(kwargs)
            return True

    async def scenario() -> list[dict]:
        session = Session()
        assert (
            await handle_interrupt_command(
                session,
                {
                    "conversation_id": "conv-cancel",
                    "turn_id": "turn-old",
                    "message_id": "assistant-old",
                },
            )
            is True
        )
        return session.cancel_calls

    assert asyncio.run(scenario()) == []


def test_matching_turn_interrupt_cancels_the_current_run() -> None:
    class Session:
        def __init__(self) -> None:
            self.active_conversation_id = "conv-cancel"
            self._conversation_streams = {
                "conv-cancel": {
                    "message_id": "assistant-current",
                    "turn_id": "turn-current",
                }
            }
            self.run_manager = SimpleNamespace(
                run_task_ids={"conv-cancel": "task-current"}
            )
            self.cancel_calls: list[dict] = []

        async def cancel_agent_runs(self, **kwargs) -> bool:
            self.cancel_calls.append(kwargs)
            return True

    async def scenario() -> list[dict]:
        session = Session()
        assert (
            await handle_interrupt_command(
                session,
                {
                    "conversation_id": "conv-cancel",
                    "turn_id": "turn-current",
                    "message_id": "assistant-current",
                    "task_id": "task-current",
                },
            )
            is True
        )
        return session.cancel_calls

    assert asyncio.run(scenario()) == [
        {"conversation_id": "conv-cancel", "reason": "user_interrupted"}
    ]


def test_approval_cache_key_is_policy_scoped_and_parses_false_strictly() -> None:
    runtime = SessionApprovalRuntimeMixin()
    runtime.turn_wait_state = TurnWaitState()
    runtime.session_id = "session-a"
    runtime.active_conversation_id = "fallback-conversation"
    ordinary_args = {"command": "echo ok", "with_escalated_permissions": "false"}
    escalated_args = {"command": "echo ok", "with_escalated_permissions": "true"}
    payload = {
        "conversation_id": "conversation-a",
        "workspace_root": "C:/workspace/a",
        "permission_mode": "confirm",
        "workspace_scope": "project",
    }

    ordinary = runtime._approval_cache_key(
        "run_command", ordinary_args, payload=payload
    )
    escalated = runtime._approval_cache_key(
        "run_command", escalated_args, payload=payload
    )
    other_workspace = runtime._approval_cache_key(
        "run_command",
        ordinary_args,
        payload={**payload, "workspace_root": "C:/workspace/b"},
    )

    assert "::ordinary::" in ordinary
    assert "::escalated::" in escalated
    assert ordinary != escalated
    assert ordinary != other_workspace


def test_control_approval_payload_preserves_policy_scope() -> None:
    class Runtime(SessionApprovalRuntimeMixin):
        pass

    runtime = Runtime()
    runtime.turn_wait_state = TurnWaitState()
    runtime.approval_diff_cache = {}
    runtime.config = SimpleNamespace(
        agent=SimpleNamespace(approval_timeout_seconds=300.0)
    )
    runtime.permission_context = SimpleNamespace(
        mode="confirm", workspace_scope="worktree"
    )
    runtime.conversation_repo = SimpleNamespace(
        get_conversation=lambda _conversation_id: SimpleNamespace(
            worktree_path="C:/workspace/worktree",
            workspace_root="C:/workspace/root",
            permission_mode="confirm",
        )
    )
    captured_scope: dict[str, str] = {}

    def sanitize(_request_id, diff, **scope):
        captured_scope.update(scope)
        return diff

    runtime._sanitize_approval_diff_for_client = sanitize
    event = SimpleNamespace(
        data={
            "tool_call_id": "call-1",
            "tool_name": "run_command",
            "args": {"command": "echo ok"},
            "conversation_id": "conversation-a",
        }
    )

    payload = runtime.build_approval_request_payload(event)

    assert payload["conversation_id"] == "conversation-a"
    assert payload["timeout_seconds"] == 300.0
    assert payload["expires_at"] > 0
    assert payload["workspace_root"] == "C:/workspace/worktree"
    assert payload["permission_mode"] == "confirm"
    assert payload["workspace_scope"] == "worktree"
    assert captured_scope == {
        "conversation_id": "conversation-a",
        "turn_id": "",
        "workspace_root": "C:/workspace/worktree",
    }


def test_approval_payload_bounds_large_arguments_without_losing_action_identity() -> (
    None
):
    runtime = SessionApprovalRuntimeMixin()
    runtime.turn_wait_state = TurnWaitState()
    runtime.approval_diff_cache = {}
    runtime.permission_context = SimpleNamespace(
        mode="confirm", workspace_scope="project"
    )
    large_content = "HEAD" + ("x" * 140_000) + "TAIL"
    wide_args = {
        "command": "apply generated changes",
        "path": "src/generated.ts",
        "content": large_content,
        **{f"field_{index}": "y" * 10_000 for index in range(30)},
    }
    event = SimpleNamespace(
        data={
            "tool_call_id": "call-large",
            "tool_name": "write_file",
            "args": wide_args,
            "conversation_id": "conversation-a",
        }
    )

    payload = runtime.build_approval_request_payload(event)
    projected = payload["request"]["input"]

    assert payload["type"] == "control_request"
    assert payload["request"]["subtype"] == "can_use_tool"
    assert projected["command"] == "apply generated changes"
    assert projected["path"] == "src/generated.ts"
    assert projected["content"].startswith("HEAD")
    assert projected["content"].endswith("TAIL")
    assert "characters omitted from approval projection" in projected["content"]
    assert projected["_projection"]["truncated"] is True
    assert projected["_projection"]["original_characters"] > 200_000
    assert len(json.dumps(projected, ensure_ascii=False)) < 262_144
    assert large_content not in json.dumps(projected, ensure_ascii=False)


def test_control_approval_payload_omits_timeout_when_wait_is_unbounded() -> None:
    runtime = SessionApprovalRuntimeMixin()
    runtime.turn_wait_state = TurnWaitState()
    runtime.approval_diff_cache = {}
    runtime.permission_context = SimpleNamespace(
        mode="confirm", workspace_scope="project"
    )
    event = SimpleNamespace(
        data={
            "tool_call_id": "call-unbounded",
            "tool_name": "run_command",
            "args": {"command": "echo ok"},
        }
    )

    payload = runtime.build_approval_request_payload(event)

    assert payload["type"] == "control_request"
    assert payload["request_id"] == "call-unbounded"
    assert "timeout_seconds" not in payload
    assert "expires_at" not in payload


def test_fenceless_interrupt_cancels_live_run_but_not_stale_replay() -> None:
    class LiveSession:
        def __init__(self) -> None:
            self.active_conversation_id = "conv-stop"
            self._conversation_streams = {"conv-stop": {}}
            self.run_manager = SimpleNamespace(
                run_task_ids={"conv-stop": "task-live"}
            )
            self.cancel_calls: list[dict] = []

        async def cancel_agent_runs(self, **kwargs) -> bool:
            self.cancel_calls.append(kwargs)
            return True

    async def live() -> list[dict]:
        session = LiveSession()
        assert (
            await handle_interrupt_command(session, {"conversation_id": "conv-stop"})
            is True
        )
        return session.cancel_calls

    # cc's Esc always aborts the running turn, even before an assistant
    # message exists to carry a fence.
    assert len(asyncio.run(live())) == 1

    class IdleSession(LiveSession):
        def __init__(self) -> None:
            super().__init__()
            self._conversation_streams = {}
            self.run_manager.run_task_ids = {}

    async def idle() -> list[dict]:
        session = IdleSession()
        assert (
            await handle_interrupt_command(session, {"conversation_id": "conv-stop"})
            is True
        )
        return session.cancel_calls

    # A durable replay arriving after the turn finished stays a no-op.
    assert asyncio.run(idle()) == []
