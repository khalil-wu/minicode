from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.provider_stream_control import (
    ProviderRetryReset,
    reset_for_provider_retry,
)
from backend.agent.provider_stream_error_event import (
    ProviderErrorEventResult,
    handle_provider_error_event,
)
from backend.agent.message import AgentEvent
from backend.agent.provider_event_projection import (
    ProviderProjectionResult,
    project_non_text_provider_event,
)
from backend.agent.policies.stream_retry import DefaultStreamRetryPolicy
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.config import AgentSettings
from backend.llm.base import (
    ProviderActivityEvent,
    StreamEvent,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)
from backend.ws.stream_state import apply_stream_event, create_stream_state
from backend.ws.handler import WebSocketSession
from backend.ws.turn_wait_state import TurnWaitState


def test_provider_managed_activity_projects_one_stable_information_row() -> None:
    event = StreamEvent(
        type=StreamEventType.PROVIDER_ACTIVITY,
        provider_activity=ProviderActivityEvent(
            id="hosted-search-1",
            kind="web_search_call",
            name="Web search",
            status="completed",
            message="Web search completed — 3 sources",
            detail="Provider-managed search",
            count=3,
        ),
    )

    async def collect():
        return [
            item
            async for item in project_non_text_provider_event(
                event,
                stream_state=StreamAttemptState(),
                stream_text=StreamTextState(iteration_id="iter:activity"),
                live_text_streaming=True,
                tool_tracker=SimpleNamespace(),
                tool_registry=SimpleNamespace(),
                tool_context=None,
                process_event_factory=lambda *_args, **_kwargs: None,
            )
        ]

    updates = asyncio.run(collect())

    progress = updates[0]
    assert progress.type == "agent.progress"
    assert progress.data == {
        "id": "provider:hosted-search-1",
        "stage": "tool",
        "status": "completed",
        "message": "Web search completed — 3 sources",
        "phase": "tool",
        "summary": "Web search completed — 3 sources",
        "visibility": "timeline",
        "label": "Web search",
        "detail": "Provider-managed search",
        "count": 3,
    }
    assert updates[1] == ProviderProjectionResult(True)


def test_whitespace_only_reasoning_delta_is_consumed_without_runtime_failure() -> None:
    event = StreamEvent(
        type=StreamEventType.THINKING_CHUNK,
        content=" \n\t",
        raw={"provider_reasoning_type": "thinking_delta"},
        content_kind="thinking",
        lifecycle="delta",
    )

    async def collect():
        return [
            item
            async for item in project_non_text_provider_event(
                event,
                stream_state=StreamAttemptState(),
                stream_text=StreamTextState(iteration_id="iter:thinking"),
                live_text_streaming=True,
                tool_tracker=SimpleNamespace(),
                tool_registry=SimpleNamespace(),
                tool_context=None,
                process_event_factory=lambda *_args, **_kwargs: None,
            )
        ]

    updates = asyncio.run(collect())

    assert updates == [ProviderProjectionResult(True)]


