from __future__ import annotations

from backend.agent.cache_metrics import cache_metric_payload
from backend.agent.provider_protocol import (
    merge_prompt_cache_safe_request_summary as _merge_prompt_cache_safe_request_summary,
    provider_raw_for_projection,
    provider_trace_payload as _provider_trace_payload,
)
from backend.agent.prompt_cache import (
    build_prompt_cache_safe_params,
    observe_prompt_cache_break,
    prompt_cache_effective_prompt_tokens,
    prompt_cache_hit_rate,
    prompt_cache_usage_stats,
    reset_prompt_cache_diagnostics,
)
from backend.agent.prompting import (
    SYSTEM_PROMPT_DYNAMIC_BOUNDARY,
    PromptSection,
    diff_prompt_section_summaries,
    summarize_prompt_sections,
)
from backend.llm.base import LLMMessage, UsageInfo


def _summary(**overrides):
    base = {
        "model": "gpt-5.4",
        "wire_api": "responses",
        "prompt_cache_key_hash": "cache-key",
        "request_params": {"stream": True, "store": False},
        "turn_aborted_marker_present": False,
        "instructions_len": 12,
        "instructions_hash": "instructions-a",
        "tools_len": 1,
        "tools_hash": "tools-a",
        "tool_names": ["read_file"],
        "tool_schema_hashes": {"read_file": "schema-a"},
    }
    base.update(overrides)
    return base


def test_prompt_cache_hit_rate_normalizes_provider_usage_semantics() -> None:
    assert prompt_cache_effective_prompt_tokens(
        input_tokens=800,
        cache_read_tokens=200,
        cache_creation_tokens=50,
    ) == 800
    assert prompt_cache_hit_rate(
        input_tokens=800,
        cache_read_tokens=200,
        cache_creation_tokens=50,
    ) == 25.0

    assert prompt_cache_effective_prompt_tokens(
        input_tokens=800,
        cache_read_tokens=200,
        cache_creation_tokens=50,
        provider="anthropic",
        input_includes_cache_read=False,
        input_includes_cache_write=False,
    ) == 1050
    assert prompt_cache_usage_stats(
        UsageInfo(
            input_tokens=100,
            cache_read_input_tokens=600,
            cache_creation_input_tokens=300,
            input_includes_cache_read=False,
            input_includes_cache_write=False,
        ),
        {"provider": "anthropic"},
    ) == {
        "prompt_cache_total_tokens": 1000,
        "ordinary_input_tokens": 100,
        "prompt_cache_hit_rate": 60.0,
    }


def test_cache_metric_payload_uses_uniform_lookup_contract() -> None:
    payload = cache_metric_payload(
        cache_layer="grep_files.search",
        tool_name="grep_files",
        run_id="run-1",
        turn_id="turn-1",
        args_signature_value="sig",
        hit=True,
        payload_size_bytes=42,
    )

    assert payload == {
        "kind": "cache_metric",
        "type": "cache.lookup",
        "cache_layer": "grep_files.search",
        "tool_name": "grep_files",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "args_signature": "sig",
        "hit": True,
        "stale": False,
        "evicted": False,
        "estimated_saved_ms": 120,
        "observed_at": payload["observed_at"],
        "payload_size_bytes": 42,
    }


def test_prompt_cache_safe_params_hashes_only_stable_prefix() -> None:
    stable = "Stable identity and rules."
    system_a = f"{stable}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nworkspace A"
    system_b = f"{stable}\n\n{SYSTEM_PROMPT_DYNAMIC_BOUNDARY}\n\nworkspace B"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "mcp__private_server__search",
                "description": "Search",
                "parameters": {"type": "object"},
            },
        }
    ]

    first = build_prompt_cache_safe_params(
        messages=[LLMMessage(role="system", content=system_a), LLMMessage(role="user", content="hi")],
        tool_schemas=tools,
        request_metadata={"conversation_id": "conv_1", "cwd": "C:/repo"},
    )
    second = build_prompt_cache_safe_params(
        messages=[LLMMessage(role="system", content=system_b), LLMMessage(role="user", content="hi")],
        tool_schemas=tools,
        request_metadata={"conversation_id": "conv_1", "cwd": "C:/repo"},
    )

    assert first["stable_system_hash"] == second["stable_system_hash"]
    assert first["full_system_hash"] != second["full_system_hash"]
    assert first["tool_names"] == ["mcp"]
    assert first["tools_chars"] > 0
    assert first["largest_tools"] == [{"name": "mcp", "chars": first["tools_chars"]}]
    assert "workspace A" not in str(first)
    assert "private_server" not in str(first)
    assert first["prompt_section_summary"] == {}


