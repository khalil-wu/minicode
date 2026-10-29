"""Native agent inputs must reach text protocols without changing durable provenance."""
from copy import deepcopy

import pytest

from backend.agent.context import ContextBuilder
from backend.llm.anthropic_adapter import AnthropicAdapter
from backend.llm.openai_adapter import _openai_chat_messages
from backend.llm.responses_input import agent_message_response_item


@pytest.mark.parametrize("message_type", ["NEW_TASK", "MESSAGE", "FINAL_ANSWER"])
def test_native_agent_input_reaches_chat_and_anthropic_wire_after_cold_restore(message_type):
    native = agent_message_response_item(author="/root/worker", recipient="/root", content="Actual child result",
        trigger_turn=message_type == "NEW_TASK", message_type=message_type)
    builder = ContextBuilder()
    builder.append_agent_input_messages([{"content": "", "provider_items": [native]}])
    restored = ContextBuilder()
    restored.load_snapshot(builder.export_snapshot())
    original = deepcopy(restored.export_snapshot())
    message, = restored._history
    expected = native["content"][0]["text"]
    assert _openai_chat_messages([message]) == [{"role": "user", "content": expected}]
    system, wire = AnthropicAdapter._convert_messages([message])
    assert system == ""
    assert len(wire) == 1 and wire[0]["role"] == "user"
    content = wire[0]["content"]
    assert (content if isinstance(content, str) else "".join(part["text"] for part in content)) == expected
    assert restored.export_snapshot() == original
    assert message.content == "" and message.input_origin == "agent" and not message.is_user_input


@pytest.mark.parametrize("convert", [_openai_chat_messages, AnthropicAdapter._convert_messages])
def test_text_protocol_rejects_opaque_agent_input_instead_of_silently_dropping_it(convert):
    native = agent_message_response_item(author="/root", recipient="/root/worker", encrypted_content="opaque-source-data",
        trigger_turn=True)
    builder = ContextBuilder()
    builder.append_agent_input_messages([{"content": "", "provider_items": [native]}])
    original = deepcopy(builder.export_snapshot())
    with pytest.raises(ValueError, match="Encrypted agent messages require the Responses API"):
        convert(builder._history)
    assert builder.export_snapshot() == original