def test_provider_activity_terminal_snapshots_keep_safe_code_and_argument_counts() -> (
    None
):
    code = "DO_NOT_PROJECT_CODE_BODY_openai_84"
    arguments = '{"query":"DO_NOT_PROJECT_ARGUMENT_BODY_openai_42"}'
    activities = [
        ProviderActivityEvent(
            id="code-1",
            kind="code_interpreter_call",
            name="Provider code execution",
            status="running",
            message="Provider code prepared",
            detail=f"Code: {len(code)} characters",
        ),
        ProviderActivityEvent(
            id="code-1",
            kind="code_interpreter_call",
            name="Provider code execution",
            status="running",
            message="Provider code interpreting",
        ),
        ProviderActivityEvent(
            id="code-1",
            kind="code_interpreter_call",
            name="Provider code execution",
            status="completed",
            message="Provider code execution completed",
        ),
        ProviderActivityEvent(
            id="mcp-1",
            kind="mcp_call",
            name="MCP tool",
            status="running",
            message="MCP tool call prepared: lookup",
            detail=(
                "Server: audit-local · Tool: lookup · "
                f"Arguments: {len(arguments)} characters"
            ),
        ),
        ProviderActivityEvent(
            id="mcp-1",
            kind="mcp_call",
            name="MCP tool",
            status="completed",
            message="MCP tool completed: lookup",
            detail="Server: audit-local · Tool: lookup",
        ),
        # Duplicate terminal frames and late running frames must not append a
        # second row or downgrade the terminal lifecycle.
        ProviderActivityEvent(
            id="mcp-1",
            kind="mcp_call",
            name="MCP tool",
            status="completed",
            message="MCP tool completed: lookup",
            detail="Server: audit-local · Tool: lookup",
        ),
        ProviderActivityEvent(
            id="mcp-1",
            kind="mcp_call",
            name="MCP tool",
            status="running",
            message="MCP tool in progress: lookup",
        ),
    ]
    stream_state = StreamAttemptState()

    async def collect():
        updates = []
        for activity in activities:
            async for item in project_non_text_provider_event(
                StreamEvent(
                    type=StreamEventType.PROVIDER_ACTIVITY,
                    provider_activity=activity,
                ),
                stream_state=stream_state,
                stream_text=StreamTextState(iteration_id="iter:activity"),
                live_text_streaming=True,
                tool_tracker=SimpleNamespace(),
                tool_registry=SimpleNamespace(),
                tool_context=None,
                process_event_factory=lambda *_args, **_kwargs: None,
            ):
                if not isinstance(item, ProviderProjectionResult):
                    updates.append(item)
        return updates

    progress = asyncio.run(collect())

    code_terminal = next(
        item
        for item in progress
        if item.data["id"] == "provider:code-1" and item.data["status"] == "completed"
    )
    assert code_terminal.data["detail"] == f"Code: {len(code)} characters"
    mcp_terminal = [item for item in progress if item.data["id"] == "provider:mcp-1"][
        -1
    ]
    assert mcp_terminal.data["status"] == "completed"
    assert mcp_terminal.data["message"] == "MCP tool completed: lookup"
    assert mcp_terminal.data["detail"] == (
        f"Server: audit-local · Tool: lookup · Arguments: {len(arguments)} characters"
    )
    assert code not in str([item.data for item in progress])
    assert arguments not in str([item.data for item in progress])
    assert len([item for item in progress if item.data["id"] == "provider:mcp-1"]) == 2


def test_reconnectable_progress_snapshot_merges_detail_and_clears_ephemeral() -> None:
    streams = {
        "conv-provider": create_stream_state(
            "conv-provider",
            "message-provider",
            "turn-provider",
        )
    }
    running = {
        "id": "provider:mcp-restore",
        "stage": "tool",
        "phase": "tool",
        "status": "running",
        "message": "MCP tool call prepared: lookup",
        "summary": "MCP tool call prepared: lookup",
        "visibility": "timeline",
        "detail": "Server: audit-local · Tool: lookup · Arguments: 47 characters",
        "ephemeral": True,
    }
    completed = {
        "id": "provider:mcp-restore",
        "stage": "tool",
        "phase": "tool",
        "status": "completed",
        "message": "MCP tool completed: lookup",
        "summary": "MCP tool completed: lookup",
        "visibility": "timeline",
        "detail": "Server: audit-local · Tool: lookup",
    }

    apply_stream_event(streams, "conv-provider", "agent.progress", running)
    snapshot = apply_stream_event(
        streams,
        "conv-provider",
        "agent.progress",
        completed,
    )
    snapshot = apply_stream_event(
        streams,
        "conv-provider",
        "agent.progress",
        running,
    )

    assert snapshot is not None
    assert snapshot["content_blocks"] == [
        {
            "type": "progress",
            "id": "provider:mcp-restore",
            "stage": "tool",
            "phase": "tool",
            "status": "completed",
            "message": "MCP tool completed: lookup",
            "summary": "MCP tool completed: lookup",
            "visibility": "timeline",
            "detail": ("Server: audit-local · Tool: lookup · Arguments: 47 characters"),
            "timestamp": snapshot["content_blocks"][0]["timestamp"],
        }
    ]


