"""Path glob matching shared by workspace search and agent search tools."""

from __future__ import annotations

import re
from typing import Callable

from pathspec.gitignore import GitIgnoreSpec


def compile_glob_filter(pattern: str) -> Callable[[str], bool]:
    """Compile a path glob with ripgrep-compatible alternatives and literals."""
    def expand(value: str) -> list[str]:
        group = re.search(r"(?<!\\)\{([^{}]*)\}", value)
        if group is None:
            return [value]
        return [
            expanded
            for alternative in group[1].split(",")
            for expanded in expand(value[:group.start()] + alternative + value[group.end():])
        ]

    # Gitignore treats a leading # and trailing spaces as syntax; rg globs
    # treat those characters as part of the requested filename.
    escaped = "\\" + pattern if pattern.startswith("#") else pattern
    trailing = len(escaped) - len(escaped.rstrip(" "))
    if trailing:
        escaped = escaped[:-trailing] + "\\ " * trailing
    spec = GitIgnoreSpec.from_lines(expand(escaped))
    if pattern.startswith("!"):
        return lambda relative: spec.check_file(relative).include is not False
    return spec.match_file
