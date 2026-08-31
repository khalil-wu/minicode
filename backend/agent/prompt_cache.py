from __future__ import annotations

import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from backend.agent.prompting import (
    _json_fingerprint,
    _short_sha256,
    diff_prompt_section_summaries,
    split_sys_prompt_prefix,
)


_MAX_TRACKED_PROMPT_CACHE_SOURCES = 32
_DEFAULT_MIN_CACHE_DROP_TOKENS = 2_000
_DEFAULT_RELATIVE_CACHE_DROP = 0.05
_PROMPT_CACHE_STATE: "OrderedDict[str, _PromptCacheObservation]" = OrderedDict()


@dataclass(frozen=True)
class _PromptCacheObservation:
    request_summary: dict[str, Any]
    cache_read_tokens: int
    cache_creation_tokens: int
    observed_at: float


def _tool_name_from_schema(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    function = schema.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or "").strip()
    return str(schema.get("name") or "").strip()


def _safe_tool_name(name: str) -> str:
    text = str(name or "").strip()
    if text.startswith("mcp__"):
        return "mcp"
    return text


def _safe_tool_names_from_schemas(tool_schemas: list[dict[str, Any]] | None) -> list[str]:
    names = [
        _safe_tool_name(_tool_name_from_schema(schema))
        for schema in (tool_schemas or [])
    ]
    return [name for name in names if name]


def _safe_tool_names_from_summary(
    summary: dict[str, Any],
    field: str = "tool_names",
) -> list[str]:
    raw = summary.get(field)
    if not isinstance(raw, list):
        return []
    return [_safe_tool_name(str(name)) for name in raw if str(name or "").strip()]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _prompt_cache_hash_state(parent: dict[str, Any], child: dict[str, Any], key: str) -> str:
    parent_value = str(parent.get(key) or "").strip()
    child_value = str(child.get(key) or "").strip()
    if not parent_value or not child_value:
        return "missing"
    return "same" if parent_value == child_value else "changed"


def prompt_cache_fork_diagnostic(
    parent_summary: Any,
    child_summary: Any,
) -> dict[str, Any]:
    """Compare parent/child cache-safe summaries without exposing prompt text.

    This is intentionally hash/count only. Subagents can report whether they
    preserved the parent's cacheable system prefix while still using their own
    restricted execution toolset.
    """
    if not isinstance(parent_summary, dict) or not isinstance(child_summary, dict):
        return {}

    stable_prefix = _prompt_cache_hash_state(parent_summary, child_summary, "stable_system_hash")
    full_system = _prompt_cache_hash_state(parent_summary, child_summary, "full_system_hash")
    tool_schemas = _prompt_cache_hash_state(parent_summary, child_summary, "tools_hash")

    parent_tools = set(_safe_tool_names_from_summary(parent_summary))
    child_tools = set(_safe_tool_names_from_summary(child_summary))
    parent_count = _safe_int(parent_summary.get("message_count"))
    child_count = _safe_int(child_summary.get("message_count"))

    if stable_prefix == "same" and tool_schemas == "same":
        status = "aligned"
    elif stable_prefix == "same":
        status = "prefix_reused"
    elif stable_prefix == "missing":
        status = "unknown"
    else:
        status = "prefix_changed"

    parent_stable_hash = str(parent_summary.get("stable_system_hash") or "").strip()
    child_stable_hash = str(child_summary.get("stable_system_hash") or "").strip()
    parent_tools_hash = str(parent_summary.get("tools_hash") or "").strip()
    child_tools_hash = str(child_summary.get("tools_hash") or "").strip()

    diagnostic: dict[str, Any] = {
        "status": status,
        "stable_prefix": stable_prefix,
        "full_system": full_system,
        "tool_schemas": tool_schemas,
        "cacheable_prefix_reused": stable_prefix == "same",
        "parent_message_count": parent_count,
        "child_message_count": child_count,
        "message_count_delta": child_count - parent_count,
        "prefix_shadow": {
            "parent_stable_system_hash": parent_stable_hash,
            "child_stable_system_hash": child_stable_hash,
        },
        "schema_shadow": {
            "parent_tools_hash": parent_tools_hash,
            "child_tools_hash": child_tools_hash,
            "parent_tool_count": len(parent_tools),
            "child_tool_count": len(child_tools),
            "child_tool_subset_of_parent": bool(parent_tools) and child_tools <= parent_tools,
        },
    }
    if parent_tools or child_tools:
        diagnostic["tool_delta"] = {
            "added": sorted(child_tools - parent_tools),
            "removed": sorted(parent_tools - child_tools),
        }
    return diagnostic


