"""ApplyPatchTool — Codex-compatible multi-file patch tool.

Applies an OpenAI Codex `apply_patch` envelope: one tool call can add, update,
delete, and rename multiple files atomically-per-file. This is Codex's signature
editing primitive; models trained on its grammar emit patches in this exact
format. See apply_patch_parser.py for the envelope spec.

Reuses the same path resolution, sensitive/protected-file guards, atomic write,
and diff-preview machinery as write_file/edit_file so security and UI behaviour
stay consistent across all file-mutating tools.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.tools.apply_patch_parser import (
    ApplyPatchError,
    ChangeKind,
    FileChange,
    apply_update_hunks,
    parse_patch,
)
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.path_resolution import PathTraversalError, _is_bypass_mode, _resolve_path
from backend.workspace.file_state_cache import get_global_file_cache

from backend.tools.file_tools_common import *  # shared helpers (diff/cache/atomic write/etc.)


class ApplyPatchTool(BaseTool):
    """Apply a Codex-style patch envelope across one or more files."""

    name = "apply_patch"
    mutates_workspace = True
    timeout_seconds = 30.0
    result_kind = "edit"
    activity_kind = "fileChange"
    display_label = "Apply patch"
    panel_hint = "diff"
    search_hint = "patch diff apply_patch edit multi-file rename"
    description = (
        "Apply a Codex patch envelope to add, update, delete, or rename files in one call. "
        "Prefer for multi-file edits/renames; use edit_file for one targeted replacement and write_file for whole new files.\n\n"
        "patch must be one string starting with '*** Begin Patch' and ending with '*** End Patch'. "
        "Use hunks like '*** Add File:', '*** Update File:', optional '*** Move to:', and '*** Delete File:'. "
        "In update hunks, prefix context with space, removals with '-', additions with '+'. "
        "Read files first so context/removal lines match exactly; do not wrap in JSON or Markdown fences."
    )
    permission = PermissionLevel.DIFF_REVIEW

    def model_description(self) -> str:
        return "Apply a Codex patch envelope for multi-file edits or renames."

    def model_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.model_description(),
            parameters={
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                    },
                },
                "required": ["patch"],
            },
            strict=True,
        )

    def get_spec(self):
        from backend.tools.contracts import ToolSpec

        return ToolSpec(
            name=self.name,
            capability="workspace.edit",
            required_args=("patch",),
            arg_roles={"patch": "generated_content"},
            arg_sources={"patch": ("model_generation",)},
            repair_policy={"patch": "needs_model_generation"},
            accepted_resource_types=("workspace_file",),
            rejected_resource_types=("uploaded_document", "web_url"),
            empty_args_policy="repair_or_block",
            blocked_guidance=(
                "apply_patch requires a complete patch envelope in the 'patch' argument "
                "(*** Begin Patch ... *** End Patch). Read the target files, generate the "
                "full patch, then retry."
            ),
        )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Full patch envelope as one string.",
                    },
                },
                "required": ["patch"],
            },
            strict=True,
        )

    def validate_input(self, args: dict[str, Any] | None = None) -> str:
        args = args or {}
        patch = args.get("patch")
        if not isinstance(patch, str) or not patch.strip():
            return "apply_patch requires a non-empty 'patch' string."
        return ""

    @staticmethod
    def _partial_apply_message(reason: str, committed_paths: list[str]) -> str:
        """Build an error message that names files already written before the
        failure, so the model knows the workspace is partially patched (apply_patch
        is atomic per-file, not across files)."""
        base = f"{reason} (apply_patch is atomic per-file, not across files)."
        if committed_paths:
            listing = ", ".join(committed_paths)
            return f"{base} The following file(s) were ALREADY written before the failure: {listing}. Re-read them to see the real workspace state before retrying."
        return base

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        patch_text = args.get("patch", "")
        if not isinstance(patch_text, str) or not patch_text.strip():
            return self._error_result("Missing patch argument")

        plans_or_error = self.plan_changes(patch_text, context)
        if isinstance(plans_or_error, str):
            return self._error_result(plans_or_error)
        plans = plans_or_error
        expected_hashes = args.get("_expected_hashes")
        if not isinstance(expected_hashes, dict):
            # Fail closed for direct/bypass execute paths: still prefer
            # read-time hashes when available, otherwise snapshot current disk.
            expected_hashes = self._snapshot_expected_hashes(plans, context)
            args["_expected_hashes"] = expected_hashes
        stale_error = self._review_snapshot_error(plans, expected_hashes)
        if stale_error:
            return self._error_result(stale_error)

        # Phase 2: emit a combined diff preview, then write.
        cache = get_global_file_cache()
        summary_lines: list[str] = []
        total_add = 0
        total_del = 0
        committed_paths: list[str] = []
        try:
            for plan in plans:
                await self._emit_plan_preview(plan, context)
            for plan in plans:
                adds, dels = self._commit_plan(plan, cache)
                total_add += adds
                total_del += dels
                summary_lines.append(plan.summary(adds, dels))
                committed_paths.append(plan.raw_path)
        except (PermissionError, OSError) as exc:
            reason = (
                f"No permission to write: {exc}"
                if isinstance(exc, PermissionError)
                else f"I/O error while writing: {exc}"
            )
            return self._error_result(self._partial_apply_message(reason, committed_paths))
        except ApplyPatchError as exc:
            return self._error_result(self._partial_apply_message(str(exc), committed_paths))

        clear_list_files_cache()
        header = (
            f"Applied patch to {len(plans)} file(s): +{total_add} -{total_del}."
        )
        return self._success_result("\n".join([header, *summary_lines]))

    def plan_changes(
        self,
        patch_text: str,
        context: ToolExecutionContext | None = None,
    ) -> "list[_ChangePlan] | str":
        try:
            changes = parse_patch(patch_text)
        except ApplyPatchError as exc:
            return str(exc)

        bypass_mode = _is_bypass_mode(context)
        plans: list[_ChangePlan] = []
        claimed_paths: set[Path] = set()
        for change in changes:
            plan_or_error = self._plan_change(change, context, bypass_mode)
            if isinstance(plan_or_error, str):
                return plan_or_error
            plan_paths = {plan_or_error.path.resolve()}
            if plan_or_error.move_to_path is not None:
                plan_paths.add(plan_or_error.move_to_path.resolve())
            if claimed_paths & plan_paths:
                return f"Patch targets the same path more than once: {change.path}"
            claimed_paths.update(plan_paths)
            plans.append(plan_or_error)
        return plans

    # --- planning -----------------------------------------------------------

    def _plan_change(
        self,
        change: FileChange,
        context: ToolExecutionContext | None,
        bypass_mode: bool,
    ) -> "_ChangePlan | str":
        try:
            path = _resolve_path(change.path, context, allow_workspace_escape=bypass_mode)
        except PathTraversalError as exc:
            return str(exc)

        guard = self._guard_path(change.path, path, bypass_mode)
        if guard:
            return guard

        if change.kind == ChangeKind.ADD:
            if path.exists():
                return f"Add File '{change.path}': file already exists. Use Update File instead."
            new_content = change.new_content or ""
            return _ChangePlan(
                kind=ChangeKind.ADD,
                raw_path=change.path,
                path=path,
                old_content="",
                new_content=new_content,
                display_path=_workspace_display_path(path, change.path, context),
            )

        if change.kind == ChangeKind.DELETE:
            if not path.exists():
                return f"Delete File '{change.path}': file does not exist."
            try:
                old_content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                return f"Delete File '{change.path}': cannot read binary or non-UTF-8 file."
            return _ChangePlan(
                kind=ChangeKind.DELETE,
                raw_path=change.path,
                path=path,
                old_content=old_content,
                new_content="",
                display_path=_workspace_display_path(path, change.path, context),
            )

        # Update (possibly with rename).
        if not path.exists():
            return f"Update File '{change.path}': file does not exist."
        try:
            old_content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return f"Update File '{change.path}': cannot read binary or non-UTF-8 file."

        try:
            new_content = apply_update_hunks(old_content, change.hunks, change.path)
        except ApplyPatchError as exc:
            return str(exc)

        dest_path = path
        dest_raw = change.path
        if change.move_to:
            try:
                dest_path = _resolve_path(change.move_to, context, allow_workspace_escape=bypass_mode)
            except PathTraversalError as exc:
                return str(exc)
            move_guard = self._guard_path(change.move_to, dest_path, bypass_mode)
            if move_guard:
                return move_guard
            if dest_path != path and dest_path.exists():
                return f"Move to '{change.move_to}': destination already exists."
            dest_raw = change.move_to

        return _ChangePlan(
            kind=ChangeKind.UPDATE,
            raw_path=change.path,
            path=path,
            old_content=old_content,
            new_content=new_content,
            display_path=_workspace_display_path(dest_path, dest_raw, context),
            move_to_path=dest_path if change.move_to else None,
        )

    @staticmethod
    def _review_snapshot_error(plans: list["_ChangePlan"], expected_hashes: Any) -> str:
        if not isinstance(expected_hashes, dict):
            return ""
        for plan in plans:
            for path in filter(None, (plan.path, plan.move_to_path)):
                expected = str(expected_hashes.get(str(path.resolve())) or "")
                if expected and expected != _path_snapshot_hash(path):
                    return f"File changed on disk after review: {plan.raw_path}. Re-read and retry."
        return ""

    @staticmethod
    def _snapshot_expected_hashes(
        plans: list["_ChangePlan"],
        context: ToolExecutionContext | None,
        *,
        read_time_hashes: dict[str, str] | None = None,
    ) -> dict[str, str]:
        """Prefer read-time content hashes; fall back to current on-disk snapshot."""
        meta = context.metadata if (context is not None and isinstance(context.metadata, dict)) else {}
        hashes = read_time_hashes if isinstance(read_time_hashes, dict) else meta.get("_read_file_hashes")
        if not isinstance(hashes, dict):
            hashes = {}
        expected: dict[str, str] = {}
        for plan in plans:
            for path in filter(None, (plan.path, plan.move_to_path)):
                key = str(path.resolve())
                expected[key] = str(hashes.get(key) or _path_snapshot_hash(path))
        return expected

    def _guard_path(self, raw_path: str, path: Path, bypass_mode: bool) -> str:
        if bypass_mode:
            return ""
        if is_sensitive_file(path):
            return (
                f"Refusing to modify sensitive file: {raw_path}. "
                "Edit credential files manually outside the agent."
            )
        if is_protected_write_path(path):
            return (
                f"Refusing to modify protected path: {raw_path}. "
                "Repository and agent configuration files must be edited manually."
            )
        return ""

    # --- preview + commit ---------------------------------------------------

    async def _emit_plan_preview(self, plan: "_ChangePlan", context: ToolExecutionContext | None) -> None:
        if plan.kind == ChangeKind.DELETE:
            return  # deletes have no new content to preview incrementally
        await _emit_write_preview_progress(
            context,
            file_path=plan.raw_path,
            old_content=plan.old_content,
            new_content=plan.new_content,
            display_path=plan.display_path,
        )

    def _commit_plan(self, plan: "_ChangePlan", cache: Any) -> tuple[int, int]:
        if plan.kind == ChangeKind.DELETE:
            try:
                plan.path.unlink()
            except FileNotFoundError:
                pass
            cache.invalidate(plan.path)
            _, adds, dels = self._diff_stats(plan.old_content, "")
            return adds, dels

        if plan.kind == ChangeKind.ADD:
            plan.path.parent.mkdir(parents=True, exist_ok=True)

        target = plan.move_to_path or plan.path
        if plan.move_to_path is not None:
            plan.move_to_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, plan.new_content)
        cache.invalidate(plan.path)
        if plan.move_to_path is not None and plan.move_to_path != plan.path:
            # Rename: remove the original after writing the destination.
            try:
                plan.path.unlink()
            except FileNotFoundError:
                pass
            cache.invalidate(plan.move_to_path)
        _, adds, dels = self._diff_stats(plan.old_content, plan.new_content)
        return adds, dels

    @staticmethod
    def _diff_stats(old_content: str, new_content: str) -> tuple[str, int, int]:
        patch, additions, deletions, _ = _generate_limited_unified_diff(
            old_content,
            new_content,
            "file",
            max_chars=0,
        )
        return patch, additions, deletions


class _ChangePlan:
    """A validated, ready-to-write change resolved against the filesystem."""

    __slots__ = (
        "kind",
        "raw_path",
        "path",
        "old_content",
        "new_content",
        "display_path",
        "move_to_path",
    )

    def __init__(
        self,
        *,
        kind: ChangeKind,
        raw_path: str,
        path: Path,
        old_content: str,
        new_content: str,
        display_path: str,
        move_to_path: Path | None = None,
    ) -> None:
        self.kind = kind
        self.raw_path = raw_path
        self.path = path
        self.old_content = old_content
        self.new_content = new_content
        self.display_path = display_path
        self.move_to_path = move_to_path

    def summary(self, adds: int, dels: int) -> str:
        if self.kind == ChangeKind.ADD:
            return f"  A {self.display_path} (+{adds})"
        if self.kind == ChangeKind.DELETE:
            return f"  D {self.raw_path} (-{dels})"
        if self.move_to_path is not None:
            return f"  R {self.raw_path} -> {self.display_path} (+{adds} -{dels})"
        return f"  M {self.display_path} (+{adds} -{dels})"


def build_apply_patch_diff_payload(
    patch_text: str,
    context: ToolExecutionContext | None = None,
    *,
    expected_hashes: dict[str, str] | None = None,
    read_time_hashes: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    from backend.permissions.review import build_structured_diff_payload, generate_unified_diff

    plans = ApplyPatchTool().plan_changes(patch_text, context)
    if isinstance(plans, str):
        return None
    if expected_hashes is not None:
        expected_hashes.update(
            ApplyPatchTool._snapshot_expected_hashes(
                plans,
                context,
                read_time_hashes=read_time_hashes,
            )
        )

    files: list[dict[str, Any]] = []
    additions = 0
    deletions = 0
    for plan in plans:
        status = (
            "added" if plan.kind == ChangeKind.ADD
            else "deleted" if plan.kind == ChangeKind.DELETE
            else "renamed" if plan.move_to_path is not None
            else "modified"
        )
        patch = generate_unified_diff(plan.display_path, plan.old_content, plan.new_content)
        if plan.move_to_path is not None and not patch.strip():
            patch = f"--- a/{plan.raw_path}\n+++ b/{plan.display_path}\n"
        payload = build_structured_diff_payload(
            plan.display_path,
            patch,
            status=status,
            old_path=plan.raw_path if plan.move_to_path is not None else None,
            size_bytes=len(plan.new_content.encode("utf-8")),
        )
        if payload.get("format") != "structured":
            continue
        entry = payload["files"][0]
        files.append(entry)
        additions += int(entry.get("additions") or 0)
        deletions += int(entry.get("deletions") or 0)
    if not files:
        return None
    return {
        "format": "structured",
        "stats": {
            "files_count": len(files),
            "additions": additions,
            "deletions": deletions,
        },
        "files": files,
    }


def _path_snapshot_hash(path: Path) -> str:
    if not path.exists():
        return "missing"
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"
