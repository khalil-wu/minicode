"""Scan src.v2/ and backend/ for source files exceeding 50 KB.

Reports candidate files that should be considered for splitting to keep
modules focused and reviewable.

Run in CI as a soft warning (exits non-zero so it shows up in the pipeline).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = [
    ROOT / "frontend" / "src.v2",
    ROOT / "backend",
]

THRESHOLD_BYTES = 50 * 1024  # 50 KB

# File extensions to consider
SOURCE_EXTENSIONS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".css",
    ".scss",
}

# Directories to skip
SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".next",
    ".git",
}


def human_size(nbytes: int) -> str:
    if nbytes >= 1024 * 1024:
        return f"{nbytes / (1024 * 1024):.1f} MB"
    return f"{nbytes / 1024:.1f} KB"


def scan_large_files() -> list[tuple[str, int]]:
    """Return [(relative_path, size_bytes)] for files over threshold."""
    results: list[tuple[str, str, int]] = []

    for scan_dir in SCAN_DIRS:
        if not scan_dir.is_dir():
            continue
        for path in sorted(scan_dir.rglob("*")):
            if not path.is_file():
                continue
            if path.suffix not in SOURCE_EXTENSIONS:
                continue
            if any(part in SKIP_DIRS for part in path.parts):
                continue

            size = path.stat().st_size
            if size > THRESHOLD_BYTES:
                rel = str(path.relative_to(ROOT))
                results.append((rel, size))

    results.sort(key=lambda x: x[1], reverse=True)
    return results


def main() -> int:
    large_files = scan_large_files()

    if not large_files:
        print(f"[OK] No source files exceed {human_size(THRESHOLD_BYTES)}")
        return 0

    print(f"[WARN] {len(large_files)} source file(s) exceed {human_size(THRESHOLD_BYTES)} — consider splitting:")
    print()
    for rel_path, size in large_files:
        print(f"  {human_size(size):>8}  {rel_path}")
    print()
    return 1


if __name__ == "__main__":
    sys.exit(main())
