"""Codex-compatible apply_patch envelope parser and applier.

Mirrors OpenAI Codex's `apply-patch` crate format so models trained on Codex's
patch grammar work unchanged. The envelope is:

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
model can correct the call, matching Codex's parser behavior.
"""
from __future__ import annotations

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
            changes.append(
                FileChange(
                    kind=ChangeKind.ADD,
                    path=path,
                    new_content="\n".join(content_lines),
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
            # New change block. The text after @@ is an orientation hint only.
            flush()
            current = PatchHunk()
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
    and replaced by its ``new_lines`` block. Locating is whitespace-exact, like
    Codex. Raises ApplyPatchError if a hunk's context cannot be found.
    """
    orig_lines = original.split("\n")
    result: list[str] = []
    cursor = 0  # index into orig_lines already consumed into result

    for hunk in hunks:
        old_block = hunk.old_lines
        if not old_block:
            # No context lines: only valid as an explicit end-of-file append.
            # A context-less insertion elsewhere is ambiguous — Codex requires
            # context to locate a hunk. Reject rather than silently inserting at
            # the cursor (which, for a leading hunk, means the top of the file).
            if not hunk.is_eof:
                raise ApplyPatchError(
                    f"Update File '{path}': a hunk has no context lines to locate it. "
                    "Add surrounding context lines, or mark it '*** End of File' for an append."
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
            preview = old_block[0] if old_block else ""
            raise ApplyPatchError(
                f"Update File '{path}': could not locate context for a hunk "
                f"(starting near {preview!r}). The file may have changed; "
                "read it again and regenerate the patch."
            )
        # Emit untouched lines before the match, then the replacement.
        result.extend(orig_lines[cursor:match_at])
        result.extend(hunk.new_lines)
        cursor = match_at + len(old_block)

    result.extend(orig_lines[cursor:])
    return "\n".join(result)


def _find_block(haystack: list[str], block: list[str], start: int) -> int:
    """Return the index where ``block`` matches in ``haystack`` at/after ``start``.

    Whitespace-exact, like Codex: a context mismatch returns -1 (and the caller
    raises ApplyPatchError) so the model re-reads and regenerates, rather than
    silently applying the hunk at the wrong location. No fuzzy/rstrip fallback.
    """
    if not block:
        return start
    blen = len(block)
    for idx in range(start, len(haystack) - blen + 1):
        if haystack[idx:idx + blen] == block:
            return idx
    return -1
