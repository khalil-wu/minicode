import asyncio
import logging
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from backend.agent.compaction import format_compaction_history, parse_compaction_output
from backend.agent.history_store import estimate_message_tokens
from backend.agent.context import (
    COMPACTION_SUMMARY_PREFIX,
    COMPACTION_SUMMARY_SUFFIX,
    ContextBuilder,
)
from backend.agent.context_ledger import estimate_native_attachments
from backend.agent.loop import AgentLoopSessionContext, run_agent_loop
from backend.agent.message import AgentEvent, UserCommand
from backend.agent.state import AgentState
from backend.artifact.store import ArtifactStore
from backend.attachments.store import AttachmentStore
from backend.config import (
    AgentSettings,
    AppConfig,
    LLMSettings,
    PermissionSettings,
    TokenBudget,
    load_config,
)
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    LLMSideCallContext,
    StreamEvent,
    StreamEventType,
    ToolCallEvent,
    UsageInfo,
)
from backend.llm.errors import classify_llm_error, sanitize_llm_error_message
from backend.main import app
from backend.mcp.manager import MCPServerConfig, MCPServerManager, ServerStatus
from backend.permissions.checker import PermissionChecker
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.mcp.client import MCPClient
from backend.tools.base import BaseTool, ToolResult, ToolSchema
from backend.tools.agent_tools import TaskTool
from backend.tools.registry import ToolRegistry
from backend.ws.handler import WebSocketSession


class CountingContent:
    def __init__(self, text: str) -> None:
        self.text = text
        self.len_calls = 0

    def __len__(self) -> int:
        self.len_calls += 1
        return len(self.text)

    def __str__(self) -> str:
        return self.text


def _valid_compaction_summary(label: str) -> str:
    return (
        f"## Goal\n{label}\n\n"
        "## Constraints & Preferences\n- Preserve existing constraints\n\n"
        "## Progress\n- Summary generated\n\n"
        "## Key Decisions\n- Keep the structured compaction contract\n\n"
        "## Next Steps\n1. Continue the task\n\n"
        f"## Critical Context\n- {label}"
    )


class _SummaryLLM(LLMAdapter):
    def __init__(self, model: str, response: str) -> None:
        self._model = model
        self.response = _valid_compaction_summary(response)
        self.simple_calls = 0
        self.simple_max_tokens: list[int | None] = []
        self.simple_messages: list[list[LLMMessage]] = []

    async def stream_chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict[str, object]] | None = None,
    ):
        if False:
            yield StreamEvent(type=StreamEventType.DONE)

    async def simple_chat(
        self,
        messages: list[LLMMessage],
        *,
        max_tokens: int | None = None,
    ) -> str:
        self.simple_calls += 1
        self.simple_max_tokens.append(max_tokens)
        self.simple_messages.append(messages)
        return self.response


def test_compaction_history_formatter_keeps_recent_role_summaries() -> None:
    messages = [
        LLMMessage(role="user", content="first request"),
        LLMMessage(role="assistant", content="first answer"),
        LLMMessage(role="tool", content="tool output", name="read_file"),
    ]

    raw_text = format_compaction_history(messages)

    assert raw_text == (
        "[User]: first request\n\n"
        "[Assistant]: first answer\n\n"
        "[Tool result]: tool output"
    )


def test_parse_compaction_output_preserves_upstream_summary_text() -> None:
    output = "\n## Active Task\nFix context compaction\n"

    parsed = parse_compaction_output(output)

    assert parsed.summary == "## Active Task\nFix context compaction"


def test_context_builder_token_usage_uses_cached_history_estimates() -> None:
    builder = ContextBuilder()
    content = CountingContent("x" * 120)

    builder.append_user(content)  # type: ignore[arg-type]

    first = builder.token_usage
    second = builder.token_usage

    # token_usage must be stable across repeated reads (cached history estimates).
    assert first == second
    assert first > 0


def test_context_estimates_follow_minicode_chars_per_token_contract() -> None:
    assert estimate_message_tokens("") == 0
    assert estimate_message_tokens("abcd") == 1
    assert estimate_message_tokens("abcde") == 2
    assert estimate_message_tokens("中文测试") == 1


def test_native_attachment_estimates_follow_minicode_fixed_contract() -> None:
    tokens, count, sources = estimate_native_attachments(
        [{"media_type": "image/png", "data": "iVBORw0KGgo="}],
        [
            {
                "file_name": "paper.pdf",
                "media_type": "application/pdf",
                "data": "JVBERi0=",
            }
        ],
    )

    assert tokens == 4_000
    assert count == 2
    assert sources == ["image/png", "paper.pdf"]


