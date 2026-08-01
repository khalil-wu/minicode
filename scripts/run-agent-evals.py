from __future__ import annotations

import argparse
from dataclasses import replace
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.evals.repository_tasks import RepositoryTaskRunner, load_repository_task


def _resolve_agent_argv(argv: list[str], *, launch_cwd: Path) -> list[str]:
    """Keep driver file arguments valid after the evaluator changes cwd."""

    resolved: list[str] = []
    for argument in argv:
        candidate = Path(argument).expanduser()
        if not candidate.is_absolute():
            candidate = launch_cwd / candidate
        resolved.append(str(candidate.resolve()) if candidate.is_file() else argument)
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a coding agent against mechanical repository-task judges.")
    parser.add_argument("manifest", type=Path, nargs="+", help="One or more repository task JSON manifests")
    parser.add_argument("--agent-command", nargs="+", required=True, help="Agent driver argv; the prompt is sent on stdin")
    parser.add_argument("--agent-name", default="", help="Stable agent label stored with every report")
    parser.add_argument("--output", type=Path, default=Path("artifacts/agent-evals"))
    parser.add_argument("--keep-workspace", action="store_true")
    parser.add_argument(
        "--seed",
        type=int,
        action="append",
        help="Run only this sampling seed; repeat to select multiple seeds.",
    )
    args = parser.parse_args()
    agent_argv = _resolve_agent_argv(args.agent_command, launch_cwd=Path.cwd())

    aggregates = []
    for manifest in args.manifest:
        task = load_repository_task(manifest)
        if args.seed:
            task = replace(task, seeds=tuple(dict.fromkeys(args.seed)))
        aggregate = RepositoryTaskRunner(output_root=args.output, keep_workspace=args.keep_workspace).run_all_seeds(
            task,
            agent_argv=agent_argv,
            manifest_dir=manifest.parent,
            agent_name=args.agent_name,
        )
        aggregates.append(aggregate.to_dict())
        print(
            f"{task.task_id}: {'PASS' if aggregate.passed else 'FAIL'} "
            f"({aggregate.pass_rate:.0%}, required {aggregate.minimum_pass_rate:.0%})"
        )
        for report in aggregate.reports:
            print(f"  seed {report.seed}: {'PASS' if report.passed else 'FAIL'}")
            for judge in report.judges:
                print(f"    {'PASS' if judge.passed else 'FAIL'} {judge.name}: {judge.detail}")
            if report.infrastructure_error:
                print(f"    INFRA {report.infrastructure_error}")
    print(
        json.dumps(
            {
                "passed": sum(item["passed"] for item in aggregates),
                "total": len(aggregates),
            },
            ensure_ascii=False,
        )
    )
    return 0 if aggregates and all(item["passed"] for item in aggregates) else 1


if __name__ == "__main__":
    sys.exit(main())