def test_reemit_pending_state_preserves_completed_commentary_source() -> None:
    async def scenario() -> list[dict]:
        streams = {
            "conv-commentary": create_stream_state(
                "conv-commentary",
                "assistant-commentary",
                "turn-commentary",
            )
        }
        apply_stream_event(
            streams,
            "conv-commentary",
            "item.started",
            {
                "item": {
                    "id": "commentary-1",
                    "type": "agent_message",
                    "source": "commentary",
                }
            },
        )
        apply_stream_event(
            streams,
            "conv-commentary",
            "agent_message.delta",
            {
                "item_id": "commentary-1",
                "delta": "Inspecting the queue.",
                "source": "commentary",
            },
        )
        apply_stream_event(
            streams,
            "conv-commentary",
            "item.completed",
            {
                "item": {
                    "id": "commentary-1",
                    "type": "agent_message",
                    "text": "Inspecting the queue.",
                    "source": "commentary",
                    "status": "completed",
                }
            },
        )

        session = object.__new__(WebSocketSession)
        session.turn_wait_state = TurnWaitState()
        session._conversation_streams = streams
        blocker = asyncio.Event()
        active_task = asyncio.create_task(blocker.wait())
        session.run_manager = SimpleNamespace(
            run_tasks={"conv-commentary": active_task}
        )
        emitted: list[dict] = []

        async def capture(event: AgentEvent) -> None:
            emitted.append(event.to_ws_message())

        session.send_event = capture
        try:
            await session.reemit_pending_state("conv-commentary")
        finally:
            active_task.cancel()
            try:
                await active_task
            except asyncio.CancelledError:
                pass
        return emitted

    emitted = asyncio.run(scenario())

    assert len(emitted) == 1
    resumed = emitted[0]
    assert resumed["type"] == "stream_resume"
    assert resumed["phase"] == "model"
    assert resumed["content_blocks"] == [
        {
            "type": "text",
            "itemId": "commentary-1",
            "content": "Inspecting the queue.",
            "source": "commentary",
            "status": "completed",
            "isStreaming": False,
        }
    ]


def test_provider_retry_discards_tracked_tools_before_resetting_payload() -> None:
    calls: list[str] = []

    class ToolExecutor:
        def cancel_remaining(self) -> None:
            calls.append("cancel_tools")

    class StreamText:
        def cancel_active_agent_message(self):
            calls.append("cancel_message")
            return None

        def reset_for_provider_retry(self) -> None:
            calls.append("reset_text")

    class StreamState:
        usage = SimpleNamespace(input_tokens=0, output_tokens=0)

        def take_unsettled_tool_announcements(self):
            calls.append("settle_announcements")
            return (("call_truncated", "write_file"),)

        def reset_provider_payload(self) -> None:
            calls.append("reset_payload")

    async def collect():
        return [
            item
            async for item in reset_for_provider_retry(
                stream_text=StreamText(),
                stream_state=StreamState(),
                tool_tracker=ToolExecutor(),
            )
        ]

    events = asyncio.run(collect())

    # The announced-but-unexecuted tool block has to be closed while the
    # abandoned attempt still knows about it: reset_provider_payload() forgets
    # it, and nothing downstream can ever settle that card afterwards.
    assert calls == [
        "cancel_tools",
        "cancel_message",
        "settle_announcements",
        "reset_text",
        "reset_payload",
    ]
    settled = [
        event
        for event in events
        if getattr(event, "type", "") == "tool_result"
        and event.data.get("id") == "call_truncated"
    ]
    assert len(settled) == 1
    assert settled[0].data.get("status") == "cancelled"
    assert isinstance(events[-1], ProviderRetryReset)


def test_provider_payload_reset_removes_abandoned_tool_attempt_state() -> None:
    state = StreamAttemptState(
        tool_calls=[ToolCallEvent(id="old", name="read_file", arguments={})],
        saw_partial_tool_call=True,
        final_tool_batch_received=True,
        partial_tool_names={"old": "read_file"},
        partial_tool_args={"old": {"file_path": "README.md"}},
        provider_activities={
            "hosted-old": ProviderActivityEvent(
                id="hosted-old",
                kind="web_search_call",
                name="Web search",
                status="running",
                message="Searching the web",
            )
        },
    )

    state.reset_provider_payload()

    assert state.tool_calls == []
    assert state.saw_partial_tool_call is False
    assert state.final_tool_batch_received is False
    assert state.partial_tool_names == {}
    assert state.partial_tool_args == {}
    assert state.provider_activities == {}


