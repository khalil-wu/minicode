from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.config import AgentSettings, TokenBudget
from backend.conversations.repository import (
    ConversationRepository,
    ConversationWriteConflict,
)
from backend.llm.base import (
    LLMAdapter,
    LLMMessage,
    StreamEvent,
    StreamEventType,
    UsageInfo,
)
from backend.ws.compaction_coordinator import compact_conversation
from backend.ws.conversation_runtime import ConversationRuntime


def _valid_summary(label: str) -> str:
    return (
        f"## Goal\n{label}\n\n"
        "## Constraints & Preferences\n- Preserve durable context\n\n"
        "## Progress\n- Compaction completed\n\n"
        "## Key Decisions\n- Keep the authoritative revision\n\n"
        "## Next Steps\n1. Continue\n\n"
        f"## Critical Context\n- {label}"
    )


class _GateSummaryLLM(LLMAdapter):
    def __init__(self, label: str = "summary", *, gated: bool = False) -> None:
        self.response = _valid_summary(label)
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.gated = gated
        self.calls: list[list[LLMMessage]] = []

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
        self.calls.append(messages)
        self.started.set()
        if self.gated:
            await self.release.wait()
        return self.response


class _GateHydrationRuntime(ConversationRuntime):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.hydration_started = asyncio.Event()
        self.release_hydration = asyncio.Event()

    async def _hydrate_snapshot(self, **kwargs: Any) -> None:
        self.hydration_started.set()
        await self.release_hydration.wait()
        await super()._hydrate_snapshot(**kwargs)


class _Session:
    def __init__(
        self,
        repository: ConversationRepository,
        builder: ContextBuilder,
        *,
        runtime_factory: Callable[..., ConversationRuntime] = ConversationRuntime,
    ) -> None:
        self.conversation_repo = repository
        self.context_builder = builder
        self._locks: dict[str, asyncio.Lock] = {}
        self.conversation_runtime = runtime_factory(
            conversation_repo=repository,
            context_builder=builder,
            build_summary_from_transcript=lambda *_args, **_kwargs: "",
            projection_lock_for=self._conversation_projection_lock,
        )

    def _conversation_projection_lock(self, conversation_id: str) -> asyncio.Lock:
        return self._locks.setdefault(conversation_id, asyncio.Lock())


def _long_history(count: int = 32) -> list[dict[str, Any]]:
    return [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"history-{index} " + (chr(97 + index % 20) * 160),
        }
        for index in range(count)
    ]


