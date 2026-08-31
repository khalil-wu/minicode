import pytest

from backend.agent.turn_context_runtime import _coerce_extension_context_messages


def test_extension_context_codec_preserves_complete_message_fields() -> None:
    raw = [{
        "role": "assistant",
        "content": "calling",
        "phase": "commentary",
        "providerItems": [{"type": "reasoning", "encrypted_content": "opaque"}],
        "runtimeContext": "runtime",
        "timestampMs": 123,
        "toolCalls": [{"id": "call-1", "name": "read_file", "arguments": {"path": "a.py"}}],
    }, {
        "role": "tool",
        "content": "contents",
        "name": "read_file",
        "toolCallId": "call-1",
        "isError": False,
        "attachmentRefs": [{"artifact_id": "artifact-1"}],
        "timestampMs": 124,
    }]

    decoded = _coerce_extension_context_messages(raw)

    assert decoded[0].provider_items == raw[0]["providerItems"]
    assert decoded[0].tool_calls[0].arguments == {"path": "a.py"}
    assert decoded[0].runtime_context == "runtime"
    assert decoded[1].attachment_refs == [{"artifact_id": "artifact-1"}]
    assert decoded[1].tool_call_id == "call-1"


def test_extension_context_codec_rejects_orphan_tool_result() -> None:
    with pytest.raises(ValueError, match="orphan tool result"):
        _coerce_extension_context_messages([
            {"role": "tool", "content": "orphan", "tool_call_id": "missing"}
        ])
