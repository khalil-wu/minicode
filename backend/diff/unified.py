"""Unified text diffs shared by approval previews and committed tool results."""
from __future__ import annotations

import difflib
from collections.abc import Iterator


def split_diff_lines(content: str) -> list[str]:
    # Git patch lines are separated by LF; other Unicode separators are data.
    lines = content.split("\n")
    return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def iter_unified_diff(
    old_content: str,
    new_content: str,
    *,
    fromfile: str,
    tofile: str,
    context_lines: int = 3,
) -> Iterator[str]:
    for line in difflib.unified_diff(
        split_diff_lines(old_content),
        split_diff_lines(new_content),
        fromfile=fromfile,
        tofile=tofile,
        n=context_lines,
    ):
        if line.endswith("\n"):
            yield line
        else:
            yield line + "\n"
            yield "\\ No newline at end of file\n"


def count_unified_diff_changes(patch: str) -> tuple[int, int]:
    additions = 0
    deletions = 0
    in_hunk = False
    for line in patch.split("\n"):
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk:
            if line.startswith("+"):
                additions += 1
            elif line.startswith("-"):
                deletions += 1
    return additions, deletions
