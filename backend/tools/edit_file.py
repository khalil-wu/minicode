"""EditFileTool (extracted from file_tools.py)."""
from __future__ import annotations

import difflib
import asyncio
import os
import tempfile
import time
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from backend.artifact.store import ArtifactStore
from backend.atomic_io import file_mutation_locks
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.path_filters import is_windows_reserved_path


from backend.tools.file_tools_common import (
    _atomic_write_text,
    _emit_write_diff,
    _generate_limited_unified_diff,
    _path_arg,
    _validate_expected_hash,
    _validate_path_arg_type,
    _validate_text_arg,
    _workspace_display_path,
    content_hash,
    invalidate_workspace_file_caches,
)

# Smart/curly quote → straight ASCII, for cc-style findActualString leniency.
# Only used as a FALLBACK when the exact old_string is not found, so exact
# matching (and its replace_all semantics) is unchanged when it succeeds.
_SMART_QUOTE_MAP = str.maketrans(
    {
        "‘": "'",  # left single
        "’": "'",  # right single
        "‚": "'",  # single low-9
        "‛": "'",  # single reversed
        "“": '"',  # left double
        "”": '"',  # right double
        "„": '"',  # double low-9
        "‟": '"',  # double reversed
        "＇": "'",  # fullwidth apostrophe
        "＂": '"',  # fullwidth quotation mark
    }
)

_LEFT_SINGLE_QUOTE = "‘"
_RIGHT_SINGLE_QUOTE = "’"
_LEFT_DOUBLE_QUOTE = "“"
_RIGHT_DOUBLE_QUOTE = "”"


def _normalize_quotes(text: str) -> str:
    return text.translate(_SMART_QUOTE_MAP)


def _is_opening_quote_context(chars: list[str], index: int) -> bool:
    if index == 0:
        return True
    return chars[index - 1] in {" ", "\t", "\n", "\r", "(", "[", "{", "—", "–"}


def _apply_curly_double_quotes(text: str) -> str:
    chars = list(text)
    result: list[str] = []
    for index, char in enumerate(chars):
        if char == '"':
            result.append(_LEFT_DOUBLE_QUOTE if _is_opening_quote_context(chars, index) else _RIGHT_DOUBLE_QUOTE)
        else:
            result.append(char)
    return "".join(result)


def _apply_curly_single_quotes(text: str) -> str:
    chars = list(text)
    result: list[str] = []
    for index, char in enumerate(chars):
        if char != "'":
            result.append(char)
            continue
        previous = chars[index - 1] if index > 0 else None
        following = chars[index + 1] if index + 1 < len(chars) else None
        if previous is not None and following is not None and previous.isalpha() and following.isalpha():
            result.append(_RIGHT_SINGLE_QUOTE)
        else:
            result.append(
                _LEFT_SINGLE_QUOTE
                if _is_opening_quote_context(chars, index)
                else _RIGHT_SINGLE_QUOTE
            )
    return "".join(result)


def _preserve_quote_style(old_string: str, actual_old_string: str, new_string: str) -> str:
    """Match CC's quote-normalization fallback without changing typography."""
    if old_string == actual_old_string:
        return new_string

    has_double_quotes = any(char in actual_old_string for char in (_LEFT_DOUBLE_QUOTE, _RIGHT_DOUBLE_QUOTE))
    has_single_quotes = any(char in actual_old_string for char in (_LEFT_SINGLE_QUOTE, _RIGHT_SINGLE_QUOTE))
    if has_double_quotes:
        new_string = _apply_curly_double_quotes(new_string)
    if has_single_quotes:
        new_string = _apply_curly_single_quotes(new_string)
    return new_string


def _normalized_quote_matches(content: str, old_string: str) -> list[tuple[int, int]]:
    """Return original-content spans for CC-compatible quote matches.

    The supported quote substitutions are one Unicode code point to one
    Unicode code point, so offsets in normalized and original content remain
    aligned. Returning spans lets replace_all preserve each occurrence's
    original typography instead of rewriting the whole file into normalized
    text.
    """
    normalized_content = _normalize_quotes(content)
    normalized_old = _normalize_quotes(old_string)
    if not normalized_old:
        return []

    matches: list[tuple[int, int]] = []
    cursor = 0
    while True:
        start = normalized_content.find(normalized_old, cursor)
        if start < 0:
            break
        matches.append((start, len(old_string)))
        cursor = start + len(normalized_old)
    return matches