def test_compaction_waits_for_complete_hydration_before_summarizing(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[_GateSummaryLLM, dict[str, Any]]:
        repository = ConversationRepository(tmp_path / "conversations")
        llm = _GateSummaryLLM("hydrated")
        builder = ContextBuilder(
            llm=llm,
            agent_settings=AgentSettings(compaction_keep_recent_tokens=80),
        )
        snapshot = {"history": _long_history(), "history_frozen_count": 32}
        record = repository.create_conversation(
            conversation_id="conv_hydration_compact",
            context_snapshot=snapshot,
        )
        session = _Session(repository, builder, runtime_factory=_GateHydrationRuntime)
        runtime = session.conversation_runtime
        assert isinstance(runtime, _GateHydrationRuntime)
        runtime.active_conversation_id = record.id
        assert runtime.load_active_conversation_snapshot(record.id, snapshot) is True
        await runtime.hydration_started.wait()

        compact_task = asyncio.create_task(
            compact_conversation(
                session,
                conversation_id=record.id,
                context_builder=builder,
            )
        )
        await asyncio.sleep(0)
        assert llm.calls == []
        runtime.release_hydration.set()
        committed = await compact_task
        return llm, committed.after_snapshot

    llm, after_snapshot = asyncio.run(scenario())
    compact_input = "\n".join(
        str(message.content or "") for call in llm.calls for message in call
    )
    assert "history-0" in compact_input
    assert after_snapshot["compaction_count"] == 1


def test_compaction_does_not_publish_into_builder_after_conversation_switch(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[ContextBuilder, dict[str, Any]]:
        repository = ConversationRepository(tmp_path / "conversations")
        builder = ContextBuilder(
            llm=_GateSummaryLLM("switched"),
            agent_settings=AgentSettings(compaction_keep_recent_tokens=80),
        )
        first_snapshot = {"history": _long_history(), "history_frozen_count": 32}
        first = repository.create_conversation(
            conversation_id="conv_compact_first",
            context_snapshot=first_snapshot,
        )
        second_snapshot = {
            "history": [{"role": "user", "content": "second conversation"}],
            "history_frozen_count": 1,
        }
        second = repository.create_conversation(
            conversation_id="conv_compact_second",
            context_snapshot=second_snapshot,
        )
        session = _Session(repository, builder, runtime_factory=_GateHydrationRuntime)
        runtime = session.conversation_runtime
        assert isinstance(runtime, _GateHydrationRuntime)
        runtime.active_conversation_id = first.id
        assert (
            runtime.load_active_conversation_snapshot(first.id, first_snapshot) is True
        )
        await runtime.hydration_started.wait()

        compact_task = asyncio.create_task(
            compact_conversation(
                session,
                conversation_id=first.id,
                context_builder=builder,
            )
        )
        await asyncio.sleep(0)
        runtime.active_conversation_id = second.id
        runtime.load_active_conversation_snapshot(second.id, second_snapshot)
        committed = await compact_task
        return builder, committed.after_snapshot

    builder, compacted_first = asyncio.run(scenario())
    assert [item["content"] for item in builder.export_snapshot()["history"]] == [
        "second conversation"
    ]
    assert compacted_first["compaction_count"] == 1


def test_hydration_revision_fence_reloads_latest_snapshot_and_clears_token_floor(
    tmp_path: Path,
) -> None:
    async def scenario() -> ContextBuilder:
        repository = ConversationRepository(tmp_path / "conversations")
        builder = ContextBuilder(token_budget=TokenBudget(total=100_000))
        old_snapshot = {
            "history": _long_history(),
            "history_frozen_count": 32,
            "consecutive_autocompact_failures": 3,
        }
        record = repository.create_conversation(
            conversation_id="conv_hydration_revision",
            context_snapshot=old_snapshot,
        )
        session = _Session(repository, builder, runtime_factory=_GateHydrationRuntime)
        runtime = session.conversation_runtime
        assert isinstance(runtime, _GateHydrationRuntime)
        runtime.active_conversation_id = record.id
        assert (
            runtime.load_active_conversation_snapshot(record.id, old_snapshot) is True
        )
        builder.record_actual_usage(UsageInfo(input_tokens=90_000, output_tokens=1))
        await runtime.hydration_started.wait()
        repository.save_context_snapshot(
            record.id,
            {
                "history": [{"role": "user", "content": "new revision"}],
                "history_frozen_count": 1,
                "consecutive_autocompact_failures": 1,
            },
        )
        runtime.release_hydration.set()
        await runtime.wait_for_hydration(record.id)
        return builder

    builder = asyncio.run(scenario())
    assert [item["content"] for item in builder.export_snapshot()["history"]] == [
        "new revision"
    ]
    assert builder.consecutive_autocompact_failures == 1
    assert builder.token_usage < 90_000


def test_compaction_cas_conflict_restores_authoritative_revision(
    tmp_path: Path,
) -> None:
    async def scenario() -> ContextBuilder:
        repository = ConversationRepository(tmp_path / "conversations")
        llm = _GateSummaryLLM("conflict", gated=True)
        builder = ContextBuilder(
            llm=llm,
            token_budget=TokenBudget(total=100_000),
            agent_settings=AgentSettings(compaction_keep_recent_tokens=80),
        )
        old_snapshot = {
            "history": _long_history(),
            "history_frozen_count": 32,
            "consecutive_autocompact_failures": 2,
        }
        record = repository.create_conversation(
            conversation_id="conv_compact_conflict",
            context_snapshot=old_snapshot,
        )
        builder.load_snapshot(old_snapshot)
        builder.record_actual_usage(UsageInfo(input_tokens=90_000, output_tokens=1))
        session = _Session(repository, builder)
        session.conversation_runtime.active_conversation_id = record.id
        compact_task = asyncio.create_task(
            compact_conversation(
                session,
                conversation_id=record.id,
                context_builder=builder,
            )
        )
        await llm.started.wait()
        repository.save_context_snapshot(
            record.id,
            {
                "history": [{"role": "user", "content": "winning revision"}],
                "history_frozen_count": 1,
                "consecutive_autocompact_failures": 1,
            },
        )
        llm.release.set()
        with pytest.raises(ConversationWriteConflict):
            await compact_task
        return builder

    builder = asyncio.run(scenario())
    assert [item["content"] for item in builder.export_snapshot()["history"]] == [
        "winning revision"
    ]
    assert builder.consecutive_autocompact_failures == 1
    assert builder.token_usage < 90_000


def test_invoked_skill_survives_compaction_resume_and_recompaction() -> None:
    async def scenario() -> dict[str, Any]:
        skill = {
            "name": "review-workflow",
            "path": "C:/skills/review-workflow/SKILL.md",
            "content": "Read the repository first, then preserve exact ownership.",
        }
        builder = ContextBuilder(
            llm=_GateSummaryLLM("first compact"),
            agent_settings=AgentSettings(compaction_keep_recent_tokens=40),
        )
        state = AgentState(user_message="start")
        state.prompt_context["skill_injections"] = [skill]
        await builder.start_turn("start", state)
        builder.append_assistant("started")
        for index in range(12):
            builder.append_user(f"work-{index} " + "x" * 120)
            builder.append_assistant(f"done-{index} " + "y" * 120)
        await builder.compact()

        resumed = ContextBuilder(
            llm=_GateSummaryLLM("second compact"),
            agent_settings=AgentSettings(compaction_keep_recent_tokens=40),
        )
        resumed.load_snapshot(builder.export_snapshot())
        for index in range(8):
            resumed.append_user(f"resume-{index} " + "a" * 120)
            resumed.append_assistant(f"answer-{index} " + "b" * 120)
        await resumed.compact()
        return resumed.export_snapshot()

    snapshot = asyncio.run(scenario())
    skill_messages = [
        item
        for item in snapshot["history"]
        if str(item.get("content") or "").startswith("<skill>\n")
    ]
    assert len(skill_messages) == 1
    assert snapshot["compaction_count"] == 2
    assert snapshot["invoked_skills"][0]["name"] == "review-workflow"


def test_partial_hydration_restores_existing_skill_once_with_identical_cache() -> None:
    skill = {
        "name": "audit",
        "path": "C:/skills/audit/SKILL.md",
        "content": "Use the source of truth.",
    }
    snapshot = {
        "history": [
            {"role": "user", "content": ContextBuilder._render_skill_payload(skill)},
            {"role": "assistant", "content": "skill loaded"},
            *_long_history(28),
        ],
        "history_frozen_count": 30,
        "invoked_skills": [skill],
        "consecutive_autocompact_failures": 2,
    }
    fully_loaded = ContextBuilder()
    fully_loaded.load_snapshot(snapshot)
    partially_loaded = ContextBuilder()
    pending = partially_loaded.load_snapshot_partial(snapshot, recent_history_count=6)
    assert not any(
        str(item.get("content") or "").startswith("<skill>\n")
        for item in partially_loaded.export_snapshot()["history"]
    )
    partially_loaded.prepend_history_messages(
        ContextBuilder.deserialize_snapshot_history(pending)
    )

    partial_snapshot = partially_loaded.export_snapshot()
    assert partial_snapshot["history"] == fully_loaded.export_snapshot()["history"]
    assert (
        sum(
            str(item.get("content") or "").startswith("<skill>\n")
            for item in partial_snapshot["history"]
        )
        == 1
    )
    assert partially_loaded._history_tokens_total == fully_loaded._history_tokens_total
    assert partially_loaded.consecutive_autocompact_failures == 2


def test_successful_compaction_resets_failure_counter_and_bounds_skill_snapshot() -> (
    None
):
    builder = ContextBuilder(
        llm=_GateSummaryLLM("reset breaker"),
        agent_settings=AgentSettings(compaction_keep_recent_tokens=40),
    )
    for _ in range(3):
        builder.record_autocompact_failure()
    state = AgentState(user_message="audit")
    state.prompt_context["skill_injections"] = [
        {
            "name": "large-audit",
            "path": "C:/skills/large-audit/SKILL.md",
            "content": "x" * 30_000,
        }
    ]
    asyncio.run(builder.start_turn("audit", state))
    for index in range(12):
        builder.append_user(f"user-{index} " + "x" * 120)
        builder.append_assistant(f"assistant-{index} " + "y" * 120)

    asyncio.run(builder.compact())

    persisted = builder.export_snapshot()
    assert persisted["consecutive_autocompact_failures"] == 0
    assert len(persisted["invoked_skills"][0]["content"]) <= 20_000
    assert (
        "skill content truncated for compaction"
        in persisted["invoked_skills"][0]["content"]
    )
