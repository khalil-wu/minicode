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


def _strip_reasoning_visibility_request(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    reasoning = cleaned.get("reasoning")
    if isinstance(reasoning, dict):
        next_reasoning = dict(reasoning)
        next_reasoning.pop("summary", None)
        next_reasoning.pop("content", None)
        if next_reasoning:
            cleaned["reasoning"] = next_reasoning
        else:
            cleaned.pop("reasoning", None)
    cleaned.pop("reasoning_summary", None)
    cleaned.pop("reasoning_content", None)
    include = cleaned.get("include")
    if isinstance(include, list):
        next_include = [item for item in include if item != "reasoning.encrypted_content"]
        if next_include:
            cleaned["include"] = next_include
        else:
            cleaned.pop("include", None)
    return cleaned


def _strip_request_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("metadata", None)
    cleaned.pop("store", None)
    return cleaned


def _strip_prompt_cache_retention_request(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("prompt_cache_retention", None)
    return cleaned


def _strip_metadata_request(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("metadata", None)
    return cleaned


def _strip_responses_stateful_request(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    cleaned.pop("store", None)
    cleaned.pop("previous_response_id", None)
    return cleaned


def _normalize_schema_for_openai(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_normalize_schema_for_openai(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    normalized = {key: _normalize_schema_for_openai(value) for key, value in schema.items()}
    if normalized.get("type") == "object" and "additionalProperties" not in normalized:
        normalized["additionalProperties"] = False
    return normalized
