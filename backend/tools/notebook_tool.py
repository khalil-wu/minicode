"""Notebook editing tool — edit Jupyter (.ipynb) cells.

Mirrors cc's NotebookEdit: replace / insert / delete a cell by index, without
requiring nbformat (a notebook is JSON with a `cells` array).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from backend.permissions.context import ToolExecutionContext
from backend.security.sensitive_files import is_protected_write_path, is_sensitive_file
from backend.tools.base import BaseTool, PermissionLevel, ToolResult, ToolSchema
from backend.tools.file_tools_common import _atomic_write_text, _validate_expected_hash, content_hash
from backend.tools.path_resolution import _is_bypass_mode, _resolve_path


class NotebookEditTool(BaseTool):
    """Edit a cell in a Jupyter notebook (.ipynb)."""

    name = "notebook_edit"
    result_kind = "edit"
    activity_kind = "fileChange"
    display_label = "Edit notebook"
    mutates_workspace = True
    read_only = False
    permission = PermissionLevel.CONFIRM
    workspace_path_fields = ("notebook_path",)
    description = (
        "Edit a Jupyter notebook (.ipynb) cell. Supports replace / insert / delete of a single cell "
        "by its 0-based index, or append when cell_index equals the cell count. Use for notebook "
        "users who need cell-level edits; for plain files prefer write_file/edit_file."
    )

    def get_schema(self) -> ToolSchema:
        return ToolSchema(
            name=self.name,
            description=self.description,
            parameters={
                "type": "object",
                "properties": {
                    "notebook_path": {"type": "string", "description": "Path to the .ipynb file."},
                    "cell_index": {"type": "integer", "description": "0-based cell index to replace/insert at/delete. Append when equal to cell count (replace/insert only)."},
                    "edit_mode": {"type": "string", "enum": ["replace", "insert", "delete"], "description": "replace (default), insert before cell_index, or delete cell_index."},
                    "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"], "description": "Cell type for replace/insert."},
                    "source": {"type": "string", "description": "New cell source (replace/insert). Markdown allowed for markdown cells."},
                },
                "required": ["notebook_path", "cell_index"],
            },
        )

    async def execute(self, args: dict[str, Any], context: ToolExecutionContext | None = None) -> ToolResult:
        raw_path = str(args.get("notebook_path") or "").strip()
        if not raw_path:
            return self._error_result("notebook_path is required")
        try:
            path = _resolve_path(raw_path, context, allow_workspace_escape=_is_bypass_mode(context))
        except Exception as exc:
            return self._error_result(f"Invalid notebook path: {exc}")
        if not path.exists():
            return self._error_result(f"Notebook not found: {raw_path}")
        if path.suffix.lower() != ".ipynb":
            return self._error_result(f"Not a notebook (.ipynb): {raw_path}")

        # Same write floor as write_file/edit_file: a notebook inside .git/ or
        # .claude/ can execute on open, so it must not be a way around the
        # protected-path and secret-file guards.
        bypass_mode = _is_bypass_mode(context)
        if is_sensitive_file(path) and not bypass_mode:
            return self._error_result(
                f"Refusing to write sensitive file: {raw_path}. "
                "Edit credential files manually outside the agent."
            )
        if is_protected_write_path(path) and not bypass_mode:
            return self._error_result(
                f"Refusing to write protected path: {raw_path}. "
                "Repository and agent configuration files must be edited manually."
            )

        # Read-before-edit + staleness guard (mirrors edit_file.py). Claude Code
        # tracks notebook freshness with a harness readFileState map rather than a
        # model-supplied hash; we use the content hash read_file recorded in
        # context metadata for the same purpose. Honor an explicit expected_hash
        # if the model passed one, otherwise fall back to the recorded read-time
        # hash so a notebook read this session is editable without the model
        # echoing the hash back (edit_file/write_file get the same hash injected
        # via generate_diff; notebook_edit resolves it here instead).
        expected_hash = args.get("expected_hash")
        if not expected_hash and context is not None and isinstance(getattr(context, "metadata", None), dict):
            read_time_hashes = context.metadata.get("_read_file_hashes")
            if isinstance(read_time_hashes, dict):
                expected_hash = read_time_hashes.get(str(path))
        ok, message = _validate_expected_hash(path, expected_hash)
        if not ok:
            return self._error_result(message)

        try:
            nb = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return self._error_result(f"Failed to read notebook JSON: {exc}")
        cells = nb.get("cells")
        if not isinstance(cells, list):
            return self._error_result("Notebook has no cells array")

        edit_mode = str(args.get("edit_mode") or "replace").strip().lower()
        try:
            idx = int(args.get("cell_index"))  # may be None → ValueError
        except (TypeError, ValueError):
            return self._error_result("cell_index must be an integer")

        if edit_mode == "delete":
            if not 0 <= idx < len(cells):
                return self._error_result(f"cell_index {idx} out of range (0..{len(cells) - 1})")
            cells.pop(idx)
            action = f"Deleted cell {idx}"
        else:
            cell_type = str(args.get("cell_type") or "code").strip().lower()
            if cell_type not in {"code", "markdown", "raw"}:
                return self._error_result(f"Invalid cell_type '{cell_type}'")
            source = args.get("source")
            if source is None:
                return self._error_result("source is required for replace/insert")
            source_lines = str(source).splitlines(keepends=True)
            new_cell = {"cell_type": cell_type, "metadata": {}, "source": source_lines}
            if cell_type == "code":
                new_cell["execution_count"] = None
                new_cell["outputs"] = []
            if edit_mode == "insert":
                if not 0 <= idx <= len(cells):
                    return self._error_result(f"cell_index {idx} out of range (0..{len(cells)})")
                cells.insert(idx, new_cell)
                action = f"Inserted {cell_type} cell at {idx}"
            else:  # replace
                if not 0 <= idx <= len(cells):
                    return self._error_result(f"cell_index {idx} out of range (0..{len(cells)})")
                if idx == len(cells):
                    cells.append(new_cell)
                    action = f"Appended {cell_type} cell"
                else:
                    cells[idx] = new_cell
                    action = f"Replaced cell {idx} with {cell_type}"

        nb["cells"] = cells
        try:
            new_text = json.dumps(nb, ensure_ascii=False, indent=1)
            _atomic_write_text(path, new_text)
        except Exception as exc:
            return self._error_result(f"Failed to write notebook: {exc}")

        return self._success_result(
            content=f"{action} in {raw_path} ({len(cells)} cells). content_hash: {content_hash(new_text)}",
            display_summary=action,
        )