def test_context_builder_uses_minicode_reserve_boundary() -> None:
    builder = ContextBuilder()
    builder.append_user("x" * 800)
    state = AgentState(user_message="short", iterations=1)
    usage = builder.get_budget_snapshot(state)["used"]
    builder._budget = replace(  # type: ignore[attr-defined]
        builder._budget,
        total=usage + 100,
        system_prompt=0,
        active_skills=0,
        memory_index=0,
        tool_schemas=0,
        agent_state=0,
        response_reserve=100,
    )

    assert (
        builder.needs_compaction(state) is False
    )

    builder._budget = replace(builder._budget, total=usage + 99)  # type: ignore[attr-defined]
    assert (
        builder.needs_compaction(state) is True
    )


def test_context_builder_compaction_boundary_uses_rendered_request_shape() -> None:
    builder = ContextBuilder()
    builder.append_user("x" * 800)
    state = AgentState(user_message="tools", iterations=5)
    for index in range(11):
        state.record_tool_call(f"tool_{index}", {"index": index}, "ok")
    builder._compaction_count = 3  # type: ignore[attr-defined]
    usage = builder.get_budget_snapshot(state)["used"]
    builder._budget = replace(  # type: ignore[attr-defined]
        builder._budget,
        total=usage + 100,
        system_prompt=0,
        active_skills=0,
        memory_index=0,
        tool_schemas=0,
        agent_state=0,
        response_reserve=100,
    )

    assert builder.needs_compaction(state) is False


def test_context_builder_compaction_counts_tool_schema_budget() -> None:
    builder = ContextBuilder(
        token_budget=TokenBudget(
            total=120,
            system_prompt=0,
            active_skills=0,
            memory_index=0,
            tool_schemas=0,
            agent_state=0,
            response_reserve=0,
        )
    )
    tool_schemas = [{"function": {"name": "very_large_tool", "description": "x" * 900}}]

    assert (
        builder.needs_compaction(
            AgentState(user_message="schema", iterations=4),
            tool_schemas=tool_schemas,
        )
        is True
    )


def test_context_builder_uses_actual_provider_usage_as_budget_floor() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=100_000))
    state = AgentState(user_message="continue")

    before = builder.get_budget_snapshot(state)["used"]
    builder.record_actual_usage(UsageInfo(input_tokens=90_000, output_tokens=100))
    after = builder.get_budget_snapshot(state)

    assert before < 90_000
    assert after["used"] == 90_000
    assert after["breakdown"]["observed_actual"] == 90_000
    assert builder.needs_compaction(state) is True


def test_budget_snapshot_counts_the_rendered_provider_request_once() -> None:
    builder = ContextBuilder(token_budget=TokenBudget(total=100_000))
    state = AgentState(user_message="inspect")
    state.tool_runtime_guidance = "runtime guidance " * 20
    state.retrieved_chunks = ["retrieved evidence " * 20]
    request_messages = asyncio.run(builder.build("inspect", state))
    expected = sum(
        estimate_message_tokens(message.content, message.tool_calls)
        for message in request_messages
    )

    assert builder.get_budget_snapshot(state)["used"] == expected


def test_context_builder_does_not_share_compaction_results_through_process_cache() -> (
    None
):
    llm = _SummaryLLM(model="model-a", response="cached summary")
    builder = ContextBuilder(llm=llm)
    early = [
        LLMMessage(role="user", content="first request"),
        LLMMessage(role="assistant", content="first answer"),
    ]

    first = asyncio.run(builder._summarize_early(early))
    second = asyncio.run(builder._summarize_early(early))

    assert first == llm.response
    assert second == llm.response
    assert llm.simple_calls == 2


def test_context_builder_compact_preserves_recent_message_objects() -> None:
    llm = _SummaryLLM(model="model-a", response="cache-safe summary")
    builder = ContextBuilder(
        agent_settings=AgentSettings(compaction_keep_recent_tokens=8), llm=llm
    )
    old_tool = LLMMessage(
        role="tool",
        content="old tool output " + ("x" * 1000),
        name="read_file",
        tool_call_id="call_old",
    )
    old_user = LLMMessage(role="user", content="old user context " + ("y" * 1000))
    recent_user = LLMMessage(role="user", content="recent user request")
    recent_assistant = LLMMessage(role="assistant", content="recent assistant answer")
    for message in [old_tool, old_user, recent_user, recent_assistant]:
        builder._history_store.append(message)  # type: ignore[attr-defined]

    old_tool_content = old_tool.content
    old_user_content = old_user.content
    recent_user_content = recent_user.content
    recent_assistant_content = recent_assistant.content

    summary = asyncio.run(builder.compact(focus="continue safely"))

    assert summary == llm.response
    assert builder._history[1] is recent_user  # type: ignore[attr-defined]
    assert builder._history[2] is recent_assistant  # type: ignore[attr-defined]
    assert recent_user.content == recent_user_content
    assert recent_assistant.content == recent_assistant_content
    assert old_tool.content == old_tool_content
    assert old_user.content == old_user_content


