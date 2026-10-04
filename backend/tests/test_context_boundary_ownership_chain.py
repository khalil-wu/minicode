from __future__ import annotations

import gc
import weakref
from dataclasses import asdict

import pytest

from backend.agent.turn_context_runtime import _coerce_extension_context_messages
from backend.llm.base import LLMMessage
from backend.sdk import SDKSession


@pytest.mark.asyncio
async def test_sdk_fork_keeps_history_without_retaining_parent_builder(tmp_path):
    parent = SDKSession(session_id="parent-history-owner", workspace_root=tmp_path)
    parent.context_builder.append_user("Original context")
    reference = weakref.ref(parent.context_builder)
    child = parent.fork(session_id="child-history-owner")
    await parent.aclose()
    del parent
    gc.collect()
    try:
        assert reference() is None
        assert child.context_builder.export_snapshot()["history"][0]["content"] == "Original context"
        child.context_builder.append_user("Child context")
        assert [message["content"] for message in child.context_builder.export_snapshot()["history"]] == [
            "Original context", "Child context",
        ]
    finally:
        await child.aclose()


@pytest.mark.parametrize("user_input", [True, False])
@pytest.mark.parametrize("camel_case", [True, False])
def test_extension_mapping_roundtrip_keeps_host_context_and_user_wire_parts(user_input, camel_case):
    reminder = "Captured host environment"
    original = LLMMessage(
        role="user", runtime_context=reminder, is_user_input=user_input,
        content=f"<system-reminder>\n{reminder}\n</system-reminder>\n\nUser question",
    )
    serialized = asdict(original)
    if camel_case:
        serialized["isUserInput"] = serialized.pop("is_user_input")
    restored = _coerce_extension_context_messages([serialized])[0]
    assert restored.is_user_input is user_input
    assert restored.to_openai_message() == original.to_openai_message()
    assert restored.user_text_parts() == original.user_text_parts()
