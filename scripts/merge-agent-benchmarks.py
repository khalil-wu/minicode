from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.evals.benchmark_merge import load_and_merge_benchmarks, write_benchmark_payload


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Merge fixed-task benchmark shards without rerunning completed seeds."
    )
    parser.add_argument("inputs", nargs="+", type=Path, help="benchmark.json shard(s)")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--duplicates",
        choices=("error", "first", "last"),
        default="error",
        help="How to resolve duplicate agent/task/seed runs in input order.",
    )
    args = parser.parse_args()

    try:
        payload = load_and_merge_benchmarks(
            args.inputs,
            duplicate_policy=args.duplicates,
        )
        write_benchmark_payload(payload, args.output)
    except ValueError as exc:
        parser.error(str(exc))

    rows = payload["runs"]
    passed = sum(bool(row.get("passed")) for row in rows)
    print(
        json.dumps(
            {
                "runs": len(rows),
                "passed": passed,
                "failed": len(rows) - passed,
                "output": str(args.output.resolve()),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
