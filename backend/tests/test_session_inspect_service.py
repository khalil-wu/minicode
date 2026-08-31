from backend.services.session_inspect_service import build_usage_inspect_result


def test_usage_inspect_reports_prompt_cache_metrics() -> None:
    ledger = {
        "estimated_tokens": 3900,
        "actual_tokens": 4000,
        "entries": [{"category": "history", "estimated_tokens": 1200}],
    }
    budget_event, context_event, outcome = build_usage_inspect_result(
        session_id="session-cache",
        conversation_id="conv-cache",
        tracker_summary={
            "input_tokens": 800,
            "output_tokens": 120,
            "cache_creation_tokens": 50,
            "cache_read_tokens": 200,
            "total_cost_usd": 0.0123,
        },
        budget_snapshot={
            "used": 4000,
            "total": 8000,
            "breakdown": {"stable_prompt": 2500, "history": 1500},
        },
        context_ledger=ledger,
    )

    assert budget_event.type == "budget_update"
    assert budget_event.data["conversation_id"] == "conv-cache"
    assert context_event.type == "context_usage"
    assert context_event.data == {
        "used": 4000,
        "limit": 8000,
        "conversation_id": "conv-cache",
        "ledger": ledger,
    }
    # Derived-denominator path (no authoritative ``prompt_cache_total_tokens``
    # in the summary). The denominator is now provider-semantics aware and
    # consistent with CostTracker.record_usage's own derivation: with
    # input_includes_cache_read/write both defaulting to True, input_tokens
    # already contains the read and write tokens, so ordinary = 800-200-50=550
    # and the effective prompt total is 550+200+50 = 800 -> 200/800 = 25.0%.
    # The old `max(input, read) + write` heuristic (850 / 23.5%) counted the
    # write tokens twice; see backend/agent/prompt_cache.py.
    assert "prompt cache read 200 write 50 hit 25.0%" in outcome.message
    assert "estimated session cost $0.0123" in outcome.message
    assert outcome.data["cost_scope"] == "session"
    assert outcome.data["prompt_cache"] == {
        "read_tokens": 200,
        "write_tokens": 50,
        "hit_rate": 25.0,
        "denominator_tokens": 800,
    }


def test_usage_inspect_prefers_authoritative_mixed_provider_cache_total() -> None:
    _budget_event, _context_event, outcome = build_usage_inspect_result(
        session_id="session-cache",
        conversation_id="conv-cache",
        tracker_summary={
            "input_tokens": 800,
            "output_tokens": 120,
            "cache_creation_tokens": 50,
            "cache_read_tokens": 200,
            "prompt_cache_total_tokens": 1050,
            "total_cost_usd": 0.0123,
        },
        budget_snapshot={"used": 4000, "total": 8000, "breakdown": {}},
    )

    assert outcome.data["prompt_cache"] == {
        "read_tokens": 200,
        "write_tokens": 50,
        "hit_rate": 19.0,
        "denominator_tokens": 1050,
    }


def test_usage_inspect_includes_zero_prompt_cache_metrics_in_data_only() -> None:
    _budget_event, _context_event, outcome = build_usage_inspect_result(
        session_id="session-cache",
        conversation_id="",
        tracker_summary={
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "total_cost_usd": 0.001,
        },
        budget_snapshot={"used": 10, "total": 100, "breakdown": {}},
    )

    assert "prompt cache" not in outcome.message
    assert outcome.data["prompt_cache"] == {
        "read_tokens": 0,
        "write_tokens": 0,
        "hit_rate": 0.0,
        "denominator_tokens": 100,
    }