def test_prompt_cache_safe_params_deduplicates_replayed_instruction_blocks() -> None:
    repeated = "Stable desktop instructions."

    single = build_prompt_cache_safe_params(
        messages=[
            LLMMessage(role="developer", content=repeated),
            LLMMessage(role="user", content="first"),
            LLMMessage(role="user", content="current"),
        ],
        tool_schemas=[],
    )
    replayed = build_prompt_cache_safe_params(
        messages=[
            LLMMessage(role="developer", content=repeated),
            LLMMessage(role="user", content="first"),
            LLMMessage(role="developer", content=repeated),
            LLMMessage(role="user", content="current"),
        ],
        tool_schemas=[],
    )

    assert replayed["stable_system_hash"] == single["stable_system_hash"]
    assert replayed["full_system_hash"] == single["full_system_hash"]
    assert replayed["message_count"] == 4


def test_prompt_cache_safe_params_preserves_two_layer_section_summary() -> None:
    section_summary = summarize_prompt_sections([
        PromptSection("stable_system", "Stable rules", "stable"),
        PromptSection("current_time", "Current time: 2026-07-13", "context", cache_break=True),
    ])

    safe = build_prompt_cache_safe_params(
        messages=[LLMMessage(role="system", content="Stable rules")],
        tool_schemas=[],
        prompt_section_summary=section_summary,
    )

    assert safe["prompt_section_summary"]["section_count"] == 2
    assert [item["name"] for item in safe["prompt_section_summary"]["sections"]] == [
        "stable_system",
        "current_time",
    ]
    assert safe["prompt_section_summary"]["layers"]["context"]["chars"] > 0


def test_provider_trace_merges_cache_safe_summary_and_loop_metrics() -> None:
    safe_params = build_prompt_cache_safe_params(
        messages=[
            LLMMessage(role="system", content="Stable rules\n\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n\nworkspace A"),
            LLMMessage(role="user", content="hi"),
        ],
        tool_schemas=[{"function": {"name": "read_file", "parameters": {"type": "object"}}}],
        request_metadata={"conversation_id": "conv_1", "cwd": "C:/repo"},
        prompt_section_summary={"section_count": 1, "total_chars": 12},
    )

    summary = _merge_prompt_cache_safe_request_summary(
        {"model": "gpt-5.5", "wire_api": "responses"},
        safe_params,
    )
    payload = _provider_trace_payload(
        provider_raw={
            "provider": "openai_responses",
            "model": "gpt-5.5",
            "request_summary": {"model": "gpt-5.5", "wire_api": "responses"},
            "prompt_cache_safe_params": safe_params,
        },
        usage=UsageInfo(input_tokens=100, cache_read_input_tokens=80),
        finish_reason="stop",
        iteration_id="iter:3",
        call_index=3,
        loop_metrics={"provider_call_count": 3, "tool_batch_count": 2, "tool_call_count": 7},
    )

    assert summary["instructions_hash"] == safe_params["stable_system_hash"]
    assert summary["instructions_full_hash"] == safe_params["full_system_hash"]
    assert summary["tools_chars"] == safe_params["tools_chars"]
    assert summary["largest_tools"] == safe_params["largest_tools"]
    assert summary["prompt_section_summary"] == {"section_count": 1, "total_chars": 12}
    assert payload["request_summary"]["prompt_section_summary"] == {"section_count": 1, "total_chars": 12}
    assert payload["loop_metrics"] == {
        "provider_call_count": 3,
        "tool_batch_count": 2,
        "tool_call_count": 7,
    }
    assert "workspace A" not in str(payload)


