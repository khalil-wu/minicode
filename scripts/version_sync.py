from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not match:
        raise RuntimeError("[project].version is missing from pyproject.toml")
    return match.group(1)


def update_json(path: Path, version: str, *, write: bool) -> bool:
    data = json.loads(path.read_text(encoding="utf-8"))
    changed = data.get("version") != version
    data["version"] = version
    root_package = data.get("packages", {}).get("")
    if isinstance(root_package, dict):
        changed = root_package.get("version") != version or changed
        root_package["version"] = version
    if changed and write:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync package versions from pyproject.toml")
    parser.add_argument("--check", action="store_true", help="fail instead of writing when versions drift")
    args = parser.parse_args()
    version = project_version()
    targets = [
        ROOT / "frontend" / "package.json",
        ROOT / "frontend" / "package-lock.json",
        ROOT / "desktop" / "package.json",
        ROOT / "desktop" / "package-lock.json",
    ]
    drifted = [path for path in targets if update_json(path, version, write=not args.check)]
    if args.check and drifted:
        for path in drifted:
            print(f"version drift: {path.relative_to(ROOT)} != {version}", file=sys.stderr)
        return 1
    if drifted:
        print(f"synced {version}: " + ", ".join(str(path.relative_to(ROOT)) for path in drifted))
    else:
        print(f"versions aligned at {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
