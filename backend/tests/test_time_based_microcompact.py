from __future__ import annotations

import asyncio
from types import SimpleNamespace

from backend.agent.context import (
    TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE,
    ContextBuilder,
)
from backend.agent.state import AgentState
from backend.config import AgentSettings
from backend.llm.base import LLMMessage, ToolCallEvent


def _builder_with_tool_history(
    *,
    result_count: int = 12,
    assistant_timestamp_ms: int = 1_700_000_000_000,
    llm: object | None = None,
) -> ContextBuilder:
    builder = ContextBuilder(
        agent_settings=AgentSettings(
            time_based_microcompact_enabled=True,
            time_based_microcompact_gap_threshold_minutes=60,
            time_based_microcompact_keep_recent=5,
        ),
        llm=llm,
        conversation_id="conversation-microcompact",
    )
    for index in range(result_count):
        call_id = f"call-{index}"
        tool_name = "read_file" if index != result_count - 1 else "task"
        builder._history_store.append(  # type: ignore[attr-defined]
            LLMMessage(
                role="assistant",
                content=f"calling {tool_name}",
                tool_calls=[
                    ToolCallEvent(id=call_id, name=tool_name, arguments={"path": "x"})
                ],
                timestamp_ms=assistant_timestamp_ms + index * 10,
            )
        )
        builder._history_store.append(  # type: ignore[attr-defined]
            LLMMessage(
                role="tool",
                content=f"result-{index}",
                name=tool_name,
                tool_call_id=call_id,
                is_error=index == 1,
                timestamp_ms=assistant_timestamp_ms + index * 10 + 1,
            )
        )

    # The trigger is based on the most recent assistant response, not the
    # timestamp of the final tool result. This mirrors Claude Code's
    # ``last assistant message`` check and avoids firing during a live tool
    # loop that just appended a result.
    builder._history_store.append(  # type: ignore[attr-defined]
        LLMMessage(
            role="assistant",
            content="completed",
            timestamp_ms=assistant_timestamp_ms + result_count * 10 + 2,
        )
    )
    return builder


def _state(*, source: str = "user", role: str = "main", subagent: str = "") -> AgentState:
    state = AgentState(user_message="continue")
    state.prompt_context.update(
        {
            "query_source": source,
            "agent_role": role,
        }
    )
    if subagent:
        state.prompt_context["subagent"] = subagent
    return state


def test_time_based_microcompact_clears_old_results_and_keeps_five() -> None:
    old = 1_700_000_000_000
    builder = _builder_with_tool_history(assistant_timestamp_ms=old)
    now = old + 12 * 10 + 2 + (60 * 60 * 1000)

    cleared = builder.maybe_time_based_microcompact(_state(), now_ms=now)

    assert cleared == 6
    tool_results = [message for message in builder._history if message.role == "tool"]  # type: ignore[attr-defined]
    assert [message.content for message in tool_results[:6]] == [
        TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE
    ] * 6
    assert [message.content for message in tool_results[6:11]] == [
        f"result-{index}" for index in range(6, 11)
    ]
    assert tool_results[-1].content == "result-11"
    assert tool_results[1].is_error is True
    assert tool_results[0].tool_call_id == "call-0"
    assert tool_results[0].timestamp_ms == old + 1
    assert [
        call.name
        for message in builder._history  # type: ignore[attr-defined]
        if message.role == "assistant" and message.tool_calls
        for call in message.tool_calls
    ] == ["read_file"] * 11 + ["task"]
    assert builder._history_frozen_count == 0  # type: ignore[attr-defined]
    assert builder._last_actual_prompt_tokens == 0  # type: ignore[attr-defined]


def test_time_based_microcompact_does_not_fire_before_threshold_or_twice() -> None:
    old = 1_700_000_000_000
    builder = _builder_with_tool_history(assistant_timestamp_ms=old)
    latest = old + 12 * 10 + 2

    assert builder.maybe_time_based_microcompact(
        _state(), now_ms=latest + (60 * 60 * 1000) - 1
    ) == 0
    assert builder._history[1].content == "result-0"  # type: ignore[attr-defined]

    assert builder.maybe_time_based_microcompact(
        _state(), now_ms=latest + (60 * 60 * 1000)
    ) == 6
    assert builder.maybe_time_based_microcompact(
        _state(), now_ms=latest + (2 * 60 * 60 * 1000)
    ) == 0


def test_time_based_microcompact_is_main_thread_only_and_preserves_noncompactable_results() -> None:
    old = 1_700_000_000_000
    now = old + (2 * 60 * 60 * 1000)

    for state in (
        _state(source="side_query", role="side_query"),
        _state(source="user", role="subagent:explore", subagent="explore"),
    ):
        builder = _builder_with_tool_history(assistant_timestamp_ms=old)
        assert builder.maybe_time_based_microcompact(state, now_ms=now) == 0
        assert all(
            message.content.startswith("result-")
            for message in builder._history  # type: ignore[attr-defined]
            if message.role == "tool"
        )

    builder = _builder_with_tool_history(assistant_timestamp_ms=old)
    assert builder.maybe_time_based_microcompact(_state(), now_ms=now) == 6
    task_result = next(
        message
        for message in builder._history  # type: ignore[attr-defined]
        if message.role == "tool" and message.tool_call_id == "call-11"
    )
    assert task_result.content == "result-11"