def _tool_schema_size_summary(tool_schemas: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Return size-only tool schema facts for prompt/cost diagnostics."""
    total_chars = 0
    largest: list[dict[str, Any]] = []
    for index, schema in enumerate(tool_schemas or []):
        try:
            raw = json.dumps(
                schema,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
        except TypeError:
            raw = repr(schema)
        chars = len(raw)
        total_chars += chars
        largest.append(
            {
                "name": _safe_tool_name(_tool_name_from_schema(schema)) or f"tool_{index}",
                "chars": chars,
            }
        )
    largest.sort(key=lambda item: (-int(item["chars"]), str(item["name"])))
    return {
        "tools_chars": total_chars,
        "largest_tools": largest[:5],
    }


def _message_cache_shadow(message: Any) -> dict[str, Any]:
    """Hash-only message shape used to locate cache-prefix divergence."""
    role = str(getattr(message, "role", "") or "")
    content = str(getattr(message, "content", "") or "")
    tool_call_id = str(getattr(message, "tool_call_id", "") or "")
    raw_calls = getattr(message, "tool_calls", None) or []
    call_shapes = [
        {
            "id_hash": _short_sha256(str(getattr(call, "id", "") or "")),
            "name": _safe_tool_name(str(getattr(call, "name", "") or "")),
            "arguments_hash": _json_fingerprint(getattr(call, "arguments", None) or {}),
        }
        for call in raw_calls
    ]
    provider_items = getattr(message, "provider_items", None) or []
    return {
        "role": role,
        "content_hash": _short_sha256(content),
        "content_chars": len(content),
        "tool_call_id_hash": _short_sha256(tool_call_id),
        "tool_calls_hash": _json_fingerprint(call_shapes),
        "tool_call_count": len(call_shapes),
        "provider_items_hash": _json_fingerprint(provider_items),
        "provider_item_count": len(provider_items) if isinstance(provider_items, list) else 0,
    }


def build_prompt_cache_safe_params(
    *,
    messages: list[Any],
    tool_schemas: list[dict[str, Any]] | None,
    request_metadata: dict[str, Any] | None = None,
    prompt_section_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a compact, non-secret snapshot of cache-critical request parts.

    This is deliberately a hash-only structure. It can be carried through tool
    execution metadata and subagent starts without leaking prompt text, file
    paths from message content, or tool arguments.
    """
    instruction_blocks: list[str] = []
    seen_instruction_blocks: set[str] = set()
    for message in messages:
        if getattr(message, "role", "") not in {"system", "developer"}:
            continue
        content = str(getattr(message, "content", "") or "").strip()
        if not content or content in seen_instruction_blocks:
            continue
        seen_instruction_blocks.add(content)
        instruction_blocks.append(content)
    instructions = "\n\n".join(instruction_blocks)
    stable_prefix = split_sys_prompt_prefix(instructions).stable_prefix if instructions else ""
    metadata = request_metadata if isinstance(request_metadata, dict) else {}
    tool_size_summary = _tool_schema_size_summary(tool_schemas)
    cache_section_summary = _cache_relevant_prompt_section_summary(prompt_section_summary)
    return {
        "stable_system_hash": _short_sha256(stable_prefix),
        "full_system_hash": _short_sha256(instructions),
        "tools_hash": _json_fingerprint(tool_schemas or []),
        "tool_names": _safe_tool_names_from_schemas(tool_schemas),
        **tool_size_summary,
        "message_count": len(messages),
        "message_shadows": [_message_cache_shadow(message) for message in messages],
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
        "prompt_section_summary": cache_section_summary,
    }


def _cache_relevant_prompt_section_summary(summary: dict[str, Any] | None) -> dict[str, Any]:
    return dict(summary) if isinstance(summary, dict) else {}


def _tracking_key(summary: dict[str, Any], source: str) -> str:
    if source:
        return source
    prompt_cache_hash = str(summary.get("prompt_cache_key_hash") or "").strip()
    if prompt_cache_hash:
        base = f"prompt:{prompt_cache_hash}"
    else:
        base = "|".join(
            [
                str(summary.get("wire_api") or ""),
                str(summary.get("model") or ""),
                str(summary.get("instructions_hash") or ""),
            ]
        )
    return f"default|{base}"


def _int_attr(value: Any, name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, dict):
        raw = value.get(name)
    else:
        raw = getattr(value, name, None)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def prompt_cache_effective_prompt_tokens(
    *,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    provider: str = "",
    input_includes_cache_read: bool = True,
    input_includes_cache_write: bool = True,
    ordinary_input_tokens: int = 0,
    prompt_cache_total_tokens: int = 0,
) -> int:
    """Return the prompt-token denominator used for cache hit-rate reporting.

    Provider usage semantics differ. OpenAI reports cached tokens as a subset of
    prompt/input tokens; Anthropic reports cache read/write tokens separately
    from ordinary input tokens. This helper normalizes both for UI/logging.
    """
    input_count = max(0, int(input_tokens or 0))
    read_count = max(0, int(cache_read_tokens or 0))
    write_count = max(0, int(cache_creation_tokens or 0))
    del provider
    authoritative_total = max(0, int(prompt_cache_total_tokens or 0))
    if authoritative_total:
        return authoritative_total
    ordinary = max(0, int(ordinary_input_tokens or 0))
    if not ordinary:
        ordinary = input_count
        if input_includes_cache_read:
            ordinary -= min(read_count, ordinary)
        if input_includes_cache_write:
            ordinary -= min(write_count, ordinary)
        ordinary = max(0, ordinary)
    return ordinary + read_count + write_count


def prompt_cache_hit_rate(
    *,
    input_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    provider: str = "",
    input_includes_cache_read: bool = True,
    input_includes_cache_write: bool = True,
    ordinary_input_tokens: int = 0,
    prompt_cache_total_tokens: int = 0,
) -> float:
    denominator = prompt_cache_effective_prompt_tokens(
        input_tokens=input_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        provider=provider,
        input_includes_cache_read=input_includes_cache_read,
        input_includes_cache_write=input_includes_cache_write,
        ordinary_input_tokens=ordinary_input_tokens,
        prompt_cache_total_tokens=prompt_cache_total_tokens,
    )
    if denominator <= 0 or cache_read_tokens <= 0:
        return 0.0
    return round(min(100.0, (max(0, int(cache_read_tokens or 0)) / denominator) * 100), 1)


def prompt_cache_usage_stats(usage: Any, provider_raw: Any | None = None) -> dict[str, float | int]:
    provider = ""
    if isinstance(provider_raw, dict):
        summary = provider_raw.get("request_summary")
        provider = str(
            (summary.get("wire_api") if isinstance(summary, dict) else "")
            or provider_raw.get("provider")
            or (summary.get("model") if isinstance(summary, dict) else "")
            or ""
        )
    input_tokens = _int_attr(usage, "input_tokens")
    cache_read = _int_attr(usage, "cache_read_input_tokens")
    cache_creation = _int_attr(usage, "cache_creation_input_tokens")
    cache_deleted = _int_attr(usage, "cache_deleted_input_tokens")
    ordinary = _int_attr(usage, "ordinary_input_tokens")
    authoritative_total = _int_attr(usage, "prompt_cache_total_tokens")
    includes_read = bool(
        usage.get("input_includes_cache_read", True)
        if isinstance(usage, dict)
        else getattr(usage, "input_includes_cache_read", True)
    )
    includes_write = bool(
        usage.get("input_includes_cache_write", True)
        if isinstance(usage, dict)
        else getattr(usage, "input_includes_cache_write", True)
    )
    total = prompt_cache_effective_prompt_tokens(
        input_tokens=input_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        provider=provider,
        input_includes_cache_read=includes_read,
        input_includes_cache_write=includes_write,
        ordinary_input_tokens=ordinary,
        prompt_cache_total_tokens=authoritative_total,
    )
    stats: dict[str, float | int] = {
        "prompt_cache_total_tokens": total,
        "ordinary_input_tokens": (
            ordinary
            if ordinary
            else max(
                0,
                total - cache_read - cache_creation,
            )
        ),
        "prompt_cache_hit_rate": prompt_cache_hit_rate(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read,
            cache_creation_tokens=cache_creation,
            provider=provider,
            input_includes_cache_read=includes_read,
            input_includes_cache_write=includes_write,
            ordinary_input_tokens=ordinary,
            prompt_cache_total_tokens=authoritative_total,
        ),
    }
    if cache_deleted:
        stats["cache_deleted_input_tokens"] = cache_deleted
    return stats


def _request_param_hash(summary: dict[str, Any]) -> str:
    return _json_fingerprint(summary.get("request_params") or {})


def _tool_schema_hashes(summary: dict[str, Any]) -> dict[str, str]:
    raw = summary.get("tool_schema_hashes")
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items()}


