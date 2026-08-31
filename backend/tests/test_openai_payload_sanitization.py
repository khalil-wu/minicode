from backend.llm.openai_adapter import _strip_openai_unsupported_fields


def test_openai_payload_sanitization_removes_nested_cache_control() -> None:
    payload = {
        "model": "gpt-test",
        "messages": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "stable prefix",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
        "tools": [
            {
                "type": "function",
                "function": {"name": "read_file"},
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "cache_control": {"type": "ephemeral"},
    }

    sanitized = _strip_openai_unsupported_fields(payload)

    assert "cache_control" not in str(sanitized)
    assert sanitized["messages"][0]["content"][0] == {
        "type": "text",
        "text": "stable prefix",
    }
    assert sanitized["tools"][0] == {
        "type": "function",
        "function": {"name": "read_file"},
    }
