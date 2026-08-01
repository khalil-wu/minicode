from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.evals.repository_tasks import RepositoryTaskRunner, load_repository_task


_RUNTIME_SOURCE_ROOTS = ("backend", "scripts")
_RUNTIME_SOURCE_SUFFIXES = {".py", ".json", ".toml", ".md"}


def _runtime_source_fingerprint(root: Path) -> str:
    """Hash the executable benchmark/runtime source, including untracked files."""

    digest = hashlib.sha256()
    files: list[Path] = []
    for relative_root in _RUNTIME_SOURCE_ROOTS:
        source_root = root / relative_root
        if not source_root.exists():
            continue
        files.extend(
            path
            for path in source_root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in _RUNTIME_SOURCE_SUFFIXES
            and "__pycache__" not in path.parts
            and not (
                relative_root == "backend"
                and "tests" in path.relative_to(source_root).parts
            )
        )
    for file_name in ("pyproject.toml",):
        path = root / file_name
        if path.is_file():
            files.append(path)
    for path in sorted(set(files), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        content = path.read_bytes()
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _agent_command(agent: str, models: dict[str, str]) -> list[str]:
    if agent == "minicode":
        return [sys.executable, "-m", "backend.evals.minicode_driver"]
    command = [sys.executable, str(ROOT / "scripts" / "external-agent-driver.py"), "--agent", agent]
    model = models.get(agent, "").strip()
    if model:
        command.extend(["--model", model])
    return command


def _aggregate_rows(reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in reports:
        for report in item["reports"]:
            metrics = report.get("agent_metrics") or {}
            rows.append(
                {
                    "agent": item["agent"],
                    "task_id": item["task_id"],
                    "seed": report["seed"],
                    "passed": report["passed"],
                    "duration_ms": report["duration_ms"],
                    "input_tokens": metrics.get("input_tokens", 0),
                    "output_tokens": metrics.get("output_tokens", 0),
                    "cache_read_input_tokens": metrics.get("cache_read_input_tokens", 0),
                    "tool_call_count": metrics.get("tool_call_count", 0),
                    "tool_failure_count": metrics.get("tool_failure_count", 0),
                    "invalid_search_count": metrics.get("invalid_search_count", 0),
                    "recovery_count": metrics.get("recovery_count", 0),
                    "cost_usd": metrics.get("cost_usd", 0.0),
                    "infrastructure_error": report.get("infrastructure_error", ""),
                    "failure_category": (report.get("failure_attribution") or {}).get("category", ""),
                    "failure_detail": (report.get("failure_attribution") or {}).get("detail", ""),
                }
            )
    return rows


def _benchmark_payload(
    *,
    catalog_path: Path,
    reports: list[dict[str, Any]],
    runtime_fingerprint: str,
    git_head: str,
    valid: bool = True,
    invalid_reason: str = "",
) -> dict[str, Any]:
    return {
        "catalog": str(catalog_path),
        "runtime": {
            "fingerprint": runtime_fingerprint,
            "git_head": git_head,
            "source_roots": list(_RUNTIME_SOURCE_ROOTS),
            "valid": valid,
            "invalid_reason": invalid_reason,
        },
        "runs": _aggregate_rows(reports),
        "aggregates": reports,
    }


def _write_outputs(output: Path, payload: dict[str, Any]) -> None:
    """Atomically checkpoint the benchmark after every completed task."""

    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "benchmark.json"
    json_tmp = output / "benchmark.json.tmp"
    json_tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    json_tmp.replace(json_path)

    rows = payload.get("runs") or []
    csv_path = output / "benchmark.csv"
    csv_tmp = output / "benchmark.csv.tmp"
    with csv_tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=list(rows[0]) if rows else ["agent", "task_id", "seed", "passed"],
        )
        writer.writeheader()
        writer.writerows(rows)
    csv_tmp.replace(csv_path)


def _load_resume_reports(
    output: Path,
    catalog_path: Path,
    *,
    runtime_fingerprint: str,
) -> list[dict[str, Any]]:
    report_path = output / "benchmark.json"
    if not report_path.exists():
        return []
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot resume from {report_path}: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("aggregates"), list):
        raise ValueError(f"cannot resume from {report_path}: invalid benchmark payload")
    saved_catalog = str(payload.get("catalog") or "").strip()
    if not saved_catalog or Path(saved_catalog).expanduser().resolve() != catalog_path:
        raise ValueError(
            f"cannot resume {report_path}: catalog differs from {catalog_path}"
        )
    runtime = payload.get("runtime")
    saved_fingerprint = (
        str(runtime.get("fingerprint") or "").strip()
        if isinstance(runtime, dict)
        else ""
    )
    if saved_fingerprint != runtime_fingerprint:
        raise ValueError(
            f"cannot resume {report_path}: runtime source fingerprint differs"
        )
    if runtime.get("valid") is False:
        raise ValueError(
            f"cannot resume {report_path}: prior batch was invalidated: "
            f"{runtime.get('invalid_reason') or 'unknown reason'}"
        )
    return [item for item in payload["aggregates"] if isinstance(item, dict)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run identical fixed-revision tasks across coding agents.")
    parser.add_argument("--catalog", type=Path, default=ROOT / "evals/repository-tasks/swebench-lite/catalog.json")
    parser.add_argument("--agents", default="minicode,codex,claude,pi")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/agent-benchmark")
    parser.add_argument("--task-limit", type=int, default=0)
    parser.add_argument(
        "--exclude-id",
        action="append",
        default=[],
        help="Skip a task id (repeatable), useful when resuming an interrupted batch.",
    )
    parser.add_argument(
        "--only-id",
        action="append",
        default=[],
        help="Run only the listed task id(s), useful for reproducible retries.",
    )
    parser.add_argument("--seed", type=int, action="append")
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Reuse task/seed runs already checkpointed in the output directory.",
    )
    parser.add_argument("--minicode-model", default="")
    parser.add_argument("--codex-model", default="")
    parser.add_argument("--claude-model", default="")
    parser.add_argument("--pi-model", default="")
    args = parser.parse_args()

    catalog_path = args.catalog.expanduser().resolve()
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    tasks = list(catalog.get("tasks") or [])
    excluded_ids = {value.strip() for value in args.exclude_id if value.strip()}
    only_ids = {value.strip() for value in args.only_id if value.strip()}
    if only_ids:
        tasks = [
            entry for entry in tasks
            if str(entry.get("id") or entry.get("task_id") or "") in only_ids
        ]
    if excluded_ids:
        tasks = [
            entry for entry in tasks
            if str(entry.get("id") or entry.get("task_id") or "") not in excluded_ids
        ]
    if args.task_limit > 0:
        tasks = tasks[: args.task_limit]
    agents = [item.strip() for item in args.agents.split(",") if item.strip()]
    unsupported = sorted(set(agents) - {"minicode", "codex", "claude", "pi"})
    if unsupported:
        parser.error(f"unsupported agents: {', '.join(unsupported)}")
    models = {
        "minicode": args.minicode_model,
        "codex": args.codex_model,
        "claude": args.claude_model,
        "pi": args.pi_model,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    runtime_fingerprint = _runtime_source_fingerprint(ROOT)
    git_head = _git_head(ROOT)
    try:
        resumed_reports = (
            _load_resume_reports(
                args.output,
                catalog_path,
                runtime_fingerprint=runtime_fingerprint,
            )
            if args.resume
            else []
        )
    except ValueError as exc:
        parser.error(str(exc))
    reports_by_task: dict[tuple[str, str], dict[str, Any]] = {
        (str(item.get("agent") or ""), str(item.get("task_id") or "")): item
        for item in resumed_reports
        if str(item.get("agent") or "").strip()
        and str(item.get("task_id") or "").strip()
    }

    for agent in agents:
        if agent == "minicode" and models[agent].strip():
            os.environ["MINICODE_EVAL_MODEL"] = models[agent].strip()
        runner = RepositoryTaskRunner(
            output_root=args.output / agent,
            keep_workspace=args.keep_workspace,
        )
        command = _agent_command(agent, models)
        for entry in tasks:
            if _runtime_source_fingerprint(ROOT) != runtime_fingerprint:
                payload = _benchmark_payload(
                    catalog_path=catalog_path,
                    reports=list(reports_by_task.values()),
                    runtime_fingerprint=runtime_fingerprint,
                    git_head=git_head,
                    valid=False,
                    invalid_reason="runtime source changed between benchmark tasks",
                )
                _write_outputs(args.output, payload)
                print(
                    "benchmark invalidated: runtime source changed between tasks",
                    file=sys.stderr,
                    flush=True,
                )
                return 2
            manifest = catalog_path.parent / str(entry["manifest"])
            task = load_repository_task(manifest)
            if args.seed:
                task = replace(task, seeds=tuple(dict.fromkeys(args.seed)))
            report_key = (agent, task.task_id)
            existing = reports_by_task.get(report_key)
            existing_seeds = {
                int(report.get("seed"))
                for report in ((existing or {}).get("reports") or [])
                if isinstance(report, dict) and report.get("seed") is not None
            }
            if args.resume and set(task.seeds).issubset(existing_seeds):
                print(f"{agent} {task.task_id}: resumed", flush=True)
                continue
            aggregate = runner.run_all_seeds(
                task,
                agent_argv=command,
                manifest_dir=manifest.parent,
                agent_name=agent,
            ).to_dict()
            aggregate["agent"] = agent
            reports_by_task[report_key] = aggregate
            print(f"{agent} {task.task_id}: {aggregate['pass_rate']:.0%}", flush=True)
            source_unchanged = _runtime_source_fingerprint(ROOT) == runtime_fingerprint
            _write_outputs(
                args.output,
                _benchmark_payload(
                    catalog_path=catalog_path,
                    reports=list(reports_by_task.values()),
                    runtime_fingerprint=runtime_fingerprint,
                    git_head=git_head,
                    valid=source_unchanged,
                    invalid_reason=(
                        "runtime source changed while a benchmark task was running"
                        if not source_unchanged
                        else ""
                    ),
                ),
            )
            if not source_unchanged:
                print(
                    "benchmark invalidated: runtime source changed during a task",
                    file=sys.stderr,
                    flush=True,
                )
                return 2

    payload = _benchmark_payload(
        catalog_path=catalog_path,
        reports=list(reports_by_task.values()),
        runtime_fingerprint=runtime_fingerprint,
        git_head=git_head,
    )
    _write_outputs(args.output, payload)
    rows = payload["runs"]

    failed = [row for row in rows if not row["passed"]]
    print(json.dumps({"runs": len(rows), "passed": len(rows) - len(failed), "failed": len(failed)}))
    return 0 if rows and not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
