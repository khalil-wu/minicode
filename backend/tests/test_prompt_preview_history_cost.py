from __future__ import annotations

from types import SimpleNamespace
import asyncio
import threading

import pytest

from backend.agent.context import ContextBuilder, clone_context_builder
from backend.agent.query_engine import QueryEngine
from backend.agent.state import AgentState


@pytest.mark.asyncio
async def test_system_prompt_preview_does_not_clone_or_mutate_history(tmp_path, monkeypatch):
    builder = ContextBuilder(workspace_root=tmp_path)
    for index in range(100):
        builder.append_user(f"retained input {index}")
    state = AgentState(user_message="new input", workspace_root=tmp_path)
    expected = clone_context_builder(builder).base_system_prompt(state)
    before = builder.export_snapshot()
    def forbidden():
        raise AssertionError("System prompt preview cloned the entire conversation")
    monkeypatch.setattr(builder._history_store, "clone", forbidden)
    turn = SimpleNamespace(metadata={}, context_builder=builder, state=state)
    await QueryEngine._publish_system_prompt(turn)
    assert turn.metadata["system_prompt"] == expected.strip()
    assert builder.export_snapshot() == before


def test_branch_context_clone_still_owns_its_history(tmp_path):
    builder = ContextBuilder(workspace_root=tmp_path)
    builder.append_user("original")
    clone = clone_context_builder(builder)
    clone.append_user("branch only")
    assert builder.history_length == 1
    assert clone.history_length == 2
    assert clone._history[0] is not builder._history[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_prompt_preview_keeps_event_loop_responsive_and_owns_worker_until_settled(tmp_path, monkeypatch, cancel):
    builder = ContextBuilder(workspace_root=tmp_path)
    builder.append_user("retained input")
    state = AgentState(user_message="new input", workspace_root=tmp_path)
    turn = SimpleNamespace(metadata={}, context_builder=builder, state=state)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    event_loop_thread = threading.get_ident()

    def held_preview(preview, incoming):
        assert threading.get_ident() != event_loop_thread
        assert preview is not builder and preview._history_store is builder._history_store
        assert incoming is state
        entered.set()
        try:
            assert release.wait(5)
            return "captured prompt"
        finally:
            finished.set()

    monkeypatch.setattr(ContextBuilder, "base_system_prompt", held_preview)
    work = asyncio.create_task(QueryEngine._publish_system_prompt(turn))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        # A transport callback can still run while instruction IO is waiting.
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), .5)
        if cancel:
            work.cancel()
        await asyncio.sleep(0)
        assert not work.done() and not finished.is_set()
        assert "system_prompt" not in turn.metadata
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await work
            assert "system_prompt" not in turn.metadata
        else:
            await work
            assert turn.metadata["system_prompt"] == "captured prompt"
        assert finished.is_set()
        assert [message.content for message in builder._history] == ["retained input"]
    finally:
        release.set()
        await asyncio.gather(work, return_exceptions=True)


@pytest.mark.asyncio
async def test_prompt_preview_preserves_instruction_read_failure(monkeypatch):
    def unavailable(*args):
        raise OSError("instruction read failed")

    monkeypatch.setattr(ContextBuilder, "base_system_prompt", unavailable)
    turn = SimpleNamespace(metadata={}, context_builder=ContextBuilder(), state=AgentState(user_message="input"))
    with pytest.raises(OSError, match="instruction read failed"):
        await QueryEngine._publish_system_prompt(turn)
    assert "system_prompt" not in turn.metadata