def test_provider_projection_keeps_safe_anthropic_metadata_without_refusal_text() -> None:
    sentinel = "DO_NOT_PERSIST_PROVIDER_REFUSAL_EXPLANATION"
    provider_raw = {
        "provider": "anthropic",
        "usage": {
            "input_tokens": 12,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 3,
                "ephemeral_1h_input_tokens": 4,
            },
            "server_tool_use": {
                "web_search_requests": 2,
                "web_fetch_requests": 1,
            },
        },
        "citations": [
            {
                "source": "anthropic:document:abc",
                "title": "Release report",
                "label": "Pages 2–3",
                "range": [2, 3],
                "location_type": "page_location",
                "cited_text": "must not escape",
            }
        ],
        "search_sources": [
            {"title": "Release", "url": "https://example.test/release"}
        ],
        "container": {
            "id": "container-1",
            "expires_at": "2026-08-16T20:00:00Z",
            "content": "must not escape",
        },
        "refusal": {
            "type": "refusal",
            "category": "cyber",
            "explanation": sentinel,
        },
    }

    projected = provider_raw_for_projection(provider_raw)
    payload = _provider_trace_payload(
        provider_raw=provider_raw,
        usage=UsageInfo(input_tokens=12),
        finish_reason="refusal",
        iteration_id="iter:refusal",
        call_index=1,
    )

    assert projected["refusal"] == {
        "type": "refusal",
        "category": "cyber",
        "explanation_available": True,
    }
    assert projected["container"] == {
        "id": "container-1",
        "expires_at": "2026-08-16T20:00:00Z",
    }
    assert projected["citations"] == [
        {
            "source": "anthropic:document:abc",
            "title": "Release report",
            "label": "Pages 2–3",
            "location_type": "page_location",
            "range": [2, 3],
        }
    ]
    assert payload["raw_usage"]["cache_creation"]["ephemeral_1h_input_tokens"] == 4
    assert payload["search_sources"] == provider_raw["search_sources"]
    assert payload["container"] == projected["container"]
    assert payload["refusal"] == projected["refusal"]
    assert sentinel not in str(projected)
    assert sentinel not in str(payload)
    assert "must not escape" not in str(projected)


def test_provider_trace_omits_empty_optional_anthropic_metadata() -> None:
    payload = _provider_trace_payload(
        provider_raw={
            "provider": "anthropic",
            "container": {},
            "refusal": {},
        },
        usage=UsageInfo(),
        finish_reason="end_turn",
        iteration_id="iter:empty-optionals",
        call_index=1,
    )

    assert "container" not in payload
    assert "refusal" not in payload


def test_prompt_section_summary_reports_layers_and_largest_sections() -> None:
    summary = summarize_prompt_sections(
        [
            PromptSection("stable_system", "Stable prompt rules", "stable"),
            PromptSection("skill_context", "skill one\nskill two", "context"),
            PromptSection("task_status", "Task status: fixing", "context", cache_break=True),
        ]
    )

    assert summary["section_count"] == 3
    assert summary["layers"]["stable"] == {
        "chars": len("Stable prompt rules"),
        "sections": 1,
        "cache_break_sections": 0,
    }
    assert summary["layers"]["context"]["cache_break_sections"] == 1
    assert summary["sections"][2]["name"] == "task_status"
    assert summary["sections"][2]["cache_break"] is True
    assert len(summary["sections"][0]["content_hash"]) == 12
    assert summary["largest_sections"][0]["chars"] >= summary["largest_sections"][-1]["chars"]


def test_prompt_section_diff_reports_added_removed_and_changed_sections() -> None:
    previous = summarize_prompt_sections(
        [
            PromptSection("stable_system", "Stable rules", "stable"),
            PromptSection("workspace_summary", "repo A", "context"),
            PromptSection("current_time", "Current time: 2026-07-01", "context", cache_break=True),
        ]
    )
    current = summarize_prompt_sections(
        [
            PromptSection("stable_system", "Stable rules v2", "stable"),
            PromptSection("skill_context", "skill active", "context"),
            PromptSection("current_time", "Current time: 2026-07-02", "context", cache_break=True),
        ]
    )

    diff = diff_prompt_section_summaries(previous, current)

    assert diff["status"] == "changed"
    assert diff["added"] == ["skill_context"]
    assert diff["removed"] == ["workspace_summary"]
    assert diff["section_count_delta"] == 0
    assert any(item["name"] == "stable_system" and "content" in item["changes"] for item in diff["changed_sections"])
    assert any(item["name"] == "current_time" and "content" in item["changes"] for item in diff["changed_sections"])
    assert diff["layer_char_deltas"]["stable"] == len("Stable rules v2") - len("Stable rules")


