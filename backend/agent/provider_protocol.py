"""Provider request metadata, stream traces, usage and cache diagnostics."""

from __future__ import annotations

import math
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.agent.terminal_projection import TurnTerminalProjection

from backend.agent.message import AgentEvent
from backend.agent.public_projection import project_public_usage, public_text
from backend.agent.state import AgentState
from backend.llm.base import (
    UsageInfo,
    _normalize_usage_cost,
    _normalize_usage_int,
)


def provider_raw_for_projection(
    provider_raw: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build the allowlisted provider trace exposed to UI/transcripts.

    Adapters may retain richer metadata while deciding recovery. Public answer,
    DONE, Inspector, and transcript projections only need stable identifiers,
    structural counts, locations, and categories. Reconstructing the envelope
    from known fields prevents headers, response bodies, prompts, credentials,
    stack traces, or newly-added provider diagnostics from becoming public by
    default.
    """

    if not isinstance(provider_raw, dict):
        return {}

    raw = provider_raw
    projected: dict[str, Any] = {}

    for key, maximum in (
        ("kind", 80),
        ("provider", 128),
        ("model", 256),
        ("finish_reason", 128),
        ("event_type", 128),
        # Provider request ids are opaque support/correlation identifiers, not
        # credentials.  Keep one bounded, secret-redacted value so Inspector
        # and exported traces remain actionable without exposing headers or
        # arbitrary response metadata.
        ("request_id", 256),
        ("trace_id", 256),
        ("iteration_id", 256),
        ("diagnostics_ref", 256),
        ("response_id_hash", 128),
        ("response_message_phase", 80),
        # Provider adapters historically used ``message_phase`` while the
        # normalized Responses surface uses ``response_message_phase``.
        # Keep both public aliases so Inspector/transcript consumers do not
        # lose the phase that determined final-vs-commentary projection.
        ("message_phase", 80),
        ("terminal_fallback", 128),
        ("recovered_from", 128),
    ):
        value = public_text(raw.get(key), max_chars=maximum, single_line=True)
        if value:
            projected[key] = value
    for key in (
        "provider_refusal",
        "diagnostics_deferred",
        "diagnostics_loaded",
    ):
        if isinstance(raw.get(key), bool):
            projected[key] = bool(raw.get(key))
    for key in ("call_index", "diagnostics_bytes"):
        value = _safe_nonnegative_int(raw.get(key))
        if value is not None:
            projected[key] = value

    refusal = raw.get("refusal")
    if isinstance(refusal, Mapping):
        safe_refusal: dict[str, Any] = {}
        refusal_type = public_text(
            refusal.get("type"), max_chars=80, single_line=True
        )
        category = public_text(
            refusal.get("category"), max_chars=80, single_line=True
        )
        if refusal_type:
            safe_refusal["type"] = refusal_type
        if category:
            safe_refusal["category"] = category
        if str(refusal.get("explanation") or "").strip() or bool(
            refusal.get("explanation_available")
        ):
            safe_refusal["explanation_available"] = True
        if safe_refusal:
            projected["refusal"] = safe_refusal

    container = raw.get("container")
    if isinstance(container, Mapping):
        safe_container = {
            key: public_text(
                container.get(key), max_chars=256, single_line=True
            )
            for key in ("id", "expires_at")
            if str(container.get(key) or "").strip()
        }
        if safe_container:
            projected["container"] = safe_container

    search_sources = raw.get("search_sources")
    if isinstance(search_sources, list):
        safe_sources: list[dict[str, str]] = []
        for source in search_sources[:256]:
            if not isinstance(source, Mapping):
                continue
            title = public_text(
                source.get("title"), max_chars=512, single_line=True
            )
            url = public_text(source.get("url"), max_chars=2_048, single_line=True)
            if not title and not url:
                continue
            item: dict[str, str] = {}
            if title:
                item["title"] = title
            if url:
                item["url"] = url
            if item not in safe_sources:
                safe_sources.append(item)
        if safe_sources:
            projected["search_sources"] = safe_sources

    citations = raw.get("citations")
    if isinstance(citations, list):
        safe_citations: list[dict[str, Any]] = []
        for citation in citations[:512]:
            if not isinstance(citation, Mapping):
                continue
            item: dict[str, Any] = {}
            for key, maximum in (
                ("source", 2_048),
                ("url", 2_048),
                ("title", 512),
                ("label", 256),
                ("location_type", 80),
            ):
                value = public_text(
                    citation.get(key), max_chars=maximum, single_line=True
                )
                if value:
                    item[key] = value
            location_range = citation.get("range")
            if isinstance(location_range, (list, tuple)) and len(location_range) == 2:
                try:
                    item["range"] = [
                        max(0, int(location_range[0])),
                        max(0, int(location_range[1])),
                    ]
                except (TypeError, ValueError):
                    item["range"] = [0, 0]
            else:
                item["range"] = [0, 0]
            if (item.get("url") or item.get("source")) and item not in safe_citations:
                safe_citations.append(item)
        if safe_citations:
            projected["citations"] = safe_citations

    for source_key, target_key in (("usage", "usage"), ("raw_usage", "raw_usage")):
        usage = project_public_usage(raw.get(source_key))
        if usage:
            projected[target_key] = usage

    output_items = _safe_provider_output_items(raw.get("output_items"))
    if output_items:
        projected["output_items"] = output_items
    timeline = _safe_provider_timeline(raw.get("provider_timeline"))
    if timeline:
        projected["provider_timeline"] = timeline
    request_summary = _safe_provider_request_summary(
        merge_prompt_cache_safe_request_summary(
            raw.get("request_summary"),
            raw.get("prompt_cache_safe_params")
            if isinstance(raw.get("prompt_cache_safe_params"), dict)
            else {},
        )
    )
    if request_summary:
        projected["request_summary"] = request_summary
    prompt_cache = _safe_prompt_cache_diagnostic(raw.get("prompt_cache_diagnostic"))
    if prompt_cache:
        projected["prompt_cache_diagnostic"] = prompt_cache
    safety = _safe_boolean_mapping(
        raw.get("safety"),
        ("redacted_prompt", "has_encrypted_reasoning"),
    )
    if safety:
        projected["safety"] = safety
    loop_metrics = _safe_count_mapping(
        raw.get("loop_metrics"),
        (
            "provider_call_count",
            "iteration",
            "iteration_limit",
            "iteration_hard_limit",
            "tool_batch_count",
            "tool_call_count",
            "completed_tool_call_count",
            "pending_tool_call_count",
            "elapsed_ms",
        ),
    )
    if loop_metrics:
        projected["loop_metrics"] = loop_metrics
    side_calls = _safe_side_calls(raw.get("side_calls"))
    if side_calls:
        projected["side_calls"] = side_calls
    provider_items_summary = _safe_provider_items_summary(
        raw.get("provider_items_summary")
    )
    if provider_items_summary:
        projected["provider_items_summary"] = provider_items_summary
    return projected


def _safe_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0 or not numeric.is_integer():
        return None
    return int(numeric)


def _safe_number(value: Any) -> int | float | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric):
        return None
    return int(numeric) if numeric.is_integer() else numeric


def _safe_count_mapping(value: Any, fields: tuple[str, ...]) -> dict[str, int]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, int] = {}
    for key in fields:
        count = _safe_nonnegative_int(source.get(key))
        if count is not None:
            result[key] = count
    return result


def _safe_boolean_mapping(value: Any, fields: tuple[str, ...]) -> dict[str, bool]:
    source = value if isinstance(value, Mapping) else {}
    return {
        key: bool(source.get(key))
        for key in fields
        if isinstance(source.get(key), bool)
    }


def _safe_string_list(value: Any, *, maximum: int = 256) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:maximum]:
        rendered = public_text(item, max_chars=256, single_line=True)
        if rendered and rendered not in result:
            result.append(rendered)
    return result


def _safe_provider_output_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    string_fields = (
        "type",
        "id",
        "status",
        "call_id",
        "name",
        "role",
        "phase",
        "action_type",
    )
    count_fields = ("index", "arguments_chars", "summary_count")
    for raw_item in value[:64]:
        if not isinstance(raw_item, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in string_fields:
            rendered = public_text(
                raw_item.get(key), max_chars=256, single_line=True
            )
            if rendered:
                item[key] = rendered
        for key in count_fields:
            count = _safe_nonnegative_int(raw_item.get(key))
            if count is not None:
                item[key] = count
        content_types = _safe_string_list(raw_item.get("content_types"), maximum=32)
        if content_types:
            item["content_types"] = content_types
        if isinstance(raw_item.get("has_encrypted_content"), bool):
            item["has_encrypted_content"] = bool(
                raw_item.get("has_encrypted_content")
            )
        if item.get("type"):
            result.append(item)
    return result


def _safe_provider_timeline(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    string_fields = (
        "event",
        "response_id_hash",
        "item_type",
        "item_id",
        "call_id",
        "name",
        "status",
        "finish_reason",
        "phase",
    )
    numeric_fields = (
        "output_index",
        "content_index",
        "sequence_number",
        "delta_chars",
        "text_chars",
        "arguments_chars",
        "code_chars",
        "annotation_count",
        "output_items_len",
        "omitted",
        "elapsed_ms",
    )
    result: list[dict[str, Any]] = []
    for raw_item in value[:512]:
        if not isinstance(raw_item, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in string_fields:
            rendered = public_text(
                raw_item.get(key), max_chars=256, single_line=True
            )
            if rendered:
                item[key] = rendered
        for key in numeric_fields:
            number = _safe_number(raw_item.get(key))
            if number is not None and number >= 0:
                item[key] = number
        if isinstance(raw_item.get("usage_present"), bool):
            item["usage_present"] = bool(raw_item.get("usage_present"))
        if item.get("event"):
            result.append(item)
    return result


def _safe_provider_items_summary(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    for key in ("count", "encrypted_reasoning_items"):
        count = _safe_nonnegative_int(source.get(key))
        if count is not None:
            result[key] = count

    raw_counts = source.get("item_counts")
    if isinstance(raw_counts, Mapping):
        item_counts: dict[str, int] = {}
        for raw_name, raw_count in list(raw_counts.items())[:64]:
            name = public_text(raw_name, max_chars=80, single_line=True)
            count = _safe_nonnegative_int(raw_count)
            if name and count is not None:
                item_counts[name] = count
        if item_counts:
            result["item_counts"] = item_counts

    hashes = _safe_string_list(
        source.get("encrypted_reasoning_hashes"),
        maximum=8,
    )
    if hashes:
        result["encrypted_reasoning_hashes"] = hashes
    return result


def _safe_named_count_rows(
    value: Any,
    *,
    string_fields: tuple[str, ...],
    numeric_fields: tuple[str, ...],
    maximum: int = 128,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw_item in value[:maximum]:
        if not isinstance(raw_item, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in string_fields:
            rendered = public_text(
                raw_item.get(key), max_chars=256, single_line=True
            )
            if rendered:
                item[key] = rendered
        for key in numeric_fields:
            number = _safe_number(raw_item.get(key))
            if number is not None:
                item[key] = number
        if item:
            result.append(item)
    return result


def _safe_prompt_section_summary(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = _safe_count_mapping(
        source,
        ("section_count", "total_chars"),
    )
    layers = source.get("layers")
    if isinstance(layers, Mapping):
        safe_layers: dict[str, Any] = {}
        for raw_name, raw_layer in list(layers.items())[:16]:
            name = public_text(raw_name, max_chars=80, single_line=True)
            counts = _safe_count_mapping(
                raw_layer,
                ("chars", "sections", "cache_break_sections"),
            )
            if name and counts:
                safe_layers[name] = counts
        if safe_layers:
            result["layers"] = safe_layers
    sections: list[dict[str, Any]] = []
    raw_sections = source.get("sections")
    if isinstance(raw_sections, list):
        for raw_item in raw_sections[:128]:
            if not isinstance(raw_item, Mapping):
                continue
            item: dict[str, Any] = {}
            for key in ("name", "layer", "content_hash"):
                rendered = public_text(
                    raw_item.get(key), max_chars=256, single_line=True
                )
                if rendered:
                    item[key] = rendered
            for key in ("index", "chars", "lines"):
                count = _safe_nonnegative_int(raw_item.get(key))
                if count is not None:
                    item[key] = count
            if isinstance(raw_item.get("cache_break"), bool):
                item["cache_break"] = bool(raw_item.get("cache_break"))
            if item:
                sections.append(item)
    if sections:
        result["sections"] = sections
    largest = _safe_named_count_rows(
        source.get("largest_sections"),
        string_fields=("name", "layer"),
        numeric_fields=("chars",),
    )
    if largest:
        result["largest_sections"] = largest
    return result


def _safe_provider_request_summary(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    for key in (
        "model",
        "wire_api",
        "instructions_hash",
        "instructions_full_hash",
        "tools_hash",
        "prompt_cache_key_hash",
    ):
        rendered = public_text(source.get(key), max_chars=256, single_line=True)
        if rendered:
            result[key] = rendered
    for key in (
        "instructions_len",
        "instructions_sent_len",
        "tools_len",
        "tools_chars",
        "input_items_len",
        "input_items_sent_len",
        "input_items_logical_len",
        "input_chars",
        "message_count",
    ):
        count = _safe_nonnegative_int(source.get(key))
        if count is not None:
            result[key] = count
    for key in ("prompt_cache_key_present", "turn_aborted_marker_present"):
        if isinstance(source.get(key), bool):
            result[key] = bool(source.get(key))
    for key in ("tool_names", "metadata_keys", "request_param_keys"):
        values = _safe_string_list(source.get(key))
        if values:
            result[key] = values
    schema_hashes = source.get("tool_schema_hashes")
    if isinstance(schema_hashes, Mapping):
        safe_hashes: dict[str, str] = {}
        for raw_name, raw_hash in list(schema_hashes.items())[:256]:
            name = public_text(raw_name, max_chars=256, single_line=True)
            digest = public_text(raw_hash, max_chars=256, single_line=True)
            if name and digest:
                safe_hashes[name] = digest
        if safe_hashes:
            result["tool_schema_hashes"] = safe_hashes
    largest_tools = _safe_named_count_rows(
        source.get("largest_tools"),
        string_fields=("name",),
        numeric_fields=("chars",),
    )
    if largest_tools:
        result["largest_tools"] = largest_tools
    largest_inputs = _safe_named_count_rows(
        source.get("largest_input_items"),
        string_fields=("type", "role", "name", "content_hash"),
        numeric_fields=("index", "chars"),
    )
    if largest_inputs:
        result["largest_input_items"] = largest_inputs
    duplicates = _safe_named_count_rows(
        source.get("duplicate_input_content"),
        string_fields=("type", "role", "content_hash"),
        numeric_fields=("count", "chars"),
    )
    if duplicates:
        result["duplicate_input_content"] = duplicates
    item_counts = _safe_count_mapping(
        source.get("input_item_counts"),
        tuple(str(key) for key in (source.get("input_item_counts") or {}).keys())
        if isinstance(source.get("input_item_counts"), Mapping)
        else (),
    )
    if item_counts:
        result["input_item_counts"] = item_counts
    request_params = source.get("request_params")
    if isinstance(request_params, Mapping):
        safe_params: dict[str, Any] = {}
        for key in (
            "temperature",
            "top_p",
            "max_tokens",
            "max_output_tokens",
            "reasoning_effort",
            "service_tier",
            "stream",
            "parallel_tool_calls",
        ):
            raw_value = request_params.get(key)
            if isinstance(raw_value, bool):
                safe_params[key] = raw_value
            elif isinstance(raw_value, (int, float)):
                number = _safe_number(raw_value)
                if number is not None:
                    safe_params[key] = number
            elif isinstance(raw_value, str):
                rendered = public_text(
                    raw_value, max_chars=80, single_line=True
                )
                if rendered:
                    safe_params[key] = rendered
        if safe_params:
            result["request_params"] = safe_params
    prompt_sections = _safe_prompt_section_summary(
        source.get("prompt_section_summary")
    )
    if prompt_sections:
        result["prompt_section_summary"] = prompt_sections
    return result


def _safe_prompt_section_delta(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    status = public_text(source.get("status"), max_chars=80, single_line=True)
    if status:
        result["status"] = status
    for key in ("added", "removed"):
        values = _safe_string_list(source.get(key), maximum=128)
        if values:
            result[key] = values
    for key in ("section_count_delta", "total_chars_delta"):
        number = _safe_number(source.get(key))
        if number is not None:
            result[key] = number
    layer_deltas = source.get("layer_char_deltas")
    if isinstance(layer_deltas, Mapping):
        safe_deltas: dict[str, int | float] = {}
        for raw_name, raw_value in list(layer_deltas.items())[:32]:
            name = public_text(raw_name, max_chars=80, single_line=True)
            number = _safe_number(raw_value)
            if name and number is not None:
                safe_deltas[name] = number
        if safe_deltas:
            result["layer_char_deltas"] = safe_deltas
    changed: list[dict[str, Any]] = []
    raw_changed = source.get("changed_sections")
    if isinstance(raw_changed, list):
        for raw_item in raw_changed[:128]:
            if not isinstance(raw_item, Mapping):
                continue
            item: dict[str, Any] = {}
            for key in ("name", "before_layer", "after_layer"):
                rendered = public_text(
                    raw_item.get(key), max_chars=256, single_line=True
                )
                if rendered:
                    item[key] = rendered
            chars_delta = _safe_number(raw_item.get("chars_delta"))
            if chars_delta is not None:
                item["chars_delta"] = chars_delta
            changes = _safe_string_list(raw_item.get("changes"), maximum=32)
            if changes:
                item["changes"] = changes
            if item:
                changed.append(item)
    if changed:
        result["changed_sections"] = changed
    return result


def _safe_prompt_cache_diagnostic(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {}
    for key in ("status", "reason", "tracking_key_hash"):
        rendered = public_text(source.get(key), max_chars=512, single_line=True)
        if rendered:
            result[key] = rendered
    for key in (
        "previous_cache_read_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "token_drop",
        "seconds_since_previous_observation",
        "instructions_len_delta",
    ):
        number = _safe_number(source.get(key))
        if number is not None:
            result[key] = number
    changes = _safe_string_list(source.get("changes"), maximum=128)
    if changes:
        result["changes"] = changes
    tool_delta = source.get("tool_delta")
    if isinstance(tool_delta, Mapping):
        safe_tool_delta: dict[str, Any] = _safe_count_mapping(
            tool_delta,
            ("added_count", "removed_count", "changed_schema_count"),
        )
        for key in ("added", "removed", "changed_schemas"):
            values = _safe_string_list(tool_delta.get(key), maximum=64)
            if values:
                safe_tool_delta[key] = values
        if safe_tool_delta:
            result["tool_delta"] = safe_tool_delta
    section_delta = _safe_prompt_section_delta(source.get("prompt_section_delta"))
    if section_delta:
        result["prompt_section_delta"] = section_delta
    return result


def _safe_side_calls(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for raw_item in value[:128]:
        if not isinstance(raw_item, Mapping):
            continue
        item: dict[str, Any] = {}
        for key in ("id", "operation", "provider", "model", "status", "error_type"):
            rendered = public_text(
                raw_item.get(key), max_chars=256, single_line=True
            )
            if rendered:
                item[key] = rendered
        for key in ("elapsed_ms", "attempts", "retry_count"):
            count = _safe_nonnegative_int(raw_item.get(key))
            if count is not None:
                item[key] = count
        usage = project_public_usage(raw_item.get("usage"))
        if usage:
            item["usage"] = usage
        if item:
            result.append(item)
    return result


def usage_terminal_projection(
    usage: UsageInfo,
    provider_raw: dict[str, Any] | None = None,
    *,
    status: str = "completed",
    reason: str = "",
) -> TurnTerminalProjection:
    from backend.agent.terminal_projection import TurnTerminalProjection

    return TurnTerminalProjection.from_usage(
        usage,
        provider_raw=provider_raw_for_projection(provider_raw),
        status=status,
        reason=reason,
    )


def add_usage(left: UsageInfo, right: UsageInfo | None) -> UsageInfo:
    """Accumulate into the turn-owned mutable usage bucket.

    ``LLMAdapter.bind_turn_usage`` keeps a ContextVar reference to ``left`` so
    non-stream side calls can charge the same turn. Replacing that object here
    would split accounting after the first provider response: stream usage
    would move to a new object while compaction/recovery kept mutating the
    stale bound bucket.
    """
    if right is None:
        return left
    left.input_tokens += _normalize_usage_int(getattr(right, "input_tokens", 0))
    left.output_tokens += _normalize_usage_int(getattr(right, "output_tokens", 0))
    left.cache_creation_input_tokens += _normalize_usage_int(
        getattr(right, "cache_creation_input_tokens", 0)
    )
    left.cache_read_input_tokens += _normalize_usage_int(
        getattr(right, "cache_read_input_tokens", 0)
    )
    left.ordinary_input_tokens += _normalize_usage_int(
        getattr(right, "normalized_ordinary_input_tokens", 0)
    )
    left.prompt_cache_total_tokens += _normalize_usage_int(
        getattr(right, "normalized_prompt_cache_total_tokens", 0)
    )
    left.cache_deleted_input_tokens = max(
        left.cache_deleted_input_tokens,
        _normalize_usage_int(getattr(right, "cache_deleted_input_tokens", 0)),
    )
    left.reasoning_output_tokens += _normalize_usage_int(
        getattr(right, "reasoning_output_tokens", 0)
    )
    left.cost_usd += _normalize_usage_cost(getattr(right, "cost_usd", 0.0))
    if not bool(getattr(right, "input_includes_cache_read", True)):
        left.input_includes_cache_read = False
    if not bool(getattr(right, "input_includes_cache_write", True)):
        left.input_includes_cache_write = False
    return left


def build_llm_request_metadata(
    *,
    metadata: dict[str, Any],
    session_id: str,
    task_id: str,
    workspace_root: Path | None,
    run_id: str,
    conversation_id: str,
) -> dict[str, Any]:
    explicit = metadata.get("llm_request_metadata")
    remaining: dict[str, Any] = dict(explicit) if isinstance(explicit, dict) else {}
    request: dict[str, Any] = {}
    # ``assistant_message_id`` is the provider-facing turn correlation id used
    # by the host when no explicit routing turn is supplied. Keep the durable
    # runtime incarnation in ``run_id``. Codex carries an optional turn_id in
    # its responses metadata; mapping the assistant message id onto that
    # canonical turn field is MiniCode's own convention.
    assistant_message_id = str(metadata.get("assistant_message_id") or "").strip()

    def pop_explicit(key: str, fallback: Any = "") -> Any:
        value = remaining.pop(key, None)
        if value is None or (isinstance(value, str) and not value.strip()):
            return fallback
        return value

    effective_conversation_id = str(
        pop_explicit("conversation_id", conversation_id) or ""
    ).strip()
    runtime_session_id = str(
        pop_explicit("minicode_session_id", session_id) or ""
    ).strip()
    app_session_id = str(
        pop_explicit("minicode_app_session_id", runtime_session_id) or ""
    ).strip()
    effective_task_id = str(
        pop_explicit("minicode_task_id", task_id) or ""
    ).strip()
    effective_run_id = str(pop_explicit("run_id", run_id) or "").strip()

    agent_mode = str(
        metadata.get("agent_mode") or remaining.get("agent_mode") or ""
    ).strip().lower()
    agent_role = str(
        metadata.get("agent_role") or remaining.get("agent_role") or ""
    ).strip().lower()
    is_non_root_agent = (
        agent_mode in {"subagent", "background"}
        or agent_role in {"subagent", "background"}
        or agent_role.startswith("subagent:")
    )

    # Root threads carry their own session identity. Child agents keep the
    # root session for prompt-cache affinity while receiving a distinct thread
    # id (Codex keeps session identity provider-side; this split is MiniCode's
    # design). These canonical keys are provider lineage, whereas the
    # ``minicode_*`` fields below remain host diagnostics/compatibility data.
    canonical_session_id = str(
        pop_explicit(
            "session_id",
            effective_conversation_id or app_session_id or runtime_session_id,
        )
        or ""
    ).strip()
    canonical_thread_id = str(
        pop_explicit(
            "thread_id",
            (
                effective_task_id
                if is_non_root_agent and effective_task_id
                else effective_conversation_id
                or effective_task_id
                or runtime_session_id
            ),
        )
        or ""
    ).strip()
    canonical_turn_id = str(
        pop_explicit("turn_id", assistant_message_id or effective_run_id) or ""
    ).strip()

    if canonical_session_id:
        request["session_id"] = canonical_session_id
    if canonical_thread_id:
        request["thread_id"] = canonical_thread_id
    if canonical_turn_id:
        request["turn_id"] = canonical_turn_id
    if is_non_root_agent:
        request["x-openai-subagent"] = str(
            pop_explicit("x-openai-subagent", "collab_spawn") or "collab_spawn"
        ).strip()
        parent_thread_id = str(
            pop_explicit("parent_thread_id", effective_conversation_id) or ""
        ).strip()
        if parent_thread_id:
            request["parent_thread_id"] = parent_thread_id

    source = pop_explicit(
        "minicode_source", str(metadata.get("minicode_source") or "desktop")
    )
    if source:
        request["minicode_source"] = source
    query_source = str(
        pop_explicit(
            "query_source",
            metadata.get("query_source") or metadata.get("minicode_source") or "user",
        )
        or "user"
    ).strip()
    if query_source:
        request["query_source"] = query_source
    if runtime_session_id:
        request["minicode_session_id"] = runtime_session_id
    if app_session_id:
        request["minicode_app_session_id"] = app_session_id
    if effective_task_id:
        request["minicode_task_id"] = effective_task_id
    cwd = str(workspace_root) if workspace_root is not None else metadata.get("cwd")
    if cwd or remaining.get("cwd"):
        request["cwd"] = pop_explicit("cwd", cwd)
    if effective_conversation_id:
        request["conversation_id"] = effective_conversation_id
    if effective_run_id:
        request["run_id"] = effective_run_id
    if assistant_message_id or remaining.get("assistant_message_id"):
        request["assistant_message_id"] = pop_explicit(
            "assistant_message_id", assistant_message_id
        )
    for key, value in remaining.items():
        request.setdefault(key, value)
    return request


def annotate_request_metadata_with_prompt_cache_fork(
    request_metadata: dict[str, Any],
    fork: dict[str, Any],
) -> None:
    if not isinstance(fork, dict) or not fork:
        return
    prefix_shadow = fork.get("prefix_shadow")
    if not isinstance(prefix_shadow, dict):
        prefix_shadow = {}
    schema_shadow = fork.get("schema_shadow")
    if not isinstance(schema_shadow, dict):
        schema_shadow = {}
    scalar_fields = {
        "prompt_cache_fork_status": fork.get("status"),
        "prompt_cache_fork_stable_prefix": fork.get("stable_prefix"),
        "prompt_cache_parent_stable_hash": prefix_shadow.get("parent_stable_system_hash"),
        "prompt_cache_child_stable_hash": prefix_shadow.get("child_stable_system_hash"),
        "prompt_cache_parent_tools_hash": schema_shadow.get("parent_tools_hash"),
        "prompt_cache_child_tools_hash": schema_shadow.get("child_tools_hash"),
    }
    for key, value in scalar_fields.items():
        text = str(value or "").strip()
        if text:
            request_metadata[key] = text


def prompt_cache_tracking_source(*, run_record: Any, session_id: str, task_id: str) -> str:
    role = str(getattr(run_record, "role", "") or "main")
    conversation_id = str(getattr(run_record, "conversation_id", "") or "").strip()
    if role == "main":
        return f"main:{conversation_id or session_id or 'default'}"
    return f"{role}:{task_id or getattr(run_record, 'run_id', '') or session_id or 'default'}"


def merge_prompt_cache_safe_request_summary(
    request_summary: Any,
    prompt_cache_safe_params: dict[str, Any] | None,
) -> dict[str, Any]:
    summary = dict(request_summary) if isinstance(request_summary, dict) else {}
    safe = prompt_cache_safe_params if isinstance(prompt_cache_safe_params, dict) else {}
    if not safe:
        return summary
    field_defaults = {
        "instructions_hash": safe.get("stable_system_hash"),
        "instructions_full_hash": safe.get("full_system_hash"),
        "tools_hash": safe.get("tools_hash"),
        "tool_names": safe.get("tool_names"),
        "tools_chars": safe.get("tools_chars"),
        "largest_tools": safe.get("largest_tools"),
        "metadata_keys": safe.get("metadata_keys"),
    }
    for key, value in field_defaults.items():
        if key not in summary and value not in (None, "", []):
            summary[key] = value
    section_summary = safe.get("prompt_section_summary")
    if isinstance(section_summary, dict) and section_summary:
        summary.setdefault("prompt_section_summary", dict(section_summary))
    if "message_count" not in summary:
        try:
            summary["message_count"] = int(safe.get("message_count") or 0)
        except (TypeError, ValueError):
            pass
    return summary


def provider_trace_payload(
    *,
    provider_raw: dict[str, Any],
    usage: UsageInfo,
    finish_reason: str,
    iteration_id: str,
    call_index: int,
    loop_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw = provider_raw_for_projection(provider_raw)
    request_summary = (
        dict(raw.get("request_summary"))
        if isinstance(raw.get("request_summary"), dict)
        else {}
    )
    payload: dict[str, Any] = {
        "kind": "provider_trace",
        "provider": raw.get("provider") or "",
        "model": raw.get("model") or request_summary.get("model") or "",
        "finish_reason": finish_reason or raw.get("finish_reason") or "",
        "event_type": raw.get("event_type") or "",
        "usage": {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": usage.cache_creation_input_tokens,
            "cache_read_input_tokens": usage.cache_read_input_tokens,
            "cache_deleted_input_tokens": usage.cache_deleted_input_tokens,
            "reasoning_output_tokens": usage.reasoning_output_tokens,
            "input_includes_cache_read": usage.input_includes_cache_read,
            "input_includes_cache_write": usage.input_includes_cache_write,
            "ordinary_input_tokens": usage.normalized_ordinary_input_tokens,
            "prompt_cache_total_tokens": usage.normalized_prompt_cache_total_tokens,
            "cost_usd": usage.cost_usd,
        },
        "raw_usage": (
            raw.get("raw_usage")
            if isinstance(raw.get("raw_usage"), dict)
            else raw.get("usage")
            if isinstance(raw.get("usage"), dict)
            else {}
        ),
        "citations": raw.get("citations") if isinstance(raw.get("citations"), list) else [],
        "search_sources": raw.get("search_sources") if isinstance(raw.get("search_sources"), list) else [],
        "output_items": raw.get("output_items") if isinstance(raw.get("output_items"), list) else [],
        "provider_timeline": raw.get("provider_timeline") if isinstance(raw.get("provider_timeline"), list) else [],
        "request_summary": request_summary,
        "prompt_cache_diagnostic": raw.get("prompt_cache_diagnostic") if isinstance(raw.get("prompt_cache_diagnostic"), dict) else {},
        "safety": raw.get("safety") if isinstance(raw.get("safety"), dict) else {"redacted_prompt": True},
        "loop_metrics": raw.get("loop_metrics") if isinstance(raw.get("loop_metrics"), dict) else dict(loop_metrics or {}),
        "iteration_id": iteration_id,
        "call_index": call_index,
    }
    container = raw.get("container")
    if isinstance(container, dict) and container:
        payload["container"] = container
    refusal = raw.get("refusal")
    if isinstance(refusal, dict) and refusal:
        payload["refusal"] = refusal
    for key in (
        "response_id_hash",
        "response_message_phase",
        "terminal_fallback",
        "recovered_from",
        "trace_id",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value:
            payload[key] = value
    if isinstance(raw.get("provider_refusal"), bool):
        payload["provider_refusal"] = bool(raw.get("provider_refusal"))
    side_calls = raw.get("side_calls")
    if isinstance(side_calls, list) and side_calls:
        payload["side_calls"] = side_calls
    provider_items_summary = raw.get("provider_items_summary")
    if isinstance(provider_items_summary, dict) and provider_items_summary:
        payload["provider_items_summary"] = provider_items_summary
    return payload


def loop_metrics_payload(
    *,
    turn_started_at: int,
    state: AgentState,
    provider_call_count: int,
    iteration_limit: int,
    iteration_hard_limit: int,
    tool_batch_count: int,
    turn_start_tool_call_count: int,
    pending_tool_call_count: int = 0,
) -> dict[str, Any]:
    """Build stable loop metrics."""
    completed = max(0, len(state.tool_calls) - max(0, int(turn_start_tool_call_count or 0)))
    pending = max(0, int(pending_tool_call_count or 0))
    now = int(time.time() * 1000)
    return {
        "provider_call_count": max(0, int(provider_call_count or 0)),
        "iteration": max(0, int(state.iterations or 0)),
        "iteration_limit": max(0, int(iteration_limit or 0)),
        "iteration_hard_limit": max(0, int(iteration_hard_limit or 0)),
        "tool_batch_count": max(0, int(tool_batch_count or 0)),
        "tool_call_count": completed + pending,
        "completed_tool_call_count": completed,
        "pending_tool_call_count": pending,
        "elapsed_ms": max(0, now - int(turn_started_at or now)),
    }
