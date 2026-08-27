"""MiniCode turn-owned aggregate diff tracking.

Native implementation for turn-level diff tracking. One tracker belongs to
one turn, consumes only exact committed text mutations, keeps the first baseline
plus the latest content, caches per-file rendering,
and never rereads the workspace.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
from dataclasses import dataclass
from pathlib import PurePosixPath


ZERO_OID = "0000000000000000000000000000000000000000"
DEV_NULL = "/dev/null"
REGULAR_FILE_MODE = "100644"
# Codex uses a 100 ms line-diff timeout. Python's stdlib matcher has no timeout,
# so use a bounded exact-content coarse fallback for pathological rewrites.
DIFF_COARSE_LINE_THRESHOLD = 20_000
DIFF_COARSE_CHAR_THRESHOLD = 2_000_000


def _normalized_path(value: str) -> str:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw:
        return ""
    normalized = PurePosixPath(raw).as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _git_blob_oid(content: str) -> str:
    payload = content.encode("utf-8")
    header = f"blob {len(payload)}\0".encode("ascii")
    return hashlib.sha1(header + payload).hexdigest()


@dataclass(frozen=True, slots=True)
class _TrackedContent:
    content: str
    revision: int


@dataclass(frozen=True, slots=True)
class _DiffCacheKey:
    left_path: str
    left_revision: int | None
    right_path: str
    right_revision: int | None


@dataclass(frozen=True, slots=True)
class TurnDiffSnapshot:
    revision: int
    unified_diff: str | None


class TurnDiffTracker:
    """Track the net exact text diff for one agent turn."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._valid = True
        self._baseline_by_path: dict[str, _TrackedContent] = {}
        self._current_by_path: dict[str, _TrackedContent] = {}
        self._origin_by_current_path: dict[str, str] = {}
        self._next_content_revision = 0
        self._revision = 0
        self._rendered_diffs: dict[_DiffCacheKey, str | None] = {}
        self._unified_diff: str | None = None
        self._rendered_diff_count = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def rendered_diff_count(self) -> int:
        return self._rendered_diff_count

    def has_unified_diff(self) -> bool:
        return self._unified_diff is not None

    def get_unified_diff(self) -> str | None:
        return self._unified_diff

    def invalidate(self) -> None:
        if not self._valid:
            return
        self._valid = False
        self._rendered_diffs.clear()
        self._unified_diff = None
        self._revision += 1

    def track_change(
        self,
        *,
        old_path: str,
        new_path: str,
        old_content: str | None,
        new_content: str | None,
        overwritten_new_content: str | None = None,
    ) -> None:
        """Apply one exact committed mutation.

        ``None`` means the path did not exist on that side.  The optional
        overwritten destination mirrors Codex's ``overwritten_move_content``.
        """

        if not self._valid:
            return
        source = _normalized_path(old_path or new_path)
        destination = _normalized_path(new_path or old_path)
        if not source or not destination:
            self.invalidate()
            return

        if source != destination:
            self._apply_update(
                source,
                destination,
                old_content,
                overwritten_new_content,
                new_content,
            )
        elif old_content is None and new_content is not None:
            self._apply_add(source, new_content, overwritten_new_content)
        elif old_content is not None and new_content is None:
            self._apply_delete(source, old_content)
        elif old_content is not None and new_content is not None:
            self._apply_update(source, None, old_content, None, new_content)
        else:
            self.invalidate()
            return

        self._revision += 1
        self._refresh_unified_diff()

    def snapshot(self) -> TurnDiffSnapshot:
        return TurnDiffSnapshot(
            revision=self._revision,
            unified_diff=self._unified_diff,
        )

    def _tracked_content(self, content: str) -> _TrackedContent:
        tracked = _TrackedContent(content=content, revision=self._next_content_revision)
        self._next_content_revision += 1
        return tracked

    def _apply_add(
        self,
        path: str,
        content: str,
        overwritten_content: str | None,
    ) -> None:
        self._origin_by_current_path.pop(path, None)
        if (
            path not in self._current_by_path
            and path not in self._baseline_by_path
            and overwritten_content is not None
        ):
            self._baseline_by_path[path] = self._tracked_content(overwritten_content)
        self._current_by_path[path] = self._tracked_content(content)

    def _apply_delete(self, path: str, content: str) -> None:
        if self._current_by_path.pop(path, None) is None and path not in self._baseline_by_path:
            self._baseline_by_path[path] = self._tracked_content(content)
        self._origin_by_current_path.pop(path, None)

    def _apply_update(
        self,
        source_path: str,
        move_path: str | None,
        old_content: str | None,
        overwritten_move_content: str | None,
        new_content: str | None,
    ) -> None:
        if old_content is None or new_content is None:
            self.invalidate()
            return
        if source_path not in self._current_by_path and source_path not in self._baseline_by_path:
            self._baseline_by_path[source_path] = self._tracked_content(old_content)

        if move_path is not None:
            if (
                move_path not in self._current_by_path
                and move_path not in self._baseline_by_path
                and overwritten_move_content is not None
            ):
                self._baseline_by_path[move_path] = self._tracked_content(
                    overwritten_move_content
                )
            origin = self._origin_by_current_path.pop(source_path, source_path)
            self._current_by_path.pop(source_path, None)
            self._current_by_path[move_path] = self._tracked_content(new_content)
            self._origin_by_current_path.pop(move_path, None)
            if move_path != origin:
                self._origin_by_current_path[move_path] = origin
            return

        self._current_by_path[source_path] = self._tracked_content(new_content)

    def _rename_pairs(self) -> dict[str, str]:
        pairs: dict[str, str] = {}
        for destination, origin in self._origin_by_current_path.items():
            if (
                destination == origin
                or origin in self._current_by_path
                or destination not in self._current_by_path
                or origin not in self._baseline_by_path
                or destination in self._baseline_by_path
            ):
                continue
            pairs[origin] = destination
        return pairs

    def _refresh_unified_diff(self) -> None:
        if not self._valid:
            self._unified_diff = None
            return
        rename_pairs = self._rename_pairs()
        paired_destinations = set(rename_pairs.values())
        handled: set[str] = set()
        previous_diffs = self._rendered_diffs
        rendered_diffs: dict[_DiffCacheKey, str | None] = {}
        aggregate: list[str] = []

        paths = sorted(
            set(self._baseline_by_path) | set(self._current_by_path),
            key=_normalized_path,
        )
        for path in paths:
            if path in handled:
                continue
            handled.add(path)
            if path in paired_destinations:
                continue

            destination = rename_pairs.get(path, path)
            handled.add(destination)
            left_content = self._baseline_by_path.get(path)
            right_content = self._current_by_path.get(destination)
            key = _DiffCacheKey(
                left_path=path,
                left_revision=left_content.revision if left_content else None,
                right_path=destination,
                right_revision=right_content.revision if right_content else None,
            )
            if key in previous_diffs:
                rendered = previous_diffs[key]
            else:
                rendered = self._render_diff(
                    left_path=path,
                    left_content=left_content.content if left_content else None,
                    right_path=destination,
                    right_content=right_content.content if right_content else None,
                )
            rendered_diffs[key] = rendered
            if rendered:
                aggregate.append(rendered if rendered.endswith("\n") else f"{rendered}\n")

        self._rendered_diffs = rendered_diffs
        unified = "".join(aggregate)
        self._unified_diff = unified or None

    def _render_diff(
        self,
        *,
        left_path: str,
        left_content: str | None,
        right_path: str,
        right_content: str | None,
    ) -> str | None:
        # Codex suppresses a pure rename: the FileChange item owns path motion,
        # while turn.diff.updated represents text changes.
        if left_content == right_content:
            return None
        if left_content is None and right_content is None:
            return None

        self._rendered_diff_count += 1
        left_oid = ZERO_OID if left_content is None else _git_blob_oid(left_content)
        right_oid = ZERO_OID if right_content is None else _git_blob_oid(right_content)
        parts = [f"diff --git a/{left_path} b/{right_path}\n"]
        if left_content is None:
            parts.append(f"new file mode {REGULAR_FILE_MODE}\n")
        elif right_content is None:
            parts.append(f"deleted file mode {REGULAR_FILE_MODE}\n")
        parts.append(f"index {left_oid}..{right_oid}\n")

        old_label = DEV_NULL if left_content is None else f"a/{left_path}"
        new_label = DEV_NULL if right_content is None else f"b/{right_path}"
        left = left_content or ""
        right = right_content or ""
        if (
            len(left) + len(right) > DIFF_COARSE_CHAR_THRESHOLD
            or left.count("\n") + right.count("\n") > DIFF_COARSE_LINE_THRESHOLD
        ):
            parts.extend(self._coarse_unified_diff(left, right, old_label, new_label))
        else:
            unified = list(
                difflib.unified_diff(
                    left.splitlines(keepends=True),
                    right.splitlines(keepends=True),
                    fromfile=old_label,
                    tofile=new_label,
                    n=3,
                    lineterm="\n",
                )
            )
            parts.extend(self._with_no_newline_markers(unified))
        return "".join(parts)

    @staticmethod
    def _with_no_newline_markers(lines: list[str]) -> list[str]:
        output: list[str] = []
        for line in lines:
            if line.startswith(("+", "-", " ")) and not line.endswith(("\n", "\r")):
                output.append(f"{line}\n")
                output.append("\\ No newline at end of file\n")
            else:
                output.append(line)
        return output

    @staticmethod
    def _coarse_unified_diff(
        left: str,
        right: str,
        old_label: str,
        new_label: str,
    ) -> list[str]:
        """Content-exact whole-file fallback for pathological rewrites."""

        left_lines = left.splitlines(keepends=True)
        right_lines = right.splitlines(keepends=True)
        old_range = f"-1,{len(left_lines)}" if left_lines else "-0,0"
        new_range = f"+1,{len(right_lines)}" if right_lines else "+0,0"
        output = [f"--- {old_label}\n", f"+++ {new_label}\n", f"@@ {old_range} {new_range} @@\n"]
        for line in left_lines:
            output.append(f"-{line}")
            if not line.endswith(("\n", "\r")):
                output.append("\n\\ No newline at end of file\n")
        for line in right_lines:
            output.append(f"+{line}")
            if not line.endswith(("\n", "\r")):
                output.append("\n\\ No newline at end of file\n")
        return output


__all__ = ["TurnDiffSnapshot", "TurnDiffTracker"]