def test_context_builder_compact_summarizes_split_turn_prefix() -> None:
    llm = _SummaryLLM(model="model-a", response="summary checkpoint")
    builder = ContextBuilder(
        agent_settings=AgentSettings(compaction_keep_recent_tokens=20), llm=llm
    )
    tool_call = LLMMessage(
        role="assistant",
        tool_calls=[
            ToolCallEvent(id="call_recent", name="read_file", arguments={})
        ],
    )
    tool_result = LLMMessage(
        role="tool",
        content="small result",
        name="read_file",
        tool_call_id="call_recent",
    )
    recent_answer = LLMMessage(role="assistant", content="done")
    for message in [
        LLMMessage(role="user", content="old request"),
        LLMMessage(role="assistant", content="old answer"),
        LLMMessage(role="user", content="current request"),
        tool_call,
        tool_result,
        recent_answer,
    ]:
        builder._history_store.append(message)  # type: ignore[attr-defined]

    summary = asyncio.run(builder.compact())

    assert llm.simple_calls == 2
    # Compaction follows the fixed 20k summary cap used by MiniCode;
    # it is not derived from the recent-context reserve.
    assert llm.simple_max_tokens == [20_000, 20_000]
    assert summary == (
        f"{llm.response}\n\n---\n\n"
        f"**Turn Context (split turn):**\n\n{llm.response}"
    )
    assert builder._history[1:] == [  # type: ignore[attr-defined]
        tool_call,
        tool_result,
        recent_answer,
    ]


def test_context_builder_updates_previous_compaction_summary() -> None:
    llm = _SummaryLLM(model="model-a", response="updated summary")
    builder = ContextBuilder(llm=llm)
    previous = (
        f"{COMPACTION_SUMMARY_PREFIX}"
        "## Goal\nPreserve this goal"
        f"{COMPACTION_SUMMARY_SUFFIX}"
    )

    summary = asyncio.run(
        builder._summarize_early(
            [
                LLMMessage(role="user", content=previous),
                LLMMessage(role="user", content="new progress"),
                LLMMessage(role="assistant", content="implemented change"),
            ]
        )
    )

    prompt = llm.simple_messages[0][-1].content
    assert summary == llm.response
    assert "<previous-summary>\n## Goal\nPreserve this goal\n</previous-summary>" in prompt
    assert "<conversation>" in prompt
    assert "[User]: new progress" in prompt
    assert COMPACTION_SUMMARY_PREFIX not in prompt


def test_context_builder_retries_transient_compaction_failure() -> None:
    class _FlakySummaryLLM(_SummaryLLM):
        def __init__(self, model: str, response: str) -> None:
            super().__init__(model, response)
            self.side_session_ids: list[str] = []
            self.side_cache_flags: list[bool] = []

        async def _side_query_chat(
            self,
            messages,
            *,
            context: LLMSideCallContext,
        ):
            self.simple_calls += 1
            self.simple_max_tokens.append(context.options.max_tokens)
            self.simple_messages.append(messages)
            options = context.options
            self.side_session_ids.append(options.session_id)
            self.side_cache_flags.append(options.enable_prompt_cache)
            if self.simple_calls < 3:
                raise RuntimeError("503 service unavailable")
            return self.response

    llm = _FlakySummaryLLM(model="model-a", response="summary after retry")
    builder = ContextBuilder(
        agent_settings=AgentSettings(
            stream_max_attempts=3,
            stream_retry_delay_seconds=0,
        ),
        llm=llm,
    )

    summary = asyncio.run(
        builder._summarize_early(
            [
                LLMMessage(role="user", content="request"),
                LLMMessage(role="assistant", content="work"),
            ]
        )
    )

    assert summary == llm.response
    assert llm.simple_calls == 3
    assert llm.simple_max_tokens == [20_000, 20_000, 20_000]
    assert len(set(llm.side_session_ids)) == 1
    assert llm.side_cache_flags == [False, False, False]