def test_time_based_microcompact_snapshot_resume_keeps_marker_and_timestamp() -> None:
    old = 1_700_000_000_000
    builder = _builder_with_tool_history(assistant_timestamp_ms=old)
    latest = old + 12 * 10 + 2
    assert builder.maybe_time_based_microcompact(_state(), now_ms=latest + 3_600_000) == 6

    snapshot = builder.export_snapshot()
    restored = ContextBuilder(
        agent_settings=AgentSettings(
            time_based_microcompact_enabled=True,
            time_based_microcompact_gap_threshold_minutes=60,
            time_based_microcompact_keep_recent=5,
        )
    )
    restored.load_snapshot(snapshot)

    restored_result = next(
        message
        for message in restored._history  # type: ignore[attr-defined]
        if message.role == "tool" and message.tool_call_id == "call-0"
    )
    assert restored_result.content == TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE
    assert restored_result.timestamp_ms == old + 1
    assert restored.maybe_time_based_microcompact(
        _state(), now_ms=latest + 24 * 60 * 60 * 1000
    ) == 0


def test_build_boundary_and_partial_hydration_preserve_cleared_prefix(
    monkeypatch, tmp_path
) -> None:
    async def _no_git_status(_workspace_root):
        return ""

    monkeypatch.setattr(
        "backend.agent.context.build_git_status_context_async",
        _no_git_status,
    )
    old = 1_700_000_000_000
    builder = _builder_with_tool_history(assistant_timestamp_ms=old)
    old_result = next(
        message
        for message in builder._history  # type: ignore[attr-defined]
        if message.role == "tool" and message.tool_call_id == "call-0"
    )
    old_result.images = [{"media_type": "image/png", "data": "old-image"}]
    old_result.documents = [
        {
            "media_type": "application/pdf",
            "data": "old-document",
            "file_name": "old.pdf",
        }
    ]
    old_result.attachment_refs = [
        {
            "artifact_id": "old-artifact",
            "file_name": "old.png",
            "media_type": "image/png",
        }
    ]
    state = _state()
    state.workspace_context = SimpleNamespace(
        root_path=tmp_path,
        get_project_summary=lambda: "",
    )

    async def _build_request():
        await builder.start_turn("continue", state)
        return await builder.build(state)

    request = asyncio.run(_build_request())

    request_result = next(
        message
        for message in request
        if message.role == "tool" and message.tool_call_id == "call-0"
    )
    assert request_result.content == TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE
    assert request_result.timestamp_ms == old + 1
    assert request_result.images == []
    assert request_result.documents == []
    assert request_result.attachment_refs == []
    assert builder._history_frozen_count == len(builder._history)  # type: ignore[attr-defined]

    snapshot = builder.export_snapshot()
    restored = ContextBuilder(
        agent_settings=AgentSettings(
            time_based_microcompact_enabled=True,
            time_based_microcompact_gap_threshold_minutes=60,
            time_based_microcompact_keep_recent=5,
        )
    )
    pending = restored.load_snapshot_partial(snapshot, recent_history_count=5)
    restored.prepend_history_messages(
        ContextBuilder.deserialize_snapshot_history(pending)
    )

    restored_result = next(
        message
        for message in restored._history  # type: ignore[attr-defined]
        if message.role == "tool" and message.tool_call_id == "call-0"
    )
    assert restored_result.content == TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE
    assert restored_result.timestamp_ms == old + 1
    assert restored_result.images == []
    assert restored_result.documents == []
    assert restored_result.attachment_refs == []
    assert restored._history_frozen_count == len(restored._history)  # type: ignore[attr-defined]


def test_legacy_partial_hydration_matches_full_pi_timestamps() -> None:
    snapshot = {
        "history": [
            {"role": "user", "content": "inspect"},
            {
                "role": "assistant",
                "content": "reading",
                "tool_calls": [
                    {
                        "id": "call-legacy",
                        "name": "read_file",
                        "arguments": {"path": "legacy.py"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE,
                "name": "read_file",
                "tool_call_id": "call-legacy",
            },
            {"role": "user", "content": "continue"},
            {"role": "assistant", "content": "done"},
        ],
        "history_frozen_count": 5,
    }
    fully_loaded = ContextBuilder()
    fully_loaded.load_snapshot(snapshot)

    partially_loaded = ContextBuilder()
    pending = partially_loaded.load_snapshot_partial(
        snapshot,
        recent_history_count=2,
    )
    partially_loaded.prepend_history_messages(
        ContextBuilder.deserialize_snapshot_history(pending)
    )

    assert [
        message.timestamp_ms
        for message in partially_loaded._history  # type: ignore[attr-defined]
    ] == [
        message.timestamp_ms
        for message in fully_loaded._history  # type: ignore[attr-defined]
    ] == [1, 2, 3, 4, 5]
    assert partially_loaded._history[2].content == (  # type: ignore[attr-defined]
        TIME_BASED_MICROCOMPACT_CLEARED_MESSAGE
    )
    assert partially_loaded._history_frozen_count == 5  # type: ignore[attr-defined]


def test_time_based_microcompact_respects_openai_24h_retention() -> None:
    class _OpenAI24h:
        _settings = SimpleNamespace(prompt_cache_retention="24h")

    old = 1_700_000_000_000
    builder = _builder_with_tool_history(assistant_timestamp_ms=old, llm=_OpenAI24h())
    latest = old + 12 * 10 + 2

    assert builder.maybe_time_based_microcompact(
        _state(), now_ms=latest + 61 * 60 * 1000
    ) == 0
    assert builder.maybe_time_based_microcompact(
        _state(), now_ms=latest + 24 * 60 * 60 * 1000
    ) == 6
