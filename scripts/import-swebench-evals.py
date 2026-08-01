from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.evals.repository_tasks import portable_pytest_selectors


DEFAULT_DATASET = ROOT / "artifacts" / "swebench-lite.json"
DEFAULT_OUTPUT = ROOT / "evals" / "repository-tasks" / "swebench-lite"
SELECTED_REPOSITORIES = {
    "pytest-dev/pytest": 17,
    "pallets/flask": 3,
}
SEEDS = [11, 29, 47]


def _decode_test_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    decoded = json.loads(str(value or "[]"))
    if not isinstance(decoded, list):
        raise ValueError("SWE-bench test list is not an array")
    return [str(item) for item in decoded]


def _patched_files(patch: str) -> list[str]:
    return sorted(set(re.findall(r"^diff --git a/(.+?) b/", patch, flags=re.MULTILINE)))


def _manifest(record: dict[str, object]) -> dict[str, object]:
    repo = str(record["repo"])
    instance_id = str(record["instance_id"])
    fail_to_pass = portable_pytest_selectors(
        _decode_test_list(record.get("FAIL_TO_PASS"))
    )
    pass_to_pass = portable_pytest_selectors(
        _decode_test_list(record.get("PASS_TO_PASS")),
        limit=20,
    )
    if not fail_to_pass:
        raise ValueError(f"{instance_id} has no FAIL_TO_PASS tests")
    targeted = ["python", "-m", "pytest", "-q", *fail_to_pass]
    regression = ["python", "-m", "pytest", "-q", *pass_to_pass]
    judges: list[dict[str, object]] = [
        {
            "name": "SWE-bench FAIL_TO_PASS",
            "argv": targeted,
            "timeout_seconds": 900,
        }
    ]
    if pass_to_pass:
        judges.append(
            {
                "name": "SWE-bench PASS_TO_PASS regression sample",
                "argv": regression,
                "timeout_seconds": 1200,
            }
        )
    verify_command = subprocess.list2cmdline(targeted)
    return {
        "id": instance_id.replace("__", "-").lower(),
        "title": f"SWE-bench Lite {instance_id}",
        "prompt": str(record["problem_statement"]).strip(),
        "source": {
            "git": f"https://github.com/{repo}.git",
            "revision": str(record["base_commit"]),
        },
        "baseline_patch": str(record["test_patch"]),
        "env": {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        "setup": [
            {
                "name": "install pinned repository",
                "argv": ["python", "-m", "pip", "install", "-e", "."],
                "timeout_seconds": 1200,
            }
        ],
        "baseline": [
            {
                "name": "prove SWE-bench regression",
                "argv": targeted,
                "timeout_seconds": 900,
            }
        ],
        "judges": judges,
        "forbidden_changes": _patched_files(str(record["test_patch"])),
        "agent_verify_command": verify_command,
        "agent_verify_timeout_seconds": 900,
        "agent_timeout_seconds": 2400,
        "seeds": SEEDS,
        "minimum_pass_rate": 2 / 3,
        "tags": ["swe-bench-lite", "real-repository", repo, "fixed-revision"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import a fixed SWE-bench Lite evaluation catalog.")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    records = json.loads(args.dataset.read_text(encoding="utf-8"))
    selected: list[dict[str, object]] = []
    for repo, count in SELECTED_REPOSITORIES.items():
        matches = [record for record in records if record.get("repo") == repo]
        if len(matches) < count:
            raise ValueError(f"dataset only contains {len(matches)} records for {repo}")
        selected.extend(matches[:count])

    args.output.mkdir(parents=True, exist_ok=True)
    catalog: list[dict[str, object]] = []
    for record in selected:
        manifest = _manifest(record)
        path = args.output / f"{manifest['id']}.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        catalog.append(
            {
                "id": manifest["id"],
                "manifest": path.name,
                "repository": record["repo"],
                "revision": record["base_commit"],
            }
        )
    (args.output / "catalog.json").write_text(
        json.dumps(
            {
                "dataset": "princeton-nlp/SWE-bench Lite",
                "task_count": len(catalog),
                "seeds": SEEDS,
                "tasks": catalog,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(catalog)} fixed-version tasks to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
