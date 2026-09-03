from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import backend.sdk as sdk_module
from backend.agent.context import ContextBuilder
from backend.agent.history_store import estimate_message_tokens
from backend.agent.message import AgentEvent
from backend.agent.state import AgentState
from backend.config import TokenBudget
from backend.sdk import SDKSession
from backend.agent.conversation_query_guard import conversation_query_guards
from backend.ws.agent_runner import (
    _clear_session_llm_cache,
    _get_or_create_session_llm,
    _lease_session_llm_for_task,
)


def test_sdk_session_rejects_overlapping_queries(monkeypatch) -> None:
    release = asyncio.Event()

    async def fake_query(_message: str, **_kwargs):
        yield AgentEvent.progress("started")
        await release.wait()
        yield AgentEvent.done()

    monkeypatch.setattr(sdk_module, "query", fake_query)

    async def scenario() -> None:
        session = SDKSession()
        first = session.query("first")
        await anext(first)
        second = session.query("second")
        with pytest.raises(RuntimeError, match="already processing"):
            await anext(second)
        await second.aclose()
        release.set()
        await first.aclose()
        assert session._active_query is False

    asyncio.run(scenario())


def test_sdk_sessions_share_process_conversation_admission(monkeypatch) -> None:
    release = asyncio.Event()

    async def fake_unclaimed(_message: str, **_kwargs):
        yield AgentEvent.progress("started")
        await release.wait()
        yield AgentEvent.done()

    monkeypatch.setattr(sdk_module, "_query_unclaimed", fake_unclaimed)

    async def scenario() -> None:
        first_session = SDKSession(metadata={"source": "sdk", "conversation_id": "conv-shared"})
        second_session = SDKSession(metadata={"source": "sdk", "conversation_id": "conv-shared"})
        first = first_session.query("first")
        await anext(first)

        second_events = [event async for event in second_session.query("second")]
        assert [event.type for event in second_events] == ["error", "done"]
        assert second_events[0].data["error_type"] == "conversation_busy"
        assert second_events[1].data["reason"] == "conversation_busy"

        release.set()
        await first.aclose()
        assert conversation_query_guards().active_claim("conv-shared") is None

    asyncio.run(scenario())


def test_sdk_query_rejects_ws_owned_conversation_without_starting_runtime(monkeypatch) -> None:
    started = False

    async def fake_unclaimed(_message: str, **_kwargs):
        nonlocal started
        started = True
        yield AgentEvent.done()

    monkeypatch.setattr(sdk_module, "_query_unclaimed", fake_unclaimed)
    guards = conversation_query_guards()
    claim = guards.try_start("conv-ws", owner_id="ws:active")
    assert claim is not None
    try:
        events = asyncio.run(_collect_sdk_events(
            sdk_module.query("hello", metadata={"conversation_id": "conv-ws"})
        ))
    finally:
        guards.end(claim)

    assert started is False
    assert events[-1].data["reason"] == "conversation_busy"


async def _collect_sdk_events(stream):
    return [event async for event in stream]


def test_context_builder_binds_replacement_llm_for_side_calls() -> None:
    builder = ContextBuilder()
    llm = object()
    budget = TokenBudget(total=1234)

    builder.bind_llm(llm)
    builder.bind_budget(budget)

    assert builder._llm is llm
    assert builder._budget is budget


def test_git_snapshot_is_session_owned_and_round_trips(monkeypatch, tmp_path) -> None:
    calls: list[str] = []

    async def fake_git_status(root) -> str:
        calls.append(str(root))
        return "session-start git status"

    monkeypatch.setattr(
        "backend.agent.context.build_git_status_context_async",
        fake_git_status,
    )
    state = AgentState(user_message="first")
    state.workspace_context = SimpleNamespace(
        root_path=tmp_path,
        get_project_summary=lambda: "",
    )
    builder = ContextBuilder()

    asyncio.run(builder.start_turn("first", state))
    asyncio.run(builder.start_turn("second", state))
    assert len(calls) == 1

    restored = ContextBuilder()
    restored.load_snapshot(builder.export_snapshot())
    asyncio.run(restored.start_turn("third", state))
    assert len(calls) == 1


def test_post_compaction_restore_bounds_oversized_single_line(tmp_path) -> None:
    source = tmp_path / "large.txt"
    source.write_bytes(b"x" * (256 * 1024 + 1))
    builder = ContextBuilder()
    state = AgentState(user_message="continue")
    state.workspace_context = SimpleNamespace(root_path=tmp_path)
    state.record_tool_call("read_file", {"file_path": "large.txt"}, "ok")

    builder._restore_recent_files_after_compaction(state)

    restored = next(
        note["content"]
        for note in builder.export_snapshot()["persistent_notes"]
        if note.get("kind") == "post_compaction_restore"
    )
    assert "1: " in restored
    assert "file view truncated at 5,000 tokens" in restored
    assert estimate_message_tokens(restored) <= 5_000


def test_replacing_session_llm_closes_previous_adapter(monkeypatch) -> None:
    closed = asyncio.Event()

    class OldAdapter:
        async def aclose(self) -> None:
            closed.set()

    replacement = object()
    monkeypatch.setattr(
        "backend.llm.model_registry.create_session_llm",
        lambda _config, model_override=None, **_kwargs: replacement,
    )
    session = SimpleNamespace(
        _llm_adapter_cache={("old",): OldAdapter()},
        _llm_close_tasks=set(),
    )
    config = SimpleNamespace(
        llm=SimpleNamespace(),
        agent=SimpleNamespace(fallback_providers=()),
    )

    async def scenario() -> None:
        selected = _get_or_create_session_llm(
            session,
            config=config,
            provider="anthropic",
            model="claude-test",
        )
        assert selected is replacement
        await asyncio.gather(*tuple(session._llm_close_tasks))

    asyncio.run(scenario())
    assert closed.is_set()
    assert list(session._llm_adapter_cache.values()) == [replacement]


def test_detached_session_job_keeps_retired_llm_alive_until_its_stream_finishes() -> None:
    closed = asyncio.Event()
    release = asyncio.Event()

    class Adapter:
        async def aclose(self) -> None:
            closed.set()

    adapter = Adapter()
    session = SimpleNamespace(
        _llm_adapter_cache={("active",): adapter},
        _llm_adapter_leases={},
        _retired_llm_adapters={},
        _llm_close_tasks=set(),
    )

    async def detached_job() -> None:
        await release.wait()

    async def scenario() -> None:
        task = asyncio.create_task(detached_job())
        _lease_session_llm_for_task(session, adapter, task)

        _clear_session_llm_cache(session)
        await asyncio.sleep(0)
        assert not closed.is_set()
        assert session._retired_llm_adapters == {id(adapter): adapter}

        release.set()
        await task
        await asyncio.sleep(0)
        if session._llm_close_tasks:
            await asyncio.gather(*tuple(session._llm_close_tasks))

    asyncio.run(scenario())
    assert closed.is_set()
    assert session._retired_llm_adapters == {}
