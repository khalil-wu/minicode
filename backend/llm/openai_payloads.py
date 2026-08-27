from __future__ import annotations

from typing import Any


def _strip_openai_unsupported_fields(value: Any) -> Any:
    """Remove Anthropic-only fields before sending OpenAI-compatible payloads."""
    if isinstance(value, dict):
        return {
            key: _strip_openai_unsupported_fields(item)
            for key, item in value.items()
            if key != "cache_control"
        }
    if isinstance(value, list):
        return [_strip_openai_unsupported_fields(item) for item in value]
    return value


def _normalize_schema_for_openai(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_schema_for_openai(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    normalized = {key: _normalize_schema_for_openai(value) for key, value in schema.items()}
    if normalized.get("type") == "object" and "additionalProperties" not in normalized:
        normalized["additionalProperties"] = False
    return normalized


# pi constrained-sampling.ts: keys OpenAI structured outputs cannot express.
_UNSUPPORTED_STRICT_SCHEMA_KEYS = frozenset(
    {
        "$ref", "$defs", "definitions", "allOf", "oneOf", "patternProperties",
        "dependentSchemas", "dependencies", "unevaluatedProperties",
        "propertyNames", "contains", "prefixItems", "not", "if", "then", "else",
    }
)


def _schema_allows_null(schema: dict[str, Any]) -> bool:
    if schema.get("type") == "null" or (
        isinstance(schema.get("type"), list) and "null" in schema["type"]
    ):
        return True
    if "const" in schema and schema["const"] is None:
        return True
    if isinstance(schema.get("enum"), list) and None in schema["enum"]:
        return True
    return isinstance(schema.get("anyOf"), list) and any(
        isinstance(variant, dict) and _schema_allows_null(variant)
        for variant in schema["anyOf"]
    )


def _make_schema_strict(schema: Any) -> dict[str, Any] | None:
    """pi's makeJsonSchemaNodeStrict: required-all + null-wrap + additionalProperties.

    Returns None when the schema uses constructs OpenAI structured outputs
    cannot express; callers then fall back to non-strict (pi's
    resolveJsonSchemaStrictSampling contract).
    """
    if not isinstance(schema, dict):
        return None
    if any(key in schema for key in _UNSUPPORTED_STRICT_SCHEMA_KEYS):
        return None

    normalized = dict(schema)

    if "anyOf" in normalized:
        variants = normalized["anyOf"]
        if not isinstance(variants, list) or not variants:
            return None
        converted_variants = []
        for variant in variants:
            if isinstance(variant, dict) and str(variant.get("type") or "") in {"object", "array"}:
                return None
            converted = _make_schema_strict(variant)
            if converted is None:
                return None
            converted_variants.append(converted)
        normalized["anyOf"] = converted_variants

    if "items" in normalized:
        if isinstance(normalized["items"], list):
            return None
        converted_items = _make_schema_strict(normalized["items"])
        if converted_items is None:
            return None
        normalized["items"] = converted_items

    if schema.get("type") != "object":
        return normalized
    if normalized.get("additionalProperties") not in (None, False):
        return None
    properties = schema.get("properties")
    if properties is not None and not isinstance(properties, dict):
        return None
    required_values = schema.get("required")
    if required_values is not None and not isinstance(required_values, list):
        return None

    property_map: dict[str, Any] = {}
    for key, value in (properties or {}).items():
        converted = _make_schema_strict(value)
        if converted is None:
            return None
        property_map[str(key)] = converted
    required = {str(item) for item in (required_values or [])}
    if not required.issubset(property_map):
        return None
    for key, converted in property_map.items():
        if key not in required and not _schema_allows_null(converted):
            property_map[key] = {"anyOf": [converted, {"type": "null"}]}
    normalized["properties"] = property_map
    normalized["required"] = list(property_map)
    normalized["additionalProperties"] = False
    return normalized


def strict_schema_for_openai(schema: Any) -> dict[str, Any] | None:
    """Convert a root tool schema to OpenAI's strict subset (pi semantics)."""
    if not isinstance(schema, dict) or schema.get("type") != "object":
        return None
    return _make_schema_strict(schema)
