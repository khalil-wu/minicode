from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.answer_recovery import recover_empty_answer
from backend.agent.max_output_recovery import (
    _continuation_provider_items,
    recover_max_output,
)
from backend.agent.loop_runtime_helpers import is_max_output_finish_reason
from backend.agent.context import ContextBuilder
from backend.agent.provider_response_recovery import (
    PostStreamRecoveryResult,
    recover_provider_response,
)
from backend.agent.recovery_controller import (
    RecoveryController,
    RecoveryDependencies,
    RecoveryProfile,
)
from backend.agent.state import AgentState
from backend.agent.stream_attempt import StreamAttemptState, StreamTextState
from backend.llm.base import UsageInfo


def test_empty_provider_refusal_uses_provider_explanation_and_category() -> None:
    state = AgentState(user_message="request")
    result = asyncio.run(
        recover_empty_answer(
            state=state,
            stream_text=StreamTextState(iteration_id="iter:refusal"),
            turn_usage=UsageInfo(input_tokens=4, output_tokens=1),
            finish_reason="refusal",
            provider_raw_done={
                "provider": "anthropic",
                "refusal": {
                    "type": "refusal",
                    "category": "cyber",
                    "explanation": "  This request crosses the allowed boundary.  ",
                },
            },
        )
    )

    assert result.action == "terminate"
    error = result.events[0]
    assert error.type == "error"
    assert error.data == {
        "message": (
            "The model declined to respond. Provider explanation: "
            "This request crosses the allowed boundary."
        ),
        "recoverable": False,
        "error_type": "refusal",
        "error_code": "provider_refusal_cyber",
        "provider_error_type": "refusal",
    }
    assert state.transition == "refusal"
    assert state.stopped_reason == "refusal"


def test_stream_interruption_profile_disables_partial_commit_for_tool_fragments() -> (
    None
):
    profile = RecoveryProfile.stream_interrupted(
        error_message="incomplete",
        error_type="incomplete_tool_stream",
        failed_stopped_reason="incomplete_tool_stream",
        recoverable=True,
        saw_partial_tool_call=True,
    )

    assert profile.allow_partial_text_commit is False
    assert profile.failed_stopped_reason == "incomplete_tool_stream"


def test_timeout_profile_preserves_recoverable_terminal_contract() -> None:
    profile = RecoveryProfile.timeout(
        saw_partial_tool_call=False,
    )

    assert profile.error_type == "timeout"
    assert profile.failed_stopped_reason == "timeout"
    assert profile.recoverable is True
    assert profile.allow_partial_text_commit is True


def test_fatal_provider_failure_cannot_enter_last_resort() -> None:
    profile = RecoveryProfile.provider_failure(
        error_message="unauthorized",
        error_type="authentication",
        failed_stopped_reason="auth",
        recoverable=False,
        provider_error_type="invalid_api_key",
    )

    assert profile.recoverable is False
    assert profile.provider_error_type == "invalid_api_key"


def test_provider_failure_runs_existing_stop_failure_hook_once() -> None:
    hook_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def run_stop_failure_hook(*args, **kwargs) -> None:
        hook_calls.append((args, kwargs))

    controller = RecoveryController(
        state=SimpleNamespace(stopped_reason=None, terminal_status=None),
        ctx=SimpleNamespace(),
        dependencies=RecoveryDependencies(
            scrub_thinking_tags=lambda text: text,
            usage_terminal_projection=lambda *args, **kwargs: None,
            run_stop_failure_hook=run_stop_failure_hook,
        ),
    )
    profile = RecoveryProfile.provider_failure(
        error_message="Model authentication failed.",
        error_type="authentication",
        failed_stopped_reason="auth",
        recoverable=False,
        provider_error_type="invalid_api_key",
    )

    async def collect_events():
        return [
            event
            async for event in controller.finish(
                usage=UsageInfo(),
                stream_text=SimpleNamespace(),
                full_text="",
                pending_tool_calls=[],
                profile=profile,
            )
        ]

    events = asyncio.run(collect_events())

    assert len(hook_calls) == 1
    assert hook_calls[0] == (
        ("authentication",),
        {
            "error_details": "Model authentication failed.",
            "last_assistant_message": "",
        },
    )
    assert [event.type for event in events] == ["error"]


