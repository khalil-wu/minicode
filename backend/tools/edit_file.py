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
from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache
from backend.workspace.path_filters import is_windows_reserved_path


from backend.tools.file_tools_common import *  # shared helpers (validation/diff/cache/etc.)

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


def _normalize_quotes(text: str) -> str:
    return text.translate(_SMART_QUOTE_MAP)


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
        "Make targeted string replacements in an existing file. Read the file first, pass expected_hash, and make old_string match exactly without line-number prefixes. "
        "old_string must be unique unless replace_all is true; use write_file for mostly new content."
    )
    permission = PermissionLevel.DIFF_REVIEW
    workspace_path_fields = ("file_path",)

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

    def streamed_input_preview(self, args: dict[str, Any]) -> dict[str, Any]:
        file_path = args.get("file_path")
        return {"file_path": file_path} if isinstance(file_path, str) else {}

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.edit",
            required_args=("file_path", "old_string", "new_string"),
        )

    def get_schema(self) -> ToolSchema:
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

        bypass_mode = _is_bypass_mode(context)
        try:
            path = _resolve_path(file_path, context, allow_workspace_escape=bypass_mode)
        except PathTraversalError as exc:
            return self._error_result(str(exc))

        if is_sensitive_file(path) and not bypass_mode:
            return self._error_result(
                f"Refusing to edit sensitive file: {file_path}. "
                "Edit credential files manually outside the agent."
            )
        if is_protected_write_path(path) and not bypass_mode:
            return self._error_result(
                f"Refusing to edit protected path: {file_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        if not path.exists():
            return self._error_result(f"File does not exist: {file_path}")

        ok, message = _validate_expected_hash(path, args.get("expected_hash"))
        if not ok:
            return self._error_result(message)

        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._error_result(f"Cannot read binary or non-UTF-8 file: {file_path}")

        # Determine replacement mode.
        replace_all = args.get("replace_all", False)
        if isinstance(replace_all, str):
            replace_all = replace_all.strip().lower() in {"true", "1", "yes", "y", "on"}

        count = content.count(old_string)
        normalized_match: tuple[int, int] | None = None  # (start, length) in original content
        if count == 0:
            # Fallback (cc findActualString parity): tolerate smart/curly vs
            # straight quote differences, common in docs/markdown and macOS
            # auto-correct. Only for single-replace; require a unique match.
            if not replace_all:
                norm_content = _normalize_quotes(content)
                norm_old = _normalize_quotes(old_string)
                start = norm_content.find(norm_old)
                if start >= 0 and norm_content.find(norm_old, start + 1) == -1:
                    normalized_match = (start, len(old_string))
            if normalized_match is None:
                excerpt = _closest_edit_excerpt(content, old_string)
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

        # Perform the replacement.
        if normalized_match is not None:
            start, length = normalized_match
            new_content = content[:start] + new_string + content[start + length:]
        elif replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            new_content = content.replace(old_string, new_string, 1)

        try:
            _atomic_write_text(path, new_content)

            # Invalidate file caches after editing.
            cache = get_global_file_cache()
            cache.invalidate(path)
            clear_list_files_cache()
            await _emit_write_diff(
                context,
                file_path=file_path,
                old_content=content,
                new_content=new_content,
                display_path=_workspace_display_path(path, file_path, context),
            )
        except PermissionError:
            return self._error_result(f"No permission to write file: {file_path}")

        replaced_count = count if replace_all else 1
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

