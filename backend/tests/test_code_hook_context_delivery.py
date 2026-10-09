from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.agent.code_execution import CodeExecutionRuntime
from backend.agent.context import ContextBuilder
from backend.agent.state import AgentState
from backend.agent.tool_execution import store_result
from backend.artifact.store import ArtifactStore
from backend.config import TokenBudget
from backend.llm.base import ToolCallEvent
from backend.permissions.context import PermissionContext, ToolExecutionContext
from backend.tools.code_execution import _present_result


@pytest.mark.parametrize("output", ["small output", "x" * 1000])
@pytest.mark.asyncio
async def test_nested_hook_context_reaches_model_once_independent_of_selected_output_budget(tmp_path, output):
    runtime = object.__new__(CodeExecutionRuntime)
    runtime.cells = {"cell": SimpleNamespace(
        id="cell", status="completed", error="", discarded_calls=0,
        output=[{"kind": "text", "text": output}], hook_context=["NESTED_HOOK_FIXTURE"],
        changed=asyncio.Event(),
    )}
    runtime.store = SimpleNamespace(receipts={})
    runtime.state = AgentState(user_message="fixture")
    context = ToolExecutionContext(
        permission=PermissionContext(), conversation_id="owner", workspace_root=tmp_path,
        artifact_store=ArtifactStore(storage_dir=tmp_path / "artifacts"),
    )
    builder = ContextBuilder(token_budget=TokenBudget(total=200000, response_reserve=1000))
    try:
        result = await _present_result(await runtime.wait("cell", yield_time_ms=0), context, 256, "tool_exec")
        assert "hook_context" not in json.loads(result.content)
        assert len(result.content) <= 256
        assert result.runtime_metadata["hook_model_context"] == ["NESTED_HOOK_FIXTURE"]
        call = ToolCallEvent(id="cell-first", name="tool_exec", arguments={})
        builder.append_user("fixture")
        builder.append_assistant_tool_calls([call])
        event = store_result(call, result, builder, runtime.state, tool_ctx=context)
        assert builder._history[-1].content.count("NESTED_HOOK_FIXTURE") == 1
        assert "NESTED_HOOK_FIXTURE" not in event.data["summary"]
        assert json.loads(event.data["summary"])["cell_id"] == "cell"
        consumed = await runtime.wait("cell", yield_time_ms=0)
        assert consumed.runtime_metadata["hook_model_context"] == []
    finally:
        context.artifact_store.shutdown()


def test_runtime_user_settings_are_outside_the_code_workspace(tmp_path):
    from backend import config, config_helpers

    assert config.SETTINGS_FILE == config_helpers.SETTINGS_FILE
    assert not config_helpers.SETTINGS_FILE.is_relative_to(tmp_path)
