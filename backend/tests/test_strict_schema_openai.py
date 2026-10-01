from __future__ import annotations

import pytest

from backend.llm.openai_adapter import OpenAIAdapter
from backend.llm.openai_payloads import strict_schema_for_openai


def test_strict_schema_required_all_and_null_wrap() -> None:
    schema = {
        "type": "object",
        "properties": {
            "required_name": {"type": "string"},
            "optional_count": {"type": "integer"},
        },
        "required": ["required_name"],
    }
    strict = strict_schema_for_openai(schema)
    assert strict is not None
    assert set(strict["required"]) == {"required_name", "optional_count"}
    assert strict["additionalProperties"] is False
    assert strict["properties"]["optional_count"] == {
        "anyOf": [{"type": "integer"}, {"type": "null"}]
    }
    assert strict["properties"]["required_name"] == {"type": "string"}


def test_strict_schema_preserves_nullable_optionals() -> None:
    schema = {
        "type": "object",
        "properties": {"maybe": {"type": ["string", "null"]}},
    }
    strict = strict_schema_for_openai(schema)
    assert strict is not None
    assert strict["properties"]["maybe"] == {"type": ["string", "null"]}


def test_strict_schema_falls_back_on_unsupported_constructs() -> None:
    assert strict_schema_for_openai({"type": "object", "$ref": "#/x"}) is None
    assert strict_schema_for_openai({"type": "object", "allOf": []}) is None
    assert strict_schema_for_openai({"type": "string"}) is None  # root must be object
    nested = {
        "type": "object",
        "properties": {"inner": {"type": "object", "patternProperties": {"x": {}}}},
    }
    assert strict_schema_for_openai(nested) is None


def test_strict_schema_nested_objects() -> None:
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
        },
    }
    strict = strict_schema_for_openai(schema)
    assert strict is not None
    items_schema = strict["properties"]["items"]["anyOf"][0]
    inner = items_schema["items"]
    assert inner["required"] == ["id"]
    assert inner["additionalProperties"] is False


@pytest.mark.parametrize("api", ["chat", "responses"])
@pytest.mark.parametrize("strict", [False, True])
def test_open_object_contract_survives_non_strict_and_strict_fallback(api, strict):
    open_object = {"type": "object"}
    if strict:
        # Open maps cannot be closed without changing the tool's input contract.
        open_object["additionalProperties"] = True
    schema = {
        "type": "object",
        "properties": {
            "options": open_object,
            "rows": {"type": "array", "items": {"type": "object"}},
            "closed": {"type": "object", "properties": {"name": {"type": "string"}}, "additionalProperties": False},
        },
        "required": ["options"],
    }
    tool = {"type": "function", "function": {"name": "mcp__data__configure", "parameters": schema, "strict": strict}}
    convert = OpenAIAdapter._normalize_chat_tools if api == "chat" else OpenAIAdapter._convert_tools_to_responses_format
    converted = convert([tool])[0]
    function = converted["function"] if api == "chat" else converted

    assert function["strict"] is False
    assert function["parameters"] == schema
    assert "additionalProperties" not in function["parameters"]
    assert function["parameters"]["properties"]["closed"]["additionalProperties"] is False