def test_compaction_has_one_side_query_retry_owner(monkeypatch) -> None:
    """A two-retry compaction budget means exactly three provider requests."""

    class _CountingSummaryLLM(_SummaryLLM):
        def __init__(self, model: str, response: str) -> None:
            super().__init__(model, response)
            self.options_seen = []

        async def _side_query_chat(
            self,
            messages,
            *,
            context: LLMSideCallContext,
        ):
            self.simple_calls += 1
            self.simple_max_tokens.append(context.options.max_tokens)
            self.simple_messages.append(messages)
            options = context.options
            self.options_seen.append(options)
            if self.simple_calls < 3:
                raise RuntimeError("503 service unavailable")
            return self.response

    sleep_calls: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleep_calls.append(delay)

    monkeypatch.setattr("backend.llm.base.asyncio.sleep", no_sleep)
    llm = _CountingSummaryLLM(model="model-a", response="bounded compaction")
    builder = ContextBuilder(
        agent_settings=AgentSettings(
            # A large foreground stream budget must not multiply compaction's
            # independent side-query retry count.
            stream_max_attempts=10,
            stream_retry_delay_seconds=0,
        ),
        llm=llm,
    )

    summary = asyncio.run(
        builder._summarize_early(
            [
                LLMMessage(role="user", content="request"),
                LLMMessage(role="assistant", content="work"),
            ]
        )
    )

    assert summary == llm.response
    assert llm.simple_calls == 3
    assert len(sleep_calls) == 2
    assert {options.operation for options in llm.options_seen} == {"compact"}
    assert {options.query_source for options in llm.options_seen} == {"compact"}
    assert {options.max_retries for options in llm.options_seen} == {2}
    assert len({options.session_id for options in llm.options_seen}) == 1


def test_context_builder_compact_resets_actual_usage_floor() -> None:
    llm = _SummaryLLM(model="model-a", response="compact after actual usage")
    builder = ContextBuilder(
        token_budget=TokenBudget(total=100_000),
        agent_settings=AgentSettings(compaction_keep_recent_tokens=1),
        llm=llm,
    )
    builder.append_user("old request")
    builder.append_assistant("old answer")
    builder.append_user("recent request")
    builder.record_actual_usage(UsageInfo(input_tokens=90_000, output_tokens=100))

    before = builder.token_usage
    asyncio.run(builder.compact(focus="shrink context"))
    after = builder.token_usage

    assert before >= 90_000
    assert after < 90_000


def test_context_builder_token_accounting_preserves_compact_guideline_reason(
    monkeypatch,
) -> None:
    load_calls: list[tuple[Path | None, str]] = []
    cache_clears: list[bool] = []

    def load_guidelines(
        workspace_root: Path | None = None,
        *,
        load_reason: str = "session_start",
        project_root_markers=None,
        project_doc_fallback_filenames=None,
        project_doc_max_bytes=None,
        hook_manager=None,
    ) -> str:
        load_calls.append((workspace_root, load_reason))
        return "project guidance"

    monkeypatch.setattr(
        "backend.agent.instruction_discovery.load_project_guidelines",
        load_guidelines,
    )
    monkeypatch.setattr(
        "backend.agent.instruction_discovery.clear_guideline_cache",
        lambda: cache_clears.append(True),
    )

    llm = _SummaryLLM(model="model-a", response="compact guideline lifecycle")
    builder = ContextBuilder(
        agent_settings=AgentSettings(compaction_keep_recent_tokens=1),
        llm=llm,
    )
    builder.append_user("old request")
    builder.append_assistant("old answer")
    builder.append_user("recent request")

    asyncio.run(builder.compact(focus="continue"))

    assert builder._guideline_load_reason == "compact"
    assert cache_clears == []

    _ = builder.token_usage

    assert load_calls == [(None, "session_start")]
    assert builder._guideline_load_reason == "compact"
    assert cache_clears == []

    state = AgentState(user_message="continue")
    asyncio.run(builder.build("continue", state))

    assert load_calls[-1] == (None, "compact")
    assert builder._guideline_load_reason == "session_start"
    assert cache_clears == [True]

    builder.base_system_prompt(state)

    assert load_calls[-1] == (None, "session_start")
    assert cache_clears == [True]


def test_context_builder_compact_restores_recent_workspace_files(
    tmp_path: Path,
) -> None:
    source = tmp_path / "src" / "app.py"
    source.parent.mkdir()
    source.write_text("line 1\nline 2\nline 3\n", encoding="utf-8")
    llm = _SummaryLLM(model="model-a", response="summary after compaction")
    builder = ContextBuilder(
        agent_settings=AgentSettings(compaction_keep_recent_tokens=1), llm=llm
    )
    builder.append_user("old request")
    builder.append_assistant("old answer")
    builder.append_user("recent request")
    state = AgentState(user_message="continue")
    state.workspace_context = SimpleNamespace(root_path=tmp_path)
    state.record_tool_call("read_file", {"file_path": "src/app.py"}, "ok")

    summary = asyncio.run(builder.compact(focus="continue", restore_state=state))
    snapshot = builder.export_snapshot()
    notes = snapshot["persistent_notes"]

    assert summary == llm.response
    assert any(note["kind"] == "post_compaction_restore" for note in notes)
    restored = next(
        note["content"] for note in notes if note["kind"] == "post_compaction_restore"
    )
    assert "src/app.py" in restored
    assert "line 1" in restored
    assert "line 3" in restored