def test_exhausted_max_output_recovery_runs_stop_failure_hook() -> None:
    hook_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def run_stop_failure_hook(*args, **kwargs) -> None:
        hook_calls.append((args, kwargs))

    class _StreamText:
        @staticmethod
        def accepted_answer_text(scrub_text) -> str:
            return "partial answer"

        @staticmethod
        def start_agent_message():
            return None

        @staticmethod
        def complete_active_agent_message(*args, **kwargs):
            return None

    class _ToolExecutor:
        @staticmethod
        def cancel_remaining() -> None:
            return None

    state = SimpleNamespace(
        max_output_partial_text="",
        max_output_recovery_count=3,
        reply="",
        stopped_reason=None,
        terminal_status=None,
    )

    result = asyncio.run(
        recover_max_output(
            state=state,
            stream_text=_StreamText(),
            tool_tracker=_ToolExecutor(),
            context_builder=SimpleNamespace(),
            budget_runtime=SimpleNamespace(),
            provider_items=[],
            turn_usage=UsageInfo(),
            finish_reason="length",
            scrub_text=lambda text: text,
            run_stop_failure_hook=run_stop_failure_hook,
        )
    )

    assert result.action == "terminate"
    assert hook_calls == [
        (
            ("max_output_tokens",),
            {
                "error_details": (
                    "The provider stopped because the output limit was reached and "
                    "continuation recovery was exhausted."
                ),
                "last_assistant_message": "partial answer",
            },
        )
    ]


def test_provider_pause_turn_replays_native_content_without_user_nudge() -> None:
    state = AgentState(user_message="research")
    context = ContextBuilder()
    context.append_user("research")
    provider_item = {
        "type": "anthropic_message",
        "content": [
            {"type": "text", "text": "Working."},
            {
                "type": "server_tool_use",
                "id": "srv-1",
                "name": "web_search",
                "input": {"query": "MiniCode"},
            },
        ],
    }
    stream_state = StreamAttemptState(response_items=[provider_item])
    stream_text = StreamTextState(
        iteration_id="iter:1",
        pending_unphased_text="Working.",
        pending_unphased_visible_text="Working.",
    )

    class _ToolExecutor:
        cancelled = False

        def cancel_remaining(self) -> None:
            self.cancelled = True

    class _Budget:
        calls: list[str] = []

        def consume_retry(self, reason: str):
            self.calls.append(reason)
            state.total_retries += 1
            state.recovery_iterations += 1
            return None

    tool_tracker = _ToolExecutor()
    budget = _Budget()

    async def collect():
        return [
            update
            async for update in recover_provider_response(
                state=state,
                stream_state=stream_state,
                stream_text=stream_text,
                tool_tracker=tool_tracker,
                context_builder=context,
                budget_runtime=budget,
                turn_usage=UsageInfo(input_tokens=3, output_tokens=2),
                finish_reason="pause_turn",
                scrub_text=lambda text: text,
                tool_batch_count=0,
                degraded_reason="",
            )
        ]

    updates = asyncio.run(collect())
    result = next(
        update for update in updates if isinstance(update, PostStreamRecoveryResult)
    )
    history = context.export_snapshot()["history"]

    assert result.action == "retry"
    assert budget.calls == ["provider_pause_turn"]
    assert tool_tracker.cancelled is True
    assert state.provider_continuation_recovery_count == 1
    assert state.transition == "provider_pause_turn"
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[-1]["content"] == "Working."
    assert history[-1]["provider_items"] == [provider_item]
    assert stream_text.pending_unphased_text == ""
    assert not any(message["role"] == "user" for message in history[1:])


def test_provider_pause_turn_without_native_state_fails_closed() -> None:
    state = AgentState(user_message="research")
    stream_state = StreamAttemptState(response_items=[])
    stream_text = StreamTextState(iteration_id="iter:1")

    class _ToolExecutor:
        cancelled = False

        def cancel_remaining(self) -> None:
            self.cancelled = True

    tool_tracker = _ToolExecutor()

    async def collect():
        return [
            update
            async for update in recover_provider_response(
                state=state,
                stream_state=stream_state,
                stream_text=stream_text,
                tool_tracker=tool_tracker,
                context_builder=ContextBuilder(),
                budget_runtime=SimpleNamespace(),
                turn_usage=UsageInfo(),
                finish_reason="pause_turn",
                scrub_text=lambda text: text,
                tool_batch_count=0,
                degraded_reason="",
            )
        ]

    updates = asyncio.run(collect())
    result = next(
        update for update in updates if isinstance(update, PostStreamRecoveryResult)
    )
    error = next(update for update in updates if getattr(update, "type", "") == "error")

    assert result.action == "terminate"
    assert error.data["error_code"] == "pause_turn_missing_provider_state"
    assert state.stopped_reason == "provider_protocol"
    assert state.terminal_status == "failed"
    assert tool_tracker.cancelled is True


