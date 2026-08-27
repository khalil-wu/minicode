"""Parser for the Codex-compatible patch envelope used at the tool boundary.

Accepts the established multi-file patch envelope syntax at the model/tool
boundary. MiniCode owns grammar validation, path resolution, and file mutation
semantics; this module never delegates application to another patch engine.
The envelope is:

    *** Begin Patch
    *** Add File: path/to/new.py
    +line one
    +line two
    *** Update File: path/to/existing.py
    *** Move to: path/to/renamed.py        (optional, Update only)
    @@ optional context header
     unchanged context line
    -removed line
    +added line
    *** Delete File: path/to/gone.py
    *** End Patch

Line prefixes inside a hunk: ``+`` add, ``-`` remove, `` `` (single space)
context, ``@@`` change-context marker. Add-File hunks contain only ``+`` lines.

This module is pure (no I/O, no workspace knowledge). The tool wrapper resolves
paths, enforces permissions, and writes files. Parsing is deliberately strict:
a malformed envelope raises ApplyPatchError with an actionable message so the
model can correct the call without entering a second execution path.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from enum import Enum

BEGIN_PATCH = "*** Begin Patch"
END_PATCH = "*** End Patch"
ADD_FILE = "*** Add File: "
DELETE_FILE = "*** Delete File: "
UPDATE_FILE = "*** Update File: "
MOVE_TO = "*** Move to: "
END_OF_FILE = "*** End of File"
CHANGE_MARKER = "@@"


class ApplyPatchError(ValueError):
    """Raised when a patch envelope is malformed or cannot be applied."""


class ChangeKind(str, Enum):
    ADD = "add"
    DELETE = "delete"
    UPDATE = "update"


@dataclass
class PatchHunk:
    """One contiguous change block within an Update File section.

    ``context`` lines orient the hunk against the current file; ``old_lines`` are
    the lines to match-and-remove (context + removed, in order) and ``new_lines``
    are the lines to write (context + added, in order). We keep both so the
    applier can locate the hunk by its old block and substitute the new block.
    """

    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    is_eof: bool = False
    change_context: str | None = None


@dataclass
class FileChange:
    kind: ChangeKind
    path: str
    move_to: str | None = None
    # Add: new_content is the whole file. Update: hunks. Delete: neither.
    new_content: str | None = None
    hunks: list[PatchHunk] = field(default_factory=list)


def parse_patch(text: str) -> list[FileChange]:
    """Parse a full apply_patch envelope into a list of FileChange.

    Raises ApplyPatchError on any structural problem.
    """
    if not text or not text.strip():
        raise ApplyPatchError("Empty patch.")
    # Normalize line endings; the envelope is line-oriented.
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # Trim a trailing empty element from a final newline so END_PATCH can be last.
    if lines and lines[-1] == "":
        lines.pop()

    if not lines or lines[0].strip() != BEGIN_PATCH:
        raise ApplyPatchError(
            f"Patch must start with '{BEGIN_PATCH}' on the first line."
        )
    if lines[-1].strip() != END_PATCH:
        raise ApplyPatchError(
            f"Patch must end with '{END_PATCH}' on the last line."
        )

    body = lines[1:-1]
    changes: list[FileChange] = []
    i = 0
    n = len(body)
    while i < n:
        line = body[i]
        if line.startswith(ADD_FILE):
            path = line[len(ADD_FILE):].strip()
            if not path:
                raise ApplyPatchError("'Add File' is missing a path.")
            i += 1
            content_lines: list[str] = []
            while i < n and not _is_section_header(body[i]):
                row = body[i]
                if not row.startswith("+"):
                    raise ApplyPatchError(
                        f"Add File '{path}': every line must start with '+', got: {row!r}"
                    )
                content_lines.append(row[1:])
                i += 1
            # Every '+' line is newline-terminated, so an added file always
            # ends with a trailing newline.
            new_content = ("\n".join(content_lines) + "\n") if content_lines else ""
            changes.append(
                FileChange(
                    kind=ChangeKind.ADD,
                    path=path,
                    new_content=new_content,
                )
            )
        elif line.startswith(DELETE_FILE):
            path = line[len(DELETE_FILE):].strip()
            if not path:
                raise ApplyPatchError("'Delete File' is missing a path.")
            changes.append(FileChange(kind=ChangeKind.DELETE, path=path))
            i += 1
        elif line.startswith(UPDATE_FILE):
            path = line[len(UPDATE_FILE):].strip()
            if not path:
                raise ApplyPatchError("'Update File' is missing a path.")
            i += 1
            move_to: str | None = None
            if i < n and body[i].startswith(MOVE_TO):
                move_to = body[i][len(MOVE_TO):].strip() or None
                i += 1
            hunks, i = _parse_update_hunks(body, i, n, path)
            if not hunks:
                raise ApplyPatchError(
                    f"Update File '{path}' has no change hunks."
                )
            changes.append(
                FileChange(
                    kind=ChangeKind.UPDATE,
                    path=path,
                    move_to=move_to,
                    hunks=hunks,
                )
            )
        elif line.strip() == "":
            i += 1  # tolerate blank lines between sections
        else:
            raise ApplyPatchError(
                f"Unexpected line outside a file section: {line!r}. "
                f"Expected one of: '{ADD_FILE.strip()}', '{UPDATE_FILE.strip()}', "
                f"'{DELETE_FILE.strip()}'."
            )

    if not changes:
        raise ApplyPatchError("Patch contains no file changes.")
    return changes


def patch_target_paths(text: str) -> list[str]:
    """Return every source and destination path referenced by a patch."""
    paths: list[str] = []
    for change in parse_patch(text):
        for value in (change.path, change.move_to):
            clean = str(value or "").strip()
            if clean and clean not in paths:
                paths.append(clean)
    return paths


def _is_section_header(line: str) -> bool:
    return (
        line.startswith(ADD_FILE)
        or line.startswith(DELETE_FILE)
        or line.startswith(UPDATE_FILE)
    )


def _parse_update_hunks(
    body: list[str], i: int, n: int, path: str
) -> tuple[list[PatchHunk], int]:
    hunks: list[PatchHunk] = []
    current: PatchHunk | None = None

    def flush() -> None:
        nonlocal current
        if current is not None and (current.old_lines or current.new_lines or current.is_eof):
            hunks.append(current)
        current = None

    while i < n and not _is_section_header(body[i]):
        row = body[i]
        if row.startswith(CHANGE_MARKER):
            # New change block. Text after @@ is an orientation anchor (for
            # example ``@@ class Media:``); the hunk is then searched for after
            # that line. Keeping the anchor materially improves patches against
            # files with repeated short blocks.
            flush()
            current = PatchHunk(change_context=row[len(CHANGE_MARKER):].strip() or None)
            i += 1
            continue
        if row.strip() == END_OF_FILE:
            if current is None:
                current = PatchHunk()
            current.is_eof = True
            i += 1
            continue
        if current is None:
            current = PatchHunk()
        if row.startswith("+"):
            current.new_lines.append(row[1:])
        elif row.startswith("-"):
            current.old_lines.append(row[1:])
        elif row.startswith(" "):
            # Context line belongs to both sides.
            current.old_lines.append(row[1:])
            current.new_lines.append(row[1:])
        elif row == "":
            # A bare empty line is treated as an empty context line.
            current.old_lines.append("")
            current.new_lines.append("")
        else:
            raise ApplyPatchError(
                f"Update File '{path}': invalid hunk line {row!r}. "
                "Lines must start with '+', '-', a space, or '@@'."
            )
        i += 1
    flush()
    return hunks, i


def apply_update_hunks(original: str, hunks: list[PatchHunk], path: str) -> str:
    """Apply Update hunks to original file content, returning the new content.

    Each hunk's ``old_lines`` block is located in the file (in order, no overlap)
    and replaced by its ``new_lines`` block. Location prefers an exact match and
    only then relaxes whitespace/punctuation (see ``_find_block``). Raises
    ApplyPatchError if a hunk's context cannot be found.
    """
    orig_lines = original.split("\n")
    result: list[str] = []
    cursor = 0  # index into orig_lines already consumed into result

    for hunk in hunks:
        if hunk.change_context:
            context_at = _find_block(orig_lines, [hunk.change_context], cursor)
            if context_at < 0:
                raise ApplyPatchError(
                    _missing_context_message(
                        path=path,
                        lines=orig_lines,
                        expected=[hunk.change_context],
                        start=cursor,
                        label="change context",
                    )
                )
            # Match the hunk after the orientation line. Preserve everything
            # through the anchor.
            anchor_end = context_at + 1
            result.extend(orig_lines[cursor:anchor_end])
            cursor = anchor_end

        old_block = hunk.old_lines
        if not old_block:
            # A pure insertion is safe when @@ supplied an orientation anchor;
            # insert immediately after it. Without an anchor, require an explicit
            # EOF marker rather than guessing a location.
            if hunk.change_context:
                result.extend(hunk.new_lines)
                continue
            if not hunk.is_eof:
                raise ApplyPatchError(
                    f"Update File '{path}': a hunk has no context lines to locate it. "
                    "Add surrounding context lines, use '@@ <exact anchor>', or mark it "
                    "'*** End of File' for an append."
                )
            result.extend(orig_lines[cursor:])
            cursor = len(orig_lines)
            result.extend(hunk.new_lines)
            continue
        if hunk.is_eof:
            # Combining context/removal lines with *** End of File is malformed:
            # the EOF intent would otherwise be silently dropped. Reject.
            raise ApplyPatchError(
                f"Update File '{path}': a hunk mixes context/removal lines with "
                "'*** End of File'. Drop the context or split into separate hunks."
            )
        match_at = _find_block(orig_lines, old_block, cursor)
        if match_at < 0:
            raise ApplyPatchError(
                _missing_context_message(
                    path=path,
                    lines=orig_lines,
                    expected=old_block,
                    start=cursor,
                    label="hunk",
                )
            )
        # Emit untouched lines before the match, then the replacement.
        result.extend(orig_lines[cursor:match_at])
        result.extend(hunk.new_lines)
        cursor = match_at + len(old_block)

    result.extend(orig_lines[cursor:])
    return "\n".join(result)


def _find_block(haystack: list[str], block: list[str], start: int) -> int:
    """Return the index where ``block`` matches in ``haystack`` at/after ``start``.

    Match with a descending strictness ladder: exact, trailing-whitespace-
    insensitive, fully stripped, then common Unicode punctuation/space
    normalization. The search remains ordered and bounded by ``start``; this is
    context location, not approximate edit generation.
    """
    if not block:
        return start
    blen = len(block)
    if blen > len(haystack):
        return -1
    last = len(haystack) - blen

    def seek(normalize) -> int:
        normalized_block = [normalize(line) for line in block]
        for idx in range(max(0, start), last + 1):
            if [normalize(line) for line in haystack[idx:idx + blen]] == normalized_block:
                return idx
        return -1

    for normalizer in (
        lambda value: value,
        lambda value: value.rstrip(),
        lambda value: value.strip(),
        _normalize_patch_context,
    ):
        found = seek(normalizer)
        if found >= 0:
            return found
    return -1


_PATCH_PUNCTUATION_MAP = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "―": "-",
        "−": "-",
        "‘": "'",
        "’": "'",
        "‚": "'",
        "‛": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        "‟": '"',
        "\u00a0": " ",
        "\u2002": " ",
        "\u2003": " ",
        "\u2004": " ",
        "\u2005": " ",
        "\u2006": " ",
        "\u2007": " ",
        "\u2008": " ",
        "\u2009": " ",
        "\u200a": " ",
        "\u202f": " ",
        "\u205f": " ",
        "\u3000": " ",
    }
)


def _normalize_patch_context(value: str) -> str:
    return value.strip().translate(_PATCH_PUNCTUATION_MAP)


def _missing_context_message(
    *,
    path: str,
    lines: list[str],
    expected: list[str],
    start: int,
    label: str,
) -> str:
    """Return a compact, actionable mismatch with the closest real file lines."""

    preview = expected[0] if expected else ""
    message = (
        f"Update File '{path}': could not locate context for a {label} "
        f"(starting near {preview!r}). The file may have changed."
    )
    if not lines or not expected:
        return f"{message} Read it again and regenerate the patch."

    anchor = next((line for line in expected if line.strip()), expected[0])
    anchor_normalized = _normalize_patch_context(anchor)
    candidate_index = -1
    for index in range(max(0, start), len(lines)):
        if _normalize_patch_context(lines[index]) == anchor_normalized:
            candidate_index = index
            break

    if candidate_index < 0 and anchor_normalized:
        best_ratio = 0.0
        # Diagnostics only: cap work on generated or vendored mega-files.
        search_end = min(len(lines), max(0, start) + 20_000)
        for index in range(max(0, start), search_end):
            candidate = _normalize_patch_context(lines[index])
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, anchor_normalized, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                candidate_index = index
        if best_ratio < 0.35:
            candidate_index = -1

    if candidate_index < 0:
        return f"{message} Read it again and regenerate a smaller patch hunk."

    excerpt_start = max(0, candidate_index - 1)
    excerpt_size = min(12, max(4, len(expected) + 2))
    excerpt_end = min(len(lines), excerpt_start + excerpt_size)
    excerpt = "\n".join(lines[excerpt_start:excerpt_end])
    return (
        f"{message}\n"
        f"Closest current file excerpt (lines {excerpt_start + 1}-{excerpt_end}):\n"
        f"{excerpt}\n"
        "Regenerate a minimal hunk from this exact content; do not repeat unchanged blocks."
    )