def test_provider_stream_settlement_marks_eof_without_done_as_failed() -> None:
    from backend.agent.provider_stream_settlement import settle_provider_stream

    class Kernel:
        def __init__(self) -> None:
            self.closed = []

        async def close_provider_attempt(self, _attempt, **kwargs):
            self.closed.append(kwargs)

    class Budget:
        def record_provider_usage_total(self, _usage):
            pass

    class Chain:
        def record_usage(self, **_kwargs):
            pass

    kernel = Kernel()

    async def collect():
        return [
            item
            async for item in settle_provider_stream(
                retry_budget_boundary=None,
                budget_runtime=Budget(),
                turn_kernel=kernel,
                provider_attempt=object(),
                finish_reason="",
                provider_stream_steered=False,
                rebuild_context_and_retry=False,
                state=SimpleNamespace(stopped_reason=""),
                pending_tool_calls=[],
                provider_raw_done={},
                provider_done=False,
                visible_text_sanitizer=None,
                stream_state=SimpleNamespace(finish_reason=""),
                stream_text=SimpleNamespace(sanitize=lambda _scrub: None),
                context_builder=SimpleNamespace(
                    record_actual_usage=lambda *_args, **_kwargs: None
                ),
                usage=UsageInfo(),
                turn_usage=UsageInfo(),
                chain=Chain(),
            )
        ]

    updates = asyncio.run(collect())
    assert kernel.closed[0]["status"] == "failed"
    assert kernel.closed[0]["data"]["error_type"] == "provider_terminal_missing"


def test_provider_error_uses_structured_429_and_retry_after_metadata_without_turn_budget(
    monkeypatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay_seconds: float, _cancel_event=None) -> None:
        sleeps.append(delay_seconds)

    monkeypatch.setattr(
        "backend.agent.provider_stream_error_event.sleep_or_cancel",
        fake_sleep,
    )

    class TurnKernel:
        def __init__(self) -> None:
            self.closed: list[dict[str, object]] = []
            self.spans: list[dict[str, object]] = []

        async def close_provider_attempt(self, _attempt, **kwargs) -> None:
            self.closed.append(kwargs)

        async def emit_runtime_span(self, event: str, **kwargs) -> None:
            self.spans.append({"event": event, **kwargs})

    class BudgetRuntime:
        def consume_retry(self, reason: str):
            raise AssertionError(
                f"provider stream retries must not consume the turn budget: {reason}"
            )

    async def degrade_and_finish(**_kwargs):
        if False:
            yield None

    async def recover_withheld_error(**_kwargs) -> bool:
        return False

    turn_kernel = TurnKernel()
    budget_runtime = BudgetRuntime()
    event = StreamEvent(
        type=StreamEventType.ERROR,
        content="generic provider failure",
        raw={
            "provider": "custom",
            "provider_error_type": "rate_limit",
            "status_code": 429,
            "retry_after_seconds": 7.0,
        },
    )

    async def collect():
        return [
            item
            async for item in handle_provider_error_event(
                event,
                state=SimpleNamespace(),
                context_builder=SimpleNamespace(),
                turn_kernel=turn_kernel,
                provider_attempt=object(),
                stream_state=SimpleNamespace(
                    incomplete_tool_stream=False,
                    saw_partial_tool_call=False,
                ),
                stream_text=SimpleNamespace(full_text=""),
                pending_tool_calls=[],
                usage=UsageInfo(),
                turn_usage=UsageInfo(),
                stream_retry_policy=DefaultStreamRetryPolicy(AgentSettings()),
                stream_attempt=0,
                stream_recovery_attempted=False,
                budget_runtime=budget_runtime,
                error_controller=SimpleNamespace(),
                iteration_id_value="iter:1",
                cancel_event=None,
                degrade_and_finish=degrade_and_finish,
                recover_withheld_error=recover_withheld_error,
                max_retries=10,
            )
        ]

    updates = asyncio.run(collect())

    assert sleeps == [7.0]
    assert turn_kernel.closed[0]["data"] == {
        "error_type": "api",
        "provider_error_type": "rate_limit",
        "status_code": 429,
    }
    assert turn_kernel.spans[0]["data"] == {
        "stream_attempt": 1,
        "retry_attempt": 1,
        "max_retries": 10,
        "provider_error_type": "rate_limit",
        "error_type": "api",
    }
    assert updates[0].type == "agent.progress"
    assert updates[0].data["id"] == "provider:connection:iter:1"
    assert updates[0].data["status"] == "running"
    assert updates[0].data["retry_attempt"] == 1
    assert updates[0].data["max_retries"] == 10
    assert updates[0].data["retry_after_ms"] == 7000
    assert updates[1].type == "rate_limit"
    assert updates[1].data["provider"] == "custom"
    assert updates[1].data["retry_after_seconds"] == 7.0
    result = updates[-1]
    assert isinstance(result, ProviderErrorEventResult)
    assert result.action == "retry"
    assert result.stream_attempt == 1
    assert result.retry_budget_boundary is None