def test_provider_compaction_stop_replays_opaque_block_and_retries() -> None:
    state = AgentState(user_message="continue")
    context = ContextBuilder()
    context.append_user("continue")
    provider_item = {
        "type": "anthropic_message",
        "content": [
            {
                "type": "compaction",
                "content": "summary",
                "encrypted_content": "opaque",
            }
        ],
    }
    stream_state = StreamAttemptState(response_items=[provider_item])
    stream_text = StreamTextState(iteration_id="iter:1")

    class _ToolExecutor:
        @staticmethod
        def cancel_remaining() -> None:
            return None

    class _Budget:
        calls: list[str] = []

        def consume_retry(self, reason: str):
            self.calls.append(reason)
            return None

    budget = _Budget()

    async def collect():
        return [
            update
            async for update in recover_provider_response(
                state=state,
                stream_state=stream_state,
                stream_text=stream_text,
                tool_tracker=_ToolExecutor(),
                context_builder=context,
                budget_runtime=budget,
                turn_usage=UsageInfo(),
                finish_reason="compaction",
                scrub_text=lambda text: text,
                tool_batch_count=0,
                degraded_reason="",
            )
        ]

    updates = asyncio.run(collect())
    result = next(
        update for update in updates if isinstance(update, PostStreamRecoveryResult)
    )

    assert result.action == "retry"
    assert budget.calls == ["provider_compaction"]
    assert state.transition == "provider_compaction"
    assert state.provider_continuation_recovery_count == 1
    assert context.export_snapshot()["history"][-1]["provider_items"] == [provider_item]


def test_max_output_continuation_removes_nested_anthropic_tool_use() -> None:
    provider_items = [
        {
            "type": "anthropic_message",
            "content": [
                {"type": "thinking", "thinking": "reason", "signature": "sig"},
                {"type": "text", "text": "partial"},
                {
                    "type": "tool_use",
                    "id": "incomplete-call",
                    "name": "write_file",
                    "input": {"path": "a.txt"},
                },
                {
                    "type": "server_tool_use",
                    "id": "server-call",
                    "name": "web_search",
                    "input": {"query": "q"},
                },
            ],
        },
        {"type": "function_call", "call_id": "openai-call"},
    ]

    assert _continuation_provider_items(provider_items) == [
        {
            "type": "anthropic_message",
            "content": [
                {"type": "thinking", "thinking": "reason", "signature": "sig"},
                {"type": "text", "text": "partial"},
                {
                    "type": "server_tool_use",
                    "id": "server-call",
                    "name": "web_search",
                    "input": {"query": "q"},
                },
            ],
        }
    ]


def test_model_context_window_finish_reason_compacts_before_continuation() -> None:
    state = AgentState(user_message="continue")
    stream_text = StreamTextState(
        iteration_id="iter:1",
        pending_unphased_text="partial answer",
        pending_unphased_visible_text="partial answer",
    )

    class _Context:
        operations: list[object] = []

        async def full_compact(self, *, restore_state):
            self.operations.append(("compact", restore_state is state))
            return "compacted summary"

        def append_assistant(self, content, **kwargs):
            self.operations.append(("assistant", content, kwargs))

        def append_user(self, content):
            self.operations.append(("user", content))

    class _ToolExecutor:
        @staticmethod
        def cancel_remaining() -> None:
            return None

    class _Budget:
        @staticmethod
        def consume_retry(reason: str):
            assert reason == "max_output_tokens_recovery"
            return None

    hook_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def stop_hook(*args, **kwargs):
        hook_calls.append((args, kwargs))

    context = _Context()
    result = asyncio.run(
        recover_max_output(
            state=state,
            stream_text=stream_text,
            tool_tracker=_ToolExecutor(),
            context_builder=context,
            budget_runtime=_Budget(),
            provider_items=[
                {
                    "type": "anthropic_message",
                    "content": [{"type": "text", "text": "partial answer"}],
                }
            ],
            turn_usage=UsageInfo(),
            finish_reason="model_context_window_exceeded",
            scrub_text=lambda text: text,
            run_stop_failure_hook=stop_hook,
        )
    )

    assert is_max_output_finish_reason("model_context_window_exceeded") is True
    assert result.action == "retry"
    assert context.operations[0] == ("compact", True)
    assert context.operations[1] == (
        "assistant",
        "partial answer",
        {
            "phase": "final_answer",
            "provider_items": [
                {
                    "type": "anthropic_message",
                    "content": [{"type": "text", "text": "partial answer"}],
                }
            ],
        },
    )
    assert context.operations[2][0] == "user"
    assert state.reactive_compaction_attempted is True
    assert state.max_output_recovery_count == 1
    assert state.transition_details["context_compacted"] is True
    assert hook_calls == []