def _closest_edit_excerpt(content: str, old_string: str) -> str:
    """Return nearby real file text so an exact-match failure is recoverable.

    This does not perform a fuzzy replacement. It only gives the model a small
    deterministic excerpt from the current file so the next edit can use exact,
    freshly observed content instead of entering a read/edit retry loop.
    """

    lines = content.splitlines()
    requested = old_string.splitlines()
    if not lines or not requested:
        return ""

    anchor = next((line for line in requested if line.strip()), requested[0])
    normalized_anchor = _normalize_quotes(anchor).strip()
    candidate_index = -1
    for index, line in enumerate(lines):
        if _normalize_quotes(line).strip() == normalized_anchor:
            candidate_index = index
            break

    if candidate_index < 0 and normalized_anchor:
        best_ratio = 0.0
        for index, line in enumerate(lines[:20_000]):
            candidate = _normalize_quotes(line).strip()
            if not candidate:
                continue
            ratio = difflib.SequenceMatcher(None, normalized_anchor, candidate).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                candidate_index = index
        if best_ratio < 0.35:
            candidate_index = -1

    if candidate_index < 0:
        return ""

    excerpt_start = max(0, candidate_index - 1)
    excerpt_size = min(12, max(4, len(requested) + 2))
    excerpt_end = min(len(lines), excerpt_start + excerpt_size)
    excerpt = "\n".join(lines[excerpt_start:excerpt_end])
    return (
        f"Closest current file excerpt (lines {excerpt_start + 1}-{excerpt_end}):\n"
        f"{excerpt}\n"
        "Retry once with a smaller old_string copied exactly from this excerpt."
    )