def test_provider_error_retries_structured_525_instead_of_finishing(
    monkeypatch,
) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay_seconds: float, _cancel_event=None) -> None:
        sleeps.append(delay_seconds)

    monkeypatch.setattr(
        "backend.agent.provider_stream_error_event.sleep_or_cancel",
        fake_sleep,
    )

    class TurnKernel:
        async def close_provider_attempt(self, _attempt, **_kwargs) -> None:
            return None

        async def emit_runtime_span(self, _event: str, **_kwargs) -> None:
            return None

    class BudgetRuntime:
        def consume_retry(self, _reason: str):
            return None

    async def degrade_and_finish(**_kwargs):
        raise AssertionError("transient 525 must not finish without retrying")
        yield

    async def recover_withheld_error(**_kwargs) -> bool:
        return False

    event = StreamEvent(
        type=StreamEventType.ERROR,
        content="Cloudflare SSL handshake failed; request was blocked",
        raw={
            "provider": "openai_chat_completions",
            "provider_error_type": "blocked",
            "status_code": 525,
        },
    )

    async def collect():
        return [
            item
            async for item in handle_provider_error_event(
                event,
                state=SimpleNamespace(),
                context_builder=SimpleNamespace(),
                turn_kernel=TurnKernel(),
                provider_attempt=object(),
                stream_state=SimpleNamespace(
                    incomplete_tool_stream=False,
                    saw_partial_tool_call=False,
                ),
                stream_text=SimpleNamespace(full_text=""),
                pending_tool_calls=[],
                usage=UsageInfo(),
                turn_usage=UsageInfo(),
                stream_retry_policy=DefaultStreamRetryPolicy(AgentSettings()),
                stream_attempt=0,
                stream_recovery_attempted=False,
                budget_runtime=BudgetRuntime(),
                error_controller=SimpleNamespace(),
                iteration_id_value="iter:525",
                cancel_event=None,
                degrade_and_finish=degrade_and_finish,
                recover_withheld_error=recover_withheld_error,
            )
        ]

    updates = asyncio.run(collect())

    assert len(sleeps) == 1
    # Default base delay 0.5s with up to +25% jitter (cc getRetryDelay shape).
    assert 0.5 <= sleeps[0] <= 0.625
    assert len(updates) == 2
    assert updates[0].type == "agent.progress"
    assert updates[0].data["id"] == "provider:connection:iter:525"
    assert updates[0].data["status"] == "running"
    assert isinstance(updates[-1], ProviderErrorEventResult)
    assert updates[-1].action == "retry"