def test_context_builder_compact_restore_ignores_paths_outside_workspace(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}_outside.txt"
    outside.write_text("do not restore", encoding="utf-8")
    llm = _SummaryLLM(model="model-a", response="summary")
    builder = ContextBuilder(
        agent_settings=AgentSettings(compaction_keep_recent_tokens=1), llm=llm
    )
    builder.append_user("old request")
    builder.append_assistant("old answer")
    builder.append_user("recent request")
    state = AgentState(user_message="continue")
    state.workspace_context = SimpleNamespace(root_path=tmp_path)
    state.record_tool_call("read_file", {"file_path": str(outside)}, "ok")

    asyncio.run(builder.compact(focus="continue", restore_state=state))

    restored = [
        note
        for note in builder.export_snapshot()["persistent_notes"]
        if note["kind"] == "post_compaction_restore"
    ]
    assert restored == []


def test_context_builder_reconciles_dangling_tool_calls_in_assistant_order() -> None:
    builder = ContextBuilder()
    builder.append_assistant_tool_calls(
        [
            ToolCallEvent(
                id="call_a", name="read_file", arguments={"file_path": "a.py"}
            ),
            ToolCallEvent(
                id="call_b", name="read_file", arguments={"file_path": "b.py"}
            ),
            ToolCallEvent(
                id="call_c", name="read_file", arguments={"file_path": "c.py"}
            ),
        ],
        content="checking files",
    )

    inserted = builder.reconcile_dangling_tool_calls()
    inserted_again = builder.reconcile_dangling_tool_calls()
    tool_messages = [
        message
        for message in builder.export_snapshot()["history"]
        if message["role"] == "tool"
    ]

    assert inserted == 3
    assert inserted_again == 0
    assert [message["tool_call_id"] for message in tool_messages] == [
        "call_a",
        "call_b",
        "call_c",
    ]
    assert all("did not complete" in message["content"] for message in tool_messages)


