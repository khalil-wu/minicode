"""Unified text diffs shared by approval previews and committed tool results."""
from __future__ import annotations

import difflib
from collections.abc import Iterator

Opcode = tuple[str, int, int, int, int]


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
    old_lines = split_diff_lines(old_content)
    new_lines = split_diff_lines(new_content)
    prefix = 0
    shared_limit = min(len(old_lines), len(new_lines))
    while prefix < shared_limit and old_lines[prefix] == new_lines[prefix]:
        prefix += 1
    if prefix == len(old_lines) == len(new_lines):
        return
    suffix = 0
    while suffix < shared_limit - prefix and old_lines[-suffix - 1] == new_lines[-suffix - 1]:
        suffix += 1
    old_end, new_end = len(old_lines) - suffix, len(new_lines) - suffix
    # These equal regions are already known from the actual bytes. Match only
    # the changed interior: popular-line filtering must not turn a one-line
    # edit into a whole-file rewrite, nor misalign repeated context at a cropped
    # window's boundary. Large shared regions stay linear; the existing matcher
    # and turn-tracker coarse policy still handle actual large rewrites.
    matcher = difflib.SequenceMatcher(None, old_lines[prefix:old_end], new_lines[prefix:new_end])
    opcodes: list[Opcode] = []
    if prefix:
        opcodes.append(("equal", 0, prefix, 0, prefix))
    opcodes.extend((tag, i1 + prefix, i2 + prefix, j1 + prefix, j2 + prefix)
                   for tag, i1, i2, j1, j2 in matcher.get_opcodes())
    if suffix:
        opcodes.append(("equal", old_end, len(old_lines), new_end, len(new_lines)))

    yield f"--- {fromfile}\n"
    yield f"+++ {tofile}\n"
    for group in _group_opcodes(opcodes, context_lines):
        old_range = _format_range(group[0][1], group[-1][2])
        new_range = _format_range(group[0][3], group[-1][4])
        yield f"@@ -{old_range} +{new_range} @@\n"
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                chunks = [(" ", old_lines[i1:i2])]
            elif tag == "replace":
                chunks = [("-", old_lines[i1:i2]), ("+", new_lines[j1:j2])]
            elif tag == "delete":
                chunks = [("-", old_lines[i1:i2])]
            else:
                chunks = [("+", new_lines[j1:j2])]
            for marker, rows in chunks:
                for row in rows:
                    yield marker + row if row.endswith("\n") else marker + row + "\n"
                    if not row.endswith("\n"):
                        yield "\\ No newline at end of file\n"


def _group_opcodes(opcodes: list[Opcode], context: int) -> Iterator[list[Opcode]]:
    # Standard unified-diff grouping: retain context at each outer boundary
    # and split equal runs longer than two context windows between changes.
    if opcodes[0][0] == "equal":
        tag, i1, i2, j1, j2 = opcodes[0]
        opcodes[0] = tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2
    if opcodes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = opcodes[-1]
        opcodes[-1] = tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)
    group: list[Opcode] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal" and i2 - i1 > context * 2:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def _format_range(start: int, stop: int) -> str:
    count = stop - start
    if count == 1:
        return str(start + 1)
    return f"{start if count == 0 else start + 1},{count}"


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