def test_provider_protocol_conversion_failure_is_fatal_and_never_enters_retry_budget() -> (
    None
):
    class TurnKernel:
        def __init__(self) -> None:
            self.closed: list[dict[str, object]] = []

        async def close_provider_attempt(self, _attempt, **kwargs) -> None:
            self.closed.append(kwargs)

    class BudgetRuntime:
        def consume_retry(self, reason: str):
            raise AssertionError(
                f"protocol failure must not consume retry budget: {reason}"
            )

    async def degrade_and_finish(**_kwargs):
        yield AgentEvent.error("protocol failure", error_type="provider_protocol")

    async def recover_withheld_error(**_kwargs) -> bool:
        return False

    kernel = TurnKernel()
    event = StreamEvent(
        type=StreamEventType.ERROR,
        content="gateway could not convert request",
        raw={
            "provider": "custom",
            "provider_error_code": "convert_request_failed",
            "provider_error_schema_type": "1301",
            "status_code": 500,
        },
    )

    async def collect():
        return [
            item
            async for item in handle_provider_error_event(
                event,
                state=SimpleNamespace(),
                context_builder=SimpleNamespace(),
                turn_kernel=kernel,
                provider_attempt=object(),
                stream_state=SimpleNamespace(
                    incomplete_tool_stream=False,
                    saw_partial_tool_call=False,
                ),
                stream_text=SimpleNamespace(
                    full_text="",
                    pending_recovery_text=lambda _scrubber: "",
                ),
                pending_tool_calls=[],
                usage=UsageInfo(),
                turn_usage=UsageInfo(),
                stream_retry_policy=DefaultStreamRetryPolicy(AgentSettings()),
                stream_attempt=0,
                stream_recovery_attempted=False,
                budget_runtime=BudgetRuntime(),
                error_controller=SimpleNamespace(),
                iteration_id_value="iter:protocol",
                cancel_event=None,
                degrade_and_finish=degrade_and_finish,
                recover_withheld_error=recover_withheld_error,
            )
        ]

    updates = asyncio.run(collect())

    assert kernel.closed == [
        {
            "status": "failed",
            "summary": "Provider stream returned an error",
            "data": {
                "error_type": "provider_protocol",
                "provider_error_type": "protocol",
                "status_code": 500,
                "provider_error_code": "convert_request_failed",
                "provider_error_schema_type": "1301",
            },
        }
    ]
    assert isinstance(updates[-1], ProviderErrorEventResult)
    assert updates[-1].action == "finish"
    assert all(
        not isinstance(update, AgentEvent) or update.type != "agent.progress"
        for update in updates
    )


def test_provider_retry_uses_stream_budget_instead_of_turn_recovery_budget() -> None:
    class TurnKernel:
        def __init__(self) -> None:
            self.closed: list[dict[str, object]] = []

        async def close_provider_attempt(self, _attempt, **kwargs) -> None:
            self.closed.append(kwargs)

    class BudgetRuntime:
        def consume_retry(self, reason: str):
            raise AssertionError(
                f"provider stream retries must not consume the turn budget: {reason}"
            )

    async def degrade_and_finish(**_kwargs):
        raise AssertionError("budget exhaustion must finish before degradation")
        yield

    async def recover_withheld_error(**_kwargs) -> bool:
        return False

    kernel = TurnKernel()
    event = StreamEvent(
        type=StreamEventType.ERROR,
        content="temporary network failure",
        raw={"provider_error_type": "network", "status_code": 503},
    )

    async def collect():
        return [
            item
            async for item in handle_provider_error_event(
                event,
                state=SimpleNamespace(),
                context_builder=SimpleNamespace(),
                turn_kernel=kernel,
                provider_attempt=object(),
                stream_state=SimpleNamespace(
                    incomplete_tool_stream=False,
                    saw_partial_tool_call=False,
                ),
                stream_text=SimpleNamespace(full_text=""),
                pending_tool_calls=[],
                usage=UsageInfo(),
                turn_usage=UsageInfo(),
                stream_retry_policy=DefaultStreamRetryPolicy(AgentSettings()),
                stream_attempt=0,
                stream_recovery_attempted=False,
                budget_runtime=BudgetRuntime(),
                error_controller=SimpleNamespace(),
                iteration_id_value="iter:budget",
                cancel_event=None,
                degrade_and_finish=degrade_and_finish,
                recover_withheld_error=recover_withheld_error,
                max_retries=2,
            )
        ]

    updates = asyncio.run(collect())

    assert len(kernel.closed) == 1
    assert kernel.closed[0]["status"] == "failed"
    assert kernel.closed[0]["summary"] == "Provider stream returned an error; retrying"
    assert kernel.closed[0]["project_progress"] is False
    assert len(updates) == 2
    assert updates[0].type == "agent.progress"
    assert updates[0].data["retry_attempt"] == 1
    assert updates[0].data["max_retries"] == 2
    assert isinstance(updates[-1], ProviderErrorEventResult)
    assert updates[-1].action == "retry"
    assert updates[-1].retry_budget_boundary is None