def test_context_builder_drops_orphan_tool_messages_and_preserves_adjacency() -> None:
    builder = ContextBuilder()
    builder.load_snapshot(
        {
            "history": [
                {"role": "tool", "content": "old orphan", "tool_call_id": "orphan"},
                {
                    "role": "assistant",
                    "content": "checking",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "name": "read_file",
                            "arguments": {"file_path": "a.py"},
                        },
                        {
                            "id": "call_b",
                            "name": "grep_files",
                            "arguments": {"pattern": "needle"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "content": "a contents",
                    "name": "read_file",
                    "tool_call_id": "call_a",
                },
                {"role": "user", "content": "continue"},
                {
                    "role": "tool",
                    "content": "late b result",
                    "name": "grep_files",
                    "tool_call_id": "call_b",
                },
            ]
        }
    )

    inserted = builder.reconcile_dangling_tool_calls()
    history = builder.export_snapshot()["history"]

    assert inserted == 1
    assert [message["role"] for message in history] == [
        "assistant",
        "tool",
        "tool",
        "user",
    ]
    assert history[1]["tool_call_id"] == "call_a"
    assert history[2]["tool_call_id"] == "call_b"
    assert "did not complete" in history[2]["content"]
    assert all(message.get("tool_call_id") != "orphan" for message in history)
    assert all(message.get("content") != "late b result" for message in history)


def test_context_builder_sanitizes_internal_fallback_prompts_and_duplicate_retry_users() -> (
    None
):
    builder = ContextBuilder()
    builder.load_snapshot(
        {
            "history": [
                {"role": "user", "content": "What happened today?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "name": "web_search",
                            "arguments": {"query": "today news"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "content": "Searched web",
                    "name": "web_search",
                    "tool_call_id": "call_a",
                },
                {
                    "role": "user",
                    "content": (
                        "Use the tool results above to answer the user's original question. "
                        "Give the final answer directly and do not call more tools."
                    ),
                },
                {"role": "user", "content": "What happened today?"},
                {"role": "user", "content": "What happened today?"},
                {"role": "assistant", "content": "(empty)"},
                {
                    "role": "user",
                    "content": "This is a current/time-sensitive answer backed by fetched web evidence. Include an absolute date.",
                },
                {"role": "user", "content": "What happened today?"},
            ],
        }
    )

    history = builder.export_snapshot()["history"]

    assert not any(
        "Use the tool results above" in message["content"] for message in history
    )
    assert [message["content"] for message in history if message["role"] == "user"] == [
        "What happened today?",
        "What happened today?",
    ]
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]


def test_context_builder_restore_removes_runtime_control_prompts() -> None:
    # ``export_snapshot`` is deliberately lossless -- it is the authoritative
    # resume/replay boundary and must reproduce the exact provider-visible
    # bodies of the turn that produced it. Runtime control prompts (the
    # empty-reply nudge, the date-grounding instruction) are stripped on the
    # *restore* boundary instead, by ``sanitize_snapshot_history``, which
    # ``load_snapshot`` runs. That is what keeps them from leaking into a later
    # session.
    builder = ContextBuilder()
    builder.append_user("What happened today?")
    builder.append_assistant("(empty)")
    builder.append_user(
        "你执行了工具调用但返回了空回复。请根据上面的工具结果提供你的回答。"
    )
    builder.append_user(
        "This is a current/time-sensitive answer backed by fetched web evidence. "
        "Include an absolute date, for example 2026-06-03, in the final answer."
    )
    builder.append_assistant("Here is the answer with a date.")

    snapshot = builder.export_snapshot()
    restored = ContextBuilder()
    restored.load_snapshot(snapshot)

    history = restored.export_snapshot()["history"]
    assert [message["role"] for message in history] == ["user", "assistant"]
    assert [message["content"] for message in history] == [
        "What happened today?",
        "Here is the answer with a date.",
    ]
    assert [
        (message["role"], message["content"])
        for message in ContextBuilder.sanitize_snapshot_history(snapshot["history"])
    ] == [
        ("user", "What happened today?"),
        ("assistant", "Here is the answer with a date."),
    ]


def test_context_builder_does_not_silently_truncate_history_to_fit_budget() -> None:
    budget = TokenBudget(
        total=45,
        system_prompt=0,
        active_skills=0,
        memory_index=0,
        tool_schemas=0,
        agent_state=0,
        response_reserve=0,
    )
    builder = ContextBuilder(token_budget=budget)
    builder.load_snapshot(
        {
            "history": [
                {"role": "user", "content": "Find current news."},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "name": "web_search",
                            "arguments": {"query": "news a"},
                        },
                        {
                            "id": "call_b",
                            "name": "web_search",
                            "arguments": {"query": "news b"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "content": "a" * 40,
                    "name": "web_search",
                    "tool_call_id": "call_a",
                },
                {
                    "role": "tool",
                    "content": "b" * 40,
                    "name": "web_search",
                    "tool_call_id": "call_b",
                },
            ],
        }
    )

    messages = asyncio.run(
        builder.build(
            user_message="Summarize it.", state=AgentState(user_message="Summarize it.")
        )
    )

    roles = [message.role for message in messages]
    assert roles[0] == "system"
    assert roles[1:5] == ["user", "assistant", "tool", "tool"]
    assert roles[-1] == "user"
    assert messages[-1].content.endswith("Summarize it.")
    assert [message.tool_call_id for message in messages if message.role == "tool"] == [
        "call_a",
        "call_b",
    ]


def test_context_builder_deduplicates_appended_retry_user_after_restored_snapshot() -> (
    None
):
    builder = ContextBuilder()
    builder.load_snapshot(
        {
            "history": [
                {"role": "user", "content": "What happened today?"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "name": "web_search",
                            "arguments": {"query": "today news"},
                        },
                    ],
                },
                {
                    "role": "tool",
                    "content": "Searched web",
                    "name": "web_search",
                    "tool_call_id": "call_a",
                },
                {"role": "user", "content": "What happened today?"},
            ],
        }
    )

    builder.append_user("What happened today?")

    history = builder.export_snapshot()["history"]
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "user",
    ]
    assert [message["content"] for message in history if message["role"] == "user"] == [
        "What happened today?",
        "What happened today?",
    ]


def test_context_builder_summary_cache_is_scoped_by_model() -> None:
    first_llm = _SummaryLLM(model="model-a", response="summary from a")
    second_llm = _SummaryLLM(model="model-b", response="summary from b")
    early = [
        LLMMessage(role="user", content="same request"),
        LLMMessage(role="assistant", content="same answer"),
    ]

    first = asyncio.run(ContextBuilder(llm=first_llm)._summarize_early(early))
    second = asyncio.run(ContextBuilder(llm=second_llm)._summarize_early(early))

    assert first == first_llm.response
    assert second == second_llm.response
    assert first_llm.simple_calls == 1
    assert second_llm.simple_calls == 1


