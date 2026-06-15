"""Verify harness submodules import shared constants from _common.py.

Checks:
  1. WEB_SEARCH_TOOL_NAMES / WEB_FETCH_TOOL_NAMES / WEB_TOOL_NAMES are only
     *defined* in _common.py — other modules must import them.
  2. _text_arg() is only *defined* in _common.py.

Run in CI or pre-commit. Exits non-zero on violations.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS_DIR = ROOT / "backend" / "agent" / "harness"

# Constants that must only be defined in _common.py
SHARED_CONSTANTS = {
    "WEB_SEARCH_TOOL_NAMES",
    "WEB_FETCH_TOOL_NAMES",
    "WEB_TOOL_NAMES",
}

# Helpers that must only be defined in _common.py
SHARED_HELPERS = {
    "_text_arg",
}

# Regex for a top-level assignment (definition), not an import
_ASSIGN_RE = re.compile(
    r"^(?:WEB_SEARCH_TOOL_NAMES|WEB_FETCH_TOOL_NAMES|WEB_TOOL_NAMES)\s*=",
    re.MULTILINE,
)
_DEF_HELPER_RE = re.compile(
    r"^def\s+_text_arg\s*\(",
    re.MULTILINE,
)


def check_file(path: Path) -> list[str]:
    """Return a list of violation messages for one file."""
    violations: list[str] = []
    source = path.read_text(encoding="utf-8")
    rel = path.relative_to(ROOT)

    if _ASSIGN_RE.search(source):
        for name in SHARED_CONSTANTS:
            pattern = re.compile(rf"^{re.escape(name)}\s*=", re.MULTILINE)
            if pattern.search(source):
                violations.append(
                    f"  {rel}: locally defines {name} — import from _common.py instead"
                )

    if _DEF_HELPER_RE.search(source):
        violations.append(
            f"  {rel}: locally defines _text_arg() — import from _common.py instead"
        )

    return violations


def main() -> int:
    violations: list[str] = []

    for py_file in sorted(HARNESS_DIR.glob("*.py")):
        # Skip _common.py itself — it is the canonical source
        if py_file.name == "_common.py":
            continue
        # Skip __init__.py (re-exports are fine)
        if py_file.name == "__init__.py":
            continue
        violations.extend(check_file(py_file))

    if violations:
        print("[FAIL] Harness import hygiene violations:")
        for v in violations:
            print(v)
        return 1

    print("[OK] All harness submodules import shared constants from _common.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
