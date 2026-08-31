from __future__ import annotations

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