def _diff_request_summaries(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> tuple[list[str], dict[str, Any]]:
    changes: list[str] = []
    details: dict[str, Any] = {}

    for field, label in (
        ("model", "model changed"),
        ("wire_api", "wire API changed"),
        ("prompt_cache_key_hash", "prompt cache key changed"),
        ("instructions_hash", "system instructions changed"),
        ("turn_aborted_marker_present", "turn-aborted marker changed"),
    ):
        if previous.get(field) != current.get(field):
            changes.append(label)

    if int(previous.get("instructions_len") or 0) != int(current.get("instructions_len") or 0):
        details["instructions_len_delta"] = int(current.get("instructions_len") or 0) - int(
            previous.get("instructions_len") or 0
        )

    previous_tools = _safe_tool_names_from_summary(previous)
    current_tools = _safe_tool_names_from_summary(current)
    previous_tool_set = set(previous_tools)
    current_tool_set = set(current_tools)
    added_tools = sorted(current_tool_set - previous_tool_set)
    removed_tools = sorted(previous_tool_set - current_tool_set)
    previous_schema_hashes = _tool_schema_hashes(previous)
    current_schema_hashes = _tool_schema_hashes(current)
    changed_schemas = sorted(
        _safe_tool_name(name)
        for name, value in current_schema_hashes.items()
        if name in previous_schema_hashes and previous_schema_hashes[name] != value
    )
    if previous.get("tools_hash") != current.get("tools_hash"):
        changes.append("tools changed")
        details["tool_delta"] = {
            "added_count": len(added_tools),
            "removed_count": len(removed_tools),
            "changed_schema_count": len(changed_schemas),
            "added": added_tools[:8],
            "removed": removed_tools[:8],
            "changed_schemas": changed_schemas[:8],
        }

    if _request_param_hash(previous) != _request_param_hash(current):
        changes.append("request params changed")

    previous_prompt_sections = previous.get("prompt_section_summary")
    current_prompt_sections = current.get("prompt_section_summary")
    section_diff = diff_prompt_section_summaries(
        previous_prompt_sections if isinstance(previous_prompt_sections, dict) else None,
        current_prompt_sections if isinstance(current_prompt_sections, dict) else None,
    )
    if section_diff["status"] == "changed":
        changes.append("prompt sections changed")
        details["prompt_section_delta"] = section_diff

    previous_messages = previous.get("message_shadows")
    current_messages = current.get("message_shadows")
    if isinstance(previous_messages, list) and isinstance(current_messages, list):
        common = 0
        for previous_message, current_message in zip(previous_messages, current_messages):
            if previous_message != current_message:
                break
            common += 1
        if common < max(len(previous_messages), len(current_messages)):
            previous_first = previous_messages[common] if common < len(previous_messages) else None
            current_first = current_messages[common] if common < len(current_messages) else None
            changed_fields: list[str] = []
            if isinstance(previous_first, dict) and isinstance(current_first, dict):
                changed_fields = sorted(
                    key
                    for key in set(previous_first) | set(current_first)
                    if previous_first.get(key) != current_first.get(key)
                )
            details["message_prefix_delta"] = {
                "common_message_count": common,
                "previous_message_count": len(previous_messages),
                "current_message_count": len(current_messages),
                "first_diverging_index": common,
                "previous_role": previous_first.get("role") if isinstance(previous_first, dict) else None,
                "current_role": current_first.get("role") if isinstance(current_first, dict) else None,
                "changed_fields": changed_fields,
            }
            changes.append("message prefix changed")

    return changes, details


def observe_prompt_cache_break(
    *,
    request_summary: Any,
    usage: Any,
    source: str = "",
    min_token_drop: int = _DEFAULT_MIN_CACHE_DROP_TOKENS,
    relative_drop: float = _DEFAULT_RELATIVE_CACHE_DROP,
) -> dict[str, Any] | None:
    """Record one provider response and return a cache-break diagnosis if any."""
    if not isinstance(request_summary, dict):
        return None

    cache_read = _int_attr(usage, "cache_read_input_tokens")
    cache_creation = _int_attr(usage, "cache_creation_input_tokens")
    key = _tracking_key(request_summary, source)
    previous = _PROMPT_CACHE_STATE.get(key)
    current = _PromptCacheObservation(
        request_summary=dict(request_summary),
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        observed_at=time.monotonic(),
    )
    _PROMPT_CACHE_STATE[key] = current
    _PROMPT_CACHE_STATE.move_to_end(key)
    while len(_PROMPT_CACHE_STATE) > _MAX_TRACKED_PROMPT_CACHE_SOURCES:
        _PROMPT_CACHE_STATE.popitem(last=False)

    if previous is None or previous.cache_read_tokens <= 0:
        return None

    token_drop = previous.cache_read_tokens - cache_read
    if token_drop < max(0, min_token_drop):
        return None
    if cache_read >= previous.cache_read_tokens * (1.0 - max(0.0, relative_drop)):
        return None

    changes, details = _diff_request_summaries(previous.request_summary, request_summary)
    if changes:
        reason = ", ".join(changes)
    else:
        age_seconds = max(0.0, current.observed_at - previous.observed_at)
        reason = "prompt unchanged; likely provider eviction or cache TTL expiry"
        details["seconds_since_previous_observation"] = round(age_seconds, 3)

    diagnostic = {
        "status": "cache_break",
        "reason": reason,
        "tracking_key_hash": _short_sha256(key),
        "previous_cache_read_tokens": previous.cache_read_tokens,
        "cache_read_tokens": cache_read,
        "cache_creation_tokens": cache_creation,
        "token_drop": token_drop,
        "changes": changes,
    }
    diagnostic.update(details)
    return diagnostic


def reset_prompt_cache_diagnostics() -> None:
    _PROMPT_CACHE_STATE.clear()
