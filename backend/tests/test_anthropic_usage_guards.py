"""Regression guards for the Anthropic cumulative usage merge."""

from __future__ import annotations

from backend.llm.anthropic_protocol import _anthropic_usage_value_or_existing


def test_message_delta_zero_does_not_erase_accumulated_output_tokens() -> None:
    """cc updateUsage discipline: message_delta repeats cumulative counters and
    is observed to send 0 mid-stream; a zero must not overwrite a non-zero
    value that an earlier delta already reported."""
    usage_obj = {"output_tokens": 0, "input_tokens": 120}

    assert (
        _anthropic_usage_value_or_existing(usage_obj, "output_tokens", 47)
        == 47
    )
    # A legitimate non-zero cumulative value still overwrites.
    assert (
        _anthropic_usage_value_or_existing(
            {"output_tokens": 53}, "output_tokens", 47
        )
        == 53
    )
    # Missing fields keep the existing counter.
    assert (
        _anthropic_usage_value_or_existing({}, "output_tokens", 47) == 47
    )


def test_message_delta_zero_keeps_existing_cache_counters() -> None:
    usage_obj = {
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }

    assert (
        _anthropic_usage_value_or_existing(
            usage_obj, "cache_read_input_tokens", 900
        )
        == 900
    )
    # 0 -> 0 stays 0 (no-op either direction).
    assert (
        _anthropic_usage_value_or_existing(
            usage_obj, "cache_creation_input_tokens", 0
        )
        == 0
    )