def test_prompt_cache_break_diagnostic_reports_tool_changes() -> None:
    reset_prompt_cache_diagnostics()

    assert observe_prompt_cache_break(
        request_summary=_summary(),
        usage=UsageInfo(cache_read_input_tokens=10_000),
        source="main:conv_1",
        min_token_drop=1,
    ) is None

    diagnostic = observe_prompt_cache_break(
        request_summary=_summary(
            tools_len=2,
            tools_hash="tools-b",
            tool_names=["read_file", "write_file"],
            tool_schema_hashes={"read_file": "schema-a", "write_file": "schema-b"},
        ),
        usage=UsageInfo(cache_read_input_tokens=1_000, cache_creation_input_tokens=400),
        source="main:conv_1",
        min_token_drop=1,
    )

    assert diagnostic is not None
    assert diagnostic["status"] == "cache_break"
    assert diagnostic["token_drop"] == 9_000
    assert "tools changed" in diagnostic["reason"]
    assert diagnostic["tool_delta"]["added"] == ["write_file"]
    assert diagnostic["cache_creation_tokens"] == 400


def test_prompt_cache_break_diagnostic_reports_prompt_section_delta() -> None:
    reset_prompt_cache_diagnostics()
    previous_sections = summarize_prompt_sections(
        [
            PromptSection("stable_system", "Stable rules", "stable"),
            PromptSection("workspace_summary", "repo A", "context"),
        ]
    )
    current_sections = summarize_prompt_sections(
        [
            PromptSection("stable_system", "Stable rules updated", "stable"),
            PromptSection("workspace_summary", "repo A", "context"),
            PromptSection("skill_context", "skill A", "context"),
        ]
    )

    observe_prompt_cache_break(
        request_summary=_summary(prompt_section_summary=previous_sections),
        usage=UsageInfo(cache_read_input_tokens=9_000),
        source="main:conv_sections",
        min_token_drop=1,
    )
    diagnostic = observe_prompt_cache_break(
        request_summary=_summary(prompt_section_summary=current_sections),
        usage=UsageInfo(cache_read_input_tokens=1_000),
        source="main:conv_sections",
        min_token_drop=1,
    )

    assert diagnostic is not None
    assert "prompt sections changed" in diagnostic["reason"]
    assert diagnostic["prompt_section_delta"]["added"] == ["skill_context"]
    assert diagnostic["prompt_section_delta"]["removed"] == []
    assert any(
        item["name"] == "stable_system"
        for item in diagnostic["prompt_section_delta"]["changed_sections"]
    )


def test_prompt_cache_break_diagnostic_reports_cache_key_change() -> None:
    reset_prompt_cache_diagnostics()

    observe_prompt_cache_break(
        request_summary=_summary(prompt_cache_key_hash="cache-a"),
        usage=UsageInfo(cache_read_input_tokens=7_000),
        source="main:conv_key",
        min_token_drop=1,
    )
    diagnostic = observe_prompt_cache_break(
        request_summary=_summary(prompt_cache_key_hash="cache-b"),
        usage=UsageInfo(cache_read_input_tokens=200),
        source="main:conv_key",
        min_token_drop=1,
    )

    assert diagnostic is not None
    assert "prompt cache key changed" in diagnostic["reason"]


def test_prompt_cache_break_diagnostic_labels_unchanged_prompt_as_provider_side() -> None:
    reset_prompt_cache_diagnostics()
    summary = _summary()

    observe_prompt_cache_break(
        request_summary=summary,
        usage=UsageInfo(cache_read_input_tokens=8_000),
        source="main:conv_2",
        min_token_drop=1,
    )
    diagnostic = observe_prompt_cache_break(
        request_summary=summary,
        usage=UsageInfo(cache_read_input_tokens=500),
        source="main:conv_2",
        min_token_drop=1,
    )

    assert diagnostic is not None
    assert diagnostic["changes"] == []
    assert "prompt unchanged" in diagnostic["reason"]
