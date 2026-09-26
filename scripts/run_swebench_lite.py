"""Generate one MiniCode patch per official instance, then use SWE-bench's grader.

Generation happens inside each official Linux instance image. Only MiniCode's
runtime source and dependency volume are mounted; the dataset, gold patches,
test patches and Docker socket are NOT available to the model process.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def docker(*args: str, check: bool = True, **kwargs):
    return subprocess.run(["docker", *args], check=check, **kwargs)


def run_instance(row: dict, args, predictions: Path) -> dict:
    instance = row["instance_id"]
    evidence = predictions.parent / instance
    evidence.mkdir()
    image = f"swebench/sweb.eval.x86_64.{instance.lower().replace('__', '_1776_')}:latest"
    container = f"minicode-{args.run_id}-{instance}".lower()
    present = docker("image", "inspect", image, check=False, capture_output=True).returncode == 0
    started = time.monotonic()
    container_started = False
    result = {"instance_id": instance, "repo": row["repo"], "base_commit": row["base_commit"],
              "model": os.environ["MINICODE_EVAL_MODEL"], "image": image,
              "status": "preparing", "resolved": None}
    try:
        with (evidence / "prepare.log").open("w", encoding="utf-8") as log:
            if not present:
                docker("pull", image, stdout=log, stderr=subprocess.STDOUT)
            digest = docker("image", "inspect", image, "--format", "{{json .RepoDigests}}", capture_output=True, text=True)
            result["image_digests"] = json.loads(digest.stdout)
            docker("run", "-d", "--name", container,
                   "-v", f"{args.source}:/minicode:ro",
                   "-v", f"{args.runtime_volume}:/opt/minicode-runtime:ro", image,
                   "tail", "-f", "/dev/null", stdout=log, stderr=subprocess.STDOUT)
            container_started = True
            result["checkout_head"] = docker("exec", container, "git", "-C", "/testbed", "rev-parse", "HEAD",
                                               capture_output=True, text=True).stdout.strip()
            docker("exec", container, "/opt/minicode-runtime/venv/bin/python", "-c",
                   "from backend.agent.query_engine import QueryEngine; print('MiniCode import ready')",
                   stdout=log, stderr=subprocess.STDOUT)
        prompt = (
            f"Fix the following issue in {row['repo']}. The working directory is /testbed.\n"
            "This is the actual repository checkout with its project dependencies installed. "
            "The testbed conda environment is on PATH; run project tests in that environment. "
            "Inspect relevant source, implement a complete fix, and verify it. "
            "Finish related changes before running their test batch. Do not weaken tests or commit changes. "
            "Work from the issue and local repository; do not search the web for a solution or benchmark patches.\n\n"
            + row["problem_statement"]
        )
        (evidence / "prompt.md").write_text(prompt, encoding="utf-8")
        env = dict(os.environ)
        env.update(
            MINICODE_EVAL_WORKSPACE="/testbed", MINICODE_STATE_ROOT="/tmp/minicode-state",
            MINICODE_EVAL_TASK_ID=instance, MINICODE_EVAL_WIRE_API="responses",
            MINICODE_EVAL_PROXY_MODE="direct",
            MINICODE_EVAL_MAX_TOKENS="16384",
            MINICODE_EVAL_BASE_URL=env["MINICODE_EVAL_BASE_URL"].replace("://127.0.0.1:", "://host.docker.internal:"),
            MINICODE_EVAL_PROFILE_JSON=json.dumps({"llm": {"provider": "custom", "context_window": 272000},
                                                  "token_budget": {"total": 272000}}),
            MINICODE_EVAL_MAX_TURN_SECONDS=str(args.turn_seconds),
            MINICODE_EVAL_EXTERNAL_SANDBOX="1", MINICODE_EVAL_PROTECT_EXISTING_TESTS="0",
            MINICODE_EVAL_COMMAND_OUTPUT_DIR="/tmp/minicode-command-output",
            PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
        )
        forwarded = [name for name in env if name.startswith("MINICODE_EVAL_")]
        forwarded += ["MINICODE_STATE_ROOT", "PYTHONUTF8", "PYTHONDONTWRITEBYTECODE"]
        command = ["exec", "-i", "--workdir", "/testbed"]
        for name in forwarded:
            command += ["--env", name]
        command += ["--env", "PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/bin:/usr/bin:/bin",
                    "--env", "CONDA_DEFAULT_ENV=testbed", "--env", "CONDA_PREFIX=/opt/miniconda3/envs/testbed",
                    container, "/opt/minicode-runtime/venv/bin/python", "/minicode/backend/evals/minicode_driver.py"]
        print(f"{instance}: generating one patch", flush=True)
        with (evidence / "trace.jsonl").open("w", encoding="utf-8") as trace, \
                (evidence / "driver.log").open("w", encoding="utf-8") as log:
            generation = docker(*command, env=env, input=prompt, text=True, encoding="utf-8",
                                stdout=trace, stderr=log, check=False)
        result["driver_exit"] = generation.returncode
        docker("exec", container, "git", "-C", "/testbed", "add", "-N", ".", capture_output=True)
        patch = docker("exec", container, "git", "-C", "/testbed", "diff", "--binary", result["checkout_head"],
                       capture_output=True, text=True, encoding="utf-8").stdout
        (evidence / "model.patch").write_text(patch, encoding="utf-8")
        for line in (evidence / "trace.jsonl").read_text(encoding="utf-8").splitlines():
            if line.startswith("{"):
                event = json.loads(line)
                if event.get("type") == "eval.driver.summary":
                    result["summary"] = event["data"]
        docker("cp", f"{container}:/tmp/minicode-command-output", str(evidence / "commands"),
               check=False, capture_output=True)
        with predictions.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"instance_id": instance, "model_name_or_path": "minicode-gpt-6-luna",
                                  "model_patch": patch}, ensure_ascii=False) + "\n")
        if "summary" not in result:
            result["status"] = "generation_error"
            return result
        if not patch.strip():
            result.update(status="empty_patch", resolved=False)
            return result
        print(f"{instance}: official grading", flush=True)
        with (evidence / "evaluation.log").open("w", encoding="utf-8") as log:
            evaluation = docker(
                "run", "--rm", "-v", "/var/run/docker.sock:/var/run/docker.sock",
                "-v", f"{args.evidence}:/benchmark:ro", "-v", f"{evidence}:/evidence",
                "minicode-swebench-tools:4.1.0", "python", "-m", "swebench.harness.run_evaluation",
                "--dataset_name", "/benchmark/official-test.json",
                "--predictions_path", f"/benchmark/runs/{args.run_id}/predictions.jsonl",
                "--instance_ids", instance, "--run_id", args.run_id, "--max_workers", "1",
                "--timeout", "1200", "--cache_level", "instance", "--clean", "false",
                "--report_dir", "/evidence/reports", stdout=log, stderr=subprocess.STDOUT, check=False,
            )
        result["evaluator_exit"] = evaluation.returncode
        reports = list(evidence.glob(f"logs/run_evaluation/{args.run_id}/*/{instance}/report.json"))
        if reports:
            official = json.loads(reports[0].read_text(encoding="utf-8"))[instance]
            result.update(status="graded", resolved=official["resolved"], official=official)
        else:
            result["status"] = "evaluation_error"
        return result
    except subprocess.CalledProcessError as exc:
        result.update(status="environment_error", command_exit=exc.returncode)
        return result
    finally:
        if container_started:
            docker("rm", "-f", container, check=False, capture_output=True)
        if not present:
            docker("image", "rm", image, check=False, capture_output=True)
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        (evidence / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in result.items() if key not in {"summary", "official"}}), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--runtime-volume", default="minicode-swebench-runtime-20260925")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--turn-seconds", type=int, default=1200)
    args = parser.parse_args()
    args.evidence = args.evidence.resolve()
    args.source = args.source.resolve()
    rows = {row["instance_id"]: row for row in json.loads((args.evidence / "official-test.json").read_text(encoding="utf-8"))}
    ids = json.loads((args.evidence / "selection.json").read_text(encoding="utf-8"))["instance_ids"]
    run_dir = args.evidence / "runs" / args.run_id
    run_dir.mkdir(parents=True)
    predictions = run_dir / "predictions.jsonl"
    results = []
    for instance in ids:
        results.append(run_instance(rows[instance], args, predictions))
        (run_dir / "summary.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if all(result["status"] in {"graded", "empty_patch"} for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