class EditFileTool(BaseTool):
    """
    Replace one exact string in a text file.

    old_string must appear exactly once.
    """

    name = "edit_file"
    mutates_workspace = True
    result_kind = "edit"
    activity_kind = "fileChange"
    display_label = "Edit"
    description = (
        "Make targeted string replacements in an existing file. Read the file first so the harness can inject its read-time guard, and make old_string match exactly without line-number prefixes. "
        "old_string must be unique unless replace_all is true; use write_file for mostly new content."
    )
    permission = PermissionLevel.DIFF_REVIEW
    workspace_path_fields = ("file_path",)

    def check_permission(self, args=None, context=None):
        if context is not None and context.mode == "plan":
            from backend.agent.plans import is_current_plan_file

            return (
                PermissionLevel.AUTO
                if is_current_plan_file(_path_arg(args or {}), context)
                else PermissionLevel.ALWAYS_DENY
            )
        return None

    def is_capability_available(self, context=None) -> bool:
        return context is None or context.mode == "plan" or super().is_capability_available(context)

    def capability_permission_level(self, context=None):
        if context is not None and context.mode == "plan":
            return PermissionLevel.AUTO
        return self.permission

    def model_description(self) -> str:
        return (
            "Make targeted string replacements in an existing file with exact old_string matching."
        )

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    # Without this the model must issue one call per occurrence
                    # for a rename. expected_hash stays out of the model-facing
                    # schema because the runtime injects the read-time hash.
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence instead of requiring old_string to be unique. Default false.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        )

    def streamed_input_preview(
        self,
        args: dict[str, Any],
        context: Any | None = None,
        prior: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        file_path = args.get("file_path")
        if not isinstance(file_path, str):
            return {}
        preview: dict[str, Any] = {"file_path": file_path}
        old_string = args.get("old_string")
        new_string = args.get("new_string")
        if not isinstance(old_string, str) or not isinstance(new_string, str):
            return preview
        # The replacement pair is complete enough to show a live +/- count
        # before the call commits. SequenceMatcher is the same algorithm the
        # committed unified diff uses, so the live badge converges to the
        # final region counts.
        old_lines = old_string.splitlines()
        new_lines = new_string.splitlines()
        plus = minus = 0
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
        for tag, i1, i2, _j1, j2 in matcher.get_opcodes():
            if tag in {"replace", "delete"}:
                minus += i2 - i1
            if tag in {"replace", "insert"}:
                plus += j2 - _j1
        if plus or minus:
            preview["diff"] = {"plus": plus, "minus": minus}
        return preview

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.edit",
            required_args=("file_path", "old_string", "new_string"),
        )

    def get_schema(self) -> ToolSchema:
        """Host-facing alias retained for direct callers; the model never sees it."""
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or workspace-relative file path.",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "Exact original text; must appear once unless replace_all is true.",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                    "expected_hash": {
                        "type": "string",
                        "description": "Latest content_hash from read_file.",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every old_string occurrence; default false.",
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
            strict=True,
        )

    def get_execution_schema(self) -> ToolSchema:
        parameters = dict(self.model_schema().parameters)
        properties = dict(parameters.get("properties") or {})
        properties["expected_hash"] = {
            "type": "string",
            "description": "Runtime-owned read-time hash; injected by the harness.",
        }
        properties["replace_all"] = {
            "type": "boolean",
            "description": "Replace every old_string occurrence; default false.",
        }
        parameters["properties"] = properties
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters=parameters,
            strict=True,
        )

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        args = args or {}
        return (
            _validate_path_arg_type(args)
            or _validate_text_arg(args, "old_string", role="the exact text to replace")
            or _validate_text_arg(args, "new_string", role="the replacement text")
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        file_path = _path_arg(args)
        old_string = args.get("old_string", "")
        new_string = args.get("new_string", "")

        if not file_path:
            return self._error_result("Missing file_path argument")
        if not old_string:
            return self._error_result("Missing old_string argument")
        if old_string == new_string:
            return self._error_result(
                "No changes to make: old_string and new_string are exactly the same."
            )

        bypass_mode = _is_bypass_mode(context)
        try:
            path = _resolve_path(
                file_path,
                context,
                allow_workspace_escape=bypass_mode,
                allow_current_plan_file=True,
            )
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        # Protected paths stay guarded even in bypass mode.
        if is_protected_write_path(path):
            return self._error_result(
                f"Refusing to edit protected path: {file_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        if not path.exists():
            return self._error_result(f"File does not exist: {file_path}")
        if path.is_symlink():
            from backend.agent.plans import is_current_plan_file

            if is_current_plan_file(path, context):
                return self._error_result(f"Refusing to edit a plan-file symlink: {file_path}")

        ok, message = _validate_expected_hash(path, args.get("expected_hash"))
        if not ok:
            return self._error_result(message)

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._error_result(f"Cannot read binary or non-UTF-8 file: {file_path}")

        # Pi strips an invisible UTF-8 BOM before matching because the model
        # will not include it in old_string, then restores it on write. Keep
        # the raw content for hashes/diffs while performing all match offsets
        # against the BOM-free body.
        bom = "\ufeff" if content.startswith("\ufeff") else ""
        match_content = content[len(bom):]

        # Determine replacement mode.
        replace_all = args.get("replace_all", False)
        if isinstance(replace_all, str):
            replace_all = replace_all.strip().lower() in {"true", "1", "yes", "y", "on"}

        count = match_content.count(old_string)
        normalized_matches: list[tuple[int, int]] = []  # (start, length) in original content
        if count == 0:
            # Fallback (cc findActualString parity): tolerate smart/curly vs
            # straight quote differences, common in docs/markdown and macOS
            # auto-correct. Preserve each actual occurrence's quote style.
            normalized_matches = _normalized_quote_matches(match_content, old_string)
            if normalized_matches and not replace_all and len(normalized_matches) > 1:
                return self._error_result(
                    f"old_string matched {len(normalized_matches)} places in {file_path}. "
                    "Provide more surrounding context so it matches exactly once, or use replace_all=true."
                )
            if not normalized_matches:
                excerpt = _closest_edit_excerpt(match_content, old_string)
                diagnostic = f"\n{excerpt}" if excerpt else ""
                return self._error_result(
                    f"old_string was not found in {file_path}. "
                    "Make sure whitespace and line endings match exactly "
                    "(watch for smart/curly quotes vs straight quotes). "
                    f"Current content_hash: {content_hash(content)}."
                    f"{diagnostic}"
                )
        elif not replace_all and count > 1:
            return self._error_result(
                f"old_string matched {count} places in {file_path}. "
                "Provide more surrounding context so it matches exactly once, or use replace_all=true."
            )

        # Perform the replacement. Quote-normalized matches are assembled from
        # original slices so CRLF/BOM/typography outside the target stays
        # untouched, including when replace_all is requested.
        if normalized_matches:
            replacement_spans = normalized_matches if replace_all else normalized_matches[:1]
            chunks: list[str] = []
            cursor = 0
            for start, length in replacement_spans:
                chunks.append(match_content[cursor:start])
                actual_old_string = match_content[start : start + length]
                chunks.append(_preserve_quote_style(old_string, actual_old_string, new_string))
                cursor = start + length
            chunks.append(match_content[cursor:])
            new_content = bom + "".join(chunks)
        elif replace_all:
            new_content = bom + match_content.replace(old_string, new_string)
        else:
            new_content = bom + match_content.replace(old_string, new_string, 1)

        if new_content == content:
            return self._error_result(
                f"No changes made to {file_path}: the replacement produced identical content."
            )

        try:
            # Recheck freshness while holding the process-wide same-file queue.
            # The first check protects the review flow; this second check makes
            # the check-and-replace commit indivisible across sessions and the
            # workspace editor API (Pi file-mutation-queue / CC critical-section
            # semantics). No await is allowed before this lock is released.
            with file_mutation_locks([path]):
                ok, message = _validate_expected_hash(path, args.get("expected_hash"))
                if not ok:
                    return self._error_result(message)
                _atomic_write_text(path, new_content)

                # Invalidate file caches before another queued mutation can
                # observe the newly committed file through a stale cache.
                cache = get_global_file_cache()
                cache.invalidate(path)
                invalidate_workspace_file_caches()
        except PermissionError:
            return self._error_result(f"No permission to write file: {file_path}")

        from backend.agent.plans import is_current_plan_file
        if not is_current_plan_file(path, context):
            await _emit_write_diff(
                context,
                file_path=file_path,
                old_content=content,
                new_content=new_content,
                display_path=_workspace_display_path(path, file_path, context),
            )

        replaced_count = (
            len(normalized_matches) if normalized_matches and replace_all else count if replace_all else 1
        )
        _, additions, deletions, _ = _generate_limited_unified_diff(
            content,
            new_content,
            file_path,
            max_chars=0,
        )
        return self._success_result(
            f"Edited {file_path}: replaced {len(old_string)} chars with {len(new_string)} chars in {replaced_count} occurrence(s). "
            f"Diff stats: +{additions} -{deletions}. content_hash: {content_hash(new_content)}"
        )
