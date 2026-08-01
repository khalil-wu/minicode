"""Merge benchmark shards without rerunning completed repository tasks."""

from __future__ import annotations

import csv
import json
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Literal


DuplicatePolicy = Literal["error", "first", "last"]
RunKey = tuple[str, str, int]


def _run_key(row: dict[str, Any]) -> RunKey:
    agent = str(row.get("agent") or "").strip()
    task_id = str(row.get("task_id") or "").strip()
    try:
        seed = int(row["seed"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"benchmark run has an invalid seed: {row!r}") from exc
    if not agent or not task_id:
        raise ValueError(f"benchmark run is missing agent/task_id: {row!r}")
    return agent, task_id, seed


def _reports_by_key(payload: dict[str, Any]) -> dict[RunKey, tuple[dict[str, Any], dict[str, Any]]]:
    reports: dict[RunKey, tuple[dict[str, Any], dict[str, Any]]] = {}
    for aggregate in payload.get("aggregates") or []:
        if not isinstance(aggregate, dict):
            continue
        agent = str(aggregate.get("agent") or "").strip()
        task_id = str(aggregate.get("task_id") or "").strip()
        for report in aggregate.get("reports") or []:
            if not isinstance(report, dict):
                continue
            key = _run_key(
                {
                    "agent": agent,
                    "task_id": task_id or report.get("task_id"),
                    "seed": report.get("seed"),
                }
            )
            reports[key] = (aggregate, report)
    return reports


def merge_benchmark_payloads(
    inputs: list[tuple[Path, dict[str, Any]]],
    *,
    duplicate_policy: DuplicatePolicy = "error",
) -> dict[str, Any]:
    """Merge benchmark payloads by ``(agent, task_id, seed)``.

    Input order is authoritative for ``first``/``last``. Detailed reports are
    reconstructed from the same shard as the selected summary row, preventing
    a retry's metrics from being paired with an older raw trace.
    """

    if duplicate_policy not in {"error", "first", "last"}:
        raise ValueError(f"unsupported duplicate policy: {duplicate_policy}")
    if not inputs:
        raise ValueError("at least one benchmark input is required")

    runtime_records = [
        payload.get("runtime")
        for _, payload in inputs
        if isinstance(payload.get("runtime"), dict)
    ]
    invalid_runtime = next(
        (
            record
            for record in runtime_records
            if record.get("valid") is False
        ),
        None,
    )
    if invalid_runtime is not None:
        raise ValueError(
            "cannot merge an invalidated benchmark batch: "
            f"{invalid_runtime.get('invalid_reason') or 'unknown reason'}"
        )
    fingerprints = {
        str(record.get("fingerprint") or "").strip()
        for record in runtime_records
        if str(record.get("fingerprint") or "").strip()
    }
    if len(fingerprints) > 1:
        raise ValueError("cannot merge benchmark batches from different runtime fingerprints")
    if runtime_records and len(runtime_records) != len(inputs):
        raise ValueError(
            "cannot merge fingerprinted and legacy benchmark batches together"
        )

    selected: OrderedDict[
        RunKey,
        tuple[dict[str, Any], dict[str, Any], dict[str, Any], Path],
    ] = OrderedDict()
    duplicate_keys: list[RunKey] = []

    for source, payload in inputs:
        rows = payload.get("runs")
        if not isinstance(rows, list):
            raise ValueError(f"{source}: missing runs array")
        reports = _reports_by_key(payload)
        for raw_row in rows:
            if not isinstance(raw_row, dict):
                raise ValueError(f"{source}: benchmark run must be an object")
            key = _run_key(raw_row)
            if key not in reports:
                raise ValueError(
                    f"{source}: no detailed aggregate report for "
                    f"{key[0]}/{key[1]}/seed-{key[2]}"
                )
            if key in selected:
                duplicate_keys.append(key)
                if duplicate_policy == "error":
                    raise ValueError(
                        "duplicate benchmark run: "
                        f"{key[0]}/{key[1]}/seed-{key[2]}"
                    )
                if duplicate_policy == "first":
                    continue
            aggregate, report = reports[key]
            row = deepcopy(raw_row)
            row["source_report"] = str(source)
            selected[key] = (
                row,
                deepcopy(aggregate),
                deepcopy(report),
                source,
            )

    grouped: OrderedDict[tuple[str, str], list[tuple[RunKey, tuple[Any, ...]]]] = OrderedDict()
    for key, value in selected.items():
        grouped.setdefault((key[0], key[1]), []).append((key, value))

    aggregates: list[dict[str, Any]] = []
    for (agent, task_id), entries in grouped.items():
        template = deepcopy(entries[-1][1][1])
        reports = [entry[1][2] for entry in entries]
        passed_count = sum(bool(report.get("passed")) for report in reports)
        template["agent"] = agent
        template["task_id"] = task_id
        template["reports"] = reports
        template["seeds"] = [key[2] for key, _ in entries]
        pass_rate = passed_count / len(reports) if reports else 0.0
        try:
            minimum_pass_rate = float(template.get("minimum_pass_rate", 1.0))
        except (TypeError, ValueError):
            minimum_pass_rate = 1.0
        template["pass_rate"] = pass_rate
        template["passed"] = bool(reports) and pass_rate >= minimum_pass_rate
        aggregates.append(template)

    rows = [value[0] for value in selected.values()]
    sources = [str(source) for source, _ in inputs]
    runtime = (
        deepcopy(runtime_records[0])
        if runtime_records
        else {
            "fingerprint": "",
            "git_head": "",
            "source_roots": [],
            "valid": False,
            "invalid_reason": "runtime source fingerprint unavailable in legacy inputs",
        }
    )
    return {
        "catalog": "merged",
        "sources": sources,
        "runtime": runtime,
        "merge": {
            "duplicate_policy": duplicate_policy,
            "duplicate_count": len(duplicate_keys),
            "selected_run_count": len(rows),
        },
        "runs": rows,
        "aggregates": aggregates,
    }


def load_and_merge_benchmarks(
    paths: list[Path],
    *,
    duplicate_policy: DuplicatePolicy = "error",
) -> dict[str, Any]:
    inputs: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        resolved = path.expanduser().resolve()
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read benchmark report {resolved}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{resolved}: benchmark payload must be an object")
        inputs.append((resolved, payload))
    return merge_benchmark_payloads(inputs, duplicate_policy=duplicate_policy)


def write_benchmark_payload(payload: dict[str, Any], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "benchmark.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    rows = payload.get("runs") or []
    fieldnames = list(rows[0]) if rows else ["agent", "task_id", "seed", "passed"]
    with (output / "benchmark.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
