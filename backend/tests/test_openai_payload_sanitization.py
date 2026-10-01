import pytest

from backend.llm.openai_adapter import _strip_openai_unsupported_fields
from backend.llm.openai_adapter import OpenAIAdapter


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


@pytest.mark.parametrize("container", ["parameters", "input_schema", "schema", "metadata", "client_metadata"])
def test_openai_sanitization_preserves_schema_and_metadata_names(container):
    business_json = {
        "cache_control": {"type": "string"},
        "properties": {"cache_control": {"type": "object", "properties": {"cache_control": {"type": "string"}}}},
        "$defs": {"cache_control": {"type": "string"}},
        "required": ["cache_control"],
    }
    payload = {container: business_json, "cache_control": {"type": "ephemeral"}}

    sanitized = _strip_openai_unsupported_fields(payload)

    assert sanitized == {container: business_json}
    assert payload["cache_control"] == {"type": "ephemeral"}


@pytest.mark.parametrize("api", ["chat", "responses"])
def test_openai_tool_schema_keeps_required_cache_control_property(api):
    tool = {
        "type": "function",
        "cache_control": {"type": "ephemeral"},
        "function": {
            "name": "mcp__cache__configure",
            "parameters": {
                "type": "object",
                "properties": {"cache_control": {"type": "string"}},
                "required": ["cache_control"],
                "additionalProperties": False,
            },
        },
    }
    convert = OpenAIAdapter._normalize_chat_tools if api == "chat" else OpenAIAdapter._convert_tools_to_responses_format
    cleaned = _strip_openai_unsupported_fields({"tools": convert([tool])})["tools"][0]
    parameters = (cleaned["function"] if api == "chat" else cleaned)["parameters"]

    assert parameters == tool["function"]["parameters"]
    assert "cache_control" not in cleaned