def test_context_builder_can_load_snapshot_partially_then_hydrate_older_history() -> (
    None
):
    builder = ContextBuilder()
    snapshot = {
        "history": [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
            {"role": "assistant", "content": "a2"},
        ],
        "persistent_notes": [
            {"kind": "summary", "title": "note", "content": "keep me"}
        ],
        "compaction_count": 2,
    }

    pending_history = builder.load_snapshot_partial(snapshot, recent_history_count=2)

    partial = builder.export_snapshot()
    assert [message["content"] for message in partial["history"]] == ["u2", "a2"]
    assert partial["persistent_notes"][0]["content"] == "keep me"
    assert partial["compaction_count"] == 2
    assert [item["content"] for item in pending_history] == ["u1", "a1"]

    builder.prepend_history_messages(
        ContextBuilder.deserialize_snapshot_history(pending_history)
    )

    hydrated = builder.export_snapshot()
    assert [message["content"] for message in hydrated["history"]] == [
        "u1",
        "a1",
        "u2",
        "a2",
    ]


def test_context_builder_freezes_sent_history_and_appends_runtime_checkpoint() -> None:
    builder = ContextBuilder()
    state = AgentState(user_message="first")
    state.prompt_context["environment"] = {"cwd": "C:/repo"}
    asyncio.run(builder.start_turn("first", state))
    first_request = asyncio.run(builder.build(state))
    first_user = next(message for message in first_request if message.role == "user")
    frozen_snapshot = first_user.content
    assert builder._history_frozen_count == len(builder._history)  # type: ignore[attr-defined]

    state.user_message = "continue"
    state.prompt_context["environment"] = {"cwd": "C:/other"}
    second_request = asyncio.run(builder.build(state))
    users = [message for message in second_request if message.role == "user"]
    assert users[0].content == frozen_snapshot
    assert users[-1].runtime_context
    assert users[-1].content != frozen_snapshot
    assert sum(1 for message in builder._history if builder._is_durable_runtime_update(message)) == 1  # type: ignore[attr-defined]


def test_context_builder_empty_hydration_does_not_freeze_later_append() -> None:
    builder = ContextBuilder()
    snapshot = {
        "history": [{"role": "user", "content": "old"}],
        "history_frozen_count": 1,
    }
    pending = builder.load_snapshot_partial(snapshot, recent_history_count=0)
    assert [item["content"] for item in pending] == ["old"]
    builder.prepend_history_messages([])
    builder.append_user("new")
    assert builder._history_frozen_count == 0  # type: ignore[attr-defined]


def test_context_builder_snapshot_freeze_boundary_uses_original_rows_after_sanitization() -> None:
    builder = ContextBuilder()
    builder._history = [  # type: ignore[attr-defined]
        LLMMessage(role="user", content="same"),
        LLMMessage(role="user", content="same"),
        LLMMessage(role="assistant", content="answer"),
    ]
    builder._history_frozen_count = 3  # type: ignore[attr-defined]
    snapshot = builder.export_snapshot(max_messages=2)
    assert [message["content"] for message in snapshot["history"]] == ["same", "answer"]
    assert snapshot["history_frozen_count"] == 2


def test_snapshot_bounds_keep_tool_call_and_result_as_one_group() -> None:
    builder = ContextBuilder()
    call = ToolCallEvent(id="call-1", name="read_file", arguments={"file_path": "a.py"})
    builder.append_user("inspect")
    builder.append_assistant_tool_calls([call])
    builder.append_tool_result(
        "call-1", "read_file", ToolResult(content="result " * 100)
    )

    snapshot = builder.export_snapshot(max_messages=1, max_chars=2_000)

    assert [message["role"] for message in snapshot["history"]] == ["assistant", "tool"]
    assert snapshot["history"][0]["tool_calls"][0]["id"] == "call-1"
    assert snapshot["history"][1]["tool_call_id"] == "call-1"
    assert len(json.dumps(snapshot["history"], ensure_ascii=False)) <= 2_000

    restored = ContextBuilder()
    pending = restored.load_snapshot_partial(snapshot, recent_history_count=1)
    assert pending == []
    assert [message["role"] for message in restored.export_snapshot()["history"]] == [
        "assistant",
        "tool",
    ]


def test_failed_compaction_does_not_advance_compaction_count() -> None:
    class _FailingSummaryLLM(LLMAdapter):
        async def stream_chat(self, messages, tools=None):
            if False:
                yield StreamEvent(type=StreamEventType.DONE)

        async def simple_chat(self, messages, *, max_tokens=None):
            del max_tokens
            raise RuntimeError("summary failed")

    builder = ContextBuilder(
        agent_settings=AgentSettings(compaction_keep_recent_tokens=4),
        llm=_FailingSummaryLLM(),
    )
    for index in range(12):
        builder.append_user(f"user-{index} " + "x" * 80)
        builder.append_assistant(f"assistant-{index} " + "y" * 80)

    try:
        asyncio.run(builder.compact())
    except RuntimeError as exc:
        assert str(exc) == "summary failed"
    else:
        raise AssertionError("compaction should propagate summary failure")

    assert builder.export_snapshot()["compaction_count"] == 0