def test_empty_max_output_stops_after_one_continuation_with_safe_cap_details() -> None:
    state = AgentState(user_message="hello")

    class _StreamText:
        resets = 0

        @staticmethod
        def accepted_answer_text(scrub_text) -> str:
            return ""

        @staticmethod
        def start_agent_message():
            return None

        @staticmethod
        def complete_active_agent_message(*args, **kwargs):
            return None

        def reset_for_retry(self) -> None:
            self.resets += 1

    class _ToolExecutor:
        @staticmethod
        def cancel_remaining() -> None:
            return None

    class _Context:
        prompts: list[str] = []

        def append_user(self, content: str) -> None:
            self.prompts.append(content)

    class _Budget:
        retries: list[str] = []

        def consume_retry(self, reason: str):
            self.retries.append(reason)
            return None

    hook_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    async def stop_hook(*args, **kwargs) -> None:
        hook_calls.append((args, kwargs))

    stream_text = _StreamText()
    context = _Context()
    budget = _Budget()
    raw_done = {
        "request_summary": {
            "request_params": {
                "max_tokens": 8_192,
            },
        },
    }

    first = asyncio.run(
        recover_max_output(
            state=state,
            stream_text=stream_text,
            tool_tracker=_ToolExecutor(),
            context_builder=context,
            budget_runtime=budget,
            provider_items=[],
            turn_usage=UsageInfo(),
            finish_reason="max_output",
            scrub_text=lambda text: text,
            run_stop_failure_hook=stop_hook,
            provider_raw_done=raw_done,
        )
    )
    second = asyncio.run(
        recover_max_output(
            state=state,
            stream_text=stream_text,
            tool_tracker=_ToolExecutor(),
            context_builder=context,
            budget_runtime=budget,
            provider_items=[],
            turn_usage=UsageInfo(),
            finish_reason="max_output",
            scrub_text=lambda text: text,
            run_stop_failure_hook=stop_hook,
            provider_raw_done=raw_done,
        )
    )

    assert first.action == "retry"
    assert any(
        event.type == "agent.progress"
        and event.data["id"] == "max_output_recovery"
        and event.data["message"] == "正在恢复被截断的输出"
        for event in first.events
    )
    assert second.action == "terminate"
    assert state.max_output_recovery_count == 1
    assert state.max_output_no_progress_count == 2
    assert stream_text.resets == 1
    assert budget.retries == ["max_output_tokens_recovery"]
    terminal_error = next(event for event in second.events if event.type == "error")
    assert "max_tokens=8192" in terminal_error.data["message"]
    assert "Authorization" not in terminal_error.data["message"]
    assert "hello" not in terminal_error.data["message"]
    assert hook_calls[0][1]["error_details"] == terminal_error.data["message"]


def test_max_output_partial_recovery_deduplicates_equal_text_and_appends_only_growth() -> (
    None
):
    state = AgentState(user_message="continue")

    class _StreamText:
        text = "abc"

        def accepted_answer_text(self, scrub_text) -> str:
            return self.text

        @staticmethod
        def start_agent_message():
            return None

        @staticmethod
        def complete_active_agent_message(*args, **kwargs):
            return None

        @staticmethod
        def reset_for_retry() -> None:
            return None

    class _ToolExecutor:
        @staticmethod
        def cancel_remaining() -> None:
            return None

    class _Context:
        def append_assistant(self, content, **kwargs):
            return None

        def append_user(self, content):
            return None

    class _Budget:
        @staticmethod
        def consume_retry(reason: str):
            return None

    async def stop_hook(*args, **kwargs) -> None:
        raise AssertionError("growing partial output must remain recoverable")

    stream_text = _StreamText()
    kwargs = {
        "state": state,
        "stream_text": stream_text,
        "tool_tracker": _ToolExecutor(),
        "context_builder": _Context(),
        "budget_runtime": _Budget(),
        "provider_items": [],
        "turn_usage": UsageInfo(),
        "finish_reason": "length",
        "scrub_text": lambda text: text,
        "run_stop_failure_hook": stop_hook,
    }

    first = asyncio.run(recover_max_output(**kwargs))
    stream_text.text = "abc"
    second = asyncio.run(recover_max_output(**kwargs))
    stream_text.text = "abcdef"
    third = asyncio.run(recover_max_output(**kwargs))

    assert [first.action, second.action, third.action] == ["retry", "retry", "retry"]
    assert state.max_output_partial_text == "abcdef"
    assert state.max_output_last_partial_text == "abcdef"
    assert state.max_output_no_progress_count == 0
