"""Check that no tool name is registered in multiple files under backend/tools/.

Scans for class-level `name = "..."` assignments in BaseTool subclasses.
If the same tool name appears as a class attribute in more than one .py file,
it is reported as a duplicate registration candidate.

Run in CI or pre-commit. Exits non-zero on duplicates.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "backend" / "tools"

# Match class-level tool name assignments:  name = "tool_name"
_NAME_ASSIGN_RE = re.compile(
    r'^\s+name\s*=\s*["\']([^"\']+)["\']',
    re.MULTILINE,
)


def scan_tool_names() -> dict[str, list[str]]:
    """Return {tool_name: [relative file paths that define it]}."""
    locations: dict[str, list[str]] = defaultdict(list)

    for py_file in sorted(TOOLS_DIR.glob("*.py")):
        if py_file.name in ("__init__.py", "base.py", "registry.py"):
            continue
        source = py_file.read_text(encoding="utf-8")
        for match in _NAME_ASSIGN_RE.finditer(source):
            tool_name = match.group(1)
            rel_path = str(py_file.relative_to(ROOT))
            locations[tool_name].append(rel_path)

    return dict(locations)


def main() -> int:
    locations = scan_tool_names()
    duplicates = {
        name: files for name, files in locations.items() if len(files) > 1
    }

    if duplicates:
        print("[FAIL] Duplicate tool name registrations detected:")
        for name in sorted(duplicates):
            print(f"  '{name}' defined in:")
            for f in duplicates[name]:
                print(f"    - {f}")
        return 1

    total = len(locations)
    print(f"[OK] {total} tool names registered — no duplicates across files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
