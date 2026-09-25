"""Run historical repository tasks with a real provider and external oracles.

Credentials use MINICODE_EVAL_API_KEY; no key is written to the task or report.
Each invocation owns new workspaces so previous solutions cannot affect results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "backend/evals/cases"


def run_case(case: str, output: Path, window: int) -> dict:
    case_root = CASES / case
    evidence = output / case
    evidence.mkdir(parents=True)
    workspace = evidence / "workspace"
    env = dict(os.environ, PYTHONUTF8="1")
    with (evidence / "prepare.log").open("w", encoding="utf-8") as log:
        subprocess.run([sys.executable, str(case_root / "prepare.py"), str(workspace)],
                       cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)

    def oracle(label: str, cwd: Path) -> int:
        with (evidence / f"{label}.log").open("w", encoding="utf-8") as log:
            return subprocess.run([sys.executable, str(case_root / "oracle.py"), "-v"],
                                  cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT).returncode

    seed_code = oracle("seed", workspace)
    reference_code = oracle("reference", ROOT)
    if seed_code == 0 or reference_code != 0:
        raise RuntimeError(f"invalid {case} oracle: seed={seed_code}, reference={reference_code}")

    prompt = (case_root / "prompt.md").read_text(encoding="utf-8")
    if case == "real_scheduler":
        prompt += ("\nUse two parallel subagents for independent read-only investigation: "
                   "one for persistence/history ownership, one for timezone/tick behavior. "
                   "Integrate their findings, implement the fixes and verify them yourself.\n")
    (evidence / "prompt.md").write_text(prompt, encoding="utf-8")
    profile = {"llm": {"provider": "custom", "context_window": window},
               "token_budget": {"total": window}}
    if case == "real_compaction":
        profile["agent"] = {"compaction_keep_recent_tokens": 6000}
    env.update(MINICODE_EVAL_WORKSPACE=str(workspace),
               MINICODE_STATE_ROOT=str(evidence / "state"),
               MINICODE_EVAL_TASK_ID=f"final-{case}-{time.time_ns()}",
               MINICODE_EVAL_COMMAND_OUTPUT_DIR=str(evidence / "commands"),
               MINICODE_EVAL_PROFILE_JSON=json.dumps(profile),
               MINICODE_EVAL_MAX_TURN_SECONDS=os.environ.get("MINICODE_EVAL_MAX_TURN_SECONDS", "1800"))
    if os.environ.get("MINICODE_EVAL_CAPTURE_REQUESTS") == "1":
        env["MINICODE_EVAL_REQUEST_OUTPUT_DIR"] = str(evidence / "requests")
    # Approvals and sandbox enforcement remain owned by the production driver.
    started = time.monotonic()
    print(f"{case}: seed fails, reference passes; starting real model", flush=True)
    with (evidence / "trace.jsonl").open("w", encoding="utf-8") as trace, \
            (evidence / "driver.log").open("w", encoding="utf-8") as log:
        result = subprocess.run([sys.executable, str(ROOT / "backend/evals/minicode_driver.py")],
                                input=prompt, text=True, encoding="utf-8", cwd=ROOT,
                                env=env, stdout=trace, stderr=log)
    oracle_code = oracle("result", workspace)
    events = [json.loads(line) for line in (evidence / "trace.jsonl").read_text(encoding="utf-8").splitlines()
              if line.startswith("{")]
    summaries = [event["data"] for event in events if event["type"] == "eval.driver.summary"]
    with (evidence / "changes.diff").open("w", encoding="utf-8") as diff:
        subprocess.run(["git", "diff", "--"], cwd=workspace, stdout=diff, check=True)
    report = {"case": case, "model": env["MINICODE_EVAL_MODEL"],
              "wire_api": env.get("MINICODE_EVAL_WIRE_API", "chat"),
              "context_window": window, "elapsed_seconds": round(time.monotonic() - started, 2),
              "seed_exit": seed_code, "reference_exit": reference_code,
              "driver_exit": result.returncode, "oracle_exit": oracle_code,
              "passed": result.returncode == 0 and oracle_code == 0,
              "summary": summaries[-1] if summaries else None}
    (evidence / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "summary"}), flush=True)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--window", type=int, help="Override the configured context window for a control run.")
    parser.add_argument("--cases", nargs="+", default=["real_scheduler", "real_file_snapshots", "real_compaction"],
                        choices=["real_scheduler", "real_file_snapshots", "real_compaction"])
    args = parser.parse_args()
    # Fail before expensive checkout preparation if provider configuration is absent.
    for name in ("MINICODE_EVAL_API_KEY", "MINICODE_EVAL_BASE_URL", "MINICODE_EVAL_MODEL"):
        if not os.environ.get(name):
            parser.error(f"missing {name}")
    output = args.out.resolve()
    reports = []
    for case in args.cases:
        reports.append(run_case(case, output, args.window or (24000 if case == "real_compaction" else 128000)))
        (output / "summary.json").write_text(json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(report["passed"] for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
