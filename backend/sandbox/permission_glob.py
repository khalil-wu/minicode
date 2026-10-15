"""Filesystem deny globs using Source GlobBuilder's path separator rules."""
from __future__ import annotations

import re


def _class_regex(content: str, negated: bool) -> str:
    ranges: list[tuple[str, str]] = []
    in_range = False
    for char in content:
        if char == "-" and not ranges:
            ranges.append(("-", "-"))
        elif char == "-" and not in_range:
            in_range = True
        elif in_range:
            start = ranges[-1][0]
            if char < start:
                raise ValueError("invalid permission glob range")
            ranges[-1] = (start, char)
            in_range = False
        else:
            ranges.append((char, char))
    if in_range:
        ranges.append(("-", "-"))
    expression = "".join(re.escape(start) if start == end else re.escape(start) + "-" + re.escape(end) for start, end in ranges)
    return "[" + ("^" if negated else "") + expression + "]"


def permission_glob_matches(pattern: str, path: str, *, windows: bool) -> bool:
    if windows:
        pattern = pattern.replace("\\", "/")
        path = path.replace("\\", "/")
    index = 0

    def sequence(in_alternative: bool = False) -> str:
        nonlocal index
        parts: list[str] = []
        while index < len(pattern):
            char = pattern[index]
            if in_alternative and char in {",", "}"}:
                break
            if char == "*":
                start = index
                while index < len(pattern) and pattern[index] == "*":
                    index += 1
                at_branch_start = not parts
                at_branch_end = index == len(pattern) or (in_alternative and pattern[index] in {",", "}"})
                recursive = index - start == 2 and (at_branch_start or pattern[start - 1] == "/") and (at_branch_end or pattern[index] == "/")
                if recursive and not at_branch_end:
                    parts.append("(?:[^/]+/)*")
                    index += 1
                else:
                    parts.append(".*" if recursive else "[^/]*")
                continue
            if char == "?":
                parts.append("[^/]")
            elif char == "[":
                end = index + 1
                if end < len(pattern) and pattern[end] in {"!", "^"}:
                    end += 1
                if end < len(pattern) and pattern[end] == "]":
                    end += 1
                end = pattern.find("]", end)
                if end < 0:
                    # Source allow_unclosed_class(true) treats this literally.
                    parts.append(r"\[")
                else:
                    character_class = pattern[index:end + 1]
                    negated = character_class[1:2] in {"!", "^"}
                    content = character_class[2:-1] if negated else character_class[1:-1]
                    parts.append(_class_regex(content, negated))
                    index = end
            elif char == "{":
                index += 1
                alternatives = [sequence(True)]
                while index < len(pattern) and pattern[index] == ",":
                    index += 1
                    alternatives.append(sequence(True))
                if index == len(pattern) or pattern[index] != "}":
                    raise ValueError("unclosed permission glob alternative")
                alternatives = [alternative for alternative in alternatives if alternative]
                parts.append("(?:" + "|".join(alternatives) + ")" if alternatives else "")
            elif char == "}" and not in_alternative:
                raise ValueError("unopened permission glob alternative")
            elif char == "\\" and not windows:
                index += 1
                if index == len(pattern):
                    raise ValueError("dangling permission glob escape")
                parts.append(re.escape(pattern[index]))
            else:
                parts.append(re.escape(char))
            index += 1
        return "".join(parts)

    expression = sequence()
    return re.fullmatch(expression, path, flags=(re.IGNORECASE | re.ASCII) if windows else 0) is not None
