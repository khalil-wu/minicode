from __future__ import annotations

from pathlib import Path

import pytest

from backend.evals.benchmark_merge import merge_benchmark_payloads


def _payload(*, task: str, seed: int, passed: bool, marker: str) -> dict:
    return {
        "catalog": "catalog.json",
        "runs": [
            {
                "agent": "minicode",
                "task_id": task,
                "seed": seed,
                "passed": passed,
                "duration_ms": 1,
            }
        ],
        "aggregates": [
            {
                "agent": "minicode",
                "task_id": task,
                "passed": passed,
                "pass_rate": 1.0 if passed else 0.0,
                "minimum_pass_rate": 1.0,
                "seeds": [seed],
                "reports": [
                    {
                        "task_id": task,
                        "seed": seed,
                        "passed": passed,
                        "agent_output": marker,
                    }
                ],
            }
        ],
    }


def _fingerprinted(payload: dict, fingerprint: str) -> dict:
    payload["runtime"] = {
        "fingerprint": fingerprint,
        "git_head": "head",
        "source_roots": ["backend", "scripts"],
        "valid": True,
        "invalid_reason": "",
    }
    return payload


def test_merge_reconstructs_aggregates_and_records_sources() -> None:
    merged = merge_benchmark_payloads(
        [
            (Path("a.json"), _payload(task="task-a", seed=11, passed=True, marker="a")),
            (Path("b.json"), _payload(task="task-b", seed=29, passed=False, marker="b")),
        ]
    )

    assert [(row["task_id"], row["seed"]) for row in merged["runs"]] == [
        ("task-a", 11),
        ("task-b", 29),
    ]
    assert merged["runs"][0]["source_report"] == "a.json"
    assert merged["aggregates"][1]["reports"][0]["agent_output"] == "b"
    assert merged["merge"]["selected_run_count"] == 2


def test_duplicate_policy_last_keeps_summary_and_trace_from_same_shard() -> None:
    first = _payload(task="task-a", seed=11, passed=False, marker="old")
    last = _payload(task="task-a", seed=11, passed=True, marker="new")

    merged = merge_benchmark_payloads(
        [(Path("old.json"), first), (Path("new.json"), last)],
        duplicate_policy="last",
    )

    assert merged["runs"][0]["passed"] is True
    assert merged["runs"][0]["source_report"] == "new.json"
    assert merged["aggregates"][0]["reports"][0]["agent_output"] == "new"
    assert merged["merge"]["duplicate_count"] == 1


def test_duplicate_policy_error_fails_closed() -> None:
    payload = _payload(task="task-a", seed=11, passed=True, marker="trace")

    with pytest.raises(ValueError, match="duplicate benchmark run"):
        merge_benchmark_payloads(
            [(Path("a.json"), payload), (Path("b.json"), payload)],
        )


def test_missing_detailed_report_is_rejected() -> None:
    payload = _payload(task="task-a", seed=11, passed=True, marker="trace")
    payload["aggregates"] = []

    with pytest.raises(ValueError, match="no detailed aggregate report"):
        merge_benchmark_payloads([(Path("a.json"), payload)])


def test_aggregate_pass_uses_configured_minimum_rate() -> None:
    first = _payload(task="task-a", seed=11, passed=True, marker="one")
    second = _payload(task="task-a", seed=29, passed=True, marker="two")
    third = _payload(task="task-a", seed=47, passed=False, marker="three")
    for payload in (first, second, third):
        payload["aggregates"][0]["minimum_pass_rate"] = 2 / 3

    merged = merge_benchmark_payloads(
        [
            (Path("one.json"), first),
            (Path("two.json"), second),
            (Path("three.json"), third),
        ]
    )

    assert merged["aggregates"][0]["pass_rate"] == pytest.approx(2 / 3)
    assert merged["aggregates"][0]["passed"] is True


def test_merge_rejects_different_runtime_fingerprints() -> None:
    first = _fingerprinted(
        _payload(task="task-a", seed=11, passed=True, marker="one"),
        "runtime-one",
    )
    second = _fingerprinted(
        _payload(task="task-b", seed=11, passed=True, marker="two"),
        "runtime-two",
    )

    with pytest.raises(ValueError, match="different runtime fingerprints"):
        merge_benchmark_payloads(
            [(Path("one.json"), first), (Path("two.json"), second)]
        )


def test_legacy_merge_is_explicitly_not_a_valid_fixed_runtime_report() -> None:
    merged = merge_benchmark_payloads(
        [
            (
                Path("legacy.json"),
                _payload(task="task-a", seed=11, passed=True, marker="legacy"),
            )
        ]
    )

    assert merged["runtime"]["valid"] is False
    assert "fingerprint unavailable" in merged["runtime"]["invalid_reason"]
