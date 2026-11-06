from __future__ import annotations

from types import SimpleNamespace

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