def test_snapshot_round_trips_media_error_and_opaque_provider_state() -> None:
    encrypted = "x" * 100_000
    builder = ContextBuilder()
    builder._history_store.append(  # type: ignore[attr-defined]
        LLMMessage(
            role="user",
            content="inspect",
            images=[{"media_type": "image/png", "data": "aGVsbG8="}],
            documents=[
                {
                    "media_type": "application/pdf",
                    "data": "JVBERi0=",
                    "file_name": "a.pdf",
                }
            ],
        )
    )
    builder._history_store.append(  # type: ignore[attr-defined]
        LLMMessage(
            role="assistant",
            provider_items=[
                {"type": "reasoning", "id": "r1", "encrypted_content": encrypted}
            ],
        )
    )
    builder._history_store.append(  # type: ignore[attr-defined]
        LLMMessage(role="tool", tool_call_id="c1", name="read_file", is_error=True)
    )

    restored = ContextBuilder()
    restored.load_snapshot(builder.export_snapshot(max_chars=500_000))
    history = restored._history  # type: ignore[attr-defined]

    assert history[0].images[0]["data"] == "aGVsbG8="
    assert history[0].documents[0]["file_name"] == "a.pdf"
    assert history[1].provider_items[0]["encrypted_content"] == encrypted
    assert history[2].is_error is True


def test_generated_image_context_round_trips_through_scoped_attachment_reference(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    attachment_root = tmp_path / "attachments"
    encoded = "aGVsbG8="

    builder = ContextBuilder(
        conversation_id="conversation-image",
        workspace_root=workspace,
    )
    builder._attachment_store = AttachmentStore(attachment_root)
    builder.append_assistant("好的，我来生成这张图片。")

    assert builder.append_generated_image_context(
        artifact_id="artifact-image-context",
        image_data=encoded,
        media_type="image/png",
        size_bytes=5,
    ) is True

    snapshot = builder.export_snapshot()
    persisted = snapshot["history"][-1]
    assert persisted["role"] == "user"
    assert persisted["attachment_refs"][0]["artifact_id"] == "artifact-image-context"
    assert persisted["images"] == []
    assert encoded not in json.dumps(persisted)

    restored = ContextBuilder(
        conversation_id="conversation-image",
        workspace_root=workspace,
    )
    restored._attachment_store = AttachmentStore(attachment_root)
    restored.load_snapshot(snapshot)
    restored._rehydrate_attachment_refs(  # type: ignore[attr-defined]
        SimpleNamespace(
            conversation_id="conversation-image",
            workspace_context=SimpleNamespace(root_path=workspace),
        ),
        workspace,
    )

    history = restored._history  # type: ignore[attr-defined]
    assert history[-1].images == [{"media_type": "image/png", "data": encoded}]


def test_user_authored_system_reminder_is_not_treated_as_runtime_provenance() -> None:
    builder = ContextBuilder()
    authored = (
        "<system-reminder>\n<environment_context>user text</environment_context>\n"
        "</system-reminder>\n\nkeep this"
    )
    builder.append_user(authored)
    builder._compact_old_user_runtime_context_for_cache(keep_recent_user_turns=0)  # type: ignore[attr-defined]

    history = builder.export_snapshot()["history"]
    assert history[0]["content"] == authored
    assert history[0]["runtime_context"] == ""


def test_context_ledger_counts_prompt_sections_as_items() -> None:
    builder = ContextBuilder()
    builder._last_prompt_section_summary = {  # type: ignore[attr-defined]
        "sections": [{"name": "stable_system", "chars": 40}]
    }
    ledger = builder.context_ledger()
    system_entry = next(
        entry for entry in ledger["entries"] if entry["category"] == "system_runtime"
    )
    assert system_entry["item_count"] == 1


def test_context_builder_full_compact_uses_the_same_token_tail_contract() -> None:
    llm = _SummaryLLM(model="model-a", response="compact summary")
    ctx = ContextBuilder(
        token_budget=TokenBudget(total=120),
        agent_settings=AgentSettings(compaction_keep_recent_tokens=6),
        llm=llm,
    )
    for idx in range(18):
        ctx.append_user(f"user message {idx} " + ("x" * 200))
        ctx.append_assistant(f"assistant message {idx} " + ("y" * 200))

    before = ctx.history_length
    before_history_tokens = ctx._history_tokens_total  # type: ignore[attr-defined]
    summary = asyncio.run(ctx.full_compact())

    assert summary == (
        f"{llm.response}\n\n---\n\n"
        f"**Turn Context (split turn):**\n\n{llm.response}"
    )
    assert llm.simple_calls == 2
    assert ctx.history_length < before
    assert ctx._history_tokens_total < before_history_tokens  # type: ignore[attr-defined]
