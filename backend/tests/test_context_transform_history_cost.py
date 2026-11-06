from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.agent.context import ContextBuilder
from backend.agent.run_context import RunContext
from backend.agent.state import AgentState
from backend.agent.turn_context_runtime import prepare_turn_context
from backend.extensions.loader import ExtensionLoader
from backend.llm.base import LLMMessage


@pytest.mark.asyncio
@pytest.mark.parametrize("transform", [False, True])
async def test_only_registered_context_transforms_copy_and_decode_history(tmp_path, monkeypatch, transform):
    from backend.agent import turn_context_runtime

    def factory(api):
        if transform:
            def change(event, _context):
                event["messages"][0].content = "extension view"
            api.on("context", change)

    loaded = await ExtensionLoader(cwd=tmp_path).load_factory(factory)
    messages = [LLMMessage("user", "canonical history")]
    context = ContextBuilder(workspace_root=tmp_path)
    monkeypatch.setattr(context, "build", AsyncMock(return_value=messages))
    decode = turn_context_runtime._coerce_extension_context_messages
    decoded = []
    def observe(values):
        decoded.append(values)
        assert transform, "No context handler should not decode canonical history"
        return decode(values)
    monkeypatch.setattr(turn_context_runtime, "_coerce_extension_context_messages", observe)
    try:
        prepared = await prepare_turn_context(context=context,
            state=AgentState(user_message="next", workspace_root=tmp_path), llm=None,
            tool_schemas=[], request_metadata={}, metadata={}, external_metadata=None,
            tool_context=SimpleNamespace(run_context=RunContext(lifecycle_runtime=loaded.runner), metadata={}),
            turn_kernel=SimpleNamespace(emit_runtime_span=AsyncMock()), run_id="context-cost")
        assert bool(decoded) is transform
        assert messages[0].content == "canonical history"
        if transform:
            assert prepared.messages[0].content == "extension view"
            assert prepared.messages[0] is not messages[0]
        else:
            assert prepared.messages is messages
    finally:
        await loaded.runner.shutdown("test complete")
